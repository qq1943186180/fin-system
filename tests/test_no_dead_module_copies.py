# -*- coding: utf-8 -*-
"""守卫：modules 包不许再出现「零调用但会被改错」的死副本。

背景（2026-10-14 审计 + 清理）：``modules/ocr_invoice.py`` 是一份 **113 行的死副本** ——
全项目零调用（只在 ``__all__`` 里出现过），却随 spec datas 一起打包，
而且与生产实际使用的**外部**脚本 ``C:/Users/li1/invoice-agent/ocr_invoice.py``
**已经漂移**（外部版多数电票「乱序兜底」正则，内部版没有）。

留着它的真实危害不是体积，而是**误导**：下一个看到"识别逻辑在哪"的人会去改
``modules/ocr_invoice.py``，改完发现完全不生效 —— 生产走的是外部脚本。

同理：``__all__`` 里漏登新模块（money/einvoice/...）也会让人以为"没有这个模块"。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODULES_DIR = ROOT / "modules"


def _project_py_files():
    for d in ("db", "modules", "gui", "tools"):
        yield from (ROOT / d).rglob("*.py")
    for name in ("run_finance.py", "app_paths.py", "logging_setup.py"):
        p = ROOT / name
        if p.exists():
            yield p


def _referenced_names():
    """全项目被引用过的标识符（含字符串形式，覆盖 getattr/反射）。"""
    text = "\n".join(p.read_text(encoding="utf-8") for p in _project_py_files())
    names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    names |= set(re.findall(r"\.\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", text))
    names |= set(re.findall(r"[\"']([A-Za-z_][A-Za-z0-9_]*)[\"']", text))
    # import 形式
    names |= set(re.findall(r"^\s*(?:from|import)\s+modules\.([a-z_]+)", text, re.M))
    names |= set(re.findall(r"^\s*from modules import ([a-z_]+)", text, re.M))
    return names


def test_every_module_file_is_referenced_somewhere():
    """modules/*.py 里不该有"没人引用"的文件。

    死代码本身不是罪，但**零调用却会被打包**的副本是哑弹：
    它会让人改错文件，且改了完全不生效（生产走外部脚本）。
    """
    referenced = _referenced_names()
    orphans = []
    for path in sorted(MODULES_DIR.glob("*.py")):
        stem = path.stem
        if stem == "__init__":
            continue
        if stem not in referenced:
            orphans.append(path.name)
    assert not orphans, (
        "这些模块文件在全项目零引用：%s。\n"
        "要么是真的死代码（删掉，别让下一个人改错文件），\n"
        "要么是被漏掉的依赖（补上引用）。" % orphans
    )


def test_all_matches_actual_module_files():
    """``__all__`` 必须覆盖全部模块文件 —— 漏登会让人以为"没这个模块"。"""
    init_text = (MODULES_DIR / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r"__all__\s*=\s*\[(.*?)\]", init_text, re.S)
    assert m, "modules/__init__.py 找不到 __all__"
    declared = set(re.findall(r"[\"']([a-z_]+)[\"']", m.group(1)))
    actual = {p.stem for p in MODULES_DIR.glob("*.py") if p.stem != "__init__"}

    missing = sorted(actual - declared)
    extra = sorted(declared - actual)
    assert not missing, "__all__ 漏登了这些模块（会被误以为不存在）：%s" % missing
    assert not extra, "__all__ 登记了不存在的模块（死条目）：%s" % extra


def test_dead_ocr_copy_is_gone():
    """🔴 ocr_invoice.py 内部死副本不许复活。

    生产 OCR 走**外部**脚本（settings.json 的 invoice_agent_command 指向
    ``C:/Users/li1/invoice-agent/ocr_invoice.py``），内部这份从未被调用，
    且已与外部版漂移。重新添加它等于重新埋"改错文件"的坑。
    """
    assert not (MODULES_DIR / "ocr_invoice.py").exists(), (
        "modules/ocr_invoice.py 又回来了 —— 它是零调用死副本，"
        "且与生产实际使用的外部脚本已漂移。改识别逻辑请改外部那份。"
    )


def test_production_ocr_command_points_external(tmp_path, monkeypatch):
    """生产 invoice_agent_command 必须指向**外部**脚本，不是包内副本。

    这条是上面那条的根因守卫：如果哪天配置被改成包内路径，
    说明又有人在按"内部副本"理解了。
    """
    import json
    import os

    settings = os.path.join(os.environ["LOCALAPPDATA"], "FinanceSystem", "config", "settings.json")
    if not os.path.exists(settings):
        pytest.skip("本机无生产 settings.json（非生产机器）")
    data = json.loads(Path(settings).read_text(encoding="utf-8"))
    cmd = data.get("invoice_agent_command") or []
    joined = " ".join(str(c) for c in cmd)
    assert "fin-system" not in joined, (
        "生产 invoice_agent_command 指向了包内副本：%s。"
        "生产 OCR 必须走外部脚本 C:/Users/li1/invoice-agent/ocr_invoice.py" % cmd
    )
