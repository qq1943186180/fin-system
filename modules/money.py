# -*- coding: utf-8 -*-
"""金额 ↔ 整数分：全项目**唯一**实现。

## 为什么有这个模块（2026-10-14 审计）

项目里原先有 **9 份**"字符串/数字 → 整数分"的实现，散落在
``einvoice`` / ``feishu_pull`` / ``invoice_gui`` / ``ledger`` / ``gui.main`` /
``feishu`` 和两个 tools 里。实测（17 个边界用例）**9 个用例结果不一致**：

* ``-5.00`` → 某份得 ``+500``：它用 ``"".join(ch for ch in s if ch.isdigit() or ch == ".")``
  过滤，**把负号筛掉了**（红字票/冲销金额会变正数）。
* ``0.125`` → 12 vs 13、``1.005`` → 100 vs 101：``round()`` 是**银行家舍入**，
  与 ``Decimal.quantize(ROUND_HALF_UP)`` 在半分位上差 1 分。
* 全角数字 ``１２３`` → 一份得 0、其余得 12300（``float()`` 吃全角，正则不吃）。
* 失败哨兵不统一：有的返 ``0``、有的返 ``None``。

**规则（本模块固化）**：
1. 一律走 ``Decimal``，**绝不走 ``float``** —— ``float("1.005")*100`` 本身就有误差。
2. 舍入用 **ROUND_HALF_UP**（会计口径的"四舍五入"）。
3. 只剥离**已知噪音**（空白 / 千分位 / 货币符号 / 元 圆），剩下的必须能被
   ``Decimal`` 解析。不用"正则擦掉所有非数字字符"——那会把 ``"12ab"`` 静默变成 1200。
4. 解析失败**必须可区分**：调用方自己决定要 0、要 None、还是要抛错。
   由这个模块提供三个出口，绝不在调用方各写一遍 try/except。
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

__all__ = [
    "MoneyParseError",
    "clean_amount_text",
    "parse_cents",
    "parse_cents_or_zero",
    "parse_cents_or_none",
    "strict_cents",
    "cents_to_yuan",
    "format_cents",
]

#: 只剥离这些"已知噪音"。故意不收字母 —— "12ab" 应当是解析失败，而不是 1200。
_NOISE = re.compile(r"[\s,¥￥$元圆]")
_ONE = Decimal("1")
_CENT = Decimal("0.01")


class MoneyParseError(ValueError):
    """金额无法解析。继承 ValueError，兼容既有 ``except ValueError`` 调用方。"""


def clean_amount_text(value: object) -> str:
    """把入参转成"干净的数字文本"；无法转换时抛 :class:`MoneyParseError`。"""
    if isinstance(value, bool):
        # bool 是 int 的子类，``True`` 会被当成 1 —— 必须显式拒绝
        raise MoneyParseError("金额不能是布尔值")
    if value is None:
        raise MoneyParseError("金额为空")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise MoneyParseError("金额不是有限数")
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 经 str() 走 Decimal，避免 float 二进制误差被二次放大
        return repr(value)
    text = _NOISE.sub("", str(value))
    if not text:
        raise MoneyParseError("金额为空")
    return text


def parse_cents(value: object) -> int:
    """解析成整数分；失败抛 :class:`MoneyParseError`（是 ``ValueError`` 子类）。"""
    text = clean_amount_text(value)
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise MoneyParseError("金额格式无效: %r" % (value,)) from exc
    if not amount.is_finite():
        raise MoneyParseError("金额不是有限数: %r" % (value,))
    return int((amount * 100).quantize(_ONE, rounding=ROUND_HALF_UP))


def parse_cents_or_zero(value: object) -> int:
    """解析成整数分；失败返 0。**仅用于"缺省即 0"的导入链路**。"""
    try:
        return parse_cents(value)
    except MoneyParseError:
        return 0


def parse_cents_or_none(value: object) -> int | None:
    """解析成整数分；失败返 None。**用于必须区分"没解析出来"与"就是 0"的工具**。"""
    try:
        return parse_cents(value)
    except MoneyParseError:
        return None


def strict_cents(value: object, field: str = "金额") -> int:
    """严格版：拒绝空值/布尔/超过两位小数。

    用于**记账录入**链路 —— 用户手输 "1.234" 时必须报错，而不是静默舍成 1.23。
    """
    if value is None or isinstance(value, bool):
        raise MoneyParseError("%s无效" % field)
    try:
        amount = Decimal(str(value)) if not isinstance(value, Decimal) else value
    except (InvalidOperation, ValueError) as exc:
        raise MoneyParseError("%s无效" % field) from exc
    if not amount.is_finite():
        raise MoneyParseError("%s无效" % field)
    rounded = amount.quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount != rounded:
        raise MoneyParseError("%s最多保留两位小数" % field)
    return int(rounded * 100)


def cents_to_yuan(cents: object) -> float:
    """整数分 → 元（float，仅供展示）。"""
    try:
        return round(int(cents) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


def format_cents(cents: object) -> str:
    """整数分 → ``"1234.56"``。"""
    try:
        return "%.2f" % (int(cents) / 100.0)
    except (TypeError, ValueError):
        return "0.00"
