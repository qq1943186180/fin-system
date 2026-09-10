# -*- coding: utf-8 -*-
"""OCR 质量体检：对邮箱历史发票附件跑一遍识别，统计关键字段抽取成功率。

只读：连邮箱（只读打开）+ 跑 OCR 子进程，**不写库、不改配置、不标已读**。

用途：补收历史发票前先量化"能收多少"。本项目实测发现 2023 年的同程/老格式
数电票文本层是**乱序**的（先输出所有"值"再输出所有"标签"），导致
`ocr_invoice.py::extract_fields` 的兜底正则抽不到 `发票号码`/金额，
且会把购买方/销售方**税号错配**（名称顺序与税号顺序不一致）。

用法::

    python tools/audit_history_invoice_ocr.py --limit 12        # 先试 12 个附件
    python tools/audit_history_invoice_ocr.py                   # 全部
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import db  # noqa: E402
from tools.backfill_history_invoices import (  # noqa: E402
    collect_candidates,
    config_path,
    load_boxes,
    mailbox_password,
)

COMPANY_TAX_NO = "91330105MACRGYRN32"


def company_tax_no() -> str:
    """从生产库 company 表读本企业税号（用于识别买卖方错配）。"""
    try:
        conn = db.get_conn()
        try:
            row = conn.execute("SELECT tax_no FROM company LIMIT 1").fetchone()
            return (row[0] if row and row[0] else COMPANY_TAX_NO).strip().upper()
        finally:
            conn.close()
    except Exception:
        return COMPANY_TAX_NO


def run_ocr(cmd: list[str], path: Path, timeout: int = 180) -> dict[str, Any]:
    try:
        r = subprocess.run(
            cmd + [str(path)], capture_output=True, text=True, timeout=timeout, encoding="utf-8"
        )
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}
    if r.returncode != 0:
        return {"_error": f"exit {r.returncode}", "_stderr": (r.stderr or "")[-200:]}
    so = (r.stdout or "").strip()
    if not so:
        return {"_error": "empty stdout"}
    try:
        d = json.loads(so)
    except Exception:
        return {"_error": "non-json stdout", "_raw": so[:200]}
    fields = d.get("fields") if isinstance(d.get("fields"), dict) else d
    return fields if isinstance(fields, dict) else {"_error": "no fields"}


def classify(fields: dict, company_no: str) -> tuple[str, list[str]]:
    """返回 (等级, 问题列表)。等级: OK / PARTIAL / BAD / ERROR"""
    issues: list[str] = []
    if "_error" in fields:
        return "ERROR", [fields["_error"]]

    has_no = bool(str(fields.get("发票号码", "")).strip())
    has_date = bool(str(fields.get("开票日期", "")).strip())
    has_total = bool(str(fields.get("价税合计", "")).strip())
    has_net = bool(str(fields.get("金额合计", "")).strip())
    has_tax = bool(str(fields.get("税额合计", "")).strip())

    if not has_no:
        issues.append("缺发票号码")
    if not has_date:
        issues.append("缺开票日期")
    if not has_total and not (has_net and has_tax):
        issues.append("缺金额")

    # 买卖方错配检测：企业税号出现在"销售方税号"上 → 疑为顺序错配
    buyer_no = str(fields.get("购买方税号", "")).strip().upper()
    seller_no = str(fields.get("销售方税号", "")).strip().upper()
    name_pairs = (str(fields.get("购买方名称", "")), str(fields.get("销售方名称", "")))
    if company_no:
        if seller_no == company_no and buyer_no and buyer_no != company_no:
            issues.append("疑买卖方错配(我方税号落在销售方)")
        if not seller_no and not buyer_no:
            issues.append("无买卖方税号")

    if not has_no and not has_date and not has_total and not has_net:
        return "BAD", issues
    if issues:
        return "PARTIAL", issues
    return "OK", issues


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="OCR 质量体检（只读）")
    ap.add_argument("--limit", type=int, default=0, help="最多体检几个附件（0=全部）")
    ap.add_argument("--out", default="", help="报告 JSON 路径")
    ap.add_argument("--config", default="", help="settings.json 路径")
    args = ap.parse_args(argv)

    cfg_file = Path(args.config) if args.config else config_path()
    boxes = load_boxes(cfg_file)
    if not boxes:
        print("!! 没有已启用邮箱", file=sys.stderr)
        return 2
    cfg = boxes[0]
    pwd = mailbox_password(cfg)
    if not pwd:
        print("!! 读不到邮箱密码", file=sys.stderr)
        return 2

    settings = json.loads(cfg_file.read_text(encoding="utf-8"))
    cmd = settings.get("invoice_agent_command")
    if not cmd:
        print("!! settings.json 缺 invoice_agent_command", file=sys.stderr)
        return 2

    company_no = company_tax_no().upper()
    print(f"企业税号: {company_no}")
    print(f"邮箱: {cfg.name} <{cfg.username}>  (只读)")
    print("扫描候选邮件 ...")
    cands = collect_candidates(cfg, pwd)
    with_att = [m for m in cands if m["attachments"]]
    total_att = sum(len(m["attachments"]) for m in with_att)
    print(f"候选 {len(cands)} 封，其中有附件 {len(with_att)} 封 / {total_att} 个附件")

    tasks: list[tuple[dict, dict]] = []
    for m in with_att:
        for a in m["attachments"]:
            tasks.append((m, a))
    if args.limit:
        tasks = tasks[: args.limit]
    print(f"开始体检 {len(tasks)} 个附件 ...\n")

    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="ocr_audit_"))
    rows: list[dict[str, Any]] = []
    for i, (m, a) in enumerate(tasks, 1):
        p = tmp / f"{i:03d}_{a['filename']}"
        p.write_bytes(a["bytes"])
        fields = run_ocr(cmd, p)
        level, issues = classify(fields, company_no)
        rows.append(
            {
                "uid": m["uid"],
                "date": (m["date"] or "")[:10],
                "subject": m["subject"],
                "filename": a["filename"],
                "level": level,
                "issues": issues,
                "invoice_no": fields.get("发票号码", ""),
                "invoice_date": fields.get("开票日期", ""),
                "total": fields.get("价税合计", ""),
                "net": fields.get("金额合计", ""),
                "tax": fields.get("税额合计", ""),
                "seller": fields.get("销售方名称", ""),
                "buyer": fields.get("购买方名称", ""),
                "source": fields.get("_source", ""),
            }
        )
        mark = {"OK": "✅", "PARTIAL": "⚠️", "BAD": "❌", "ERROR": "🔥"}[level]
        extra = (" | " + "，".join(issues)) if issues else ""
        print(f"{mark} [{i:>3}/{len(tasks)}] {a['filename'][:52]:<52}{extra}")

    print("\n" + "=" * 96)
    from collections import Counter

    cnt = Counter(r["level"] for r in rows)
    print("体检汇总")
    for lv, label in (("OK", "✅ 完整可入库"), ("PARTIAL", "⚠️ 部分缺失(会被拒)"),
                      ("BAD", "❌ 基本抽不到"), ("ERROR", "🔥 运行错误")):
        n = cnt.get(lv, 0)
        pct = (n / len(rows) * 100) if rows else 0
        print(f"  {label:<18}: {n:>3} 个 ({pct:.0f}%)")
    print(f"  合计: {len(rows)} 个附件")

    issue_cnt = Counter(i for r in rows for i in r["issues"])
    if issue_cnt:
        print("\n问题分布:")
        for k, v in issue_cnt.most_common():
            print(f"  {k:<28}: {v}")

    print("\n— 按年份看成功率 —")
    by_year: dict[str, list[str]] = {}
    for r in rows:
        y = (r["date"] or r["invoice_date"] or "?")[:4]
        by_year.setdefault(y, []).append(r["level"])
    for y in sorted(by_year):
        lv = by_year[y]
        ok = lv.count("OK")
        print(f"  {y}: {ok}/{len(lv)} 完整")

    out = Path(args.out) if args.out else (
        Path(__file__).resolve().parents[1] / "logs" / f"ocr_audit_{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"company_tax_no": company_no, "rows": rows}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n明细: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
