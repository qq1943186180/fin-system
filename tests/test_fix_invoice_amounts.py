# -*- coding: utf-8 -*-
"""金额口径修正工具（tools/fix_invoice_amounts.py）回归测试。

被测工具会**直接改生产库的台账与凭证**，所以测试重心是**会计正确性 + 安全边界**，
而不只是"能跑通"：

  * 只认 `ocr_result.json` 的票面真值，且只在 `tax_cents==0 && total_cents==net` 这一
    "确属该缺陷"的形态上动手；其它形态一律报告冲突、不猜；
  * 默认只读，不加 `--apply` 绝不写库；写前必备份（WAL 下必须走在线备份 API）；
  * 凭证不是一借一贷 / 含 2221 进项税科目 / 已记账 / 分录金额与发票不符 → 一律拒绝；
  * 改后借贷必须仍然相等，否则整体回滚；
  * 幂等：已正确的行不会被二次写入；
  * 每次改动都留 append-only 的 `audit_event`。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "fix_invoice_amounts.py"

# 滴滴 3% 增值税电子普通发票（票面真值：96.83 + 2.91 = 99.74）
DD_INVOICE_NO = "26117000001021765684"


def _load_tool():
    """tools/ 不是包，按文件路径加载；先注册进 sys.modules 以免注解解析拿不到命名空间。"""
    import sys

    spec = importlib.util.spec_from_file_location("fix_invoice_amounts", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fx = _load_tool()
import db as dbmod  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------
@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "finance.db"
    dbmod.init_db(path)
    return path


@pytest.fixture()
def conn(db_path: Path):
    """用工具自身的 connect()：autocommit + finance_allow_posted_mutation UDF。"""
    connection = fx.connect(db_path)
    yield connection
    connection.close()


def add_invoice(
    conn,
    *,
    invoice_no: str,
    net_cents: int,
    tax_cents: int,
    total_cents: int,
    date: str = "2026-07-10",
    voucher_id: int | None = None,
    status: str = "待确认",
) -> int:
    cursor = conn.execute(
        "INSERT INTO invoice_doc(invoice_no, date, seller, amount, tax, total,"
        " net_amount_cents, tax_cents, total_cents, voucher_id, status)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            invoice_no,
            date,
            "滴滴出行科技有限公司",
            net_cents / 100.0,
            tax_cents / 100.0,
            total_cents / 100.0,
            net_cents,
            tax_cents,
            total_cents,
            voucher_id,
            status,
        ),
    )
    return int(cursor.lastrowid)


def add_voucher(
    conn,
    *,
    vno: str = "2026-07-0015",
    status: str = "pending_review",
    debit_account: str = "5602",
    credit_account: str = "2202",
    cents: int = 9683,
) -> int:
    cursor = conn.execute(
        "INSERT INTO voucher(vno, date, period, summary, creator, status, source)"
        " VALUES (?,?,?,?,?,?,?)",
        (vno, "2026-07-10", "2026-07", "发票-%s" % DD_INVOICE_NO, "系统", status, "invoice"),
    )
    voucher_id = int(cursor.lastrowid)
    conn.execute(
        "INSERT INTO voucher_line(voucher_id, seq, account_code, summary,"
        " debit, debit_cents, credit, credit_cents) VALUES (?,?,?,?,?,?,?,?)",
        (voucher_id, 1, debit_account, "费用", cents / 100.0, cents, 0.0, 0),
    )
    conn.execute(
        "INSERT INTO voucher_line(voucher_id, seq, account_code, summary,"
        " debit, debit_cents, credit, credit_cents) VALUES (?,?,?,?,?,?,?,?)",
        (voucher_id, 2, credit_account, "应付", 0.0, 0, cents / 100.0, cents),
    )
    return voucher_id


def write_truth(path: Path, entries: list[tuple[str, dict]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([{"file": name, "fields": f} for name, f in entries], ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def ticket(invoice_no: str = DD_INVOICE_NO, net: str = "96.83", tax: str = "2.91", total=None) -> dict:
    fields = {"发票号码": invoice_no, "金额合计": net, "税额合计": tax}
    if total is not None:
        fields["价税合计"] = total
    return fields


def truth_for(conn) -> dict:
    """只含一张滴滴票的票面真值（99.74 = 96.83 + 2.91）。"""
    return {
        DD_INVOICE_NO: {
            "net_cents": 9683,
            "tax_cents": 291,
            "total_cents": 9974,
            "file": "dd.pdf",
        }
    }


def defect_row(conn, voucher_id: int | None = None) -> int:
    """受污染形态：税额被丢弃，价税合计塌成不含税。"""
    return add_invoice(
        conn,
        invoice_no=DD_INVOICE_NO,
        net_cents=9683,
        tax_cents=0,
        total_cents=9683,
        voucher_id=voucher_id,
    )


# ---------------------------------------------------------------------------
# 金额解析 / 票面真值解析
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("591.11", 59111),
        ("591", 59100),
        ("1,774.00", 177400),
        ("¥96.83", 9683),
        ("￥96.83", 9683),
        ("  2.91  ", 291),
        ("0.005", 1),  # 四舍五入到分
        ("", None),
        (None, None),
        ("-", None),
        ("abc", None),
    ],
)
def test_to_cents_parses_ticket_amounts(raw, expected) -> None:
    assert fx._to_cents(raw) == expected


def test_load_ocr_truth_indexes_by_invoice_no_and_skips_placeholders(tmp_path: Path) -> None:
    path = write_truth(
        tmp_path / "ocr_result.json",
        [
            ("dd.pdf", ticket(total="99.74")),
            ("junk.pdf", {"发票号码": "-", "金额合计": "0", "税额合计": "0", "价税合计": "0"}),
            ("noluck.pdf", {"发票号码": "26337000000663360916", "金额合计": "591.11"}),  # 缺税额
        ],
    )

    truth = fx.load_ocr_truth(path)

    assert set(truth) == {DD_INVOICE_NO}
    assert truth[DD_INVOICE_NO] == {
        "net_cents": 9683,
        "tax_cents": 291,
        "total_cents": 9974,
        "file": "dd.pdf",
    }


def test_load_ocr_truth_derives_price_inclusive_when_missing(tmp_path: Path) -> None:
    """价税合计抽不到时，用「不含税 + 税额」推出来 —— 不再退化成不含税。"""
    path = write_truth(tmp_path / "ocr_result.json", [("dd.pdf", ticket())])

    truth = fx.load_ocr_truth(path)

    assert truth[DD_INVOICE_NO]["total_cents"] == 9974


def test_load_ocr_truth_skips_contradictory_three_piece(tmp_path: Path) -> None:
    """票面三件套自相矛盾（不含税+税额 ≠ 价税合计）时不能当"真值"。"""
    path = write_truth(tmp_path / "ocr_result.json", [("dd.pdf", ticket(total="99.99", net="96.83", tax="2.91"))])

    assert fx.load_ocr_truth(path) == {}


# ---------------------------------------------------------------------------
# 方案生成：只在该动手时才动手
# ---------------------------------------------------------------------------
def test_plan_fixes_defect_signature_row(conn) -> None:
    invoice_id = defect_row(conn)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert conflicts == []
    assert len(plan) == 1
    entry = plan[0]
    assert entry["invoice_id"] == invoice_id
    assert entry["old"] == {"tax_cents": 0, "total_cents": 9683}
    assert entry["new"] == {"tax_cents": 291, "total_cents": 9974}
    assert entry["delta_cents"] == 291
    assert entry["voucher"] is None  # 未关联凭证 -> 只修台账


def test_plan_skips_already_correct_rows(conn) -> None:
    """幂等：已修正过的行不能再进计划。"""
    add_invoice(conn, invoice_no=DD_INVOICE_NO, net_cents=9683, tax_cents=291, total_cents=9974)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert conflicts == []


def test_plan_ignores_invoices_without_ticket_truth(conn) -> None:
    add_invoice(conn, invoice_no="26337000000663360916", net_cents=59111, tax_cents=0, total_cents=59111)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert conflicts == []


def test_plan_conflicts_when_net_amount_differs_from_ticket(conn) -> None:
    """不含税就不符 -> 不是本缺陷，拒绝，交人工。"""
    add_invoice(conn, invoice_no=DD_INVOICE_NO, net_cents=5000, tax_cents=0, total_cents=5000)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert len(conflicts) == 1
    assert "不含税金额不符" in conflicts[0]


def test_plan_conflicts_on_unexpected_amount_shape(conn) -> None:
    """tax 已填但 total 仍塌着 —— 形态陌生，不能套用同一套改写。"""
    add_invoice(conn, invoice_no=DD_INVOICE_NO, net_cents=9683, tax_cents=291, total_cents=9683)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert len(conflicts) == 1
    assert "金额形态非本缺陷特征" in conflicts[0]


# ---------------------------------------------------------------------------
# 方案生成：凭证侧
# ---------------------------------------------------------------------------
def test_plan_includes_voucher_adjustment_for_linked_invoice(conn) -> None:
    voucher_id = add_voucher(conn, cents=9683)
    defect_row(conn, voucher_id=voucher_id)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert conflicts == []
    voucher = plan[0]["voucher"]
    assert voucher["voucher_id"] == voucher_id
    assert voucher["delta_cents"] == 291
    by_side = {line["side"]: line for line in voucher["lines"]}
    assert by_side["debit"]["account_code"] == "5602"
    assert by_side["credit"]["account_code"] == "2202"
    assert by_side["debit"]["new_cents"] == by_side["credit"]["new_cents"] == 9974


def test_plan_rejects_posted_voucher(conn) -> None:
    # 已记账凭证的分录不可再插改（DB 触发器），所以要按状态机流转到 posted
    voucher_id = add_voucher(conn, status="draft", cents=9683)
    for status in ("pending_review", "audited", "posted"):
        conn.execute("UPDATE voucher SET status=? WHERE id=?", (status, voucher_id))
    assert conn.execute("SELECT status FROM voucher WHERE id=?", (voucher_id,)).fetchone()["status"] == "posted"
    defect_row(conn, voucher_id=voucher_id)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert any("已记账" in item for item in conflicts)


def test_plan_rejects_multi_line_voucher(conn) -> None:
    voucher_id = add_voucher(conn, cents=9683)
    conn.execute(
        "INSERT INTO voucher_line(voucher_id, seq, account_code, summary,"
        " debit, debit_cents, credit, credit_cents) VALUES (?,?,?,?,?,?,?,?)",
        (voucher_id, 3, "5601", "杂费", 1.00, 100, 0.0, 0),
    )
    defect_row(conn, voucher_id=voucher_id)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert any("非一借一贷" in item for item in conflicts)


def test_plan_rejects_voucher_touching_input_tax_account(conn) -> None:
    """含 2221 说明是可抵扣口径，必须人工拆进项税，不能整体改金额。"""
    voucher_id = add_voucher(conn, debit_account="2221", credit_account="2202", cents=9683)
    defect_row(conn, voucher_id=voucher_id)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert any("2221" in item for item in conflicts)


def test_plan_rejects_voucher_amount_mismatch(conn) -> None:
    voucher_id = add_voucher(conn, cents=9000)  # 与发票 9683 不符
    defect_row(conn, voucher_id=voucher_id)

    plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert plan == []
    assert any("与发票原金额不一致" in item for item in conflicts)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------
def test_apply_updates_ledger_voucher_and_audit(conn, db_path: Path) -> None:
    voucher_id = add_voucher(conn, cents=9683)
    invoice_id = defect_row(conn, voucher_id=voucher_id)
    plan, _ = fx.build_plan(conn, truth_for(conn))

    applied = fx.apply_plan(conn, plan, actor="tester")

    assert len(applied) == 1
    fresh = fx.connect(db_path)
    try:
        row = fresh.execute(
            "SELECT tax_cents, total_cents, tax, total FROM invoice_doc WHERE id=?", (invoice_id,)
        ).fetchone()
        assert (row["tax_cents"], row["total_cents"]) == (291, 9974)
        assert (row["tax"], row["total"]) == (2.91, 99.74)  # REAL 列同步，不留下口径分叉

        lines = fresh.execute(
            "SELECT account_code, debit_cents, credit_cents FROM voucher_line"
            " WHERE voucher_id=? ORDER BY seq",
            (voucher_id,),
        ).fetchall()
        assert lines[0]["debit_cents"] == 9974 and lines[0]["credit_cents"] == 0
        assert lines[1]["credit_cents"] == 9974 and lines[1]["debit_cents"] == 0

        totals = fresh.execute(
            "SELECT COALESCE(SUM(debit_cents),0) d, COALESCE(SUM(credit_cents),0) c"
            " FROM voucher_line WHERE voucher_id=?",
            (voucher_id,),
        ).fetchone()
        assert totals["d"] == totals["c"] == 9974

        events = [
            (r["entity_type"], r["event_type"], r["actor"])
            for r in fresh.execute("SELECT entity_type, event_type, actor FROM audit_event ORDER BY id")
        ]
        assert events == [
            ("invoice_doc", "invoice_amount_corrected", "tester"),
            ("voucher", "voucher_amount_corrected", "tester"),
        ]
    finally:
        fresh.close()


def test_apply_is_idempotent(conn) -> None:
    defect_row(conn)  # 未关联凭证 -> 只产生 1 条台账审计
    plan, _ = fx.build_plan(conn, truth_for(conn))
    fx.apply_plan(conn, plan, actor="tester")

    second_plan, conflicts = fx.build_plan(conn, truth_for(conn))

    assert second_plan == []
    assert conflicts == []
    assert conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 1  # 不重复留痕


def test_apply_refuses_when_ledger_changed_after_planning(conn) -> None:
    """TOCTOU：计划生成后、执行前别处改了金额 -> 必须放弃整批并回滚。"""
    invoice_id = defect_row(conn)
    plan, _ = fx.build_plan(conn, truth_for(conn))

    conn.execute("UPDATE invoice_doc SET total_cents=1 WHERE id=?", (invoice_id,))

    with pytest.raises(RuntimeError, match="已变化"):
        fx.apply_plan(conn, plan, actor="tester")
    assert conn.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 0


def test_apply_rolls_back_everything_when_audit_write_fails(conn, db_path: Path, monkeypatch) -> None:
    """任何一步失败都必须整体回滚：不留半截台账，也不留半截审计。"""
    voucher_id = add_voucher(conn, cents=9683)
    invoice_id = defect_row(conn, voucher_id=voucher_id)
    plan, _ = fx.build_plan(conn, truth_for(conn))

    def boom(*args, **kwargs):
        raise RuntimeError("模拟审计写入失败")

    monkeypatch.setattr(fx, "append_audit", boom)

    with pytest.raises(RuntimeError, match="模拟审计写入失败"):
        fx.apply_plan(conn, plan, actor="tester")

    fresh = fx.connect(db_path)
    try:
        row = fresh.execute(
            "SELECT tax_cents, total_cents FROM invoice_doc WHERE id=?", (invoice_id,)
        ).fetchone()
        assert (row["tax_cents"], row["total_cents"]) == (0, 9683)  # 台账未变
        line = fresh.execute(
            "SELECT debit_cents FROM voucher_line WHERE voucher_id=? AND seq=1", (voucher_id,)
        ).fetchone()
        assert line["debit_cents"] == 9683  # 凭证未变
        assert fresh.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 0
    finally:
        fresh.close()


# ---------------------------------------------------------------------------
# 备份
# ---------------------------------------------------------------------------
def test_backup_database_creates_verified_snapshot(db_path: Path) -> None:
    backup = fx.backup_database(db_path, db_path.parent / "backups")

    assert backup.is_file()
    assert backup.parent.name == "backups"
    assert backup.name.endswith(".bak")

    check = sqlite3.connect(backup)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table in ("invoice_doc", "voucher", "voucher_line", "audit_event", "company"):
            source = sqlite3.connect(db_path)
            try:
                expected = source.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            finally:
                source.close()
            assert check.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] == expected
    finally:
        check.close()


def test_backup_preserves_pending_wal_data(tmp_path: Path) -> None:
    """WAL 里尚未 checkpoint 的改动必须进快照，否则备份会"看起来对、实际是旧的"。"""
    db_path = tmp_path / "finance.db"
    dbmod.init_db(db_path)
    writer = sqlite3.connect(str(db_path))
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("UPDATE company SET name='未checkpoint公司' WHERE id=1")
        writer.commit()
        backup = fx.backup_database(db_path, tmp_path / "backups")
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
def test_main_dry_run_does_not_write(db_path: Path, tmp_path: Path, capsys) -> None:
    conn = fx.connect(db_path)
    defect_row(conn)
    conn.close()
    results = write_truth(tmp_path / "ocr_result.json", [("dd.pdf", ticket())])

    code = fx.main(["--db", str(db_path), "--ocr-results", str(results)])

    assert code == 0
    assert "只读模式" in capsys.readouterr().out

    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT tax_cents, total_cents FROM invoice_doc").fetchone() == (0, 9683)
        assert check.execute("SELECT COUNT(*) FROM audit_event").fetchone()[0] == 0
    finally:
        check.close()
    assert not (db_path.parent / "backups").exists()


def test_main_apply_creates_backup_and_writes(db_path: Path, tmp_path: Path, capsys) -> None:
    conn = fx.connect(db_path)
    voucher_id = add_voucher(conn, cents=9683)
    defect_row(conn, voucher_id=voucher_id)
    conn.close()
    results = write_truth(tmp_path / "ocr_result.json", [("dd.pdf", ticket())])

    code = fx.main(["--db", str(db_path), "--ocr-results", str(results), "--apply", "--actor", "tester"])

    assert code == 0
    out = capsys.readouterr().out
    assert "已备份数据库" in out
    assert "修正发票：1" in out
    assert "同步修正凭证：1" in out
    assert (db_path.parent / "backups").is_dir()

    check = sqlite3.connect(db_path)
    check.row_factory = sqlite3.Row
    try:
        row = check.execute("SELECT tax_cents, total_cents FROM invoice_doc").fetchone()
        assert (row["tax_cents"], row["total_cents"]) == (291, 9974)
        assert check.execute("SELECT debit_cents FROM voucher_line WHERE seq=1").fetchone()[0] == 9974
    finally:
        check.close()


def test_main_reports_missing_database(tmp_path: Path, capsys) -> None:
    code = fx.main(["--db", str(tmp_path / "nope" / "finance.db")])

    assert code == 2
    assert "数据库不存在" in capsys.readouterr().err


def test_main_reports_missing_ticket_truth(db_path: Path, tmp_path: Path, capsys) -> None:
    code = fx.main(["--db", str(db_path), "--ocr-results", str(tmp_path / "nope.json")])

    assert code == 2
    assert "票面真值文件不存在" in capsys.readouterr().err
