# -*- coding: utf-8 -*-
"""报表导出服务：Excel / PDF。"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from modules.integration import IntegrationResult, IntegrationStatus

LOG = logging_setup.get_logger("export_service")


def _check_deps() -> IntegrationResult:
    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return IntegrationResult(IntegrationStatus.CONFIG_ERROR, "缺少 openpyxl，请运行: pip install openpyxl")
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return IntegrationResult(IntegrationStatus.CONFIG_ERROR, "缺少 fpdf2，请运行: pip install fpdf2")
    return IntegrationResult(IntegrationStatus.SUCCESS, "ok")


def _get_conn() -> sqlite3.Connection | None:
    try:
        import db
        return db.get_conn()
    except Exception as exc:
        LOG.error("获取数据库连接失败: %s", exc)
        return None


# ---------- 科目余额表 ----------

def export_balance_sheet(path: str | Path, fmt: str = "xlsx") -> IntegrationResult:
    """导出科目余额表。"""
    dep = _check_deps()
    if not dep.ok:
        return dep

    path = Path(path)
    conn = _get_conn()
    if conn is None:
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, "数据库连接失败")

    try:
        rows = conn.execute(
            """
            SELECT a.code, a.name, a.category,
                   COALESCE(SUM(CASE WHEN j.direction='借' THEN j.amount ELSE -j.amount END), 0) AS balance
            FROM account a
            LEFT JOIN journal j ON j.account_id = a.id
            GROUP BY a.id
            ORDER BY a.code
            """
        ).fetchall()
    except Exception as exc:
        LOG.error("查询科目余额失败: %s", exc)
        conn.close()
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, f"查询失败: {exc}")
    finally:
        conn.close()

    headers = ["科目编码", "科目名称", "类别", "余额"]
    data = [(r["code"], r["name"], r["category"], f"{r['balance']:.2f}") for r in rows]

    if fmt == "xlsx":
        return _write_xlsx(path, "科目余额表", headers, data)
    if fmt == "pdf":
        return _write_pdf(path, "科目余额表", headers, data)
    return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, f"不支持的格式: {fmt}")


# ---------- 凭证明细 ----------

def export_vouchers(path: str | Path, fmt: str = "xlsx", period: str | None = None) -> IntegrationResult:
    """导出凭证明细。"""
    dep = _check_deps()
    if not dep.ok:
        return dep

    path = Path(path)
    conn = _get_conn()
    if conn is None:
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, "数据库连接失败")

    try:
        sql = """
            SELECT v.voucher_no, v.period, v.date, v.summary, v.status,
                   GROUP_CONCAT(l.account_code || '|' || l.summary || '|' || l.debit || '|' || l.credit) AS lines
            FROM voucher v
            LEFT JOIN voucher_line l ON l.voucher_id = v.id
        """
        args: list[Any] = []
        if period:
            sql += " WHERE v.period = ?"
            args.append(period)
        sql += " GROUP BY v.id ORDER BY v.date DESC, v.voucher_no DESC"
        rows = conn.execute(sql, args).fetchall()
    except Exception as exc:
        LOG.error("查询凭证失败: %s", exc)
        conn.close()
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, f"查询失败: {exc}")
    finally:
        conn.close()

    headers = ["凭证号", "期间", "日期", "摘要", "状态", "分录明细"]
    data = []
    for r in rows:
        lines = r["lines"] or ""
        line_str = "; ".join(
            f"{p[0]} {p[1]} 借{p[2]} 贷{p[3]}" for p in (l.split("|") for l in lines.split(";")) if len(p) == 4
        )
        data.append((r["voucher_no"], r["period"], r["date"], r["summary"], r["status"], line_str))

    if fmt == "xlsx":
        return _write_xlsx(path, "凭证明细", headers, data)
    if fmt == "pdf":
        return _write_pdf(path, "凭证明细", headers, data)
    return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, f"不支持的格式: {fmt}")


# ---------- 发票明细 ----------

def export_invoices(path: str | Path, fmt: str = "xlsx", status: str | None = None) -> IntegrationResult:
    """导出发票明细。"""
    dep = _check_deps()
    if not dep.ok:
        return dep

    path = Path(path)
    conn = _get_conn()
    if conn is None:
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, "数据库连接失败")

    try:
        sql = "SELECT invoice_no, date, seller_name, buyer_name, total, tax, total_cents, status, created_at FROM invoice_doc"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY date DESC, created_at DESC"
        rows = conn.execute(sql, args).fetchall()
    except Exception as exc:
        LOG.error("查询发票失败: %s", exc)
        conn.close()
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, f"查询失败: {exc}")
    finally:
        conn.close()

    headers = ["发票号码", "开票日期", "销售方", "购买方", "价税合计", "税额", "状态", "入库时间"]
    data = [
        (r["invoice_no"], r["date"], r["seller_name"], r["buyer_name"], f"{r['total']:.2f}", f"{r['tax']:.2f}", r["status"], r["created_at"])
        for r in rows
    ]

    if fmt == "xlsx":
        return _write_xlsx(path, "发票明细", headers, data)
    if fmt == "pdf":
        return _write_pdf(path, "发票明细", headers, data)
    return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, f"不支持的格式: {fmt}")


# ---------- 底层写入 ----------

def _write_xlsx(path: Path, sheet_name: str, headers: list[str], data: list[tuple]) -> IntegrationResult:
    try:
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name
        ws.append(headers)
        for row in data:
            ws.append(row)
        # Auto width (best effort)
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    max_length = max(max_length, len(str(cell.value or "")))
                except Exception:
                    pass
            ws.column_dimensions[column_letter].width = min(max_length + 2, 60)
        path.parent.mkdir(parents=True, exist_ok=True)
        wb.save(str(path))
        return IntegrationResult(IntegrationStatus.SUCCESS, f"已导出: {path}")
    except Exception as exc:
        LOG.error("写入 Excel 失败: %s", exc)
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, f"Excel 导出失败: {exc}")


def _write_pdf(path: Path, title: str, headers: list[str], data: list[tuple]) -> IntegrationResult:
    try:
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_auto_page_break(auto=True, margin=15)
        # Use default built-in font (avoid Chinese font issues)
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, title, ln=True, align="L")
        pdf.ln(2)

        pdf.set_font("Helvetica", "B", 9)
        col_widths = _pdf_col_widths(headers)
        for h, w in zip(headers, col_widths):
            pdf.cell(w, 7, str(h), border=1, align="C")
        pdf.ln()

        pdf.set_font("Helvetica", "", 9)
        for row in data:
            for value, w in zip(row, col_widths):
                text = str(value) if value is not None else ""
                pdf.cell(w, 7, text, border=1, align="L")
            pdf.ln()

        path.parent.mkdir(parents=True, exist_ok=True)
        pdf.output(str(path))
        return IntegrationResult(IntegrationStatus.SUCCESS, f"已导出: {path}")
    except Exception as exc:
        LOG.error("写入 PDF 失败: %s", exc)
        return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, f"PDF 导出失败: {exc}")


def _pdf_col_widths(headers: list[str]) -> list[float]:
    """简易等宽列，合计 190mm (A4 宽 210mm - 边距)。"""
    n = len(headers)
    base = 190 / n
    return [base] * n
