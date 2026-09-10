# -*- coding: utf-8 -*-
"""发票探测：调用外部 OCR 命令 / 本地数电票解析，并判定识别质量（只读，不写库）。

被两个工具复用：
- ``tools/audit_history_invoice_ocr.py``（质量体检）
- ``tools/backfill_history_invoices.py``（补收前预筛，只收能识别的票）

判定分级
--------
- ``OK``      票号 + 日期 + 金额齐全且自洽 → 可入库
- ``PARTIAL`` 缺其中某项 → ``register_invoice`` 会拒（``BUSINESS_REJECTED``）
- ``BAD``     关键字段基本全无 → 会被拒
- ``ERROR``   OCR 子进程失败/超时/输出非 JSON

⚠️ 已知的识别缺陷（2026-09-11 实测，见 HANDOFF §10.3）：
外部 ``ocr_invoice.py`` 的「乱序文本层」兜底逻辑假设**名称与税号的出现顺序一致**，
但部分厂商（同程/滴滴 DZFP）的 PDF 里**名称按购买方在前、税号按销售方在前**，
导致购买方/销售方**税号错配**；且票号/金额的「值」在文件头、「标签」在文件尾，
正则抽不到。本模块的 ``疑买卖方错配`` 判定就是用来暴露这个问题的。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

DEFAULT_COMPANY_TAX_NO = "91330105MACRGYRN32"

# 行程单/结算单等辅助凭证 —— 本就不含发票字段，不应算作识别失败
AUX_MARKERS = ("行程单", "结算单", "结账单", "报销单", "对账单", "行程报销")


def is_auxiliary(filename: str) -> bool:
    """文件名是否表明它是行程单/结算单等**非发票**辅助凭证。"""
    return any(k in (filename or "") for k in AUX_MARKERS)


def load_agent_command(config_file: Path) -> list[str] | None:
    """从 settings.json 读 invoice_agent_command。"""
    try:
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
    except Exception:
        return None
    cmd = data.get("invoice_agent_command")
    if isinstance(cmd, list) and cmd:
        return [str(x) for x in cmd]
    return None


def run_ocr(cmd: list[str], path: Path, timeout: int = 180) -> dict[str, Any]:
    """跑 OCR 子进程，返回 fields 字典（失败时带 ``_error``）。"""
    try:
        r = subprocess.run(
            cmd + [str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
        )
    except subprocess.TimeoutExpired:
        return {"_error": "timeout"}
    except Exception as exc:
        return {"_error": f"spawn failed: {exc}"}
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


def classify(fields: dict, company_no: str = "") -> tuple[str, list[str]]:
    """判定识别质量，返回 ``(level, issues)``。"""
    issues: list[str] = []
    if "_error" in fields:
        return "ERROR", [str(fields["_error"])]

    has_no = bool(str(fields.get("发票号码", "")).strip())
    has_date = bool(str(fields.get("开票日期", "")).strip())
    has_total = bool(str(fields.get("价税合计", "")).strip())
    has_net = bool(str(fields.get("金额合计", "")).strip())
    has_tax = bool(str(fields.get("税额合计", "")).strip())
    # 不含税金额可由「价税合计 − 税额」推导，缺它不算缺陷
    # （数电票 XML 解析在分位不自洽时会主动留空，交由入库端推导）
    if not has_net and has_total and has_tax:
        has_net = True

    if not has_no:
        issues.append("缺发票号码")
    if not has_date:
        issues.append("缺开票日期")
    if not has_total and not (has_net and has_tax):
        issues.append("缺金额")

    company_no = (company_no or "").strip().upper()
    buyer_no = str(fields.get("购买方税号", "")).strip().upper()
    seller_no = str(fields.get("销售方税号", "")).strip().upper()
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


def probe_bytes(
    cmd: list[str], filename: str, data: bytes, company_no: str = "", timeout: int = 180
) -> dict[str, Any]:
    """把附件字节落临时文件后探测。

    数电票 XML/OFD 走本地结构化解析（不需要 OCR 子进程）；
    PDF/图片才调用外部 OCR。

    返回 ``{filename, level, issues, fields, is_auxiliary}``。
    """
    import tempfile

    from modules.einvoice import EINVOICE_EXTS, EinvoiceParseError, parse_einvoice_attachment

    suffix = Path(filename).suffix.lower() or ".bin"
    if suffix in EINVOICE_EXTS:
        try:
            fields = parse_einvoice_attachment(data, suffix)
        except EinvoiceParseError as exc:
            fields = {"_error": str(exc)}
        level, issues = classify(fields, company_no)
        return {
            "filename": filename,
            "level": level,
            "issues": issues,
            "fields": fields,
            "is_auxiliary": is_auxiliary(filename),
        }

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, prefix="ocr_probe_", delete=False) as fh:
            tmp_path = Path(fh.name)
            fh.write(data)
        fields = run_ocr(cmd, tmp_path, timeout=timeout)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    level, issues = classify(fields, company_no)
    return {
        "filename": filename,
        "level": level,
        "issues": issues,
        "fields": fields,
        "is_auxiliary": is_auxiliary(filename),
    }
