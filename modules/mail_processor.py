# -*- coding: utf-8 -*-
"""邮件附件处理：OCR / 数电票 XML 解析 + 发票入库。"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.einvoice import (
    EINVOICE_EXTS,
    SUPPORTED_EXTS,
    EinvoiceParseError,
    parse_einvoice_attachment,
)
from modules.integration import IntegrationResult, IntegrationStatus

from logging_setup import get_logger

LOG = get_logger("modules.mail_processor")

#: 兼容旧名（历史上本模块自持一份白名单，现已收敛到 modules.einvoice）
ALLOWED_EXTS = SUPPORTED_EXTS


def _agent_command() -> list[str] | None:
    """读取 invoice_agent_command。"""
    try:
        from app_paths import CONFIG_DIR
        config_dir = Path(CONFIG_DIR)
    except Exception:
        LOG.exception("读取 CONFIG_DIR 失败，退回项目 config 目录")
        config_dir = Path(__file__).resolve().parents[1] / "config"

    settings_path = config_dir / "settings.json"
    if not settings_path.exists():
        return None
    try:
        import json
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        cmd = settings.get("invoice_agent_command")
        if isinstance(cmd, list) and cmd:
            return [str(x) for x in cmd]
    except Exception:
        pass
    return None


def _quarantine(data: bytes, filename: str) -> None:
    """把无法解析的附件留存到 invoices/failed/，供人工排查。

    「静默丢弃」是本项目已修过的一次真实缺陷（XML/OFD 不被白名单接收、
    无日志无提示），此处保证任何解析失败都**留痕**。
    """
    try:
        from app_paths import INVOICES_FAILED_DIR

        INVOICES_FAILED_DIR.mkdir(parents=True, exist_ok=True)
        target = INVOICES_FAILED_DIR / f"{Path(filename).stem}_{int(time.time())}{Path(filename).suffix}"
        target.write_bytes(data)
        LOG.info("失败附件已保存: %s", target)
    except Exception:
        LOG.exception("保存失败附件时出错（不影响主流程）: %s", filename)


def _run_ocr(
    cmd: list[str],
    tmp_path: str,
    data: bytes,
    filename: str,
) -> dict | IntegrationResult:
    """跑外部 OCR agent，返回字段字典；失败时返回 IntegrationResult。"""
    result = subprocess.run(
        cmd + [tmp_path],
        capture_output=True,
        text=True,
        timeout=120,
    )
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if result.returncode != 0:
        LOG.error("OCR 失败: %s | stderr: %s", filename, stderr[:200])
        _quarantine(data, filename)
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"OCR 识别失败: {stderr[:100] or 'exit ' + str(result.returncode)}",
            details={"stdout": stdout, "stderr": stderr},
        )

    # 解析 OCR 输出：ocr_invoice.py 单文件模式在 stdout 打 {"file":..., "fields":{...}}
    try:
        import json
        ocr_out = json.loads(stdout) if stdout else {}
    except Exception:
        ocr_data = {"raw": stdout}
    else:
        # 兼容两种形态：直接 fields 字典 / {"file":..,"fields":..}
        if isinstance(ocr_out, dict) and isinstance(ocr_out.get("fields"), dict):
            ocr_data = dict(ocr_out["fields"])
        elif isinstance(ocr_out, list) and ocr_out:
            ocr_data = dict(ocr_out[0].get("fields", ocr_out[0]))
        else:
            ocr_data = ocr_out if isinstance(ocr_out, dict) else {"raw": stdout}
        ocr_data["_mail_filename"] = filename
    return ocr_data


def process_attachment(
    filename: str,
    data: bytes,
    subject: str = "",
) -> IntegrationResult:
    """对单个附件执行 OCR 并调用 register_invoice 入库。"""
    return _process_attachment_impl(filename, data, subject)
def _process_attachment_impl(
    filename: str,
    data: bytes,
    subject: str = "",
) -> IntegrationResult:
    """内部实现：解析附件字段（PDF/图片走 OCR、XML/OFD 走本地解析）并入库。"""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"不支持的文件类型: {suffix}",
        )

    is_einvoice = suffix in EINVOICE_EXTS
    # 数电票 XML/OFD 是结构化数据，不需要 OCR 子进程；仅在走 OCR 时才要求配置
    cmd = None if is_einvoice else _agent_command()
    if not is_einvoice and not cmd:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "未配置 invoice_agent_command（发票识别命令）。",
        )

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="mail_invoice_")
        os.close(fd)
        Path(tmp_path).write_bytes(data)

        if is_einvoice:
            try:
                fields = parse_einvoice_attachment(data, suffix)
            except EinvoiceParseError as exc:
                # 绝不静默丢弃：落盘留痕 + 明确原因，人工可据此补 XML
                _quarantine(data, filename)
                LOG.warning("数电票解析失败: %s | %s", filename, exc)
                return IntegrationResult(
                    IntegrationStatus.BUSINESS_REJECTED,
                    f"数电票解析失败: {exc}",
                )
            fields.setdefault("_source_filename", filename)
        else:
            fields = _run_ocr(cmd or [], tmp_path, data, filename)
            if isinstance(fields, IntegrationResult):
                return fields

        # 调用现有 register_invoice 入库；临时文件此刻仍存在，作为原件来源归档留存
        from modules.invoice_gui import register_invoice

        if fields.get("_source_filename") is None:
            fields["_source_filename"] = filename
        reg_result = register_invoice(fields, source_file=tmp_path)
        if not reg_result.ok:
            return reg_result

        return IntegrationResult(
            IntegrationStatus.SUCCESS,
            f"发票已入库: {filename}",
            reg_result.data,
        )

    except subprocess.TimeoutExpired:
        LOG.error("OCR 超时: %s", filename)
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            "OCR 超时，请稍后重试。",
        )
    except Exception as exc:
        LOG.exception("处理附件异常: %s", filename)
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"处理异常: {exc}",
        )
    finally:
        if tmp_path and Path(tmp_path).exists():
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass


def process_file(file_path: str) -> IntegrationResult:
    """对本地文件执行 OCR 并调用 register_invoice 入库（用于重试）。"""
    path = Path(file_path)
    if not path.exists():
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            f"文件不存在: {file_path}",
        )
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTS:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"不支持的文件类型: {suffix}",
        )
    try:
        data = path.read_bytes()
        # 重试场景没有主题，沿用原文件名
        return _process_attachment_impl(path.name, data)
    except Exception as exc:
        LOG.exception("读取文件失败: %s", file_path)
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"读取文件失败: {exc}",
        )

