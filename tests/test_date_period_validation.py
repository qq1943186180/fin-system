# -*- coding: utf-8 -*-
"""守卫：日期/期间校验必须只有**一个**实现，且 5 个入口行为逐字节一致。

背景（2026-10-14 审计 + 收敛）：项目里有 4 份 ``_validate_period``
（gui/main 用正则，closing/ledger/reports 用 strptime + 回环）和 2 份
``_validate_date``，实测接受域不一致：

* ``0000-01`` —— GUI 正则版**放行**、strptime 版**拒绝**（第 0 年不是合法日期）；
* ``None`` 入参 —— GUI 版抛 **AttributeError**（``None.strip()``），
  其余抛 ``ValueError``；而调用方普遍只捕 ``ValueError`` → 会漏；
* ``20260701`` —— GUI 版静默归一成 ``2026-07-01``，ledger 版拒绝。

已收敛：gui/main、modules/closing、modules/reports 全部委托到
``modules.ledger`` 那两个函数。本测试锁住"5 个入口给同一答案"。
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PERIODS = [
    "2026-07", "2026-7", "2026-13", "2026-00", "0000-01", "9999-12",
    " 2026-07 ", "2026-07-01", "202607", "", None, "2026-1", "abcd-07",
]
DATES = [
    "2026-07-01", "20260701", "2026-7-1", "2026-13-01", "2026-02-30",
    " 2026-07-01 ", "", None, "2026-07-01T00:00:00", "0000-01-01",
]


def _load(name: str):
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def entries():
    """5 个入口：4 处 _validate_period + 2 处 _validate_date（去重后 5 个）。"""
    gm = _load("gui.main")
    cl = _load("modules.closing")
    lg = _load("modules.ledger")
    rp = _load("modules.reports")
    return {
        "period": {
            "gui.main": gm._validate_period,
            "closing": cl._validate_period,
            "ledger": lg._validate_period,
            "reports": rp._validate_period,
        },
        "date": {
            "gui.main": gm._validate_date,
            "ledger": lg._validate_date,
        },
    }


def _verdict(fn, value):
    try:
        return ("ok", fn(value))
    except Exception as exc:  # noqa: BLE001
        return ("raise", type(exc).__name__)


@pytest.mark.parametrize("value", PERIODS)
def test_all_period_entrypoints_agree(entries, value):
    got = {name: _verdict(fn, value) for name, fn in entries["period"].items()}
    uniq = set(got.values())
    assert len(uniq) == 1, (
        "_validate_period(%r) 四个入口判定不同：%s" % (value, got)
    )


@pytest.mark.parametrize("value", DATES)
def test_all_date_entrypoints_agree(entries, value):
    got = {name: _verdict(fn, value) for name, fn in entries["date"].items()}
    uniq = set(got.values())
    assert len(uniq) == 1, (
        "_validate_date(%r) 两个入口判定不同：%s" % (value, got)
    )


# ---------------------------------------------------------------- 规范行为
def test_none_input_raises_valueerror_not_attributeerror(entries):
    """🔴 None 入参必须抛 ValueError —— 调用方普遍只捕 ValueError。"""
    for name, fn in entries["period"].items():
        kind, exc = _verdict(fn, None)
        assert (kind, exc) == ("raise", "ValueError"), (
            "%s._validate_period(None) 抛的是 %s —— "
            "AttributeError 会绕过调用方的 except ValueError" % (name, exc)
        )
    for name, fn in entries["date"].items():
        kind, exc = _verdict(fn, None)
        assert (kind, exc) == ("raise", "ValueError"), (
            "%s._validate_date(None) 抛的是 %s" % (name, exc)
        )


def test_year_zero_period_rejected(entries):
    """🔴 0000-01 格式对但不是合法日期，必须拒。"""
    for name, fn in entries["period"].items():
        assert _verdict(fn, "0000-01")[0] == "raise", (
            "%s._validate_period 放行了 0000-01 —— 第 0 年不是合法日期" % name
        )


def test_non_canonical_forms_rejected(entries):
    """紧凑式/非补零形式一律拒绝：输入什么就还什么，不猜。"""
    assert _verdict(entries["date"]["ledger"], "20260701")[0] == "raise"
    assert _verdict(entries["period"]["ledger"], "2026-7")[0] == "raise"


def test_valid_values_pass_through_unchanged(entries):
    assert _verdict(entries["period"]["ledger"], "2026-07") == ("ok", "2026-07")
    assert _verdict(entries["period"]["ledger"], " 2026-07 ") == ("ok", "2026-07")
    assert _verdict(entries["date"]["ledger"], "2026-07-01") == ("ok", "2026-07-01")


# ---------------------------------------------------------------- 源码守卫
def test_only_one_implementation_remains():
    """除 ledger 外，其余文件不许再自带校验逻辑（必须是一行委托）。"""
    offenders = []
    for path in (ROOT / "gui" / "main.py", ROOT / "modules" / "closing.py",
                 ROOT / "modules" / "reports.py"):
        text = path.read_text(encoding="utf-8")
        for fname in ("_validate_period", "_validate_date"):
            start = text.find("def %s" % fname)
            if start == -1:
                continue
            end = text.find("\ndef ", start + 1)
            seg = text[start:end if end != -1 else start + 1500]
            seg = seg[: seg.find('"""', seg.find('"""') + 3) + 3] if seg.count('"""') >= 2 else seg
            body = seg.split(":", 1)[1] if ":" in seg else ""
            # 委托形如 "return ledger._validate_xxx(...)"
            if "ledger._validate" not in body:
                line = text[:start].count("\n") + 1
                offenders.append("%s:%d (%s)" % (path.name, line, fname))
    assert not offenders, (
        "这些位置又在自持日期/期间校验逻辑了：%s。\n"
        "唯一实现是 modules/ledger.py 的那两个函数，其余请一行委托。"
        % "、".join(offenders)
    )
