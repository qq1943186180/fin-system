# -*- coding: utf-8 -*-
"""方案 B 回归测试：数电票 XML / OFD 支持（合规归档格式）。

锁住的坑（2026-09-11）：
  1. 附件白名单只认 ``{.pdf,.jpg,.jpeg,.png}``，**XML / OFD 被静默丢弃**
     —— 无日志、无提示、无计数；
  2. 财会〔2020〕6号 + 财政部口径：数电票的 PDF/OFD 是**预览件**，
     **含数字签名的 XML 才是符合入账要求的电子文件格式** ——
     只归档 PDF 对数电票**不合规**；
  3. XML 是结构化权威数据，不该也不需要走 OCR（"从排版里猜字段"）。
"""
from __future__ import annotations

import importlib
import io
import json
import zipfile
from pathlib import Path

import pytest

# ---- 官方样例（《电子凭证会计数据标准——全面数字化的电子发票(试行版)》）----
SAMPLE_XML = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<EInvoice>
    <Header>
        <EIid>23442000000000130159</EIid>
        <EInvoiceTag>SWEI4400</EInvoiceTag>
        <Version>0.2</Version>
        <InherentLabel>
            <InIssuType><LabelCode>Y</LabelCode><LabelName>是否蓝字发票标志</LabelName></InIssuType>
        </InherentLabel>
    </Header>
    <EInvoiceData>
        <SellerInformation>
            <SellerIdNum>91440600065160777Y</SellerIdNum>
            <SellerName>箭牌家居集团股份有限公司</SellerName>
            <SellerAddr>佛山市三水区南山镇康裕三路1号1座（住所申报）</SellerAddr>
        </SellerInformation>
        <BuyerInformation>
            <BuyerIdNum>914406065608413420</BuyerIdNum>
            <BuyerName>佛山市顺德区尊品无纺布制品有限公司</BuyerName>
        </BuyerInformation>
        <BasicInformation>
            <TotalAmWithoutTax>2.00</TotalAmWithoutTax>
            <TotalTaxAm>0.12</TotalTaxAm>
            <TotalTax-includedAmount>2.12</TotalTax-includedAmount>
            <TotalTax-includedAmountInChinese>贰圆壹角贰分</TotalTax-includedAmountInChinese>
            <Drawer>谢岳荣</Drawer>
            <RequestTime>2023-02-23</RequestTime>
        </BasicInformation>
        <IssuItemInformation>
            <ItemName>*人力资源服务*劳务</ItemName>
            <SpecMod></SpecMod>
            <MeaUnits></MeaUnits>
            <Quantity>2</Quantity>
            <UnPrice>1</UnPrice>
            <Amount>2.00</Amount>
            <TaxRate>0.060000</TaxRate>
            <ComTaxAm>0.120000</ComTaxAm>
            <TotaltaxIncludedAmount>2.120000</TotaltaxIncludedAmount>
            <TaxClassificationCode>3040803010000000000</TaxClassificationCode>
        </IssuItemInformation>
        <AdditionalInformation><Remark>备注内容描述</Remark></AdditionalInformation>
    </EInvoiceData>
    <SellerAuthentication><AuthenticationMethods>02</AuthenticationMethods></SellerAuthentication>
    <TaxSupervisionInfo>
        <InvoiceNumber>23442000000000130159</InvoiceNumber>
        <IssueTime>2023-02-23 10:08:05</IssueTime>
        <TaxBureauCode>14400000000</TaxBureauCode>
        <TaxBureauName>国家税务总局广东省税务局</TaxBureauName>
    </TaxSupervisionInfo>
</EInvoice>
"""


@pytest.fixture()
def finance_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_SYSTEM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FINANCE_SYSTEM_TEST_MODE", "1")

    import app_paths
    import db

    importlib.reload(app_paths)
    importlib.reload(db)

    from modules import invoice_gui

    importlib.reload(invoice_gui)
    db.configure_db_path(tmp_path / "finance.db")
    db.init_db(tmp_path / "finance.db")
    app_paths.ensure_app_dirs()
    return app_paths, db, invoice_gui


# ------------------------------------------------------------ XML 解析


def test_parse_official_sample_fields():
    from modules.einvoice import parse_einvoice_xml

    fields = parse_einvoice_xml(SAMPLE_XML.encode("utf-8"))

    assert fields["发票号码"] == "23442000000000130159"
    assert fields["开票日期"] == "2023-02-23"
    assert fields["销售方名称"] == "箭牌家居集团股份有限公司"
    assert fields["销售方税号"] == "91440600065160777Y"
    assert fields["购买方名称"] == "佛山市顺德区尊品无纺布制品有限公司"
    assert fields["购买方税号"] == "914406065608413420"
    assert fields["金额合计"] == "2.00"
    assert fields["税额合计"] == "0.12"
    assert fields["价税合计"] == "2.12"
    assert fields["价税合计大写"] == "贰圆壹角贰分"
    assert fields["开票人"] == "谢岳荣"
    assert fields["_source"] == "einvoice-xml"
    assert fields["ocr_confidence"] == 1.0


def test_amounts_with_six_decimals_normalised():
    """税额在 XML 里常是 6 位小数，必须归一成 2 位，否则下游口径混乱。"""
    from modules.einvoice import parse_einvoice_xml

    xml = SAMPLE_XML.replace("<TotalTaxAm>0.12</TotalTaxAm>", "<TotalTaxAm>0.120000</TotalTaxAm>")
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert fields["税额合计"] == "0.12"


def test_item_lines_collected():
    """数电票可有多行明细；明细行是 PDF 正则做不到的。"""
    from modules.einvoice import parse_einvoice_xml

    extra = """        <IssuItemInformation>
            <ItemName>*信息技术服务*开发费</ItemName>
            <Quantity>1</Quantity>
            <Amount>100.00</Amount>
            <TaxRate>0.060000</TaxRate>
            <ComTaxAm>6.000000</ComTaxAm>
        </IssuItemInformation>
"""
    xml = SAMPLE_XML.replace("        <AdditionalInformation>", extra + "        <AdditionalInformation>")
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert len(fields["明细行"]) == 2
    assert fields["明细行"][0]["itemname"] == "*人力资源服务*劳务"
    assert fields["明细行"][1]["itemname"] == "*信息技术服务*开发费"


def test_namespaced_xml_supported():
    """真实税局文件常带命名空间，必须能剥掉。"""
    from modules.einvoice import parse_einvoice_xml

    xml = SAMPLE_XML.replace(
        "<EInvoice>",
        '<EInvoice xmlns="http://www.chinatax.gov.cn/einv" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">',
    )
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert fields["发票号码"] == "23442000000000130159"


def test_case_variant_root_and_tags():
    """不同版本存在大小写变体（Einvoice / TotaltaxIncludedAmount）。"""
    from modules.einvoice import parse_einvoice_xml

    xml = (
        SAMPLE_XML.replace("<EInvoice>", "<Einvoice>")
        .replace("</EInvoice>", "</Einvoice>")
        .replace("TotalTax-includedAmount>", "TotaltaxIncludedAmount>")
    )
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert fields["价税合计"] == "2.12"


def test_top_level_total_wins_over_item_level():
    """明细行也有 TotaltaxIncludedAmount；取顶层值，不能被明细行覆盖。"""
    from modules.einvoice import parse_einvoice_xml

    xml = SAMPLE_XML.replace(
        "<TotalTax-includedAmount>2.12</TotalTax-includedAmount>",
        "<TotalTax-includedAmount>9.99</TotalTax-includedAmount>",
    )
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert fields["价税合计"] == "9.99", "必须取 BasicInformation 下的顶层值"


def test_reject_non_einvoice_xml():
    from modules.einvoice import EinvoiceParseError, parse_einvoice_xml

    with pytest.raises(EinvoiceParseError) as exc:
        parse_einvoice_xml(b"<?xml version='1.0'?><root><a>1</a></root>")
    assert "EInvoice" in str(exc.value)


def test_reject_malformed_xml():
    from modules.einvoice import EinvoiceParseError, parse_einvoice_xml

    with pytest.raises(EinvoiceParseError) as exc:
        parse_einvoice_xml(b"<EInvoice><unclosed>")
    assert "无法解析" in str(exc.value)


def test_missing_invoice_number_gives_actionable_error():
    from modules.einvoice import EinvoiceParseError, parse_einvoice_xml

    xml = SAMPLE_XML.replace(
        "<InvoiceNumber>23442000000000130159</InvoiceNumber>", ""
    )
    with pytest.raises(EinvoiceParseError) as exc:
        parse_einvoice_xml(xml.encode("utf-8"))
    assert "发票号码" in str(exc.value)


def test_inconsistent_cents_drops_net_amount():
    """分位各自四舍五入后不自洽时，丢掉不含税金额让下游推导，而不是整张票被拒。"""
    from modules.einvoice import parse_einvoice_xml

    xml = (
        SAMPLE_XML.replace("<TotalAmWithoutTax>2.00</TotalAmWithoutTax>", "<TotalAmWithoutTax>2.005</TotalAmWithoutTax>")
        .replace("<TotalTaxAm>0.12</TotalTaxAm>", "<TotalTaxAm>0.125</TotalTaxAm>")
        .replace("<TotalTax-includedAmount>2.12</TotalTax-includedAmount>", "<TotalTax-includedAmount>2.13</TotalTax-includedAmount>")
    )
    fields = parse_einvoice_xml(xml.encode("utf-8"))
    assert fields["金额合计"] == ""
    assert fields["价税合计"] == "2.13"
    assert fields["税额合计"] == "0.13"


# ------------------------------------------------------------ OFD


def _make_ofd(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def test_ofd_with_embedded_xml_parsed():
    from modules.einvoice import parse_einvoice_attachment

    ofd = _make_ofd(
        {
            "OFD.xml": b"<?xml version='1.0'?><OFD><DocBody/></OFD>",
            "Doc_0/Pages/Page_0/Content.xml": b"<?xml version='1.0'?><Page/>",
            "Doc_0/Attachments/einvoice.xml": SAMPLE_XML.encode("utf-8"),
        }
    )
    fields = parse_einvoice_attachment(ofd, ".ofd")
    assert fields["发票号码"] == "23442000000000130159"
    assert fields["_source"] == "einvoice-ofd-xml"


def test_ofd_without_xml_raises_with_actionable_message():
    from modules.einvoice import EinvoiceParseError, parse_einvoice_attachment

    ofd = _make_ofd({"OFD.xml": b"<?xml version='1.0'?><OFD/>"})
    with pytest.raises(EinvoiceParseError) as exc:
        parse_einvoice_attachment(ofd, ".ofd")
    msg = str(exc.value)
    assert "XML" in msg and "批量下载" in msg


def test_ofd_broken_zip_raises():
    from modules.einvoice import EinvoiceParseError, parse_einvoice_attachment

    with pytest.raises(EinvoiceParseError):
        parse_einvoice_attachment(b"not a zip at all", ".ofd")


# ------------------------------------------------------------ 白名单收敛


def test_supported_exts_single_source_of_truth():
    from modules import mail_processor, mail_service
    from modules.einvoice import SUPPORTED_EXTS

    assert ".xml" in SUPPORTED_EXTS and ".ofd" in SUPPORTED_EXTS
    assert mail_processor.ALLOWED_EXTS == SUPPORTED_EXTS
    # 配置默认值也必须是同一份，避免三处手工同步漂移
    cfg = mail_service.MailBoxConfig(name="x", imap_host="h")
    assert set(cfg.allowed_ext) == set(SUPPORTED_EXTS)


# ------------------------------------------------------------ 端到端入库


def test_xml_attachment_registers_without_ocr_agent(finance_env, tmp_path):
    """XML 走本地解析，即便没配 invoice_agent_command 也必须能入库。"""
    from modules import mail_processor

    result = mail_processor.process_attachment(
        "数电票.xml", SAMPLE_XML.encode("utf-8")
    )

    assert result.ok, result.message
    row = result.data
    assert row["invoice_no"] == "23442000000000130159"
    assert row["date"] == "2023-02-23"
    assert row["net_amount_cents"] == 200
    assert row["tax_cents"] == 12
    assert row["total_cents"] == 212
    assert row["ocr_version"] == "einvoice-xml"
    assert row["file_path"].endswith(".xml")
    assert Path(row["file_path"]).is_file()


def test_xml_attachment_archives_original(finance_env):
    from modules import mail_processor

    result = mail_processor.process_attachment("数电票.xml", SAMPLE_XML.encode("utf-8"))
    archived = Path(result.data["file_path"])
    assert archived.parent.name == "2023-02", "应按开票月份归档"
    assert archived.read_bytes() == SAMPLE_XML.encode("utf-8")


def test_invalid_xml_not_silently_dropped(finance_env):
    """解析失败必须留痕到 invoices/failed/，绝不静默丢弃。"""
    from modules import mail_processor

    result = mail_processor.process_attachment("坏文件.xml", b"<EInvoice><oops>")

    assert not result.ok
    app_paths = finance_env[0]
    failed = sorted(Path(app_paths.INVOICES_FAILED_DIR).glob("坏文件_*.xml"))
    assert failed, "解析失败的附件必须落盘留痕"
    assert failed[0].read_bytes() == b"<EInvoice><oops>"


def test_unsupported_extension_still_rejected(finance_env):
    from modules import mail_processor

    result = mail_processor.process_attachment("恶意.exe", b"MZ")
    assert not result.ok
    assert "不支持的文件类型" in result.message


def test_process_file_accepts_xml(finance_env, tmp_path):
    from modules import mail_processor

    src = tmp_path / "inbox" / "票.xml"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(SAMPLE_XML.encode("utf-8"))

    result = mail_processor.process_file(str(src))
    assert result.ok, result.message
    assert result.data["invoice_no"] == "23442000000000130159"


def test_duplicate_xml_not_double_counted(finance_env):
    from modules import mail_processor

    first = mail_processor.process_attachment("a.xml", SAMPLE_XML.encode("utf-8"))
    second = mail_processor.process_attachment("b.xml", SAMPLE_XML.encode("utf-8"))
    assert first.ok
    assert not second.ok, "同一张票不应重复入库"


# ------------------------------------------------------------ 探测工具


def test_probe_bytes_handles_xml_without_ocr_command():
    """补收预筛对 XML 不应调用 OCR 子进程（cmd 为空也要能 OK）。"""
    from tools.invoice_ocr_probe import probe_bytes

    r = probe_bytes([], "数电票.xml", SAMPLE_XML.encode("utf-8"))
    assert r["level"] == "OK"
    assert r["fields"]["发票号码"] == "23442000000000130159"


def test_probe_bytes_reports_bad_xml():
    from tools.invoice_ocr_probe import probe_bytes

    r = probe_bytes([], "坏.xml", b"<root/>")
    assert r["level"] == "ERROR"


def test_classify_accepts_derivable_net_amount():
    """缺「金额合计」但有「价税合计 + 税额」时应判定可入库（可推导）。"""
    from tools.invoice_ocr_probe import classify

    level, issues = classify(
        {
            "发票号码": "26337000000663360916",
            "开票日期": "2026-07-10",
            "价税合计": "2.13",
            "税额合计": "0.13",
            "金额合计": "",
            "销售方税号": "91330105MACRGYRN32",
        }
    )
    assert level == "OK", issues


def test_einvoice_exts_are_disjoint_from_image_exts():
    """XML/OFD 与 PDF/图片是两条不同链路，集合不能重叠（否则会误走 OCR）。"""
    from modules.einvoice import EINVOICE_EXTS, IMAGE_EXTS

    assert not (EINVOICE_EXTS & IMAGE_EXTS)
    assert ".xml" in EINVOICE_EXTS and ".ofd" in EINVOICE_EXTS
    assert ".pdf" in IMAGE_EXTS
