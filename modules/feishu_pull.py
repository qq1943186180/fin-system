# -*- coding: utf-8 -*-
"""从飞书审批拉取员工自助提交的报销单。

## 为什么需要这个模块

原先的报销流程是「财务在桌面端建单 → 推到飞书 → 人工点同步」，本质是让
财务替所有员工录入，员工根本用不了。改成**员工在手机飞书自助提单、财务系统
反向拉取**之后，这个模块负责「拉」的那一半。

## 数据流

    飞书审批实例列表 (approval_code + 毫秒时间窗)
      → 逐条取实例详情 (form 里是员工填的内容)
      → 解析出 报销类型/事由/明细(内容·日期·金额)/发起人
      → 按 user_id 匹配或新建本地员工
      → 按报销类型映射到费用科目
      → 建本地 expense + expense_item（幂等，重复拉取不会重复建单）

## 幂等

飞书实例的 ``instance_code`` 是天然唯一键，存进 ``expense.instance_code``。
拉取时先查这一列，已存在就跳过。所以**可以随便重复点，不会重复建单**。

## 已知限制

- 只在本地记账，**不会**把结果回写飞书。
- 拉回来的单初始状态是 ``pending_approval``；飞书里已通过的，拉取时直接按
  飞书状态落 ``approved``（省掉再点一次「同步飞书状态」）。
- 报销类型 → 科目映射来自配置 ``expense_type_accounts``，没配的走默认科目。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import urllib.parse

from db import get_conn, init_db
from modules.integration import IntegrationResult, IntegrationStatus

try:
    from logging_setup import get_logger

    LOG = get_logger("modules.feishu_pull")
except ImportError:
    LOG = logging.getLogger(__name__)


# 飞书审批实例状态 → 本地 expense 状态
FEISHU_TO_LOCAL_STATUS = {
    "PENDING": "pending_approval",
    "APPROVED": "approved",
    "REJECTED": "rejected",
    "CANCELED": "rejected",
    "DELETED": "rejected",
}

# 只拉这些状态（其余状态在飞书里没意义，拉了也没用）
SYNCABLE_STATUSES = tuple(FEISHU_TO_LOCAL_STATUS)

# 报销类型 → 费用科目 的兜底映射。
# 飞书模板里「报销类型」是单选，选项文本见 references/feishu_approval.md。
DEFAULT_TYPE_ACCOUNTS = {
    "差旅费": "5602",
    "住宿费": "5602",
    "交通费": "5602",
    "招待费": "5602",
    "团建费": "5602",
    "通讯费": "5602",
    "其他": "5602",
}

# 飞书日期控件返回的是 ISO8601（带 +08:00 时区），取前 10 位即可
_ISO_DATE_LEN = 10


def _cfg_extra() -> dict:
    """读取本模块专用的配置段（复用 feishu.py 的合并逻辑）。"""

    from modules import feishu

    return feishu._cfg()


def type_account_map() -> dict[str, str]:
    """报销类型 → 科目编码。配置项 ``expense_type_accounts`` 优先。"""

    mapping = dict(DEFAULT_TYPE_ACCOUNTS)
    configured = _cfg_extra().get("expense_type_accounts")
    if isinstance(configured, dict):
        for key, value in configured.items():
            code = str(value or "").strip()
            if code:
                mapping[str(key)] = code
    return mapping


def default_account_code() -> str:
    cfg = _cfg_extra()
    code = str(cfg.get("default_expense_account") or "").strip()
    return code or "5602"


# --------------------------------------------------------------------------
# 飞书侧读取
# --------------------------------------------------------------------------


def _list_instance_codes(token: str, start_ms: int, end_ms: int) -> IntegrationResult:
    """列出时间窗内的审批实例编号。

    ⚠️ ``start_time`` / ``end_time`` **必须是毫秒**时间戳，且**两个都要传**。
    传秒会静默返回 ``code=0, instance_code_list=[]`` —— 不报错、不给空页提示，
    看起来就像"这段时间没有单"。缺任一个则报 ``99992402 field validation failed``。
    """

    from modules import feishu

    query = urllib.parse.urlencode(
        {
            "approval_code": _cfg_extra()["approval_code"],
            "start_time": int(start_ms),
            "end_time": int(end_ms),
            "page_size": 100,
        }
    )
    result = feishu._request("GET", "/approval/v4/instances?%s" % query, token=token)
    if not result.ok:
        return result
    codes = ((result.data or {}).get("data") or {}).get("instance_code_list") or []
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "已获取 %d 个审批实例。" % len(codes),
        list(codes),
        request_id=result.request_id,
    )


def _get_instance_detail(instance_code: str, token: str) -> IntegrationResult:
    from modules import feishu

    result = feishu._request(
        "GET",
        "/approval/v4/instances/%s?user_id_type=user_id"
        % urllib.parse.quote(instance_code, safe=""),
        token=token,
    )
    if not result.ok:
        return result
    detail = (result.data or {}).get("data")
    if not isinstance(detail, dict):
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "飞书未返回审批实例详情。",
            request_id=result.request_id,
        )
    return IntegrationResult(
        IntegrationStatus.SUCCESS, "已获取审批实例详情。", detail, request_id=result.request_id
    )


def _get_user_name(user_id: str, token: str, cache: dict) -> str:
    """把飞书 user_id 换成姓名；失败就退回 user_id 本身（不阻断拉取）。"""

    if not user_id:
        return ""
    if user_id in cache:
        return cache[user_id]

    from modules import feishu

    result = feishu._request(
        "GET",
        "/contact/v3/users/%s?user_id_type=user_id" % urllib.parse.quote(user_id, safe=""),
        token=token,
    )
    name = ""
    if result.ok:
        user = ((result.data or {}).get("data") or {}).get("user") or {}
        name = str(user.get("name") or "").strip()
    cache[user_id] = name or user_id
    return cache[user_id]


# --------------------------------------------------------------------------
# 表单解析
# --------------------------------------------------------------------------


def _parse_form(form) -> dict:
    """把实例详情里的 form 解析成结构化字段。

    ``form`` 可能是 JSON 字符串，也可能是已解析的列表（两种都遇到过）。
    fieldList 控件的 ``value`` 是「行 × 列」的二维数组。
    """

    if isinstance(form, str):
        try:
            form = json.loads(form)
        except (TypeError, ValueError):
            return {}
    if not isinstance(form, list):
        return {}

    widgets = _cfg_extra().get("widgets") or {}
    w_type = widgets.get("expense_type")
    w_reason = widgets.get("reason")
    w_detail = widgets.get("detail")
    w_summary = widgets.get("detail_summary")
    w_date = widgets.get("detail_date")
    w_amount = widgets.get("detail_amount")

    parsed: dict = {"type_text": "", "reason": "", "items": []}

    for widget in form:
        if not isinstance(widget, dict):
            continue
        wid = widget.get("id")
        if wid == w_type:
            # radioV2 的 value 是选项文本，option.text 也可以
            parsed["type_text"] = str(
                widget.get("value")
                or ((widget.get("option") or {}).get("text"))
                or ""
            ).strip()
        elif wid == w_reason:
            parsed["reason"] = str(widget.get("value") or "").strip()
        elif wid == w_detail:
            parsed["items"] = _parse_detail_rows(widget.get("value"), w_summary, w_date, w_amount)

    return parsed


def _parse_detail_rows(rows, w_summary, w_date, w_amount) -> list[dict]:
    """解析 fieldList 的行。每行是若干子控件，按 id 取内容/日期/金额。"""

    if not isinstance(rows, list):
        return []
    items: list[dict] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        cell: dict = {"summary": "", "date": "", "amount_cents": 0}
        for field in row:
            if not isinstance(field, dict):
                continue
            fid = field.get("id")
            value = field.get("value")
            if fid == w_summary:
                cell["summary"] = str(value or "").strip()
            elif fid == w_date:
                cell["date"] = str(value or "")[:_ISO_DATE_LEN]
            elif fid == w_amount:
                cell["amount_cents"] = _to_cents(value)
        if cell["amount_cents"] > 0:
            items.append(cell)
    return items


def _to_cents(value) -> int:
    """金额控件可能给字符串、数字、或带千分位的文本；无法解析返回 0。

    实现已收敛到 :mod:`modules.money`（2026-10-14）。原先这里用
    ``round(float(...) * 100)`` —— 那是**银行家舍入**，与记账链路的
    ROUND_HALF_UP 在半分位上差 1 分（``0.125`` 会得 12 而非 13）。
    """
    from modules import money

    return money.parse_cents_or_zero(value)


# --------------------------------------------------------------------------
# 本地建单
# --------------------------------------------------------------------------


def _resolve_employee(conn, name: str, feishu_user_id: str) -> int:
    """按姓名找员工；没有就建一个。返回 employee.id。"""

    from modules import expense as expense_mod

    clean = str(name or "").strip()
    if not clean:
        clean = feishu_user_id or "未知员工"
    row = conn.execute("SELECT id FROM employee WHERE name=?", (clean,)).fetchone()
    if row:
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO employee(name,dept,phone,note) VALUES (?,?,?,?)",
        (clean, "", "", "由飞书审批自动创建（user_id=%s）" % feishu_user_id),
    )
    employee_id = int(cur.lastrowid)
    # 复用 expense 模块的审计风格
    try:
        import db as _db

        _db.append_audit_event(
            conn, "employee", employee_id, "employee_created", "飞书同步", {"name": clean}
        )
    except Exception:  # 审计失败不阻断拉取
        LOG.warning("Failed to append employee audit event for %s", clean)
    return employee_id


def _create_local_expense(conn, instance_code, parsed, employee_id, feishu_status) -> int:
    """在给定连接上建 expense + expense_item。调用方负责事务。"""

    from modules import expense as expense_mod
    from modules import ledger

    type_map = type_account_map()
    fallback = default_account_code()
    account_code = type_map.get(parsed.get("type_text") or "", fallback)

    items = parsed.get("items") or []
    total_cents = sum(int(i["amount_cents"]) for i in items)

    # 明细的日期优先，取最早一条作为单据日期
    dates = [i["date"] for i in items if i.get("date")]
    date_str = min(dates) if dates else _dt.date.today().isoformat()

    number = expense_mod._next_expense_number(conn, date_str[:7])
    now = ledger._now()
    local_status = FEISHU_TO_LOCAL_STATUS.get(feishu_status, "pending_approval")

    summary = parsed.get("reason") or (parsed.get("type_text") or "费用报销")
    cur = conn.execute(
        "INSERT INTO expense(no,employee_id,date,amount,amount_cents,summary,status,"
        "instance_code,submitted_by,submitted_at,approved_at,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            number,
            employee_id,
            date_str,
            total_cents / 100,
            total_cents,
            summary,
            local_status,
            instance_code,
            "飞书审批",
            now,
            now if local_status == "approved" else None,
            now,
            now,
        ),
    )
    expense_id = int(cur.lastrowid)

    for item in items:
        item_summary = item.get("summary") or (parsed.get("type_text") or "费用")
        conn.execute(
            "INSERT INTO expense_item(expense_id,account_code,summary,amount,amount_cents,invoice_ref) "
            "VALUES (?,?,?,?,?,?)",
            (
                expense_id,
                account_code,
                item_summary,
                item["amount_cents"] / 100,
                item["amount_cents"],
                "",
            ),
        )

    try:
        import db as _db

        _db.append_audit_event(
            conn,
            "expense",
            expense_id,
            "expense_pulled_from_feishu",
            "飞书同步",
            {
                "number": number,
                "instance_code": instance_code,
                "amount_cents": total_cents,
                "feishu_status": feishu_status,
            },
        )
    except Exception:
        LOG.warning("Failed to append expense audit event for %s", instance_code)

    return expense_id


# --------------------------------------------------------------------------
# 对外入口
# --------------------------------------------------------------------------


def fetch_feishu_expenses(days: int = 30, dry_run: bool = False) -> IntegrationResult:
    """拉取最近 ``days`` 天飞书审批实例，把没入过库的建成本地报销单。

    幂等：``expense.instance_code`` 是唯一键，已存在的实例直接跳过。
    ``dry_run=True`` 只解析不写库，用于预览。
    """

    from modules import feishu

    if days <= 0:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, "拉取天数必须大于 0。")

    token_result = feishu.get_token()
    if not token_result.ok:
        return token_result
    token = token_result.data

    now = _dt.datetime.now()
    end_ms = int(now.timestamp() * 1000)
    start_ms = int((now - _dt.timedelta(days=days)).timestamp() * 1000)

    listed = _list_instance_codes(token, start_ms, end_ms)
    if not listed.ok:
        return listed
    codes = listed.data or []

    init_db()
    conn = get_conn()
    try:
        known = {
            row[0]
            for row in conn.execute(
                "SELECT instance_code FROM expense WHERE instance_code IS NOT NULL AND instance_code<>''"
            )
        }
    finally:
        conn.close()

    created: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    user_cache: dict = {}

    for code in codes:
        if code in known:
            skipped.append({"instance_code": code, "reason": "already_imported"})
            continue

        detail_result = _get_instance_detail(code, token)
        if not detail_result.ok:
            errors.append({"instance_code": code, "error": detail_result.status.value})
            continue

        detail = detail_result.data
        feishu_status = str(detail.get("status") or "").upper()
        if feishu_status not in SYNCABLE_STATUSES:
            skipped.append({"instance_code": code, "reason": "status_%s" % feishu_status.lower()})
            continue

        parsed = _parse_form(detail.get("form"))
        if not parsed.get("items"):
            # 没明细没法记账，但要让用户看见，不是静默丢掉
            skipped.append({"instance_code": code, "reason": "no_detail_rows"})
            continue

        user_id = str(detail.get("user_id") or "")
        employee_name = _get_user_name(user_id, token, user_cache)

        if dry_run:
            created.append(
                {
                    "instance_code": code,
                    "employee": employee_name,
                    "type": parsed.get("type_text"),
                    "reason": parsed.get("reason"),
                    "amount_cents": sum(int(i["amount_cents"]) for i in parsed["items"]),
                    "feishu_status": feishu_status,
                    "dry_run": True,
                }
            )
            continue

        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            # TOCTOU：拿锁后再确认一次，防并发重复建单
            again = conn.execute(
                "SELECT id FROM expense WHERE instance_code=?", (code,)
            ).fetchone()
            if again:
                conn.rollback()
                skipped.append({"instance_code": code, "reason": "already_imported"})
                continue
            employee_id = _resolve_employee(conn, employee_name, user_id)
            expense_id = _create_local_expense(
                conn, code, parsed, employee_id, feishu_status
            )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            LOG.warning("Failed to import Feishu instance %s: %s", code, exc)
            errors.append({"instance_code": code, "error": type(exc).__name__})
            continue
        finally:
            conn.close()

        created.append(
            {
                "instance_code": code,
                "expense_id": expense_id,
                "employee": employee_name,
                "type": parsed.get("type_text"),
                "reason": parsed.get("reason"),
                "amount_cents": sum(int(i["amount_cents"]) for i in parsed["items"]),
                "feishu_status": feishu_status,
            }
        )

    message = "拉取完成：新建 %d 单，跳过 %d 单，失败 %d 单。" % (
        len(created),
        len(skipped),
        len(errors),
    )
    payload = {"created": created, "skipped": skipped, "errors": errors}
    if errors:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR, message, payload, details={"errors": errors}
        )
    return IntegrationResult(IntegrationStatus.SUCCESS, message, payload)
