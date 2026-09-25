# -*- coding: utf-8 -*-
"""Reliable Feishu approval integration.

Secrets are read from Windows DPAPI storage, while non-sensitive IDs live in
the FinanceSystem settings directory.  Remote instance creation uses Feishu's
``uuid`` idempotency field, and synchronisation never posts accounting entries
directly.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from pathlib import Path
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from db import get_conn, init_db
from modules import expense as expense_mod
from modules.integration import IntegrationResult, IntegrationStatus

try:
    from app_paths import CONFIG_DIR, ensure_app_dirs
except ImportError:
    CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"

    def ensure_app_dirs():
        Path(CONFIG_DIR).mkdir(parents=True, exist_ok=True)

try:
    from security import get_secret
except ImportError:
    def get_secret(_name: str) -> str | None:
        return None


try:
    from logging_setup import get_logger

    LOG = get_logger("modules.feishu")
except ImportError:
    LOG = logging.getLogger(__name__)
API_ROOT = "https://open.feishu.cn/open-apis"
SECRET_NAME = "feishu.app_secret"

DEFAULT_WIDGETS = {
    "expense_type": "widget16510509268920001",
    "expense_type_value": "l2hd3cj7-ic8oib4joe9-0",
    "reason": "widget16510509704570001",
    "detail": "widget16510509950440001",
    "detail_summary": "widget16510510048490001",
    "detail_date": "widget16510510138590001",
    "detail_amount": "widget16510510254730001",
}


def _legacy_config() -> dict:
    path = Path(__file__).resolve().parents[1] / "config" / "feishu.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            value.pop("app_secret", None)
            return value
    except (OSError, ValueError):
        LOG.warning("Legacy Feishu configuration is unreadable")
    return {}


def _cfg() -> dict[str, Any]:
    ensure_app_dirs()
    merged = _legacy_config()
    dedicated = Path(CONFIG_DIR) / "feishu.json"
    if dedicated.exists():
        try:
            value = json.loads(dedicated.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                value.pop("app_secret", None)
                merged.update(value)
        except (OSError, ValueError):
            LOG.warning("Feishu configuration is unreadable")
    path = Path(CONFIG_DIR) / "settings.json"
    if path.exists():
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(settings, dict) and isinstance(settings.get("feishu"), dict):
                merged.update(settings["feishu"])
        except (OSError, ValueError):
            LOG.warning("FinanceSystem settings are unreadable")
    # 生产环境强制走 DPAPI；明文 app_secret 不应出现在配置文件中。
    merged.pop("app_secret", None)
    widgets = dict(DEFAULT_WIDGETS)
    if isinstance(merged.get("widgets"), dict):
        widgets.update(merged["widgets"])
    merged["widgets"] = widgets
    return merged


def effective_config() -> dict[str, Any]:
    """Return the merged, effective (secret-free) Feishu configuration.

    Callers that are about to *rewrite* the configuration must start from this
    value rather than from ``settings.json`` alone: the dedicated
    ``feishu.json`` files carry keys (``allow_self_approval`` in particular)
    that ``settings.json`` never stores, so reading only the latter would
    silently drop them on the next save.
    """

    return _cfg()


def build_settings_section(edited: dict[str, Any], allow_self_approval: bool) -> dict[str, Any]:
    """Compose the ``feishu`` section that ``settings.json`` should hold.

    Only the values the settings page actually edits are written here; every
    other key keeps living in the dedicated ``feishu.json`` (``effective_config()``
    merges both, and ``settings.json`` wins).

    ``allow_self_approval`` must be passed in by the caller -- normally from a
    checkbox initialised with ``effective_config()``.  Never hard-code a default
    for it: the value only ever exists in ``feishu.json``, so writing a
    hard-coded ``False`` here silently breaks a working single-operator setup
    (initiator == approver) the first time someone clicks "save".
    """

    section = {key: str(value or "").strip() for key, value in dict(edited or {}).items()}
    section["app_secret_ref"] = SECRET_NAME
    section["allow_self_approval"] = bool(allow_self_approval)
    return section


def _request_id(headers) -> str | None:
    for name in ("X-Tt-Logid", "X-Request-Id", "X-Request-ID"):
        value = headers.get(name) if headers else None
        if value:
            return value
    return None


def _request(
    method: str,
    path: str,
    payload: dict | None = None,
    token: str | None = None,
    *,
    attempts: int = 3,
) -> IntegrationResult:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer %s" % token
    request = urllib.request.Request(
        API_ROOT + path,
        data=data,
        headers=headers,
        method=method,
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                request_id = _request_id(response.headers)
                try:
                    body = json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    return IntegrationResult(
                        IntegrationStatus.RETRYABLE_ERROR,
                        "飞书返回了无法解析的数据，可稍后重试。",
                        request_id=request_id,
                    )
                if body.get("code", 0) == 0:
                    return IntegrationResult(
                        IntegrationStatus.SUCCESS,
                        "飞书请求成功。",
                        body,
                        request_id=request_id,
                    )
                return IntegrationResult(
                    IntegrationStatus.BUSINESS_REJECTED,
                    "飞书拒绝了本次请求，请检查审批配置。",
                    body,
                    request_id=request_id,
                    details={"code": body.get("code")},
                )
        except urllib.error.HTTPError as exc:
            request_id = _request_id(exc.headers)
            if exc.code == 429 or 500 <= exc.code < 600:
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                return IntegrationResult(
                    IntegrationStatus.RETRYABLE_ERROR,
                    "飞书服务暂时不可用，可稍后安全重试。",
                    request_id=request_id,
                    details={"http_status": exc.code},
                )
            return IntegrationResult(
                IntegrationStatus.BUSINESS_REJECTED,
                "飞书拒绝了本次请求，请检查应用权限和审批配置。",
                request_id=request_id,
                details={"http_status": exc.code},
            )
        except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
            LOG.info("Feishu network attempt failed: %s", type(exc).__name__)
            if attempt + 1 < attempts:
                time.sleep(2 ** attempt)
                continue
            return IntegrationResult(
                IntegrationStatus.RETRYABLE_ERROR,
                "连接飞书失败，可稍后安全重试。",
            )
    return IntegrationResult(IntegrationStatus.RETRYABLE_ERROR, "连接飞书失败。")


def validate_config() -> IntegrationResult:
    cfg = _cfg()
    required = ("app_id", "approval_code", "initiator_open_id", "approver_open_id", "approval_node_id")
    missing = [name for name in required if not str(cfg.get(name) or "").strip()]
    secret_name = cfg.get("app_secret_ref") or SECRET_NAME
    if not get_secret(secret_name):
        missing.append("app_secret(DPAPI)")
    if missing:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "飞书配置不完整：%s" % "、".join(missing),
            details={"missing": missing},
        )
    if (
        cfg.get("initiator_open_id") == cfg.get("approver_open_id")
        and not cfg.get("allow_self_approval", False)
    ):
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "发起人与审批人相同；正式模式下请配置不同审批人。",
        )
    return IntegrationResult(IntegrationStatus.SUCCESS, "飞书配置检查通过。")


def get_token() -> IntegrationResult:
    checked = validate_config()
    if not checked.ok:
        return checked
    cfg = _cfg()
    secret_name = cfg.get("app_secret_ref") or SECRET_NAME
    result = _request(
        "POST",
        "/auth/v3/tenant_access_token/internal",
        {"app_id": cfg["app_id"], "app_secret": get_secret(secret_name)},
        attempts=3,
    )
    if not result.ok:
        return result
    token = (result.data or {}).get("tenant_access_token")
    if not token:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            "飞书未返回访问令牌，请检查应用状态。",
            request_id=result.request_id,
        )
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "飞书认证成功。",
        token,
        request_id=result.request_id,
    )


def _expense_amount_cents(item) -> int:
    keys = set(item.keys()) if hasattr(item, "keys") else set()
    if "amount_cents" in keys and item["amount_cents"] is not None:
        return int(item["amount_cents"])
    return int(round(float(item["amount"] or 0) * 100))


def _iso(value: str) -> str:
    try:
        date = _dt.date.fromisoformat(value)
    except (TypeError, ValueError):
        date = _dt.date.today()
    return date.isoformat() + "T00:00:00+08:00"


def _approval_uuid(expense_id: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "finance-system:expense:%s" % expense_id))


def submit_expense_approval(expense_row: dict) -> IntegrationResult:
    checked = validate_config()
    if not checked.ok:
        return checked
    if expense_row.get("instance_code"):
        return IntegrationResult(
            IntegrationStatus.SUCCESS,
            "该报销单已经提交飞书，不会重复提交。",
            expense_row["instance_code"],
        )
    token_result = get_token()
    if not token_result.ok:
        return token_result
    cfg = _cfg()
    init_db()
    conn = get_conn()
    try:
        items = conn.execute(
            "SELECT * FROM expense_item WHERE expense_id=? ORDER BY id",
            (expense_row["id"],),
        ).fetchall()
    finally:
        conn.close()
    if not items:
        return IntegrationResult(IntegrationStatus.BUSINESS_REJECTED, "报销单没有费用明细。")
    widgets = cfg["widgets"]
    detail = []
    for item in items:
        detail.append(
            [
                {"id": widgets["detail_summary"], "type": "input", "value": item["summary"] or "费用"},
                {"id": widgets["detail_date"], "type": "date", "value": _iso(expense_row.get("date") or "")},
                {
                    "id": widgets["detail_amount"],
                    "type": "amount",
                    "value": "%.2f" % (_expense_amount_cents(item) / 100.0),
                },
            ]
        )
    form = [
        {
            "id": widgets["expense_type"],
            "type": "radioV2",
            "value": widgets["expense_type_value"],
        },
        {
            "id": widgets["reason"],
            "type": "textarea",
            "value": expense_row.get("summary") or "费用报销",
        },
        {"id": widgets["detail"], "type": "fieldList", "value": detail},
    ]
    payload: dict[str, Any] = {
        "approval_code": cfg["approval_code"],
        "open_id": cfg["initiator_open_id"],
        "form": json.dumps(form, ensure_ascii=False),
        "uuid": _approval_uuid(int(expense_row["id"])),
    }
    if cfg.get("department_id") not in (None, "", "0"):
        payload["department_id"] = cfg["department_id"]
    # 模板为「发起人自选审批人」模式，不传 node_approver_open_id_list，
    # 由飞书在提交时弹出审批人选择界面。
    result = _request(
        "POST",
        "/approval/v4/instances?user_id_type=open_id",
        payload,
        token_result.data,
    )
    if not result.ok:
        return result
    instance_code = ((result.data or {}).get("data") or {}).get("instance_code")
    if not instance_code:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "飞书已接受请求但未返回审批实例编号，请使用相同报销单重试核对。",
            request_id=result.request_id,
        )
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT instance_code FROM expense WHERE id=?", (expense_row["id"],)
        ).fetchone()
        if current and current[0] and current[0] != instance_code:
            conn.rollback()
            return IntegrationResult(
                IntegrationStatus.BUSINESS_REJECTED,
                "本地报销单已关联另一审批实例，请人工核对。",
                current[0],
                request_id=result.request_id,
            )
        conn.execute(
            "UPDATE expense SET instance_code=? WHERE id=?",
            (instance_code, expense_row["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "已提交飞书审批。",
        instance_code,
        request_id=result.request_id,
    )


def _get_instance_status(instance_code: str, token: str) -> IntegrationResult:
    result = _request(
        "GET",
        "/approval/v4/instances/%s" % urllib.parse.quote(instance_code, safe=""),
        token=token,
    )
    if not result.ok:
        return result
    status = ((result.data or {}).get("data") or {}).get("status")
    if not status:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "飞书未返回审批状态。",
            request_id=result.request_id,
        )
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "审批状态已同步。",
        status,
        request_id=result.request_id,
    )


def sync_approval_status() -> IntegrationResult:
    token_result = get_token()
    if not token_result.ok:
        return token_result
    init_db()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM expense WHERE status IN ('pending_approval','pending_review') "
            "AND instance_code IS NOT NULL AND instance_code<>'' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    changed = []
    errors = []
    for row in rows:
        result = _get_instance_status(row["instance_code"], token_result.data)
        if not result.ok:
            errors.append({"expense_id": row["id"], "status": result.status.value})
            continue
        if result.data == "APPROVED":
            ok, message, *_ = expense_mod.approve(row["id"], "飞书审批")
            if ok:
                changed.append({"expense_id": row["id"], "status": "approved"})
        elif result.data == "REJECTED":
            reject = getattr(expense_mod, "reject_expense", None)
            if reject:
                ok, message = reject(row["id"], "飞书审批", "飞书审批驳回")
                if ok:
                    changed.append({"expense_id": row["id"], "status": "rejected"})
            else:
                errors.append({"expense_id": row["id"], "status": "local_api_missing"})
    if errors:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "部分审批状态同步失败，可安全重试。",
            {"changed": changed, "errors": errors},
        )
    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "审批状态同步完成。",
        changed,
    )


def self_check(test_network: bool = False) -> IntegrationResult:
    checked = validate_config()
    if not checked.ok or not test_network:
        return checked
    token = get_token()
    if not token.ok:
        return token
    return IntegrationResult(IntegrationStatus.SUCCESS, "飞书配置和连接检查通过。")


if __name__ == "__main__":
    print(json.dumps(self_check(False).as_dict(), ensure_ascii=False))
