# -*- coding: utf-8 -*-
"""邮件服务：IMAP 收件 + SMTP 发信 + 附件提取。"""
from __future__ import annotations

import imaplib
import logging
import os
import re
import smtplib
import ssl
import tempfile
from email import policy, encoders
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Sequence

from imap_tools import AND, MailBox

from modules.integration import IntegrationResult, IntegrationStatus

from security import get_secret, set_secret

from logging_setup import get_logger

LOG = get_logger("modules.mail")

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB


def _encrypted_password(name: str, plain: str) -> None:
    """将明文密码迁移到 DPAPI 存储。"""
    try:
        set_secret(name, plain)
    except Exception:
        LOG.warning("密码 DPAPI 存储失败，将使用明文 fallback")


def _get_secret_password(name: str, fallback: str | None = None) -> str | None:
    """优先读 DPAPI，失败则 fallback。"""
    secret = get_secret(name)
    if secret:
        return secret
    return fallback


class MailBoxConfig:
    def __init__(
        self,
        name: str,
        imap_host: str,
        imap_port: int = 993,
        username: str = "",
        password: str = "",
        use_ssl: bool = True,
        enabled: bool = True,
        last_uid: int = 0,
        folder: str = "INBOX",
        subject_filter: str = "发票|电子发票|invoice",
        allowed_ext: Sequence[str] = (".pdf", ".jpg", ".jpeg", ".png"),
        smtp_host: str = "",
        smtp_port: int = 465,
        smtp_use_ssl: bool = True,
        sender_whitelist: Sequence[str] | None = None,
        subject_regex: str = "",
        max_size_mb: int = 20,
        reject_action: str = "skip",
    ):
        self.name = name
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.enabled = enabled
        self.last_uid = last_uid
        self.folder = folder
        self.subject_filter = subject_filter
        self.allowed_ext = tuple(allowed_ext)
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_use_ssl = smtp_use_ssl
        self.sender_whitelist = tuple(sender_whitelist or [])
        self.subject_regex = subject_regex
        self.max_size_mb = max_size_mb
        self.reject_action = reject_action if reject_action in ("skip", "move") else "skip"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MailBoxConfig":
        password = str(d.get("password", ""))
        safe_user = str(d.get("username", "")).replace("@", "_at_").replace(".", "_")
        secret_name = f"mail_box.password.{safe_user}"
        if password:
            try:
                from security import set_secret
                set_secret(secret_name, password)
            except Exception:
                pass
        return cls(
            name=str(d.get("name", "")),
            imap_host=str(d.get("imap_host", "")),
            imap_port=int(d.get("imap_port", 993)),
            username=str(d.get("username", "")),
            password="",
            use_ssl=bool(d.get("use_ssl", True)),
            enabled=bool(d.get("enabled", True)),
            last_uid=int(d.get("last_uid", 0)),
            folder=str(d.get("folder", "INBOX")),
            subject_filter=str(d.get("subject_filter", "发票|电子发票|invoice")),
            allowed_ext=list(d.get("allowed_ext", [".pdf", ".jpg", ".jpeg", ".png"])),
            smtp_host=str(d.get("smtp_host", "")),
            smtp_port=int(d.get("smtp_port", 465)),
            smtp_use_ssl=bool(d.get("smtp_use_ssl", True)),
            sender_whitelist=d.get("sender_whitelist"),
            subject_regex=str(d.get("subject_regex", "")),
            max_size_mb=int(d.get("max_size_mb", 20)),
            reject_action=str(d.get("reject_action", "skip")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "imap_host": self.imap_host,
            "imap_port": self.imap_port,
            "username": self.username,
            # password 不写入磁盘，仅通过 DPAPI secret 读取
            "use_ssl": self.use_ssl,
            "enabled": self.enabled,
            "last_uid": self.last_uid,
            "folder": self.folder,
            "subject_filter": self.subject_filter,
            "allowed_ext": list(self.allowed_ext),
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "smtp_use_ssl": self.smtp_use_ssl,
            "sender_whitelist": list(self.sender_whitelist),
            "subject_regex": self.subject_regex,
            "max_size_mb": self.max_size_mb,
            "reject_action": self.reject_action,
        }


def _match_subject(subject: str, pattern: str) -> bool:
    """简易主题匹配：支持中文 | 分隔的多个关键词。"""
    keywords = [k.strip() for k in pattern.split("|") if k.strip()]
    if not keywords:
        return True
    subject_lower = (subject or "").lower()
    return any(k.lower() in subject_lower for k in keywords)


def _match_subject_regex(subject: str, pattern: str) -> bool:
    """正则主题匹配。"""
    if not pattern:
        return True
    try:
        return bool(re.search(pattern, subject or ""))
    except re.error:
        LOG.warning("subject_regex 语法错误: %s", pattern)
        return True


def _match_sender(msg_from: str, whitelist: Sequence[str]) -> bool:
    """发件人白名单匹配。"""
    if not whitelist:
        return True
    _, addr = parseaddr(msg_from or "")
    addr = addr.lower()
    return any(w.lower() in addr for w in whitelist)


def fetch_attachments(config: MailBoxConfig, *, mark_seen: bool = False) -> IntegrationResult:
    """IMAP 收件并提取附件。

    返回 IntegrationResult，data 为 list[dict]：
    [{"filename": ..., "bytes": ..., "content_type": ..., "subject": ...}]
    """
    if not config.enabled:
        return IntegrationResult(IntegrationStatus.SUCCESS, "邮箱已禁用。")

    safe_user = config.username.replace("@", "_at_").replace(".", "_")
    secret_name = f"mail_box.password.{safe_user}"
    password = _get_secret_password(secret_name, config.password)
    if not password:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            f"邮箱 {config.name} 密码为空，请在设置中配置。",
        )

    result_list: list[dict[str, Any]] = []
    try:
        with MailBox(config.imap_host, config.imap_port, ssl=config.use_ssl) as mailbox:
            mailbox.login(config.username, password)
            mailbox.folder.set(config.folder)
            criteria = AND(uid=[config.last_uid + 1, "ALL"]) if config.last_uid else AND(all=True)
            for msg in mailbox.fetch(criteria, mark_seen=mark_seen):
                # 发件人白名单
                if not _match_sender(msg.from_, config.sender_whitelist):
                    LOG.debug("[%s] 跳过非白名单发件人: %s", config.name, msg.from_)
                    continue
                # 主题关键词过滤
                if not _match_subject(msg.subject or "", config.subject_filter):
                    continue
                # 正则主题过滤
                if not _match_subject_regex(msg.subject or "", config.subject_regex):
                    LOG.debug("[%s] 跳过正则不匹配: %s", config.name, msg.subject)
                    continue
                for att in msg.attachments:
                    fname = att.filename or "attachment"
                    ext = Path(fname).suffix.lower()
                    if ext not in config.allowed_ext:
                        continue
                    size_mb = (att.size or 0) / (1024 * 1024)
                    if size_mb > config.max_size_mb:
                        LOG.warning("附件过大，跳过: %s (%.1f MB > %d MB)", fname, size_mb, config.max_size_mb)
                        continue
                    try:
                        data = att.payload
                        if isinstance(data, str):
                            data = data.encode("utf-8")
                        result_list.append(
                            {
                                "filename": fname,
                                "bytes": data,
                                "content_type": att.content_type,
                                "subject": msg.subject,
                                "uid": msg.uid,
                                "message_id": msg.message_id,
                            }
                        )
                    except Exception:
                        LOG.exception("提取附件失败: %s", fname)
                # 更新 last_uid（即使没有附件也前进，避免重复遍历）
                if msg.uid and msg.uid > config.last_uid:
                    config.last_uid = msg.uid
    except imaplib.IMAP4.error as exc:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"IMAP 认证/协议错误: {exc}",
        )
    except (OSError, TimeoutError, ConnectionError) as exc:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"连接邮箱失败: {exc}",
        )
    except Exception as exc:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"收件异常: {exc}",
        )

    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        f"获取 {len(result_list)} 个附件。",
        result_list,
    )


def send_mail(
    config: MailBoxConfig,
    to_addrs: Sequence[str],
    subject: str,
    body: str,
    attachments: Sequence[tuple[str, bytes, str]] | None = None,
) -> IntegrationResult:
    """SMTP 发信。attachments: list of (filename, bytes, content_type)。"""
    if not config.smtp_host:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "未配置 SMTP。",
        )
    secret_name = f"mail_box.password.{config.username.replace('@', '_at_').replace('.', '_')}"
    password = _get_secret_password(secret_name, config.password)
    if not password:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            f"邮箱 {config.name} 密码为空。",
        )

    msg = EmailMessage()
    msg["From"] = config.username
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)

    for fname, data, ctype in attachments or []:
        maintype, subtype = ctype.split("/", 1) if "/" in ctype else ("application", "octet-stream")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)

    context = ssl.create_default_context() if config.smtp_use_ssl else None
    try:
        if config.smtp_use_ssl:
            with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, context=context) as server:
                server.login(config.username, password)
                server.sendmail(config.username, list(to_addrs), msg.as_bytes())
        else:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
                if config.smtp_port == 587:
                    server.starttls(context=context)
                server.login(config.username, password)
                server.sendmail(config.username, list(to_addrs), msg.as_bytes())
    except smtplib.SMTPException as exc:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"SMTP 发送失败: {exc}",
        )
    except (OSError, TimeoutError) as exc:
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"SMTP 连接失败: {exc}",
        )

    return IntegrationResult(
        IntegrationStatus.SUCCESS,
        "邮件已发送。",
    )
