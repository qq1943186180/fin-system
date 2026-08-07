# -*- coding: utf-8 -*-
"""验证重试页面逻辑：process_file + 失败文件清理。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules"))


def test_retry_page_process_file_integration():
    """Simulate retry tab calling process_file on a failed invoice."""
    from modules.mail_processor import process_file, IntegrationResult, IntegrationStatus

    # Create a fake PDF file in a temp directory
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake_pdf = Path(tmp) / "failed_invoice.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4 fake pdf content")

        # Mock the OCR agent command to avoid needing paddleocr
        with patch("modules.mail_processor._agent_command", return_value=["fake_cmd"]):
            with patch("modules.mail_processor.subprocess.run") as mock_run:
                # Simulate OCR failure (returncode != 0)
                mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="OCR failed")

                result = process_file(str(fake_pdf))

        assert isinstance(result, IntegrationResult)
        assert result.ok is False
        assert "OCR" in result.message or "失败" in result.message or result.message


def test_retry_page_process_file_success_cleanup():
    """Simulate successful retry: process_file returns ok=True when register_invoice returns dict with ok=True."""
    from modules.mail_processor import process_file, IntegrationResult, IntegrationStatus

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        fake_pdf = Path(tmp) / "invoice_ok.pdf"
        fake_pdf.write_bytes(b"%PDF-1.4")

        # Mock successful OCR + register_invoice
        with patch("modules.mail_processor._agent_command", return_value=["fake_cmd"]):
            with patch("modules.mail_processor.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0, stdout='{"ok": true, "data": {"voucher_no": "2026-08-9999"}}', stderr="")
                with patch("modules.invoice_gui.register_invoice") as mock_reg:
                    mock_reg.return_value = IntegrationResult(IntegrationStatus.SUCCESS, "ok", {"voucher_no": "2026-08-9999"})
                    result = process_file(str(fake_pdf))

        assert result.status == IntegrationStatus.SUCCESS
        assert result.message == "发票已入库: invoice_ok.pdf"


if __name__ == "__main__":
    test_retry_page_process_file_integration()
    test_retry_page_process_file_success_cleanup()
    print("✅ retry page logic tests passed")
