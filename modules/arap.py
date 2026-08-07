# -*- coding: utf-8 -*-
"""Accounts-receivable/payable partners, projects, balances, and allocations."""

from __future__ import annotations

import os
import sqlite3
import sys
from typing import Any


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db
from modules import ledger


PARTNER_TYPE_ALIASES = {
    "客户": "customer",
    "customer": "customer",
    "供应商": "supplier",
    "supplier": "supplier",
}
ITEM_TYPE_ALIASES = {
    "应收": "receivable",
    "receivable": "receivable",
    "应付": "payable",
    "payable": "payable",
}


def _partner_type(value: str) -> str:
    normalized = PARTNER_TYPE_ALIASES.get(str(value or "").strip())
    if not normalized:
        raise ValueError("往来方类型必须是客户/customer或供应商/supplier")
    return normalized


def add_partner(
    ptype: str,
    name: str,
    tax_no: str = "",
    contact: str = "",
    phone: str = "",
    addr: str = "",
    note: str = "",
) -> int:
    normalized_type = _partner_type(ptype)
    name = str(name or "").strip()
    if not name:
        raise ValueError("往来方名称不能为空")
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT INTO partner(type,name,tax_no,contact,phone,addr,note) VALUES (?,?,?,?,?,?,?)",
            (
                normalized_type,
                name,
                str(tax_no or "").strip(),
                str(contact or "").strip(),
                str(phone or "").strip(),
                str(addr or "").strip(),
                str(note or "").strip(),
            ),
        )
        partner_id = int(cur.lastrowid)
        db.append_audit_event(
            conn,
            "partner",
            partner_id,
            "partner_created",
            "系统",
            {"type": normalized_type, "name": name},
        )
    return partner_id


def list_partners(ptype: str | None = None) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        if ptype:
            normalized = _partner_type(ptype)
            legacy = "客户" if normalized == "customer" else "供应商"
            rows = conn.execute(
                "SELECT * FROM partner WHERE type IN (?,?) ORDER BY name,id", (normalized, legacy)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM partner ORDER BY type,name,id").fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["type"] = PARTNER_TYPE_ALIASES.get(item["type"], item["type"])
            output.append(item)
        return output
    finally:
        conn.close()


def partner_balance_cents(partner_id: int) -> tuple[int, int]:
    conn = db.get_conn()
    try:
        if not conn.execute("SELECT 1 FROM partner WHERE id=?", (partner_id,)).fetchone():
            return 0, 0
        receivable = conn.execute(
            "SELECT COALESCE(SUM(vl.debit_cents),0) d,COALESCE(SUM(vl.credit_cents),0) c "
            "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
            "WHERE vl.account_code='1122' AND vl.aux_id=? AND v.status='posted'",
            (partner_id,),
        ).fetchone()
        payable = conn.execute(
            "SELECT COALESCE(SUM(vl.debit_cents),0) d,COALESCE(SUM(vl.credit_cents),0) c "
            "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
            "WHERE vl.account_code='2202' AND vl.aux_id=? AND v.status='posted'",
            (partner_id,),
        ).fetchone()
        ar_cents = int(receivable["d"] or 0) - int(receivable["c"] or 0)
        ap_cents = int(payable["c"] or 0) - int(payable["d"] or 0)
        return ar_cents, ap_cents
    finally:
        conn.close()


def partner_balance(partner_id: int) -> tuple[float, float]:
    receivable, payable = partner_balance_cents(partner_id)
    return receivable / 100, payable / 100


def ar_ap_ledger(ptype: str, partner_id: int | None = None) -> list[dict[str, Any]]:
    normalized = _partner_type(ptype)
    account_code = "1122" if normalized == "customer" else "2202"
    conn = db.get_conn()
    try:
        sql = (
            "SELECT vl.*,v.date,v.vno,v.id voucher_id,v.summary vs,p.name pname "
            "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
            "LEFT JOIN partner p ON p.id=vl.aux_id "
            "WHERE vl.account_code=? AND v.status='posted'"
        )
        args: list[Any] = [account_code]
        if partner_id is not None:
            sql += " AND vl.aux_id=?"
            args.append(partner_id)
        sql += " ORDER BY v.date,v.id,vl.seq"
        output = []
        for row in conn.execute(sql, args):
            item = dict(row)
            item["debit"] = int(item["debit_cents"] or 0) / 100
            item["credit"] = int(item["credit_cents"] or 0) / 100
            output.append(item)
        return output
    finally:
        conn.close()


def ar_ap_summary() -> list[dict[str, Any]]:
    output = []
    for partner in list_partners():
        receivable_cents, payable_cents = partner_balance_cents(int(partner["id"]))
        if receivable_cents or payable_cents:
            output.append(
                {
                    "id": partner["id"],
                    "type": partner["type"],
                    "name": partner["name"],
                    "receivable_cents": receivable_cents,
                    "payable_cents": payable_cents,
                    "应收": receivable_cents / 100,
                    "应付": payable_cents / 100,
                }
            )
    return output


def create_arap_item(
    partner_id: int,
    item_type: str,
    amount: Any = None,
    *,
    amount_cents: int | None = None,
    reference_no: str = "",
    description: str = "",
    due_date: str | None = None,
    source_type: str | None = None,
    source_id: int | None = None,
    actor: str = "系统",
) -> tuple[bool, str, int | None]:
    """Create a traceable AR/AP project that cash documents can allocate against."""
    try:
        normalized_type = ITEM_TYPE_ALIASES.get(str(item_type or "").strip())
        if not normalized_type:
            raise ValueError("项目类型必须是应收/receivable或应付/payable")
        cents = (
            ledger._strict_cents(amount_cents, "amount_cents")
            if amount_cents is not None
            else ledger._money_to_cents(amount)
        )
        if cents <= 0:
            raise ValueError("应收应付项目金额必须大于零")
        if due_date:
            due_date = ledger._validate_date(due_date)
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("操作人不能为空")
        with db.transaction() as conn:
            partner = conn.execute("SELECT id,type FROM partner WHERE id=?", (partner_id,)).fetchone()
            if not partner:
                raise ValueError("往来方不存在")
            now = ledger._now()
            cur = conn.execute(
                "INSERT INTO arap_item(partner_id,item_type,reference_no,description,amount_cents,"
                "settled_cents,due_date,status,source_type,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,0,?,'open',?,?,?,?)",
                (
                    partner_id,
                    normalized_type,
                    str(reference_no or "").strip(),
                    str(description or "").strip(),
                    cents,
                    due_date,
                    str(source_type or "").strip() or None,
                    source_id,
                    now,
                    now,
                ),
            )
            item_id = int(cur.lastrowid)
            db.append_audit_event(
                conn,
                "arap_item",
                item_id,
                "arap_item_created",
                actor,
                {"partner_id": partner_id, "item_type": normalized_type, "amount_cents": cents},
            )
        return True, "应收应付项目已创建", item_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def list_arap_items(
    partner_id: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        sql = (
            "SELECT ai.*,p.name partner_name,p.type partner_type FROM arap_item ai "
            "JOIN partner p ON p.id=ai.partner_id WHERE 1=1"
        )
        args: list[Any] = []
        if partner_id is not None:
            sql += " AND ai.partner_id=?"
            args.append(partner_id)
        if status:
            if status not in ("open", "partial", "settled"):
                raise ValueError("应收应付项目状态无效")
            sql += " AND ai.status=?"
            args.append(status)
        sql += " ORDER BY ai.due_date IS NULL,ai.due_date,ai.id"
        output = []
        for row in conn.execute(sql, args):
            item = dict(row)
            item["amount"] = int(item["amount_cents"]) / 100
            item["settled"] = int(item["settled_cents"]) / 100
            item["outstanding_cents"] = int(item["amount_cents"]) - int(item["settled_cents"])
            item["outstanding"] = item["outstanding_cents"] / 100
            output.append(item)
        return output
    finally:
        conn.close()


if __name__ == "__main__":
    db.init_db()
    print(ar_ap_summary())
