#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""校验 release 里的 exe 是否真的包含指定源码改动（打包回归验证）。

## 为什么需要它

`grep <exe> <符号名>` **不可靠**：PyInstaller 把 Python 字节码压进
PKG(CArchive) → PYZ(ZlibArchive)，是 zlib 压缩的，任何 Python 级名字都 grep 不到
（实测：连早已存在的 `_BACKUP_NAME_RE` / `configure_light_style` 也是 0 次命中）。

正确做法是把 PYZ 解出来，取目标模块的 code object，扫描"名字痕迹"：
  * 嵌套 code object 的 `co_name`（函数/类名）
  * `co_names`（全局名、属性名）
  * `co_varnames` / `co_freevars` / `co_cellvars`（形参、局部、闭包）
  * `co_consts` 里的字符串 **以及字符串元组**（CPython 3.11 把关键字参数名
    放在 co_consts 的元组里 —— 漏掉元组会误报 MISSING）

## 用法

    python tools/verify_release_packaging.py <exe> <tmp_dir>

需要 `pyinstaller` 可 import（只为复用它的归档读取器）：

    <某 3.11 venv>/Scripts/python.exe -m pip install pyinstaller==6.21.0

退出码 0 = 全部命中；1 = 有缺失。
"""
from __future__ import annotations

import marshal
import sys
from pathlib import Path

# P6/P7 引入、且能唯一标识"新代码已进包"的符号。
# 每项都配上模块内既有符号作为对照，避免"模块没扫到"被误判成"符号缺失"。
TARGETS: dict[str, list[str]] = {
    "modules.invoice_gui": [
        "_archive_invoice_file",    # P6 核心：归档原件
        "_archive_month",           # P6：按开票月份分目录
        "_archive_enabled",         # P6：setting 开关读取
        "_safe_name",               # P6：文件名净化
        "invoice_archive_enabled",  # P6：settings key（字符串常量）
        "load_invoices",            # 对照：既有函数
    ],
    "app_paths": [
        "INVOICES_ARCHIVE_DIR",     # P6 新增常量
        "ensure_app_dirs",          # 对照
    ],
    "modules.mail_processor": [
        "_source_filename",         # P6：临时文件原名的传递键
        "source_file",             # P6 新增关键字实参
        "register_invoice",         # 对照
    ],
    "modules.einvoice": [
        "parse_einvoice_xml",        # 方案 B：数电票 XML 结构化解析
        "extract_ofd_invoice_xml",   # 方案 B：OFD 内嵌 XML 提取
        "parse_einvoice_attachment", # 方案 B：按后缀分派
        "TotalTax-includedAmount",   # 方案 B：顶层价税合计元素名常量
        "einvoice-ofd-xml",          # 方案 B：来源标记（字符串常量）
        "EinvoiceParseError",        # 对照：模块自定义异常
    ],
    "modules.feishu": [
        "effective_config",          # 飞书修复：合并后的生效配置（设置页据此初始化勾选框）
        "build_settings_section",    # 飞书修复：保存时由它组装 settings.json 的 feishu 段
        "allow_self_approval",       # 飞书修复：不得被硬写 False 的那个键
        "sync_approval_status",      # 飞书同步修复（2026-09-26）：状态字面量回归点
        "~pending_approval",         # 飞书同步修复：英文状态字面量，嵌在长 SQL 串里 → 子串匹配
        "validate_config",           # 对照：既有函数
    ],
    "db": [
        "_migration_6",              # 补 expense.instance_code 列（2026-09-26）
        "expense_feishu_instance_code",  # migration 6 的注册名（字符串常量）
        "_add_column",               # 对照：既有幂等 ALTER 助手
    ],
    "modules.feishu_pull": [
        "fetch_feishu_expenses",     # 从飞书拉取报销单（2026-09-26）
        "_parse_form",               # 解析飞书 form
        "_parse_detail_rows",        # 解析 fieldList 二维数组
        "_resolve_employee",         # 员工自动建档
        "_create_local_expense",     # 本地建单
        "expense_pulled_from_feishu",  # 审计事件名（字符串常量）
        "already_imported",          # 幂等跳过原因（字符串常量）
    ],
    "gui.main": [
        "feishu_allow_self_var",     # 飞书修复：勾选框变量名（已进 co_varnames）
        "Panel.TCheckbutton",        # 飞书修复：面板底色勾选框样式（字符串常量）
        "save_integrations",         # 对照：既有函数
    ],
}

ENTRY_TARGETS = ("self_check", "_write_self_check_report", "launch")


def collect(code, syms: set) -> set:
    """把 code object 里所有"名字痕迹"收进 syms（递归）。"""
    syms.add(code.co_name)
    for table in (code.co_names, code.co_varnames, code.co_freevars, code.co_cellvars):
        syms.update(str(x) for x in table)
    for const in code.co_consts:
        if isinstance(const, str):
            syms.add(const)
        elif isinstance(const, (tuple, frozenset)):
            # CPython 3.11 把关键字参数名放进 co_consts 的字符串元组里
            syms.update(str(x) for x in const if isinstance(x, str))
        elif hasattr(const, "co_name"):
            collect(const, syms)
    return syms


def module_symbols(zar, name: str) -> set | None:
    if name not in zar.toc:
        return None
    raw = zar.extract(name)
    code = raw if hasattr(raw, "co_name") else marshal.loads(raw)
    return collect(code, set())


def _hit(sym: str, syms: set) -> bool:
    """符号是否命中。

    默认精确匹配。符号前缀 ``~`` 表示**子串匹配**：用于那些天然嵌在更长
    字符串常量里的东西（例如拼在一条长 SQL 里的状态字面量
    ``"SELECT ... WHERE status IN ('pending_approval', ...)"``）。
    这类符号永远不可能作为独立常量出现，精确匹配会稳定假阴性。
    """
    if sym.startswith("~"):
        needle = sym[1:]
        return any(needle in s for s in syms)
    return sym in syms


def check(exe: Path, tmp_dir: Path, out=print) -> list[str]:
    """返回缺失符号列表（空 = 全部命中）。"""
    from PyInstaller.archive.readers import CArchiveReader, ZlibArchiveReader

    tmp_dir.mkdir(parents=True, exist_ok=True)
    out("exe: %s (%d bytes)" % (exe.name, exe.stat().st_size))

    car = CArchiveReader(str(exe))
    car_names = list(car.toc.keys())
    pyz_entry = next((n for n in car_names if n.endswith(".pyz")), None)
    if not pyz_entry:
        return ["<PYZ 条目未找到>"]

    pyz_path = tmp_dir / pyz_entry
    pyz_path.write_bytes(car.extract(pyz_entry))
    zar = ZlibArchiveReader(str(pyz_path))
    out("PYZ 模块数: %d" % len(zar.toc))

    missing: list[str] = []
    out("")
    out("=== 符号检查 ===")
    for module, wanted in TARGETS.items():
        syms = module_symbols(zar, module)
        if syms is None:
            out("[%s] 模块不在 PYZ 内!" % module)
            missing.extend("%s.*" % module for _ in wanted)
            continue
        out("[%s]" % module)
        for sym in wanted:
            found = _hit(sym, syms)
            out("    %-28s %s" % (sym, "FOUND" if found else "MISSING"))
            if not found:
                missing.append("%s.%s" % (module, sym))

    out("")
    out("=== 入口脚本（在 CArchive 内，不在 PYZ）===")
    entry_syms: set = set()
    for name in car_names:
        if name.endswith((".pyz", ".pyd", ".dll")):
            continue
        try:
            code = marshal.loads(car.extract(name))
        except Exception:
            continue
        if hasattr(code, "co_name"):
            collect(code, entry_syms)
    for sym in ENTRY_TARGETS:
        found = sym in entry_syms
        out("    %-28s %s" % (sym, "FOUND" if found else "MISSING"))
        if not found:
            missing.append("entry.%s" % sym)
    return missing


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2:
        print(__doc__)
        return 2
    exe, tmp_dir = Path(argv[0]), Path(argv[1])
    if not exe.is_file():
        print("exe 不存在：%s" % exe, file=sys.stderr)
        return 2

    missing = check(exe, tmp_dir)
    print()
    print("=== 结论 ===")
    if missing:
        print("SOME_SYMBOLS_MISSING: " + ", ".join(missing))
        return 1
    print("ALL_TARGET_SYMBOLS_PRESENT -> release 包已含目标改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
