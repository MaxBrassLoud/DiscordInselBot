# bot/tools/api_tester_gui.py
"""
API-Tester – Tkinter UI (überarbeitet)
========================================
Moderner API-Tester mit Sidebar-Navigation und Live-Endpunkt-Erkennung.

Layout:
  ┌──────────────┬─────────────────────────────────────┐
  │  SIDEBAR     │  TOPBAR (URL / Key / Auth)          │
  │  (Endpoints) ├─────────────────────────────────────┤
  │  Kategorien  │  REQUEST-PANEL (Params)             │
  │  + Suche     ├─────────────────────────────────────┤
  │              │  RESPONSE-TABS                      │
  └──────────────┴─────────────────────────────────────┘

Verwendung:
    python -m bot.tools.api_tester_gui
"""
from __future__ import annotations

import json
import re
import threading
import tkinter as tk
from datetime import datetime
from tkinter import messagebox, ttk
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    raise SystemExit("Bitte installieren: pip install requests")


# ══════════════════════════════════════════════════════════════════════════════
# STYLE-TOKENS (passend zu main.css)
# ══════════════════════════════════════════════════════════════════════════════

BG        = "#0a0b0d"   # Basis
SURFACE   = "#111318"   # Sidebar / Topbar
CARD      = "#181c22"   # Panels
CARD2     = "#1d2128"   # Eingabe-Hintergrund
HOVER     = "#22272f"   # Hover
BORDER    = "#252b35"
BORDER2   = "#2e3540"

TEXT      = "#e8edf5"
TEXT2     = "#8b95a8"
TEXT3     = "#555f6e"

GREEN     = "#4ade80"
GREEN2    = "#22c55e"
GREEN_BG  = "#0f2418"   # aktive Auswahl
RED       = "#f87171"
ORANGE    = "#fb923c"
BLUE      = "#60a5fa"
BLUE_BG   = "#0f1e33"
GOLD      = "#fbbf24"
PURPLE    = "#a855f7"

FONT_UI   = "Segoe UI"
FONT_MONO = "Consolas"


# ══════════════════════════════════════════════════════════════════════════════
# HILFS-WIDGETS
# ══════════════════════════════════════════════════════════════════════════════

class RoundedEntry(tk.Frame):
    """Ein Eingabefeld mit umrahmter Border und Fokus-Highlight."""
    def __init__(self, parent, textvariable, width=30, show=None, bg=CARD2):
        super().__init__(parent, bg=BORDER2, bd=0)
        self._inner_bg = bg
        self.entry = tk.Entry(
            self, textvariable=textvariable,
            font=(FONT_UI, 10), width=width, show=show,
            bg=bg, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0,
        )
        self.entry.pack(fill="both", expand=True, padx=1, pady=1, ipady=6, ipadx=8)
        self.entry.bind("<FocusIn>",  lambda e: self.configure(bg=GREEN2))
        self.entry.bind("<FocusOut>", lambda e: self.configure(bg=BORDER2))

    def get(self):
        return self.entry.get()

    def set(self, v):
        self.entry.delete(0, tk.END)
        self.entry.insert(0, v)


class FlatButton(tk.Button):
    """Ein Button mit Hover-Effekt und optionaler Primärfarbe."""
    def __init__(self, parent, text, command, *, primary=False, danger=False,
                 ghost=False, width=None, **kw):
        if primary:
            bg, fg, hover = GREEN2, "#000", "#3ee06f"
        elif danger:
            bg, fg, hover = CARD2, RED, "#2a1a1a"
        elif ghost:
            bg, fg, hover = CARD2, TEXT2, HOVER
        else:
            bg, fg, hover = CARD2, TEXT, HOVER

        font = (FONT_UI, 9, "bold")
        super().__init__(
            parent, text=text, command=command,
            font=font, bg=bg, fg=fg,
            activebackground=hover, activeforeground=fg,
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
            **kw,
        )
        if width:
            self.configure(width=width)

        self._bg = bg
        self._hover = hover
        self.bind("<Enter>", lambda e: self.configure(bg=self._hover))
        self.bind("<Leave>", lambda e: self.configure(bg=self._bg))
        self.bind("<ButtonRelease-1>",
                  lambda e: self.configure(bg=self._hover))


def make_card(parent, padx=16, pady=14):
    """Standard-Karte mit Border."""
    return tk.Frame(parent, bg=CARD, padx=padx, pady=pady,
                    highlightthickness=1, highlightbackground=BORDER)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

class ApiTesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("API-Tester · Insel Bot")
        self.geometry("1280x860")
        self.minsize(1080, 720)
        self.configure(bg=BG)

        # Zustand
        self._last_response: requests.Response | None = None
        self._history: list[dict] = []
        self._endpoints: list[dict] = []
        self._endpoints_by_label: dict[str, dict] = {}
        self._selected_endpoint: dict | None = None
        self._param_rows: list[dict] = []

        self._setup_ttk_styles()
        self._build_ui()

    # ── TTK-Styles ────────────────────────────────────────────────────────────

    def _setup_ttk_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        # Combobox
        style.configure(
            "Dark.TCombobox",
            fieldbackground=CARD2, background=CARD2, foreground=TEXT,
            arrowcolor=TEXT2, bordercolor=BORDER2,
            lightcolor=CARD2, darkcolor=CARD2,
            selectbackground=CARD2, selectforeground=TEXT,
            padding=6,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", CARD2)],
            foreground=[("readonly", TEXT)],
            bordercolor=[("focus", GREEN2)],
        )
        self.option_add("*TCombobox*Listbox.background", CARD)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.option_add("*TCombobox*Listbox.selectBackground", GREEN_BG)
        self.option_add("*TCombobox*Listbox.selectForeground", GREEN)
        self.option_add("*TCombobox*Listbox.font", f"{{{FONT_UI}}} 10")

        # Notebook
        style.configure(
            "Dark.TNotebook",
            background=BG, borderwidth=0, tabmargins=0,
        )
        style.configure(
            "Dark.TNotebook.Tab",
            background=CARD2, foreground=TEXT2, padding=[16, 8],
            font=(FONT_UI, 9, "bold"), borderwidth=0,
        )
        style.map(
            "Dark.TNotebook.Tab",
            background=[("selected", CARD)],
            foreground=[("selected", GREEN)],
        )

        # Treeview
        style.configure(
            "Dark.Treeview",
            background=CARD2, fieldbackground=CARD2, foreground=TEXT,
            rowheight=26, borderwidth=0, font=(FONT_UI, 9),
        )
        style.configure(
            "Dark.Treeview.Heading",
            background=CARD, foreground=TEXT2, relief="flat",
            font=(FONT_UI, 8, "bold"),
        )
        style.map(
            "Dark.Treeview.Heading",
            background=[("active", HOVER)],
        )
        style.map(
            "Dark.Treeview",
            background=[("selected", GREEN_BG)],
            foreground=[("selected", GREEN)],
        )

        # Scrollbar
        style.configure(
            "Dark.Vertical.TScrollbar",
            background=CARD2, troughcolor=BG, bordercolor=BG,
            arrowcolor=TEXT3, gripcount=0, width=8,
        )
        style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", BORDER2)],
        )

    # ── Haupt-Layout ──────────────────────────────────────────────────────────

    def _build_ui(self):
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        self._build_sidebar(body)

        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        self._build_topbar(main)
        self._build_request_panel(main)
        self._build_response_panel(main)

    # ── Sidebar ───────────────────────────────────────────────────────────────

    def _build_sidebar(self, parent):
        sidebar = tk.Frame(parent, bg=SURFACE, width=290)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # ── Brand ─────────────────────────────────────────────────────────────
        brand = tk.Frame(sidebar, bg=SURFACE, padx=20, pady=20)
        brand.pack(fill="x")

        logo_row = tk.Frame(brand, bg=SURFACE)
        logo_row.pack(anchor="w")

        tk.Label(
            logo_row, text="🧪",
            font=(FONT_UI, 14),
            bg=BLUE_BG, fg=BLUE,
            padx=8, pady=4,
        ).pack(side="left")

        brand_text = tk.Frame(logo_row, bg=SURFACE)
        brand_text.pack(side="left", padx=(10, 0))

        tk.Label(
            brand_text, text="API-Tester",
            font=(FONT_UI, 12, "bold"), bg=SURFACE, fg=TEXT,
        ).pack(anchor="w")

        tk.Label(
            brand_text, text="Insel Bot · Open API",
            font=(FONT_UI, 8), bg=SURFACE, fg=TEXT3,
        ).pack(anchor="w")

        # Trennlinie
        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x")

        # ── Verbindung ────────────────────────────────────────────────────────
        conn = tk.Frame(sidebar, bg=SURFACE, padx=20, pady=16)
        conn.pack(fill="x")

        tk.Label(
            conn, text="VERBINDUNG",
            font=(FONT_UI, 8, "bold"), bg=SURFACE, fg=TEXT3,
        ).pack(anchor="w")

        tk.Label(
            conn, text="Basis-URL",
            font=(FONT_UI, 9), bg=SURFACE, fg=TEXT2,
        ).pack(anchor="w", pady=(10, 4))

        self.base_url_var = tk.StringVar(value="http://localhost:5000")
        RoundedEntry(conn, self.base_url_var, width=26).pack(fill="x")

        tk.Label(
            conn, text="API-Key",
            font=(FONT_UI, 9), bg=SURFACE, fg=TEXT2,
        ).pack(anchor="w", pady=(10, 4))

        self.key_var = tk.StringVar(value="")
        RoundedEntry(conn, self.key_var, width=26).pack(fill="x")

        tk.Label(
            conn, text="Authentifizierung",
            font=(FONT_UI, 9), bg=SURFACE, fg=TEXT2,
        ).pack(anchor="w", pady=(10, 4))

        self.auth_var = tk.StringVar(value="header")
        auth_row = tk.Frame(conn, bg=SURFACE)
        auth_row.pack(anchor="w", fill="x")
        for val, lbl in [("header", "Header"),
                         ("bearer", "Bearer"),
                         ("query",  "Query")]:
            tk.Radiobutton(
                auth_row, text=lbl, variable=self.auth_var, value=val,
                font=(FONT_UI, 8), bg=SURFACE, fg=TEXT2,
                selectcolor=CARD2, activebackground=SURFACE,
                activeforeground=TEXT, highlightthickness=0, bd=0,
                cursor="hand2",
            ).pack(side="left", padx=(0, 8))

        self.load_ep_btn = FlatButton(
            conn, "Endpunkte laden",
            command=self._on_load_endpoints,
            primary=True, width=26,
        )
        self.load_ep_btn.pack(fill="x", pady=(14, 6))

        self.conn_status = tk.Label(
            conn, text="",
            font=(FONT_UI, 8), bg=SURFACE, fg=TEXT3,
            wraplength=250, justify="left",
        )
        self.conn_status.pack(anchor="w")

        # Trennlinie
        tk.Frame(sidebar, bg=BORDER, height=1).pack(fill="x", pady=(4, 0))

        # ── Endpunkt-Header ──────────────────────────────────────────────────
        ep_header = tk.Frame(sidebar, bg=SURFACE, padx=20, pady=8)
        ep_header.pack(fill="x", pady=(14, 8))

        tk.Label(
            ep_header, text="ENDPUNKTE",
            font=(FONT_UI, 8, "bold"), bg=SURFACE, fg=TEXT3,
        ).pack(side="left")

        self.ep_count_label = tk.Label(
            ep_header, text="—",
            font=(FONT_UI, 8, "bold"), bg=SURFACE, fg=TEXT3,
        )
        self.ep_count_label.pack(side="right")

        # ── Suche ─────────────────────────────────────────────────────────────
        search_row = tk.Frame(sidebar, bg=SURFACE, padx=20)
        search_row.pack(fill="x", pady=(0, 8))

        self.search_var = tk.StringVar(value="")
        search_entry = RoundedEntry(search_row, self.search_var, width=26)
        search_entry.pack(fill="x")
        search_entry.entry.configure(font=(FONT_UI, 9))
        self.search_var.trace_add("write", lambda *a: self._render_endpoint_list())

        # ── Endpunkt-Liste (scrollbar) ────────────────────────────────────────
        list_wrap = tk.Frame(sidebar, bg=SURFACE)
        list_wrap.pack(fill="both", expand=True, padx=(12, 8), pady=(0, 16))

        self.ep_canvas = tk.Canvas(
            list_wrap, bg=SURFACE, highlightthickness=0, bd=0,
        )
        self.ep_canvas.pack(side="left", fill="both", expand=True)

        self.ep_scroll = ttk.Scrollbar(
            list_wrap, orient="vertical",
            command=self.ep_canvas.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.ep_scroll.pack(side="right", fill="y")

        self.ep_canvas.configure(yscrollcommand=self.ep_scroll.set)

        self.ep_inner = tk.Frame(self.ep_canvas, bg=SURFACE)
        self._ep_inner_id = self.ep_canvas.create_window(
            (0, 0), window=self.ep_inner, anchor="nw",
        )

        self.ep_inner.bind(
            "<Configure>",
            lambda e: self.ep_canvas.configure(
                scrollregion=self.ep_canvas.bbox("all")
            ),
        )
        self.ep_canvas.bind(
            "<Configure>",
            lambda e: self.ep_canvas.itemconfigure(
                self._ep_inner_id, width=e.width
            ),
        )
        self.ep_canvas.bind(
            "<Enter>",
            lambda e: self.ep_canvas.bind_all(
                "<MouseWheel>", lambda ev: self.ep_canvas.yview_scroll(
                    int(-1 * (ev.delta / 120)), "units",
                ),
            ),
        )
        self.ep_canvas.bind(
            "<Leave>",
            lambda e: self.ep_canvas.unbind_all("<MouseWheel>"),
        )

    # ── Topbar ────────────────────────────────────────────────────────────────

    def _build_topbar(self, parent):
        topbar = tk.Frame(parent, bg=SURFACE, height=60)
        topbar.pack(fill="x")
        topbar.pack_propagate(False)

        left = tk.Frame(topbar, bg=SURFACE)
        left.pack(side="left", fill="y", padx=(24, 0))

        self.topbar_title = tk.Label(
            left, text="Wähle einen Endpunkt",
            font=(FONT_UI, 12, "bold"), bg=SURFACE, fg=TEXT,
        )
        self.topbar_title.pack(anchor="w", pady=(12, 0))

        self.topbar_sub = tk.Label(
            left, text="Links in der Sidebar auswählen",
            font=(FONT_UI, 8), bg=SURFACE, fg=TEXT3,
        )
        self.topbar_sub.pack(anchor="w")

        right = tk.Frame(topbar, bg=SURFACE)
        right.pack(side="right", fill="y", padx=(0, 24))

        self.scope_badge = tk.Label(
            right, text="",
            font=(FONT_UI, 8, "bold"),
            bg=CARD2, fg=TEXT3, padx=10, pady=4,
        )
        self.scope_badge.pack(side="right", pady=18)

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x")

    # ── Request-Panel ─────────────────────────────────────────────────────────

    def _build_request_panel(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="x", padx=24, pady=(18, 8))

        # Methode + URL-Zeile
        row1 = tk.Frame(wrap, bg=BG)
        row1.pack(fill="x")

        # Methoden-Segment
        self.method_var = tk.StringVar(value="GET")
        method_seg = tk.Frame(row1, bg=CARD2,
                              highlightthickness=1,
                              highlightbackground=BORDER2)
        method_seg.pack(side="left")

        self._method_buttons: dict[str, tk.Label] = {}
        for m in ["GET", "POST", "PUT", "PATCH", "DELETE"]:
            lbl = tk.Label(
                method_seg, text=m,
                font=(FONT_UI, 9, "bold"),
                bg=CARD2, fg=TEXT2,
                padx=14, pady=8, cursor="hand2",
            )
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, mm=m: self._set_method(mm))
            self._method_buttons[m] = lbl

        self._refresh_method_segment()

        # URL-Eingabe
        url_frame = tk.Frame(row1, bg=BORDER2)
        url_frame.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.url_var = tk.StringVar(value="/open-api/users")
        url_entry = tk.Entry(
            url_frame, textvariable=self.url_var,
            font=(FONT_MONO, 10),
            bg=CARD2, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0,
        )
        url_entry.pack(fill="both", expand=True, padx=1, pady=1,
                       ipady=8, ipadx=10)
        url_entry.bind("<FocusIn>",  lambda e: url_frame.configure(bg=GREEN2))
        url_entry.bind("<FocusOut>", lambda e: url_frame.configure(bg=BORDER2))

        FlatButton(row1, "📋", command=self._copy_url, ghost=True).pack(
            side="left", padx=(10, 0),
        )

        # Beschreibung
        self.endpoint_desc = tk.Label(
            wrap, text="",
            font=(FONT_UI, 9), bg=BG, fg=TEXT2,
            justify="left", anchor="w", wraplength=900,
        )
        self.endpoint_desc.pack(fill="x", pady=(12, 0))

        # Params-Karte
        params_card = make_card(wrap, padx=18, pady=14)
        params_card.pack(fill="x", pady=(14, 0))

        phead = tk.Frame(params_card, bg=CARD)
        phead.pack(fill="x")

        tk.Label(
            phead, text="QUERY-PARAMETER",
            font=(FONT_UI, 8, "bold"), bg=CARD, fg=TEXT3,
        ).pack(side="left")

        self.param_hint = tk.Label(
            phead, text="",
            font=(FONT_UI, 8), bg=CARD, fg=TEXT3,
        )
        self.param_hint.pack(side="left", padx=(10, 0))

        FlatButton(
            phead, "+ Parameter",
            command=self._add_param_row_empty, ghost=True,
        ).pack(side="right")

        self.param_container = tk.Frame(params_card, bg=CARD)
        self.param_container.pack(fill="x", pady=(10, 0))

        # Senden-Leiste
        send_row = tk.Frame(wrap, bg=BG)
        send_row.pack(fill="x", pady=(14, 0))

        self.send_btn = FlatButton(
            send_row, "  ▶  Anfrage senden",
            command=self._on_send, primary=True,
        )
        self.send_btn.configure(font=(FONT_UI, 10, "bold"), padx=22, pady=10)
        self.send_btn.pack(side="left")

        FlatButton(
            send_row, "URL kopieren",
            command=self._copy_url, ghost=True,
        ).pack(side="left", padx=(8, 0))

        FlatButton(
            send_row, "Felder leeren",
            command=self._clear_form, ghost=True,
        ).pack(side="left", padx=(8, 0))

        tk.Label(
            send_row, text="Enter in URL-Feld = senden",
            font=(FONT_UI, 8), bg=BG, fg=TEXT3,
        ).pack(side="right", pady=8)

        url_entry.bind("<Return>", lambda e: self._on_send())

    def _set_method(self, method: str):
        self.method_var.set(method)
        self._refresh_method_segment()

    def _refresh_method_segment(self):
        current = self.method_var.get()
        color_by_method = {
            "GET":    GREEN,
            "POST":   BLUE,
            "PUT":    GOLD,
            "PATCH":  ORANGE,
            "DELETE": RED,
        }
        for m, lbl in self._method_buttons.items():
            if m == current:
                lbl.configure(bg=CARD2, fg=color_by_method.get(m, TEXT))
            else:
                lbl.configure(bg=CARD2, fg=TEXT3)

    # ── Response-Panel ────────────────────────────────────────────────────────

    def _build_response_panel(self, parent):
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(fill="both", expand=True, padx=24, pady=(8, 20))

        status_row = tk.Frame(wrap, bg=BG)
        status_row.pack(fill="x", pady=(0, 8))

        tk.Label(
            status_row, text="RESPONSE",
            font=(FONT_UI, 8, "bold"), bg=BG, fg=TEXT3,
        ).pack(side="left")

        self.status_badge = tk.Label(
            status_row, text="—",
            font=(FONT_UI, 9, "bold"),
            bg=CARD, fg=TEXT3, padx=10, pady=3,
            highlightthickness=1, highlightbackground=BORDER2,
        )
        self.status_badge.pack(side="left", padx=(12, 0))

        self.time_label = tk.Label(
            status_row, text="",
            font=(FONT_UI, 8), bg=BG, fg=TEXT3,
        )
        self.time_label.pack(side="left", padx=(12, 0))

        FlatButton(
            status_row, "Antwort kopieren",
            command=self._copy_response, ghost=True,
        ).pack(side="right")

        # Notebook
        self.notebook = ttk.Notebook(wrap, style="Dark.TNotebook")
        self.notebook.pack(fill="both", expand=True)

        # ── Tab: JSON ────────────────────────────────────────────────────────
        raw = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(raw, text="  JSON  ")

        raw_wrap = tk.Frame(raw, bg=CARD)
        raw_wrap.pack(fill="both", expand=True, padx=1, pady=1)

        self.raw_text = tk.Text(
            raw_wrap, font=(FONT_MONO, 9),
            bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, wrap="none",
            highlightthickness=0, padx=14, pady=12,
            selectbackground=GREEN_BG, selectforeground=GREEN,
        )
        self.raw_text.pack(fill="both", expand=True, side="left")

        raw_sb = ttk.Scrollbar(
            raw_wrap, command=self.raw_text.yview,
            style="Dark.Vertical.TScrollbar",
        )
        raw_sb.pack(side="right", fill="y")
        self.raw_text.configure(yscrollcommand=raw_sb.set)

        self.raw_text.tag_configure("key",    foreground=BLUE)
        self.raw_text.tag_configure("string", foreground=GREEN)
        self.raw_text.tag_configure("number", foreground=GOLD)
        self.raw_text.tag_configure("bool",   foreground=PURPLE)
        self.raw_text.tag_configure("null",   foreground=TEXT3)
        self.raw_text.tag_configure("punct",  foreground=TEXT3)

        # ── Tab: Users ───────────────────────────────────────────────────────
        users_frame = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(users_frame, text="  Users  ")
        self.tree = ttk.Treeview(
            users_frame, style="Dark.Treeview",
            columns=("id", "display_name", "username",
                     "status", "is_online", "is_bot"),
            show="headings", height=10,
        )
        for col, (label, width, anchor) in {
            "id":           ("User-ID",      160, "w"),
            "display_name": ("Anzeigename",  200, "w"),
            "username":     ("Username",     150, "w"),
            "status":       ("Status",        90, "center"),
            "is_online":    ("Online",         70, "center"),
            "is_bot":       ("Bot",            50, "center"),
        }.items():
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor)
        self.tree.pack(fill="both", expand=True, side="left",
                       padx=(1, 0), pady=1)
        tree_sb = ttk.Scrollbar(
            users_frame, command=self.tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        tree_sb.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=tree_sb.set)

        self.tree.tag_configure("online",  foreground=GREEN)
        self.tree.tag_configure("offline", foreground=TEXT3)

        # ── Tab: Leaderboard ─────────────────────────────────────────────────
        lb_frame = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(lb_frame, text="  Leaderboard  ")
        self.lb_tree = ttk.Treeview(
            lb_frame, style="Dark.Treeview",
            columns=("rank", "display_name", "level",
                     "xp", "messages", "voice_minutes"),
            show="headings", height=10,
        )
        for col, (label, width, anchor) in {
            "rank":          ("#",           50, "center"),
            "display_name":  ("Name",       220, "w"),
            "level":         ("Level",       80, "center"),
            "xp":            ("XP",         120, "e"),
            "messages":      ("Msg",        100, "e"),
            "voice_minutes": ("Voice (min)", 120, "e"),
        }.items():
            self.lb_tree.heading(col, text=label)
            self.lb_tree.column(col, width=width, anchor=anchor)
        self.lb_tree.pack(fill="both", expand=True, side="left",
                          padx=(1, 0), pady=1)
        lb_sb = ttk.Scrollbar(
            lb_frame, command=self.lb_tree.yview,
            style="Dark.Vertical.TScrollbar",
        )
        lb_sb.pack(side="right", fill="y")
        self.lb_tree.configure(yscrollcommand=lb_sb.set)

        self.lb_tree.tag_configure("gold",   foreground=GOLD)
        self.lb_tree.tag_configure("silver", foreground=TEXT2)
        self.lb_tree.tag_configure("bronze", foreground=ORANGE)

        # ── Tab: Verlauf ─────────────────────────────────────────────────────
        hist_frame = tk.Frame(self.notebook, bg=CARD)
        self.notebook.add(hist_frame, text="  Verlauf  ")

        hist_wrap = tk.Frame(hist_frame, bg=CARD)
        hist_wrap.pack(fill="both", expand=True, padx=1, pady=1)

        self.history_list = tk.Listbox(
            hist_wrap,
            font=(FONT_MONO, 9),
            bg=CARD, fg=TEXT2,
            selectbackground=GREEN_BG, selectforeground=GREEN,
            relief="flat", bd=0, highlightthickness=0,
            activestyle="none",
        )
        self.history_list.pack(fill="both", expand=True, side="left",
                               padx=(14, 0), pady=12)
        self.history_list.bind("<Double-Button-1>", self._on_history_select)

        hist_sb = ttk.Scrollbar(
            hist_wrap, command=self.history_list.yview,
            style="Dark.Vertical.TScrollbar",
        )
        hist_sb.pack(side="right", fill="y")
        self.history_list.configure(yscrollcommand=hist_sb.set)

    # ══════════════════════════════════════════════════════════════════════════
    # ENDPUNKTE LADEN
    # ══════════════════════════════════════════════════════════════════════════

    def _build_headers_for_load(self) -> dict:
        headers = {"Accept": "application/json"}
        key = self.key_var.get().strip()
        mode = self.auth_var.get()
        if key:
            if mode == "header":
                headers["X-API-Key"] = key
            elif mode == "bearer":
                headers["Authorization"] = f"Bearer {key}"
        return headers

    def _on_load_endpoints(self):
        base = self.base_url_var.get().strip().rstrip("/")
        if not base:
            messagebox.showwarning("Fehlt", "Bitte Basis-URL eingeben.")
            return

        self.load_ep_btn.configure(state="disabled", text="Lade…",
                                   bg=BORDER2)
        self.conn_status.configure(text="🔄 Verbinde…", fg=ORANGE)

        threading.Thread(
            target=self._do_load_endpoints,
            args=(base,),
            daemon=True,
        ).start()

    def _do_load_endpoints(self, base_url: str):
        headers = self._build_headers_for_load()
        params = {}
        if self.auth_var.get() == "query":
            key = self.key_var.get().strip()
            if key:
                params["api_key"] = key

        url = f"{base_url}/open-api/endpoints"
        try:
            r = requests.get(url, headers=headers, params=params, timeout=10)
            if not r.ok:
                try:
                    err_json = r.json()
                    err_msg = err_json.get("error", r.text[:200])
                except Exception:
                    err_msg = r.text[:200]
                self.after(0, self._on_load_endpoints_error,
                           f"HTTP {r.status_code}: {err_msg}")
                return
            data = r.json()
            endpoints = data.get("endpoints") or []
            self.after(0, self._on_load_endpoints_success, endpoints)
        except requests.exceptions.ConnectionError:
            self.after(0, self._on_load_endpoints_error,
                       f"Verbindung zu {base_url} fehlgeschlagen.")
        except requests.exceptions.Timeout:
            self.after(0, self._on_load_endpoints_error,
                       "Timeout – Server antwortet nicht innerhalb 10s.")
        except Exception as e:
            self.after(0, self._on_load_endpoints_error, f"Fehler: {e}")

    def _on_load_endpoints_success(self, endpoints: list[dict]):
        self.load_ep_btn.configure(state="normal", text="Endpunkte laden",
                                   bg=GREEN2, fg="#000")
        self._endpoints = endpoints
        self._endpoints_by_label = {ep["label"]: ep for ep in endpoints}

        self.conn_status.configure(
            text=f"✅ {len(endpoints)} Endpunkte geladen",
            fg=GREEN,
        )
        self.ep_count_label.configure(text=str(len(endpoints)))

        self._render_endpoint_list()

        if endpoints:
            self._select_endpoint(endpoints[0])

    def _on_load_endpoints_error(self, msg: str):
        self.load_ep_btn.configure(state="normal", text="Endpunkte laden",
                                   bg=GREEN2, fg="#000")
        self.conn_status.configure(text=f"❌ {msg}", fg=RED)

    # ══════════════════════════════════════════════════════════════════════════
    # ENDPUNKT-LISTE (Sidebar)
    # ══════════════════════════════════════════════════════════════════════════

    def _render_endpoint_list(self):
        for child in self.ep_inner.winfo_children():
            child.destroy()

        if not self._endpoints:
            tk.Label(
                self.ep_inner,
                text="Keine Endpunkte geladen.\n"
                     "Klicke 'Endpunkte laden'.",
                font=(FONT_UI, 9), bg=SURFACE, fg=TEXT3,
                justify="left", wraplength=240,
            ).pack(anchor="w", padx=8, pady=8)
            return

        query = self.search_var.get().strip().lower()

        by_cat: dict[str, list[dict]] = {}
        for ep in self._endpoints:
            cats = ep.get("tags") or ["sonstiges"]
            cat  = cats[0] if cats else "sonstiges"
            if query and (
                query not in ep["label"].lower()
                and query not in ep.get("description", "").lower()
                and query not in ep.get("path", "").lower()
            ):
                continue
            by_cat.setdefault(cat, []).append(ep)

        cat_titles = {
            "users":     "👥  Users",
            "guild":     "🏝️  Server",
            "community": "🎉  Community",
            "meta":      "⚙️  Meta",
            "debug":     "🐞  Debug",
            "sonstiges": "📦  Sonstiges",
        }

        for cat in sorted(by_cat.keys(), key=lambda c: (c == "sonstiges", c)):
            title = cat_titles.get(cat, f"📦  {cat.title()}")
            self._add_category_header(title)

            for ep in by_cat[cat]:
                self._add_endpoint_button(ep)

    def _add_category_header(self, text):
        wrap = tk.Frame(self.ep_inner, bg=SURFACE)
        wrap.pack(fill="x", padx=8, pady=(12, 4))

        tk.Label(
            wrap, text=text,
            font=(FONT_UI, 8, "bold"),
            bg=SURFACE, fg=TEXT3,
        ).pack(anchor="w")

    def _add_endpoint_button(self, ep: dict):
        selected = (
            self._selected_endpoint
            and self._selected_endpoint.get("id") == ep.get("id")
        )

        bg = GREEN_BG if selected else SURFACE
        fg = GREEN    if selected else TEXT2
        hover = "#142e20" if selected else HOVER

        method_colors = {
            "GET":    GREEN,
            "POST":   BLUE,
            "PUT":    GOLD,
            "PATCH":  ORANGE,
            "DELETE": RED,
        }
        m_color = method_colors.get(ep.get("method", "GET"), TEXT3)

        frame = tk.Frame(self.ep_inner, bg=bg, cursor="hand2")
        frame.pack(fill="x", padx=4, pady=1)

        inner = tk.Frame(frame, bg=bg, padx=12, pady=8)
        inner.pack(fill="x")

        # Method-Dot
        dot_wrap = tk.Frame(inner, bg=bg, width=4, height=4)
        dot_wrap.pack(side="left", padx=(0, 8), pady=(6, 0))
        dot_wrap.pack_propagate(False)
        dot = tk.Frame(dot_wrap, bg=m_color, width=4, height=4)
        dot.pack(fill="both", expand=True)

        text_wrap = tk.Frame(inner, bg=bg)
        text_wrap.pack(side="left", fill="x", expand=True)

        label = tk.Label(
            text_wrap, text=ep["label"],
            font=(FONT_UI, 9, "bold"),
            bg=bg, fg=fg, anchor="w",
        )
        label.pack(anchor="w")

        path_label = tk.Label(
            text_wrap,
            text=f"{ep.get('method', 'GET')}  {ep.get('path', '')}",
            font=(FONT_MONO, 7),
            bg=bg,
            fg=TEXT3 if not selected else "#1a5e3a",
            anchor="w",
        )
        path_label.pack(anchor="w")

        all_widgets = [frame, inner, text_wrap, label, path_label, dot_wrap]

        def _apply(bg_color):
            for w in all_widgets:
                w.configure(bg=bg_color)

        def on_click(_e):
            self._select_endpoint(ep)

        def on_enter(_e):
            if selected:
                return
            _apply(hover)

        def on_leave(_e):
            if selected:
                return
            _apply(bg)

        for w in all_widgets:
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)

    # ══════════════════════════════════════════════════════════════════════════
    # ENDPUNKT AUSWÄHLEN
    # ══════════════════════════════════════════════════════════════════════════

    def _select_endpoint(self, ep: dict):
        self._selected_endpoint = ep

        self.topbar_title.configure(text=ep["label"])
        self.topbar_sub.configure(
            text=f"{ep.get('method', 'GET')}  {ep.get('path', '')}"
        )

        scope = ep.get("scope", "")
        if scope:
            self.scope_badge.configure(
                text=f"🔑 {scope}",
                bg=GREEN_BG, fg=GREEN,
            )
        else:
            self.scope_badge.configure(text="", bg=CARD2, fg=TEXT3)

        self._set_method(ep.get("method", "GET"))
        self.url_var.set(ep.get("path", ""))

        desc = ep.get("description", "")
        tags = "  ·  ".join(ep.get("tags", []))
        self.endpoint_desc.configure(
            text=f"{desc}\n" + (f"Tags: {tags}" if tags else "")
        )

        for row in list(self._param_rows):
            row["frame"].destroy()
        self._param_rows = []

        params = ep.get("params", [])
        self.param_hint.configure(
            text=f"{len(params)} definiert"
            if params else "Keine Parameter"
        )

        for p in params:
            self._add_param_row(
                key=p.get("name", ""),
                value=p.get("default", "") or p.get("example", "") or "",
                info=p.get("description", ""),
                required=bool(p.get("required")),
                example=p.get("example", ""),
            )

        if not params:
            self._add_param_row("", "", "", False, "")

        self._render_endpoint_list()

    # ══════════════════════════════════════════════════════════════════════════
    # PARAMETER-ZEILEN
    # ══════════════════════════════════════════════════════════════════════════

    def _add_param_row(self, key: str = "", value: str = "",
                       info: str = "", required: bool = False,
                       example: str = ""):
        frame = tk.Frame(self.param_container, bg=CARD)
        frame.pack(fill="x", pady=3)

        dot = tk.Frame(frame, bg=(ORANGE if required else BORDER), width=3)
        dot.pack(side="left", fill="y", padx=(0, 8))

        k_var = tk.StringVar(value=key)
        k_wrap = tk.Frame(frame, bg=BORDER2)
        k_wrap.pack(side="left", padx=(0, 6))
        k_entry = tk.Entry(
            k_wrap, textvariable=k_var, width=20,
            font=(FONT_MONO, 9, "bold"),
            bg=CARD2, fg=BLUE, insertbackground=TEXT,
            relief="flat", bd=0,
        )
        k_entry.pack(padx=1, pady=1, ipady=6, ipadx=8)
        k_entry.bind("<FocusIn>",  lambda e: k_wrap.configure(bg=GREEN2))
        k_entry.bind("<FocusOut>", lambda e: k_wrap.configure(bg=BORDER2))

        v_var = tk.StringVar(value=value)
        v_wrap = tk.Frame(frame, bg=BORDER2)
        v_wrap.pack(side="left", fill="x", expand=True, padx=(0, 6))
        v_entry = tk.Entry(
            v_wrap, textvariable=v_var,
            font=(FONT_MONO, 9),
            bg=CARD2, fg=GREEN, insertbackground=TEXT,
            relief="flat", bd=0,
        )
        v_entry.pack(fill="both", expand=True, padx=1, pady=1,
                     ipady=6, ipadx=8)
        v_entry.bind("<FocusIn>",  lambda e: v_wrap.configure(bg=GREEN2))
        v_entry.bind("<FocusOut>", lambda e: v_wrap.configure(bg=BORDER2))

        info_text = info or ""
        if example and not info:
            info_text = f"z.B. {example}"
        info_lbl = tk.Label(
            frame, text=info_text[:50],
            font=(FONT_UI, 8), bg=CARD, fg=TEXT3,
            anchor="w",
        )
        info_lbl.pack(side="left", padx=(0, 6))

        del_btn = tk.Label(
            frame, text="✕",
            font=(FONT_UI, 10, "bold"),
            bg=CARD, fg=TEXT3,
            padx=6, cursor="hand2",
        )
        del_btn.pack(side="right")
        del_btn.bind("<Button-1>", lambda e: self._remove_param_row(frame))
        del_btn.bind("<Enter>", lambda e: del_btn.configure(fg=RED))
        del_btn.bind("<Leave>", lambda e: del_btn.configure(fg=TEXT3))

        self._param_rows.append({
            "key_var": k_var, "value_var": v_var,
            "frame": frame,
        })

    def _add_param_row_empty(self):
        self._add_param_row("", "", "", False, "")

    def _remove_param_row(self, frame):
        self._param_rows = [r for r in self._param_rows if r["frame"] is not frame]
        frame.destroy()

    # ══════════════════════════════════════════════════════════════════════════
    # REQUEST-AKTIONEN
    # ══════════════════════════════════════════════════════════════════════════

    def _collect_params(self) -> dict[str, str]:
        out = {}
        for row in self._param_rows:
            k = row["key_var"].get().strip()
            v = row["value_var"].get().strip()
            if k:
                out[k] = v
        return out

    def _build_full_url(self) -> str:
        base = self.base_url_var.get().strip().rstrip("/")
        path = self.url_var.get().strip()
        if path.startswith("http://") or path.startswith("https://"):
            full = path
        else:
            if not path.startswith("/"):
                path = "/" + path
            full = f"{base}{path}"

        params = self._collect_params()
        if self.auth_var.get() == "query":
            key = self.key_var.get().strip()
            if key and "api_key" not in params:
                params["api_key"] = key

        if params:
            sep = "&" if "?" in full else "?"
            full = f"{full}{sep}{urlencode(params)}"
        return full

    def _copy_url(self):
        url = self._build_full_url()
        self.clipboard_clear()
        self.clipboard_append(url)
        self.conn_status.configure(text="📋 URL kopiert", fg=GREEN)

    def _copy_response(self):
        if not self._last_response:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(self.raw_text.get("1.0", tk.END).strip())
            self.conn_status.configure(text="📋 Antwort kopiert", fg=GREEN)
        except Exception:
            pass

    def _clear_form(self):
        for row in list(self._param_rows):
            row["value_var"].set("")
        self.raw_text.delete("1.0", tk.END)
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.lb_tree.get_children():
            self.lb_tree.delete(item)
        self.status_badge.configure(
            text="—", fg=TEXT3, bg=CARD,
            highlightbackground=BORDER2,
        )
        self.time_label.configure(text="")
        self._last_response = None

    # ══════════════════════════════════════════════════════════════════════════
    # REQUEST SENDEN
    # ══════════════════════════════════════════════════════════════════════════

    def _on_send(self):
        url = self._build_full_url()
        if not url:
            messagebox.showwarning("Fehlt",
                                   "Bitte URL eingeben oder Endpunkt wählen.")
            return

        method = self.method_var.get().upper()

        headers = {"Accept": "application/json"}
        api_key = self.key_var.get().strip()
        auth_mode = self.auth_var.get()
        if api_key:
            if auth_mode == "header":
                headers["X-API-Key"] = api_key
            elif auth_mode == "bearer":
                headers["Authorization"] = f"Bearer {api_key}"

        self.send_btn.configure(
            state="disabled", text="  ⏳  Sende…",
            bg=BORDER2,
        )
        self.status_badge.configure(
            text="Wird gesendet…",
            fg=ORANGE, bg=CARD,
            highlightbackground=BORDER2,
        )

        threading.Thread(
            target=self._do_request,
            args=(method, url, headers),
            daemon=True,
        ).start()

    def _do_request(self, method: str, url: str, headers: dict):
        start = datetime.now()
        try:
            resp = requests.request(
                method=method, url=url, headers=headers, timeout=15,
            )
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000
            self.after(0, self._on_response, resp, elapsed_ms)
        except requests.exceptions.Timeout:
            self.after(0, self._on_error,
                       "Timeout – Server hat nicht innerhalb von 15s geantwortet.")
        except requests.exceptions.ConnectionError as e:
            self.after(0, self._on_error,
                       f"Verbindungsfehler – läuft die Flask-App?\n{e}")
        except Exception as e:
            self.after(0, self._on_error, f"Fehler: {e}")

    def _on_response(self, resp: requests.Response, elapsed_ms: float):
        self._last_response = resp
        self.send_btn.configure(
            state="normal", text="  ▶  Anfrage senden",
            bg=GREEN2,
        )

        if 200 <= resp.status_code < 300:
            color, bg_badge = GREEN, GREEN_BG
        elif 300 <= resp.status_code < 400:
            color, bg_badge = BLUE, BLUE_BG
        elif resp.status_code == 429:
            color, bg_badge = ORANGE, "#2e1f0d"
        elif 400 <= resp.status_code < 500:
            color, bg_badge = GOLD, "#2e2610"
        else:
            color, bg_badge = RED, "#2e0f0f"

        self.status_badge.configure(
            text=f"{resp.status_code}  {resp.reason}",
            fg=color, bg=bg_badge,
            highlightbackground=color,
        )
        self.time_label.configure(
            text=f"·  {elapsed_ms:.0f} ms  ·  {len(resp.content)} bytes"
        )

        self.raw_text.delete("1.0", tk.END)
        data = None
        try:
            data = resp.json()
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            self._insert_json_pretty(pretty)
        except Exception:
            self.raw_text.insert("1.0", resp.text or "(leere Antwort)")

        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.lb_tree.get_children():
            self.lb_tree.delete(item)

        if isinstance(data, dict):
            if isinstance(data.get("users"), list) and data["users"]:
                for u in data["users"]:
                    is_on = bool(u.get("is_online"))
                    tag = "online" if is_on else "offline"
                    self.tree.insert("", "end", values=(
                        u.get("id", ""),
                        u.get("display_name") or u.get("username") or "",
                        u.get("username", ""),
                        u.get("status", ""),
                        "✅" if is_on else "—",
                        "🤖" if u.get("is_bot") else "",
                    ), tags=(tag,))
                self.notebook.select(1)
            elif isinstance(data.get("top"), list) and data["top"]:
                for e in data["top"]:
                    rank = e.get("rank", 0)
                    tag = ""
                    if rank == 1:
                        tag = "gold"
                    elif rank == 2:
                        tag = "silver"
                    elif rank == 3:
                        tag = "bronze"
                    self.lb_tree.insert("", "end", values=(
                        rank,
                        e.get("display_name", ""),
                        e.get("level", ""),
                        e.get("xp", ""),
                        e.get("messages", ""),
                        e.get("voice_minutes", ""),
                    ), tags=(tag,) if tag else ())
                self.notebook.select(2)

        entry = {
            "time":   datetime.now().strftime("%H:%M:%S"),
            "method": resp.request.method,
            "url":    resp.url,
            "status": resp.status_code,
            "ms":     elapsed_ms,
        }
        self._history.append(entry)
        line = (f"  {entry['time']}   {entry['method']:<6} "
                f"{entry['status']}   {entry['ms']:>5.0f}ms   "
                f"{entry['url']}")
        self.history_list.insert(tk.END, line)
        self.history_list.see(tk.END)

    def _insert_json_pretty(self, text: str):
        """Fügt JSON mit einfachem Syntax-Highlighting ein."""
        token_re = re.compile(
            r'("(?:[^"\\]|\\.)*")\s*:|'
            r'("(?:[^"\\]|\\.)*")|'
            r'\b(true|false)\b|'
            r'\bnull\b|'
            r'(-?\d+\.?\d*(?:[eE][+-]?\d+)?)|'
            r'([{}\[\],:])'
        )
        pos = 0
        for m in token_re.finditer(text):
            if m.start() > pos:
                self.raw_text.insert(tk.END, text[pos:m.start()])
            if m.group(1):
                self.raw_text.insert(tk.END, m.group(1), "key")
                self.raw_text.insert(tk.END, ":", "punct")
            elif m.group(2):
                self.raw_text.insert(tk.END, m.group(2), "string")
            elif m.group(3):
                self.raw_text.insert(tk.END, m.group(3), "bool")
            elif m.group(0) == "null":
                self.raw_text.insert(tk.END, "null", "null")
            elif m.group(4):
                self.raw_text.insert(tk.END, m.group(4), "number")
            else:
                self.raw_text.insert(tk.END, m.group(0), "punct")
            pos = m.end()
        if pos < len(text):
            self.raw_text.insert(tk.END, text[pos:])

    def _on_error(self, msg: str):
        self.send_btn.configure(
            state="normal", text="  ▶  Anfrage senden",
            bg=GREEN2,
        )
        self.status_badge.configure(
            text="FEHLER",
            fg=RED, bg="#2e0f0f",
            highlightbackground=RED,
        )
        self.time_label.configure(text="")
        self.raw_text.delete("1.0", tk.END)
        self.raw_text.insert("1.0", msg, "null")
        self.notebook.select(0)

    # ══════════════════════════════════════════════════════════════════════════
    # VERLAUF
    # ══════════════════════════════════════════════════════════════════════════

    def _on_history_select(self, event):
        sel = self.history_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._history):
            entry = self._history[idx]
            base  = entry["url"].split("?")[0]
            query = entry["url"].split("?")[1] if "?" in entry["url"] else ""

            self.url_var.set(base)
            self._set_method(entry["method"])

            for row in list(self._param_rows):
                row["frame"].destroy()
            self._param_rows = []

            if query:
                for pair in query.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        self._add_param_row(k, v, "", False, "")
            else:
                self._add_param_row("", "", "", False, "")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = ApiTesterApp()
    app.mainloop()