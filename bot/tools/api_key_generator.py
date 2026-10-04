# bot/tools/api_key_generator_gui.py
"""
API-Key-Generator – Tkinter UI
================================
Erzeugt neue API-Keys und speichert NUR den SHA-256-Hash in Supabase.
Der Klartext-Key wird einmalig angezeigt und kann kopiert werden.

Verwendung:
    python -m bot.tools.api_key_generator_gui

Benötigt:
    pip install supabase python-dotenv
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sys
import threading
import tkinter as tk
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, ttk

# ── Projekt-Root zu sys.path hinzufügen ───────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

try:
    from bot.core.supabase_client import init_supabase, get_supabase
    _SUPABASE_IMPORT_OK = True
except Exception as e:
    _SUPABASE_IMPORT_OK = False
    _SUPABASE_IMPORT_ERR = str(e)


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
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def generate_api_key() -> str:
    """URL-sicherer Key mit erkennbarem Präfix."""
    return "insel_" + secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════════

class ApiKeyGeneratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🔑 API-Key Generator – Insel Bot")
        self.geometry("760x680")
        self.minsize(680, 620)
        self.configure(bg=BG)

        self._generated_key: str | None = None
        self._supabase_ready = False

        self._build_ui()
        self._init_supabase()

    # ── Supabase Init ─────────────────────────────────────────────────────────

    def _init_supabase(self):
        if not _SUPABASE_IMPORT_OK:
            self._set_status(f"❌ Supabase-Import fehlgeschlagen: {_SUPABASE_IMPORT_ERR}", RED)
            return
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_KEY", "").strip()
        if not url or not key:
            self._set_status("❌ SUPABASE_URL / SUPABASE_KEY fehlen in .env", RED)
            return
        try:
            init_supabase(url, key)
            self._supabase_ready = True
            self._set_status("✅ Verbunden mit Supabase", GREEN)
        except Exception as e:
            self._set_status(f"❌ Supabase-Fehler: {e}", RED)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Header ───────────────────────────────────────────────────────────
        header = tk.Frame(self, bg=BG, pady=22)
        header.pack(fill="x", padx=28)

        tk.Label(
            header, text="🔑 API-Key Generator",
            font=("Segoe UI", 18, "bold"), bg=BG, fg=TEXT,
        ).pack(side="left")

        tk.Label(
            header, text="Insel Bot · Open API",
            font=("Segoe UI", 9), bg=BG, fg=TEXT3,
        ).pack(side="left", padx=(12, 0), pady=(6, 0))

        # ── Settings-Karte ───────────────────────────────────────────────────
        settings = tk.Frame(
            self, bg=CARD, padx=20, pady=18,
            highlightthickness=1, highlightbackground=BORDER,
        )
        settings.pack(fill="x", padx=28, pady=(0, 14))

        tk.Label(
            settings, text="KEY-EINSTELLUNGEN",
            font=("Segoe UI", 8, "bold"), bg=CARD, fg=TEXT3,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        # Label
        tk.Label(settings, text="Bezeichnung:",
                 font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=1, column=0, sticky="w", pady=4)

        self.label_var = tk.StringVar(value="Insel Website")
        self._entry(settings, self.label_var, width=40
                    ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=4)

        # Guild-ID
        tk.Label(settings, text="Server-ID (optional):",
                 font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=2, column=0, sticky="w", pady=4)

        self.guild_var = tk.StringVar(value="")
        self._entry(settings, self.guild_var, width=40
                    ).grid(row=2, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=4)

        tk.Label(settings, text="leer = für alle Server",
                 font=("Segoe UI", 8), bg=CARD, fg=TEXT3
                 ).grid(row=3, column=1, columnspan=2, sticky="w", padx=(10, 0))

        # Scopes
        tk.Label(settings, text="Scopes:",
                 font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=4, column=0, sticky="w", pady=(10, 4))

        self.scopes_var = tk.StringVar(value="users.read")
        self._entry(settings, self.scopes_var, width=40
                    ).grid(row=4, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 4))

        tk.Label(settings, text="Komma-separiert · aktuell verfügbar: users.read, admin",
                 font=("Segoe UI", 8), bg=CARD, fg=TEXT3
                 ).grid(row=5, column=1, columnspan=2, sticky="w", padx=(10, 0))

        # Rate-Limit
        tk.Label(settings, text="Rate-Limit:",
                 font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=6, column=0, sticky="w", pady=(10, 4))

        rl_frame = tk.Frame(settings, bg=CARD)
        rl_frame.grid(row=6, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 4))

        self.rate_var = tk.StringVar(value="60")
        self._entry(rl_frame, self.rate_var, width=8).pack(side="left")
        tk.Label(rl_frame, text="Anfragen pro Minute",
                 font=("Segoe UI", 9), bg=CARD, fg=TEXT3
                 ).pack(side="left", padx=(8, 0))

        # Ablaufdatum
        tk.Label(settings, text="Läuft ab nach:",
                 font=("Segoe UI", 10), bg=CARD, fg=TEXT2
                 ).grid(row=7, column=0, sticky="w", pady=(10, 4))

        exp_frame = tk.Frame(settings, bg=CARD)
        exp_frame.grid(row=7, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 4))

        self.expires_var = tk.StringVar(value="0")
        self._entry(exp_frame, self.expires_var, width=8).pack(side="left")
        tk.Label(exp_frame, text="Tagen (0 = nie)",
                 font=("Segoe UI", 9), bg=CARD, fg=TEXT3
                 ).pack(side="left", padx=(8, 0))

        settings.columnconfigure(1, weight=1)

        # ── Generate Button ──────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=(4, 14))

        self.gen_btn = self._btn(
            btn_frame, "✨  API-Key generieren",
            command=self._on_generate,
            bg=GREEN2, fg="#000", width=26,
        )
        self.gen_btn.pack()

        # ── Status ───────────────────────────────────────────────────────────
        self.status_label = tk.Label(
            self, text="", font=("Segoe UI", 9), bg=BG, fg=TEXT3,
        )
        self.status_label.pack(pady=(0, 10))

        # ── Result-Karte (nur sichtbar nach Generation) ──────────────────────
        self.result_card = tk.Frame(
            self, bg=CARD, padx=20, pady=18,
            highlightthickness=1, highlightbackground=BORDER2,
        )
        # wird on-demand gepackt

        rk_title = tk.Frame(self.result_card, bg=CARD)
        rk_title.pack(fill="x")

        tk.Label(
            rk_title, text="⚠️  API-Key – nur JETZT sichtbar!",
            font=("Segoe UI", 10, "bold"), bg=CARD, fg=GOLD,
        ).pack(side="left")

        tk.Label(
            self.result_card,
            text="Der Klartext-Key wird nirgends gespeichert (nur der SHA-256-Hash).\n"
                 "Kopiere ihn jetzt und bewahre ihn sicher auf.",
            font=("Segoe UI", 8), bg=CARD, fg=TEXT3,
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

        # Key-Textfeld
        key_frame = tk.Frame(self.result_card, bg=CARD)
        key_frame.pack(fill="x")

        self.key_text = tk.Text(
            key_frame, height=3, font=("Courier New", 10, "bold"),
            bg=CARD2, fg=GREEN, insertbackground=TEXT,
            relief="flat", bd=0, wrap="char",
            highlightthickness=1, highlightbackground=BORDER2,
        )
        self.key_text.pack(fill="x", side="left", expand=True)
        self.key_text.configure(state="disabled")

        # Key-Aktionen
        key_actions = tk.Frame(self.result_card, bg=CARD)
        key_actions.pack(fill="x", pady=(10, 0))

        self._btn(key_actions, "📋  Key kopieren",
                  command=self._copy_key,
                  bg=BLUE, fg="#000", width=18).pack(side="left", padx=(0, 6))

        self._btn(key_actions, "💾  Als Datei speichern",
                  command=self._save_key,
                  bg=CARD2, fg=TEXT, width=22).pack(side="left", padx=(0, 6))

        self._btn(key_actions, "🔒  Key ausblenden",
                  command=self._hide_result,
                  bg=CARD2, fg=TEXT, width=20).pack(side="left")

        # ── Log-Bereich ──────────────────────────────────────────────────────
        log_frame = tk.Frame(self, bg=BG)
        log_frame.pack(fill="both", expand=True, padx=28, pady=(4, 20))

        tk.Label(
            log_frame, text="VERLAUF",
            font=("Segoe UI", 8, "bold"), bg=BG, fg=TEXT3,
        ).pack(anchor="w", pady=(0, 4))

        self.log_text = tk.Text(
            log_frame, height=8, font=("Courier New", 8),
            bg=CARD2, fg=TEXT2, insertbackground=TEXT,
            relief="flat", bd=0, wrap="word",
            highlightthickness=1, highlightbackground=BORDER,
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")

    # ── Widget-Helper ─────────────────────────────────────────────────────────

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
            relief="flat", bd=0, padx=14, pady=8, cursor="hand2",
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

    # ── Status / Log ──────────────────────────────────────────────────────────

    def _set_status(self, text: str, color: str = TEXT3):
        self.status_label.configure(text=text, fg=color)

    def _log(self, text: str, color: str | None = None):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, f"[{ts}] {text}\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    # ── Generate ──────────────────────────────────────────────────────────────

    def _on_generate(self):
        if not self._supabase_ready:
            messagebox.showerror(
                "Supabase nicht bereit",
                "Die Verbindung zu Supabase konnte nicht hergestellt werden.\n\n"
                "Prüfe SUPABASE_URL und SUPABASE_KEY in der .env-Datei.",
            )
            return

        label = self.label_var.get().strip()
        if not label:
            messagebox.showwarning("Fehlt", "Bitte gib eine Bezeichnung ein.")
            return

        guild_id = self.guild_var.get().strip() or None
        if guild_id and not guild_id.isdigit():
            messagebox.showwarning("Ungültig", "Server-ID muss numerisch sein.")
            return

        scopes = self.scopes_var.get().strip() or "users.read"

        try:
            rate_limit = int(self.rate_var.get().strip() or "60")
            if rate_limit < 1 or rate_limit > 10000:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Ungültig", "Rate-Limit muss eine Zahl zwischen 1 und 10000 sein.")
            return

        try:
            expires_days = int(self.expires_var.get().strip() or "0")
            if expires_days < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Ungültig", "Ablauf muss eine Zahl ≥ 0 sein.")
            return

        # Button sperren, Arbeit im Hintergrund
        self.gen_btn.configure(state="disabled", text="⏳  Generiere…")
        self._set_status("Erzeuge API-Key…", ORANGE)

        threading.Thread(
            target=self._do_generate,
            args=(label, guild_id, scopes, rate_limit, expires_days),
            daemon=True,
        ).start()

    def _do_generate(self, label, guild_id, scopes, rate_limit, expires_days):
        try:
            raw_key  = generate_api_key()
            key_hash = hash_key(raw_key)

            expires_at = None
            if expires_days > 0:
                expires_at = (
                    datetime.now(timezone.utc) + timedelta(days=expires_days)
                ).isoformat()

            sb = get_supabase()
            sb.table("api_keys").insert({
                "key_hash":   key_hash,
                "label":      label,
                "guild_id":   guild_id,
                "scopes":     scopes,
                "rate_limit": rate_limit,
                "expires_at": expires_at,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }).execute()

            self.after(0, self._on_generate_success,
                       raw_key, label, guild_id, scopes, rate_limit, expires_days)
        except Exception as e:
            self.after(0, self._on_generate_error, str(e))

    def _on_generate_success(self, raw_key, label, guild_id, scopes,
                             rate_limit, expires_days):
        self._generated_key = raw_key

        # Key anzeigen
        self.key_text.configure(state="normal")
        self.key_text.delete("1.0", tk.END)
        self.key_text.insert("1.0", raw_key)
        self.key_text.configure(state="disabled")

        # Result-Karte einblenden
        self.result_card.pack(fill="x", padx=28, pady=(0, 14), before=self.log_text.master)

        # Log + Status
        scope_info = scopes
        guild_info = guild_id or "ALLE"
        exp_info   = f"{expires_days}d" if expires_days > 0 else "nie"
        self._log(f"✅ Key erstellt: '{label}' | Server: {guild_info} | "
                  f"Scopes: {scope_info} | Rate: {rate_limit}/min | Ablauf: {exp_info}",
                  GREEN)
        self._set_status("✅ API-Key erfolgreich erstellt", GREEN)

        # UI zurücksetzen
        self.gen_btn.configure(state="normal", text="✨  API-Key generieren")

    def _on_generate_error(self, msg: str):
        self.gen_btn.configure(state="normal", text="✨  API-Key generieren")
        self._set_status(f"❌ Fehler: {msg}", RED)
        self._log(f"❌ Fehler beim Erstellen: {msg}", RED)
        messagebox.showerror("Fehler", f"API-Key konnte nicht erstellt werden:\n\n{msg}")

    # ── Key-Aktionen ──────────────────────────────────────────────────────────

    def _copy_key(self):
        if not self._generated_key:
            return
        self.clipboard_clear()
        self.clipboard_append(self._generated_key)
        self._set_status("📋 Key in Zwischenablage kopiert", GREEN)
        self._log("📋 Key kopiert", GREEN)

    def _save_key(self):
        if not self._generated_key:
            return
        from tkinter import filedialog
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Textdateien", "*.txt"), ("Alle Dateien", "*.*")],
            initialfile=f"api_key_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            title="API-Key speichern",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# API-Key für Insel Bot\n")
                f.write(f"# Erstellt: {datetime.now(timezone.utc).isoformat()}\n")
                f.write(f"# Label: {self.label_var.get()}\n")
                f.write(f"\n{self._generated_key}\n")
            try:
                os.chmod(path, 0o600)
            except Exception:
                pass
            self._set_status(f"💾 Key gespeichert: {os.path.basename(path)}", GREEN)
            self._log(f"💾 Key gespeichert: {path}", GREEN)
            messagebox.showinfo("Gespeichert", f"API-Key gespeichert:\n{path}")
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte Datei nicht schreiben:\n{e}")

    def _hide_result(self):
        self.result_card.pack_forget()
        self._generated_key = None
        self._set_status("🔒 Key ausgeblendet", TEXT3)
        self._log("🔒 Key ausgeblendet", TEXT3)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = ApiKeyGeneratorApp()
    app.mainloop()