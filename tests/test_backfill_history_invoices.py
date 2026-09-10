# -*- coding: utf-8 -*-
"""历史发票补收工具（tools/backfill_history_invoices.py）与 OCR 探测的回归测试。

工具会**真实连邮箱、真实写生产库**，所以测试只覆盖**可离线验证的纯逻辑**：

  * OCR 分级判定（OK/PARTIAL/BAD/ERROR）与"我方税号落在销售方"的错配识别；
  * 行程单/结算单等辅助凭证的识别（它们**不该**算识别失败）；
  * 「收 N 封」的语义 —— 默认只取含受支持附件的邮件；
  * **OCR 预筛选票**：这是本项目实测最容易踩坑的地方 —— 按 uid 升序的前 6 封
    （2023 同程系）OCR 全抽不到票号/金额，不做预筛则 `--limit 5 --apply`
    会全部被拒、一张票进不来；
  * 补收入库必须写 `audit_event`（P5/P7 惯例：写生产库要留痕）。

不测：真实 IMAP 连接、真实 OCR 子进程、真实 `register_invoice` 写库
（这些属于端到端，需真实邮箱，见 HANDOFF §10.3 的实跑记录）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOLS = PROJECT_ROOT / "tools"

COMPANY_NO = "91330105MACRGYRN32"


def _load(name: str, path: Path):
    """tools/ 非包，按文件路径加载并注册进 sys.modules。"""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load("invoice_ocr_probe", TOOLS / "invoice_ocr_probe.py")
bf = _load("backfill_history_invoices", TOOLS / "backfill_history_invoices.py")

import db as dbmod  # noqa: E402


# ---------------------------------------------------------------------------
# OCR 分级判定
# ---------------------------------------------------------------------------
def _fields(**kw):
    base = {
        "发票号码": "24337000000044417401",
        "开票日期": "2024-06-19",
        "价税合计": "121.00",
        "金额合计": "107.08",
        "税额合计": "13.92",
        "购买方税号": COMPANY_NO,
        "销售方税号": "91330100MA27XR8YX6",
    }
    base.update(kw)
    return base


def test_classify_ok_full_fields():
    level, issues = probe.classify(_fields(), COMPANY_NO)
    assert level == "OK"
    assert issues == []


def test_classify_ok_when_tax_total_missing_but_net_and_tax_present():
    """价税合计缺失但有净额+税额 → 下游能自行兜底，仍算 OK。"""
    f = _fields()
    f.pop("价税合计")
    level, issues = probe.classify(f, COMPANY_NO)
    assert level == "OK"


def test_classify_partial_missing_amount():
    f = _fields()
    f.pop("价税合计")
    f.pop("税额合计")
    level, issues = probe.classify(f, COMPANY_NO)
    assert level == "PARTIAL"
    assert "缺金额" in issues


def test_classify_partial_missing_invoice_no():
    f = _fields()
    f.pop("发票号码")
    level, issues = probe.classify(f, COMPANY_NO)
    assert level == "PARTIAL"
    assert "缺发票号码" in issues


def test_classify_detects_swapped_buyer_seller_tax_no():
    """我方税号落在销售方 → 疑买卖方错配（同程票实测就是这个形态）。"""
    f = _fields(购买方税号="91320105MA1MXUCP1F", 销售方税号=COMPANY_NO)
    level, issues = probe.classify(f, COMPANY_NO)
    assert level == "PARTIAL"
    assert any("错配" in i for i in issues)


def test_classify_no_swap_when_company_is_buyer():
    f = _fields(购买方税号=COMPANY_NO, 销售方税号="91320105MA1MXUCP1F")
    _, issues = probe.classify(f, COMPANY_NO)
    assert not any("错配" in i for i in issues)


def test_classify_bad_when_all_empty():
    level, issues = probe.classify({"行程条数": 0, "_source": "ocr"}, COMPANY_NO)
    assert level == "BAD"
    assert "缺发票号码" in issues


def test_classify_error_on_subprocess_failure():
    level, issues = probe.classify({"_error": "exit 1"}, COMPANY_NO)
    assert level == "ERROR"
    assert issues == ["exit 1"]


def test_classify_without_company_no_skips_swap_check():
    """没给企业税号时不应误报错配。"""
    f = _fields(购买方税号="X" * 18, 销售方税号=COMPANY_NO)
    level, _ = probe.classify(f, "")
    assert level == "OK"


# ---------------------------------------------------------------------------
# 辅助凭证识别
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "同程用车行程单_2023-11-30.pdf",
        "订单1218849523结算单.pdf",
        "结账单20260615.pdf",
        "滴滴出行行程报销单A.pdf",
        "第三方发票及行程单.pdf",
    ],
)
def test_is_auxiliary_true(name):
    assert probe.is_auxiliary(name) is True


@pytest.mark.parametrize(
    "name",
    ["滴滴电子发票A.pdf", "花小猪打车电子发票.pdf", "dzfp_26332000005074455211.pdf"],
)
def test_is_auxiliary_false(name):
    assert probe.is_auxiliary(name) is False


# ---------------------------------------------------------------------------
# load_agent_command
# ---------------------------------------------------------------------------
def test_load_agent_command(tmp_path: Path):
    cfg = tmp_path / "settings.json"
    cfg.write_text(
        json.dumps({"invoice_agent_command": ["py.exe", "ocr.py"]}), encoding="utf-8"
    )
    assert probe.load_agent_command(cfg) == ["py.exe", "ocr.py"]


def test_load_agent_command_missing_or_invalid(tmp_path: Path):
    assert probe.load_agent_command(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"invoice_agent_command": []}), encoding="utf-8")
    assert probe.load_agent_command(bad) is None


# ---------------------------------------------------------------------------
# pick：排序与过滤
# ---------------------------------------------------------------------------
def _mail(uid: int, n_att: int = 1):
    return {
        "uid": uid,
        "subject": f"m{uid}",
        "from_": "a@b.c",
        "date": "2024-06-19T10:00:00",
        "attachments": [{"filename": f"f{i}.pdf", "bytes": b"x"} for i in range(n_att)],
    }


def test_pick_oldest_is_default_order():
    items = [_mail(30), _mail(10), _mail(20)]
    got = bf.pick(items, 2, "oldest")
    assert [m["uid"] for m in got] == [10, 20]


def test_pick_newest_order():
    items = [_mail(30), _mail(10), _mail(20)]
    got = bf.pick(items, 2, "newest")
    assert [m["uid"] for m in got] == [30, 20]


def test_pick_skips_attachmentless_mails():
    """纯通知/下载链接型邮件不该吃额度（本项目 66 封里有 16 封是这种）。"""
    items = [_mail(10, 0), _mail(20, 1), _mail(30, 0), _mail(40, 1)]
    got = bf.pick(items, 5, "oldest")
    assert [m["uid"] for m in got] == [20, 40]


def test_pick_limit_zero():
    assert bf.pick([_mail(10)], 0, "oldest") == []


# ---------------------------------------------------------------------------
# pick_recognizable：OCR 预筛选票（核心安全网）
# ---------------------------------------------------------------------------
def _probe_result(level: str, aux: bool = False):
    return {"filename": "x.pdf", "level": level, "issues": [], "fields": {}, "is_auxiliary": aux}


def test_pick_recognizable_skips_unrecognizable_head(monkeypatch):
    """前两封 OCR 失败 → 必须跳过，选中第 3 封（否则 --limit 会全部白收）。"""
    calls: list[int] = []

    def fake_probe(cmd, filename, data, company_no="", timeout=180):
        calls.append(1)
        uid = fake_probe.order.pop(0)
        return _probe_result("OK" if uid == 300 else "BAD")

    fake_probe.order = [100, 200, 300, 400]
    monkeypatch.setattr(bf, "probe_bytes", fake_probe)

    mails = [_mail(100), _mail(200), _mail(300), _mail(400)]
    selected, probes = bf.pick_recognizable(
        mails, 1, "oldest", ["ocr"], COMPANY_NO, verbose=False
    )
    assert [m["uid"] for m, _ in selected] == [300]
    assert [p["uid"] for p in probes] == [100, 200, 300]  # 收够即停，不再探测 400


def test_pick_recognizable_ignores_auxiliary_attachments(monkeypatch):
    """只有行程单的邮件不算"可入库"，不该被选中。"""
    def fake_probe(cmd, filename, data, company_no="", timeout=180):
        return _probe_result("OK", aux=True)

    monkeypatch.setattr(bf, "probe_bytes", fake_probe)
    selected, probes = bf.pick_recognizable(
        [_mail(100)], 3, "oldest", ["ocr"], COMPANY_NO, verbose=False
    )
    assert selected == []
    assert probes[0]["recognizable_invoices"] == 0


def test_pick_recognizable_counts_invoices_per_mail(monkeypatch):
    """一封邮件含多张发票 → recognizable_invoices 累加。"""

    def fake_probe(cmd, filename, data, company_no="", timeout=180):
        return _probe_result("OK")

    monkeypatch.setattr(bf, "probe_bytes", fake_probe)
    selected, probes = bf.pick_recognizable(
        [_mail(100, 3)], 1, "oldest", ["ocr"], COMPANY_NO, verbose=False
    )
    assert len(selected) == 1
    assert probes[0]["recognizable_invoices"] == 3


def test_pick_recognizable_skips_attachmentless(monkeypatch):
    def fake_probe(cmd, filename, data, company_no="", timeout=180):
        return _probe_result("OK")

    monkeypatch.setattr(bf, "probe_bytes", fake_probe)
    selected, _ = bf.pick_recognizable(
        [_mail(100, 0), _mail(200, 1)], 5, "oldest", ["ocr"], COMPANY_NO, verbose=False
    )
    assert [m["uid"] for m, _ in selected] == [200]


def test_pick_recognizable_respects_limit(monkeypatch):
    def fake_probe(cmd, filename, data, company_no="", timeout=180):
        return _probe_result("OK")

    monkeypatch.setattr(bf, "probe_bytes", fake_probe)
    mails = [_mail(u) for u in (10, 20, 30, 40)]
    selected, _ = bf.pick_recognizable(
        mails, 2, "oldest", ["ocr"], COMPANY_NO, verbose=False
    )
    assert [m["uid"] for m, _ in selected] == [10, 20]


# ---------------------------------------------------------------------------
# CLI 默认值
# ---------------------------------------------------------------------------
def test_parse_args_defaults():
    a = bf.parse_args([])
    assert a.limit == 3
    assert a.order == "oldest"
    assert a.apply is False
    assert a.probe is True  # 预筛默认开启


def test_parse_args_no_probe_disables_prescreen():
    a = bf.parse_args(["--no-probe", "--limit", "5", "--apply"])
    assert a.probe is False
    assert a.limit == 5
    assert a.apply is True


# ---------------------------------------------------------------------------
# 审计留痕
# ---------------------------------------------------------------------------
@pytest.fixture()
def tmp_db(tmp_path: Path, monkeypatch):
    path = tmp_path / "finance.db"
    dbmod.init_db(path)
    monkeypatch.setattr(dbmod, "DB_PATH", str(path))
    return path


def test_record_backfill_audit_writes_event(tmp_db: Path):
    ok = bf.record_backfill_audit(
        {"id": 12, "invoice_no": "24337000000044417401", "total_cents": 12100},
        {"uid": 1691478008, "subject": "一嗨出行订单号1218849523发票", "date": "2024-06-19T16:15:00"},
        "订单1218849523电子发票(1).pdf",
        "li1",
    )
    assert ok is True

    import sqlite3

    conn = sqlite3.connect(str(tmp_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute("select * from audit_event order by id desc limit 1").fetchone()
    conn.close()
    assert row["entity_type"] == "invoice_doc"
    assert row["entity_id"] == 12
    assert row["event_type"] == "invoice_backfilled"
    assert row["actor"] == "li1"
    detail = json.loads(row["details_json"])
    assert detail["source"] == "email_backfill"
    assert detail["mail_uid"] == 1691478008
    assert detail["invoice_no"] == "24337000000044417401"
    assert detail["attachment"].endswith(".pdf")


def test_record_backfill_audit_returns_false_on_failure(monkeypatch):
    """审计写失败不能抛异常（否则会掩盖已成功的入库）。"""
    def boom(*a, **kw):
        raise RuntimeError("disk full")

    monkeypatch.setattr(bf.db, "get_conn", boom)
    assert bf.record_backfill_audit({"id": 1}, {}, "f.pdf", "li1") is False


# ---------------------------------------------------------------------------
# render_plan 不崩
# ---------------------------------------------------------------------------
def test_render_plan_marks_auxiliary(capsys):
    cfg = bf.MailBoxConfig(name="box", imap_host="h", username="u@e.com", last_uid=1000)
    mail = _mail(900, 2)
    results = [_probe_result("OK"), _probe_result("OK", aux=True)]
    bf.render_plan([(mail, results)], cfg, apply=False, skipped_no_att=16, probes=[{"uid": 900, "recognizable_invoices": 1}])
    out = capsys.readouterr().out
    assert "辅助凭证" in out
    assert "16 封" in out
    assert "1 张待入库发票" in out


def test_render_plan_empty_selection(capsys):
    cfg = bf.MailBoxConfig(name="box", imap_host="h", username="u@e.com")
    bf.render_plan([], cfg, apply=False)
    assert "没有可补收" in capsys.readouterr().out
