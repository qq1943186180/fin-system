# -*- coding: utf-8 -*-
"""Financial statements built from posted vouchers.

All calculations use integer cents.  The public functions keep the original
float fields for the Tk UI, and additionally expose ``*_cents`` fields for
tests, exports, and future integrations.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import datetime as _dt
from typing import Iterable, Sequence

from db import get_conn


POSTED_STATUSES = ("已记账", "posted")
CASH_CODES = ("1001", "1002", "1012")


class ReportBlockedError(RuntimeError):
    """Raised when unresolved data-quality issues make reports unsafe."""

    def __init__(self, issues: Sequence[dict]):
        self.issues = list(issues)
        super().__init__("存在未处理的数据异常，正式报表已暂停生成")


@dataclass(frozen=True)
class ReportRow:
    code: str
    name: str
    amount_cents: int

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "name": self.name,
            "amount_cents": self.amount_cents,
            "amount": _yuan(self.amount_cents),
        }


def _yuan(cents: int) -> float:
    return round(int(cents) / 100.0, 2)


def _validate_period(period: str) -> str:
    value = str(period or "").strip()
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m")
    except (TypeError, ValueError):
        raise ValueError("会计期间必须是有效的 YYYY-MM") from None
    if value != parsed.strftime("%Y-%m"):
        raise ValueError("会计期间必须是有效的 YYYY-MM")
    return value


def _table_columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute('PRAGMA table_info("%s")' % table)}


def _money_expr(conn, alias: str, cents_col: str, legacy_col: str) -> str:
    table = "voucher_line" if alias == "vl" else alias
    cols = _table_columns(conn, table)
    if cents_col in cols:
        return "COALESCE(%s.%s, 0)" % (alias, cents_col)
    return "CAST(ROUND(COALESCE(%s.%s, 0) * 100.0) AS INTEGER)" % (alias, legacy_col)


def _posted_clause(alias: str = "v") -> tuple[str, list[str]]:
    marks = ",".join("?" for _ in POSTED_STATUSES)
    return "%s.status IN (%s)" % (alias, marks), list(POSTED_STATUSES)


@contextmanager
def _snapshot(conn=None):
    own = conn is None
    c = conn or get_conn()
    try:
        if own:
            c.execute("BEGIN")
        yield c
    finally:
        if own:
            try:
                c.rollback()
            finally:
                c.close()


def get_blocking_issues(conn=None) -> list[dict]:
    """Return issues that must be resolved before formal reports are used."""
    with _snapshot(conn) as c:
        tables = {
            row[0]
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "data_issue" in tables:
            cols = _table_columns(c, "data_issue")
            severity_filter = ""
            args: list[object] = []
            if "severity" in cols:
                severity_filter = " AND severity IN ('blocking','BLOCKING','阻断')"
            rows = c.execute(
                "SELECT * FROM data_issue WHERE status IN ('open','OPEN','待处理')"
                + severity_filter
                + " ORDER BY id",
                args,
            ).fetchall()
            if rows:
                return [dict(row) for row in rows]

        # Legacy safety net before the migration has run.
        issues: list[dict] = []
        if "voucher_line" in tables:
            row = c.execute(
                "SELECT COUNT(*) FROM voucher_line "
                "WHERE account_code IS NULL OR TRIM(account_code)=''"
            ).fetchone()
            if row and row[0]:
                issues.append(
                    {
                        "id": "legacy-null-account",
                        "issue_code": "MISSING_ACCOUNT_CODE",
                        "severity": "blocking",
                        "status": "open",
                        "count": row[0],
                    }
                )
        return issues


def assert_reports_ready(conn=None) -> None:
    issues = get_blocking_issues(conn)
    if issues:
        raise ReportBlockedError(issues)


def _account_balances(conn, end_period: str) -> dict[str, int]:
    d_expr = _money_expr(conn, "vl", "debit_cents", "debit")
    c_expr = _money_expr(conn, "vl", "credit_cents", "credit")
    posted_sql, args = _posted_clause()
    rows = conn.execute(
        f"""
        SELECT a.code, a.direction,
               COALESCE(SUM({d_expr}), 0) AS debit_cents,
               COALESCE(SUM({c_expr}), 0) AS credit_cents
          FROM voucher_line vl
          JOIN voucher v ON v.id=vl.voucher_id
          JOIN account a ON a.code=vl.account_code
         WHERE {posted_sql} AND v.period<=?
         GROUP BY a.code, a.direction
        """,
        [*args, end_period],
    ).fetchall()
    result: dict[str, int] = {}
    for row in rows:
        debit = int(row["debit_cents"] or 0)
        credit = int(row["credit_cents"] or 0)
        result[row["code"]] = debit - credit if row["direction"] == "借" else credit - debit
    return result


def _occurrences(conn, start_period: str, end_period: str) -> dict[str, tuple[int, int]]:
    d_expr = _money_expr(conn, "vl", "debit_cents", "debit")
    c_expr = _money_expr(conn, "vl", "credit_cents", "credit")
    posted_sql, args = _posted_clause()
    rows = conn.execute(
        f"""
        SELECT vl.account_code,
               COALESCE(SUM({d_expr}), 0) debit_cents,
               COALESCE(SUM({c_expr}), 0) credit_cents
          FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id
         WHERE {posted_sql} AND v.period BETWEEN ? AND ?
         GROUP BY vl.account_code
        """,
        [*args, start_period, end_period],
    ).fetchall()
    return {
        row["account_code"]: (int(row["debit_cents"] or 0), int(row["credit_cents"] or 0))
        for row in rows
    }


ASSET_ITEMS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("货币资金", (("1001", 1), ("1002", 1), ("1012", 1))),
    ("应收票据", (("1121", 1),)),
    ("应收账款", (("1122", 1), ("1231", -1))),
    ("预付账款", (("1123", 1),)),
    ("其他应收款", (("1221", 1),)),
    ("存货", (("1401", 1), ("1403", 1), ("1405", 1))),
    ("长期股权投资", (("1511", 1),)),
    ("固定资产原价", (("1601", 1),)),
    ("减：累计折旧", (("1602", -1),)),
    ("无形资产", (("1701", 1), ("1702", -1))),
    ("其他资产", (("1901", 1),)),
)

LIABILITY_ITEMS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("短期借款", (("2001", 1),)),
    ("应付票据", (("2201", 1),)),
    ("应付账款", (("2202", 1),)),
    ("预收账款", (("2203", 1),)),
    ("应付职工薪酬", (("2211", 1),)),
    ("应交税费", (("2221", 1),)),
    ("应付利息", (("2231", 1),)),
    ("其他应付款", (("2241", 1),)),
    ("长期借款", (("2501", 1),)),
)

EQUITY_ITEMS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("实收资本", (("3001", 1),)),
    ("资本公积", (("3002", 1),)),
    ("盈余公积", (("3101", 1),)),
    ("本年利润", (("3103", 1),)),
    ("利润分配", (("3104", 1),)),
)


def _mapped_rows(
    balances: dict[str, int],
    definitions: Iterable[tuple[str, tuple[tuple[str, int], ...]]],
) -> list[dict]:
    rows: list[dict] = []
    for name, components in definitions:
        amount = sum(balances.get(code, 0) * sign for code, sign in components)
        if amount:
            rows.append(ReportRow("+".join(code for code, _ in components), name, amount).as_dict())
    return rows


def _income_core(conn, period: str) -> dict:
    year = period[:4]
    current = _occurrences(conn, period, period)
    ytd = _occurrences(conn, f"{year}-01", period)
    accounts = conn.execute(
        "SELECT code,name,direction FROM account WHERE category='损益' ORDER BY code"
    ).fetchall()

    def build(values: dict[str, tuple[int, int]]) -> dict:
        revenue: list[dict] = []
        expense: list[dict] = []
        revenue_total = 0
        expense_total = 0
        for account in accounts:
            debit, credit = values.get(account["code"], (0, 0))
            amount = credit - debit if account["direction"] == "贷" else debit - credit
            if not amount:
                continue
            row = ReportRow(account["code"], account["name"], amount).as_dict()
            if account["direction"] == "贷":
                revenue.append(row)
                revenue_total += amount
            else:
                expense.append(row)
                expense_total += amount
        return {
            "revenue": revenue,
            "expense": expense,
            "total_revenue_cents": revenue_total,
            "total_expense_cents": expense_total,
            "net_profit_cents": revenue_total - expense_total,
            "total_revenue": _yuan(revenue_total),
            "total_expense": _yuan(expense_total),
            "net_profit": _yuan(revenue_total - expense_total),
        }

    return {"current": build(current), "year_to_date": build(ytd)}


def balance_sheet(period: str, conn=None, *, allow_unresolved: bool = False) -> dict:
    with _snapshot(conn) as c:
        if not allow_unresolved:
            assert_reports_ready(c)
        balances = _account_balances(c, period)
        assets = _mapped_rows(balances, ASSET_ITEMS)
        liabilities = _mapped_rows(balances, LIABILITY_ITEMS)
        equity = _mapped_rows(balances, EQUITY_ITEMS)
        income = _income_core(c, period)["year_to_date"]
        unclosed = int(income["net_profit_cents"])
        if unclosed:
            equity.append(ReportRow("PL-OPEN", "未结转损益", unclosed).as_dict())
        total_assets = sum(row["amount_cents"] for row in assets)
        total_liabilities = sum(row["amount_cents"] for row in liabilities)
        total_equity = sum(row["amount_cents"] for row in equity)
        difference = total_assets - total_liabilities - total_equity
        return {
            "period": period,
            "assets": assets,
            "liabilities": liabilities,
            "equity": equity,
            "total_assets_cents": total_assets,
            "total_liabilities_cents": total_liabilities,
            "total_equity_cents": total_equity,
            "difference_cents": difference,
            "total_assets": _yuan(total_assets),
            "total_liabilities": _yuan(total_liabilities),
            "total_equity": _yuan(total_equity),
            "difference": _yuan(difference),
            "balanced": difference == 0,
        }


def income_statement(period: str, conn=None, *, allow_unresolved: bool = False) -> dict:
    with _snapshot(conn) as c:
        if not allow_unresolved:
            assert_reports_ready(c)
        result = _income_core(c, period)
        # Backwards-compatible top-level fields represent the selected month.
        return {"period": period, **result["current"], **result}


def _cash_balance(conn, end_period: str, before: bool = False) -> int:
    d_expr = _money_expr(conn, "vl", "debit_cents", "debit")
    c_expr = _money_expr(conn, "vl", "credit_cents", "credit")
    posted_sql, args = _posted_clause()
    operator = "<" if before else "<="
    marks = ",".join("?" for _ in CASH_CODES)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM({d_expr} - {c_expr}), 0)
          FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id
         WHERE {posted_sql} AND vl.account_code IN ({marks}) AND v.period {operator} ?
        """,
        [*args, *CASH_CODES, end_period],
    ).fetchone()
    return int(row[0] or 0)


def _cash_category(counterpart_codes: set[str]) -> str:
    if counterpart_codes & {"1511", "1601", "1602", "1701", "1702"}:
        return "investing"
    if counterpart_codes & {"2001", "2501", "3001", "3002"}:
        return "financing"
    return "operating"


def cash_flow_statement(period: str, conn=None, *, allow_unresolved: bool = False) -> dict:
    with _snapshot(conn) as c:
        if not allow_unresolved:
            assert_reports_ready(c)
        d_expr = _money_expr(c, "vl", "debit_cents", "debit")
        c_expr = _money_expr(c, "vl", "credit_cents", "credit")
        posted_sql, args = _posted_clause()
        rows = c.execute(
            f"""
            SELECT v.id, vl.account_code, {d_expr} debit_cents, {c_expr} credit_cents
              FROM voucher v JOIN voucher_line vl ON vl.voucher_id=v.id
             WHERE {posted_sql} AND v.period=? ORDER BY v.id,vl.seq
            """,
            [*args, period],
        ).fetchall()
        by_voucher: dict[int, list] = defaultdict(list)
        for row in rows:
            by_voucher[int(row["id"])].append(row)

        flows = defaultdict(int)
        for lines in by_voucher.values():
            cash_delta = sum(
                int(line["debit_cents"] or 0) - int(line["credit_cents"] or 0)
                for line in lines
                if line["account_code"] in CASH_CODES
            )
            if not cash_delta:
                continue
            counterparts = {
                line["account_code"]
                for line in lines
                if line["account_code"] not in CASH_CODES and line["account_code"]
            }
            category = _cash_category(counterparts)
            flows[category] += cash_delta
            if cash_delta > 0:
                flows[category + "_in"] += cash_delta
            else:
                flows[category + "_out"] += -cash_delta

        opening = _cash_balance(c, period, before=True)
        ending = _cash_balance(c, period)
        net = ending - opening
        classified = flows["operating"] + flows["investing"] + flows["financing"]
        unclassified = net - classified
        op_in = flows["operating_in"]
        op_out = flows["operating_out"]
        return {
            "period": period,
            "opening_cash_cents": opening,
            "operating_in_cents": op_in,
            "operating_out_cents": op_out,
            "operating_net_cents": flows["operating"],
            "investing_cents": flows["investing"],
            "financing_cents": flows["financing"],
            "unclassified_cents": unclassified,
            "net_cash_cents": net,
            "ending_cash_cents": ending,
            "opening_cash": _yuan(opening),
            "operating_in": _yuan(op_in),
            "operating_out": _yuan(op_out),
            "operating_net": _yuan(flows["operating"]),
            "investing": _yuan(flows["investing"]),
            "financing": _yuan(flows["financing"]),
            "unclassified": _yuan(unclassified),
            "net_cash": _yuan(net),
            "ending_cash": _yuan(ending),
            "reconciled": opening + net == ending and unclassified == 0,
        }


def account_balance_table(
    period: str,
    conn=None,
    *,
    include_zero: bool = False,
    allow_unresolved: bool = False,
) -> dict:
    """Return the standard account balance table for one accounting period."""
    period = _validate_period(period)
    with _snapshot(conn) as c:
        if not allow_unresolved:
            assert_reports_ready(c)
        d_expr = _money_expr(c, "vl", "debit_cents", "debit")
        c_expr = _money_expr(c, "vl", "credit_cents", "credit")
        rows = c.execute(
            f"""
            SELECT a.code,a.name,a.category,a.direction,
                   COALESCE(SUM(CASE WHEN v.period<? THEN {d_expr} ELSE 0 END),0) opening_debit_cents,
                   COALESCE(SUM(CASE WHEN v.period<? THEN {c_expr} ELSE 0 END),0) opening_credit_cents,
                   COALESCE(SUM(CASE WHEN v.period=? THEN {d_expr} ELSE 0 END),0) period_debit_cents,
                   COALESCE(SUM(CASE WHEN v.period=? THEN {c_expr} ELSE 0 END),0) period_credit_cents
              FROM account a
              LEFT JOIN voucher_line vl ON vl.account_code=a.code
              LEFT JOIN voucher v ON v.id=vl.voucher_id
                    AND v.status IN (?,?) AND v.period<=?
             GROUP BY a.code,a.name,a.category,a.direction
             ORDER BY a.code
            """,
            (period, period, period, period, *POSTED_STATUSES, period),
        ).fetchall()
        output: list[dict] = []
        totals = {key: 0 for key in (
            "opening_debit_cents", "opening_credit_cents",
            "period_debit_cents", "period_credit_cents",
            "ending_debit_cents", "ending_credit_cents",
        )}
        for row in rows:
            opening_net = int(row["opening_debit_cents"] or 0) - int(row["opening_credit_cents"] or 0)
            ending_net = opening_net + int(row["period_debit_cents"] or 0) - int(row["period_credit_cents"] or 0)
            item = {
                "code": row["code"],
                "name": row["name"],
                "category": row["category"],
                "direction": row["direction"],
                "opening_debit_cents": int(row["opening_debit_cents"] or 0),
                "opening_credit_cents": int(row["opening_credit_cents"] or 0),
                "period_debit_cents": int(row["period_debit_cents"] or 0),
                "period_credit_cents": int(row["period_credit_cents"] or 0),
                "ending_debit_cents": max(ending_net, 0),
                "ending_credit_cents": max(-ending_net, 0),
            }
            if not include_zero and not any(item[key] for key in totals):
                # The totals dictionary is only a key template here; use the
                # row fields directly so zero-balance chart rows stay hidden.
                if not any(item[key] for key in totals):
                    continue
            for key in totals:
                totals[key] += item[key]
            for key, value in list(item.items()):
                if key.endswith("_cents"):
                    item[key[:-6]] = _yuan(value)
            output.append(item)
        return {"period": period, "rows": output, "totals": totals}


def chronological_ledger(
    period: str | None = None,
    conn=None,
    *,
    start_period: str | None = None,
    end_period: str | None = None,
    allow_unresolved: bool = False,
) -> dict:
    """Return the posted journal (序时簿), ordered by date and voucher line."""
    if period:
        start_period = end_period = _validate_period(period)
    else:
        start_period = _validate_period(start_period or "1900-01")
        end_period = _validate_period(end_period or "2999-12")
    with _snapshot(conn) as c:
        if not allow_unresolved:
            assert_reports_ready(c)
        d_expr = _money_expr(c, "vl", "debit_cents", "debit")
        c_expr = _money_expr(c, "vl", "credit_cents", "credit")
        rows = c.execute(
            f"""
            SELECT v.id voucher_id,v.vno,v.date,v.period,v.summary voucher_summary,
                   v.creator,v.source,v.reversal_of_id,vl.seq,vl.account_code,
                   a.name account_name,vl.summary line_summary,
                   {d_expr} debit_cents,{c_expr} credit_cents,
                   vl.aux_type,vl.aux_id
              FROM voucher v
              JOIN voucher_line vl ON vl.voucher_id=v.id
              LEFT JOIN account a ON a.code=vl.account_code
             WHERE v.status IN (?,?) AND v.period BETWEEN ? AND ?
             ORDER BY v.date,v.vno,vl.seq
            """,
            (*POSTED_STATUSES, start_period, end_period),
        ).fetchall()
        output: list[dict] = []
        running_debit = 0
        running_credit = 0
        for row in rows:
            debit = int(row["debit_cents"] or 0)
            credit = int(row["credit_cents"] or 0)
            running_debit += debit
            running_credit += credit
            item = dict(row)
            item.update({
                "debit_cents": debit,
                "credit_cents": credit,
                "running_debit_cents": running_debit,
                "running_credit_cents": running_credit,
                "debit": _yuan(debit),
                "credit": _yuan(credit),
                "running_debit": _yuan(running_debit),
                "running_credit": _yuan(running_credit),
            })
            output.append(item)
        return {
            "start_period": start_period,
            "end_period": end_period,
            "rows": output,
            "total_debit_cents": running_debit,
            "total_credit_cents": running_credit,
            "total_debit": _yuan(running_debit),
            "total_credit": _yuan(running_credit),
            "balanced": running_debit == running_credit,
        }


def generate_report_bundle(period: str) -> dict:
    """Generate all statements from one read transaction snapshot."""
    with _snapshot() as conn:
        assert_reports_ready(conn)
        return {
            "period": period,
            "balance_sheet": balance_sheet(period, conn),
            "income_statement": income_statement(period, conn),
            "cash_flow_statement": cash_flow_statement(period, conn),
            "account_balance_table": account_balance_table(period, conn),
            "chronological_ledger": chronological_ledger(period, conn),
        }


if __name__ == "__main__":
    import json
    import sys

    selected = sys.argv[1] if len(sys.argv) > 1 else "2026-07"
    print(json.dumps(generate_report_bundle(selected), ensure_ascii=False, indent=2))
