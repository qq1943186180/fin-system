# -*- coding: utf-8 -*-
"""验收：逐页构建 GUI + 新模块核心功能（隔离数据根，不碰生产库）。"""
import os, sys, tempfile, traceback
from pathlib import Path

# 隔离数据根（必须在任何项目模块 import 之前）
_isolated = Path(tempfile.mkdtemp(prefix="fin-accept-"))
os.environ["FINANCE_SYSTEM_HOME"] = str(_isolated)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tkinter as tk

def main():
    results = []

    # ---------- 1. GUI 逐页构建 ----------
    try:
        import db
        db.init_db()
        import gui.main as m
        root = tk.Tk()
        root.withdraw()  # 不显示窗口
        app = m.FinanceApp(root)
        builders = getattr(app, "NAV_ITEMS", ())
        page_map = {
            "工作台": "_page_dashboard", "账务": "_page_ledger", "资金": "_page_cash",
            "往来": "_page_partners", "费用票据": "_page_expense_invoice",
            "邮件收票": None, "进销存": None, "报表": "_page_reports",
            "期末处理": None, "设置": "_page_settings",
        }
        # 用 _show_page 逐页触发
        for name in builders:
            try:
                app._show_page(name)
                root.update_idletasks()
                results.append((f"GUI页:{name}", "OK", ""))
            except Exception as e:
                results.append((f"GUI页:{name}", "FAIL", f"{type(e).__name__}: {e}"))
        root.destroy()
    except Exception as e:
        results.append(("GUI初始化", "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    # ---------- 2. 新模块：期末结转 ----------
    try:
        import db
        from modules import closing
        st = closing.get_closing_status("2026-12")
        results.append(("closing.get_closing_status", "OK", str(st)[:120]))
    except Exception as e:
        results.append(("closing.get_closing_status", "FAIL", f"{type(e).__name__}: {e}"))

    # ---------- 3. 新模块：进销存 ----------
    try:
        from modules import inventory
        p = inventory.list_products()
        b = inventory.get_stock_balance()
        results.append(("inventory.list_products", "OK", f"{len(p)} 个商品"))
        results.append(("inventory.get_stock_balance", "OK", f"{len(b)} 行库存"))
    except Exception as e:
        results.append(("inventory.*", "FAIL", f"{type(e).__name__}: {e}"))

    # ---------- 4. 新模块：导出 ----------
    try:
        from modules import export_service
        out = _isolated / "out"
        out.mkdir(exist_ok=True)
        r1 = export_service.export_balance_sheet(out / "bs.xlsx", fmt="xlsx")
        r2 = export_service.export_vouchers(out / "vou.pdf", fmt="pdf")
        r3 = export_service.export_invoices(out / "inv.xlsx", fmt="xlsx")
        results.append(("export 资产负债表→xlsx", "OK" if r1.ok else "FAIL", f"{r1.message} | file={ (out/'bs.xlsx').exists() }"))
        results.append(("export 凭证→pdf", "OK" if r2.ok else "FAIL", f"{r2.message} | file={ (out/'vou.pdf').exists() }"))
        results.append(("export 发票→xlsx", "OK" if r3.ok else "FAIL", f"{r3.message} | file={ (out/'inv.xlsx').exists() }"))
    except Exception as e:
        results.append(("export.*", "FAIL", f"{type(e).__name__}: {e}\n{traceback.format_exc()}"))

    # ---------- 输出 ----------
    print("=" * 60)
    ok = sum(1 for _, s, _ in results if s == "OK")
    fail = sum(1 for _, s, _ in results if s == "FAIL")
    for name, status, detail in results:
        icon = "✅" if status == "OK" else "❌"
        print(f"{icon} {name}: {status}" + (f"  — {detail}" if detail else ""))
    print("=" * 60)
    print(f"合计：{ok} OK / {fail} FAIL")
    return 1 if fail else 0

if __name__ == "__main__":
    sys.exit(main())
