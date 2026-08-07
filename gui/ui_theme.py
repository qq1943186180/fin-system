# -*- coding: utf-8 -*-
"""Light business theme engine for tkinter Finance GUI.

Provides a clean, professional light theme suitable for enterprise
financial software: white background, light gray panels, blue accent,
and consistent typography.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# ------------------------------------------------------------------
# Colour tokens — light business style
# ------------------------------------------------------------------
C_BG = "#f8f9fa"           # main window background (very light gray)
C_CARD = "#ffffff"         # card / panel surface (white)
C_CARD_HOVER = "#f3f4f6"   # card hover / elevated
C_BORDER = "#e5e7eb"       # subtle borders / dividers
C_NAV = "#ffffff"          # sidebar background (white)
C_NAV_ACTIVE = "#eff6ff"   # sidebar active item bg (light blue tint)
C_NAV_HOVER = "#f3f4f6"    # sidebar hover (light gray)
C_NAV_TEXT = "#6b7280"     # sidebar inactive text
C_NAV_TEXT_ACTIVE = "#1d4ed8"  # sidebar active text (blue)

C_TEXT = "#111827"         # primary text (near black)
C_TEXT_MUTED = "#6b7280"   # secondary / hint text
C_TEXT_DIM = "#9ca3af"     # very dim text

C_ACCENT = "#2563eb"       # primary action blue
C_ACCENT_HOVER = "#1d4ed8"
C_ACCENT_SOFT = "#dbeafe"  # soft accent background

C_OK = "#16a34a"
C_OK_SOFT = "#dcfce7"
C_WARN = "#d97706"
C_WARN_SOFT = "#fef3c7"
C_DANGER = "#dc2626"
C_DANGER_SOFT = "#fee2e2"

C_INPUT_BG = "#ffffff"
C_INPUT_FG = C_TEXT
C_INPUT_BORDER = C_BORDER

# Back-compat alias used by legacy pages
C_PANEL = C_CARD

# ------------------------------------------------------------------
# Font tokens
# ------------------------------------------------------------------
FONT = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_B = ("Microsoft YaHei UI", 11, "bold")
FONT_TITLE = ("Microsoft YaHei UI", 18, "bold")
FONT_H2 = ("Microsoft YaHei UI", 14, "bold")
FONT_H3 = ("Microsoft YaHei UI", 12, "bold")
FONT_NUM = ("Microsoft YaHei UI", 24, "bold")
FONT_MONO = ("Consolas", 10)


# ------------------------------------------------------------------
# Public helpers
# ------------------------------------------------------------------

def configure_light_style(root: tk.Tk) -> None:
    """Apply a full light business theme to ttk widgets."""
    style = ttk.Style(root)

    # Pick a native theme that supports override reasonably well.
    available = list(style.theme_names())
    preferred = [t for t in ("vista", "clam", "alt", "default") if t in available]
    if preferred:
        style.theme_use(preferred[0])

    # General
    style.configure(".", font=FONT, background=C_BG, foreground=C_TEXT)
    style.configure("TFrame", background=C_BG)
    style.configure("TLabel", background=C_BG, foreground=C_TEXT)

    # Notebook
    style.configure("TNotebook", background=C_BG, borderwidth=0)
    style.configure("TNotebook.Tab", background=C_CARD, foreground=C_TEXT_MUTED,
                    padding=(12, 6), borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", C_ACCENT), ("!selected", C_CARD)],
              foreground=[("selected", "#ffffff"), ("!selected", C_TEXT_MUTED)])

    # Treeview
    style.configure("Treeview",
                    background=C_CARD,
                    foreground=C_TEXT,
                    fieldbackground=C_CARD,
                    borderwidth=0,
                    rowheight=28)
    style.configure("Treeview.Heading",
                    background=C_BG,
                    foreground=C_TEXT_MUTED,
                    font=FONT_SMALL,
                    borderwidth=0)
    style.map("Treeview",
              background=[("selected", C_ACCENT_SOFT)],
              foreground=[("selected", C_ACCENT)])

    # Buttons
    style.configure("TButton",
                    background=C_CARD,
                    foreground=C_TEXT,
                    borderwidth=1,
                    focusthickness=0,
                    padding=(10, 6))
    style.map("TButton",
              background=[("active", C_CARD_HOVER), ("pressed", C_BORDER)],
              foreground=[("active", C_TEXT), ("pressed", C_TEXT)])

    # Primary action button
    style.configure("Primary.TButton",
                    background=C_ACCENT,
                    foreground="#ffffff",
                    borderwidth=0,
                    padding=(14, 8))
    style.map("Primary.TButton",
              background=[("active", C_ACCENT_HOVER), ("pressed", "#1e40af")],
              foreground=[("active", "#ffffff"), ("pressed", "#ffffff")])

    # Entry
    style.configure("TEntry",
                    fieldbackground=C_INPUT_BG,
                    foreground=C_INPUT_FG,
                    borderwidth=1,
                    insertcolor=C_TEXT)

    # Combobox
    style.configure("TCombobox",
                    fieldbackground=C_INPUT_BG,
                    foreground=C_INPUT_FG,
                    borderwidth=1)

    # Scrollbar
    style.configure("TScrollbar",
                    background=C_CARD,
                    troughcolor=C_BG,
                    borderwidth=0,
                    arrowcolor=C_TEXT_MUTED)


# Back-compat alias so existing code still works.
configure_dark_style = configure_light_style


class CardFrame(tk.Canvas):
    """A rounded rectangle card container."""

    def __init__(self, parent, radius: int = 12, bg: str = C_CARD,
                 border: str = C_BORDER, padx: int = 16, pady: int = 16, **kwargs):
        super().__init__(parent, bg=parent.cget("bg") if parent else C_BG,
                         highlightthickness=0, **kwargs)
        self._radius = radius
        self._bg = bg
        self._border = border
        self._padx = padx
        self._pady = pady

        self.inner = tk.Frame(self, bg=bg)
        self.create_window((padx, pady), window=self.inner, anchor="nw")

        self.bind("<Configure>", self._draw)

    def _draw(self, event=None) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        self.create_rounded_rect(0, 0, w, h, radius=self._radius,
                                 fill=self._bg, outline=self._border, width=1)
        # Keep inner frame sized
        self.itemconfig(self.create_window((self._padx, self._pady), window=self.inner, anchor="nw"),
                        width=w - self._padx * 2, height=h - self._pady * 2)

    def create_rounded_rect(self, x1, y1, x2, y2, radius=12, **kwargs):
        """Draw a rounded rectangle."""
        points = [
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)


class ModernButton(tk.Canvas):
    """A canvas-based modern button with hover/active states."""

    def __init__(self, parent, text: str = "", command=None,
                 variant: str = "primary", font=FONT, height: int = 36, **kwargs):
        bg = parent.cget("bg") if parent else C_BG
        super().__init__(parent, bg=bg, highlightthickness=0, height=height, **kwargs)
        self._text = text
        self._command = command
        self._variant = variant
        self._font = font
        self._hover = False
        self._pressed = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

        self.bind("<Configure>", lambda e: self._draw())

    def _colors(self):
        if self._variant == "primary":
            base = C_ACCENT
            hover = C_ACCENT_HOVER
            text_color = "#ffffff"
        elif self._variant == "ghost":
            base = C_CARD
            hover = C_CARD_HOVER
            text_color = C_TEXT
        else:
            base = C_BG
            hover = C_CARD_HOVER
            text_color = C_TEXT
        if self._pressed:
            base = C_BORDER if self._variant == "ghost" else "#1e40af"
        return base, hover, text_color

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 2 or h < 2:
            return
        base, hover, text_color = self._colors()
        bg = hover if self._hover and not self._pressed else base
        radius = 8
        self.create_rounded_rect(0, 0, w, h, radius=radius, fill=bg, outline=bg)
        self.create_text(w // 2, h // 2, text=self._text, fill=text_color,
                         font=self._font, anchor="center")

    def create_rounded_rect(self, x1, y1, x2, y2, radius=8, **kwargs):
        points = [
            x1 + radius, y1, x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2, x1 + radius, y2,
            x1, y2, x1, y2 - radius, x1, y1 + radius, x1, y1,
        ]
        return self.create_polygon(points, smooth=True, **kwargs)

    def _on_enter(self, e):
        self._hover = True
        self._draw()

    def _on_leave(self, e):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, e):
        self._pressed = True
        self._draw()

    def _on_release(self, e):
        self._pressed = False
        self._draw()
        if self._command:
            self._command()


class NavButton(tk.Frame):
    """Sidebar navigation button with active indicator."""

    def __init__(self, parent, text: str = "", command=None, **kwargs):
        super().__init__(parent, bg=C_NAV, **kwargs)
        self._text = text
        self._command = command
        self._active = False

        self._indicator = tk.Canvas(self, width=4, height=28, bg=C_NAV,
                                    highlightthickness=0)
        self._indicator.pack(side="left", padx=(8, 6))

        self._label = tk.Label(self, text=text, font=FONT, bg=C_NAV,
                               fg=C_NAV_TEXT, anchor="w", padx=4)
        self._label.pack(side="left", fill="both", expand=True)

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, e):
        if not self._active:
            self._label.config(bg=C_NAV_HOVER, fg=C_NAV_TEXT_ACTIVE)
            self.config(bg=C_NAV_HOVER)

    def _on_leave(self, e):
        if not self._active:
            self._label.config(bg=C_NAV, fg=C_NAV_TEXT)
            self.config(bg=C_NAV)

    def _on_click(self, e):
        if self._command:
            self._command()

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self.config(bg=C_NAV_ACTIVE)
            self._label.config(bg=C_NAV_ACTIVE, fg=C_NAV_TEXT_ACTIVE)
            self._indicator.delete("all")
            self._indicator.config(bg=C_ACCENT)
            self._indicator.create_rounded_rect = lambda *a, **k: None
            self._indicator.configure(bg=C_ACCENT)
        else:
            self.config(bg=C_NAV)
            self._label.config(bg=C_NAV, fg=C_NAV_TEXT)
            self._indicator.configure(bg=C_NAV)


class StatCard(tk.Frame):
    """Dashboard statistics card."""

    def __init__(self, parent, label: str = "", value: str = "",
                 colour: str = C_ACCENT, **kwargs):
        super().__init__(parent, bg=C_CARD, **kwargs)
        self._colour = colour

        top = tk.Frame(self, bg=C_CARD)
        top.pack(fill="x", padx=16, pady=(16, 4))
        tk.Label(top, text=label, font=FONT_SMALL, bg=C_CARD, fg=C_TEXT_MUTED).pack(side="left")

        tk.Label(self, text=value, font=FONT_NUM, bg=C_CARD, fg=colour).pack(
            anchor="w", padx=16, pady=(0, 16))
