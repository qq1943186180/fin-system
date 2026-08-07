# -*- coding: utf-8 -*-
"""邮件服务模块单元测试。"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules"))
sys.path.insert(0, str(ROOT / "db"))
sys.path.insert(0, str(ROOT / "gui"))


@pytest.fixture(autouse=True)
def _fresh_modules():
    """确保每次测试拿到最新模块。"""
    for mod in ("modules.mail_service",):
        sys.modules.pop(mod, None)
    yield


def test_sender_whitelist_no_whitelist_accepts_all():
    from modules.mail_service import _match_sender

    assert _match_sender("anyone@example.com", []) is True


def test_sender_whitelist_match():
    from modules.mail_service import _match_sender

    assert _match_sender("alice@trusted.com", ["trusted.com"]) is True
    assert _match_sender("bob@other.com", ["trusted.com"]) is False


def test_subject_regex_match():
    from modules.mail_service import _match_subject_regex

    assert _match_subject_regex("发票 #123", r"#\d+") is True
    assert _match_subject_regex("hello", r"#\d+") is False
    assert _match_subject_regex("hello", "") is True


def test_subject_regex_invalid_pattern():
    from modules.mail_service import _match_subject_regex

    # 语法错误的正则应 fallback 到 True（不过滤）
    assert _match_subject_regex("anything", r"[invalid(") is True


def test_subject_keyword_match():
    from modules.mail_service import _match_subject

    assert _match_subject("电子发票", "发票|电子发票|invoice") is True
    assert _match_subject("hello", "发票|电子发票|invoice") is False


def test_max_size_mb_filter():
    from modules.mail_service import MailBoxConfig

    cfg = MailBoxConfig("test", "imap.test", max_size_mb=1)
    assert cfg.max_size_mb == 1


def test_reject_action_validation():
    from modules.mail_service import MailBoxConfig

    cfg = MailBoxConfig("test", "imap.test", reject_action="invalid")
    assert cfg.reject_action == "skip"


def test_config_round_trip():
    from modules.mail_service import MailBoxConfig

    original = MailBoxConfig(
        name="test",
        imap_host="imap.test",
        imap_port=993,
        username="user@test.com",
        password="secret",
        enabled=True,
        last_uid=42,
        folder="INBOX",
        subject_filter="发票|invoice",
        allowed_ext=[".pdf", ".png"],
        smtp_host="smtp.test",
        smtp_port=465,
        smtp_use_ssl=True,
        sender_whitelist=["trusted.com", "admin@company.com"],
        subject_regex=r"#\d+",
        max_size_mb=10,
        reject_action="move",
    )
    d = original.to_dict()
    restored = MailBoxConfig.from_dict(d)
    assert restored.name == original.name
    assert restored.sender_whitelist == original.sender_whitelist
    assert restored.subject_regex == original.subject_regex
    assert restored.max_size_mb == original.max_size_mb
    assert restored.reject_action == original.reject_action


def test_fetch_attachments_filters_by_sender():
    from modules.mail_service import MailBoxConfig, fetch_attachments

    cfg = MailBoxConfig(
        "test", "imap.test", username="u", password="p",
        sender_whitelist=["trusted.com"],
    )

    fake_msg = MagicMock()
    fake_msg.from_ = "unknown@other.com"
    fake_msg.subject = "发票 #1"
    fake_msg.uid = 10
    fake_msg.attachments = []

    fake_mailbox = MagicMock()
    fake_mailbox.__enter__ = MagicMock(return_value=fake_mailbox)
    fake_mailbox.__exit__ = MagicMock(return_value=False)
    fake_mailbox.login = MagicMock()
    fake_mailbox.folder.set = MagicMock()
    fake_mailbox.fetch = MagicMock(return_value=[fake_msg])

    with patch("modules.mail_service.MailBox", return_value=fake_mailbox):
        result = fetch_attachments(cfg)

    assert result.ok
    assert result.data == []
