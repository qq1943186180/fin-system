# -*- coding: utf-8 -*-
"""邮件附件处理：OCR 识别 + 发票入库。"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from modules.integration import IntegrationResult, IntegrationStatus

from logging_setup import get_logger

LOG = get_logger("modules.mail_processor")


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
    """内部实现：对单个附件执行 OCR 并调用 register_invoice 入库。"""
    cmd = _agent_command()
    if not cmd:
        return IntegrationResult(
            IntegrationStatus.CONFIG_ERROR,
            "未配置 invoice_agent_command（发票识别命令）。",
        )

    suffix = Path(filename).suffix.lower()
    if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"不支持的文件类型: {suffix}",
        )

    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="mail_invoice_")
        os.close(fd)
        Path(tmp_path).write_bytes(data)

        # 调用现有 OCR 子进程
        import subprocess

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
            # 保存失败附件到 invoices/failed/ 目录，人工后可重试
            try:
                from app_paths import INVOICES_FAILED_DIR
                failed_path = INVOICES_FAILED_DIR / f"{Path(filename).stem}_{int(time.time())}{Path(filename).suffix}"
                failed_path.write_bytes(data)
                LOG.info("失败附件已保存: %s", failed_path)
            except Exception:
                pass
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

        # 调用现有 register_invoice 入库
        from modules.invoice_gui import register_invoice

        # register_invoice 期望 dict；传入 OCR 数据
        reg_result = register_invoice(ocr_data)
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
    if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
        return IntegrationResult(
            IntegrationStatus.BUSINESS_REJECTED,
            f"不支持的文件类型: {suffix}",
        )
    try:
        data = path.read_bytes()
        return _process_attachment_impl(path.name, data)
    except Exception as exc:
        LOG.exception("读取文件失败: %s", file_path)
        return IntegrationResult(
            IntegrationStatus.RETRYABLE_ERROR,
            f"读取文件失败: {exc}",
        )

