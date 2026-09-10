# -*- coding: utf-8 -*-
"""P6 根因修复的回归测试：发票原件归档 + file_path 落地 + 缺失告警。

锁住的坑（2026-09-11 定位）：
  1. invoice_agent.summarize() 只输出 6 个中文字段，丢掉原件路径/税号/价税合计；
  2. mail_processor 只取 OCR 输出的 fields，丢掉 file 键，且临时文件随后被删；
  3. 结果 invoice_doc.file_path 全空，发票无法回溯原件。
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture()
def finance_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths
    import db

    importlib.reload(app_paths)
    importlib.reload(db)

    from modules import invoice_gui

    importlib.reload(invoice_gui)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")
    app_paths.ensure_app_dirs()
    return app_paths, db, invoice_gui


def _make_pdf(tmp_path: Path, name: str = "滴滴电子发票A.pdf", body: bytes = b"%PDF-1.4 demo") -> Path:
    src = tmp_path / "inbox" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(body)
    return src


def _write_settings(app_paths, payload: dict) -> None:
    app_paths.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (app_paths.CONFIG_DIR / "settings.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


# ---------- 归档基础行为 ----------


def test_ensure_app_dirs_creates_archive_dir(finance_env):
    app_paths, _db, _gui = finance_env
    assert Path(app_paths.INVOICES_ARCHIVE_DIR).is_dir()


def test_archive_copies_into_month_directory(finance_env, tmp_path):
    app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)

    out = gui._archive_invoice_file(str(src), "26337000000663360916", "2026-07-10")

    expected = Path(app_paths.INVOICES_ARCHIVE_DIR) / "2026-07" / "26337000000663360916_滴滴电子发票A.pdf"
    assert Path(out) == expected
    assert expected.read_bytes() == src.read_bytes()
    assert src.exists(), "归档必须是复制，不能移动原件"


def test_archive_is_idempotent_for_same_content(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)

    first = gui._archive_invoice_file(str(src), "NO1", "2026-07-10")
    second = gui._archive_invoice_file(str(src), "NO1", "2026-07-10")

    assert first == second
    siblings = sorted(p.name for p in Path(first).parent.iterdir())
    assert siblings == ["NO1_滴滴电子发票A.pdf"]


def test_archive_suffixes_instead_of_overwriting_on_conflict(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)
    target_dir = Path(gui.INVOICES_ARCHIVE_DIR) / "2026-07"
    target_dir.mkdir(parents=True, exist_ok=True)
    occupied = target_dir / "NO1_滴滴电子发票A.pdf"
    occupied.write_bytes(b"%PDF-1.4 OTHER INVOICE")

    out = Path(gui._archive_invoice_file(str(src), "NO1", "2026-07-10"))

    assert out != occupied
    assert out.name == "NO1_滴滴电子发票A(2).pdf"
    assert occupied.read_bytes() == b"%PDF-1.4 OTHER INVOICE", "已存在的归档不得被覆盖"
    assert out.read_bytes() == src.read_bytes()


def test_archive_returns_source_when_file_missing(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    ghost = tmp_path / "inbox" / "nope.pdf"

    assert gui._archive_invoice_file(str(ghost), "NO1", "2026-07-10") == str(ghost)


def test_archive_returns_empty_for_blank_source(finance_env):
    _app_paths, _db, gui = finance_env
    assert gui._archive_invoice_file("", "NO1", "2026-07-10") == ""
    assert gui._archive_invoice_file(None, "NO1", "2026-07-10") == ""


def test_archive_skipped_when_disabled_by_setting(finance_env, tmp_path):
    app_paths, _db, gui = finance_env
    _write_settings(app_paths, {"invoice_archive_enabled": False})
    src = _make_pdf(tmp_path)

    out = gui._archive_invoice_file(str(src), "NO1", "2026-07-10")

    assert out == str(src)
    assert not (Path(app_paths.INVOICES_ARCHIVE_DIR) / "2026-07").exists()


def test_archive_source_already_in_archive_is_noop(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)
    first = gui._archive_invoice_file(str(src), "NO1", "2026-07-10")

    again = gui._archive_invoice_file(first, "NO1", "2026-07-10")

    assert again == first
    assert not (Path(first).parent / "2026-07" / "2026-07").exists()


def test_archive_unknown_month_for_unparsable_date(finance_env, tmp_path):
    app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)

    out = gui._archive_invoice_file(str(src), "NO1", "not-a-date")

    assert Path(out).parent == Path(app_paths.INVOICES_ARCHIVE_DIR) / "unknown"


# ---------- normalize_invoice 集成 ----------


def test_normalize_archives_original_and_fixes_amount_split(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)
    inv = {
        "发票号码": "26337000000663360916",
        "开票日期": "2026-07-10",
        "销售方名称": "杭州滴滴出行科技有限公司",
        "销售方税号": "91330110MA2H0BC10Q",
        "购买方名称": "高迥（杭州）贸易有限责任公司",
        "购买方税号": "91330105MACRGYRN32",
        "金额合计": "591.11",
        "税额合计": "17.74",
    }

    row = gui.normalize_invoice(inv, source_file=str(src))

    assert Path(row["file_path"]).is_file()
    assert Path(row["file_path"]) != src
    assert row["file_hash"] == gui._file_hash(src)
    # 金额口径：价税合计缺失时必须由 不含税 + 税额 补出，不能丢掉进项税
    assert (row["net_amount_cents"], row["tax_cents"], row["total_cents"]) == (59111, 1774, 60885)
    assert row["seller_tax_no"] == "91330110MA2H0BC10Q"
    assert row["buyer_tax_no"] == "91330105MACRGYRN32"


def test_normalize_uses_mail_filename_for_archive_name(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path, name="mail_invoice_ab12cd.pdf")
    inv = {
        "发票号码": "NO-MAIL-1",
        "开票日期": "2026-09-10",
        "金额合计": "100.00",
        "税额合计": "3.00",
        "_mail_filename": "【一嗨-403.00元】电子发票.pdf",
    }

    row = gui.normalize_invoice(inv, source_file=str(src))

    assert Path(row["file_path"]).name == "NO-MAIL-1_【一嗨-403.00元】电子发票.pdf"


def test_normalize_tolerates_illegal_characters_in_file_name(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path, name="raw.pdf")
    inv = {
        "发票号码": "NO/ILLEGAL",
        "开票日期": "2026-09-10",
        "金额合计": "100.00",
        "税额合计": "3.00",
        "_mail_filename": 'bad:name*with?"chars.pdf',
    }

    row = gui.normalize_invoice(inv, source_file=str(src))

    assert Path(row["file_path"]).is_file()
    assert not any(ch in Path(row["file_path"]).name for ch in '\\/:*?"<>|')


# ---------- register_invoice 端到端 ----------


def test_register_invoice_persists_archived_path(finance_env, tmp_path):
    _app_paths, _db, gui = finance_env
    src = _make_pdf(tmp_path)
    inv = {
        "发票号码": "26337000000663360916",
        "开票日期": "2026-07-10",
        "销售方名称": "杭州滴滴出行科技有限公司",
        "销售方税号": "91330110MA2H0BC10Q",
        "金额合计": "591.11",
        "税额合计": "17.74",
    }

    result = gui.register_invoice(inv, source_file=str(src))

    assert result.ok, result.message
    stored = Path(result.data["file_path"])
    assert stored.is_file()
    assert stored.parent.name == "2026-07"
    assert "invoices" in str(stored) and "archive" in str(stored)


def test_register_invoice_without_source_keeps_path_empty(finance_env):
    _app_paths, _db, gui = finance_env
    inv = {
        "发票号码": "NO-SOURCE-1",
        "开票日期": "2026-07-10",
        "金额合计": "10.00",
        "税额合计": "0.30",
    }

    result = gui.register_invoice(inv)

    assert result.ok, result.message
    assert not result.data["file_path"], "没有来源文件时不应凭空编造路径"


# ---------- load_invoices 告警 ----------


def test_load_invoices_warns_when_file_path_missing(finance_env, tmp_path, monkeypatch):
    _app_paths, _db, gui = finance_env
    # logging_setup 里 logger.propagate=False，caplog 抓不到，直接盯调用
    warnings: list[str] = []
    monkeypatch.setattr(gui.LOG, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "total_amount": 591.11,
                "rows": [
                    {"发票号码": "A1", "开票日期": "2026-07-10", "金额": "591.11"},
                    {"发票号码": "A2", "开票日期": "2026-07-10", "金额": "8.78"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = gui.load_invoices(summary)

    assert len(rows) == 2
    assert any("缺少发票原件路径" in message for message in warnings)


def test_load_invoices_silent_when_path_present(finance_env, tmp_path, monkeypatch):
    _app_paths, _db, gui = finance_env
    warnings: list[str] = []
    monkeypatch.setattr(gui.LOG, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))
    summary = tmp_path / "summary.json"
    summary.write_text(
        json.dumps(
            {"rows": [{"发票号码": "A1", "开票日期": "2026-07-10", "file_path": "x.pdf"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = gui.load_invoices(summary)

    assert len(rows) == 1
    assert not warnings


def test_load_invoices_still_accepts_legacy_six_key_rows(finance_env, tmp_path):
    """向后兼容：修复前生成的 summary.json（仅 6 个中文字段）必须仍能读出。"""
    _app_paths, _db, gui = finance_env
    summary = tmp_path / "summary.json"
    legacy = [
        {
            "发票号码": "26337000000663360916",
            "开票日期": "2026-07-10",
            "销售方": "杭州滴滴出行科技有限公司",
            "金额": "591.11",
            "税额": "17.74",
            "购买方": "高迥（杭州）贸易有限责任公司",
        }
    ]
    summary.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    assert gui.load_invoices(summary) == legacy


# ---------- mail_processor 端到端（不真跑 OCR） ----------


class _CompletedProcess:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_mail_processor_archives_attachment_original(finance_env, monkeypatch):
    app_paths, _db, _gui = finance_env
    _write_settings(app_paths, {"invoice_agent_command": ["python", "ocr_invoice.py"]})

    from modules import mail_processor

    importlib.reload(mail_processor)

    ocr_payload = json.dumps(
        {
            "file": "【一嗨-403.00元】电子发票.pdf",
            "fields": {
                "发票号码": "26337000000601987996",
                "开票日期": "2026-09-10",
                "销售方名称": "一嗨汽车租赁有限公司",
                "金额合计": "356.64",
                "税额合计": "46.36",
                "价税合计": "403.00",
            },
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        mail_processor.subprocess,
        "run",
        lambda *args, **kwargs: _CompletedProcess(ocr_payload),
    )

    result = mail_processor.process_attachment("【一嗨-403.00元】电子发票.pdf", b"%PDF-1.4 mail", "发票")

    assert result.ok, result.message
    stored = Path(result.data["file_path"])
    assert stored.is_file(), "邮件附件原件必须在临时文件被删除前归档留存"
    assert stored.name == "26337000000601987996_【一嗨-403.00元】电子发票.pdf"
    assert (result.data["net_amount_cents"], result.data["tax_cents"], result.data["total_cents"]) == (
        35664,
        4636,
        40300,
    )
