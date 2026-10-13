# -*- coding: utf-8 -*-
"""业务数据回补工具：公司抬头 + 发票原始凭证关联（file_path / file_hash）。

背景（2026-09-10 排查结论，全部经实测核验）
------------------------------------------
1) ``company`` 表恒为 1 行且 ``name`` / ``tax_no`` / ``addr`` / ``note`` 全空。
   报表、打印、合同抬头取不到公司名。
2) ``invoice_doc.file_path`` 恒为空串。根因不在财务系统，而在发票识别 agent：
   ``invoice_agent.py::summarize()`` 把 OCR 结果压缩成 6 个中文字段，
   丢掉了 ``file``（文件名）以及 ``销售方税号`` / ``购买方税号`` / ``价税合计``。
   财务系统 ``register_invoice(raw, raw.get("file_path") or raw.get("file"))``
   因此永远拿不到 source_file，``file_path`` 只能是空串；同一原因也让
   2026-07-12 入库的 4 张滴滴发票 ``seller_tax_no`` / ``buyer_tax_no`` 为空。

设计原则
--------
* **默认只读**：不加 ``--apply`` 时只打印方案，绝不写库、绝不复制文件。
* **写前备份**：``--apply`` 先把 finance.db 复制到 ``backups/`` 并校验字节数。
* **硬证据关联**：一律按「发票号码」精确 join。证据有两个来源——
  ``results/ocr_result.json``（其 ``file`` 与 ``发票号码`` 天然成对），
  以及用识别 agent 的单文件模式对 ``invoices/failed/`` 下的 PDF 重新识别。
  文件名里的金额只作**第二道校验**，金额不符即拒绝关联。
* **拒绝模糊**：一个发票号命中多份文件、或多行发票指向同一文件 → 只报告，不猜。
* **幂等**：只填写目标列为空的行；重复运行不会重复写入。
  ``invoice_doc.file_hash`` 上还有非空唯一索引 ``ux_invoice_file_hash`` 兜底。
* **留痕**：每次改动写一条 append-only 的 ``audit_event``。

用法
----
    python tools/backfill_business_data.py                      # 只读，打印方案
    python tools/backfill_business_data.py --company-name ... --apply
    python tools/backfill_business_data.py --fields file_path,file_hash --apply
    python tools/backfill_business_data.py --json               # 机器可读方案
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

try:  # 控制台编码兜底，避免中文在 cp936 终端抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 仅在极老的解释器上触发
    pass

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app_paths import get_db_path  # noqa: E402
import db as dbmod  # noqa: E402


# 发票识别 agent 的默认位置（本机实测布局，可用 CLI 覆盖）
DEFAULT_AGENT_ROOT = Path("C:/Users/li1/invoice-agent")
DEFAULT_OCR_PYTHON = Path("C:/tmp/ocr_venv2/Scripts/python.exe")

# 允许回补的 invoice_doc 列
INVOICE_FIELDS = ("file_path", "file_hash", "seller_tax_no", "buyer_tax_no")
DEFAULT_FIELDS = ",".join(INVOICE_FIELDS)

# 允许回补的 company 列
COMPANY_FIELDS = ("name", "tax_no", "addr", "note")

# 小于该字节数的“发票”文件视为失败占位符，不参与识别
JUNK_BYTES = 128

AMOUNT_TOLERANCE = 0.011


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def sha256_file(path: str | os.PathLike[str] | None) -> str:
    """与 ``modules.invoice_gui._file_hash`` 完全一致的实现（整文件 sha256 十六进制）。"""
    if not path:
        return ""
    file = Path(path)
    if not file.is_file():
        return ""
    digest = hashlib.sha256()
    with file.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalise_invoice_no(value: Any) -> str:
    """发票号码规范化：只保留数字，长度不足 8 位视为无效（'-' 占位符会被丢弃）。"""
    if value is None:
        return ""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits if len(digits) >= 8 else ""


def _nonempty(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _to_cents(value: Any) -> int | None:
    """把 OCR 文本金额转成分；无法解析返回 None。

    🔴 2026-10-14 修：旧实现用
    ``"".join(ch for ch in str(value) if ch.isdigit() or ch == ".")`` 过滤字符，
    **把负号筛掉了** —— ``-5.00`` 会变成 ``+500``。红字票/冲销金额进来直接变正数。
    现已收敛到 :mod:`modules.money`。
    """
    from modules import money

    return money.parse_cents_or_none(value)


# ---------------------------------------------------------------------------
# 证据层：谁才是某张发票的原始文件
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Evidence:
    invoice_no: str
    source_path: Path
    fields: dict[str, Any]
    origin: str

    def total_cents(self) -> int | None:
        for key in ("价税合计", "金额合计", "total"):
            cents = _to_cents(self.fields.get(key))
            if cents is not None:
                return cents
        return None


def evidence_from_agent_results(results_dir: Path, invoices_dir: Path) -> list[Evidence]:
    """读 ``results/ocr_result.json``：它是 ``{"file": 文件名, "fields": {...}}`` 的列表，
    文件名与发票号码天然成对，是可信度最高的关联证据。"""
    path = Path(results_dir) / "ocr_result.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[Evidence] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            continue
        invoice_no = normalise_invoice_no(fields.get("发票号码"))
        name = item.get("file")
        if not invoice_no or not name:
            continue
        out.append(
            Evidence(
                invoice_no=invoice_no,
                source_path=(Path(invoices_dir) / name).resolve(),
                fields=fields,
                origin="ocr_result.json",
            )
        )
    return out


def make_ocr_runner(
    ocr_python: Path,
    ocr_script: Path,
    *,
    timeout: int = 300,
) -> Callable[[Path], dict[str, Any] | None]:
    """返回一个 ``pdf -> {"file":..., "fields":...} | None`` 的识别函数。"""

    def run(pdf_path: Path) -> dict[str, Any] | None:
        if not Path(ocr_python).is_file() or not Path(ocr_script).is_file():
            return None
        try:
            proc = subprocess.run(
                [str(ocr_python), str(ocr_script), "--file", str(pdf_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
                cwd=str(Path(ocr_script).parent),
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        for line in reversed((proc.stdout or "").splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("fields"), dict):
                    return parsed
        return None

    return run


def evidence_from_ocr_calls(
    pdf_paths: Iterable[Path],
    runner: Callable[[Path], dict[str, Any] | None],
) -> tuple[list[Evidence], list[str]]:
    """对给定 PDF 逐个重新识别，返回 (证据列表, 失败说明列表)。"""
    out: list[Evidence] = []
    failures: list[str] = []
    for pdf in pdf_paths:
        pdf = Path(pdf)
        try:
            size = pdf.stat().st_size
        except OSError as exc:
            failures.append("%s：无法读取（%s）" % (pdf.name, exc.__class__.__name__))
            continue
        if size < JUNK_BYTES:
            failures.append("%s：%d 字节，是失败占位符，已忽略" % (pdf.name, size))
            continue
        parsed = runner(pdf)
        if not parsed:
            failures.append("%s：识别未返回结果" % pdf.name)
            continue
        fields = parsed.get("fields") or {}
        invoice_no = normalise_invoice_no(fields.get("发票号码"))
        if not invoice_no:
            failures.append("%s：识别不出发票号码" % pdf.name)
            continue
        out.append(
            Evidence(
                invoice_no=invoice_no,
                source_path=pdf.resolve(),
                fields=fields,
                origin="单文件识别",
            )
        )
    return out, failures


def index_evidence(items: Iterable[Evidence]) -> dict[str, list[Evidence]]:
    index: dict[str, list[Evidence]] = {}
    for item in items:
        index.setdefault(item.invoice_no, []).append(item)
    return index


# ---------------------------------------------------------------------------
# 方案层
# ---------------------------------------------------------------------------
@dataclass
class LinkPlan:
    invoice_id: int
    invoice_no: str
    fields: dict[str, str]
    source_path: Path
    target_path: Path
    file_hash: str
    origin: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_no": self.invoice_no,
            "fields": dict(self.fields),
            "source_path": str(self.source_path),
            "target_path": str(self.target_path),
            "file_hash": self.file_hash,
            "origin": self.origin,
        }


@dataclass
class CompanyPlan:
    name: str
    tax_no: str
    addr: str
    note: str
    is_insert: bool = False

    def changes(self, current: dict[str, Any]) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for key in COMPANY_FIELDS:
            new = getattr(self, key)
            if not new:
                continue
            old = "" if current.get(key) is None else str(current.get(key))
            if old.strip() != new.strip():
                out[key] = (old, new)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in COMPANY_FIELDS}


@dataclass
class Plan:
    links: list[LinkPlan] = field(default_factory=list)
    company: CompanyPlan = field(default_factory=lambda: CompanyPlan("", "", "", ""))
    company_current: dict[str, Any] = field(default_factory=dict)
    company_row_exists: bool = True
    unresolved: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    ignored_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "links": [link.to_dict() for link in self.links],
            "company": {
                "exists": self.company_row_exists,
                "current": self.company_current,
                "desired": self.company.to_dict(),
            },
            "unresolved": list(self.unresolved),
            "conflicts": list(self.conflicts),
            "unchanged": list(self.unchanged),
            "ignored_files": list(self.ignored_files),
        }


def _month_of(value: Any, fallback: datetime) -> str:
    """优先用发票自身的开票日期决定归档月份，避免把 7 月的票放进 9 月目录。"""
    if _nonempty(value):
        text = str(value).strip()
        if len(text) >= 7 and text[4] == "-" and text[:4].isdigit() and text[5:7].isdigit():
            return "%s-%s" % (text[:4], text[5:7])
    return fallback.strftime("%Y-%m")


def _archive_target(
    archive_dir: Path, invoice_no: str, source: Path, month: str
) -> Path:
    """归档文件名前置发票号，便于人工核对与避免重名。"""
    return (Path(archive_dir) / month / ("%s_%s" % (invoice_no, source.name))).resolve()


def build_plan(
    conn,
    evidence_by_no: dict[str, list[Evidence]],
    *,
    fields: Iterable[str] = INVOICE_FIELDS,
    company: CompanyPlan | None = None,
    archive_dir: Path | None = None,
    copy_files: bool = True,
    now: datetime | None = None,
) -> Plan:
    """纯计算：给出「打算改什么」，不触碰数据库与磁盘。"""
    now = now or datetime.now(timezone.utc).astimezone()
    wanted = [name for name in INVOICE_FIELDS if name in set(fields)]
    plan = Plan(company=company or CompanyPlan("", "", "", ""))

    # ---- company ----
    row = conn.execute(
        "SELECT name, tax_no, addr, note FROM company WHERE id=1"
    ).fetchone()
    if row is None:
        plan.company_row_exists = False
        plan.company_current = {key: None for key in COMPANY_FIELDS}
    else:
        plan.company_row_exists = True
        plan.company_current = {key: row[key] for key in COMPANY_FIELDS}

    # ---- invoice links ----
    rows = conn.execute(
        "SELECT id, invoice_no, date, total_cents, file_path, file_hash, "
        "       seller_tax_no, buyer_tax_no "
        "FROM invoice_doc ORDER BY id"
    ).fetchall()

    planned_hashes: dict[str, int] = {}   # 目标文件哈希 -> invoice_id

    for rec in rows:
        invoice_id = int(rec["id"])
        invoice_no = normalise_invoice_no(rec["invoice_no"])
        missing = [name for name in wanted if not _nonempty(rec[name])]
        if not missing:
            plan.unchanged.append(
                "发票 id=%d %s：%s 已填写，跳过" % (invoice_id, invoice_no or "(无号码)", "、".join(wanted))
            )
            continue

        if not invoice_no:
            plan.unresolved.append("发票 id=%d：入库时发票号码为空，无法关联" % invoice_id)
            continue

        candidates = list(evidence_by_no.get(invoice_no, []))
        if not candidates:
            plan.unresolved.append(
                "发票 id=%d %s：找不到可核验的 PDF 原始文件" % (invoice_id, invoice_no)
            )
            continue
        if len(candidates) > 1:
            plan.conflicts.append(
                "发票 id=%d %s：命中 %d 份文件（%s），需人工确认"
                % (
                    invoice_id,
                    invoice_no,
                    len(candidates),
                    "、".join(sorted(c.source_path.name for c in candidates)),
                )
            )
            continue

        evidence = candidates[0]

        # 磁盘上必须真实存在
        if not evidence.source_path.is_file():
            plan.unresolved.append(
                "发票 id=%d %s：证据指向的文件不存在（%s）"
                % (invoice_id, invoice_no, evidence.source_path)
            )
            continue

        # 第二道校验：金额必须一致
        evidence_cents = evidence.total_cents()
        row_cents = int(rec["total_cents"] or 0)
        if evidence_cents is None:
            plan.conflicts.append(
                "发票 id=%d %s：PDF 金额无法解析，拒绝关联" % (invoice_id, invoice_no)
            )
            continue
        if row_cents and abs(evidence_cents - row_cents) > int(AMOUNT_TOLERANCE * 100):
            plan.conflicts.append(
                "发票 id=%d %s：金额不符（库内 %.2f / PDF %.2f），拒绝关联"
                % (invoice_id, invoice_no, row_cents / 100.0, evidence_cents / 100.0)
            )
            continue

        digest = sha256_file(evidence.source_path)
        if not digest:
            plan.unresolved.append("发票 id=%d %s：无法计算文件哈希" % (invoice_id, invoice_no))
            continue

        if digest in planned_hashes and planned_hashes[digest] != invoice_id:
            plan.conflicts.append(
                "发票 id=%d %s：与 id=%d 指向同一份文件，拒绝重复关联"
                % (invoice_id, invoice_no, planned_hashes[digest])
            )
            continue
        other = conn.execute(
            "SELECT id FROM invoice_doc WHERE file_hash=? AND id<>? LIMIT 1",
            (digest, invoice_id),
        ).fetchone()
        if other is not None:
            plan.conflicts.append(
                "发票 id=%d %s：该文件已关联到发票 id=%d" % (invoice_id, invoice_no, other["id"])
            )
            continue

        target = (
            _archive_target(
                archive_dir,
                invoice_no,
                evidence.source_path,
                _month_of(rec["date"], now),
            )
            if copy_files and archive_dir is not None
            else evidence.source_path
        )

        values: dict[str, str] = {}
        if "file_path" in wanted and not _nonempty(rec["file_path"]):
            values["file_path"] = str(target)
        if "file_hash" in wanted and not _nonempty(rec["file_hash"]):
            values["file_hash"] = digest
        for column, keys in (
            ("seller_tax_no", ("销售方税号", "seller_tax_no")),
            ("buyer_tax_no", ("购买方税号", "buyer_tax_no")),
        ):
            if column in wanted and not _nonempty(rec[column]):
                for key in keys:
                    value = evidence.fields.get(key)
                    if _nonempty(value):
                        values[column] = str(value).strip()
                        break
        if not values:
            plan.unchanged.append("发票 id=%d %s：无可回补内容" % (invoice_id, invoice_no))
            continue

        planned_hashes[digest] = invoice_id
        plan.links.append(
            LinkPlan(
                invoice_id=invoice_id,
                invoice_no=invoice_no,
                fields=values,
                source_path=evidence.source_path,
                target_path=target,
                file_hash=digest,
                origin=evidence.origin,
            )
        )

    return plan


# ---------------------------------------------------------------------------
# 执行层
# ---------------------------------------------------------------------------
CHECK_TABLES = ("invoice_doc", "company", "audit_event", "voucher", "voucher_line")


def backup_database(db_path: Path, *, when: datetime | None = None) -> Path:
    """在线备份数据库到同级 ``backups/``，并做完整性 + 行数校验。

    生产库运行在 WAL 模式（``db/finance.db-wal``），直接 ``shutil.copy2`` 有可能
    连同未 checkpoint 的页一起拷走、拿到不一致的快照。这里改用 SQLite 的在线
    备份 API，它在事务视角下产出一份自洽的副本。
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError("数据库不存在：%s" % db_path)
    when = when or datetime.now()
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / ("finance.db.backfill-%s.bak" % when.strftime("%Y%m%d-%H%M%S"))

    source = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        dest = sqlite3.connect(str(target))
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()

    # 校验：备份必须可打开、完整，且关键表行数与原库一致
    check = sqlite3.connect(str(target))
    try:
        status = check.execute("PRAGMA integrity_check").fetchone()[0]
        if status != "ok":
            raise OSError("备份完整性校验未通过：%s" % status)
        original = sqlite3.connect(str(db_path), timeout=30.0)
        try:
            for table in CHECK_TABLES:
                before = original.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                after = check.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
                if before != after:
                    raise OSError(
                        "备份行数不一致：%s %d != %d" % (table, before, after)
                    )
        finally:
            original.close()
    finally:
        check.close()
    return target


def apply_plan(
    conn,
    plan: Plan,
    *,
    actor: str,
    copy_files: bool = True,
    force_company: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """执行方案：先落地归档文件（保证库里的路径一定存在），再在一个事务里写库。"""
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    result: dict[str, Any] = {
        "copied": [],
        "linked": [],
        "company_changed": [],
        "skipped": [],
        "warnings": [],
    }

    # 0) 执行前重新核对：方案生成到真正落盘之间，列可能已被别处（人工 / 另一次运行）填上。
    #    先把这些列剔掉，避免既覆盖别人写的值、又在磁盘上留下没人引用的孤儿归档文件。
    effective: list[LinkPlan] = []
    for link in plan.links:
        current = conn.execute(
            "SELECT file_path, file_hash, seller_tax_no, buyer_tax_no "
            "FROM invoice_doc WHERE id=?",
            (link.invoice_id,),
        ).fetchone()
        if current is None:
            result["warnings"].append("发票 id=%d 已不存在，已跳过" % link.invoice_id)
            continue
        kept = {
            column: value
            for column, value in link.fields.items()
            if column in INVOICE_FIELDS and not _nonempty(current[column])
        }
        if not kept:
            result["skipped"].append("发票 id=%d：目标列已被填充，跳过" % link.invoice_id)
            continue
        link.fields = kept
        effective.append(link)

    # 1) 文件先落地（保证库里的路径一定指向真实存在的文件）
    if copy_files:
        for link in effective:
            if "file_path" not in link.fields:
                continue
            target = Path(link.target_path)
            try:
                if target.is_file() and sha256_file(target) == link.file_hash:
                    result["copied"].append(str(target) + "（已存在，复用）")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(link.source_path, target)
                if sha256_file(target) != link.file_hash:
                    raise OSError("归档后哈希不一致")
                result["copied"].append(str(target))
            except OSError as exc:
                result["warnings"].append("归档失败，已跳过 %s：%s" % (link.invoice_no, exc))
                link.fields.pop("file_path", None)
                link.fields.pop("file_hash", None)

    # 2) 写库
    with dbmod.transaction(conn) as tx:
        for link in effective:
            values = {k: v for k, v in link.fields.items() if k in INVOICE_FIELDS}
            if not values:
                continue
            # 双保险：即使上面刚核对过，SQL 里再要求目标列仍为空，绝不覆盖已有值
            guards = [
                "(%s IS NULL OR TRIM(%s)='')" % (column, column) for column in values
            ]
            assignments = ", ".join("%s=?" % column for column in values)
            sql = (
                "UPDATE invoice_doc SET %s, updated_at=? WHERE id=? AND %s"
                % (assignments, " AND ".join(guards))
            )
            cursor = tx.execute(sql, (*values.values(), stamp, link.invoice_id))
            if cursor.rowcount == 0:
                result["skipped"].append(
                    "发票 id=%d：目标列已被填充，跳过、未记审计" % link.invoice_id
                )
                continue
            dbmod.append_audit_event(
                tx,
                "invoice_doc",
                link.invoice_id,
                "invoice_file_linked",
                actor,
                {
                    "invoice_no": link.invoice_no,
                    "fields": values,
                    "source_path": str(link.source_path),
                    "file_hash": link.file_hash,
                    "origin": link.origin,
                    "tool": "backfill_business_data",
                },
            )
            result["linked"].append({"id": link.invoice_id, "fields": values})

        desired = plan.company.changes(plan.company_current) if not force_company else {
            key: (str(plan.company_current.get(key) or ""), getattr(plan.company, key))
            for key in COMPANY_FIELDS
            if getattr(plan.company, key)
        }
        if desired:
            if not plan.company_row_exists:
                tx.execute(
                    "INSERT INTO company(id, name, tax_no, addr, note) VALUES (1,?,?,?,?)",
                    tuple(plan.company.to_dict()[key] or None for key in COMPANY_FIELDS),
                )
                dbmod.append_audit_event(
                    tx, "company", 1, "company_created", actor,
                    {key: plan.company.to_dict()[key] for key in COMPANY_FIELDS},
                )
                result["company_changed"] = list(desired)
            else:
                for key, (old, new) in desired.items():
                    tx.execute("UPDATE company SET %s=? WHERE id=1" % key, (new,))
                    dbmod.append_audit_event(
                        tx, "company", 1, "company_field_updated", actor,
                        {"field": key, "old": old, "new": new, "tool": "backfill_business_data"},
                    )
                result["company_changed"] = list(desired)
    return result


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def render_plan(plan: Plan, db_path: Path) -> str:
    lines: list[str] = []
    lines.append("数据库：%s" % db_path)
    lines.append("")
    lines.append("── 公司抬头 ──")
    if not plan.company_row_exists:
        lines.append("  company 表无 id=1 行，将新建。")
    for key in COMPANY_FIELDS:
        current = plan.company_current.get(key)
        current = "(空)" if not _nonempty(current) else str(current)
        lines.append("  %-8s 现值：%s" % (key, current))
    desired = plan.company.to_dict()
    provided = {k: v for k, v in desired.items() if v}
    if provided:
        lines.append("  计划写入：")
        for key, value in provided.items():
            lines.append("    %-8s -> %s" % (key, value))
    else:
        lines.append("  未提供 --company-* 参数，本次不改公司抬头。")
    lines.append("")
    lines.append("── 发票原始凭证关联 ──")
    if plan.links:
        for link in plan.links:
            lines.append("  [id=%d] %s（证据来源：%s）" % (link.invoice_id, link.invoice_no, link.origin))
            for key, value in link.fields.items():
                lines.append("      %-14s = %s" % (key, value))
            if link.target_path != link.source_path:
                lines.append("      归档：%s" % link.target_path)
    else:
        lines.append("  无可关联项。")
    if plan.unchanged:
        lines.append("")
        lines.append("── 无需处理（%d）──" % len(plan.unchanged))
        for item in plan.unchanged:
            lines.append("  · " + item)
    if plan.unresolved:
        lines.append("")
        lines.append("── 无法关联（%d）──" % len(plan.unresolved))
        for item in plan.unresolved:
            lines.append("  · " + item)
    if plan.conflicts:
        lines.append("")
        lines.append("── ⚠ 冲突，已拒绝（%d）──" % len(plan.conflicts))
        for item in plan.conflicts:
            lines.append("  · " + item)
    if plan.ignored_files:
        lines.append("")
        lines.append("── 忽略的文件（%d）──" % len(plan.ignored_files))
        for item in plan.ignored_files:
            lines.append("  · " + item)
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="业务数据回补：公司抬头 + 发票原始凭证（file_path/file_hash）。默认只读。",
    )
    parser.add_argument("--db", default=None, help="目标数据库，默认取 app_paths.get_db_path()")
    parser.add_argument("--apply", action="store_true", help="真正写库（会先自动备份）")
    parser.add_argument("--fields", default=DEFAULT_FIELDS, help="要回补的 invoice_doc 列，逗号分隔")
    parser.add_argument("--actor", default="li1", help="审计事件的操作者标识")
    parser.add_argument("--agent-root", default=str(DEFAULT_AGENT_ROOT), help="发票识别 agent 根目录")
    parser.add_argument("--ocr-python", default=str(DEFAULT_OCR_PYTHON), help="识别 agent 所用解释器")
    parser.add_argument("--ocr-script", default=None, help="ocr_invoice.py 路径")
    parser.add_argument("--archive-dir", default=None, help="归档目录，默认 <db目录>/../invoices/archive")
    parser.add_argument("--no-copy", action="store_true", help="不复制文件，直接把 file_path 指向原位置")
    parser.add_argument("--force-company", action="store_true", help="公司抬头非空时也覆盖")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--company-name", default="", help="公司名称")
    parser.add_argument("--company-tax-no", default="", help="统一社会信用代码")
    parser.add_argument("--company-addr", default="", help="注册地址")
    parser.add_argument("--company-note", default="", help="备注")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    db_path = Path(args.db).resolve() if args.db else Path(get_db_path()).resolve()
    if not db_path.is_file():
        print("数据库不存在：%s" % db_path, file=sys.stderr)
        return 2

    fields = [name.strip() for name in args.fields.split(",") if name.strip()]
    unknown = [name for name in fields if name not in INVOICE_FIELDS]
    if unknown:
        print("不支持的列：%s（可选：%s）" % (", ".join(unknown), ", ".join(INVOICE_FIELDS)), file=sys.stderr)
        return 2

    agent_root = Path(args.agent_root)
    ocr_script = Path(args.ocr_script) if args.ocr_script else agent_root / "ocr_invoice.py"
    archive_dir = (
        Path(args.archive_dir).resolve()
        if args.archive_dir
        else (db_path.parent.parent / "invoices" / "archive").resolve()
    )

    # ---- 收集证据 ----
    evidence = evidence_from_agent_results(agent_root / "results", agent_root / "invoices")
    failures: list[str] = []

    scanned_dirs: list[Path] = []
    for candidate in (db_path.parent.parent / "invoices" / "failed",):
        if candidate.is_dir():
            scanned_dirs.append(candidate)
    runner = make_ocr_runner(Path(args.ocr_python), ocr_script)
    for directory in scanned_dirs:
        extra, fails = evidence_from_ocr_calls(sorted(directory.glob("*.pdf")), runner)
        evidence.extend(extra)
        failures.extend(fails)

    evidence_by_no = index_evidence(evidence)

    # ---- 出方案 ----
    conn = dbmod.get_conn(db_path)
    try:
        plan = build_plan(
            conn,
            evidence_by_no,
            fields=fields,
            company=CompanyPlan(
                name=args.company_name.strip(),
                tax_no=args.company_tax_no.strip(),
                addr=args.company_addr.strip(),
                note=args.company_note.strip(),
            ),
            archive_dir=archive_dir,
            copy_files=not args.no_copy,
        )
    finally:
        conn.close()
    plan.ignored_files.extend(failures)

    if args.json:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_plan(plan, db_path))
        print("")
        print(
            "证据：ocr_result.json %d 条 + 单文件识别 %d 条 = %d 条"
            % (
                sum(1 for e in evidence if e.origin == "ocr_result.json"),
                sum(1 for e in evidence if e.origin != "ocr_result.json"),
                len(evidence),
            )
        )

    if not args.apply:
        print("")
        print("[只读模式] 未写入任何内容。确认无误后加 --apply 执行。")
        return 0

    if not plan.links and not plan.company.changes(plan.company_current):
        print("")
        print("没有需要写入的内容，已跳过。")
        return 0

    backup = backup_database(db_path)
    print("")
    print("已备份数据库 -> %s" % backup)

    conn = dbmod.get_conn(db_path)
    try:
        outcome = apply_plan(
            conn,
            plan,
            actor=args.actor,
            copy_files=not args.no_copy,
            force_company=args.force_company,
        )
    finally:
        conn.close()

    print("")
    print("── 执行结果 ──")
    print("  归档文件：%d" % len(outcome["copied"]))
    for item in outcome["copied"]:
        print("    · " + item)
    print("  发票关联：%d" % len(outcome["linked"]))
    for item in outcome["linked"]:
        print("    · id=%s %s" % (item["id"], item["fields"]))
    if outcome["skipped"]:
        print("  已跳过：%d" % len(outcome["skipped"]))
        for item in outcome["skipped"]:
            print("    · " + item)
    print("  公司抬头：%s" % (", ".join(outcome["company_changed"]) or "无改动"))
    if outcome["warnings"]:
        print("  ⚠ 警告：")
        for item in outcome["warnings"]:
            print("    · " + item)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
