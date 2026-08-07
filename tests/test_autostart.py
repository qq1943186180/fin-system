# -*- coding: utf-8 -*-
"""开机自启模块单元测试。"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "modules"))

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows 注册表")


def _mock_winreg():
    """Create a mock winreg module."""
    mock = MagicMock()
    mock.HKEY_CURRENT_USER = 0x80000001
    return mock


def test_get_autostart_disabled_when_key_missing():
    import app_paths

    mock_winreg = _mock_winreg()
    mock_key = MagicMock()
    mock_key.__enter__ = MagicMock(return_value=mock_key)
    mock_key.__exit__ = MagicMock(return_value=False)
    mock_winreg.OpenKey.return_value = mock_key
    # QueryValueEx raises FileNotFoundError when key doesn't exist
    def _query_raise(*_args, **_kwargs):
        raise FileNotFoundError
    mock_winreg.QueryValueEx.side_effect = _query_raise

    original = app_paths.winreg
    app_paths.winreg = mock_winreg
    try:
        assert app_paths.get_autostart_enabled() is False
    finally:
        app_paths.winreg = original


def test_get_autostart_enabled_when_present():
    import app_paths

    mock_winreg = _mock_winreg()
    mock_key = MagicMock()
    mock_key.__enter__ = MagicMock(return_value=mock_key)
    mock_key.__exit__ = MagicMock(return_value=False)
    mock_winreg.OpenKey.return_value = mock_key
    mock_winreg.QueryValueEx.return_value = (r'"C:\test\app.exe"', 1)

    original = app_paths.winreg
    app_paths.winreg = mock_winreg
    try:
        assert app_paths.get_autostart_enabled() is True
    finally:
        app_paths.winreg = original


def test_set_autostart_writes_registry():
    import app_paths

    mock_winreg = _mock_winreg()
    mock_key = MagicMock()
    mock_key.__enter__ = MagicMock(return_value=mock_key)
    mock_key.__exit__ = MagicMock(return_value=False)
    mock_winreg.OpenKey.return_value = mock_key
    mock_winreg.QueryValueEx.side_effect = FileNotFoundError

    original = app_paths.winreg
    app_paths.winreg = mock_winreg
    try:
        fake_root = Path(r"C:\fake")
        with patch.object(app_paths, "INSTALL_ROOT", fake_root):
            app_paths.set_autostart_enabled(True)
    finally:
        app_paths.winreg = original

    assert mock_winreg.SetValueEx.called
    args = mock_winreg.SetValueEx.call_args
    assert args[0][1] == "FinanceSystem"


def test_set_autostart_disabled_deletes_registry():
    import app_paths

    mock_winreg = _mock_winreg()
    mock_key = MagicMock()
    mock_key.__enter__ = MagicMock(return_value=mock_key)
    mock_key.__exit__ = MagicMock(return_value=False)
    mock_winreg.OpenKey.return_value = mock_key
    mock_winreg.QueryValueEx.side_effect = FileNotFoundError

    original = app_paths.winreg
    app_paths.winreg = mock_winreg
    try:
        app_paths.set_autostart_enabled(False)
    finally:
        app_paths.winreg = original

    assert mock_winreg.DeleteValue.called
