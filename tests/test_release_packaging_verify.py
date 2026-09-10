# -*- coding: utf-8 -*-
"""release 打包回归验证器（tools/verify_release_packaging.py）的测试。

为什么专门测 `collect()`：它是整个验证器的判定核心，而它**已经错过两次**——
① 只扫嵌套 code object 的 `co_name`，漏掉 `co_names`（模块级名字）；
② 漏掉 `co_consts` 里的**字符串元组**（CPython 3.11 用元组存关键字参数名）。
两次都会把"已进包"误报成 MISSING，从而让验证形同虚设。

真实 exe 的端到端检查需要 pyinstaller 可 import，故用 importorskip 保护；
`collect()` 的逻辑本身用合成 code object 测，不依赖 pyinstaller。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = PROJECT_ROOT / "tools" / "verify_release_packaging.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("verify_release_packaging", TOOL_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vr = _load_tool()


def _make_sample():
    """构造代表性代码形态：关键字实参、闭包、局部变量、元组常量、嵌套函数。"""
    src = (
        "TOP_CONST = 'top-level-string'\n"
        "\n"
        "def outer(payload, source_file=None):\n"
        "    local_marker = 1\n"
        "    key = 'kwarg-key'\n"
        "    def inner():\n"
        "        return local_marker, key\n"
        "    result = register(payload, source_file=inner())\n"
        "    tup = (1, 2)\n"
        "    return result\n"
    )
    namespace: dict = {}
    code = compile(src, "<sample>", "exec")
    exec(code, namespace)  # noqa: S102 - 测试内部合成代码
    return code


def test_collect_finds_module_level_names_and_consts() -> None:
    syms = vr.collect(_make_sample(), set())

    assert "TOP_CONST" in syms          # co_names（模块级 STORE_NAME）
    assert "top-level-string" in syms   # co_consts 里的字符串
    assert "outer" in syms              # 嵌套 code object 的 co_name


def test_collect_finds_function_scoped_forms() -> None:
    """形参名 / 局部名 / 关键字参数名 / 闭包名 —— 四种都在不同的表里。"""
    syms = vr.collect(_make_sample(), set())

    assert "payload" in syms            # co_varnames（形参）
    assert "local_marker" in syms       # co_varnames（局部）
    assert "source_file" in syms        # co_consts 的**字符串元组**（关键字实参名）
    assert "key" in syms
    assert "inner" in syms              # 嵌套函数
    assert "register" in syms           # co_names（全局函数调用）


def test_collect_recurses_into_nested_code_objects() -> None:
    syms = vr.collect(_make_sample(), set())

    assert "inner" in syms
    # inner 里引用的外层局部变量（以 freevar 形式出现）
    assert "local_marker" in syms


def test_collect_accepts_preexisting_set() -> None:
    seed = {"already-here"}
    syms = vr.collect(_make_sample(), seed)

    assert "already-here" in syms
    assert "TOP_CONST" in syms


def test_targets_include_sentinel_symbols_from_p6() -> None:
    """回归本意：目标清单必须盯着 P6 引入的符号，别被后人误删。"""
    assert "_archive_invoice_file" in vr.TARGETS["modules.invoice_gui"]
    assert "INVOICES_ARCHIVE_DIR" in vr.TARGETS["app_paths"]
    assert "source_file" in vr.TARGETS["modules.mail_processor"]
    # 每个模块都要有"对照符号"，否则模块漏打包会被误判成符号缺失
    for module, wanted in vr.TARGETS.items():
        assert len(wanted) >= 2, module


def test_main_reports_missing_exe(tmp_path: Path, capsys) -> None:
    code = vr.main([str(tmp_path / "nope.exe"), str(tmp_path / "tmp")])

    assert code == 2
    assert "exe 不存在" in capsys.readouterr().err


@pytest.mark.skipif(
    importlib.util.find_spec("PyInstaller") is None,
    reason="需要 pyinstaller 才能解包校验真实 exe",
)
def test_real_release_bundle_contains_p6_symbols(tmp_path: Path) -> None:
    exe = PROJECT_ROOT / "release" / "财务系统" / "财务系统.exe"
    if not exe.is_file():
        pytest.skip("release 尚未打包")

    missing = vr.check(exe, tmp_path / "pyz", out=lambda *a: None)

    assert missing == []
