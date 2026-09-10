"""Filesystem locations for the Finance System.

Program resources stay beside the source tree (or inside the frozen bundle).
Mutable user data always lives below ``%LOCALAPPDATA%\FinanceSystem``.  Tests
may override the data root with ``FINANCE_SYSTEM_HOME`` before importing this
module.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "FinanceSystem"
APP_HOME_ENV = "FINANCE_SYSTEM_HOME"


def _install_root() -> Path:
    if getattr(sys, "frozen", False):
        bundle_root = getattr(sys, "_MEIPASS", None)
        if bundle_root:
            return Path(bundle_root).resolve()
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _app_data_root() -> Path:
    override = os.environ.get(APP_HOME_ENV)
    if override:
        return Path(os.path.expandvars(os.path.expanduser(override))).resolve()

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        # This branch is primarily useful for non-Windows development and does
        # not weaken the Windows release location.
        local_app_data = os.environ.get("XDG_DATA_HOME")
    if not local_app_data:
        local_app_data = str(Path.home() / ".local" / "share")
    return (Path(local_app_data) / APP_NAME).resolve()


INSTALL_ROOT = _install_root()
APP_DATA_ROOT = _app_data_root()
DB_DIR = APP_DATA_ROOT / "db"
DB_PATH = DB_DIR / "finance.db"
REPORTS_DIR = APP_DATA_ROOT / "reports"
INVOICES_DIR = APP_DATA_ROOT / "invoices"
INVOICES_FAILED_DIR = APP_DATA_ROOT / "invoices" / "failed"
# 入库成功后的发票原件留存目录：<archive>/<开票月份>/<发票号>_<原名>
INVOICES_ARCHIVE_DIR = APP_DATA_ROOT / "invoices" / "archive"
LOG_DIR = APP_DATA_ROOT / "logs"
BACKUP_DIR = APP_DATA_ROOT / "backups"
CONFIG_DIR = APP_DATA_ROOT / "config"
FEISHU_CONFIG_PATH = CONFIG_DIR / "feishu.json"
CREDENTIAL_STORE_PATH = CONFIG_DIR / "credentials.json"


@dataclass(frozen=True)
class AppPaths:
    install_root: Path = INSTALL_ROOT
    app_data_root: Path = APP_DATA_ROOT
    db_dir: Path = DB_DIR
    db_path: Path = DB_PATH
    reports_dir: Path = REPORTS_DIR
    invoices_dir: Path = INVOICES_DIR
    log_dir: Path = LOG_DIR
    backup_dir: Path = BACKUP_DIR
    config_dir: Path = CONFIG_DIR
    feishu_config_path: Path = FEISHU_CONFIG_PATH
    credential_store_path: Path = CREDENTIAL_STORE_PATH


PATHS = AppPaths()


def ensure_app_dirs() -> AppPaths:
    """Create the mutable per-user directories and return their paths."""

    for directory in (
        APP_DATA_ROOT,
        DB_DIR,
        REPORTS_DIR,
        INVOICES_DIR,
        INVOICES_FAILED_DIR,
        INVOICES_ARCHIVE_DIR,
        LOG_DIR,
        BACKUP_DIR,
        CONFIG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    return PATHS


def get_db_path() -> Path:
    """Return the canonical per-user SQLite database path."""

    return DB_PATH


def get_reports_dir() -> Path:
    """Return the canonical report export directory."""

    return REPORTS_DIR


def get_backup_dir() -> Path:
    """Return the canonical database backup directory."""

    return BACKUP_DIR


def get_log_dir() -> Path:
    """Return the canonical rotating-log directory."""

    return LOG_DIR


def is_in_app_data(path: os.PathLike[str] | str) -> bool:
    """Return whether *path* resolves inside the mutable application root."""

    try:
        Path(path).resolve().relative_to(APP_DATA_ROOT)
        return True
    except (OSError, ValueError):
        return False


def resource_path(*parts: str) -> Path:
    """Return an immutable bundled/source resource path."""

    return INSTALL_ROOT.joinpath(*parts)


# ---------- 开机自启 ----------

import winreg  # noqa: PLC0415


def get_autostart_enabled() -> bool:
    """Return whether the app is registered in HKCU Run."""

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "FinanceSystem")
                return bool(value)
            except FileNotFoundError:
                return False
    except OSError:
        return False


def set_autostart_enabled(enabled: bool) -> None:
    """Enable or disable auto-start via HKCU Run."""

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                # Use the frozen exe if available, otherwise python script
                if getattr(sys, "frozen", False):
                    exe_path = sys.executable
                else:
                    exe_path = str(INSTALL_ROOT / "run_finance.py")
                winreg.SetValueEx(key, "FinanceSystem", 0, winreg.REG_SZ, f'"{exe_path}"')
            else:
                try:
                    winreg.DeleteValue(key, "FinanceSystem")
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise RuntimeError(f"注册表操作失败: {exc}") from exc
