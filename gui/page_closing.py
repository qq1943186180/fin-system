# -*- coding: utf-8 -*-
"""期末处理页：结转损益与年终结转。"""
from __future__ import annotations

import datetime as _dt
from tkinter import ttk, messagebox, Frame, Label
import tkinter as tk

from modules import closing
from gui.ui_theme import (
    C_BG, C_CARD, C_BORDER, C_TEXT, C_TEXT_MUTED, C_ACCENT, C_OK, C_DANGER,
    FONT, FONT_SMALL, FONT_B, FONT_H2, FONT_H3,
    CardFrame, ModernButton,
)


def _current_period() -> str:
    return _dt.date.today().strftime("%Y-%m")


class ClosingPage(Frame):
    REF = "closing"

    def __init__(self, master, app, **kw):
        kw.setdefault("bg", C_BG)
        super().__init__(master, **kw)
        self.app = app
        self._build_ui()
        self._refresh()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        top = Frame(self, bg=C_BG)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        top.grid_columnconfigure(1, weight=1)
        Label(top, text="🔚 期末处理", font=FONT_H2, bg=C_BG, fg=C_TEXT).grid(row=0, column=0, sticky="w")
        Label(top, text="结转损益 → 结账 → 年终结转，账才能真正做完", font=FONT_SMALL, bg=C_BG, fg=C_TEXT_MUTED).grid(row=1, column=0, sticky="w", pady=(2, 0))

        ctrl = Frame(self, bg=C_BG)
        ctrl.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 8))
        ctrl.grid_columnconfigure(2, weight=1)
        Label(ctrl, text="会计期间", bg=C_BG, font=FONT).grid(row=0, column=0, sticky="w")
        self.period_var = tk.StringVar(value=_current_period())
        self.period_entry = ttk.Entry(ctrl, textvariable=self.period_var, width=10)
        self.period_entry.grid(row=0, column=1, padx=6)
        ModernButton(ctrl, text="结转损益", variant="primary", font=FONT_SMALL, height=30,
                     command=self._on_carry).grid(row=0, column=2, padx=6, sticky="w")
        ModernButton(ctrl, text="年终结转（本期间所属年度）", variant="ghost", font=FONT_SMALL, height=30,
                     command=self._on_yearend).grid(row=0, column=3, padx=6)

        body = Frame(self, bg=C_BG)
        body.grid(row=2, column=0, sticky="nsew", padx=20, pady=(4, 12))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(1, weight=1)

        # 状态卡
        status_card = CardFrame(body)
        status_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        status_card.grid_columnconfigure(1, weight=1)
        Label(status_card, text="当前状态", font=FONT_B, bg=C_CARD, fg=C_TEXT).grid(row=0, column=0, sticky="w", padx=14, pady=10)
        self.status_label = Label(status_card, text="", font=FONT, bg=C_CARD, fg=C_TEXT, justify="left")
        self.status_label.grid(row=0, column=1, sticky="w", padx=10, pady=10)

        # 历史
        Label(body, text="结转凭证记录", font=FONT_H3, bg=C_BG, fg=C_TEXT).grid(row=1, column=0, sticky="w", pady=(6, 2))
        self.tree = ttk.Treeview(body, columns=("vno", "type", "period", "status", "date"),
                                 show="headings", height=10)
        for col, txt, w in (("vno", "凭证号", 130), ("type", "类型", 100), ("period", "期间/年度", 100), ("status", "状态", 90), ("date", "记账日期", 120)):
            self.tree.heading(col, text=txt)
            self.tree.column(col, width=w)
        self.tree.grid(row=2, column=0, sticky="nsew")

    def _refresh(self):
        period = self.period_var.get().strip()
        year = period[:4] if len(period) >= 4 else _dt.date.today().year
        try:
            status = closing.get_closing_status(period)
        except Exception:
            status = None
        if status:
            carry = status.get("carryover_vno")
            ye = status.get("yearend_vno")
            pl = status.get("current_profit_loss_cents", 0) / 100
            lines = [f"所选期间 {period}", f"本年利润（截至所选期间）余额：{pl:,.2f} 元"]
            lines.append(f"结转损益凭证：{carry or '—'}（{status.get('carryover_status') or '未执行'}）")
            lines.append(f"年终结转凭证（{year}）：{ye or '—'}（{status.get('yearend_status') or '未执行'}）")
            self.status_label.configure(text="\n".join(lines))
        else:
            self.status_label.configure(text=f"期间 {period} 状态读取失败")

        # 历史
        for row in self.tree.get_children():
            self.tree.delete(row)
        try:
            import db
            conn = db.get_conn()
            try:
                for r in conn.execute(
                    "SELECT vno,source,idempotency_key,status,date FROM voucher "
                    "WHERE source='closing' ORDER BY date DESC, vno DESC"
                ):
                    typ = "年终结转" if str(r["idempotency_key"] or "").startswith("yearend") else "结转损益"
                    per = str(r["idempotency_key"] or "").split(":", 1)[-1]
                    self.tree.insert("", "end", values=(r["vno"], typ, per, r["status"], r["date"]))
            finally:
                conn.close()
        except Exception:
            pass

    def _on_carry(self):
        period = self.period_var.get().strip()
        if not (len(period) == 7 and period[4] == "-"):
            messagebox.showerror("期间无效", "请输入 YYYY-MM 格式的会计期间", parent=self.winfo_toplevel())
            return
        ok, msg, _vid = closing.carry_forward_profit_loss(period, self.app.default_actor)
        if ok:
            messagebox.showinfo("结转完成", msg, parent=self.winfo_toplevel())
        else:
            messagebox.showerror("无法结转", msg, parent=self.winfo_toplevel())
        self._refresh()

    def _on_yearend(self):
        period = self.period_var.get().strip()
        year = period[:4] if len(period) >= 4 else _dt.date.today().year
        ok, msg, _vid = closing.close_fiscal_year(year, self.app.default_actor)
        if ok:
            messagebox.showinfo("年结完成", msg, parent=self.winfo_toplevel())
        else:
            messagebox.showerror("无法年结", msg, parent=self.winfo_toplevel())
        self._refresh()
