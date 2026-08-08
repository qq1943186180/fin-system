# -*- coding: utf-8 -*-
"""进销存页面：商品管理、入库/出库/期初、库存现状与明细。"""
from __future__ import annotations

from tkinter import ttk, messagebox, Frame, Label, StringVar, Toplevel
import tkinter as tk

from modules import inventory
from gui.ui_theme import (
    C_BG, C_CARD, C_BORDER, C_TEXT, C_TEXT_MUTED, C_ACCENT, C_OK, C_DANGER,
    FONT, FONT_SMALL, FONT_B, FONT_H2, FONT_H3,
    CardFrame, ModernButton, configure_dark_style,
)

DOC_LABELS = {"purchase_in": "采购入库", "sale_out": "销售出库", "initial": "期初建账"}


def _money(cents: object) -> str:
    try:
        return f"{int(cents or 0) / 100:,.2f}"
    except (TypeError, ValueError):
        return "0.00"


class InventoryPage(Frame):
    REF = "inventory"

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
        top.grid_columnconfigure(6, weight=1)
        Label(top, text="📦 进销存", font=FONT_H2, bg=C_BG, fg=C_TEXT).grid(row=0, column=0, sticky="w")
        ModernButton(top, text="+ 商品", variant="ghost", font=FONT_SMALL, height=28,
                     command=self._add_product).grid(row=0, column=1, padx=4)
        ModernButton(top, text="+ 采购入库", variant="primary", font=FONT_SMALL, height=28,
                     command=lambda: self._add_doc("purchase_in")).grid(row=0, column=2, padx=4)
        ModernButton(top, text="+ 销售出库", variant="primary", font=FONT_SMALL, height=28,
                     command=lambda: self._add_doc("sale_out")).grid(row=0, column=3, padx=4)
        ModernButton(top, text="期初建账", variant="ghost", font=FONT_SMALL, height=28,
                     command=lambda: self._add_doc("initial")).grid(row=0, column=4, padx=4)
        ModernButton(top, text="刷新", variant="ghost", font=FONT_SMALL, height=28,
                     command=self._refresh).grid(row=0, column=5, padx=4)

        hint = Label(top, text="入库/出库自动生成凭证并入账，毛利可在报表体现",
                     font=FONT_SMALL, bg=C_BG, fg=C_TEXT_MUTED)
        hint.grid(row=1, column=0, columnspan=7, sticky="w", pady=(2, 0))

        notebook = ttk.Notebook(self)
        notebook.grid(row=2, column=0, sticky="nsew", padx=20, pady=(6, 12))
        stock_tab, doc_tab, prod_tab = [Frame(notebook, bg=C_BG) for _ in range(3)]
        notebook.add(stock_tab, text="库存现状")
        notebook.add(doc_tab, text="库存单据")
        notebook.add(prod_tab, text="商品管理")
        self.notebook = notebook

        self.stock_tree = self._tree(stock_tab, (
            ("name", "商品", 200, "w"), ("sku", "编号", 90, "w"), ("spec", "规格", 110, "w"),
            ("unit", "单位", 60, "c"), ("qty", "数量", 90, "e"),
            ("avg", "加权均价", 110, "e"), ("amount", "库存金额", 130, "e"),
        ), height=16)
        self.stock_tree.bind("<Double-1>", lambda _e: self._show_ledger())

        self.doc_tree = self._tree(doc_tab, (
            ("doc_no", "单据号", 140, "w"), ("type", "类型", 100, "c"), ("date", "日期", 100, "c"),
            ("summary", "摘要", 260, "w"), ("lines", "行数", 60, "c"), ("voucher", "凭证", 120, "w"),
        ), height=16)

        self.prod_tree = self._tree(prod_tab, (
            ("name", "商品", 200, "w"), ("sku", "编号", 90, "w"), ("spec", "规格", 110, "w"),
            ("unit", "单位", 60, "c"), ("category", "分类", 120, "w"),
        ), height=16)

        self.ledger_win = None

    def _tree(self, parent, cols, height=10):
        tree = ttk.Treeview(parent, columns=[c[0] for c in cols], show="headings", height=height)
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        for key, txt, w, anchor in cols:
            tree.heading(key, text=txt)
            tree.column(key, width=w, anchor=anchor)
        return tree

    def _refresh(self):
        for row in self.stock_tree.get_children():
            self.stock_tree.delete(row)
        for b in inventory.get_stock_balance():
            self.stock_tree.insert("", "end", values=(
                b.get("name"), b.get("sku") or "", b.get("spec") or "", b.get("unit") or "",
                f"{float(b.get('qty') or 0):.2f}",
                _money(b.get("avg_cost_cents")),
                _money(b.get("amount_cents")),
            ))
        for row in self.doc_tree.get_children():
            self.doc_tree.delete(row)
        for d in inventory.list_stock_docs():
            self.doc_tree.insert("", "end", values=(
                d.get("doc_no"), DOC_LABELS.get(d.get("doc_type"), d.get("doc_type")),
                d.get("date"), d.get("summary") or "", d.get("lines"),
                d.get("vno") if "voucher_id" in d and d.get("voucher_id") else "—",
            ))
        for row in self.prod_tree.get_children():
            self.prod_tree.delete(row)
        for p in inventory.list_products():
            self.prod_tree.insert("", "end", values=(
                p.get("name"), p.get("sku") or "", p.get("spec") or "",
                p.get("unit") or "", p.get("category") or "",
            ))

    # ---------------- 商品 ----------------
    def _add_product(self):
        dlg = Toplevel(self.winfo_toplevel())
        dlg.title("新增商品")
        dlg.configure(bg=C_BG)
        dlg.geometry("420x300")
        fields = {}
        rows = [("名称*", "name"), ("编号(SKU)", "sku"), ("规格", "spec"), ("单位", "unit"), ("分类", "category")]
        for i, (label, key) in enumerate(rows):
            Label(dlg, text=label, bg=C_BG, fg=C_TEXT, font=FONT).grid(row=i, column=0, sticky="w", padx=16, pady=6)
            e = tk.Entry(dlg, width=28, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT,
                         highlightthickness=1, highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
            e.grid(row=i, column=1, padx=8, pady=6)
            if key == "unit":
                e.insert(0, "个")
            fields[key] = e

        def save():
            ok, msg, _pid = inventory.add_product(
                fields["name"].get().strip(),
                sku=fields["sku"].get().strip() or None,
                spec=fields["spec"].get().strip(),
                unit=fields["unit"].get().strip() or "个",
                category=fields["category"].get().strip(),
            )
            if not ok:
                messagebox.showerror("添加失败", msg, parent=dlg)
                return
            dlg.destroy()
            self._refresh()

        ModernButton(dlg, text="保存", variant="primary", font=FONT_SMALL, height=30, command=save
                     ).grid(row=len(rows), column=1, sticky="e", padx=8, pady=12)
        fields["name"].focus_set()

    # ---------------- 单据 ----------------
    def _add_doc(self, doc_type: str):
        dlg = Toplevel(self.winfo_toplevel())
        dlg.title(DOC_LABELS.get(doc_type, doc_type))
        dlg.configure(bg=C_BG)
        dlg.geometry("640x460")

        Label(dlg, text="日期 (YYYY-MM-DD)", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=0, column=0, sticky="w", padx=16, pady=6)
        date_var = StringVar(value=__import__("datetime").date.today().isoformat())
        tk.Entry(dlg, textvariable=date_var, width=14, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT,
                 highlightthickness=1, highlightbackground=C_BORDER).grid(row=0, column=1, padx=6, pady=6)

        Label(dlg, text="往来方(可选)", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=0, column=2, sticky="w", padx=10)
        partners = self._load_partners()
        partner_var = StringVar()
        partner_cb = ttk.Combobox(dlg, textvariable=partner_var, width=22, state="readonly",
                                  values=[""] + [f"{p['id']}:{p['name']}" for p in partners])
        partner_cb.grid(row=0, column=3, padx=6, pady=6)

        Label(dlg, text="摘要", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=1, column=0, sticky="w", padx=16, pady=6)
        summary_var = StringVar()
        tk.Entry(dlg, textvariable=summary_var, width=40, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT,
                 highlightthickness=1, highlightbackground=C_BORDER).grid(row=1, column=1, columnspan=3, padx=6, pady=6, sticky="ew")

        # 行表
        line_frame = Frame(dlg, bg=C_BG)
        line_frame.grid(row=2, column=0, columnspan=4, sticky="nsew", padx=12, pady=6)
        line_frame.grid_columnconfigure(0, weight=1)
        line_tree = ttk.Treeview(line_frame, columns=("product", "qty", "price", "sale"), show="headings", height=8)
        for col, txt, w in (("product", "商品", 200), ("qty", "数量", 80), ("price", "单价(元)", 100), ("sale", "售价(元)", 100)):
            line_tree.heading(col, text=txt)
            line_tree.column(col, width=w)
        line_tree.pack(fill="both", expand=True)

        line_rows: list[dict] = []

        def add_line():
            pdlg = Toplevel(dlg)
            pdlg.title("添加行")
            pdlg.configure(bg=C_BG)
            pdlg.geometry("420x220")
            prods = inventory.list_products()
            if not prods:
                messagebox.showerror("无商品", "请先添加商品", parent=dlg)
                pdlg.destroy()
                return
            Label(pdlg, text="商品", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=0, column=0, padx=12, pady=6)
            pvar = StringVar(value=f"{prods[0]['id']}:{prods[0]['name']}")
            ttk.Combobox(pdlg, textvariable=pvar, width=24, state="readonly",
                         values=[f"{p['id']}:{p['name']}" for p in prods]).grid(row=0, column=1, padx=6, pady=6)
            Label(pdlg, text="数量", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=1, column=0, padx=12, pady=6)
            qe = tk.Entry(pdlg, width=12, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT)
            qe.grid(row=1, column=1, padx=6, pady=6)
            Label(pdlg, text="单价(元)", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=2, column=0, padx=12, pady=6)
            pe = tk.Entry(pdlg, width=12, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT)
            pe.grid(row=2, column=1, padx=6, pady=6)
            Label(pdlg, text="售价(元,出库填)", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=3, column=0, padx=12, pady=6)
            se = tk.Entry(pdlg, width=12, bg=C_CARD, fg=C_TEXT, insertbackground=C_TEXT)
            se.grid(row=3, column=1, padx=6, pady=6)

            def ok_line():
                try:
                    pid = int(str(pvar.get()).split(":", 1)[0])
                    qty = float(qe.get())
                    price = float(pe.get() or 0)
                    sale = float(se.get() or 0)
                except ValueError:
                    messagebox.showerror("输入错误", "数量/单价/售价必须为数字", parent=pdlg)
                    return
                pname = str(pvar.get()).split(":", 1)[1] if ":" in str(pvar.get()) else str(pvar.get())
                line_rows.append({"product_id": pid, "name": pname, "qty": qty,
                                  "unit_price_cents": int(round(price * 100)),
                                  "sale_price_cents": int(round(sale * 100))})
                line_tree.insert("", "end", values=(pname, qty, f"{price:.2f}", f"{sale:.2f}"))
                pdlg.destroy()

            ModernButton(pdlg, text="确定", variant="primary", font=FONT_SMALL, height=28, command=ok_line
                         ).grid(row=4, column=1, sticky="e", padx=6, pady=10)

        ModernButton(dlg, text="+ 添加行", variant="ghost", font=FONT_SMALL, height=28, command=add_line
                     ).grid(row=3, column=0, columnspan=4, padx=12, pady=4, sticky="w")

        def submit():
            if not line_rows:
                messagebox.showerror("无明细", "请至少添加一行商品", parent=dlg)
                return
            partner_id = None
            if partner_var.get():
                try:
                    partner_id = int(str(partner_var.get()).split(":", 1)[0])
                except ValueError:
                    pass
            ok, msg, _did = inventory.create_stock_doc(
                doc_type, date_var.get().strip(), line_rows,
                creator=self.app.default_actor, partner_id=partner_id,
                summary=summary_var.get().strip(),
            )
            if not ok:
                messagebox.showerror("保存失败", msg, parent=dlg)
                return
            dlg.destroy()
            self._refresh()
            messagebox.showinfo("完成", msg, parent=self.winfo_toplevel())

        ModernButton(dlg, text="保存单据", variant="primary", font=FONT_SMALL, height=32, command=submit
                     ).grid(row=4, column=0, columnspan=4, padx=12, pady=10, sticky="e")

    def _load_partners(self):
        try:
            import db
            conn = db.get_conn()
            try:
                return [dict(r) for r in conn.execute("SELECT id,name FROM partner ORDER BY name")]
            finally:
                conn.close()
        except Exception:
            return []

    def _show_ledger(self):
        sel = self.stock_tree.selection()
        if not sel:
            return
        name = self.stock_tree.item(sel[0], "values")[0]
        prods = [p for p in inventory.list_products() if p["name"] == name]
        if not prods:
            return
        pid = prods[0]["id"]
        win = Toplevel(self.winfo_toplevel())
        win.title(f"库存明细 - {name}")
        win.configure(bg=C_BG)
        win.geometry("560x360")
        tree = ttk.Treeview(win, columns=("date", "type", "qty", "price", "amount"), show="headings", height=14)
        for col, txt, w in (("date", "日期", 100), ("type", "类型", 100), ("qty", "数量", 80), ("price", "单价", 100), ("amount", "金额", 120)):
            tree.heading(col, text=txt)
            tree.column(col, width=w)
        tree.pack(fill="both", expand=True, padx=12, pady=12)
        for l in inventory.stock_ledger(pid):
            tree.insert("", "end", values=(
                l.get("date"), DOC_LABELS.get(l.get("doc_type"), l.get("doc_type")),
                f"{float(l.get('qty') or 0):.2f}",
                _money(l.get("unit_price_cents")),
                _money(l.get("amount_cents")),
            ))
