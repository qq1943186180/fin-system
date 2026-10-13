# -*- coding: utf-8 -*-
"""回滚误写入生产库的飞书拉取行（默认只读预览，--apply 才写）。

背景：2026-09-26 17:35 诊断脚本因模块级 from db import 快照问题，
把 2 行飞书单写进了生产库而非副本：
    id=8  2026-09-0003  instance_code=C80BDD3F-F82A-481B-97BC-A206FB59C8EF
    id=9  2026-09-0004  instance_code=386F9F76-B033-4D88-8160-43D8D5891D4E

设计要点（照抄 8 层安全设计）：
  1. 默认只读，必须显式 --apply
  2. 在线备份（WAL 安全）
  3. BEGIN IMMEDIATE + 写前复查
  4. 只删「指定 instance_code」且 status 仍是 approved 的行
  5. 已挂 voucher_id 的拒绝删（会破坏账务平衡）
  6. 写后自校验行数
  7. 🔴 **不删 audit_event，改为补一条反向审计事件**（2026-10-14 改）
     ``audit_event`` 有 ``audit_event_no_delete`` 触发器，全库强制 append-only
     —— 这是正确的审计纪律（见 sme-finance-desktop 技能「清理残留」：
     成对的东西要么一起留、要么一起走，默认一起留）。所以不清历史，
     而是追加一条 ``expense_pull_rolled_back`` 说明这两行是误写并已作废，
     既清了账，又保留完整痕迹。
"""
import json
import os
import sys
import argparse
import datetime as _dt
import sqlite3

TARGETS = [
    "C80BDD3F-F82A-481B-97BC-A206FB59C8EF",
    "386F9F76-B033-4D88-8160-43D8D5891D4E",
]

DB = os.path.expandvars(r"%LOCALAPPDATA%\FinanceSystem\db\finance.db")


def again_row(conn, eid):
    """取一行报销单的原始内容，用于写反向审计事件。"""
    try:
        r = conn.execute(
            "SELECT no,instance_code,amount_cents,summary FROM expense WHERE id=?", (eid,)
        ).fetchone()
        return {"no": r[0], "instance_code": r[1], "amount_cents": r[2], "summary": r[3]} if r else None
    except sqlite3.Error:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的执行删除（默认只读）")
    args = ap.parse_args()

    print("目标库: %s" % DB)
    print("模式  : %s" % ("APPLY（会写库）" if args.apply else "只读预览"))
    print()

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        print("=== 待处理行 ===")
        rows = []
        for code in TARGETS:
            r = conn.execute(
                "SELECT id,no,status,amount_cents,instance_code,voucher_id,created_at "
                "FROM expense WHERE instance_code=?", (code,)
            ).fetchone()
            if r:
                rows.append(r)
                print("  id=%s no=%s status=%s amt=%.2f voucher_id=%s created=%s"
                      % (r[0], r[1], r[2], (r[3] or 0) / 100.0, r[5], r[6]))
            else:
                print("  (未找到 %s)" % code)

        if not rows:
            print("\n没有需要处理的行。")
            return

        # 安全闸门
        blocked = []
        for r in rows:
            if r[5]:
                blocked.append((r[0], "已挂凭证 voucher_id=%s，删除会破坏账务" % r[5]))
            if r[2] not in ("approved",):
                blocked.append((r[0], "状态是 %s，非 approved，不在预期范围" % r[2]))
        if blocked:
            print("\n🔴 拒绝执行：")
            for bid, why in blocked:
                print("   id=%s: %s" % (bid, why))
            return

        ids = [r[0] for r in rows]
        print("\n=== 连带删除的 expense_item ===")
        for i in ids:
            items = list(conn.execute(
                "SELECT id,account_code,summary,amount_cents FROM expense_item WHERE expense_id=?", (i,)))
            for it in items:
                print("  expense_id=%s item_id=%s 科目=%s 摘要=%r %.2f"
                      % (i, it[0], it[1], it[2], (it[3] or 0) / 100.0))
            if not items:
                print("  expense_id=%s (无明细)" % i)

        print("\n=== 关联的审计事件（保留，改为追加反向记录）===")
        for i in ids:
            ev = list(conn.execute(
                "SELECT id,event_type,actor FROM audit_event WHERE entity_type='expense' AND entity_id=?", (i,)))
            for e in ev:
                print("  id=%s type=%s actor=%s（append-only，不删）" % e)

        print("\n=== 影响面 ===")
        tot = list(conn.execute("SELECT COUNT(*),COALESCE(SUM(amount_cents),0) FROM expense"))[0]
        print("  删除前：expense %d 行，合计 %.2f" % (tot[0], tot[1] / 100.0))
        print("  删除后：expense %d 行，合计 %.2f" % (tot[0] - len(ids), (tot[1] - sum(r[3] or 0 for r in rows)) / 100.0))
        print("  审计事件：不删（append-only），改为为每行追加一条 expense_pull_rolled_back")

        if not args.apply:
            print("\n（只读模式，未做任何修改。加 --apply 才执行）")
            return

        # ---- 在线备份（WAL 安全）----
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = os.path.join(os.path.dirname(DB), "..", "backups", "finance.del-pull-%s.db" % stamp)
        bak = os.path.abspath(bak)
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        dest = sqlite3.connect(bak)
        with dest:
            conn.backup(dest)
        dest.close()
        print("\n[备份] -> %s" % bak)

        # ---- 执行 ----
        try:
            conn.execute("BEGIN IMMEDIATE")
            for i in ids:
                again = conn.execute("SELECT id,status,voucher_id FROM expense WHERE id=?", (i,)).fetchone()
                if not again:
                    raise RuntimeError("TOCTOU: id=%s 已消失" % i)
                if again[2]:
                    raise RuntimeError("TOCTOU: id=%s 已挂凭证 %s" % (i, again[2]))
                if again[1] != "approved":
                    raise RuntimeError("TOCTOU: id=%s 状态变了 -> %s" % (i, again[1]))
            for i in ids:
                # 先留痕：在删行之前把原始内容写进反向审计事件（行删了就取不到了）
                r = again_row(conn, i)
                conn.execute(
                    "INSERT INTO audit_event(entity_type,entity_id,event_type,actor,details_json,created_at) "
                    "VALUES('expense',?,?,?,?,?)",
                    (
                        i,
                        "expense_pull_rolled_back",
                        "rollback_pulled_expenses.py",
                        json.dumps(
                            {
                                "reason": "诊断脚本误写生产库（2026-09-26 17:35），非真实业务单据",
                                "no": r["no"] if r else None,
                                "instance_code": r["instance_code"] if r else None,
                                "amount_cents": (r["amount_cents"] if r else None),
                                "summary": r["summary"] if r else None,
                            },
                            ensure_ascii=False,
                        ),
                        _dt.datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                conn.execute("DELETE FROM expense_item WHERE expense_id=?", (i,))
                conn.execute("DELETE FROM expense WHERE id=?", (i,))
            conn.commit()
        except Exception as exc:
            conn.rollback()
            print("🔴 已回滚：%s" % exc)
            return

        # ---- 自校验 ----
        after = list(conn.execute("SELECT COUNT(*),COALESCE(SUM(amount_cents),0) FROM expense"))[0]
        left = [r for r in conn.execute(
            "SELECT id FROM expense WHERE instance_code IN (%s)"
            % ",".join("?" * len(TARGETS)), TARGETS)]
        marks = list(conn.execute(
            "SELECT id FROM audit_event WHERE event_type='expense_pull_rolled_back'"))
        print("\n[结果] expense %d 行，合计 %.2f" % (after[0], after[1] / 100.0))
        print("[校验] 目标行残留 = %d（应为 0）%s" % (len(left), "OK" if not left else "!! 异常"))
        print("[校验] 反向审计事件 = %d 条（应为 %d）%s"
              % (len(marks), len(ids), "OK" if len(marks) >= len(ids) else "!! 异常"))
        qc = [r[0] for r in conn.execute("PRAGMA quick_check")]
        print("[校验] quick_check = %s" % qc)
        integ = [r[0] for r in conn.execute("PRAGMA foreign_key_check")]
        print("[校验] foreign_key_check = %s" % ("OK" if not integ else integ))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
