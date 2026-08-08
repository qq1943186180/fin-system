# -*- coding: utf-8 -*-
"""期末处理：结转损益与年终结转。

设计要点（与现有账套引擎一致）：
- 结转凭证通过 ``ledger`` 创建，并自动走 提交→审核→记账 流程（同一操作人时
  填写兼任例外原因，符合分岗约束）。
- 凭证来源标记为 ``closing``，并以 ``idempotency_key`` 去重，重复点击不会重复生成。
- 结转必须在期间结账之前执行（记账守卫会阻止已结账期间的凭证过账）。
"""
from __future__ import annotations

import datetime as _dt
import re
import sqlite3
from typing import Any

import db
from modules import ledger


PROFIT_LOSS_CODE = "3103"  # 本年利润
RETENTION_CODE = "3104"    # 利润分配
CARRYOVER_SOURCE = "closing"


def _validate_period(period: str) -> str:
    value = str(period or "").strip()
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m")
    except (TypeError, ValueError):
        raise ValueError("会计期间必须是有效的 YYYY-MM") from None
    if value != parsed.strftime("%Y-%m"):
        raise ValueError("会计期间必须是有效的 YYYY-MM")
    return value


def _last_day_of_period(period: str) -> str:
    year, month = map(int, period.split("-"))
    if month == 12:
        nxt = _dt.date(year + 1, 1, 1)
    else:
        nxt = _dt.date(year, month + 1, 1)
    last = nxt - _dt.timedelta(days=1)
    return last.isoformat()


def _is_period_closed_conn(conn: sqlite3.Connection, period: str) -> bool:
    year, month = map(int, period.split("-"))
    return bool(
        conn.execute(
            "SELECT closed FROM period WHERE year=? AND month=? AND closed=1", (year, month)
        ).fetchone()
    )


def _occurrences(conn: sqlite3.Connection, start_period: str, end_period: str) -> dict[str, tuple[int, int]]:
    """Return {(account_code): (debit_cents, credit_cents)} for posted vouchers in range."""
    rows = conn.execute(
        """
        SELECT vl.account_code,
               COALESCE(SUM(vl.debit_cents), 0) debit_cents,
               COALESCE(SUM(vl.credit_cents), 0) credit_cents
          FROM voucher_line vl JOIN voucher v ON v.id = vl.voucher_id
         WHERE v.status IN ('posted','已记账') AND v.period BETWEEN ? AND ?
         GROUP BY vl.account_code
        """,
        (start_period, end_period),
    ).fetchall()
    return {row["account_code"]: (int(row["debit_cents"] or 0), int(row["credit_cents"] or 0)) for row in rows}


def _system_post_in_transaction(conn: sqlite3.Connection, voucher_id: int, actor: str, reason: str) -> None:
    """提交→审核→记账（同一操作人，填写兼任例外原因以满足分岗约束）。"""
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "draft":
        ledger._transition_in_transaction(conn, voucher_id, "pending_review", actor, reason)
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "pending_review":
        ledger._transition_in_transaction(conn, voucher_id, "audited", actor, reason)
    v = conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if v and v["status"] == "audited":
        ledger._transition_in_transaction(conn, voucher_id, "posted", actor, reason)


def carry_forward_profit_loss(period: str, actor: str, *, reason: str | None = None):
    """将某一会计期间的损益类科目余额结转到「本年利润」。

    返回 (ok: bool, message: str, voucher_id: int | None)。
    """
    period = _validate_period(period)
    actor = (actor or "").strip() or "系统"
    reason = reason or "期末自动结转损益"
    try:
        with db.transaction() as conn:
            if _is_period_closed_conn(conn, period):
                return False, f"会计期间 {period} 已结账，无法再结转损益", None
            existing = conn.execute(
                "SELECT id,vno,status FROM voucher WHERE source=? AND idempotency_key=?",
                (CARRYOVER_SOURCE, "carryover:" + period),
            ).fetchone()
            if existing:
                return True, f"本期结转损益凭证已存在（{existing['vno']}），无需重复操作", int(existing["id"])

            occ = _occurrences(conn, period, period)
            lines: list[dict[str, Any]] = []
            rev_total = 0
            exp_total = 0
            for code, (debit, credit) in occ.items():
                acct = conn.execute(
                    "SELECT name,direction,category FROM account WHERE code=?", (code,)
                ).fetchone()
                if not acct or acct["category"] != "损益":
                    continue
                if acct["direction"] == "贷":
                    net = credit - debit
                    if net <= 0:
                        continue
                    lines.append({
                        "account_code": code,
                        "summary": f"结转损益-{acct['name']}",
                        "debit_cents": net,
                        "credit_cents": 0,
                    })
                    rev_total += net
                else:
                    net = debit - credit
                    if net <= 0:
                        continue
                    lines.append({
                        "account_code": code,
                        "summary": f"结转损益-{acct['name']}",
                        "debit_cents": 0,
                        "credit_cents": net,
                    })
                    exp_total += net

            if not lines:
                return False, f"会计期间 {period} 无损益发生额，无需结转", None

            if rev_total > 0:
                lines.append({
                    "account_code": PROFIT_LOSS_CODE,
                    "summary": "结转损益-本年利润",
                    "debit_cents": 0,
                    "credit_cents": rev_total,
                })
            if exp_total > 0:
                lines.append({
                    "account_code": PROFIT_LOSS_CODE,
                    "summary": "结转损益-本年利润",
                    "debit_cents": exp_total,
                    "credit_cents": 0,
                })

            date_str = _last_day_of_period(period)
            voucher_id, vno, _ = ledger._create_voucher_in_transaction(
                conn, date_str, f"结转{period}损益",
                lines, "系统自动结转", CARRYOVER_SOURCE, None, "carryover:" + period,
            )
            _system_post_in_transaction(conn, voucher_id, actor, reason)
            db.append_audit_event(
                conn, "closing", voucher_id, "profit_loss_carried_forward", actor,
                {"period": period, "rev_total_cents": rev_total, "exp_total_cents": exp_total},
            )
            return True, f"已生成结转损益凭证 {vno} 并记账", voucher_id
    except (ValueError, sqlite3.Error) as exc:
        return False, f"结转损益失败: {exc}", None


def close_fiscal_year(year: str | int, actor: str, *, reason: str | None = None):
    """将「本年利润」余额结转到「利润分配-未分配利润」（年终结转）。

    返回 (ok: bool, message: str, voucher_id: int | None)。
    """
    year = str(year).strip()
    if not re.match(r"^\d{4}$", year):
        return False, "会计年度必须是 4 位年份", None
    actor = (actor or "").strip() or "系统"
    reason = reason or "年终结转本年利润"
    try:
        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT id,vno,status FROM voucher WHERE source=? AND idempotency_key=?",
                (CARRYOVER_SOURCE, "yearend:" + year),
            ).fetchone()
            if existing:
                return True, f"年终结转凭证已存在（{existing['vno']}），无需重复操作", int(existing["id"])

            net = ledger.account_balance_cents(PROFIT_LOSS_CODE, f"{year}-12")
            if net == 0:
                return False, f"{year} 年本年利润余额为 0，无需年终结转", None

            if net > 0:
                lines = [
                    {"account_code": PROFIT_LOSS_CODE, "summary": f"{year}年终结转",
                     "debit_cents": net, "credit_cents": 0},
                    {"account_code": RETENTION_CODE, "summary": f"{year}年终结转-未分配利润",
                     "debit_cents": 0, "credit_cents": net},
                ]
            else:
                amt = -net
                lines = [
                    {"account_code": PROFIT_LOSS_CODE, "summary": f"{year}年终结转",
                     "debit_cents": 0, "credit_cents": amt},
                    {"account_code": RETENTION_CODE, "summary": f"{year}年终结转-未分配利润",
                     "debit_cents": amt, "credit_cents": 0},
                ]

            date_str = f"{year}-12-31"
            voucher_id, vno, _ = ledger._create_voucher_in_transaction(
                conn, date_str, f"{year}年本年利润结转",
                lines, "系统自动结转", CARRYOVER_SOURCE, None, "yearend:" + year,
            )
            _system_post_in_transaction(conn, voucher_id, actor, reason)
            db.append_audit_event(
                conn, "closing", voucher_id, "fiscal_year_closed", actor,
                {"year": year, "net_profit_cents": net},
            )
            return True, f"已生成年终结转凭证 {vno} 并记账", voucher_id
    except (ValueError, sqlite3.Error) as exc:
        return False, f"年终结转失败: {exc}", None


def get_closing_status(period: str) -> dict[str, Any]:
    """返回期间结转状态，供界面展示。"""
    period = _validate_period(period)
    year = period[:4]
    conn = db.get_conn()
    try:
        carry = conn.execute(
            "SELECT vno,status FROM voucher WHERE source=? AND idempotency_key=?",
            (CARRYOVER_SOURCE, "carryover:" + period),
        ).fetchone()
        year_end = conn.execute(
            "SELECT vno,status FROM voucher WHERE source=? AND idempotency_key=?",
            (CARRYOVER_SOURCE, "yearend:" + year),
        ).fetchone()
        net_pl = ledger.account_balance_cents(PROFIT_LOSS_CODE, period)
        return {
            "period": period,
            "year": year,
            "carryover_vno": carry["vno"] if carry else None,
            "carryover_status": carry["status"] if carry else None,
            "yearend_vno": year_end["vno"] if year_end else None,
            "yearend_status": year_end["status"] if year_end else None,
            "current_profit_loss_cents": net_pl,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    import json
    print(json.dumps(get_closing_status("2026-07"), ensure_ascii=False, indent=2))
