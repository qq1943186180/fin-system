"""Canonical source and PyInstaller entry point for the Finance System."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import traceback
from pathlib import Path


ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app_paths import APP_DATA_ROOT, DB_PATH, ensure_app_dirs, is_in_app_data
from logging_setup import configure_logging


def _write_self_check_report(path: str | None, payload: dict) -> None:
    if not path:
        return
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)


def self_check(report_path: str | None = None) -> int:
    """Validate imports and mutable-path isolation without opening a window."""

    logger = configure_logging()
    payload: dict[str, object] = {
        "ok": False,
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
    }
    try:
        ensure_app_dirs()
        if not is_in_app_data(DB_PATH):
            raise RuntimeError("configured database path is outside the application data root")

        from gui import main as gui_main

        if not hasattr(gui_main, "FinanceApp"):
            raise RuntimeError("GUI entry point FinanceApp is missing")
        required_modules = (
            "db",
            "modules.ledger",
            "modules.reports",
            "modules.arap",
            "modules.expense",
            "modules.invoice_gui",
            "modules.feishu",
            "modules.integration",
            "mail_service",
            "mail_sync",
            "mail_processor",
            "page_mail",
        )
        loaded = {name: importlib.import_module(name) for name in required_modules}

        runtime_db_path = Path(loaded["db"].DB_PATH).resolve()
        if runtime_db_path != Path(DB_PATH).resolve():
            raise RuntimeError("database module is not using the per-user application data path")

        probe = APP_DATA_ROOT / ".self-check-write"
        probe.write_text("ok", encoding="ascii")
        probe.unlink()
        payload.update(
            {
                "ok": True,
                "app_data_writable": True,
                "runtime_components": ["gui.main.FinanceApp", *required_modules],
            }
        )
        logger.info("runtime self-check passed")
        _write_self_check_report(report_path, payload)
        return 0
    except Exception as exc:
        payload.update({"error_type": type(exc).__name__, "error": str(exc)})
        logger.exception("runtime self-check failed")
        _write_self_check_report(report_path, payload)
        return 1


def launch() -> int:
    """Start the Tk desktop application."""

    logger = configure_logging()
    try:
        ensure_app_dirs()
        import tkinter as tk
        from tkinter import messagebox

        from gui.main import FinanceApp

        root = tk.Tk()
        FinanceApp(root)
        logger.info("application started")
        root.mainloop()
        return 0
    except Exception:
        logger.critical("unhandled startup failure", exc_info=True)
        try:
            from tkinter import messagebox

            messagebox.showerror(
                "财务系统启动失败",
                "程序未能启动。详细信息已写入本机日志目录。",
            )
        except Exception:
            traceback.print_exc()
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Finance System desktop application")
    parser.add_argument("--self-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--self-check-report", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.self_check:
        return self_check(args.self_check_report)
    return launch()


if __name__ == "__main__":
    raise SystemExit(main())
