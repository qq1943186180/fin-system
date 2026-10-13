# -*- coding: utf-8 -*-
"""Transactional accounting service: vouchers, periods, cash, and ledgers."""

from __future__ import annotations

import datetime as _dt
import json
import os
import sqlite3
import sys
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import db


VOUCHER_STATUSES = ("draft", "pending_review", "audited", "posted")
SOURCE_ALIASES = {
    "manual": "manual",
    "手工": "manual",
    "cash": "cash",
    "收付款": "cash",
    "expense": "expense",
    "报销": "expense",
    "invoice": "invoice",
    "发票": "invoice",
    "reversal": "reversal",
    "冲销": "reversal",
    "opening": "opening",
    "期初": "opening",
    "import": "import",
    "导入": "import",
    "closing": "closing",
    "结转": "closing",
    "期末": "closing",
    "inventory": "inventory",
    "进销存": "inventory",
    "库存": "inventory",
}
DOC_TYPE_ALIASES = {"receipt": "receipt", "收款": "receipt", "payment": "payment", "付款": "payment"}


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


# 🔴 日期/期间校验的**唯一实现**（2026-10-14 收敛）。
# 原先项目里有 4 份 _validate_period（gui/main 用正则、closing/ledger/reports 用
# strptime + 回环）和 2 份 _validate_date，接受域实测不一致：
#   * "0000-01" —— GUI 正则版放行、strptime 版拒绝（第 0 年不是合法日期）；
#   * None 入参 —— GUI 正则版抛 AttributeError（None.strip()），其余抛 ValueError，
#     而调用方普遍只捕 ValueError → 会漏。
# 现在 gui/main、modules/closing、modules/reports 全部委托到这两个函数。
# 改校验规则**只改这里**，并同步 tests/test_date_period_validation.py。
def _validate_date(date_str: str) -> str:
    """校验 ``YYYY-MM-DD``；要求**规范化**（不接受 ``20260701`` 之类的紧凑式）。

    ``None``/空串统一抛 ``ValueError``（不是 AttributeError）——
    调用方几乎都只捕 ``ValueError``。
    """
    value = str(date_str or "").strip()
    try:
        parsed = _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError("日期必须是有效的 YYYY-MM-DD") from None
    if value != parsed.isoformat():
        raise ValueError("日期必须是有效的 YYYY-MM-DD")
    return value


def _validate_period(period: str) -> str:
    """校验会计期间 ``YYYY-MM``；要求**规范化**（不接受 ``2026-7``）。

    用 strptime + 回环而非正则：能顺带拒掉 ``0000-01`` 这种"格式对但不是
    合法日期"的输入，且对 ``None`` 抛 ValueError 而非 AttributeError。
    """
    value = str(period or "").strip()
    try:
        parsed = _dt.datetime.strptime(value, "%Y-%m")
    except (TypeError, ValueError):
        raise ValueError("会计期间必须是有效的 YYYY-MM") from None
    if value != parsed.strftime("%Y-%m"):
        raise ValueError("会计期间必须是有效的 YYYY-MM")
    return value


def _period_of(date_str: str) -> str:
    return _validate_date(date_str)[:7]


def _money_to_cents(value: Any, field: str = "金额") -> int:
    """严格版：拒绝空值/布尔/超过两位小数。

    实现已收敛到 :mod:`modules.money`（2026-10-14）。
    """
    from modules import money

    try:
        return money.strict_cents(value, field)
    except money.MoneyParseError as exc:
        raise ValueError(str(exc)) from None


def _strict_cents(value: Any, field: str) -> int:
    """入参**已经是整数分**时的校验（与 _money_to_cents 不是同一事实）。"""
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field}必须是整数分")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip() and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"{field}必须是整数分")


def _line_cents(line: dict[str, Any], side: str) -> int:
    cents_key = f"{side}_cents"
    if cents_key in line and line[cents_key] is not None:
        cents = _strict_cents(line[cents_key], cents_key)
        if side in line and line[side] not in (None, ""):
            legacy = _money_to_cents(line[side], side)
            if legacy != cents:
                raise ValueError(f"{cents_key} 与 {side} 不一致")
        return cents
    return _money_to_cents(line.get(side, 0) or 0, side)


def _normalise_source(source: str) -> str:
    value = str(source or "manual").strip()
    normalized = SOURCE_ALIASES.get(value)
    if not normalized:
        raise ValueError(f"不支持的凭证来源: {value}")
    return normalized


def _normalise_idempotency_key(value: Any) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if len(key) > 200:
        raise ValueError("幂等标识不能超过 200 个字符")
    return key


def _is_period_closed_conn(conn: sqlite3.Connection, period: str) -> bool:
    year, month = map(int, period.split("-"))
    return bool(
        conn.execute(
            "SELECT closed FROM period WHERE year=? AND month=? AND closed=1", (year, month)
        ).fetchone()
    )


def is_period_closed(period: str) -> bool:
    period = _validate_period(period)
    conn = db.get_conn()
    try:
        return _is_period_closed_conn(conn, period)
    finally:
        conn.close()


def _normalise_lines(
    conn: sqlite3.Connection,
    lines: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    materialized = list(lines or [])
    if len(materialized) < 2:
        raise ValueError("凭证至少需要两行分录")
    normalized: list[dict[str, Any]] = []
    total_debit = 0
    total_credit = 0
    for index, raw in enumerate(materialized, 1):
        if not isinstance(raw, dict):
            raise ValueError(f"第 {index} 行分录格式无效")
        account_code = str(raw.get("account_code") or "").strip()
        if not account_code:
            raise ValueError(f"第 {index} 行会计科目不能为空")
        account = conn.execute(
            "SELECT code,is_leaf FROM account WHERE code=?", (account_code,)
        ).fetchone()
        if not account or int(account["is_leaf"] or 0) != 1:
            raise ValueError(f"第 {index} 行会计科目不存在或不是末级科目: {account_code}")
        if account_code == "UNRESOLVED":
            raise ValueError(f"第 {index} 行必须明确选择会计科目")
        debit_cents = _line_cents(raw, "debit")
        credit_cents = _line_cents(raw, "credit")
        if debit_cents < 0 or credit_cents < 0:
            raise ValueError(f"第 {index} 行借贷金额不能为负数")
        if (debit_cents > 0) == (credit_cents > 0):
            raise ValueError(f"第 {index} 行必须且只能填写借方或贷方金额")
        total_debit += debit_cents
        total_credit += credit_cents
        aux_type = str(raw.get("aux_type") or "").strip() or None
        aux_id = raw.get("aux_id")
        if aux_id in ("", None):
            aux_id = None
        else:
            try:
                aux_id = int(aux_id)
            except (TypeError, ValueError):
                raise ValueError(f"第 {index} 行辅助核算编号无效") from None
        normalized.append(
            {
                "account_code": account_code,
                "summary": str(raw.get("summary") or "").strip(),
                "debit_cents": debit_cents,
                "credit_cents": credit_cents,
                "aux_type": aux_type,
                "aux_id": aux_id,
            }
        )
    if total_debit <= 0 or total_credit <= 0:
        raise ValueError("凭证借贷金额必须大于零")
    if total_debit != total_credit:
        raise ValueError(
            f"借贷不平衡：借方 {total_debit / 100:.2f}，贷方 {total_credit / 100:.2f}"
        )
    return normalized


def validate_voucher_lines(
    lines: Iterable[dict[str, Any]],
) -> tuple[bool, str, list[dict[str, Any]]]:
    conn = db.get_conn()
    try:
        normalized = _normalise_lines(conn, lines)
        return True, "校验通过", normalized
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), []
    finally:
        conn.close()


def debit_credit_ok(lines: Iterable[dict[str, Any]]) -> bool:
    return validate_voucher_lines(lines)[0]


def _next_voucher_number(conn: sqlite3.Connection, period: str) -> str:
    row = conn.execute("SELECT last_seq FROM voucher_sequence WHERE period=?", (period,)).fetchone()
    if row:
        sequence = int(row["last_seq"]) + 1
        conn.execute("UPDATE voucher_sequence SET last_seq=? WHERE period=?", (sequence, period))
    else:
        sequence = 1
        conn.execute("INSERT INTO voucher_sequence(period,last_seq) VALUES (?,?)", (period, sequence))
    return f"{period}-{sequence:04d}"


def next_voucher_no(date_str: str) -> str:
    """Return a non-reserving preview. Actual allocation occurs in create_voucher."""
    period = _period_of(date_str)
    conn = db.get_conn()
    try:
        row = conn.execute("SELECT last_seq FROM voucher_sequence WHERE period=?", (period,)).fetchone()
        return f"{period}-{(int(row['last_seq']) + 1 if row else 1):04d}"
    finally:
        conn.close()


def _next_document_number(conn: sqlite3.Connection, document_type: str, period: str) -> str:
    row = conn.execute(
        "SELECT last_seq FROM document_sequence WHERE document_type=? AND period=?",
        (document_type, period),
    ).fetchone()
    if row:
        sequence = int(row["last_seq"]) + 1
        conn.execute(
            "UPDATE document_sequence SET last_seq=? WHERE document_type=? AND period=?",
            (sequence, document_type, period),
        )
    else:
        sequence = 1
        conn.execute(
            "INSERT INTO document_sequence(document_type,period,last_seq) VALUES (?,?,?)",
            (document_type, period, sequence),
        )
    prefix = "RCPT" if document_type == "receipt" else "PAY"
    return f"{prefix}-{period}-{sequence:04d}"


def _create_voucher_in_transaction(
    conn: sqlite3.Connection,
    date_str: str,
    summary: str,
    lines: Iterable[dict[str, Any]],
    creator: str,
    source: str,
    ref_id: int | None,
    idempotency_key: str | None,
    *,
    reversal_of_id: int | None = None,
) -> tuple[int, str, bool]:
    date_str = _validate_date(date_str)
    period = date_str[:7]
    summary = str(summary or "").strip()
    creator = str(creator or "").strip()
    source = _normalise_source(source)
    idempotency_key = _normalise_idempotency_key(idempotency_key)
    if not summary:
        raise ValueError("凭证摘要不能为空")
    if not creator:
        raise ValueError("制单人不能为空")
    if _is_period_closed_conn(conn, period):
        raise ValueError(f"会计期间 {period} 已结账，不能新增凭证")
    if idempotency_key:
        existing = conn.execute(
            "SELECT id,vno FROM voucher WHERE source=? AND idempotency_key=?",
            (source, idempotency_key),
        ).fetchone()
        if existing:
            return int(existing["id"]), str(existing["vno"]), False
    normalized = _normalise_lines(conn, lines)
    vno = _next_voucher_number(conn, period)
    now = _now()
    cur = conn.execute(
        "INSERT INTO voucher(vno,date,period,summary,creator,status,source,ref_id,created_at,"
        "reversal_of_id,idempotency_key,updated_at) VALUES (?,?,?,?,?,'draft',?,?,?,?,?,?)",
        (
            vno,
            date_str,
            period,
            summary,
            creator,
            source,
            ref_id,
            now,
            reversal_of_id,
            idempotency_key,
            now,
        ),
    )
    voucher_id = int(cur.lastrowid)
    for seq, line in enumerate(normalized, 1):
        conn.execute(
            "INSERT INTO voucher_line(voucher_id,seq,account_code,summary,debit,credit,"
            "debit_cents,credit_cents,aux_type,aux_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                voucher_id,
                seq,
                line["account_code"],
                line["summary"],
                line["debit_cents"] / 100,
                line["credit_cents"] / 100,
                line["debit_cents"],
                line["credit_cents"],
                line["aux_type"],
                line["aux_id"],
            ),
        )
    db.append_audit_event(
        conn,
        "voucher",
        voucher_id,
        "voucher_created",
        creator,
        {
            "vno": vno,
            "source": source,
            "ref_id": ref_id,
            "idempotency_key": idempotency_key,
            "debit_cents": sum(line["debit_cents"] for line in normalized),
        },
    )
    return voucher_id, vno, True


def create_voucher(
    date_str: str,
    summary: str,
    lines: Iterable[dict[str, Any]],
    creator: str = "系统",
    source: str = "manual",
    ref_id: int | None = None,
    idempotency_key: str | None = None,
    submit: bool = False,
) -> tuple[bool, str, int | None]:
    """Create a balanced draft; integer-cent line fields are authoritative."""
    try:
        with db.transaction() as conn:
            voucher_id, vno, created = _create_voucher_in_transaction(
                conn, date_str, summary, lines, creator, source, ref_id, idempotency_key
            )
            if submit and created:
                _transition_in_transaction(conn, voucher_id, "pending_review", creator, None)
        if not created:
            return True, f"相同业务已生成凭证 {vno}，未重复创建", voucher_id
        suffix = "并已提交审核" if submit else "（草稿）"
        return True, f"凭证 {vno} 已保存{suffix}", voucher_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def _voucher_issue_exists(conn: sqlite3.Connection, voucher_id: int) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM data_issue di LEFT JOIN voucher_line vl "
            "ON di.entity_type='voucher_line' AND di.entity_id=vl.id "
            "WHERE di.status='open' AND di.severity='blocking' AND "
            "((di.entity_type='voucher' AND di.entity_id=?) OR vl.voucher_id=?) LIMIT 1",
            (voucher_id, voucher_id),
        ).fetchone()
    )


def _sync_source_status(conn: sqlite3.Connection, voucher: sqlite3.Row, status: str) -> None:
    source = voucher["source"]
    ref_id = voucher["ref_id"]
    if ref_id is None:
        return
    now = _now()
    if source == "cash":
        conn.execute("UPDATE cash_doc SET status=?,updated_at=? WHERE id=?", (status, now, ref_id))
        if status == "posted":
            related = conn.execute(
                "SELECT related_type,related_id FROM cash_doc WHERE id=?", (ref_id,)
            ).fetchone()
            if related and related["related_type"] == "expense_payment" and related["related_id"]:
                conn.execute(
                    "UPDATE expense SET status='paid',payment_cash_doc_id=?,paid_at=?,updated_at=? "
                    "WHERE id=? AND status='accounted'",
                    (ref_id, now, now, related["related_id"]),
                )
    elif source == "expense":
        mapped = {
            "draft": "approved",
            "pending_review": "accounting_pending",
            "audited": "accounting_audited",
            "posted": "accounted",
        }[status]
        conn.execute(
            "UPDATE expense SET status=?,accounted_at=CASE WHEN ?='posted' THEN ? ELSE accounted_at END,"
            "updated_at=? WHERE id=? AND status<>'paid'",
            (mapped, status, now, now, ref_id),
        )
    elif source == "invoice":
        mapped = {
            "draft": "confirmed",
            "pending_review": "pending_review",
            "audited": "audited",
            "posted": "posted",
        }[status]
        conn.execute(
            "UPDATE invoice_doc SET status=?,updated_at=? WHERE id=?", (mapped, now, ref_id)
        )


def _transition_in_transaction(
    conn: sqlite3.Connection,
    voucher_id: int,
    target: str,
    actor: str,
    exception_reason: str | None,
) -> tuple[bool, str]:
    actor = str(actor or "").strip()
    reason = str(exception_reason or "").strip() or None
    if not actor:
        raise ValueError("操作人不能为空")
    voucher = conn.execute("SELECT * FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    if not voucher:
        raise ValueError("凭证不存在")
    current = voucher["status"]
    expected = {"pending_review": "draft", "audited": "pending_review", "posted": "audited"}[target]
    if current == target:
        return True, "凭证已经处于目标状态"
    if current != expected:
        raise ValueError(f"凭证状态必须从 {expected} 变更为 {target}，当前为 {current}")
    if _is_period_closed_conn(conn, voucher["period"]):
        raise ValueError(f"会计期间 {voucher['period']} 已结账")
    if target in ("audited", "posted"):
        lines = [dict(row) for row in conn.execute(
            "SELECT * FROM voucher_line WHERE voucher_id=? ORDER BY seq", (voucher_id,)
        )]
        _normalise_lines(conn, lines)
        if _voucher_issue_exists(conn, voucher_id):
            raise ValueError("凭证存在未解决的阻断级数据问题")
    if target == "audited" and actor in {voucher["creator"], voucher["submitted_by"]} and not reason:
        raise ValueError("制单/提交人与审核人为同一人时，必须填写兼任例外原因")
    if target == "posted" and actor in {voucher["creator"], voucher["reviewer"]} and not reason:
        raise ValueError("制单/审核人与记账人为同一人时，必须填写兼任例外原因")
    now = _now()
    if target == "pending_review":
        conn.execute(
            "UPDATE voucher SET status=?,submitted_by=?,submitted_at=?,updated_at=? WHERE id=?",
            (target, actor, now, now, voucher_id),
        )
        event = "voucher_submitted"
    elif target == "audited":
        conn.execute(
            "UPDATE voucher SET status=?,reviewer=?,audited_at=?,exception_reason=?,updated_at=? WHERE id=?",
            (target, actor, now, reason, now, voucher_id),
        )
        event = "voucher_audited"
    else:
        conn.execute(
            "UPDATE voucher SET status=?,poster=?,posted_at=?,exception_reason=COALESCE(?,exception_reason),"
            "updated_at=? WHERE id=?",
            (target, actor, now, reason, now, voucher_id),
        )
        event = "voucher_posted"
    updated = conn.execute("SELECT * FROM voucher WHERE id=?", (voucher_id,)).fetchone()
    _sync_source_status(conn, updated, target)
    db.append_audit_event(
        conn,
        "voucher",
        voucher_id,
        event,
        actor,
        {"from": current, "to": target, "exception_reason": reason},
    )
    return True, f"凭证已变更为 {target}"


def _transition(
    voucher_id: int,
    target: str,
    actor: str,
    exception_reason: str | None = None,
) -> tuple[bool, str]:
    try:
        with db.transaction() as conn:
            return _transition_in_transaction(conn, voucher_id, target, actor, exception_reason)
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc)


def submit_voucher(voucher_id: int, actor: str = "系统") -> tuple[bool, str]:
    return _transition(voucher_id, "pending_review", actor)


def audit_voucher(
    voucher_id: int,
    reviewer: str = "审核员",
    exception_reason: str | None = None,
) -> tuple[bool, str]:
    return _transition(voucher_id, "audited", reviewer, exception_reason)


def post_voucher(
    voucher_id: int,
    poster: str = "记账员",
    exception_reason: str | None = None,
) -> tuple[bool, str]:
    return _transition(voucher_id, "posted", poster, exception_reason)


def delete_draft_voucher(
    voucher_id: int,
    actor: str,
    reason: str,
) -> tuple[bool, str, int | None]:
    """Delete an unsubmitted manual draft while preserving an audit event.

    Posted and workflow-linked vouchers are deliberately never physically
    deleted.  They must be reversed so the accounting trail remains intact.
    """
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor or not reason:
        return False, "删除草稿必须填写操作人和原因", None
    try:
        with db.transaction() as conn:
            voucher = conn.execute("SELECT * FROM voucher WHERE id=?", (voucher_id,)).fetchone()
            if not voucher:
                raise ValueError("凭证不存在")
            if voucher["status"] != "draft":
                raise ValueError("只有草稿凭证可以删除；已记账凭证请生成冲销凭证")

            linked = []
            for table in ("expense", "cash_doc", "invoice_doc"):
                row = conn.execute(
                    f"SELECT id FROM {table} WHERE voucher_id=? LIMIT 1", (voucher_id,)
                ).fetchone()
                if row:
                    linked.append(f"{table}:{row['id']}")
            if linked:
                raise ValueError("凭证已关联业务单据，不能删除：" + ", ".join(linked))

            db.append_audit_event(
                conn,
                "voucher",
                voucher_id,
                "voucher_deleted",
                actor,
                {"vno": voucher["vno"], "reason": reason, "status": voucher["status"]},
            )
            conn.execute("DELETE FROM voucher WHERE id=?", (voucher_id,))
        return True, f"草稿凭证 {voucher['vno']} 已删除", voucher_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def reverse_voucher(
    voucher_id: int,
    date_str: str,
    actor: str,
    reason: str,
) -> tuple[bool, str, int | None]:
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    if not actor or not reason:
        return False, "冲销人和冲销原因不能为空", None
    try:
        with db.transaction() as conn:
            original = conn.execute("SELECT * FROM voucher WHERE id=?", (voucher_id,)).fetchone()
            if not original:
                raise ValueError("原凭证不存在")
            if original["status"] != "posted":
                raise ValueError("只有已记账凭证可以冲销")
            original_lines = conn.execute(
                "SELECT * FROM voucher_line WHERE voucher_id=? ORDER BY seq", (voucher_id,)
            ).fetchall()
            reversal_lines = [
                {
                    "account_code": row["account_code"],
                    "summary": f"冲销：{row['summary'] or original['summary']}",
                    "debit_cents": int(row["credit_cents"]),
                    "credit_cents": int(row["debit_cents"]),
                    "aux_type": row["aux_type"],
                    "aux_id": row["aux_id"],
                }
                for row in original_lines
            ]
            new_id, vno, created = _create_voucher_in_transaction(
                conn,
                date_str,
                f"冲销 {original['vno']}：{reason}",
                reversal_lines,
                actor,
                "reversal",
                voucher_id,
                f"reversal:{voucher_id}",
                reversal_of_id=voucher_id,
            )
            if created:
                db.append_audit_event(
                    conn,
                    "voucher",
                    voucher_id,
                    "reversal_created",
                    actor,
                    {"reversal_voucher_id": new_id, "reversal_vno": vno, "reason": reason},
                )
        if not created:
            return True, f"该凭证已有冲销草稿 {vno}", new_id
        return True, f"冲销凭证 {vno} 已生成（草稿），需按正常流程审核记账", new_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def close_period(period: str, actor: str) -> tuple[bool, str]:
    try:
        period = _validate_period(period)
        actor = str(actor or "").strip()
        if not actor:
            raise ValueError("结账人不能为空")
        year, month = map(int, period.split("-"))
        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM period WHERE year=? AND month=?", (year, month)
            ).fetchone()
            if existing and int(existing["closed"] or 0) == 1:
                return True, f"会计期间 {period} 已结账"
            pending = int(
                conn.execute(
                    "SELECT COUNT(*) FROM voucher WHERE period=? AND status<>'posted'", (period,)
                ).fetchone()[0]
            )
            if pending:
                raise ValueError(f"会计期间 {period} 还有 {pending} 张凭证未记账")
            blocking = int(
                conn.execute(
                    "SELECT COUNT(*) FROM data_issue di LEFT JOIN voucher_line vl "
                    "ON di.entity_type='voucher_line' AND di.entity_id=vl.id "
                    "LEFT JOIN voucher v ON v.id=COALESCE(vl.voucher_id,CASE WHEN di.entity_type='voucher' THEN di.entity_id END) "
                    "WHERE di.status='open' AND di.severity='blocking' AND v.period=?",
                    (period,),
                ).fetchone()[0]
            )
            if blocking:
                raise ValueError(f"会计期间 {period} 还有 {blocking} 个阻断级数据问题")
            unbalanced = int(
                conn.execute(
                    "SELECT COUNT(*) FROM (SELECT v.id FROM voucher v JOIN voucher_line vl ON vl.voucher_id=v.id "
                    "WHERE v.period=? GROUP BY v.id HAVING SUM(vl.debit_cents)<>SUM(vl.credit_cents) "
                    "OR SUM(vl.debit_cents)<=0)",
                    (period,),
                ).fetchone()[0]
            )
            if unbalanced:
                raise ValueError(f"会计期间 {period} 存在 {unbalanced} 张不平衡凭证")
            now = _now()
            conn.execute(
                "INSERT INTO period(year,month,closed,closed_by,closed_at) VALUES (?,?,1,?,?) "
                "ON CONFLICT(year,month) DO UPDATE SET closed=1,closed_by=excluded.closed_by,"
                "closed_at=excluded.closed_at",
                (year, month, actor, now),
            )
            period_id = int(
                conn.execute("SELECT id FROM period WHERE year=? AND month=?", (year, month)).fetchone()[0]
            )
            db.append_audit_event(conn, "period", period_id, "period_closed", actor, {"period": period})
        return True, f"会计期间 {period} 已结账"
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc)


def reopen_period(period: str, actor: str, reason: str) -> tuple[bool, str]:
    try:
        period = _validate_period(period)
        actor = str(actor or "").strip()
        reason = str(reason or "").strip()
        if not actor or not reason:
            raise ValueError("反结账操作人和原因不能为空")
        year, month = map(int, period.split("-"))
        with db.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM period WHERE year=? AND month=?", (year, month)
            ).fetchone()
            if not row or int(row["closed"] or 0) != 1:
                raise ValueError(f"会计期间 {period} 尚未结账")
            now = _now()
            conn.execute(
                "UPDATE period SET closed=0,reopened_by=?,reopened_at=?,reopen_reason=? WHERE id=?",
                (actor, now, reason, row["id"]),
            )
            db.append_audit_event(
                conn, "period", int(row["id"]), "period_reopened", actor, {"period": period, "reason": reason}
            )
        return True, f"会计期间 {period} 已反结账"
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc)


def get_vouchers(period: str | None = None, status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        sql = "SELECT v.*,EXISTS(SELECT 1 FROM voucher r WHERE r.reversal_of_id=v.id) is_reversed FROM voucher v WHERE 1=1"
        args: list[Any] = []
        if period:
            sql += " AND v.period=?"
            args.append(_validate_period(period))
        if status:
            if status not in VOUCHER_STATUSES:
                raise ValueError("凭证状态无效")
            sql += " AND v.status=?"
            args.append(status)
        sql += " ORDER BY v.date DESC,v.id DESC LIMIT ?"
        args.append(max(1, min(int(limit), 5000)))
        result = []
        for row in conn.execute(sql, args):
            lines = []
            for line in conn.execute(
                "SELECT vl.*,a.name account_name FROM voucher_line vl "
                "LEFT JOIN account a ON a.code=vl.account_code WHERE vl.voucher_id=? ORDER BY vl.seq",
                (row["id"],),
            ):
                item = dict(line)
                item["debit"] = int(item["debit_cents"] or 0) / 100
                item["credit"] = int(item["credit_cents"] or 0) / 100
                lines.append(item)
            result.append({"voucher": dict(row), "lines": lines})
        return result
    finally:
        conn.close()


def list_vouchers(period: str | None = None) -> list[dict[str, Any]]:
    conn = db.get_conn()
    try:
        sql = (
            "SELECT v.id,v.vno,v.date,v.period,v.summary,v.status,v.source,v.creator,"
            "COUNT(vl.id) lines,COALESCE(SUM(vl.debit_cents),0) debit_cents,"
            "COALESCE(SUM(vl.credit_cents),0) credit_cents "
            "FROM voucher v LEFT JOIN voucher_line vl ON vl.voucher_id=v.id"
        )
        args: list[Any] = []
        if period:
            sql += " WHERE v.period=?"
            args.append(_validate_period(period))
        sql += " GROUP BY v.id ORDER BY v.date DESC,v.id DESC"
        rows = []
        for row in conn.execute(sql, args):
            item = dict(row)
            item["debit"] = int(item["debit_cents"]) / 100
            item["credit"] = int(item["credit_cents"]) / 100
            rows.append(item)
        return rows
    finally:
        conn.close()


def account_balance_cents(account_code: str, end_period: str) -> int:
    end_period = _validate_period(end_period)
    conn = db.get_conn()
    try:
        account = conn.execute("SELECT direction FROM account WHERE code=?", (account_code,)).fetchone()
        if not account:
            return 0
        row = conn.execute(
            "SELECT COALESCE(SUM(vl.debit_cents),0) d,COALESCE(SUM(vl.credit_cents),0) c "
            "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
            "WHERE vl.account_code=? AND v.status='posted' AND v.period<=?",
            (account_code, end_period),
        ).fetchone()
        debit = int(row["d"] or 0)
        credit = int(row["c"] or 0)
        return debit - credit if account["direction"] in ("借", "debit") else credit - debit
    finally:
        conn.close()


def account_balance(account_code: str, end_period: str) -> float:
    return account_balance_cents(account_code, end_period) / 100


def trial_balance(end_period: str) -> tuple[list[dict[str, Any]], float, float]:
    end_period = _validate_period(end_period)
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT a.code,a.name,COALESCE(SUM(CASE WHEN v.status='posted' AND v.period<=? "
            "THEN vl.debit_cents ELSE 0 END),0) d,"
            "COALESCE(SUM(CASE WHEN v.status='posted' AND v.period<=? THEN vl.credit_cents ELSE 0 END),0) c "
            "FROM account a LEFT JOIN voucher_line vl ON vl.account_code=a.code "
            "LEFT JOIN voucher v ON v.id=vl.voucher_id GROUP BY a.code,a.name ORDER BY a.code",
            (end_period, end_period),
        ).fetchall()
        output: list[dict[str, Any]] = []
        total_debit = 0
        total_credit = 0
        for row in rows:
            net = int(row["d"] or 0) - int(row["c"] or 0)
            if net == 0:
                continue
            debit_balance = max(net, 0)
            credit_balance = max(-net, 0)
            total_debit += debit_balance
            total_credit += credit_balance
            output.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "debit_balance_cents": debit_balance,
                    "credit_balance_cents": credit_balance,
                    "debit_balance": debit_balance / 100,
                    "credit_balance": credit_balance / 100,
                }
            )
        return output, total_debit / 100, total_credit / 100
    finally:
        conn.close()


def _prev_period(period: str) -> str:
    period = _validate_period(period)
    year, month = map(int, period.split("-"))
    month -= 1
    if month == 0:
        year -= 1
        month = 12
    return f"{year:04d}-{month:02d}"


def ledger(account_code: str, period: str | None = None) -> dict[str, Any]:
    conn = db.get_conn()
    try:
        account = conn.execute("SELECT * FROM account WHERE code=?", (account_code,)).fetchone()
        if not account:
            raise ValueError("会计科目不存在")
        args: list[Any] = [account_code]
        sql = (
            "SELECT vl.*,v.date,v.vno,v.summary vsum FROM voucher_line vl "
            "JOIN voucher v ON v.id=vl.voucher_id WHERE vl.account_code=? AND v.status='posted'"
        )
        opening_cents = 0
        if period:
            period = _validate_period(period)
            sql += " AND v.period=?"
            args.append(period)
            previous = _prev_period(period)
            row = conn.execute(
                "SELECT COALESCE(SUM(vl.debit_cents),0) d,COALESCE(SUM(vl.credit_cents),0) c "
                "FROM voucher_line vl JOIN voucher v ON v.id=vl.voucher_id "
                "WHERE vl.account_code=? AND v.status='posted' AND v.period<=?",
                (account_code, previous),
            ).fetchone()
            raw_opening = int(row["d"] or 0) - int(row["c"] or 0)
            opening_cents = raw_opening if account["direction"] in ("借", "debit") else -raw_opening
        sql += " ORDER BY v.date,v.id,vl.seq"
        balance = opening_cents
        entries = []
        for row in conn.execute(sql, args):
            debit = int(row["debit_cents"] or 0)
            credit = int(row["credit_cents"] or 0)
            balance += debit - credit if account["direction"] in ("借", "debit") else credit - debit
            entries.append(
                {
                    "date": row["date"],
                    "vno": row["vno"],
                    "voucher_id": row["voucher_id"],
                    "summary": row["summary"] or row["vsum"],
                    "debit_cents": debit,
                    "credit_cents": credit,
                    "balance_cents": balance,
                    "debit": debit / 100,
                    "credit": credit / 100,
                    "balance": balance / 100,
                }
            )
        return {
            "account": dict(account),
            "opening_cents": opening_cents,
            "opening": opening_cents / 100,
            "entries": entries,
        }
    finally:
        conn.close()


def create_cash_doc(
    doc_type: str,
    date_str: str,
    partner_type: str | None,
    partner_id: int | None,
    amount: Any = None,
    account_code: str = "1002",
    summary: str = "",
    creator: str = "系统",
    *,
    amount_cents: int | None = None,
    counterparty_account_code: str | None = None,
    bank_account_id: int | None = None,
    arap_item_id: int | None = None,
    idempotency_key: str | None = None,
    submit: bool = True,
    related_type: str | None = None,
    related_id: int | None = None,
) -> tuple[bool, str, int | None]:
    """Atomically create a cash document and its voucher; never auto-post it."""
    try:
        normalized_type = DOC_TYPE_ALIASES.get(str(doc_type or "").strip())
        if not normalized_type:
            raise ValueError("收付款类型必须是 receipt/收款 或 payment/付款")
        date_str = _validate_date(date_str)
        period = date_str[:7]
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("收付款摘要不能为空")
        cents = _strict_cents(amount_cents, "amount_cents") if amount_cents is not None else _money_to_cents(amount)
        if cents <= 0:
            raise ValueError("收付款金额必须大于零")
        key = _normalise_idempotency_key(idempotency_key)
        counterparty_account_code = counterparty_account_code or (
            "1122" if normalized_type == "receipt" else "2202"
        )
        with db.transaction() as conn:
            if key:
                existing = conn.execute(
                    "SELECT id,doc_no FROM cash_doc WHERE idempotency_key=?", (key,)
                ).fetchone()
                if existing:
                    return True, f"相同业务已生成资金单 {existing['doc_no']}，未重复创建", int(existing["id"])
            if _is_period_closed_conn(conn, period):
                raise ValueError(f"会计期间 {period} 已结账")
            if partner_id is not None:
                if str(partner_type or "").strip() in ("员工", "employee"):
                    party = conn.execute("SELECT id FROM employee WHERE id=?", (partner_id,)).fetchone()
                    if not party:
                        raise ValueError("员工不存在")
                else:
                    party = conn.execute("SELECT id,type FROM partner WHERE id=?", (partner_id,)).fetchone()
                    if not party:
                        raise ValueError("往来方不存在")
            if bank_account_id is not None:
                bank = conn.execute(
                    "SELECT id,account_code FROM bank_account WHERE id=?", (bank_account_id,)
                ).fetchone()
                if not bank:
                    raise ValueError("银行账户不存在")
                if bank["account_code"] and account_code != bank["account_code"]:
                    raise ValueError("资金科目与银行账户绑定科目不一致")
            if arap_item_id is not None:
                item = conn.execute("SELECT * FROM arap_item WHERE id=?", (arap_item_id,)).fetchone()
                if not item:
                    raise ValueError("应收应付项目不存在")
                if partner_id is not None and int(item["partner_id"]) != int(partner_id):
                    raise ValueError("应收应付项目与往来方不一致")
                outstanding = int(item["amount_cents"]) - int(item["settled_cents"])
                if cents > outstanding:
                    raise ValueError("核销金额不能超过应收应付项目未结金额")
            doc_no = _next_document_number(conn, normalized_type, period)
            now = _now()
            cur = conn.execute(
                "INSERT INTO cash_doc(doc_no,doc_type,date,partner_type,partner_id,amount,amount_cents,"
                "account_code,counterparty_account_code,bank_account_id,arap_item_id,summary,status,"
                "idempotency_key,related_type,related_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    doc_no,
                    normalized_type,
                    date_str,
                    partner_type,
                    partner_id,
                    cents / 100,
                    cents,
                    account_code,
                    counterparty_account_code,
                    bank_account_id,
                    arap_item_id,
                    summary,
                    "draft",
                    key,
                    related_type,
                    related_id,
                    now,
                    now,
                ),
            )
            doc_id = int(cur.lastrowid)
            if normalized_type == "receipt":
                lines = [
                    {"account_code": account_code, "summary": summary, "debit_cents": cents, "credit_cents": 0},
                    {
                        "account_code": counterparty_account_code,
                        "summary": summary,
                        "debit_cents": 0,
                        "credit_cents": cents,
                        "aux_type": partner_type,
                        "aux_id": partner_id,
                    },
                ]
            else:
                lines = [
                    {
                        "account_code": counterparty_account_code,
                        "summary": summary,
                        "debit_cents": cents,
                        "credit_cents": 0,
                        "aux_type": partner_type,
                        "aux_id": partner_id,
                    },
                    {"account_code": account_code, "summary": summary, "debit_cents": 0, "credit_cents": cents},
                ]
            voucher_id, vno, _ = _create_voucher_in_transaction(
                conn, date_str, summary, lines, creator, "cash", doc_id, key
            )
            conn.execute("UPDATE cash_doc SET voucher_id=?,updated_at=? WHERE id=?", (voucher_id, _now(), doc_id))
            if arap_item_id is not None:
                conn.execute(
                    "INSERT INTO settlement_allocation(cash_doc_id,arap_item_id,amount_cents,created_at) "
                    "VALUES (?,?,?,?)",
                    (doc_id, arap_item_id, cents, _now()),
                )
                conn.execute(
                    "UPDATE arap_item SET settled_cents=settled_cents+?,status=CASE "
                    "WHEN settled_cents+?>=amount_cents THEN 'settled' ELSE 'partial' END,updated_at=? WHERE id=?",
                    (cents, cents, _now(), arap_item_id),
                )
            db.append_audit_event(
                conn,
                "cash_doc",
                doc_id,
                "cash_doc_created",
                creator,
                {"doc_no": doc_no, "voucher_id": voucher_id, "vno": vno, "amount_cents": cents},
            )
            if submit:
                _transition_in_transaction(conn, voucher_id, "pending_review", creator, None)
        suffix = "待审核" if submit else "草稿"
        return True, f"资金单 {doc_no} 与凭证 {vno} 已原子创建（{suffix}）", doc_id
    except (ValueError, sqlite3.Error) as exc:
        return False, str(exc), None


def get_open_data_issues(severity: str | None = None) -> list[dict[str, Any]]:
    return db.get_open_data_issues(severity)


def resolve_data_issue(issue_id: int, account_code: str, actor: str, reason: str) -> tuple[bool, str]:
    return db.resolve_data_issue(issue_id, account_code, actor, reason)


if __name__ == "__main__":
    db.init_db()
    print(json.dumps(db.run_integrity_checks(), ensure_ascii=False, indent=2))
