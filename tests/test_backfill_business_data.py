# -*- coding: utf-8 -*-
"""业务数据回补工具（tools/backfill_business_data.py）回归测试。

被测工具会写生产库，因此测试重点是**安全边界**，而不只是功能：
  * 默认只读：不加 --apply 绝不碰数据库；
  * 写前必须备份；
  * 只按发票号码硬关联，金额不符 / 命中多份 / 重复指向同一文件一律拒绝；
  * 只填空列，绝不覆盖已有值；
  * 幂等：重复执行不产生第二次写入；
  * 每次改动都留 audit_event。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "backfill_business_data.py"


def _load_tool():
    """tools/ 不是包，按文件路径加载；必须先注册进 sys.modules，
    否则 @dataclass 解析类型注解时会拿不到模块命名空间。"""
    import sys

    spec = importlib.util.spec_from_file_location("backfill_business_data", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bf = _load_tool()
import db as dbmod  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    root = tmp_path / "app"
    (root / "db").mkdir(parents=True)
    (root / "invoices" / "failed").mkdir(parents=True)
    (root / "invoices" / "archive").mkdir(parents=True)
    return root


@pytest.fixture()
def db_path(workdir: Path) -> Path:
    path = workdir / "db" / "finance.db"
    dbmod.init_db(path)
    return path


@pytest.fixture()
def conn(db_path: Path):
    connection = dbmod.get_conn(db_path)
    yield connection
    connection.close()


def add_invoice(
    conn,
    *,
    invoice_no: str,
    total_cents: int,
    date: str = "2026-07-10",
    seller: str = "测试供应商",
    file_path: str = "",
    file_hash: str = "",
    seller_tax_no: str = "",
    buyer_tax_no: str = "",
) -> int:
    cursor = conn.execute(
        "INSERT INTO invoice_doc(invoice_no, date, seller, total, total_cents, "
        "  amount, tax, file_path, file_hash, seller_tax_no, buyer_tax_no, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            invoice_no,
            date,
            seller,
            total_cents / 100.0,
            total_cents,
            total_cents / 100.0,
            0.0,
            file_path,
            file_hash,
            seller_tax_no,
            buyer_tax_no,
            "待确认",
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def make_pdf(directory: Path, name: str, payload: bytes = b"%PDF-1.7 fake invoice") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_bytes(payload)
    return target


def write_ocr_result(results_dir: Path, entries: list[tuple[str, dict]]) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "ocr_result.json").write_text(
        json.dumps(
            [{"file": name, "fields": fields} for name, fields in entries],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def build_agent_evidence(workdir: Path, entries: list[tuple[str, dict]]):
    agent_root = workdir / "agent"
    invoices = agent_root / "invoices"
    for name, _ in entries:
        make_pdf(invoices, name)
    write_ocr_result(agent_root / "results", entries)
    return bf.evidence_from_agent_results(agent_root / "results", invoices)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def test_sha256_matches_invoice_gui_implementation(tmp_path: Path) -> None:
    """归档哈希必须与财务系统入库时用的算法一致，否则去重索引语义会漂移。"""
    from modules.invoice_gui import _file_hash

    sample = tmp_path / "sample.pdf"
    sample.write_bytes(b"x" * 5000)

    assert bf.sha256_file(sample) == _file_hash(sample)
    assert bf.sha256_file(sample) != ""
    assert bf.sha256_file(tmp_path / "missing.pdf") == ""


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("26337000000663360916", "26337000000663360916"),
        (" 26337000000663360916 ", "26337000000663360916"),
        ("26337000000663360916\n", "26337000000663360916"),
        ("-", ""),
        ("", ""),
        (None, ""),
        ("1234", ""),
    ],
)
def test_normalise_invoice_no_rejects_placeholders(raw, expected: str) -> None:
    assert bf.normalise_invoice_no(raw) == expected


def test_evidence_from_agent_results_parses_and_skips_placeholders(workdir: Path) -> None:
    entries = [
        ("a.pdf", {"发票号码": "-", "金额合计": "0"}),
        ("b.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"}),
    ]
    items = build_agent_evidence(workdir, entries)

    assert len(items) == 1
    assert items[0].invoice_no == "26337000000663360916"
    assert items[0].source_path.name == "b.pdf"
    assert items[0].total_cents() == 59111


def test_evidence_from_ocr_calls_ignores_junk_placeholders(workdir: Path) -> None:
    """25 字节的 failed_invoice_*.pdf 是失败占位符，不能当证据。"""
    failed = workdir / "invoices" / "failed"
    make_pdf(failed, "failed_invoice_1.pdf", b"x" * 25)
    good = make_pdf(failed, "real.pdf", b"y" * 500)

    calls: list[Path] = []

    def runner(path: Path):
        calls.append(path)
        return {"file": path.name, "fields": {"发票号码": "26317000002656636842", "价税合计": "216.96"}}

    items, failures = bf.evidence_from_ocr_calls(sorted(failed.glob("*.pdf")), runner)

    assert len(items) == 1
    assert items[0].source_path == good.resolve()
    assert calls == [good]                       # 占位符没有被送去识别
    assert any("占位符" in message for message in failures)


# ---------------------------------------------------------------------------
# 方案生成
# ---------------------------------------------------------------------------
def test_plan_links_verified_invoice(conn, workdir: Path) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    evidence = build_agent_evidence(
        workdir,
        [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11",
                    "销售方税号": "91330110MA2H0BC10Q", "购买方税号": "91330105MACRGYRN32"})],
    )

    plan = bf.build_plan(
        conn,
        bf.index_evidence(evidence),
        archive_dir=workdir / "invoices" / "archive",
    )

    assert len(plan.links) == 1
    assert plan.conflicts == []
    link = plan.links[0]
    assert link.invoice_id == 1
    assert link.fields["file_path"].endswith("26337000000663360916_A.pdf")
    assert link.fields["file_hash"] == bf.sha256_file(evidence[0].source_path)
    assert link.fields["seller_tax_no"] == "91330110MA2H0BC10Q"
    assert link.fields["buyer_tax_no"] == "91330105MACRGYRN32"


def test_plan_refuses_on_amount_mismatch(conn, workdir: Path) -> None:
    """金额是第二道校验：PDF 金额与库内不一致就必须拒绝，不能靠文件名猜。"""
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "500.00"})]
    )

    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    assert plan.links == []
    assert len(plan.conflicts) == 1
    assert "金额不符" in plan.conflicts[0]


def test_plan_refuses_on_ambiguous_candidates(conn, workdir: Path) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    evidence = build_agent_evidence(
        workdir,
        [
            ("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"}),
            ("A-副本.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"}),
        ],
    )

    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    assert plan.links == []
    assert len(plan.conflicts) == 1
    assert "命中 2 份文件" in plan.conflicts[0]


def test_plan_reports_unresolved_without_evidence(conn, workdir: Path) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)

    plan = bf.build_plan(conn, {}, archive_dir=workdir / "invoices" / "archive")

    assert plan.links == []
    assert len(plan.unresolved) == 1
    assert "找不到可核验的 PDF" in plan.unresolved[0]


def test_plan_skips_already_filled_rows(conn, workdir: Path) -> None:
    add_invoice(
        conn,
        invoice_no="26337000000663360916",
        total_cents=59111,
        file_path="C:/already/there.pdf",
        file_hash="deadbeef",
        seller_tax_no="91330110MA2H0BC10Q",
        buyer_tax_no="91330105MACRGYRN32",
    )
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"})]
    )

    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    assert plan.links == []
    assert len(plan.unchanged) == 1
    assert plan.conflicts == []


def test_plan_only_fills_the_empty_columns(conn, workdir: Path) -> None:
    """file_path 已填、税号为空时，只能补税号，不能重复写 file_path。"""
    add_invoice(
        conn,
        invoice_no="26337000000663360916",
        total_cents=59111,
        file_path="C:/already/there.pdf",
        file_hash="deadbeef",
    )
    evidence = build_agent_evidence(
        workdir,
        [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11",
                    "销售方税号": "91330110MA2H0BC10Q", "购买方税号": "91330105MACRGYRN32"})],
    )

    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    assert len(plan.links) == 1
    assert set(plan.links[0].fields) == {"seller_tax_no", "buyer_tax_no"}


def test_plan_refuses_to_link_same_file_twice(conn, workdir: Path) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    add_invoice(conn, invoice_no="26337000000663360917", total_cents=59111)
    evidence = build_agent_evidence(
        workdir,
        [
            ("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"}),
            ("A-again.pdf", {"发票号码": "26337000000663360917", "价税合计": "591.11"}),
        ],
    )
    # 让两份证据指向同一份真实文件内容 -> 哈希相同
    (workdir / "agent" / "invoices" / "A-again.pdf").write_bytes(
        (workdir / "agent" / "invoices" / "A.pdf").read_bytes()
    )
    refreshed = bf.evidence_from_agent_results(
        workdir / "agent" / "results", workdir / "agent" / "invoices"
    )

    plan = bf.build_plan(conn, bf.index_evidence(refreshed), archive_dir=workdir / "invoices" / "archive")

    assert len(plan.links) == 1
    assert any("指向同一份文件" in item for item in plan.conflicts)


def test_plan_archives_under_invoice_own_month(conn, workdir: Path) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111, date="2026-07-10")
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"})]
    )

    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    assert "2026-07" in plan.links[0].fields["file_path"]
    assert Path(plan.links[0].fields["file_path"]).name == "26337000000663360916_A.pdf"


def test_company_plan_only_fills_empty_fields(conn) -> None:
    plan = bf.build_plan(
        conn,
        {},
        company=bf.CompanyPlan(name="高迥（杭州）贸易有限责任公司", tax_no="91330105MACRGYRN32",
                               addr="杭州市拱墅区", note=""),
        archive_dir=None,
        copy_files=False,
    )

    assert plan.company.changes(plan.company_current) == {
        "name": ("", "高迥（杭州）贸易有限责任公司"),
        "tax_no": ("", "91330105MACRGYRN32"),
        "addr": ("", "杭州市拱墅区"),
    }


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def test_apply_writes_fields_audit_and_is_idempotent(conn, workdir: Path, db_path: Path) -> None:
    invoice_id = add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"})]
    )
    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")

    outcome = bf.apply_plan(conn, plan, actor="tester")
    assert len(outcome["linked"]) == 1

    row = conn.execute("SELECT * FROM invoice_doc WHERE id=?", (invoice_id,)).fetchone()
    assert row["file_path"].endswith("26337000000663360916_A.pdf")
    assert Path(row["file_path"]).is_file()             # 库里路径必须真实存在
    assert row["file_hash"] == bf.sha256_file(row["file_path"])

    events = conn.execute(
        "SELECT event_type, actor FROM audit_event WHERE entity_type='invoice_doc' AND entity_id=?",
        (invoice_id,),
    ).fetchall()
    assert [(e["event_type"], e["actor"]) for e in events] == [("invoice_file_linked", "tester")]

    # 幂等：再跑一次应无任何链接
    plan2 = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")
    assert plan2.links == []
    assert len(plan2.unchanged) == 1


def test_apply_never_overwrites_existing_file_path(conn, workdir: Path) -> None:
    invoice_id = add_invoice(
        conn, invoice_no="26337000000663360916", total_cents=59111,
        file_path="C:/human/entered.pdf", file_hash="human-hash",
    )
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11",
                             "销售方税号": "91330110MA2H0BC10Q"})]
    )
    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")
    bf.apply_plan(conn, plan, actor="tester")

    row = conn.execute("SELECT * FROM invoice_doc WHERE id=?", (invoice_id,)).fetchone()
    assert row["file_path"] == "C:/human/entered.pdf"
    assert row["file_hash"] == "human-hash"
    assert row["seller_tax_no"] == "91330110MA2H0BC10Q"


def test_apply_blocks_overwriting_a_path_filled_after_planning(conn, workdir: Path) -> None:
    """TOCTOU：方案生成后、执行前，另一处（人工或另一次运行）填上了路径。

    此时必须放弃写入该列——既不覆盖别人写的值，也不留下没人引用的归档文件。
    """
    invoice_id = add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    evidence = build_agent_evidence(
        workdir, [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"})]
    )
    plan = bf.build_plan(conn, bf.index_evidence(evidence), archive_dir=workdir / "invoices" / "archive")
    assert "file_path" in plan.links[0].fields

    # 竞态：执行前别处已填入
    conn.execute(
        "UPDATE invoice_doc SET file_path='C:/human/late.pdf', file_hash='human' WHERE id=?",
        (invoice_id,),
    )
    conn.commit()

    outcome = bf.apply_plan(conn, plan, actor="tester")

    row = conn.execute("SELECT file_path, file_hash FROM invoice_doc WHERE id=?", (invoice_id,)).fetchone()
    assert row["file_path"] == "C:/human/late.pdf"
    assert row["file_hash"] == "human"
    assert outcome["linked"] == []
    assert len(outcome["skipped"]) == 1
    assert outcome["copied"] == []                       # 不该留下孤儿归档文件
    assert conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 0


def test_apply_company_inserts_when_row_missing(conn) -> None:
    conn.execute("DELETE FROM company")
    conn.commit()
    plan = bf.build_plan(
        conn, {},
        company=bf.CompanyPlan(name="高迥（杭州）贸易有限责任公司", tax_no="91330105MACRGYRN32",
                               addr="", note=""),
        archive_dir=None, copy_files=False,
    )
    assert plan.company_row_exists is False

    bf.apply_plan(conn, plan, actor="tester")

    row = conn.execute("SELECT * FROM company WHERE id=1").fetchone()
    assert row["name"] == "高迥（杭州）贸易有限责任公司"
    assert row["tax_no"] == "91330105MACRGYRN32"


def test_backup_database_is_consistent_and_verifiable(db_path: Path) -> None:
    """WAL 模式下必须用在线备份 API，产出自洽副本并做完整性/行数校验。"""
    backup = bf.backup_database(db_path)

    assert backup.is_file()
    assert backup.parent.name == "backups"

    check = sqlite3.connect(backup)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table in ("invoice_doc", "company", "audit_event"):
            original = sqlite3.connect(db_path)
            try:
                expected = original.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            finally:
                original.close()
            assert check.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == expected
    finally:
        check.close()


def test_backup_database_preserves_pending_wal_data(workdir: Path) -> None:
    """写进 WAL 但尚未 checkpoint 的数据，必须出现在备份里。"""
    db_path = workdir / "db" / "finance.db"
    dbmod.init_db(db_path)
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE company SET name='未checkpoint公司' WHERE id=1")
        writer.commit()
        # 不关闭连接 -> 数据仍在 -wal 里
        backup = bf.backup_database(db_path)
        assert (workdir / "db" / "finance.db-wal").exists()
    finally:
        writer.close()

    check = sqlite3.connect(backup)
    try:
        assert check.execute("SELECT name FROM company WHERE id=1").fetchone()[0] == "未checkpoint公司"
    finally:
        check.close()


# ---------------------------------------------------------------------------
# 命令行边界
# ---------------------------------------------------------------------------
def test_main_dry_run_does_not_touch_database(workdir: Path, db_path: Path, capsys) -> None:
    conn = dbmod.get_conn(db_path)
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    conn.close()

    code = bf.main(["--db", str(db_path), "--ocr-python", str(workdir / "nope.exe")])

    assert code == 0
    output = capsys.readouterr().out
    assert "只读模式" in output

    check = sqlite3.connect(db_path)
    row = check.execute("SELECT file_path, file_hash FROM invoice_doc").fetchone()
    assert row == ("", "")
    assert check.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 0
    check.close()
    assert not (workdir / "db" / "backups").exists()


def test_main_apply_creates_backup_and_writes(workdir: Path, db_path: Path, capsys) -> None:
    conn = dbmod.get_conn(db_path)
    add_invoice(conn, invoice_no="26337000000663360916", total_cents=59111)
    conn.close()

    # 预置 agent 证据
    agent = workdir / "agent"
    make_pdf(agent / "invoices", "A.pdf")
    write_ocr_result(
        agent / "results",
        [("A.pdf", {"发票号码": "26337000000663360916", "价税合计": "591.11"})],
    )

    code = bf.main(
        [
            "--db", str(db_path),
            "--agent-root", str(agent),
            "--ocr-python", str(workdir / "nope.exe"),
            "--company-name", "高迥（杭州）贸易有限责任公司",
            "--apply",
        ]
    )

    assert code == 0
    assert "已备份数据库" in capsys.readouterr().out
    assert (workdir / "db" / "backups").is_dir()

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    row = check.execute("SELECT * FROM invoice_doc").fetchone()
    assert row["file_path"].endswith("26337000000663360916_A.pdf")
    assert check.execute("SELECT name FROM company WHERE id=1").fetchone()["name"] == "高迥（杭州）贸易有限责任公司"
    kinds = {r[0] for r in check.execute("SELECT event_type FROM audit_event")}
    assert kinds == {"invoice_file_linked", "company_field_updated"}
    check.close()


def test_main_rejects_unknown_field(db_path: Path, capsys) -> None:
    code = bf.main(["--db", str(db_path), "--fields", "file_path,evil_column"])

    assert code == 2
    assert "不支持的列" in capsys.readouterr().err


def test_main_reports_missing_database(tmp_path: Path, capsys) -> None:
    code = bf.main(["--db", str(tmp_path / "nope" / "finance.db")])

    assert code == 2
    assert "数据库不存在" in capsys.readouterr().err
