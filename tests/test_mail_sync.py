# -*- coding: utf-8 -*-
"""邮件同步模块单元测试。"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules"))
sys.path.insert(0, str(ROOT / "db"))


class FakeProvider:
    def __init__(self, boxes):
        self._boxes = {b.name: b for b in boxes}

    def __call__(self):
        return list(self._boxes.values())

    def update_box(self, cfg):
        self._boxes[cfg.name] = cfg


def test_run_once_processes_enabled_boxes():
    from modules.mail_sync import MailSyncEngine
    from modules.mail_service import MailBoxConfig, IntegrationResult, IntegrationStatus

    cfg = MailBoxConfig("box1", "imap.test", enabled=True, last_uid=0)
    provider = FakeProvider([cfg])

    fake_result = IntegrationResult(IntegrationStatus.SUCCESS, "ok", [
        {"filename": "inv.pdf", "bytes": b"data", "message_id": "<1>", "uid": 5,
         "content_type": "application/pdf", "subject": "发票"},
    ])

    engine = MailSyncEngine(provider, poll_interval_minutes=30)
    with patch("modules.mail_sync.fetch_attachments", return_value=fake_result):
        summary = engine.run_once()

    assert summary["boxes"][0]["ok"] is True
    assert summary["boxes"][0]["attachments"][0]["filename"] == "inv.pdf"
    assert cfg.last_uid == 5


def test_run_once_skips_disabled_boxes():
    from modules.mail_sync import MailSyncEngine
    from modules.mail_service import MailBoxConfig

    cfg = MailBoxConfig("box1", "imap.test", enabled=False)
    provider = FakeProvider([cfg])

    engine = MailSyncEngine(provider)
    with patch("modules.mail_sync.fetch_attachments") as mock_fetch:
        summary = engine.run_once()

    mock_fetch.assert_not_called()
    assert summary["boxes"] == []


def test_run_once_persists_last_uid():
    from modules.mail_sync import MailSyncEngine
    from modules.mail_service import MailBoxConfig, IntegrationResult, IntegrationStatus

    cfg = MailBoxConfig("box1", "imap.test", enabled=True, last_uid=0)
    provider = FakeProvider([cfg])

    fake_result = IntegrationResult(IntegrationStatus.SUCCESS, "ok", [
        {"filename": "a.pdf", "bytes": b"x", "message_id": "<1>", "uid": 10,
         "content_type": "application/pdf", "subject": "发票"},
        {"filename": "b.pdf", "bytes": b"y", "message_id": "<2>", "uid": 20,
         "content_type": "application/pdf", "subject": "发票"},
    ])

    engine = MailSyncEngine(provider)
    with patch("modules.mail_sync.fetch_attachments", return_value=fake_result):
        engine.run_once()

    assert cfg.last_uid == 20
    assert provider._boxes["box1"].last_uid == 20


def test_deduplication_by_message_id_and_md5():
    from modules.mail_sync import MailSyncEngine
    from modules.mail_service import MailBoxConfig, IntegrationResult, IntegrationStatus

    cfg = MailBoxConfig("box1", "imap.test", enabled=True, last_uid=0)
    provider = FakeProvider([cfg])

    data = b"same-content"
    md5 = hashlib.md5(data).hexdigest()  # noqa: S324
    fake_result = IntegrationResult(IntegrationStatus.SUCCESS, "ok", [
        {"filename": "a.pdf", "bytes": data, "message_id": "<1>", "uid": 1,
         "content_type": "application/pdf", "subject": "发票"},
        {"filename": "a.pdf", "bytes": data, "message_id": "<1>", "uid": 1,
         "content_type": "application/pdf", "subject": "发票"},
        {"filename": "b.pdf", "bytes": data, "message_id": "<2>", "uid": 2,
         "content_type": "application/pdf", "subject": "发票"},
    ])

    engine = MailSyncEngine(provider)
    with patch("modules.mail_sync.fetch_attachments", return_value=fake_result):
        summary = engine.run_once()

    # 第一个重复被去重，第二个 message_id 不同所以保留
    assert len(summary["boxes"][0]["attachments"]) == 2
