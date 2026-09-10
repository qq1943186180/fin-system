# -*- coding: utf-8 -*-
"""补收历史发票：按限量、从旧到新，收取被水位线跳过的发票邮件。

背景
----
邮箱水位线 ``last_uid`` 只增不减，一旦被推到顶部（本项目 2026-09 实测
``last_uid=1691478373`` = INBOX 倒数第二封），则所有更旧的发票邮件对正常增量
轮询**永久不可见**。2023-09 ~ 2026-09 共 66 封发票邮件处于该状态。

设计要点（照抄 P7「改生产数据类工具」的安全清单）
------------------------------------------------
1. **默认只读**（dry-run）：只连邮箱列清单，不 OCR、不入库、不改配置。
2. ``--limit N`` 限量，默认 3 —— 试跑用。
3. **从旧到新**（uid 升序）：否则水位线一路推进会把中间的老邮件永久跳过。
4. 🔴 **绝不修改 ``last_uid``**：补收是**旁路**，与正常增量互不干扰。
   幂等性由 ``register_invoice`` 的发票号唯一约束保证（重复票返回
   ``BUSINESS_REJECTED``，不会写坏数据）。
5. ``--apply`` 前**强制在线备份**（``db.backup_database``，含 integrity_check）。
6. **只读方式打开 IMAP 文件夹 + 显式 ``mark_seen=False``**，绝不改邮箱已读状态。
7. 仅处理 ``uid <= last_uid`` 的邮件（即"被水位线跳过的"），不碰正常增量的新邮件。
8. 逐封独立处理，一封失败不影响其余；明细写 JSON 报告便于复查。

用法
----
    # 只读预览（安全，随时可跑）
    python tools/backfill_history_invoices.py --limit 3

    # 真实补收（会写库，先自动备份）
    python tools/backfill_history_invoices.py --limit 3 --apply

    # 换个顺序看最新几封（只读预览用，一般不要用它 --apply）
    python tools/backfill_history_invoices.py --limit 5 --order newest
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from modules.integration import IntegrationStatus  # noqa: E402
from modules.mail_processor import process_attachment  # noqa: E402
from modules.mail_service import (  # noqa: E402
    MailBoxConfig,
    _match_sender,
    _match_subject,
    _match_subject_regex,
)
from security import get_secret  # noqa: E402
from tools.invoice_ocr_probe import load_agent_command, probe_bytes  # noqa: E402

from logging_setup import get_logger  # noqa: E402

LOG = get_logger("tools.backfill_history_invoices")

# 业务性拒绝（非发票 / 已入库）不算失败
BENIGN = {IntegrationStatus.BUSINESS_REJECTED}


# ---------------------------------------------------------------- 配置


def config_path() -> Path:
    try:
        from app_paths import CONFIG_DIR

        return Path(CONFIG_DIR) / "settings.json"
    except Exception:
        return Path(__file__).resolve().parents[1] / "config" / "settings.json"


def load_boxes(path: Path | None = None) -> list[MailBoxConfig]:
    """读取所有【已启用】的邮箱配置。"""
    p = path or config_path()
    if not p.exists():
        raise FileNotFoundError(f"配置文件不存在: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    boxes = []
    for raw in data.get("mail_boxes", []) or []:
        cfg = MailBoxConfig.from_dict(raw)
        if cfg.enabled:
            boxes.append(cfg)
    return boxes


def mailbox_password(cfg: MailBoxConfig) -> str | None:
    safe = cfg.username.replace("@", "_at_").replace(".", "_")
    return get_secret(f"mail_box.password.{safe}") or (cfg.password or None)


# ---------------------------------------------------------------- 邮箱侦察


def collect_candidates(cfg: MailBoxConfig, password: str) -> list[dict[str, Any]]:
    """只读扫描邮箱，返回被水位线跳过的候选邮件（含附件字节）。

    返回 [{uid, subject, from_, date, attachments:[{filename,bytes,content_type}]}]
    """
    from imap_tools import AND, MailBox

    out: list[dict[str, Any]] = []
    with MailBox(cfg.imap_host, cfg.imap_port) as mb:
        mb.login(cfg.username, password, initial_folder=None)
        try:
            mb.client.xatom(
                "ID",
                '("name" "FinanceSystem" "version" "1.0" "vendor" "FinanceSystem")',
            )
        except Exception:
            LOG.debug("IMAP ID 命令不被支持，忽略")
        # 只读打开：即使后面 fetch 忘了 mark_seen=False 也不会改服务器状态
        mb.folder.set(cfg.folder, readonly=True)

        # 两阶段：先只拉头过滤主题（省流量 / 防 163 限流）
        hit_uids: list[str] = []
        for head in mb.fetch(AND(all=True), headers_only=True, mark_seen=False, bulk=50):
            if not _match_sender(head.from_, cfg.sender_whitelist):
                continue
            subj = head.subject or ""
            if not _match_subject(subj, cfg.subject_filter):
                continue
            if not _match_subject_regex(subj, cfg.subject_regex):
                continue
            try:
                uid_int = int(head.uid)
            except (TypeError, ValueError):
                continue
            # 只取「被水位线跳过」的历史邮件；正常增量的新邮件交给轮询线程
            if cfg.last_uid > 0 and uid_int > cfg.last_uid:
                continue
            hit_uids.append(str(head.uid))

        if not hit_uids:
            return []

        # 再拉命中邮件的完整内容取附件
        for msg in mb.fetch(AND(uid=",".join(hit_uids)), mark_seen=False):
            atts = []
            for att in msg.attachments or []:
                fname = att.filename or "attachment"
                if Path(fname).suffix.lower() not in cfg.allowed_ext:
                    continue
                size_mb = (att.size or 0) / (1024 * 1024)
                if size_mb > cfg.max_size_mb:
                    LOG.warning("附件过大跳过: %s (%.1f MB)", fname, size_mb)
                    continue
                try:
                    data = att.payload
                    if isinstance(data, str):
                        data = data.encode("utf-8")
                except Exception:
                    LOG.exception("提取附件失败: %s", fname)
                    continue
                atts.append(
                    {"filename": fname, "bytes": data, "content_type": att.content_type}
                )
            out.append(
                {
                    "uid": int(msg.uid),
                    "subject": msg.subject or "",
                    "from_": msg.from_ or "",
                    "date": msg.date.isoformat() if msg.date else "",
                    "attachments": atts,
                }
            )
    return out


def pick(
    candidates: Iterable[dict], limit: int, order: str, *, only_with_attachments: bool = True
) -> list[dict]:
    """按 uid 排序后取前 limit 封。

    order='oldest' 从旧到新（默认）。
    默认只取**含受支持附件**的邮件 —— 「收 N 封」应指真的能入库的 N 封，
    否则纯通知类空壳邮件（本项目 66 封里有 16 封是"点链接下载"型）会白吃额度。
    """
    items = list(candidates)
    if only_with_attachments:
        items = [m for m in items if m["attachments"]]
    items.sort(key=lambda m: m["uid"], reverse=(order == "newest"))
    return items[: max(0, limit)]


def pick_recognizable(
    candidates: Iterable[dict],
    limit: int,
    order: str,
    cmd: list[str],
    company_no: str,
    *,
    verbose: bool = True,
) -> tuple[list[tuple[dict, list[dict]]], list[dict]]:
    """按 uid 顺序逐封探测 OCR，凑够 limit 封「**含可入库发票**」的邮件就停。

    这样做让 ``--limit N`` 名副其实：收 N 封**真的能进库**的邮件。
    本项目实测：按 uid 升序的前 6 封（2023 同程系）OCR 全部抽不到票号/金额，
    若不做预筛，``--limit 5 --apply`` 会全部 BUSINESS_REJECTED、一张票进不来。

    返回 ``(selected, probes)``：
    - ``selected``: [(mail, [probe...])]，只含"至少一个附件可入库"的邮件
    - ``probes``:   所有被探测过的邮件记录（含跳过的），供报告用
    """
    items = [m for m in candidates if m["attachments"]]
    items.sort(key=lambda m: m["uid"], reverse=(order == "newest"))

    selected: list[tuple[dict, list[dict]]] = []
    probes: list[dict] = []
    for m in items:
        if len(selected) >= max(0, limit):
            break
        results = [
            probe_bytes(cmd, a["filename"], a["bytes"], company_no)
            for a in m["attachments"]
        ]
        inv_ok = [
            r for r in results if r["level"] == "OK" and not r["is_auxiliary"]
        ]
        record = {
            "uid": m["uid"],
            "date": (m["date"] or "")[:10],
            "subject": m["subject"],
            "recognizable_invoices": len(inv_ok),
            "attachments": [
                {"filename": r["filename"], "level": r["level"], "issues": r["issues"],
                 "auxiliary": r["is_auxiliary"]}
                for r in results
            ],
        }
        probes.append(record)
        if verbose:
            mark = "✅" if inv_ok else "❌"
            print(
                f"  {mark} 探测 uid={m['uid']} {record['date']} "
                f"{m['subject'][:36]:<36} 可入库 {len(inv_ok)} 张"
            )
        if inv_ok:
            selected.append((m, results))
    return selected, probes


def load_company_tax_no() -> str:
    """从生产库 company 表读本企业税号（用于识别买卖方错配）。"""
    from tools.invoice_ocr_probe import DEFAULT_COMPANY_TAX_NO

    try:
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT tax_no FROM company LIMIT 1").fetchone()
            return (row[0] if row and row[0] else DEFAULT_COMPANY_TAX_NO).strip().upper()
        finally:
            conn.close()
    except Exception:
        return DEFAULT_COMPANY_TAX_NO


# ---------------------------------------------------------------- 渲染


def render_plan(
    selected: list[tuple[dict, list[dict] | None]],
    cfg: MailBoxConfig,
    apply: bool,
    *,
    skipped_no_att: int = 0,
    probes: list[dict] | None = None,
) -> None:
    mode = "【写入模式 --apply】" if apply else "【只读预览 dry-run】"
    print("=" * 96)
    print(f"补收历史发票 {mode}")
    print(f"邮箱: {cfg.name} <{cfg.username}>   文件夹: {cfg.folder}")
    print(f"水位线 last_uid = {cfg.last_uid}  （本工具绝不修改它）")
    print("=" * 96)
    if skipped_no_att:
        print(f"ℹ️ 另有 {skipped_no_att} 封主题命中但**无受支持附件**（多为「点链接下载」型），已跳过。")
    if probes:
        n_probed = len(probes)
        n_dead = sum(1 for p in probes if not p["recognizable_invoices"])
        print(f"ℹ️ OCR 预筛：探测 {n_probed} 封，其中 {n_dead} 封**无可识别发票**已跳过。")
    if selected:
        print()
    if not selected:
        print("没有可补收的历史发票邮件。")
        return
    total_att = 0
    for i, (m, results) in enumerate(selected, 1):
        d = (m["date"] or "")[:16].replace("T", " ")
        print(f"{i:>2}. uid={m['uid']:<12} {d:<17} {m['from_'][:40]:<40}")
        print(f"    主题: {m['subject'][:78]}")
        for idx, a in enumerate(m["attachments"]):
            r = (results or [None] * len(m["attachments"]))[idx]
            if r and (r["level"] != "OK" or r["is_auxiliary"]):
                tag = "辅助凭证" if r["is_auxiliary"] else f"跳过({r['level']})"
                print(f"      · {a['filename']}  ({len(a['bytes']) / 1024:.1f} KB)  [{tag}]")
                continue
            total_att += 1
            print(f"      · {a['filename']}  ({len(a['bytes']) / 1024:.1f} KB)")
        if not m["attachments"]:
            print("      · （无受支持的附件）")
    print("-" * 96)
    print(f"合计 {len(selected)} 封邮件 / {total_att} 张待入库发票")


# ---------------------------------------------------------------- 执行


def record_backfill_audit(
    row: dict[str, Any], mail: dict, filename: str, actor: str
) -> bool:
    """为补收入库写审计记录（独立事务；失败只记日志，不影响入库结果）。

    ``register_invoice`` 自身不写 ``audit_event``（正常轮询入库也不写），
    但**补收属于"事后批量写入生产库"**，按项目惯例（P5/P7）必须留痕可追溯。
    """
    conn = None
    try:
        conn = db.get_conn()
        conn.execute("BEGIN IMMEDIATE")
        db.append_audit_event(
            conn,
            "invoice_doc",
            row.get("id"),
            "invoice_backfilled",
            actor,
            {
                "source": "email_backfill",
                "mail_uid": mail.get("uid"),
                "mail_subject": mail.get("subject"),
                "mail_date": mail.get("date"),
                "attachment": filename,
                "invoice_no": row.get("invoice_no"),
                "total_cents": row.get("total_cents"),
            },
        )
        conn.commit()
        return True
    except Exception:
        LOG.exception("写审计失败: invoice=%s", row.get("id"))
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            conn.close()


def apply_backfill(
    selected: list[tuple[dict, list[dict] | None]], cfg: MailBoxConfig, actor: str
) -> dict[str, Any]:
    """真实处理：送 OCR → register_invoice 入库。返回统计。

    若带 OCR 预筛结果，则**只处理判定为 OK 的发票附件**；行程单等辅助凭证与
    识别失败的附件直接跳过（不送 OCR、不产生 invoices/failed 占位文件）。
    """
    from modules.integration import IntegrationResult

    stat = {
        "emails": 0,
        "attachments": 0,
        "ingested": 0,
        "audited": 0,
        "duplicate_or_rejected": 0,
        "failed": 0,
        "skipped_not_recognizable": 0,
        "details": [],
    }
    for m, results in selected:
        stat["emails"] += 1
        for idx, a in enumerate(m["attachments"]):
            r = (results or [None] * len(m["attachments"]))[idx]
            if r and (r["level"] != "OK" or r["is_auxiliary"]):
                stat["skipped_not_recognizable"] += 1
                stat["details"].append(
                    {
                        "uid": m["uid"],
                        "filename": a["filename"],
                        "status": "SKIPPED",
                        "level": r["level"],
                        "auxiliary": r["is_auxiliary"],
                        "message": "OCR 预筛未通过，跳过（避免产生 failed 占位文件）",
                    }
                )
                continue

            stat["attachments"] += 1
            try:
                res: IntegrationResult = process_attachment(
                    a["filename"], a["bytes"], m["subject"]
                )
            except Exception as exc:  # 兜底，不让单封异常中断整批
                LOG.exception("处理异常: %s", a["filename"])
                stat["failed"] += 1
                stat["details"].append(
                    {
                        "uid": m["uid"],
                        "filename": a["filename"],
                        "status": "EXCEPTION",
                        "message": str(exc),
                    }
                )
                continue

            if res.ok:
                stat["ingested"] += 1
                if isinstance(res.data, dict) and res.data.get("id") is not None:
                    if record_backfill_audit(res.data, m, a["filename"], actor):
                        stat["audited"] += 1
            elif res.status in BENIGN:
                stat["duplicate_or_rejected"] += 1
            else:
                stat["failed"] += 1
            stat["details"].append(
                {
                    "uid": m["uid"],
                    "subject": m["subject"],
                    "filename": a["filename"],
                    "status": res.status.name,
                    "message": res.message,
                }
            )
            print(f"    [{res.status.name}] {a['filename']} — {res.message}")
    return stat


def render_summary(stat: dict[str, Any], backup: str | None, report: Path) -> None:
    print("=" * 96)
    print("处理结果")
    print(f"  邮件 {stat['emails']} 封 / 提交 OCR {stat['attachments']} 个附件")
    print(f"  ✅ 新入库        : {stat['ingested']}（含审计留痕 {stat.get('audited', 0)} 条）")
    print(f"  ℹ️ 重复或业务拒绝: {stat['duplicate_or_rejected']}")
    print(f"  ⏭️ 预筛跳过      : {stat['skipped_not_recognizable']}")
    print(f"  ❌ 失败          : {stat['failed']}")
    if backup:
        print(f"  备份            : {backup}")
    print(f"  明细报告        : {report}")
    print("=" * 96)


# ---------------------------------------------------------------- CLI


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="补收历史发票（默认只读；--apply 才写库）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--limit", type=int, default=3, help="最多处理几封邮件（默认 3）")
    p.add_argument(
        "--order",
        choices=("oldest", "newest"),
        default="oldest",
        help="取值顺序（默认 oldest=从旧到新，避免老邮件被永久跳过）",
    )
    p.add_argument("--apply", action="store_true", help="真实入库（不加则只读预览）")
    p.add_argument(
        "--no-probe",
        dest="probe",
        action="store_false",
        help="关闭 OCR 预筛（默认开启：只收能识别出票号+日期+金额的票）",
    )
    p.set_defaults(probe=True)
    p.add_argument("--box", default="", help="只处理指定名称的邮箱（默认所有已启用邮箱）")
    p.add_argument("--actor", default="system", help="写入审计的操作者（默认 system）")
    p.add_argument("--report", default="", help="明细 JSON 报告路径（默认 logs/ 下带时间戳）")
    p.add_argument("--config", default="", help="settings.json 路径（默认生产配置）")
    return p.parse_args(argv)


def default_report_path() -> Path:
    try:
        from app_paths import LOGS_DIR

        base = Path(LOGS_DIR)
    except Exception:
        base = Path(__file__).resolve().parents[1] / "logs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / f"backfill_history_invoices_{stamp}.json"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    cfg_file = Path(args.config) if args.config else config_path()
    try:
        boxes = load_boxes(cfg_file)
    except FileNotFoundError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 2
    if args.box:
        boxes = [b for b in boxes if b.name == args.box]
    if not boxes:
        print("!! 没有匹配的已启用邮箱。", file=sys.stderr)
        return 2

    report = Path(args.report) if args.report else default_report_path()
    report.parent.mkdir(parents=True, exist_ok=True)
    overall: dict[str, Any] = {
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry-run",
        "limit": args.limit,
        "order": args.order,
        "probe": args.probe,
        "boxes": [],
    }

    cmd = load_agent_command(cfg_file)
    company_no = load_company_tax_no()
    if args.probe:
        if not cmd:
            print("!! settings.json 缺 invoice_agent_command，无法预筛。", file=sys.stderr)
            return 2
        print(f"企业税号: {company_no}（用于识别买卖方错配）")

    backup: str | None = None
    if args.apply:
        # 写库前强制在线备份（含 integrity_check）
        backup = db.backup_database()
        print(f"✅ 已备份: {backup}")
        overall["backup"] = backup

    exit_code = 0
    for cfg in boxes:
        pwd = mailbox_password(cfg)
        if not pwd:
            print(f"!! 邮箱 {cfg.name} 读不到密码（DPAPI），跳过。", file=sys.stderr)
            exit_code = 2
            continue

        print(f"\n>>> 扫描 {cfg.name} <{cfg.username}> ...")
        try:
            candidates = collect_candidates(cfg, pwd)
        except Exception as exc:
            LOG.exception("扫描邮箱失败: %s", cfg.name)
            print(f"!! 扫描失败: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        skipped_no_att = sum(1 for m in candidates if not m["attachments"])
        probes: list[dict] | None = None
        if args.probe:
            print(f"候选 {len(candidates)} 封（有附件 {len(candidates) - skipped_no_att} 封），开始 OCR 预筛 ...")
            selected, probes = pick_recognizable(
                candidates, args.limit, args.order, cmd or [], company_no
            )
        else:
            selected = [(m, None) for m in pick(candidates, args.limit, args.order)]

        render_plan(selected, cfg, args.apply, skipped_no_att=skipped_no_att, probes=probes)

        box_entry: dict[str, Any] = {
            "name": cfg.name,
            "username": cfg.username,
            "last_uid": cfg.last_uid,
            "candidates_total": len(candidates),
            "candidates_with_attachments": sum(1 for m in candidates if m["attachments"]),
            "candidates_without_attachments": skipped_no_att,
            "probes": probes or [],
            "selected": [
                {
                    "uid": m["uid"],
                    "subject": m["subject"],
                    "from": m["from_"],
                    "date": m["date"],
                    "attachments": [a["filename"] for a in m["attachments"]],
                }
                for m, _ in selected
            ],
        }

        if args.apply and selected:
            stat = apply_backfill(selected, cfg, args.actor)
            box_entry["result"] = stat
            if stat["failed"]:
                exit_code = 1
        overall["boxes"].append(box_entry)

    overall["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report.write_text(
        json.dumps(overall, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if args.apply:
        keys = ("emails", "attachments", "ingested", "audited", "duplicate_or_rejected",
                "skipped_not_recognizable", "failed")
        total = {
            k: sum(b.get("result", {}).get(k, 0) for b in overall["boxes"]) for k in keys
        }
        render_summary(total, backup, report)
    else:
        print(f"\n（只读预览结束；加 --apply 才真实入库。明细: {report}）")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
