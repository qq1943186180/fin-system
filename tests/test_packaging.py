from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fresh_import(monkeypatch: pytest.MonkeyPatch, home: Path, module_name: str):
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(home))
    for name in (module_name, "app_paths"):
        sys.modules.pop(name, None)
    return importlib.import_module(module_name)


def test_app_paths_are_redirected_to_per_user_home(monkeypatch, tmp_path):
    app_paths = _fresh_import(monkeypatch, tmp_path / "profile", "app_paths")
    paths = app_paths.ensure_app_dirs()

    assert paths.app_data_root == (tmp_path / "profile").resolve()
    assert paths.db_path == paths.app_data_root / "db" / "finance.db"
    assert app_paths.is_in_app_data(paths.reports_dir / "report.csv")
    assert not app_paths.is_in_app_data(PROJECT_ROOT / "db" / "finance.db")
    for directory in (
        paths.db_dir,
        paths.reports_dir,
        paths.invoices_dir,
        paths.log_dir,
        paths.backup_dir,
        paths.config_dir,
    ):
        assert directory.is_dir()


def test_test_secret_backend_is_memory_only(monkeypatch, tmp_path):
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")
    security = _fresh_import(monkeypatch, tmp_path / "profile", "security")
    value = "unit-test-secret-that-must-not-reach-disk"

    security.set_secret("feishu.app_secret", value)
    assert security.get_secret("feishu.app_secret") == value
    assert not security.CREDENTIAL_STORE_PATH.exists()
    assert security.delete_secret("feishu.app_secret")
    assert security.get_secret("feishu.app_secret") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI integration test")
def test_windows_dpapi_round_trip_is_encrypted(tmp_path):
    token = "dpapi-test-secret-7e390f99"
    script = """
from pathlib import Path
import os
import security
security.set_secret('test.roundtrip', os.environ['FS_TEST_TOKEN'])
assert security.get_secret('test.roundtrip') == os.environ['FS_TEST_TOKEN']
raw = Path(security.CREDENTIAL_STORE_PATH).read_bytes()
assert os.environ['FS_TEST_TOKEN'].encode('utf-8') not in raw
assert security.delete_secret('test.roundtrip')
"""
    env = os.environ.copy()
    env["FINANCE_SYSTEM_HOME"] = str(tmp_path / "profile")
    env.pop("FINANCE_SYSTEM_TEST_MODE", None)
    env["FS_TEST_TOKEN"] = token
    subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_rotating_log_redacts_credentials_and_invoice_ids(monkeypatch, tmp_path):
    logging_setup = _fresh_import(monkeypatch, tmp_path / "profile", "logging_setup")
    logger = logging.getLogger(logging_setup.LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    log_dir = tmp_path / "logs"
    logger = logging_setup.configure_logging(log_dir=log_dir, max_bytes=4096)
    logger.error(
        'app_secret="secret-value" Authorization: Bearer token-value '
        '\u53d1\u7968\u53f7\u7801=12345678901234567890'
    )
    for handler in logger.handlers:
        handler.flush()
        handler.close()
    logger.handlers.clear()

    text = (log_dir / logging_setup.DEFAULT_LOG_FILE).read_text(encoding="utf-8")
    assert "secret-value" not in text
    assert "token-value" not in text
    assert "12345678901234567890" not in text
    assert text.count(logging_setup.REDACTED) >= 3


def test_feishu_example_contains_reference_not_secret():
    payload = json.loads(
        (PROJECT_ROOT / "config" / "feishu.example.json").read_text(encoding="utf-8")
    )
    assert "app_secret" not in payload
    assert payload["app_secret_ref"] == "feishu.app_secret"


def test_spec_is_relative_and_excludes_mutable_data():
    text = (PROJECT_ROOT / "财务系统.spec").read_text(encoding="utf-8")
    normalized = text.replace("\\", "/").lower()
    assert "c:/users/" not in normalized
    assert "finance.db" not in normalized
    assert "feishu.json" not in normalized
    assert 'project_root / "run_finance.py"' in text


def test_source_runtime_self_check(tmp_path):
    report = tmp_path / "self-check.json"
    env = os.environ.copy()
    env["FINANCE_SYSTEM_HOME"] = str(tmp_path / "profile")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "run_finance.py"),
            "--self-check",
            "--self-check-report",
            str(report),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (result.stdout, result.stderr, report.read_text() if report.exists() else "")
    assert json.loads(report.read_text(encoding="utf-8"))["ok"] is True


def test_existing_release_has_no_mutable_data_or_secret_config():
    release = PROJECT_ROOT / "release" / "财务系统"
    if not release.exists():
        pytest.skip("release bundle has not been built")
    forbidden = {"finance.db", "feishu.json", "credentials.json", ".env", ".venv"}
    found = [path for path in release.rglob("*") if path.name.lower() in forbidden]
    assert not found
    assert (release / "manifest.sha256").is_file()
