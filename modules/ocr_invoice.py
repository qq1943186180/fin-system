#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
发票识别模块 (PP-OCRv4 稳定线, Windows 验证通过)
策略: 数电票/电子发票优先读 PDF 文本层(结构化, 100%准);
      文本层为空或扫描件时, 用 PP-OCRv4 做图像 OCR 兜底。
依赖: paddleocr>=2.10, paddlepaddle==2.6.2, pymupdf
"""
import os, re, glob, json, argparse, tempfile
from pathlib import Path

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

# 字段抽取 (适配滴滴/数电票格式; 通用字段也能抽)
def extract_fields(blob):
    out = {}
    m = re.search(r"发票号码[:：]\s*([0-9]{8,30})", blob)
    if m: out["发票号码"] = m.group(1)
    m = re.search(r"开票日期[:：]\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?", blob)
    if m:
        out["开票日期"] = "%04d-%02d-%02d" % tuple(map(int, m.groups()))
    ms = re.findall(
        r"名称[:：]\s*([\u4e00-\u9fa5A-Za-z0-9（）()·&．.\-]{2,40}?)\s*统一社会信用代码/纳税人识别号[:：]\s*([A-Z0-9]{15,30})",
        blob)
    if ms:
        out["购买方名称"] = ms[0][0].strip()
        out["购买方税号"] = ms[0][1].strip()
    if len(ms) >= 2:
        out["销售方名称"] = ms[1][0].strip()
        out["销售方税号"] = ms[1][1].strip()
    m = re.search(r"价税合计.*?[¥￥]\s*([0-9][0-9,]*\.?[0-9]*)", blob)
    if m: out["价税合计"] = m.group(1)
    m = re.search(r"合\s*计\s*([0-9][0-9,]*\.?[0-9]*)\s*[¥￥]\s*([0-9][0-9,]*\.?[0-9]*)", blob)
    if m:
        out["金额合计"] = m.group(1)
        out["税额合计"] = m.group(2)
    m = re.search(r"税率/征收率[:：]?\s*(\d+%?)", blob)
    if m: out["税率"] = m.group(1)
    m = re.search(r"开票人[:：]\s*([\u4e00-\u9fa5]{2,10})", blob)
    if m: out["开票人"] = m.group(1)
    out["行程条数"] = len(re.findall(r"出行日期", blob))
    return out

_ocr = None
def get_ocr():
    global _ocr
    if _ocr is None:
        from paddleocr import PaddleOCR
        _ocr = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _ocr

def ocr_image(img_path):
    res = get_ocr().ocr(img_path)
    lines = []
    confidences = []
    for block in res:
        for ln in block:
            lines.append(ln[1][0])
            if len(ln[1]) > 1:
                confidences.append(float(ln[1][1]))
    confidence = sum(confidences) / len(confidences) if confidences else 0.0
    return lines, confidence

def process_pdf(pdf_path):
    name = os.path.basename(pdf_path)
    blob = ""
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            blob = "\n".join(p.get_text() for p in doc)
    except Exception:
        blob = ""
    blob = "\n".join(l for l in blob.splitlines() if l.strip())
    source = "textlayer" if blob else "ocr"
    confidence = 1.0 if blob else 0.0
    if not blob:
        # OCR 兜底: 转图识别
        import fitz
        parts = []
        scores = []
        with tempfile.TemporaryDirectory(prefix="finance-ocr-") as temp_dir:
            with fitz.open(pdf_path) as doc:
                for pi in range(len(doc)):
                    pix = doc[pi].get_pixmap(dpi=200)
                    tmp = str(Path(temp_dir) / ("page-%d.png" % pi))
                    pix.save(tmp)
                    page_lines, page_score = ocr_image(tmp)
                    parts.extend(page_lines)
                    if page_score:
                        scores.append(page_score)
        blob = "\n".join(parts)
        confidence = sum(scores) / len(scores) if scores else 0.0
    fields = extract_fields(blob)
    fields["_source"] = source
    fields["ocr_version"] = "PP-OCRv4" if source == "ocr" else "pdf-text-layer"
    fields["ocr_confidence"] = round(confidence, 4)
    return {"file": name, "fields": fields}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("indir")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = args.out or os.path.join(args.indir, "..", "results", "ocr_result.json")
    pdfs = sorted(glob.glob(os.path.join(args.indir, "*.pdf")))
    results = [process_pdf(p) for p in pdfs]
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("processed %d invoice files" % len(results))
    print("saved ->", os.path.abspath(out))

if __name__ == "__main__":
    main()
