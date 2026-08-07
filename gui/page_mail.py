# -*- coding: utf-8 -*-
"""邮件收票页：多邮箱配置 + 手动收件 + 收件记录 + 系统托盘。"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from tkinter import ttk, messagebox, Frame, Label, Button
import tkinter as tk

from modules.mail_service import MailBoxConfig
from modules.mail_sync import MailSyncEngine
from modules.mail_processor import process_attachment
from modules.integration import IntegrationStatus
from gui.ui_theme import (
    C_BG, C_CARD, C_BORDER, C_TEXT, C_TEXT_MUTED, C_ACCENT,
    FONT, FONT_SMALL, FONT_B, FONT_H2, FONT_H3,
    CardFrame, ModernButton, configure_dark_style,
)

try:
    from logging_setup import get_logger

    LOG = get_logger("gui.mail")
except ImportError:
    LOG = logging.getLogger(__name__)


class MailPage(Frame):
    REF = "mail"

    def __init__(self, master, app, **kw):
        kw.setdefault("bg", C_BG)
        super().__init__(master, **kw)
        self.app = app
        self._configs: list[MailBoxConfig] = []
        self._sync: MailSyncEngine | None = None
        self._records: list[dict] = []
        self._build_ui()
        self._load_configs()
        self._start_sync()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # 顶栏
        top = Frame(self, bg=C_BG)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(16, 8))
        top.grid_columnconfigure(1, weight=1)

        Label(top, text="📬 邮件收票", font=FONT_H2, bg=C_BG, fg=C_TEXT).grid(
            row=0, column=0, sticky="w"
        )

        btns = Frame(top, bg=C_BG)
        btns.grid(row=0, column=1, sticky="e")
        ModernButton(btns, text="+ 新增", variant="ghost", font=FONT_SMALL, height=28,
                     command=self._on_add).pack(side="left", padx=4)
        ModernButton(btns, text="编辑", variant="ghost", font=FONT_SMALL, height=28,
                     command=self._on_edit).pack(side="left", padx=4)
        ModernButton(btns, text="删除", variant="ghost", font=FONT_SMALL, height=28,
                     command=self._on_delete).pack(side="left", padx=4)
        ModernButton(btns, text="🔄 立即收件", variant="primary", font=FONT_SMALL, height=28,
                     command=self._on_fetch).pack(side="left", padx=4)

        # 主体：左列表 + 右记录
        body = Frame(self, bg=C_BG)
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 16))
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # 左侧：邮箱列表
        left = CardFrame(body, radius=12, bg=C_CARD, border=C_BORDER, padx=10, pady=10)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 10))
        left.grid_rowconfigure(0, weight=1)

        cols = ("name", "address", "status", "last_sync")
        self._tree = ttk.Treeview(left.inner, columns=cols, show="headings", height=22)
        self._tree.heading("name", text="名称")
        self._tree.heading("address", text="邮箱地址")
        self._tree.heading("status", text="状态")
        self._tree.heading("last_sync", text="最后同步")
        self._tree.column("name", width=130)
        self._tree.column("address", width=200)
        self._tree.column("status", width=60)
        self._tree.column("last_sync", width=120)
        self._tree.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # 右侧：收件记录
        right = CardFrame(body, radius=12, bg=C_CARD, border=C_BORDER, padx=12, pady=12)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_columnconfigure(0, weight=1)

        Label(right.inner, text="最近收件记录", font=FONT_H3, bg=C_CARD, fg=C_TEXT).grid(
            row=0, column=0, sticky="w", padx=4, pady=(0, 8)
        )

        cols2 = ("time", "box", "filename", "subject", "status")
        self._record_tree = ttk.Treeview(right.inner, columns=cols2, show="headings", height=22)
        self._record_tree.heading("time", text="时间")
        self._record_tree.heading("box", text="邮箱")
        self._record_tree.heading("filename", text="文件名")
        self._record_tree.heading("subject", text="主题")
        self._record_tree.heading("status", text="状态")
        for col, w in zip(cols2, [110, 90, 180, 220, 80]):
            self._record_tree.column(col, width=w)
        self._record_tree.grid(row=1, column=0, sticky="nsew", padx=4, pady=(0, 4))

        # 状态栏
        self._status_lbl = Label(self, text="就绪", anchor="w", bg=C_BG, fg=C_TEXT_MUTED, font=FONT)
        self._status_lbl.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 12))

    # ------------------------------------------------------------------
    # 配置加载/保存
    # ------------------------------------------------------------------
    def _load_configs(self):
        try:
            from app_paths import CONFIG_DIR

            path = Path(CONFIG_DIR) / "settings.json"
        except Exception:
            path = Path(__file__).resolve().parents[2] / "config" / "settings.json"
        if not path.exists():
            self._refresh_tree()
            return
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
            raw = settings.get("mail_boxes", [])
            self._configs = [MailBoxConfig.from_dict(d) for d in raw if isinstance(d, dict)]
        except Exception:
            LOG.exception("加载邮件配置失败")
        self._refresh_tree()

    def _save_configs(self):
        try:
            from app_paths import CONFIG_DIR

            path = Path(CONFIG_DIR) / "settings.json"
        except Exception:
            path = Path(__file__).resolve().parents[2] / "config" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        settings: dict = {}
        if path.exists():
            try:
                settings = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        settings["mail_boxes"] = [c.to_dict() for c in self._configs]
        settings["mail_poll_interval_minutes"] = settings.get("mail_poll_interval_minutes", 30)
        path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def _refresh_tree(self):
        for i in self._tree.get_children():
            self._tree.delete(i)
        for c in self._configs:
            self._tree.insert(
                "",
                "end",
                values=(
                    c.name,
                    c.username,
                    "启用" if c.enabled else "禁用",
                    f"UID {c.last_uid}",
                ),
                tags=(c.username,),
            )

    # ------------------------------------------------------------------
    # 同步引擎
    # ------------------------------------------------------------------
    def _config_provider(self):
        return self._configs

    def _update_box(self, updated: MailBoxConfig):
        for i, c in enumerate(self._configs):
            if c.username == updated.username:
                self._configs[i] = updated
                break
        self._save_configs()
        self._refresh_tree()

    def _start_sync(self):
        if self._sync is None:
            self._sync = MailSyncEngine(self._config_provider, poll_interval_minutes=30)
            try:
                interval = 30
                try:
                    from app_paths import CONFIG_DIR

                    path = Path(CONFIG_DIR) / "settings.json"
                    if path.exists():
                        settings = json.loads(path.read_text(encoding="utf-8"))
                        interval = int(settings.get("mail_poll_interval_minutes", 30))
                except Exception:
                    pass
                self._sync._poll_interval = max(60, interval * 60)
            except Exception:
                pass
            self._sync.start()

    def _get_poll_interval(self) -> int:
        try:
            from app_paths import CONFIG_DIR

            path = Path(CONFIG_DIR) / "settings.json"
            if path.exists():
                settings = json.loads(path.read_text(encoding="utf-8"))
                return int(settings.get("mail_poll_interval_minutes", 30))
        except Exception:
            pass
        return 30

    # ------------------------------------------------------------------
    # 按钮事件
    # ------------------------------------------------------------------
    def _on_add(self):
        dlg = MailBoxEditDialog(self, None)
        self.wait_window(dlg)
        if dlg.result:
            self._configs.append(dlg.result)
            self._save_configs()
            self._refresh_tree()
            self._status_lbl.configure(text=f"已新增邮箱: {dlg.result.name}")

    def _on_edit(self):
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个邮箱。")
            return
        item = self._tree.item(sel[0])
        addr = item["values"][1]
        cfg = next((c for c in self._configs if c.username == addr), None)
        if not cfg:
            return
        dlg = MailBoxEditDialog(self, cfg)
        self.wait_window(dlg)
        if dlg.result:
            self._save_configs()
            self._refresh_tree()
            self._status_lbl.configure(text=f"已更新: {dlg.result.name}")

    def _on_delete(self):
        sel = self._tree.selection()
        if not sel:
            return
        item = self._tree.item(sel[0])
        addr = item["values"][1]
        if not messagebox.askyesno("确认", f"删除邮箱 {addr}？"):
            return
        self._configs = [c for c in self._configs if c.username != addr]
        self._save_configs()
        self._refresh_tree()

    def _on_fetch(self):
        self._status_lbl.configure(text="正在收件...")
        self.update_idletasks()
        try:
            if self._sync:
                summary = self._sync.run_once()
                self._append_records(summary)
                boxes = summary.get("boxes", [])
                msgs = [f"{b['name']}: {len(b.get('attachments', []))} 附件" for b in boxes]
                self._status_lbl.configure(text=" | ".join(msgs) if msgs else "无新邮件")
            else:
                self._status_lbl.configure(text="同步引擎未启动")
        except Exception:
            LOG.exception("手动收件失败")
            self._status_lbl.configure(text="收件失败，请查看日志。")

    def _append_records(self, summary: dict):
        import datetime

        for box in summary.get("boxes", []):
            for att in box.get("attachments", []):
                self._records.insert(
                    0,
                    {
                        "time": datetime.datetime.now().strftime("%H:%M:%S"),
                        "box": box.get("name", ""),
                        "filename": att.get("filename", ""),
                        "subject": (att.get("subject", "") or "")[:50],
                        "status": "待 OCR",
                    },
                )
        self._records = self._records[:200]
        for i in self._record_tree.get_children():
            self._record_tree.delete(i)
        for r in self._records:
            self._record_tree.insert("", "end", values=tuple(r.values()))


class MailBoxEditDialog(tk.Toplevel):
    def __init__(self, master, cfg: MailBoxConfig | None):
        super().__init__(master)
        self.title("邮箱配置" if cfg is None else "编辑邮箱")
        self.result: MailBoxConfig | None = None
        self._cfg = cfg
        self.configure(bg=C_BG)
        self._build_form()
        if cfg:
            self._fill(cfg)
        self.grab_set()
        self.geometry("520x620")

    def _build_form(self):
        self.grid_columnconfigure(1, weight=1)
        pad = {"padx": 12, "pady": 6}

        fields = [
            ("名称", "name"),
            ("IMAP 服务器", "imap_host"),
            ("IMAP 端口", "imap_port"),
            ("用户名", "username"),
            ("密码", "password"),
            ("文件夹", "folder"),
            ("主题过滤（| 分隔）", "subject_filter"),
            ("主题正则（可选）", "subject_regex"),
            ("发件人白名单（| 分隔，可选）", "sender_whitelist"),
            ("附件大小上限 MB", "max_size_mb"),
            ("SMTP 服务器", "smtp_host"),
            ("SMTP 端口", "smtp_port"),
        ]
        self._entries: dict[str, tk.Entry] = {}
        for i, (label, key) in enumerate(fields):
            Label(self, text=label, bg=C_BG, fg=C_TEXT, font=FONT).grid(row=i, column=0, sticky="w", **pad)
            show = "*" if key == "password" else None
            entry = tk.Entry(self, width=40, show=show, bg=C_CARD, fg=C_TEXT,
                             insertbackground=C_TEXT, highlightthickness=1,
                             highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
            entry.grid(row=i, column=1, sticky="ew", **pad)
            self._entries[key] = entry

        self._enabled_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="启用", variable=self._enabled_var,
                       bg=C_BG, fg=C_TEXT, selectcolor=C_CARD,
                       activebackground=C_BG, activeforeground=C_TEXT).grid(
            row=len(fields), column=0, columnspan=2, sticky="w", **pad
        )

        btns = Frame(self, bg=C_BG)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, pady=12)
        ModernButton(btns, text="保存", variant="primary", font=FONT_SMALL, height=28,
                     command=self._on_save).pack(side="left", padx=8)
        ModernButton(btns, text="取消", variant="ghost", font=FONT_SMALL, height=28,
                     command=self.destroy).pack(side="left", padx=8)

    def _fill(self, cfg: MailBoxConfig):
        mapping = {
            "name": cfg.name,
            "imap_host": cfg.imap_host,
            "imap_port": str(cfg.imap_port),
            "username": cfg.username,
            "password": cfg.password,
            "folder": cfg.folder,
            "subject_filter": cfg.subject_filter,
            "smtp_host": cfg.smtp_host,
            "smtp_port": str(cfg.smtp_port),
        }
        for key, val in mapping.items():
            if key in self._entries:
                self._entries[key].delete(0, "end")
                self._entries[key].insert(0, val)
        self._enabled_var.set(cfg.enabled)

    def _on_save(self):
        data = {k: e.get().strip() for k, e in self._entries.items()}
        try:
            data["imap_port"] = int(data.get("imap_port", 993) or 993)
            data["smtp_port"] = int(data.get("smtp_port", 465) or 465)
        except ValueError:
            messagebox.showerror("错误", "端口必须是数字。")
            return
        if not data.get("imap_host") or not data.get("username"):
            messagebox.showerror("错误", "IMAP 服务器和用户名必填。")
            return
        data["enabled"] = self._enabled_var.get()
        pwd = data.get("password", "")
        secret_name = f"mail_box.password.{data['username'].replace('@', '_at_').replace('.', '_')}"
        if pwd:
            from security import set_secret

            set_secret(secret_name, pwd)
        try:
            self.result = MailBoxConfig.from_dict(data)
        except Exception as exc:
            messagebox.showerror("错误", f"配置无效: {exc}")
            return
        self.destroy()
