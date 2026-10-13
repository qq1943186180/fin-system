"""数电票（全面数字化的电子发票）结构化解析 —— 不依赖 OCR。

## 为什么需要这个模块

财会〔2020〕6号 + 财政部/税务总局答复口径明确：
数电票的 **PDF / OFD 是「预览」发票内容的格式**，
**含数字签名的 XML 才是符合财政部入账要求的电子文件格式**。

本系统此前的附件白名单只认 ``{.pdf, .jpg, .jpeg, .png}``，
**XML / OFD 被静默丢弃**（无日志、无提示、无计数）——
对数电票而言既不完整也不合规。本模块补上这一环：

* ``.xml``  → 直接解析税局权威结构化数据（**零识别误差**，含明细行、可验签）
* ``.ofd``  → 尝试取出内嵌的数电票 XML；取不到则**归档留痕并明确报告**，
  绝不静默丢弃（OFD 是版式件，合规入账仍需 XML）

## 与 OCR 的分工

PDF / 图片走外部 OCR agent（``invoice_agent_command``）；
XML / OFD 走本模块。前者是「从排版里猜字段」，后者是「取权威真值」。

## 字段映射依据

《电子凭证会计数据标准——全面数字化的电子发票(试行版)》官方样例要素表，
XML 结构为 ``EInvoice / Header / EInvoiceData{ SellerInformation,
BuyerInformation, BasicInformation, IssuItemInformation,
AdditionalInformation } / TaxSupervisionInfo / TaxBureauSignature``；
其中 ``TaskSupervisionInfo/InvoiceNumber`` = 发票号码，
``BasicInformation/RequestTime`` = 开票日期。

解析器对 **命名空间、元素大小写、多版本别名** 均做归一化，
并按「父节点」消歧（``IssuItemInformation`` 下也有 ``TotaltaxIncludedAmount``，
不能与顶层 ``TotalTax-includedAmount`` 混为一谈）。
"""

from __future__ import annotations

import io
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

LOG = logging.getLogger(__name__)


# ------------------------------------------------------------------ 支持类型
#: 单一事实来源 —— 邮件同步 / 回填 / 重试三条链路都从这里取，
#: 避免「三处手工同步」（历史教训见 HANDOFF P4 依赖三处同步）。
#:
#: ``IMAGE_EXTS``   走外部 OCR（从排版里猜字段）
#: ``EINVOICE_EXTS`` 走本模块结构化解析（取税局权威真值），两条链路互斥
#: ``SUPPORTED_EXTS`` 并集，供配置/界面展示
IMAGE_EXTS: frozenset[str] = frozenset({".pdf", ".jpg", ".jpeg", ".png"})
XML_EXTS: frozenset[str] = frozenset({".xml"})
OFD_EXTS: frozenset[str] = frozenset({".ofd"})
EINVOICE_EXTS: frozenset[str] = XML_EXTS | OFD_EXTS

#: 供配置/界面展示的稳定顺序
SUPPORTED_EXTS: tuple[str, ...] = (".pdf", ".jpg", ".jpeg", ".png", ".xml", ".ofd")

assert IMAGE_EXTS | EINVOICE_EXTS == set(SUPPORTED_EXTS)  # 防白名单漂移


class EinvoiceParseError(ValueError):
    """XML/OFD 不是可解析的数电票。"""


# ------------------------------------------------------------ 元素名归一化
def _local(tag: str) -> str:
    """去掉命名空间前缀并小写：``{ns}SellerName`` → ``sellername``。"""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.strip().lower()


def _text_of(elem: ET.Element) -> str:
    return (elem.text or "").strip()


class _Index:
    """``小写元素名 -> [(文本, 父元素小写名), ...]``，保留文档顺序。

    记录父名是为了消歧：``IssuItemInformation`` 里的 ``TotaltaxIncludedAmount``
    与顶层 ``TotalTax-includedAmount`` 是不同含义的字段。
    """

    def __init__(self, root: ET.Element) -> None:
        self._map: dict[str, list[tuple[str, str]]] = {}
        self._walk(root, "")

    def _walk(self, elem: ET.Element, parent: str) -> None:
        name = _local(elem.tag)
        value = _text_of(elem)
        if value:
            self._map.setdefault(name, []).append((value, parent))
        for child in elem:
            self._walk(child, name)

    def pick(self, *names: str, parents: tuple[str, ...] = ()) -> str:
        """按优先级取第一个非空值；``parents`` 非空时优先取该父节点下的值。"""
        for name in names:
            entries = self._map.get(name.lower())
            if not entries:
                continue
            if parents:
                for value, parent in entries:
                    if parent in parents:
                        return value
            for value, _parent in entries:
                return value
        return ""


def _collect_items(root: ET.Element) -> list[dict[str, str]]:
    """收集全部发票明细行（数电票可有多行、结构可重复）。"""
    rows: list[dict[str, str]] = []
    for elem in root.iter():
        name = _local(elem.tag)
        # 兼容 IssuItemInformation / IssuingItemInformation / IssuedItemInformation
        if not (name.startswith("issu") and "item" in name):
            continue
        row: dict[str, str] = {}
        for child in elem:
            key = _local(child.tag)
            if key and key not in row:
                row[key] = _text_of(child)
        if row:
            rows.append(row)
    return rows


def _to_cents(value: str) -> int:
    """``"2.12"`` / ``"0.120000"`` → ``212`` / ``12``；无法解析返回 0。

    实现已收敛到 :mod:`modules.money`（2026-10-14）。原先这里自带一份
    正则擦除 + Decimal 的实现，与项目里另外 8 份判据不一致。
    """
    from modules import money

    return money.parse_cents_or_zero(value)


def _fmt_money(value: str) -> str:
    """税额在 XML 里常是 6 位小数（``0.120000``），统一成 2 位。"""
    if not value:
        return ""
    cents = _to_cents(value)
    if cents == 0 and not re.search(r"\d", value):
        return ""
    return "%.2f" % (cents / 100.0)


# ------------------------------------------------------------------ 主解析
def _parse_root(data: bytes) -> ET.Element:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise EinvoiceParseError("XML 格式无法解析：%s" % exc) from exc
    if _local(root.tag) not in {"einvoice", "e-invoice"}:
        raise EinvoiceParseError(
            "不是数电票 XML（根元素为 %s，期望 EInvoice）" % (root.tag,)
        )
    return root


def parse_einvoice_xml(data: bytes) -> dict:
    """数电票 XML → 与 OCR 输出同构的字段字典（中文键，可直接入库）。

    :raises EinvoiceParseError: 不是可识别的数电票 XML。
    """
    root = _parse_root(data)
    index = _Index(root)

    invoice_no = index.pick("InvoiceNumber", "发票号码")
    date = index.pick("RequestTime", "开票日期", "IssueTime")
    if not invoice_no:
        raise EinvoiceParseError("数电票 XML 缺少发票号码（TaxSupervisionInfo/InvoiceNumber）")
    if not date:
        raise EinvoiceParseError("数电票 XML 缺少开票日期（BasicInformation/RequestTime）")

    seller_name = index.pick("SellerName", "销售方名称", parents=("sellerinformation",))
    seller_tax = index.pick("SellerIdNum", "销售方税号", parents=("sellerinformation",))
    buyer_name = index.pick("BuyerName", "购买方名称", parents=("buyerinformation",))
    buyer_tax = index.pick("BuyerIdNum", "购买方税号", parents=("buyerinformation",))

    basic = ("basicinformation",)
    total_cn = index.pick(
        "TotalTax-includedAmountInChinese",
        "TotaltaxIncludedAmountInChinese",
        "价税合计大写",
        parents=basic,
    ) or index.pick("TotalTax-includedAmountInChinese", "TotaltaxIncludedAmountInChinese")
    net_raw = index.pick("TotalAmWithoutTax", "金额合计", parents=basic)
    tax_raw = index.pick("TotalTaxAm", "税额合计", parents=basic)
    total_raw = index.pick(
        "TotalTax-includedAmount", "TotaltaxIncludedAmount", "价税合计", parents=basic
    )
    if not total_raw:
        total_raw = index.pick("TotalTax-includedAmount", "TotaltaxIncludedAmount")
    drawer = index.pick("Drawer", "开票人", parents=basic)
    remark = index.pick("Remark", "备注", parents=("additionalinformation",))

    net, tax, total = _fmt_money(net_raw), _fmt_money(tax_raw), _fmt_money(total_raw)
    if total:
        if not tax:
            tax = "%.2f" % max(0, (_to_cents(total) - _to_cents(net)) / 100.0)
        if not net:
            net = "%.2f" % max(0, (_to_cents(total) - _to_cents(tax)) / 100.0)
        # 分位各自四舍五入后可能不再自洽；下游 normalize_invoice 要求
        # net + tax == total，否则整张票被拒。此时丢弃「不含税金额」，
        # 让下游按「价税合计 − 税额」推导（与 normalize_invoice 的既有口径一致）。
        if _to_cents(net) + _to_cents(tax) != _to_cents(total):
            LOG.warning(
                "数电票 %s 金额不自洽：不含税 %s + 税额 %s != 价税合计 %s，"
                "已按「价税合计 − 税额」推导不含税金额",
                invoice_no, net, tax, total,
            )
            net = ""

    items = _collect_items(root)
    return {
        "发票号码": invoice_no,
        "开票日期": date,
        "销售方名称": seller_name,
        "销售方税号": seller_tax,
        "购买方名称": buyer_name,
        "购买方税号": buyer_tax,
        "金额合计": net,
        "税额合计": tax,
        "价税合计": total,
        "价税合计大写": total_cn,
        "开票人": drawer,
        "备注": remark,
        "明细行": items,
        "_source": "einvoice-xml",
        "ocr_confidence": 1.0,  # 结构化直取，无识别误差
    }


# ------------------------------------------------------------------ OFD
#: 单个内嵌 XML 的读取上限（防解压炸弹）
_MAX_EMBEDDED_XML = 8 * 1024 * 1024


def extract_ofd_invoice_xml(data: bytes) -> bytes | None:
    """从 OFD（ZIP 容器）里找出内嵌的数电票 XML；找不到返回 ``None``。"""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".xml")]
            # 版式描述文件不可能装发票数据，先跳过，省解压
            names.sort(key=lambda n: (n.lower().endswith("ofd.xml"), len(n)))
            for name in names:
                try:
                    info = zf.getinfo(name)
                except KeyError:
                    continue
                if info.file_size > _MAX_EMBEDDED_XML:
                    continue
                try:
                    blob = zf.read(name)
                except (zipfile.BadZipFile, RuntimeError, OSError):
                    continue
                if b"EInvoice" not in blob[:4096] and b"Einvoice" not in blob[:4096]:
                    continue
                try:
                    _parse_root(blob)
                except EinvoiceParseError:
                    continue
                return blob
    except (zipfile.BadZipFile, OSError):
        return None
    return None


# ------------------------------------------------------------------ 入口
def parse_einvoice_attachment(data: bytes, suffix: str) -> dict:
    """按后缀解析 XML/OFD 附件。

    :raises EinvoiceParseError: OFD 内未找到可解析的数电票 XML。
    """
    suffix = suffix.lower()
    if suffix in XML_EXTS:
        return parse_einvoice_xml(data)
    if suffix in OFD_EXTS:
        embedded = extract_ofd_invoice_xml(data)
        if embedded is not None:
            fields = parse_einvoice_xml(embedded)
            fields["_source"] = "einvoice-ofd-xml"
            return fields
        raise EinvoiceParseError(
            "OFD 内未找到结构化数电票 XML。OFD 是版式（预览）件，"
            "合规入账需要 XML —— 请在税局「批量下载」时同时勾选 XML。"
        )
    raise EinvoiceParseError("不支持的类型：%s" % suffix)
