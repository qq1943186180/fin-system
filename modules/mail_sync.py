# -*- coding: utf-8 -*-
"""邮件同步：轮询 + 去重（Message-ID + 附件 MD5）"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from modules.integration import IntegrationResult, IntegrationStatus
from modules.mail_service import MailBoxConfig, fetch_attachments
from modules.mail_processor import process_attachment

from logging_setup import get_logger

LOG = get_logger("modules.mail_sync")

# 最小轮询间隔（秒）
MIN_POLL_INTERVAL = 60


class MailSyncEngine:
    def __init__(self, config_provider, poll_interval_minutes: int = 30):
        self._config_provider = config_provider
        self._poll_interval = max(MIN_POLL_INTERVAL, poll_interval_minutes * 60)
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self._running = False

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    break
            try:
                self._run_once()
            except Exception:
                LOG.exception("轮询异常")
            time.sleep(self._poll_interval)

    def _run_once(self) -> None:
        configs = self._config_provider()
        if not configs:
            return
        for cfg in configs:
            if not isinstance(cfg, MailBoxConfig) or not cfg.enabled:
                continue
            result = fetch_attachments(cfg)
            if not result.ok:
                LOG.warning("[%s] 收件失败: %s", cfg.name, result.message)
                continue
            attachments = result.data or []
            if not attachments:
                # 无附件，不推进 last_uid（避免跳过待处理邮件）
                continue
            LOG.info("[%s] 获取 %d 个附件，开始处理", cfg.name, len(attachments))
            all_ok = True
            for att in attachments:
                try:
                    proc_result = process_attachment(
                        att.get("filename", ""),
                        att.get("bytes", b""),
                        att.get("subject", ""),
                    )
                    if not proc_result.ok:
                        LOG.warning("[%s] 附件处理失败: %s — %s",
                                    cfg.name, att.get("filename", ""), proc_result.message)
                        all_ok = False
                except Exception:
                    LOG.exception("[%s] 附件处理异常: %s", cfg.name, att.get("filename", ""))
                    all_ok = False
            # 所有附件处理成功后才推进 last_uid，避免丢失未处理邮件
            if all_ok:
                cfg.last_uid = max((a.get("uid", 0) or 0) for a in attachments)
                self._persist_last_uid(cfg)

    def _persist_last_uid(self, cfg: MailBoxConfig) -> None:
        try:
            self._config_provider.update_box(cfg)
        except Exception:
            LOG.exception("保存 last_uid 失败: %s", cfg.name)

    def run_once(self) -> dict[str, Any]:
        """手动触发一次，返回汇总。"""
        summary: dict[str, Any] = {"boxes": []}
        configs = self._config_provider()
        for cfg in configs:
            if not isinstance(cfg, MailBoxConfig) or not cfg.enabled:
                continue
            result = fetch_attachments(cfg, mark_seen=False)
            box_info = {
                "name": cfg.name,
                "ok": result.ok,
                "status": result.status.name,
                "message": result.message,
                "attachments": [],
            }
            if result.ok and result.data:
                seen = set()
                for att in result.data:
                    h = hashlib.md5(att.get("bytes", b"")).hexdigest()  # noqa: S324
                    key = (att.get("message_id"), h)
                    if key in seen:
                        continue
                    seen.add(key)
                    box_info["attachments"].append(
                        {
                            "filename": att.get("filename"),
                            "content_type": att.get("content_type"),
                            "subject": att.get("subject"),
                            "size": len(att.get("bytes", b"")),
                        }
                    )
                cfg.last_uid = max((a.get("uid", 0) or 0) for a in result.data)
                self._persist_last_uid(cfg)
            summary["boxes"].append(box_info)
        return summary
