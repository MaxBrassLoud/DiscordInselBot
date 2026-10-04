# bot/tools/api_tester_gui.py
"""
API-Tester – Tkinter UI
========================
Stellt eine Anfrage an die Open-API des Insel Bots und zeigt die Antwort.

Verwendung:
    python -m bot.tools.api_tester_gui

Unterstützt:
  • Beliebige URL / Endpoint
  • API-Key als Header, Bearer oder Query-Parameter
  • Query-Parameter-Editor
  • Roh-JSON- und Tabellen-Ansicht
  • History der letzten Anfragen (Session)

Benötigt:
    pip install requests
"""
from __future__ import annotations

import json
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
# STYLE
# ══════════════════════════════════════════════════════════════════════════════

BG       = "#0a0b0d"
CARD     = "#1d2128"
CARD2    = "#181c22"
BORDER   = "#252b35"
BORDER2  = "#2e3540"
TEXT     = "#e8edf5"
TEXT2    = "#8b95a8"
TEXT3    = "#555f6e"
GREEN    = "#4ade80"
GREEN2   = "#22c55e"
RED      = "#f87171"
ORANGE   = "#fb923c"
BLUE     = "#60a5fa"
GOLD     = "#fbbf24"


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

class ApiTesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🧪 API-Tester – Insel Bot")
        self.geometry("980x820")
        self.minsize(820, 700)
        self.configure(bg=BG)

        # Zustand
        self._last_response: requests.Response | None = None
        self._history: list[dict] = []

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ───────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=BG, pady=18)
        header.pack(fill="x", padx=24)

        tk.Label(
            header, text="🧪 API-Tester",
            font=("Segoe UI", 18, "bold"), bg=BG, fg=TEXT,
        ).pack(side="left")

        tk.Label(
            header, text="Open API · Insel Bot",
            font=("Segoe UI", 9), bg=BG, fg=TEXT3,
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        # ── Request-Karte ────────────────────────────────────────────────────
        req = self._card(self)
        req.pack(fill="x", padx=24, pady=(0, 12))

        tk.Label(
            req, text="REQUEST",
            font=("Segoe UI", 8, "bold"), bg=CARD, fg=TEXT3,
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))

        # Methode + URL
        tk.Label(req, text="Methode:", font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=1, column=0, sticky="w", pady=4)

        self.method_var = tk.StringVar(value="GET")
        method_cb = ttk.Combobox(
            req, textvariable=self.method_var,
            values=["GET", "POST", "PUT", "PATCH", "DELETE"],
            width=8, state="readonly", font=("Segoe UI", 10),
        )
        method_cb.grid(row=1, column=1, sticky="w", padx=(10, 10), pady=4)

        tk.Label(req, text="URL:", font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=1, column=2, sticky="w", pady=4)

        self.url_var = tk.StringVar(
            value="http://localhost:5000/open-api/users"
        )
        self._entry(req, self.url_var, width=60
                    ).grid(row=1, column=3, sticky="ew", padx=(10, 0), pady=4)

        # API-Key + Auth-Modus
        tk.Label(req, text="API-Key:", font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=2, column=0, sticky="w", pady=4)

        self.key_var = tk.StringVar(value="")
        self._entry(req, self.key_var, width=40
                    ).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(10, 10), pady=4)

        tk.Label(req, text="Auth:", font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=2, column=3, sticky="w", pady=4)

        auth_frame = tk.Frame(req, bg=CARD)
        auth_frame.grid(row=3, column=3, sticky="w", pady=(0, 4))

        self.auth_var = tk.StringVar(value="header")  # header | bearer | query
        for val, lbl in [("header", "X-API-Key"),
                         ("bearer", "Bearer"),
                         ("query", "?api_key")]:
            tk.Radiobutton(
                auth_frame, text=lbl, variable=self.auth_var, value=val,
                font=("Segoe UI", 8), bg=CARD, fg=TEXT2,
                selectcolor=CARD2, activebackground=CARD, activeforeground=TEXT,
                highlightthickness=0, bd=0,
            ).pack(side="left", padx=(0, 12))

        # Query-Parameter
        tk.Label(req, text="Query-Parameter:", font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(10, 4))

        param_frame = tk.Frame(req, bg=CARD)
        param_frame.grid(row=5, column=0, columnspan=4, sticky="ew")

        # Header-Zeile
        tk.Label(param_frame, text="Key", font=("Segoe UI", 8, "bold"),
                 bg=CARD, fg=TEXT3, width=20, anchor="w"
                 ).grid(row=0, column=0, padx=(0, 4))
        tk.Label(param_frame, text="Wert", font=("Segoe UI", 8, "bold"),
                 bg=CARD, fg=TEXT3, width=40, anchor="w"
                 ).grid(row=0, column=1, padx=(0, 4))

        self.param_rows: list[tuple[tk.StringVar, tk.StringVar, tk.Frame]] = []
        self.param_container = tk.Frame(param_frame, bg=CARD)
        self.param_container.grid(row=1, column=0, columnspan=3, sticky="ew")
        param_frame.columnconfigure(1, weight=1)

        # Start mit 2 leeren Zeilen
        self._add_param_row("role", "")
        self._add_param_row("online", "true")

        # Buttons unter den Parametern
        add_btn = self._btn(param_frame, "➕  Parameter hinzufügen",
                            command=self._add_param_row_empty,
                            bg=CARD2, fg=TEXT2)
        add_btn.grid(row=2, column=0, sticky="w", pady=(6, 0))

        # Save/Load-Preset
        preset_btn = self._btn(param_frame, "💾  Als Preset speichern",
                               command=self._save_preset,
                               bg=CARD2, fg=TEXT2)
        preset_btn.grid(row=2, column=1, sticky="w", pady=(6, 0), padx=(6, 0))

        load_btn = self._btn(param_frame, "📂  Preset laden",
                             command=self._load_preset,
                             bg=CARD2, fg=TEXT2)
        load_btn.grid(row=2, column=2, sticky="w", pady=(6, 0), padx=(6, 0))

        # Send-Button
        btn_row = tk.Frame(req, bg=CARD)
        btn_row.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(14, 0))

        self.send_btn = self._btn(
            btn_row, "🚀  Anfrage senden",
            command=self._on_send,
            bg=GREEN2, fg="#000", width=24,
        )
        self.send_btn.pack(side="left")

        self._btn(btn_row, "📋  URL kopieren",
                  command=self._copy_url,
                  bg=CARD2, fg=TEXT2, width=18).pack(side="left", padx=(8, 0))

        self._btn(btn_row, "🗑️  Felder leeren",
                  command=self._clear_form,
                  bg=CARD2, fg=TEXT2, width=18).pack(side="left", padx=(8, 0))

        self._btn(btn_row, "🎯  Beispiel laden",
                  command=self._load_example,
                  bg=CARD2, fg=TEXT2, width=20).pack(side="left", padx=(8, 0))

        req.columnconfigure(3, weight=1)

        # ── Response-Karte ───────────────────────────────────────────────────
        resp = self._card(self)
        resp.pack(fill="both", expand=True, padx=24, pady=(0, 12))

        # Response-Header-Zeile
        resp_header = tk.Frame(resp, bg=CARD)
        resp_header.pack(fill="x", pady=(0, 8))

        tk.Label(resp_header, text="RESPONSE",
                 font=("Segoe UI", 8, "bold"), bg=CARD, fg=TEXT3
                 ).pack(side="left")

        self.status_badge = tk.Label(
            resp_header, text="—",
            font=("Segoe UI", 9, "bold"), bg=CARD, fg=TEXT3,
            padx=8, pady=2,
        )
        self.status_badge.pack(side="left", padx=(12, 0))

        self.time_label = tk.Label(
            resp_header, text="",
            font=("Segoe UI", 9), bg=CARD, fg=TEXT3,
        )
        self.time_label.pack(side="left", padx=(12, 0))

        # Response-Tabs (Raw JSON / Users-Tabelle)
        self.notebook = ttk.Notebook(resp)
        self.notebook.pack(fill="both", expand=True)

        # Tab 1: Raw JSON
        raw_frame = tk.Frame(self.notebook, bg=CARD2)
        self.notebook.add(raw_frame, text="📄  Roh-JSON")

        self.raw_text = tk.Text(
            raw_frame, font=("Courier New", 9),
            bg=CARD2, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, wrap="none",
            highlightthickness=0,
        )
        self.raw_text.pack(fill="both", expand=True, side="left")
        raw_sb_y = tk.Scrollbar(raw_frame, command=self.raw_text.yview,
                                bg=BORDER2, troughcolor=CARD2, bd=0, relief="flat", width=8)
        raw_sb_y.pack(side="right", fill="y")
        self.raw_text.configure(yscrollcommand=raw_sb_y.set)

        # Tab 2: Users-Tabelle (wenn /open-api/users)
        table_frame = tk.Frame(self.notebook, bg=CARD2)
        self.notebook.add(table_frame, text="👥  Users-Tabelle")

        cols = ("id", "display_name", "username", "status", "is_online", "is_bot")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=10)
        headers = {
            "id":           ("User-ID",      140, "w"),
            "display_name": ("Anzeigename",  180, "w"),
            "username":     ("Username",     140, "w"),
            "status":       ("Status",        90, "center"),
            "is_online":    ("Online",         70, "center"),
            "is_bot":       ("Bot",            50, "center"),
        }
        for col, (label, width, anchor) in headers.items():
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, anchor=anchor)

        self.tree.pack(fill="both", expand=True, side="left")
        tree_sb_y = tk.Scrollbar(table_frame, command=self.tree.yview,
                                 bg=BORDER2, troughcolor=CARD2, bd=0, relief="flat", width=8)
        tree_sb_y.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=tree_sb_y.set)

        # Tab 3: History
        hist_frame = tk.Frame(self.notebook, bg=CARD2)
        self.notebook.add(hist_frame, text="🕓  Verlauf")

        self.history_list = tk.Listbox(
            hist_frame, font=("Courier New", 9),
            bg=CARD2, fg=TEXT2, selectbackground=BORDER2, selectforeground=TEXT,
            relief="flat", bd=0, highlightthickness=0,
        )
        self.history_list.pack(fill="both", expand=True, side="left")
        self.history_list.bind("<Double-Button-1>", self._on_history_select)
        hist_sb = tk.Scrollbar(hist_frame, command=self.history_list.yview,
                               bg=BORDER2, troughcolor=CARD2, bd=0, relief="flat", width=8)
        hist_sb.pack(side="right", fill="y")
        self.history_list.configure(yscrollcommand=hist_sb.set)

    # ── Widget-Helper ─────────────────────────────────────────────────────────

    def _card(self, parent):
        return tk.Frame(
            parent, bg=CARD, padx=20, pady=16,
            highlightthickness=1, highlightbackground=BORDER,
        )

    def _entry(self, parent, var, width=30):
        return tk.Entry(
            parent, textvariable=var, width=width,
            font=("Segoe UI", 10),
            bg=CARD2, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER2,
            highlightcolor=GREEN2,
        )

    def _btn(self, parent, text, command, bg=CARD2, fg=TEXT, width=None):
        kwargs = dict(
            text=text, command=command,
            font=("Segoe UI", 9, "bold"),
            bg=bg, fg=fg,
            activebackground=BORDER2, activeforeground=TEXT,
            relief="flat", bd=0, padx=12, pady=7, cursor="hand2",
        )
        if width:
            kwargs["width"] = width
        btn = tk.Button(parent, **kwargs)
        btn.bind("<Enter>", lambda e: btn.configure(bg=self._lighten(bg)))
        btn.bind("<Leave>", lambda e: btn.configure(bg=bg))
        return btn

    @staticmethod
    def _lighten(hex_color: str) -> str:
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            return f"#{min(255,r+25):02x}{min(255,g+25):02x}{min(255,b+25):02x}"
        except Exception:
            return hex_color

    # ── Param-Zeilen ──────────────────────────────────────────────────────────

    def _add_param_row(self, key: str = "", value: str = ""):
        row = tk.Frame(self.param_container, bg=CARD)
        row.pack(fill="x", pady=2)

        k_var = tk.StringVar(value=key)
        v_var = tk.StringVar(value=value)

        k_entry = self._entry(row, k_var, width=20)
        k_entry.pack(side="left", padx=(0, 6))

        v_entry = self._entry(row, v_var, width=52)
        v_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        del_btn = tk.Button(
            row, text="✕",
            command=lambda: self._remove_param_row(row, k_var, v_var, row),
            font=("Segoe UI", 9, "bold"),
            bg=CARD2, fg=RED,
            activebackground=BORDER2, activeforeground=RED,
            relief="flat", bd=0, padx=8, pady=4, cursor="hand2",
        )
        del_btn.pack(side="right")

        self.param_rows.append((k_var, v_var, row))

    def _add_param_row_empty(self):
        self._add_param_row("", "")

    def _remove_param_row(self, row, k_var, v_var, frame):
        self.param_rows = [
            (k, v, f) for (k, v, f) in self.param_rows if f is not frame
        ]
        frame.destroy()

    # ── Form-Aktionen ─────────────────────────────────────────────────────────

    def _collect_params(self) -> dict[str, str]:
        out = {}
        for k_var, v_var, _ in self.param_rows:
            k = k_var.get().strip()
            v = v_var.get().strip()
            if k:
                out[k] = v
        return out

    def _build_url(self) -> str:
        base = self.url_var.get().strip()
        params = self._collect_params()

        if self.auth_var.get() == "query":
            key = self.key_var.get().strip()
            if key and "api_key" not in params:
                params["api_key"] = key

        if params:
            sep = "&" if "?" in base else "?"
            return f"{base}{sep}{urlencode(params)}"
        return base

    def _copy_url(self):
        url = self._build_url()
        self.clipboard_clear()
        self.clipboard_append(url)
        messagebox.showinfo("Kopiert", f"URL in Zwischenablage:\n\n{url}")

    def _clear_form(self):
        for k_var, v_var, _ in list(self.param_rows):
            v_var.set("")
        self.raw_text.delete("1.0", tk.END)
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.status_badge.configure(text="—", fg=TEXT3, bg=CARD)
        self.time_label.configure(text="")
        self._last_response = None

    def _load_example(self):
        self.url_var.set("http://localhost:5000/open-api/users")
        self.method_var.set("GET")
        # Params zurücksetzen
        for _, _, frame in list(self.param_rows):
            frame.destroy()
        self.param_rows = []
        self._add_param_row("role", "1410628676499800196")
        self._add_param_row("online", "true")
        self._add_param_row("guild_id", "1253751493513969735")

    def _save_preset(self):
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
            initialfile="api_preset.json",
            title="Preset speichern",
        )
        if not path:
            return
        data = {
            "url":      self.url_var.get(),
            "method":   self.method_var.get(),
            "auth":     self.auth_var.get(),
            "key":      self.key_var.get(),
            "params":   self._collect_params(),
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            messagebox.showinfo("Gespeichert", f"Preset gespeichert:\n{path}")
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte nicht speichern:\n{e}")

    def _load_preset(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            filetypes=[("JSON", "*.json"), ("Alle Dateien", "*.*")],
            title="Preset laden",
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.url_var.set(data.get("url", ""))
            self.method_var.set(data.get("method", "GET"))
            self.auth_var.set(data.get("auth", "header"))
            self.key_var.set(data.get("key", ""))
            for _, _, frame in list(self.param_rows):
                frame.destroy()
            self.param_rows = []
            for k, v in (data.get("params") or {}).items():
                self._add_param_row(k, v)
            messagebox.showinfo("Geladen", f"Preset geladen:\n{path}")
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte nicht laden:\n{e}")

    # ── Senden ────────────────────────────────────────────────────────────────

    def _on_send(self):
        url = self._build_url()
        if not url:
            messagebox.showwarning("Fehlt", "Bitte eine URL eingeben.")
            return

        method = self.method_var.get().upper()
        api_key = self.key_var.get().strip()
        auth_mode = self.auth_var.get()

        headers = {"Accept": "application/json"}
        if api_key:
            if auth_mode == "header":
                headers["X-API-Key"] = api_key
            elif auth_mode == "bearer":
                headers["Authorization"] = f"Bearer {api_key}"
            # query-Modus ist schon in der URL

        self.send_btn.configure(state="disabled", text="⏳  Sende…")
        self.status_badge.configure(text="…", fg=ORANGE, bg=CARD)

        threading.Thread(
            target=self._do_request,
            args=(method, url, headers),
            daemon=True,
        ).start()

    def _do_request(self, method: str, url: str, headers: dict):
        start = datetime.now()
        try:
            resp = requests.request(
                method=method, url=url, headers=headers,
                timeout=15,
            )
            elapsed_ms = (datetime.now() - start).total_seconds() * 1000
            self.after(0, self._on_response, resp, elapsed_ms)
        except requests.exceptions.Timeout:
            self.after(0, self._on_error, "Timeout – Server hat nicht innerhalb von 15s geantwortet.")
        except requests.exceptions.ConnectionError as e:
            self.after(0, self._on_error, f"Verbindungsfehler – läuft die Flask-App?\n{e}")
        except Exception as e:
            self.after(0, self._on_error, f"Fehler: {e}")

    def _on_response(self, resp: requests.Response, elapsed_ms: float):
        self._last_response = resp
        self.send_btn.configure(state="normal", text="🚀  Anfrage senden")

        # Status-Badge
        if 200 <= resp.status_code < 300:
            color = GREEN
        elif 300 <= resp.status_code < 400:
            color = BLUE
        elif resp.status_code == 429:
            color = ORANGE
        elif 400 <= resp.status_code < 500:
            color = GOLD
        else:
            color = RED
        self.status_badge.configure(
            text=f"{resp.status_code} {resp.reason}",
            fg=color, bg=CARD,
        )
        self.time_label.configure(text=f"· {elapsed_ms:.0f} ms · {len(resp.content)} bytes")

        # Raw JSON pretty-printen
        self.raw_text.delete("1.0", tk.END)
        try:
            data = resp.json()
            pretty = json.dumps(data, indent=2, ensure_ascii=False)
            self.raw_text.insert("1.0", pretty)
        except Exception:
            self.raw_text.insert("1.0", resp.text or "(leere Antwort)")

        # Users-Tabelle befüllen (falls passend)
        for item in self.tree.get_children():
            self.tree.delete(item)
        try:
            data = resp.json()
            if isinstance(data, dict) and isinstance(data.get("users"), list):
                for u in data["users"]:
                    self.tree.insert("", "end", values=(
                        u.get("id", ""),
                        u.get("display_name") or u.get("username") or "",
                        u.get("username", ""),
                        u.get("status", ""),
                        "✅" if u.get("is_online") else "—",
                        "🤖" if u.get("is_bot") else "",
                    ))
                if data["users"]:
                    self.notebook.select(1)   # Users-Tab
        except Exception:
            pass

        # History-Eintrag
        entry = {
            "time":     datetime.now().strftime("%H:%M:%S"),
            "method":   resp.request.method,
            "url":      resp.url,
            "status":   resp.status_code,
            "ms":       elapsed_ms,
        }
        self._history.append(entry)
        self.history_list.insert(
            tk.END,
            f"[{entry['time']}] {entry['method']} {entry['status']} "
            f"({entry['ms']:.0f}ms) {entry['url']}"
        )
        self.history_list.see(tk.END)

    def _on_error(self, msg: str):
        self.send_btn.configure(state="normal", text="🚀  Anfrage senden")
        self.status_badge.configure(text="FEHLER", fg=RED, bg=CARD)
        self.time_label.configure(text="")
        self.raw_text.delete("1.0", tk.END)
        self.raw_text.insert("1.0", msg)
        self.notebook.select(0)

    # ── History ───────────────────────────────────────────────────────────────

    def _on_history_select(self, event):
        sel = self.history_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if 0 <= idx < len(self._history):
            entry = self._history[idx]
            # URL + Methode in Formular übernehmen
            base = entry["url"].split("?")[0]
            query = entry["url"].split("?")[1] if "?" in entry["url"] else ""
            self.url_var.set(base)
            self.method_var.set(entry["method"])

            # Params aus URL lösen
            for _, _, frame in list(self.param_rows):
                frame.destroy()
            self.param_rows = []
            if query:
                for pair in query.split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        self._add_param_row(k, v)
            else:
                self._add_param_row("", "")


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = ApiTesterApp()
    app.mainloop()