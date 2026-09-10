# -*- coding: utf-8 -*-
"""构建配置一致性：依赖必须在三处保持同步，否则冻结 exe 会静默缺库。

背景（2026-09-10 加固）：

新增三方依赖必须同时改 **三处**：
  ① ``pyproject.toml`` 的 ``[project].dependencies``
  ② ``build.ps1`` 第 [2/6] 步的 ``$RuntimePackages`` 安装列表
  ③ ``财务系统.spec`` 的 ``hiddenimports``

``build.ps1`` 不读 ``pyproject.toml``（第 ② 步是显式列表），而 PyInstaller 对
缺失的 hiddenimport **只发 WARNING 不报错** —— 于是构建「成功」，但 exe 点开
该功能才崩（已实际踩过：报表导出用的 ``openpyxl`` / ``fpdf2``）。

现在有两道门禁：
  - ``build.ps1`` 的 ``Assert-DependencyParity``：构建期，缺任一即中止（会真装包并 import）。
  - 本文件：静态解析三处清单，秒级报红，**不必跑一次完整构建**。

为什么两者都要：构建门禁只有跑构建时才生效（数分钟 + 需要网络），
日常 ``pytest`` 需要一个更快的等价格检查，否则漂移会在下一次打包才暴露。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
BUILD_SCRIPT = PROJECT_ROOT / "build.ps1"
SPEC_FILE = PROJECT_ROOT / "财务系统.spec"

# pip 发行名 → import 名。必须与 build.ps1 的 $script:DependencyImportNames 同步。
IMPORT_NAME_OVERRIDES = {"fpdf2": "fpdf"}


def _to_import_name(distribution: str) -> str:
    """与 build.ps1 的 ConvertTo-ImportName 保持同一套规则。"""
    bare = re.split(r"[<>=!~;\[]", distribution)[0].strip().lower()
    if not bare:
        raise ValueError("依赖项没有包名: %r" % distribution)
    return IMPORT_NAME_OVERRIDES.get(bare, bare.replace("-", "_").replace(".", "_"))


def _declared_dependencies() -> list[str]:
    """pyproject.toml 的 [project].dependencies 原始条目。"""
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["project"]["dependencies"]


def _script_packages() -> list[str]:
    """build.ps1 中 $RuntimePackages = @(...) 里的包名。"""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"\$RuntimePackages\s*=\s*@\(([^)]*)\)", text)
    assert block, "build.ps1 里找不到 $RuntimePackages 安装列表"
    return re.findall(r'"([^"]+)"', block.group(1))


def _spec_hidden_imports() -> set[str]:
    text = SPEC_FILE.read_text(encoding="utf-8")
    block = re.search(r"hiddenimports\s*=\s*\[(.*?)\]", text, re.S)
    assert block, "spec 里找不到 hiddenimports"
    return set(re.findall(r"['\"]([^'\"]+)['\"]", block.group(1)))


def _script_import_overrides() -> dict[str, str]:
    """解析 build.ps1 里的 $DependencyImportNames 映射表。"""
    text = BUILD_SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"\$script:DependencyImportNames\s*=\s*@\{(.*?)\}", text, re.S)
    assert block, "build.ps1 里找不到 $script:DependencyImportNames 映射表"
    return {
        key.lower(): value.lower()
        for key, value in re.findall(r'"([^"]+)"\s*=\s*"([^"]+)"', block.group(1))
    }


class TestThreeWayConsistency:
    """三处清单必须描述同一个依赖集合。"""

    def test_script_matches_pyproject(self) -> None:
        declared = sorted({_to_import_name(item) for item in _declared_dependencies()})
        installed = sorted({_to_import_name(item) for item in _script_packages()})
        assert installed == declared, (
            "build.ps1 的安装列表与 pyproject.toml 的 dependencies 不一致：\n"
            "  pyproject: %s\n  build.ps1: %s" % (declared, installed)
        )

    def test_every_dependency_is_in_spec_hiddenimports(self) -> None:
        hidden = _spec_hidden_imports()
        missing = [
            name
            for name in sorted({_to_import_name(i) for i in _declared_dependencies()})
            if name not in hidden
        ]
        assert not missing, (
            "以下依赖未出现在 财务系统.spec 的 hiddenimports 里，"
            "冻结 exe 会静默缺库（PyInstaller 只发 WARNING）：%s" % missing
        )

    def test_import_name_mapping_is_shared(self) -> None:
        """包名与 import 名不同时，两边必须用同一套映射（fpdf2 → fpdf）。"""
        assert _script_import_overrides() == IMPORT_NAME_OVERRIDES, (
            "build.ps1 的 $DependencyImportNames 与测试期望的映射不一致。"
            "两边必须同步，否则 ①校验三处一致 与 ②真正 import 的会是不同名字。"
        )

    def test_fpdf2_maps_to_fpdf_specifically(self) -> None:
        """把最容易踩的那个说清楚：pip 装 fpdf2，代码里 import 的是 fpdf。"""
        assert _to_import_name("fpdf2") == "fpdf"
        assert "fpdf" in _spec_hidden_imports()
        assert "fpdf2" not in _spec_hidden_imports(), (
            "hiddenimports 里不能写 pip 发行名 fpdf2 —— PyInstaller 要的是 import 名 fpdf"
        )


class TestBuildScriptConstraints:
    """build.ps1 自身的硬约束。"""

    def test_build_script_is_pure_ascii(self) -> None:
        """脚本头部声明必须保持 ASCII。

        Windows PowerShell 5.1 会把**无 BOM 的 UTF-8** 源文件按 ANSI 解码，
        中文注释会变成乱码并可能吃掉引号导致语法错误。往 build.ps1 写注释
        一律用英文。
        """
        raw = BUILD_SCRIPT.read_bytes()
        offenders = [
            (index, byte)
            for index, byte in enumerate(raw)
            if byte > 0x7F
        ]
        assert not offenders, (
            "build.ps1 含非 ASCII 字节（首个位于偏移 %d）。"
            "该脚本必须保持纯 ASCII，注释请用英文。" % (offenders[0][0] if offenders else -1)
        )

    def test_python_requirement_matches_build_resolver(self) -> None:
        """pyproject 的 requires-python 必须允许 3.11（build.ps1 硬性要求 3.11）。"""
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        requires = data["project"]["requires-python"]
        assert "3.11" in requires, (
            "build.ps1 的 Resolve-Python311 只接受 3.11，"
            "但 pyproject.toml 的 requires-python 是 %r" % requires
        )


class TestParityGateIsWiredIntoBuild:
    """门禁必须真的接在构建流程里，而不只是定义了函数。"""

    def test_parity_gate_is_invoked(self) -> None:
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        assert "Assert-DependencyParity" in text, "build.ps1 未定义依赖一致性门禁"
        assert re.search(
            r"Assert-DependencyParity\s+-BuildPython", text
        ), "build.ps1 定义了 Assert-DependencyParity 却没有调用它"

    def test_gate_runs_after_install_and_before_pyinstaller(self) -> None:
        """顺序要求：装完依赖才能 import 校验；必须在 PyInstaller 之前失败。"""
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        install_at = text.index("pip install --disable-pip-version-check --no-input @RuntimePackages")
        gate_at = text.index("Assert-DependencyParity -BuildPython")
        pyinstaller_at = text.index("PyInstaller --noconfirm --clean")
        assert install_at < gate_at < pyinstaller_at, (
            "顺序应为：安装依赖 → 一致性门禁 → PyInstaller（实测 %d / %d / %d）"
            % (install_at, gate_at, pyinstaller_at)
        )
