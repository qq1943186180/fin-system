# -*- coding: utf-8 -*-
"""邮件收票页：多邮箱配置 + 手动收件 + 收件记录 + 系统托盘。"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from tkinter import ttk, messagebox, Frame, Label, Button
import tkinter as tk

from modules.mail_service import MailBoxConfig, MAIL_PROVIDER_PRESETS, detect_provider, apply_preset_to_dict
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
        # 纯读取：返回当前邮箱配置列表
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
            # mail_sync._persist_last_uid 经 provider.update_box(cfg) 回写水位线；
            # bound method 不能挂属性，用普通函数对象承载 update_box。
            provider = lambda: self._configs  # noqa: E731
            provider.update_box = self._update_box  # type: ignore[attr-defined]
            self._sync = MailSyncEngine(provider, poll_interval_minutes=30)
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
        self._advanced = False
        self._build_form()
        if cfg:
            self._fill(cfg)
        self.grab_set()
        self.geometry("520x320")

    def _build_form(self):
        pad = {"padx": 16, "pady": 6}
        self.grid_columnconfigure(1, weight=1)

        # ---- 基础区 ----
        # 服务商
        Label(self, text="服务商", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=0, column=0, sticky="w", **pad)
        self._provider_var = tk.StringVar(value="自定义")
        providers = list(MAIL_PROVIDER_PRESETS.keys())
        self._provider_cb = tk.OptionMenu(self, self._provider_var, *providers,
                                          command=lambda _=None: self._on_provider_change())
        self._provider_cb.config(width=18, bg=C_CARD, fg=C_TEXT, highlightthickness=1,
                                 highlightbackground=C_BORDER)
        self._provider_cb.grid(row=0, column=1, sticky="ew", **pad)

        # 邮箱地址（同时作用户名）
        Label(self, text="邮箱地址", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=1, column=0, sticky="w", **pad)
        self._username_entry = tk.Entry(self, width=40, bg=C_CARD, fg=C_TEXT,
                                        insertbackground=C_TEXT, highlightthickness=1,
                                        highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
        self._username_entry.grid(row=1, column=1, sticky="ew", **pad)
        self._username_entry.bind("<FocusOut>", lambda e: self._on_username_changed())

        # 密码
        Label(self, text="密码/授权码", bg=C_BG, fg=C_TEXT, font=FONT).grid(row=2, column=0, sticky="w", **pad)
        self._password_entry = tk.Entry(self, width=40, show="*", bg=C_CARD, fg=C_TEXT,
                                        insertbackground=C_TEXT, highlightthickness=1,
                                        highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
        self._password_entry.grid(row=2, column=1, sticky="ew", **pad)

        # 高级切换
        self._adv_frame = tk.Frame(self, bg=C_BG)
        self._adv_btn = ModernButton(self, text="▼ 高级设置", variant="ghost", font=FONT_SMALL,
                                     height=26, command=self._toggle_advanced)
        self._adv_btn.grid(row=3, column=0, columnspan=2, sticky="w", **pad)

        # 高级区（默认隐藏）
        self._advanced_widgets: list[tk.Widget] = []
        row = 4
        advanced_fields = [
            ("IMAP 服务器", "imap_host"),
            ("IMAP 端口", "imap_port"),
            ("SMTP 服务器", "smtp_host"),
            ("SMTP 端口", "smtp_port"),
            ("文件夹", "folder"),
            ("主题过滤（| 分隔）", "subject_filter"),
            ("主题正则（可选）", "subject_regex"),
            ("发件人白名单（| 分隔）", "sender_whitelist"),
            ("附件大小上限 MB", "max_size_mb"),
        ]
        self._entries: dict[str, tk.Entry] = {}
        for label, key in advanced_fields:
            lbl = Label(self, text=label, bg=C_BG, fg=C_TEXT, font=FONT)
            lbl.grid(row=row, column=0, sticky="w", **pad)
            entry = tk.Entry(self, width=40, bg=C_CARD, fg=C_TEXT,
                             insertbackground=C_TEXT, highlightthickness=1,
                             highlightbackground=C_BORDER, highlightcolor=C_ACCENT)
            entry.grid(row=row, column=1, sticky="ew", **pad)
            self._entries[key] = entry
            self._advanced_widgets.extend([lbl, entry])
            row += 1

        self._enabled_var = tk.BooleanVar(value=True)
        chk = tk.Checkbutton(self, text="启用", variable=self._enabled_var,
                             bg=C_BG, fg=C_TEXT, selectcolor=C_CARD,
                             activebackground=C_BG, activeforeground=C_TEXT)
        chk.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        self._advanced_widgets.append(chk)
        row += 1

        btns = Frame(self, bg=C_BG)
        btns.grid(row=row, column=0, columnspan=2, pady=12)
        ModernButton(btns, text="保存", variant="primary", font=FONT_SMALL, height=28,
                     command=self._on_save).pack(side="left", padx=8)
        ModernButton(btns, text="取消", variant="ghost", font=FONT_SMALL, height=28,
                     command=self.destroy).pack(side="left", padx=8)

        self._show_advanced(False)

    def _toggle_advanced(self):
        self._advanced = not self._advanced
        self._show_advanced(self._advanced)
        self._adv_btn.configure(text="▲ 收起高级设置" if self._advanced else "▼ 高级设置")
        self.geometry("520x620" if self._advanced else "520x320")

    def _show_advanced(self, show: bool):
        for w in self._advanced_widgets:
            if show:
                w.grid()
            else:
                w.grid_remove()

    def _on_provider_change(self):
        provider = self._provider_var.get()
        username = self._username_entry.get().strip()
        if provider == "自定义" and username:
            detected = detect_provider(username)
            if detected != "自定义":
                self._provider_var.set(detected)
                provider = detected
        data = {
            "imap_host": "",
            "imap_port": 993,
            "smtp_host": "",
            "smtp_port": 465,
        }
        data = apply_preset_to_dict(data, provider)
        self._entries["imap_host"].delete(0, "end")
        self._entries["imap_host"].insert(0, data["imap_host"])
        self._entries["imap_port"].delete(0, "end")
        self._entries["imap_port"].insert(0, str(data["imap_port"]))
        self._entries["smtp_host"].delete(0, "end")
        self._entries["smtp_host"].insert(0, data["smtp_host"])
        self._entries["smtp_port"].delete(0, "end")
        self._entries["smtp_port"].insert(0, str(data["smtp_port"]))

    def _on_username_changed(self):
        username = self._username_entry.get().strip()
        provider = detect_provider(username)
        if provider != "自定义":
            self._provider_var.set(provider)
            self._on_provider_change()

    def _fill(self, cfg: MailBoxConfig):
        provider = detect_provider(cfg.username) or "自定义"
        self._provider_var.set(provider)
        self._username_entry.delete(0, "end")
        self._username_entry.insert(0, cfg.username)
        self._password_entry.delete(0, "end")
        self._password_entry.insert(0, cfg.password)
        self._enabled_var.set(cfg.enabled)
        mapping = {
            "imap_host": cfg.imap_host,
            "imap_port": str(cfg.imap_port),
            "smtp_host": cfg.smtp_host,
            "smtp_port": str(cfg.smtp_port),
            "folder": cfg.folder,
            "subject_filter": cfg.subject_filter,
            "subject_regex": cfg.subject_regex,
            "sender_whitelist": "|".join(cfg.sender_whitelist),
            "max_size_mb": str(cfg.max_size_mb),
        }
        for key, val in mapping.items():
            if key in self._entries:
                self._entries[key].delete(0, "end")
                self._entries[key].insert(0, val)
        self._on_provider_change()

    def _on_save(self):
        data = {
            "name": self._username_entry.get().strip().split("@")[0] or "默认邮箱",
            "username": self._username_entry.get().strip(),
            "password": self._password_entry.get().strip(),
            "enabled": self._enabled_var.get(),
            "imap_host": self._entries["imap_host"].get().strip(),
            "imap_port": self._entries["imap_port"].get().strip() or "993",
            "smtp_host": self._entries["smtp_host"].get().strip(),
            "smtp_port": self._entries["smtp_port"].get().strip() or "465",
            "folder": self._entries["folder"].get().strip() or "INBOX",
            "subject_filter": self._entries["subject_filter"].get().strip() or "发票|电子发票|invoice",
            "subject_regex": self._entries["subject_regex"].get().strip(),
            "sender_whitelist": self._entries["sender_whitelist"].get().strip(),
            "max_size_mb": self._entries["max_size_mb"].get().strip() or "20",
        }
        data = apply_preset_to_dict(data, self._provider_var.get())
        try:
            data["imap_port"] = int(data.get("imap_port", 993) or 993)
            data["smtp_port"] = int(data.get("smtp_port", 465) or 465)
            data["max_size_mb"] = int(data.get("max_size_mb", 20) or 20)
        except ValueError:
            messagebox.showerror("错误", "端口和附件上限必须是数字。")
            return
        if not data.get("imap_host") or not data.get("username"):
            messagebox.showerror("错误", "IMAP 服务器和邮箱地址必填。")
            return
        try:
            self.result = MailBoxConfig.from_dict(data)
        except Exception as exc:
            messagebox.showerror("错误", f"配置无效: {exc}")
            return
        self.destroy()
