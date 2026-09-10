#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修正 invoice_doc 的金额口径错误（代号 P7）。

## 背景

历史 `invoice_agent.py::summarize()` 的 `金额` 字段兼职了两种含义
（`价税合计 or 金额合计`）。OCR 对滴滴票抽不到「价税合计」时，`金额` 会落到
**不含税**的 `金额合计`；下游 `normalize_invoice()` 又把它当作 `net_amount_cents`，
而 `税额合计` 因 key 名不匹配（summary 里叫「税额」、代码找「税额合计」）取到 0，
最终 `total = net + tax = net` —— **进项税额被静默丢弃，价税合计少计**。

本工具按 `results/ocr_result.json` 的**票面真值**修正受影响的台账行，
并同步修正由该发票生成的凭证分录。

## 会计口径（已由用户确认）

滴滴开的是 **3% 增值税电子普通发票**，进项税额**不可抵扣** →
成本费用按**价税合计全额**入账。因此凭证分录的做法是把原金额整体调整为
`total_cents`（借贷同增），而不是拆出 `2221 应交税费—应交增值税(进项税额)`。

若将来出现可抵扣凭证，需走另一条路径（拆 2221）；本工具遇到含 2221 的凭证会
**拒绝处理并报告**，避免用错误口径自动改写进项税。

## 安全设计

- **默认只读**：不加 `--apply` 只打印方案，不写库。
- **写前备份**：SQLite 在线备份 API（生产库是 WAL 模式，`shutil.copy2` 可能拿到
  不一致快照）+ `integrity_check` + 关键表行数校验。
- **只在"确属该缺陷"时动手**：仅当现值满足 `tax_cents == 0 && total_cents == net_amount_cents`
  且票面真值 `net + tax == total` 时才纳入计划；其他形态一律**报告冲突、不猜**。
- **幂等**：现值已等于真值 → 跳过。
- **拒绝模糊**：凭证非「一借一贷」、含 2221、借贷不等、或凭证已 `posted` → 拒绝。
- **改后自校验**：每张受影响的凭证重新核算借贷合计必须相等。
- **留痕**：每次改动写一条 append-only 的 `audit_event`。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

DEFAULT_AGENT_RESULTS = Path("C:/Users/li1/invoice-agent/results/ocr_result.json")
ADJUSTABLE_ACCOUNTS_BLOCKED = ("2221",)
TOOL_NAME = "fix_invoice_amounts"


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


# ---------------------------------------------------------------- 金额工具


def _to_cents(value) -> int | None:
    """把票面金额字符串转成整数分；无法解析返回 None。"""
    if value is None:
        return None
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    if not text:
        return None
    try:
        from decimal import Decimal, ROUND_HALF_UP

        return int((Decimal(text) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def _cents_to_real(cents: int) -> float:
    return round(int(cents) / 100.0, 2)


# ---------------------------------------------------------------- 票面真值


def load_ocr_truth(results_path: os.PathLike | str) -> dict[str, dict]:
    """从 `ocr_result.json` 读票面三件套，按发票号码索引。

    返回 `{发票号码: {"net_cents":…, "tax_cents":…, "total_cents":…, "file":…}}`。
    """
    payload = json.loads(Path(results_path).read_text(encoding="utf-8"))
    truth: dict[str, dict] = {}
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        fields = entry.get("fields") or {}
        invoice_no = str(fields.get("发票号码") or "").strip()
        if not invoice_no or invoice_no == "-":
            continue
        net = _to_cents(fields.get("金额合计"))
        tax = _to_cents(fields.get("税额合计"))
        total = _to_cents(fields.get("价税合计"))
        if net is None or tax is None:
            continue
        if total is None:
            total = net + tax
        if net + tax != total:
            continue
        truth[invoice_no] = {
            "net_cents": net,
            "tax_cents": tax,
            "total_cents": total,
            "file": entry.get("file") or "",
        }
    return truth


# ---------------------------------------------------------------- 连接


def connect(db_path: os.PathLike | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # 触发器会调用该函数；返回 0 = 不允许改动已记账凭证（与运行时默认一致）
    conn.create_function("finance_allow_posted_mutation", 0, lambda: 0)
    return conn


# ---------------------------------------------------------------- 备份


def _table_counts(conn: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables:
        try:
            counts[table] = int(conn.execute('SELECT COUNT(*) FROM "%s"' % table).fetchone()[0])
        except sqlite3.Error:
            counts[table] = -1
    return counts


BACKUP_TABLES = ("invoice_doc", "voucher", "voucher_line", "audit_event", "company")


def backup_database(db_path: os.PathLike | str, backup_dir: os.PathLike | str) -> Path:
    """用 SQLite 在线备份 API 生成一致性快照，并校验完整性。"""
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / ("%s.fixamounts-%s.bak" % (db_path.name, stamp))
    source = sqlite3.connect(str(db_path))
    try:
        before = _table_counts(source, BACKUP_TABLES)
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    probe = sqlite3.connect(str(target))
    try:
        check = probe.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise RuntimeError("备份完整性校验失败: %s" % check)
        after = _table_counts(probe, BACKUP_TABLES)
    finally:
        probe.close()
    if before != after:
        raise RuntimeError("备份行数校验失败: %s != %s" % (before, after))
    return target


# ---------------------------------------------------------------- 计划


def _voucher_adjustment(conn: sqlite3.Connection, voucher_id: int, old_total: int, new_total: int) -> dict:
    """算出凭证分录的调整方案；不可安全自动处理时抛 ValueError。"""
    voucher = conn.execute(
        "SELECT id, vno, status FROM voucher WHERE id=?", (voucher_id,)
    ).fetchone()
    if not voucher:
        raise ValueError("凭证 %s 不存在" % voucher_id)
    if voucher["status"] == "posted":
        raise ValueError("凭证 %s（%s）已记账，不可自动改写" % (voucher_id, voucher["vno"]))
    lines = conn.execute(
        "SELECT id, seq, account_code, debit_cents, credit_cents FROM voucher_line "
        "WHERE voucher_id=? ORDER BY seq",
        (voucher_id,),
    ).fetchall()
    if len(lines) != 2:
        raise ValueError("凭证 %s 有 %d 行分录（非一借一贷），需人工处理" % (voucher_id, len(lines)))
    accounts = {str(line["account_code"]) for line in lines}
    blocked = accounts & set(ADJUSTABLE_ACCOUNTS_BLOCKED)
    if blocked:
        raise ValueError(
            "凭证 %s 含科目 %s（进项税额），需按可抵扣口径人工拆分" % (voucher_id, ",".join(sorted(blocked)))
        )
    debit_lines = [line for line in lines if int(line["debit_cents"]) > 0]
    credit_lines = [line for line in lines if int(line["credit_cents"]) > 0]
    if len(debit_lines) != 1 or len(credit_lines) != 1:
        raise ValueError("凭证 %s 不是一借一贷，需人工处理" % voucher_id)
    if int(debit_lines[0]["debit_cents"]) != old_total or int(credit_lines[0]["credit_cents"]) != old_total:
        raise ValueError(
            "凭证 %s 分录金额与发票原金额不一致，需人工核对" % voucher_id
        )
    delta = int(new_total) - int(old_total)
    return {
        "voucher_id": int(voucher_id),
        "vno": voucher["vno"],
        "delta_cents": delta,
        "lines": [
            {
                "id": int(debit_lines[0]["id"]),
                "account_code": str(debit_lines[0]["account_code"]),
                "side": "debit",
                "old_cents": int(debit_lines[0]["debit_cents"]),
                "new_cents": int(new_total),
            },
            {
                "id": int(credit_lines[0]["id"]),
                "account_code": str(credit_lines[0]["account_code"]),
                "side": "credit",
                "old_cents": int(credit_lines[0]["credit_cents"]),
                "new_cents": int(new_total),
            },
        ],
    }


def build_plan(conn: sqlite3.Connection, truth: dict[str, dict]) -> tuple[list[dict], list[str]]:
    """纯计算：产出可执行计划与冲突清单（不写库）。"""
    plan: list[dict] = []
    conflicts: list[str] = []
    rows = conn.execute(
        "SELECT id, invoice_no, net_amount_cents, tax_cents, total_cents, tax, total, voucher_id "
        "FROM invoice_doc ORDER BY id"
    ).fetchall()
    for row in rows:
        invoice_no = str(row["invoice_no"] or "").strip()
        if invoice_no not in truth:
            continue
        expected = truth[invoice_no]
        net_cents = int(row["net_amount_cents"] or 0)
        tax_cents = int(row["tax_cents"] or 0)
        total_cents = int(row["total_cents"] or 0)

        if net_cents != expected["net_cents"]:
            conflicts.append(
                "id=%s（%s）不含税金额不符：库内 %s ≠ 票面 %s，不自动处理"
                % (row["id"], invoice_no, net_cents, expected["net_cents"])
            )
            continue
        if tax_cents == expected["tax_cents"] and total_cents == expected["total_cents"]:
            continue  # 已经正确，幂等跳过
        if not (tax_cents == 0 and total_cents == net_cents):
            conflicts.append(
                "id=%s（%s）金额形态非本缺陷特征（tax=%s total=%s），不自动处理"
                % (row["id"], invoice_no, tax_cents, total_cents)
            )
            continue

        entry = {
            "invoice_id": int(row["id"]),
            "invoice_no": invoice_no,
            "file": expected.get("file") or "",
            "old": {"tax_cents": tax_cents, "total_cents": total_cents},
            "new": {"tax_cents": expected["tax_cents"], "total_cents": expected["total_cents"]},
            "delta_cents": expected["total_cents"] - total_cents,
            "voucher": None,
        }
        voucher_id = row["voucher_id"]
        if voucher_id:
            try:
                entry["voucher"] = _voucher_adjustment(
                    conn, int(voucher_id), total_cents, expected["total_cents"]
                )
            except ValueError as exc:
                conflicts.append("id=%s（%s）：%s" % (row["id"], invoice_no, exc))
                continue
        plan.append(entry)
    return plan, conflicts


# ---------------------------------------------------------------- 审计


def append_audit(
    conn: sqlite3.Connection, entity_type: str, entity_id: int, event_type: str, actor: str, details: dict
) -> None:
    conn.execute(
        "INSERT INTO audit_event(entity_type, entity_id, event_type, actor, details_json, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (
            entity_type,
            entity_id,
            event_type,
            actor,
            json.dumps(details, ensure_ascii=False, sort_keys=True),
            _now(),
        ),
    )


# ---------------------------------------------------------------- 执行


def apply_plan(
    conn: sqlite3.Connection, plan: list[dict], actor: str = "li1", archive_dir=None
) -> list[dict]:
    """在单个事务内写库；失败整体回滚。"""
    applied: list[dict] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 预检查：确认所有目标仍处于"可安全修改"的状态（防计划与实际脱节）
        for entry in plan:
            row = conn.execute(
                "SELECT tax_cents, total_cents, net_amount_cents, voucher_id FROM invoice_doc WHERE id=?",
                (entry["invoice_id"],),
            ).fetchone()
            if not row:
                raise RuntimeError("发票 id=%s 已不存在" % entry["invoice_id"])
            if int(row["tax_cents"]) != entry["old"]["tax_cents"] or int(
                row["total_cents"]
            ) != entry["old"]["total_cents"]:
                raise RuntimeError(
                    "发票 id=%s 的金额在计划生成后已变化，请重新生成计划" % entry["invoice_id"]
                )
            if int(row["voucher_id"] or 0) != int(
                (entry["voucher"] or {}).get("voucher_id") or 0
            ):
                raise RuntimeError("发票 id=%s 的凭证关联已变化，请重新生成计划" % entry["invoice_id"])

        for entry in plan:
            new = entry["new"]
            conn.execute(
                "UPDATE invoice_doc SET tax=?, total=?, tax_cents=?, total_cents=?, updated_at=? WHERE id=?",
                (
                    _cents_to_real(new["tax_cents"]),
                    _cents_to_real(new["total_cents"]),
                    new["tax_cents"],
                    new["total_cents"],
                    _now(),
                    entry["invoice_id"],
                ),
            )
            append_audit(
                conn,
                "invoice_doc",
                entry["invoice_id"],
                "invoice_amount_corrected",
                actor,
                {
                    "tool": TOOL_NAME,
                    "invoice_no": entry["invoice_no"],
                    "old": entry["old"],
                    "new": new,
                    "evidence": "ocr_result.json",
                },
            )

            voucher = entry.get("voucher")
            if voucher:
                for line in voucher["lines"]:
                    if line["side"] == "debit":
                        conn.execute(
                            "UPDATE voucher_line SET debit=?, debit_cents=? WHERE id=?",
                            (_cents_to_real(line["new_cents"]), line["new_cents"], line["id"]),
                        )
                    else:
                        conn.execute(
                            "UPDATE voucher_line SET credit=?, credit_cents=? WHERE id=?",
                            (_cents_to_real(line["new_cents"]), line["new_cents"], line["id"]),
                        )
                # 改后自校验：借贷必须仍然相等
                totals = conn.execute(
                    "SELECT COALESCE(SUM(debit_cents),0) AS d, COALESCE(SUM(credit_cents),0) AS c "
                    "FROM voucher_line WHERE voucher_id=?",
                    (voucher["voucher_id"],),
                ).fetchone()
                if int(totals["d"]) != int(totals["c"]):
                    raise RuntimeError(
                        "凭证 %s 调整后借贷不平（%s ≠ %s），已回滚"
                        % (voucher["vno"], totals["d"], totals["c"])
                    )
                append_audit(
                    conn,
                    "voucher",
                    voucher["voucher_id"],
                    "voucher_amount_corrected",
                    actor,
                    {
                        "tool": TOOL_NAME,
                        "vno": voucher["vno"],
                        "delta_cents": voucher["delta_cents"],
                        "reason": "invoice_amount_corrected",
                        "invoice_id": entry["invoice_id"],
                    },
                )
            applied.append(entry)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return applied


# ---------------------------------------------------------------- 输出


def render_plan(plan: list[dict], conflicts: list[str], db_path) -> None:
    print("数据库：%s" % db_path)
    print()
    if not plan and not conflicts:
        print("[无需处理] 未发现符合本缺陷特征的金额错误。")
        return
    print("── 待修正（%d 张）──" % len(plan))
    for entry in plan:
        old, new = entry["old"], entry["new"]
        print("  [id=%s] %s" % (entry["invoice_id"], entry["invoice_no"]))
        print(
            "      tax_cents   %s -> %s" % (old["tax_cents"], new["tax_cents"])
        )
        print(
            "      total_cents %s -> %s   (+%s 分，即 ¥%.2f)"
            % (old["total_cents"], new["total_cents"], entry["delta_cents"], entry["delta_cents"] / 100.0)
        )
        voucher = entry.get("voucher")
        if voucher:
            print("      凭证 %s（id=%s）同步调整：" % (voucher["vno"], voucher["voucher_id"]))
            for line in voucher["lines"]:
                print(
                    "        %s %s  %s -> %s"
                    % (line["side"], line["account_code"], line["old_cents"], line["new_cents"])
                )
        else:
            print("      未关联凭证，仅修台账")
    if conflicts:
        print()
        print("── 冲突/拒绝（%d 项，不会自动处理）──" % len(conflicts))
        for item in conflicts:
            print("  · %s" % item)
    total_delta = sum(entry["delta_cents"] for entry in plan)
    print()
    print("合计补计价税 %s 分（¥%.2f）" % (total_delta, total_delta / 100.0))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="修正 invoice_doc 金额口径错误（默认只读，--apply 才写）"
    )
    parser.add_argument("--db", default=None, help="目标数据库，默认取 app_paths.get_db_path()")
    parser.add_argument("--apply", action="store_true", help="真正写库（会先自动备份）")
    parser.add_argument("--actor", default="li1", help="审计事件的操作者标识")
    parser.add_argument(
        "--ocr-results", default=str(DEFAULT_AGENT_RESULTS), help="票面真值来源 ocr_result.json"
    )
    parser.add_argument("--backup-dir", default=None, help="备份目录，默认 <db目录>/backups")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def _resolve_db_path(explicit=None) -> Path:
    if explicit:
        return Path(explicit)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app_paths import get_db_path  # noqa: PLC0415

    return Path(get_db_path())


def main(argv=None) -> int:
    args = parse_args(argv)
    db_path = _resolve_db_path(args.db)
    if not db_path.exists():
        print("数据库不存在：%s" % db_path, file=sys.stderr)
        return 2
    results_path = Path(args.ocr_results)
    if not results_path.exists():
        print("票面真值文件不存在：%s" % results_path, file=sys.stderr)
        return 2

    truth = load_ocr_truth(results_path)
    conn = connect(db_path)
    try:
        plan, conflicts = build_plan(conn, truth)
        if args.json:
            print(json.dumps({"plan": plan, "conflicts": conflicts}, ensure_ascii=False, indent=2))
        else:
            render_plan(plan, conflicts, db_path)
        if not args.apply:
            if not args.json:
                print()
                print("[只读模式] 未写入任何内容。确认无误后加 --apply 执行。")
            return 0 if not conflicts else 1

        if not plan:
            print()
            print("[无改动] 没有需要写入的内容。")
            return 0
        backup_dir = Path(args.backup_dir) if args.backup_dir else db_path.parent / "backups"
        backup_path = backup_database(db_path, backup_dir)
        print()
        print("已备份数据库 -> %s" % backup_path)
        applied = apply_plan(conn, plan, actor=args.actor)
        print()
        print("── 执行结果 ──")
        print("  修正发票：%d" % len(applied))
        print("  同步修正凭证：%d" % len([e for e in applied if e.get("voucher")]))
        print("  合计补计价税：¥%.2f" % (sum(e["delta_cents"] for e in applied) / 100.0))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
