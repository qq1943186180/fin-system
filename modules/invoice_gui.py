# -*- coding: utf-8 -*-
"""Invoice ingestion, deduplication, review, and voucher preparation.

Invoices are persisted before accounting.  This module never posts a voucher;
it creates a draft or pending-review voucher after the user confirms the
supplier, account, tax handling, and payment state.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

from db import get_conn, init_db
from modules import ledger
from modules.integration import IntegrationResult, IntegrationStatus

try:
    from app_paths import CONFIG_DIR, INVOICES_ARCHIVE_DIR, INVOICES_DIR, ensure_app_dirs
except ImportError:  # developer checkout before packaging helpers are installed
    _ROOT = Path(__file__).resolve().parents[1]
    CONFIG_DIR = _ROOT / "config"
    INVOICES_DIR = _ROOT / "data" / "invoices"
    INVOICES_ARCHIVE_DIR = INVOICES_DIR / "archive"

    def ensure_app_dirs():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        INVOICES_DIR.mkdir(parents=True, exist_ok=True)
        INVOICES_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)


try:
    from logging_setup import get_logger

    LOG = get_logger("modules.invoice")
except ImportError:
    LOG = logging.getLogger(__name__)


def _now() -> str:
    return _dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _settings() -> dict[str, Any]:
    ensure_app_dirs()
    path = Path(CONFIG_DIR) / "settings.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        LOG.exception("Unable to read invoice integration settings")
        return {}


def _agent_command() -> list[str] | None:
    """Return a direct argv command; shell command strings are never used."""
    raw = os.environ.get("FINANCE_INVOICE_AGENT_COMMAND")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed and all(isinstance(x, str) for x in parsed):
                return parsed
        except ValueError:
            # Environment values may use ordinary Windows command-line quoting.
            parsed = shlex.split(raw, posix=False)
            if parsed:
                return parsed
    configured = _settings().get("invoice_agent_command")
    if isinstance(configured, list) and configured and all(isinstance(x, str) for x in configured):
        return configured
    return None


def fetch_and_recognize(timeout: int = 600) -> IntegrationResult:
    """Run the configured local invoice agent and return its summary path."""
    command = _agent_command()
    if not command:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "尚未配置发票识别程序，请在设置中配置 invoice_agent_command。",
        )
    executable = Path(command[0])
    if ("\\" in command[0] or "/" in command[0]) and not executable.exists():
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "发票识别程序不存在，请检查设置中的命令路径。",
        )
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "发票识别超时，可稍后安全重试。",
        )
    except OSError as exc:
        LOG.warning("Invoice agent launch failed: %s", type(exc).__name__)
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "无法启动发票识别程序，请检查命令和运行环境。",
        )
    if result.returncode != 0:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "发票识别未完成，可查看诊断日志后重试。",
            details={"returncode": result.returncode},
        )

    configured_summary = _settings().get("invoice_summary_path")
    candidates = [Path(configured_summary)] if configured_summary else []
    if len(command) >= 2:
        candidates.append(Path(command[-1]).resolve().parent / "results" / "summary.json")
    candidates.append(Path(INVOICES_DIR) / "summary.json")
    summary = next((p for p in candidates if p.exists()), None)
    if not summary:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "识别程序已结束，但未找到 summary.json。",
        )
    return IntegrationResult(IntegrationStatus.SUCCESS, "识别完成，待人工核对。", str(summary))


def load_invoices(summary_path: str | os.PathLike | None) -> list[dict]:
    if not summary_path or not Path(summary_path).exists():
        return []
    try:
        data = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        LOG.exception("Unable to parse invoice summary")
        return []
    if isinstance(data, dict):
        rows = data.get("invoices", data.get("rows", []))
    else:
        rows = data
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    missing = [row for row in rows if not _text(row, "file_path", "file")]
    if missing:
        LOG.warning(
            "summary 中 %d/%d 行缺少发票原件路径（file_path/file）：这些发票入库后"
            "无法回溯原件，请检查发票识别程序的 summarize 输出。首个缺失行: 发票号码=%s",
            len(missing),
            len(rows),
            _text(missing[0], "发票号码", "invoice_no") or "(未知)",
        )
    return rows


def _text(inv: dict, *keys: str) -> str:
    for key in keys:
        value = inv.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _date(value: str) -> str:
    value = value.strip()
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", value)
    if not match:
        raise ValueError("开票日期格式应为 YYYY-MM-DD")
    year, month, day = map(int, match.groups())
    return _dt.date(year, month, day).isoformat()


def _cents(value: Any) -> int:
    if value is None or value == "":
        return 0
    cleaned = re.sub(r"[^0-9.\-]", "", str(value).replace(",", ""))
    if not cleaned:
        return 0
    from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation as exc:
        raise ValueError("发票金额格式无效") from exc


def _file_hash(path: str | os.PathLike | None) -> str:
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        return ""
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ARCHIVE_ENABLED_SETTING = "invoice_archive_enabled"


def _archive_enabled() -> bool:
    value = _settings().get(ARCHIVE_ENABLED_SETTING)
    return True if value is None else bool(value)


def _archive_month(date_iso: str) -> str:
    match = re.match(r"(\d{4})-(\d{1,2})", str(date_iso or ""))
    return "%s-%02d" % (match.group(1), int(match.group(2))) if match else "unknown"


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(name)).strip(" .")
    return cleaned or "invoice"


def _archive_invoice_file(
    source: str | os.PathLike | None,
    invoice_no: str,
    date_iso: str,
    display_name: str | None = None,
) -> str:
    """Copy the invoice original into ``<app data>/invoices/archive/<月份>/``.

    Returns the archived path, or the original path when archiving is disabled or
    impossible.  Archiving never raises: a failure downgrades to the source path
    so that bookkeeping is not blocked by filesystem problems.
    """
    raw = str(source or "").strip()
    if not raw:
        return ""
    src = Path(raw)
    if not src.is_file():
        return raw
    if not _archive_enabled():
        return str(src)
    try:
        archive_root = Path(INVOICES_ARCHIVE_DIR).resolve()
        resolved = src.resolve()
    except OSError:
        archive_root, resolved = Path(INVOICES_ARCHIVE_DIR), src
    if archive_root == resolved.parent or archive_root in resolved.parents:
        return str(resolved)
    target_dir = archive_root / _archive_month(date_iso)
    original = _safe_name(display_name) if display_name else _safe_name(src.name)
    target = target_dir / ("%s_%s" % (_safe_name(invoice_no), original))
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _file_hash(target) == _file_hash(src):
                return str(target)
            stem, suffix = target.stem, target.suffix
            for index in range(2, 100):
                candidate = target_dir / ("%s(%d)%s" % (stem, index, suffix))
                if not candidate.exists():
                    target = candidate
                    break
            else:
                return str(src)
        shutil.copy2(src, target)
        return str(target)
    except OSError as exc:
        LOG.warning("发票原件归档失败，沿用原路径: %s", type(exc).__name__)
        return str(src)


def normalize_invoice(inv: dict, source_file: str | os.PathLike | None = None) -> dict:
    invoice_no = _text(inv, "发票号码", "invoice_no", "code")
    seller_tax_no = _text(inv, "销售方税号", "seller_tax_no")
    date_raw = _text(inv, "开票日期", "date")
    if not invoice_no:
        raise ValueError("发票号码不能为空")
    if not date_raw:
        raise ValueError("开票日期不能为空")
    net = _cents(inv.get("金额合计", inv.get("金额", inv.get("amount"))))
    tax = _cents(inv.get("税额合计", inv.get("tax")))
    total = _cents(inv.get("价税合计", inv.get("total")))
    if not total:
        total = net + tax
    if not net:
        net = total - tax
    if total <= 0 or net < 0 or tax < 0 or net + tax != total:
        raise ValueError("金额、税额与价税合计不一致")
    date_iso = _date(date_raw)
    raw_file = str(source_file or inv.get("file_path") or inv.get("file") or "")
    display_name = _text(inv, "_source_filename", "_mail_filename") or None
    file_path = (
        _archive_invoice_file(raw_file, invoice_no, date_iso, display_name) or raw_file
    )
    return {
        "invoice_no": invoice_no,
        "date": date_iso,
        "seller": _text(inv, "销售方名称", "销售方", "seller", "name"),
        "seller_tax_no": seller_tax_no,
        "buyer": _text(inv, "购买方名称", "购买方", "buyer"),
        "buyer_tax_no": _text(inv, "购买方税号", "buyer_tax_no"),
        "net_amount_cents": net,
        "tax_cents": tax,
        "total_cents": total,
        "file_path": file_path,
        "file_hash": _file_hash(file_path),
        "ocr_version": _text(inv, "ocr_version", "_source") or "unknown",
        "ocr_confidence": inv.get("ocr_confidence"),
    }


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}


def _find_existing(conn, invoice: dict):
    if invoice["seller_tax_no"]:
        row = conn.execute(
            "SELECT * FROM invoice_doc WHERE invoice_no=? AND seller_tax_no=? LIMIT 1",
            (invoice["invoice_no"], invoice["seller_tax_no"]),
        ).fetchone()
        if row:
            return row
    if invoice["file_hash"] and "file_hash" in _columns(conn, "invoice_doc"):
        return conn.execute(
            "SELECT * FROM invoice_doc WHERE file_hash=? LIMIT 1", (invoice["file_hash"],)
        ).fetchone()
    return None


def register_invoice(
    inv: dict,
    source_file: str | os.PathLike | None = None,
) -> IntegrationResult:
    try:
        normalized = normalize_invoice(inv, source_file)
    except ValueError as exc:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, str(exc))
    init_db()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _find_existing(conn, normalized)
        if existing:
            conn.rollback()
            return IntegrationResult(
                IntegrationStatus.BUSINESS_REJECTED,
                "该发票已经入库，不会重复处理。",
                dict(existing),
            )
        cols = _columns(conn, "invoice_doc")
        values: dict[str, Any] = {
            **normalized,
            "amount": normalized["net_amount_cents"] / 100.0,
            "tax": normalized["tax_cents"] / 100.0,
            "total": normalized["total_cents"] / 100.0,
            "status": "待确认",
            "payment_status": "未付款",
            "created_at": _now(),
            "updated_at": _now(),
            "idempotency_key": "invoice:%s:%s" % (
                normalized["invoice_no"],
                normalized["seller_tax_no"] or normalized["file_hash"] or "unknown",
            ),
        }
        selected = {key: value for key, value in values.items() if key in cols}
        names = ",".join(selected)
        marks = ",".join("?" for _ in selected)
        cur = conn.execute(
            f"INSERT INTO invoice_doc({names}) VALUES ({marks})", tuple(selected.values())
        )
        invoice_id = int(cur.lastrowid)
        conn.commit()
        row = conn.execute("SELECT * FROM invoice_doc WHERE id=?", (invoice_id,)).fetchone()
        return IntegrationResult(
            IntegrationStatus.SUCCESS,
            "发票已入库，等待人工确认。",
            dict(row),
        )
    except Exception as exc:
        conn.rollback()
        LOG.warning("Invoice registration failed: %s", type(exc).__name__)
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "发票入库失败，可稍后安全重试。",
        )
    finally:
        conn.close()


def list_invoices(status: str | None = None) -> list[dict]:
    init_db()
    conn = get_conn()
    try:
        sql = "SELECT * FROM invoice_doc"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY date DESC,id DESC"
        return [dict(row) for row in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def invoice_to_voucher(
    inv: dict | int,
    expense_account: str = "5602",
    partner_type: str = "供应商",
    partner_id: int | None = None,
    *,
    payment_status: str = "未付款",
    deductible_tax: bool = False,
    creator: str = "系统",
    submit: bool = True,
) -> IntegrationResult:
    """Create one idempotent voucher after explicit human confirmation."""
    init_db()
    if isinstance(inv, int):
        conn = get_conn()
        row = conn.execute("SELECT * FROM invoice_doc WHERE id=?", (inv,)).fetchone()
        conn.close()
        if not row:
            return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, "发票不存在。")
        invoice = dict(row)
    else:
        result = register_invoice(inv)
        if result.ok:
            invoice = result.data
        elif result.data and isinstance(result.data, dict):
            invoice = result.data
        else:
            return result
    if invoice.get("voucher_id"):
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            "该发票已关联凭证，不会重复入账。",
            invoice.get("voucher_id"),
        )
    if not partner_id:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            "请先确认供应商后再生成凭证。",
        )
    if payment_status not in {"未付款", "已付款"}:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, "付款状态无效。")
    total = int(invoice.get("total_cents") or round(float(invoice.get("total") or 0) * 100))
    net = int(invoice.get("net_amount_cents") or round(float(invoice.get("amount") or 0) * 100))
    tax = int(invoice.get("tax_cents") or round(float(invoice.get("tax") or 0) * 100))
    if total <= 0:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, "发票金额无效。")
    summary = "发票-%s" % invoice.get("invoice_no", "")
    lines = [
        {
            "account_code": expense_account,
            "summary": summary,
            "debit_cents": net if deductible_tax else total,
            "credit_cents": 0,
            "aux_type": "无",
            "aux_id": None,
        }
    ]
    if deductible_tax and tax:
        lines.append(
            {
                "account_code": "2221",
                "summary": summary + "-进项税",
                "debit_cents": tax,
                "credit_cents": 0,
                "aux_type": "无",
                "aux_id": None,
            }
        )
    credit_code = "1002" if payment_status == "已付款" else "2202"
    lines.append(
        {
            "account_code": credit_code,
            "summary": summary,
            "debit_cents": 0,
            "credit_cents": total,
            "aux_type": partner_type if credit_code == "2202" else "无",
            "aux_id": partner_id if credit_code == "2202" else None,
        }
    )
    ok, message, voucher_id = ledger.create_voucher(
        invoice.get("date") or _dt.date.today().isoformat(),
        summary,
        lines,
        creator=creator,
        source="invoice",
        ref_id=invoice["id"],
        idempotency_key="invoice:%s" % invoice["id"],
        submit=submit,
    )
    if not ok:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, message)
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE invoice_doc SET voucher_id=?,status=?,partner_id=?,expense_account=?,"
            "payment_status=?,updated_at=? WHERE id=? AND voucher_id IS NULL",
            (
                voucher_id,
                "待审核" if submit else "草稿",
                partner_id,
                expense_account,
                payment_status,
                _now(),
                invoice["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "发票已生成待审核凭证。" if submit else "发票已生成凭证草稿。",
        voucher_id,
    )


if __name__ == "__main__":
    print(json.dumps({"configured": bool(_agent_command())}, ensure_ascii=False))
