# -*- coding: utf-8 -*-
"""进销存最小可用版：商品、入库/出库/期初、库存余额、销售成本自动入账。

设计：
- 库存成本采用移动加权平均，维护 ``stock_balance`` 表。
- 入库（purchase_in）自动生成凭证：借 库存商品(1405) 贷 应付账款(2202)。
- 出库（sale_out）自动生成凭证：借 主营业务成本(5401) 贷 库存商品(1405)，
  并借 应收账款/银行存款 贷 主营业务收入(5001)，使毛利可在报表中体现。
- 期初（initial）直接设置库存余额，不生成凭证。
"""
from __future__ import annotations

import datetime as _dt
import sqlite3
from typing import Any

import db
from modules import ledger


DOC_TYPES = ("purchase_in", "sale_out", "initial")
DOC_TYPE_LABELS = {"purchase_in": "采购入库", "sale_out": "销售出库", "initial": "期初建账"}
INVENTORY_CODE = "1405"
COGS_CODE = "5401"
REVENUE_CODE = "5001"
AP_CODE = "2202"
BANK_CODE = "1002"
AR_CODE = "1122"


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _post_system(conn: sqlite3.Connection, voucher_id: int, actor: str, reason: str) -> None:
    """提交→审核→记账（系统生成凭证，同一操作人带兼任原因）。"""
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "draft":
        ledger._transition_in_transaction(conn, voucher_id, "pending_review", actor, reason)
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "pending_review":
        ledger._transition_in_transaction(conn, voucher_id, "audited", actor, reason)
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "audited":
        ledger._transition_in_transaction(conn, voucher_id, "posted", actor, reason)


def add_product(
    name: str,
    *,
    sku: str | None = None,
    spec: str = "",
    unit: str = "个",
    category: str = "",
    note: str = "",
    cost_method: str = "weighted",
) -> tuple[bool, str, int | None]:
    name = (name or "").strip()
    if not name:
        return False, "商品名称不能为空", None
    try:
        with db.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO product(sku,name,spec,unit,category,cost_method,note,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (sku or None, name, spec, unit, category, cost_method, note, _now()),
            )
            pid = int(cur.lastrowid)
            db.append_audit_event(conn, "product", pid, "product_created", "系统", {"name": name})
        return True, f"商品 {name} 已添加", pid
    except sqlite3.IntegrityError:
        return False, "商品编号(SKU)已存在", None
    except sqlite3.Error as exc:
        return False, f"添加商品失败: {exc}", None


def list_products() -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM product ORDER BY id")]
    finally:
        conn.close()


def get_product(product_id: int) -> dict[str, Any] | None:
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT * FROM product WHERE id=?", (product_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _balance(conn: sqlite3.Connection, product_id: int) -> tuple[float, int]:
    row = conn.execute(
        "SELECT qty, amount_cents FROM stock_balance WHERE product_id=?", (product_id,)
    ).fetchone()
    if row:
        return float(row["qty"] or 0), int(row["amount_cents"] or 0)
    return 0.0, 0


def _apply_balance(conn: sqlite3.Connection, product_id: int, qty: float, amount_cents: int) -> None:
    avg = int(round(amount_cents / qty)) if qty else 0
    conn.execute(
        "INSERT INTO stock_balance(product_id,qty,amount_cents,avg_cost_cents,updated_at) "
        "VALUES (?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET "
        "qty=excluded.qty, amount_cents=excluded.amount_cents, "
        "avg_cost_cents=excluded.avg_cost_cents, updated_at=excluded.updated_at",
        (product_id, qty, amount_cents, avg, _now()),
    )


def _next_doc_no(conn: sqlite3.Connection, period: str) -> str:
    row = conn.execute("SELECT last_seq FROM stock_doc_sequence WHERE period=?", (period,)).fetchone()
    seq = (int(row["last_seq"]) + 1) if row else 1
    conn.execute(
        "INSERT INTO stock_doc_sequence(period,last_seq) VALUES (?,?) "
        "ON CONFLICT(period) DO UPDATE SET last_seq=excluded.last_seq",
        (period, seq),
    )
    return f"ST-{period}-{seq:04d}"


def create_stock_doc(
    doc_type: str,
    date_str: str,
    lines: list[dict[str, Any]],
    creator: str = "系统",
    *,
    partner_id: int | None = None,
    summary: str = "",
    auto_post: bool = True,
) -> tuple[bool, str, int | None]:
    """创建库存单据并更新库存余额，可选自动生成并记账对应凭证。

    lines 每项：``product_id, qty, unit_price_cents, sale_price_cents(可选)``。
    返回 (ok, message, doc_id)。
    """
    if doc_type not in DOC_TYPES:
        return False, f"不支持的单据类型: {doc_type}", None
    try:
        date_str = ledger._validate_date(date_str)
    except ValueError as exc:
        return False, str(exc), None
    period = date_str[:7]
    creator = (creator or "").strip() or "系统"
    summary = (summary or "").strip()
    materialized = list(lines or [])
    if not materialized:
        return False, "单据至少包含一行商品", None

    try:
        with db.transaction() as conn:
            if ledger._is_period_closed_conn(conn, period):
                return False, f"会计期间 {period} 已结账，不能新增库存单据", None

            doc_no = _next_doc_no(conn, period)
            now = _now()
            cur = conn.execute(
                "INSERT INTO stock_doc(doc_no,doc_type,date,period,partner_id,summary,status,"
                "creator,created_at,updated_at) VALUES (?,?,?,?,?,?,'posted',?,?,?)",
                (doc_no, doc_type, date_str, period, partner_id, summary, creator, now, now),
            )
            doc_id = int(cur.lastrowid)

            total_amount = 0
            total_sale = 0
            cogs_total = 0
            for idx, raw in enumerate(materialized, 1):
                pid = int(raw.get("product_id"))
                qty = float(raw.get("qty") or 0)
                if qty <= 0:
                    return False, f"第 {idx} 行数量必须大于零", None
                product = conn.execute("SELECT id,name FROM product WHERE id=?", (pid,)).fetchone()
                if not product:
                    return False, f"第 {idx} 行商品不存在", None
                unit_price = int(raw.get("unit_price_cents") or 0)
                sale_price = int(raw.get("sale_price_cents") or 0)
                amount = unit_price * int(round(qty))
                sale_amount = sale_price * int(round(qty))

                if doc_type == "purchase_in":
                    bal_qty, bal_amt = _balance(conn, pid)
                    new_qty = bal_qty + qty
                    new_amt = bal_amt + amount
                    _apply_balance(conn, pid, new_qty, new_amt)
                    total_amount += amount
                elif doc_type == "sale_out":
                    bal_qty, bal_amt = _balance(conn, pid)
                    if qty > bal_qty + 1e-9:
                        return False, f"商品 {product['name']} 库存不足（现有 {bal_qty}，需 {qty}）", None
                    avg = (bal_amt / bal_qty) if bal_qty else 0
                    cogs = int(round(avg * qty))
                    _apply_balance(conn, pid, bal_qty - qty, bal_amt - cogs)
                    cogs_total += cogs
                    total_sale += sale_amount
                else:  # initial
                    _apply_balance(conn, pid, qty, amount)
                    total_amount += amount

                conn.execute(
                    "INSERT INTO stock_doc_line(stock_doc_id,product_id,qty,unit_price_cents,"
                    "amount_cents,sale_price_cents,sale_amount_cents,note) VALUES (?,?,?,?,?,?,?,?)",
                    (doc_id, pid, qty, unit_price, amount, sale_price, sale_amount,
                     str(raw.get("note") or "")),
                )

            voucher_id = None
            if doc_type in ("purchase_in", "sale_out", "initial") and auto_post:
                voucher_lines = _build_voucher_lines(
                    doc_type, total_amount, cogs_total, total_sale, partner_id
                )
                if voucher_lines:
                    vid, vno, _ = ledger._create_voucher_in_transaction(
                        conn, date_str, summary or DOC_TYPE_LABELS[doc_type],
                        voucher_lines, creator, "inventory", doc_id, f"stock:{doc_id}",
                    )
                    _post_system(conn, vid, creator, "进销存自动入账")
                    voucher_id = vid
                    conn.execute("UPDATE stock_doc SET voucher_id=? WHERE id=?", (vid, doc_id))
                db.append_audit_event(
                    conn, "stock_doc", doc_id, "stock_doc_created", creator,
                    {"doc_no": doc_no, "doc_type": doc_type, "voucher_id": voucher_id},
                )
            return True, f"库存单据 {doc_no} 已保存" + ("并记账" if voucher_id else ""), doc_id
    except (ValueError, sqlite3.Error) as exc:
        return False, f"创建库存单据失败: {exc}", None


def _build_voucher_lines(
    doc_type: str,
    total_amount: int,
    cogs_total: int,
    total_sale: int,
    partner_id: int | None,
) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    if doc_type == "purchase_in" and total_amount > 0:
        lines.append({"account_code": INVENTORY_CODE, "summary": "采购入库",
                      "debit_cents": total_amount, "credit_cents": 0})
        lines.append({"account_code": AP_CODE, "summary": "采购入库-应付款",
                      "debit_cents": 0, "credit_cents": total_amount,
                      "aux_type": "supplier" if partner_id else None,
                      "aux_id": partner_id})
    elif doc_type == "sale_out":
        if cogs_total > 0:
            lines.append({"account_code": COGS_CODE, "summary": "销售成本",
                          "debit_cents": cogs_total, "credit_cents": 0})
            lines.append({"account_code": INVENTORY_CODE, "summary": "结转库存商品",
                          "debit_cents": 0, "credit_cents": cogs_total})
        if total_sale > 0:
            cust_code = AR_CODE if partner_id else BANK_CODE
            lines.append({"account_code": cust_code, "summary": "销售收款",
                          "debit_cents": total_sale, "credit_cents": 0,
                          "aux_type": "customer" if partner_id else None,
                          "aux_id": partner_id})
            lines.append({"account_code": REVENUE_CODE, "summary": "销售收入",
                          "debit_cents": 0, "credit_cents": total_sale})
    elif doc_type == "initial" and total_amount > 0:
        lines.append({"account_code": INVENTORY_CODE, "summary": "期初库存",
                      "debit_cents": total_amount, "credit_cents": 0})
        lines.append({"account_code": "3104", "summary": "期初库存-未分配利润",
                      "debit_cents": 0, "credit_cents": total_amount})
    return lines


def get_stock_balance() -> list[dict[str, Any]]:
    """返回当前库存：商品信息 + 数量 + 均价 + 金额。"""
    conn = db.get_conn()
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT p.id,p.sku,p.name,p.spec,p.unit,p.category,sb.qty,sb.amount_cents,"
                "sb.avg_cost_cents FROM product p LEFT JOIN stock_balance sb ON sb.product_id=p.id "
                "ORDER BY p.id"
            )
        ]
    finally:
        conn.close()


def stock_ledger(product_id: int) -> list[dict[str, Any]]:
    """返回某商品的出入库明细（按单据日期排序）。"""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT sd.date,sd.doc_no,sd.doc_type,sdl.qty,sdl.unit_price_cents,sdl.amount_cents,"
            "sdl.sale_price_cents,sdl.sale_amount_cents,sdl.note "
            "FROM stock_doc_line sdl JOIN stock_doc sd ON sd.id=sdl.stock_doc_id "
            "WHERE sdl.product_id=? ORDER BY sd.date,sd.id",
            (product_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_stock_docs(limit: int = 200) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT sd.*, COUNT(sdl.id) lines FROM stock_doc sd "
            "LEFT JOIN stock_doc_line sdl ON sdl.stock_doc_id=sd.id "
            "GROUP BY sd.id ORDER BY sd.date DESC, sd.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    db.init_db()
    print("products:", list_products())
    print("balance:", get_stock_balance())
