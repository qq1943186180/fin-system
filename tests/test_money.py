# -*- coding: utf-8 -*-
"""modules/money.py —— 全项目唯一的「金额 ↔ 整数分」实现。

2026-10-14 审计发现项目里有 9 份同类实现且实测 9/17 个边界用例不一致
（负号丢失、银行家舍入差 1 分、全角数字分歧、失败哨兵不统一）。
本测试锁住统一后的行为，并显式记录与旧实现的**有意差异**。
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules import money  # noqa: E402


# ---------------------------------------------------------------- 基本解析
@pytest.mark.parametrize("raw,expected", [
    ("2.12", 212),
    ("0.120000", 12),          # XML 税额常是 6 位小数
    ("1,234.56", 123456),      # 千分位
    ("¥30", 3000),
    ("￥30", 3000),
    ("30.999", 3100),
    ("1234.56元", 123456),
    ("壹佰圆", None),           # 大写金额不是数字文本，应失败
    (1234.56, 123456),
    (100, 10000),
    ("0", 0),
    ("0.00", 0),
])
def test_parse_cents_accepts_realistic_amounts(raw, expected):
    if expected is None:
        with pytest.raises(money.MoneyParseError):
            money.parse_cents(raw)
    else:
        assert money.parse_cents(raw) == expected


def test_parse_cents_rounds_half_up_not_bankers():
    """🔴 旧实现的银行家舍入会在这里差 1 分。"""
    assert money.parse_cents("0.125") == 13, "ROUND_HALF_UP 应得 13（round() 会给 12）"
    assert money.parse_cents("1.005") == 101, "ROUND_HALF_UP 应得 101（round() 会给 100）"
    assert money.parse_cents("2.675") == 268


def test_parse_cents_preserves_negative_sign():
    """🔴 旧 backfill 实现把 -5.00 变成 +500（isdigit 过滤丢负号）。"""
    assert money.parse_cents("-5.00") == -500
    assert money.parse_cents_or_none("-5.00") == -500
    assert money.parse_cents("-0.01") == -1


def test_parse_cents_uses_decimal_not_float():
    """float 二进制误差：1.005 在 float 下是 1.00499999..."""
    assert money.parse_cents(1.005) == 101
    assert money.parse_cents(Decimal("1.005")) == 101


# ---------------------------------------------------------------- 失败必须可区分
@pytest.mark.parametrize("raw", ["", None, "abc", "-", "1.2.3", "12ab", "  "])
def test_unparseable_values_rejected(raw):
    """无法解析的输入必须抛错，而不是静默变成 0 或猜一个值。

    "12ab" 刻意判失败：旧实现用正则擦掉所有非数字字符，会把它静默变成 1200。
    新实现只剥离已知噪音（千分位/货币符号/元 圆），剩下的必须能被 Decimal 解析。
    """
    with pytest.raises(money.MoneyParseError):
        money.parse_cents(raw)


def test_fullwidth_digits_are_accepted():
    """全角数字被 Decimal 原生接受（decimal 走 Unicode 数字规范化）。

    这与旧实现的**多数**一致（feishu_pull / fix_amounts / backfill 三份都吃），
    只有 einvoice 的正则版拒掉。OCR 出的中文金额常见全角，故取"接受"。
    """
    assert money.parse_cents("１２３") == 12300
    assert money.parse_cents("１２３.４５") == 12345


def test_three_exit_hatches():
    """同一解析失败的三种出口，语义互不相同。"""
    assert money.parse_cents_or_zero("abc") == 0
    assert money.parse_cents_or_none("abc") is None
    with pytest.raises(money.MoneyParseError):
        money.parse_cents("abc")
    # 三者对合法值必须一致
    assert money.parse_cents_or_zero("2.12") == money.parse_cents("2.12") == 212
    assert money.parse_cents_or_none("2.12") == 212


def test_bool_is_rejected():
    """bool 是 int 子类，True 会被当成 1 分 —— 必须显式拒绝。"""
    with pytest.raises(money.MoneyParseError):
        money.parse_cents(True)
    with pytest.raises(money.MoneyParseError):
        money.parse_cents(False)


# ---------------------------------------------------------------- 严格版
def test_strict_cents_rejects_excess_decimals():
    """记账录入链路：手输 1.234 必须报错，不能静默舍成 1.23。"""
    assert money.strict_cents("1.23") == 123
    assert money.strict_cents("1.2") == 120
    with pytest.raises(money.MoneyParseError):
        money.strict_cents("1.234")
    with pytest.raises(money.MoneyParseError):
        money.strict_cents("1.005")  # 三位小数，即使能四舍五入也要拒


def test_strict_cents_rejects_empty_and_bool():
    with pytest.raises(money.MoneyParseError):
        money.strict_cents(None)
    with pytest.raises(money.MoneyParseError):
        money.strict_cents(True)


def test_money_parse_error_is_value_error():
    """既有调用方普遍 ``except ValueError``，子类化保证不破坏它们。"""
    assert issubclass(money.MoneyParseError, ValueError)


# ---------------------------------------------------------------- 展示
def test_format_and_yuan():
    assert money.format_cents(123456) == "1234.56"
    assert money.format_cents(0) == "0.00"
    assert money.cents_to_yuan(123456) == 1234.56
    assert money.cents_to_yuan("abc") == 0.0


# ---------------------------------------------------------------- 零依赖守卫
def test_module_is_a_leaf_with_no_project_imports():
    """🔴 这个模块必须保持零项目依赖。

    否则"从 money import 一个常量"会把整张依赖图拖进调用方
    （小分包 / 首屏包尤其致命）。这是审计时明确记下的坑。
    """
    src = (ROOT / "modules" / "money.py").read_text(encoding="utf-8")
    body = src.split('__all__', 1)[1]
    for banned in ("import db", "from db", "import gui", "from gui",
                   "import modules", "from modules", "import app_paths",
                   "import security", "import tkinter", "import requests"):
        assert banned not in body, (
            "modules/money.py 引入了 %r —— 它必须是零依赖叶子模块，"
            "否则会把重依赖拖进所有调用方" % banned
        )
