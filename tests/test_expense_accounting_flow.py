# -*- coding: utf-8 -*-
"""端到端：报销单「生成凭证」/「登记付款」真的能走通完整状态机。

背景（2026-10-14）：``expense.account_expense()`` / ``pay_expense()`` 写好了、
也有单测，但 **GUI 里零调用入口** —— 「飞书审批通过」之后状态机永远卡在
``approved``，走不到 ``accounted`` / ``paid``。本次在报销页补了两个按钮。

本文件用**真实 Tk + 真实服务层 + 隔离库**把整条链走完：
待审批 → 已审核 → 已记账 → 已付款，并核对凭证真的生成、借贷真的平衡。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="module")
def tk_root():
    tk = pytest.importorskip("tkinter")

    from gui import ui_theme

    root = None
    error: Exception | None = None
    for _ in range(3):
        try:
            root = tk.Tk()
            break
        except Exception as exc:  # noqa: BLE001
            error = exc
            time.sleep(0.3)
    if root is None:
        pytest.skip("当前环境无可用显示，跳过真实 Tk 冒烟：%s" % error)
    root.withdraw()
    try:
        ui_theme.configure_light_style(root)
        yield root
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """隔离数据根 + 最小科目表 + 一名员工。"""
    home = tmp_path / "home"
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(home))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import importlib

    import app_paths
    import db
    importlib.reload(app_paths)
    importlib.reload(db)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")

    from modules import expense as expense_mod
    from modules import ledger as ledger_mod
    importlib.reload(expense_mod)
    importlib.reload(ledger_mod)

    conn = db.get_conn()
    # 科目：费用 + 其他应付款 + 两种资金
    for code, name, cat, direction in [
        ("5602", "管理费用", "损益", "借"),
        ("2241", "其他应付款-员工", "负债", "贷"),
        ("1001", "库存现金", "资产", "借"),
        ("1002", "银行存款", "资产", "借"),
    ]:
        conn.execute(
            "INSERT OR REPLACE INTO account(code,name,category,direction,parent,is_leaf)"
            " VALUES(?,?,?,?,NULL,1)",
            (code, name, cat, direction),
        )
    conn.commit()
    conn.close()
    expense_mod.add_employee("李尚运", dept="技术部", phone="", note="")
    return db, expense_mod


def _find_buttons(widget, texts):
    import tkinter as tk
    from tkinter import ttk

    found = {}
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            children = w.winfo_children()
        except Exception:  # noqa: BLE001
            children = []
        stack.extend(children)
        if not isinstance(w, (tk.Button, ttk.Button)):
            continue
        label = str(w.cget("text"))
        for t in texts:
            if t in label:
                found.setdefault(t, w)
    return found


def _build_expense_tab(tk_root, gm):
    import tkinter as tk

    parent = tk.Frame(tk_root)
    host = gm.FinanceApp.__new__(gm.FinanceApp)
    host.root = tk_root
    host.default_actor = "测试会计"
    host._issues = []
    host._issue_check_failed = False
    host._active_nav = "费用票据"
    host.header_status = tk.Label(tk_root, text="")
    gm.FinanceApp._build_expense_tab(host, parent)
    tk_root.update_idletasks()
    return parent, host


def _tree_rows(parent):
    """返回 {单号: values}，从页面里的 Treeview 读。"""
    import tkinter as tk
    from tkinter import ttk

    stack = [parent]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, ttk.Treeview):
            return {str(w.item(i, "values")[1]): w.item(i, "values")
                    for i in w.get_children()}
    return {}


# ---------------------------------------------------------------- 按钮存在性
def test_expense_page_offers_account_and_pay_buttons(tk_root, env):
    """🔴 核心守卫：GUI 必须有这两个入口（曾经一个都没有）。"""
    import gui.main as gm

    parent, _host = _build_expense_tab(tk_root, gm)
    try:
        buttons = _find_buttons(parent, ("生成凭证", "登记付款"))
        assert "生成凭证" in buttons, "报销页缺少「生成凭证」按钮 —— account_expense 又没入口了"
        assert "登记付款" in buttons, "报销页缺少「登记付款」按钮 —— pay_expense 又没入口了"
    finally:
        parent.destroy()


# ---------------------------------------------------------------- 完整状态机
def test_full_state_machine_end_to_end(tk_root, env, monkeypatch):
    """待审批 → 已审核 → 已记账 → 已付款，凭证真的生成且借贷平衡。"""
    import gui.main as gm

    db, expense_mod = env
    from modules import ledger as ledger_mod

    pops: list = []
    monkeypatch.setattr(gm.messagebox, "showinfo", lambda *a, **k: pops.append(("info", a)))
    monkeypatch.setattr(gm.messagebox, "showwarning", lambda *a, **k: pops.append(("warn", a)))
    monkeypatch.setattr(gm.messagebox, "showerror", lambda *a, **k: pops.append(("error", a)))
    monkeypatch.setattr(gm.messagebox, "askyesno", lambda *a, **k: True)

    parent, host = _build_expense_tab(tk_root, gm)

    # 造一张待审批的报销单
    created = expense_mod.create_expense(
        date_str="2026-10-14",
        emp_id=1,
        summary="差旅费",
        items=[{"account_code": "5602", "summary": "高铁票", "amount_cents": 30000}],
        submitted_by="李尚运",
    )
    eid = created[2] if isinstance(created, tuple) else created
    if eid is None:
        pytest.skip("create_expense 返回结构不含 id，跳过端到端")

    try:
        # 1) 审核（approve 只改状态，不碰凭证）
        expense_mod.approve(eid, "张经理")
        # 2) 生成凭证
        ok, msg, vid = expense_mod.account_expense(eid, "测试会计")
        assert ok, "account_expense 失败：%s" % msg
        assert vid, "应返回凭证 ID"
        row = db.get_conn().execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
        # account_expense(submit=True) 生成的是**待审核凭证**，报销单同步到
        # accounting_pending；要到 accounted 必须凭证过账（账务页操作）。
        assert row["status"] == "accounting_pending", (
            "生成凭证并提交审核后应为 accounting_pending，实际 %s" % row["status"]
        )
        assert row["voucher_id"] == vid
        vrow = db.get_conn().execute("SELECT status FROM voucher WHERE id=?", (vid,)).fetchone()
        assert vrow["status"] == "pending_review", "凭证应处于 pending_review，实际 %s" % vrow["status"]

        conn = db.get_conn()
        lines = conn.execute(
            "SELECT account_code, debit_cents, credit_cents FROM voucher_line WHERE voucher_id=?",
            (vid,),
        ).fetchall()
        conn.close()
        debit = sum(int(l["debit_cents"]) for l in lines)
        credit = sum(int(l["credit_cents"]) for l in lines)
        assert debit == credit == 30000, "凭证应借贷平衡且等于票面金额，实际 借%d 贷%d" % (debit, credit)
        codes = {l["account_code"] for l in lines}
        assert codes == {"5602", "2241"}, "分录科目应为 5602 + 2241，实际 %s" % codes

        # 3) 凭证审核 + 过账 -> 报销单变 accounted
        #    状态机：pending_review -> audited -> posted（不能跳级）
        ok, msg = ledger_mod._transition(vid, "audited", "审核人")
        assert ok, "凭证审核失败：%s" % msg
        ok, msg = ledger_mod._transition(vid, "posted", "张会计")
        assert ok, "凭证过账失败：%s" % msg
        row = db.get_conn().execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
        assert row["status"] == "accounted", "凭证过账后报销单应为 accounted，实际 %s" % row["status"]

        # 4) 登记付款（只建付款凭据；凭据过账后报销单才变 paid）
        ok, msg, doc_id = expense_mod.pay_expense(eid, "2026-10-14", account_code="1002", actor="出纳")
        assert ok, "pay_expense 失败：%s" % msg
        row = db.get_conn().execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
        assert row["status"] == "accounted", "仅建凭据时不应直接变 paid，实际 %s" % row["status"]

        # 5) 付款凭据过账 -> paid
        #    注意：pay_expense 返回的是 cash_doc.id，不是 voucher.id
        pay_vid = db.get_conn().execute(
            "SELECT voucher_id FROM cash_doc WHERE id=?", (doc_id,)
        ).fetchone()["voucher_id"]
        assert pay_vid, "付款凭据应已关联凭证"
        ok, msg = ledger_mod._transition(pay_vid, "audited", "审核人")
        assert ok, "付款凭证审核失败：%s" % msg
        ok, msg = ledger_mod._transition(pay_vid, "posted", "张会计")
        assert ok, "付款凭据过账失败：%s" % msg
        row = db.get_conn().execute("SELECT * FROM expense WHERE id=?", (eid,)).fetchone()
        assert row["status"] == "paid", "付款凭据过账后应变为 paid，实际 %s" % row["status"]

        # 6) 幂等：重复记账 / 重复付款都不该炸
        ok2, msg2, vid2 = expense_mod.account_expense(eid, "测试会计")
        assert ok2 and vid2 == vid, "重复生成凭证应幂等返回同一凭证：%s" % msg2
        ok3, msg3, _ = expense_mod.pay_expense(eid, "2026-10-14", account_code="1002", actor="出纳")
        assert ok3, "重复付款应幂等：%s" % msg3

        # 5) 状态守卫：未审核不能记账
        created2 = expense_mod.create_expense(
            date_str="2026-10-14",
            emp_id=1,
            summary="办公用品",
            items=[{"account_code": "5602", "summary": "打印纸", "amount_cents": 5000}],
            submitted_by="李尚运",
        )
        eid2 = created2[2] if isinstance(created2, tuple) else created2
        bad = expense_mod.account_expense(eid2, "测试会计")
        assert not bad[0], "未审核的报销单不该允许生成凭证"

        # 6) 未记账不能付款
        bad2 = expense_mod.pay_expense(eid2, "2026-10-14", account_code="1002", actor="出纳")
        assert not bad2[0], "未记账的报销单不该允许登记付款"

        # 7) 生成凭证但**未过账**（accounting_pending）也不能付款
        #    —— 这条锁住真实状态机：付款必须等凭证过账到 accounted
        expense_mod.approve(eid2, "张经理")
        ok_a, _m, vid_a = expense_mod.account_expense(eid2, "测试会计")
        assert ok_a, "第二张单生成凭证应成功：%s" % _m
        row2 = db.get_conn().execute("SELECT status FROM expense WHERE id=?", (eid2,)).fetchone()
        assert row2["status"] == "accounting_pending"
        bad3 = expense_mod.pay_expense(eid2, "2026-10-14", account_code="1002", actor="出纳")
        assert not bad3[0], (
            "凭证还没过账（accounting_pending）不该允许付款 —— "
            "否则钱付了账还没记"
        )
    finally:
        parent.destroy()


def test_gui_buttons_respect_state_machine(tk_root, env, monkeypatch):
    """按钮回调自身要有状态守卫：选中「待审批」行点「生成凭证」应被拒。"""
    import gui.main as gm

    db, expense_mod = env
    warns: list = []
    monkeypatch.setattr(gm.messagebox, "showwarning", lambda *a, **k: warns.append(a))
    monkeypatch.setattr(gm.messagebox, "showinfo", lambda *a, **k: None)
    monkeypatch.setattr(gm.messagebox, "showerror", lambda *a, **k: warns.append(a))

    parent, host = _build_expense_tab(tk_root, gm)
    created = expense_mod.create_expense(
        emp_id=1,
        date_str="2026-10-14",
        summary="测试-待审批",
        items=[{"account_code": "5602", "summary": "x", "amount_cents": 1000}],
        submitted_by="李尚运",
    )
    eid = created[2] if isinstance(created, tuple) else created
    assert eid, "create_expense 应返回新建的 id，实际 %r" % (created,)
    try:
        buttons = _find_buttons(parent, ("生成凭证", "登记付款", "刷新"))
        # 不选任何行 → 应提示先选择
        buttons["生成凭证"].invoke()
        assert warns, "未选行时点「生成凭证」应给出提示"
        warns.clear()

        # 建单发生在页面构建之后，点一下「刷新」把新行加载进来
        buttons["刷新"].invoke()
        tk_root.update_idletasks()

        # 选中待审批的行 → 应提示状态不符
        import tkinter as tk
        from tkinter import ttk

        tree = None
        stack = [parent]
        while stack:
            w = stack.pop()
            stack.extend(w.winfo_children())
            if isinstance(w, ttk.Treeview):
                tree = w
                break
        assert tree is not None
        rows = _tree_rows(parent)
        assert str(eid) in {str(v[0]) for v in rows.values()} or any(
            str(v[0]) == str(eid) for v in rows.values()
        ), "新建的报销单应出现在列表里：%s" % rows
        for iid in tree.get_children():
            if str(tree.item(iid, "values")[0]) == str(eid):
                tree.selection_set(iid)
                break
        tk_root.update_idletasks()
        assert tree.selection(), "应能选中新建的那一行"

        buttons["生成凭证"].invoke()
        assert warns and any("状态不符" in str(a) for a in warns), (
            "待审批行点「生成凭证」应提示状态不符，实际弹了：%s" % warns
        )
        warns.clear()

        buttons["登记付款"].invoke()
        assert warns and any("状态不符" in str(a) for a in warns), (
            "待审批行点「登记付款」应提示状态不符"
        )
    finally:
        parent.destroy()
