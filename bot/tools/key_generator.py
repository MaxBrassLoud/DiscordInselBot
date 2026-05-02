"""
tools/key_generator.py
=======================
Tkinter-Tool zum Erstellen von RSA-Schlüsselpaaren für das Abstimmungssystem.

Verwendung:
    python tools/key_generator.py

Abhängigkeiten:
    pip install cryptography
"""

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import threading
import os

try:
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import serialization, hashes
    CRYPTO_OK = True
except ImportError:
    CRYPTO_OK = False


# ══════════════════════════════════════════════════════════════════════════════
# FARBEN & STYLE
# ══════════════════════════════════════════════════════════════════════════════

BG       = "#060708"
SURFACE  = "#0f1117"
CARD     = "#161b24"
CARD2    = "#1c2130"
BORDER   = "#1f2535"
BORDER2  = "#2a3245"
TEXT     = "#e2e8f5"
SUB      = "#7a8499"
DIM      = "#3d4660"
ACCENT   = "#5b8cff"
GREEN    = "#4ade80"
RED      = "#f87171"
GOLD     = "#fbbf24"


class KeyGeneratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("🔐 RSA Key Generator – Abstimmungssystem")
        self.geometry("780x720")
        self.resizable(True, True)
        self.configure(bg=BG)
        self.minsize(640, 560)

        self._private_key_pem: str | None = None
        self._public_key_pem:  str | None = None

        self._build_ui()

        if not CRYPTO_OK:
            messagebox.showerror(
                "Abhängigkeit fehlt",
                "Das Paket 'cryptography' ist nicht installiert.\n\n"
                "Bitte ausführen:\n    pip install cryptography",
            )

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── Title bar ────────────────────────────────────────────────────────
        title_frame = tk.Frame(self, bg=BG, pady=28)
        title_frame.pack(fill="x", padx=32)

        tk.Label(
            title_frame, text="🔐 RSA Key Generator",
            font=("Segoe UI", 20, "bold"),
            bg=BG, fg=TEXT,
        ).pack(side="left")

        tk.Label(
            title_frame, text="Abstimmungssystem",
            font=("Segoe UI", 10),
            bg=BG, fg=SUB,
        ).pack(side="left", padx=(12, 0), pady=4)

        # ── Settings card ─────────────────────────────────────────────────────
        settings = self._card(self, padx=32, pady=0)
        settings.pack(fill="x", padx=32, pady=(0, 16))

        tk.Label(settings, text="EINSTELLUNGEN", font=("Segoe UI", 8, "bold"),
                 bg=CARD, fg=DIM).grid(row=0, column=0, columnspan=4, sticky="w", pady=(20, 12))

        # Key size
        tk.Label(settings, text="Schlüsselgröße:", font=("Segoe UI", 10),
                 bg=CARD, fg=SUB).grid(row=1, column=0, sticky="w", padx=(0, 16))

        self.key_size_var = tk.IntVar(value=4096)
        for val, lbl in [(2048, "2048 bit"), (4096, "4096 bit (empfohlen)")]:
            rb = tk.Radiobutton(
                settings, text=lbl,
                variable=self.key_size_var, value=val,
                font=("Segoe UI", 10), bg=CARD, fg=TEXT,
                selectcolor=CARD2, activebackground=CARD, activeforeground=TEXT,
                highlightthickness=0, bd=0,
            )
            rb.grid(row=1, column=1 if val == 2048 else 2, sticky="w", padx=8)

        # Passwort
        tk.Label(settings, text="Passwort (optional):", font=("Segoe UI", 10),
                 bg=CARD, fg=SUB).grid(row=2, column=0, sticky="w", pady=(14, 0))

        self.password_var = tk.StringVar()
        pw_entry = tk.Entry(
            settings, textvariable=self.password_var, show="•",
            font=("Segoe UI", 10), bg=CARD2, fg=TEXT,
            insertbackground=TEXT, relief="flat",
            highlightthickness=1, highlightbackground=BORDER2,
            highlightcolor=ACCENT, width=28,
        )
        pw_entry.grid(row=2, column=1, columnspan=2, sticky="w", padx=8, pady=(14, 0))

        tk.Label(
            settings,
            text="(schützt den privaten Schlüssel beim Export)",
            font=("Segoe UI", 8), bg=CARD, fg=DIM,
        ).grid(row=2, column=3, sticky="w", padx=4, pady=(14, 0))

        settings.rowconfigure(3, minsize=20)

        # ── Generate button ────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=BG)
        btn_frame.pack(pady=(0, 20))

        self.gen_btn = self._btn(
            btn_frame, "✨  Schlüsselpaar generieren",
            command=self._generate_threaded,
            bg=ACCENT, fg="#000",
        )
        self.gen_btn.pack()

        # Progress
        self.progress_label = tk.Label(
            self, text="", font=("Segoe UI", 9), bg=BG, fg=SUB
        )
        self.progress_label.pack()

        # ── Key display ───────────────────────────────────────────────────────
        keys_frame = tk.Frame(self, bg=BG)
        keys_frame.pack(fill="both", expand=True, padx=32, pady=(8, 0))
        keys_frame.columnconfigure(0, weight=1)
        keys_frame.columnconfigure(1, weight=1)

        # Public key
        pub_card = self._card(keys_frame)
        pub_card.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        pub_card.rowconfigure(1, weight=1)
        pub_card.columnconfigure(0, weight=1)

        pub_header = tk.Frame(pub_card, bg=CARD)
        pub_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        tk.Label(pub_header, text="🔓  PUBLIC KEY", font=("Segoe UI", 9, "bold"),
                 bg=CARD, fg=GREEN).pack(side="left")
        tk.Label(
            pub_header,
            text="→ wird weitergegeben",
            font=("Segoe UI", 8), bg=CARD, fg=DIM,
        ).pack(side="left", padx=8)

        self.pub_text = self._text_area(pub_card)
        self.pub_text.grid(row=1, column=0, sticky="nsew")

        pub_btn_row = tk.Frame(pub_card, bg=CARD)
        pub_btn_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._btn(pub_btn_row, "📋  Kopieren", command=self._copy_public).pack(side="left", padx=(0, 6))
        self._btn(pub_btn_row, "💾  Speichern", command=self._save_public).pack(side="left")

        # Private key
        prv_card = self._card(keys_frame)
        prv_card.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        prv_card.rowconfigure(1, weight=1)
        prv_card.columnconfigure(0, weight=1)

        prv_header = tk.Frame(prv_card, bg=CARD)
        prv_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        tk.Label(prv_header, text="🔐  PRIVATE KEY", font=("Segoe UI", 9, "bold"),
                 bg=CARD, fg=GOLD).pack(side="left")
        tk.Label(
            prv_header,
            text="→ geheim halten!",
            font=("Segoe UI", 8), bg=CARD, fg=DIM,
        ).pack(side="left", padx=8)

        self.prv_text = self._text_area(prv_card)
        self.prv_text.grid(row=1, column=0, sticky="nsew")

        prv_btn_row = tk.Frame(prv_card, bg=CARD)
        prv_btn_row.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._btn(prv_btn_row, "📋  Kopieren", command=self._copy_private).pack(side="left", padx=(0, 6))
        self._btn(prv_btn_row, "💾  Speichern", command=self._save_private).pack(side="left")

        # ── Footer ─────────────────────────────────────────────────────────────
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=32, pady=16)

        tk.Label(
            footer,
            text=(
                "⚠️  Den privaten Schlüssel sicher aufbewahren – er kann nicht wiederhergestellt werden.\n"
                "Der öffentliche Schlüssel wird in der JSON-Datei hinterlegt (Feld 'RSA_Public_Key')."
            ),
            font=("Segoe UI", 8), bg=BG, fg=DIM,
            justify="left",
        ).pack(side="left")

    # ── Widget helpers ─────────────────────────────────────────────────────────

    def _card(self, parent, padx=16, pady=16):
        frame = tk.Frame(parent, bg=CARD, padx=padx, pady=pady,
                         highlightthickness=1, highlightbackground=BORDER)
        return frame

    def _btn(self, parent, text, command, bg=CARD2, fg=TEXT):
        btn = tk.Button(
            parent, text=text, command=command,
            font=("Segoe UI", 9, "bold"),
            bg=bg, fg=fg, activebackground=BORDER2, activeforeground=TEXT,
            relief="flat", bd=0, padx=14, pady=7, cursor="hand2",
        )
        btn.bind("<Enter>", lambda e: btn.configure(bg=self._lighten(bg)))
        btn.bind("<Leave>", lambda e: btn.configure(bg=bg))
        return btn

    def _text_area(self, parent):
        txt = tk.Text(
            parent,
            font=("Courier New", 7),
            bg=CARD2, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0,
            highlightthickness=1, highlightbackground=BORDER2,
            wrap="word", state="disabled",
            height=12,
        )
        sb = tk.Scrollbar(parent, command=txt.yview, bg=BORDER2, troughcolor=CARD2,
                          bd=0, relief="flat", width=6)
        txt.configure(yscrollcommand=sb.set)
        return txt

    @staticmethod
    def _lighten(hex_color: str) -> str:
        """Slightly lightens a hex color for hover."""
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            r = min(255, r + 25)
            g = min(255, g + 25)
            b = min(255, b + 25)
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return hex_color

    # ── Key generation ─────────────────────────────────────────────────────────

    def _set_text(self, widget: tk.Text, content: str):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", content)
        widget.configure(state="disabled")

    def _generate_threaded(self):
        if not CRYPTO_OK:
            messagebox.showerror("Fehler", "Bitte 'cryptography' installieren:\n  pip install cryptography")
            return
        self.gen_btn.configure(state="disabled", text="⏳  Generiert...")
        self.progress_label.configure(text="Schlüsselpaar wird erstellt…", fg=SUB)
        threading.Thread(target=self._do_generate, daemon=True).start()

    def _do_generate(self):
        try:
            key_size = self.key_size_var.get()
            password = self.password_var.get().strip()

            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=key_size,
            )

            # Serialize private key
            if password:
                enc = serialization.BestAvailableEncryption(password.encode())
            else:
                enc = serialization.NoEncryption()

            prv_pem = private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=enc,
            ).decode("utf-8")

            pub_pem = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("utf-8")

            self._private_key_pem = prv_pem
            self._public_key_pem  = pub_pem

            self.after(0, self._update_ui_after_gen, pub_pem, prv_pem, key_size, bool(password))
        except Exception as e:
            self.after(0, self._gen_error, str(e))

    def _update_ui_after_gen(self, pub_pem, prv_pem, key_size, has_pw):
        self._set_text(self.pub_text, pub_pem)
        self._set_text(self.prv_text, prv_pem)
        self.gen_btn.configure(state="normal", text="✨  Schlüsselpaar generieren")
        self.progress_label.configure(
            text=f"✅  {key_size}-bit RSA Schlüsselpaar erstellt{'  (passwortgeschützt)' if has_pw else ''}",
            fg=GREEN,
        )

    def _gen_error(self, msg):
        self.gen_btn.configure(state="normal", text="✨  Schlüsselpaar generieren")
        self.progress_label.configure(text=f"❌  Fehler: {msg}", fg=RED)

    # ── Copy / Save ───────────────────────────────────────────────────────────

    def _copy_public(self):
        if not self._public_key_pem:
            messagebox.showwarning("Kein Schlüssel", "Bitte zuerst einen Schlüssel generieren.")
            return
        self.clipboard_clear()
        self.clipboard_append(self._public_key_pem)
        self.progress_label.configure(text="📋  Public Key in Zwischenablage kopiert", fg=GREEN)

    def _copy_private(self):
        if not self._private_key_pem:
            messagebox.showwarning("Kein Schlüssel", "Bitte zuerst einen Schlüssel generieren.")
            return
        if not messagebox.askyesno(
            "⚠️ Sicherheitswarnung",
            "Der private Schlüssel wird in die Zwischenablage kopiert.\n\n"
            "Stelle sicher, dass du ihn nicht versehentlich teilst.\n\n"
            "Fortfahren?",
        ):
            return
        self.clipboard_clear()
        self.clipboard_append(self._private_key_pem)
        self.progress_label.configure(text="📋  Private Key in Zwischenablage kopiert", fg=GOLD)

    def _save_public(self):
        if not self._public_key_pem:
            messagebox.showwarning("Kein Schlüssel", "Bitte zuerst einen Schlüssel generieren.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pem",
            filetypes=[("PEM Dateien", "*.pem"), ("Alle Dateien", "*.*")],
            initialfile="public_key.pem",
            title="Public Key speichern",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._public_key_pem)
        messagebox.showinfo("Gespeichert", f"Public Key gespeichert:\n{path}")
        self.progress_label.configure(text=f"💾  Public Key gespeichert → {os.path.basename(path)}", fg=GREEN)

    def _save_private(self):
        if not self._private_key_pem:
            messagebox.showwarning("Kein Schlüssel", "Bitte zuerst einen Schlüssel generieren.")
            return
        if not messagebox.askyesno(
            "⚠️ Sicherheitswarnung",
            "Du bist dabei, den PRIVATEN Schlüssel zu speichern.\n\n"
            "• Speichere ihn an einem sicheren Ort\n"
            "• Teile ihn NIEMALS mit anderen\n"
            "• Ohne diesen Schlüssel sind die Abstimmungsdaten nicht entschlüsselbar\n\n"
            "Fortfahren?",
        ):
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".pem",
            filetypes=[("PEM Dateien", "*.pem"), ("Alle Dateien", "*.*")],
            initialfile="private_key.pem",
            title="Private Key speichern (GEHEIM!)",
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._private_key_pem)
        # Dateiberechtigungen einschränken (Unix)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        messagebox.showinfo(
            "Gespeichert",
            f"Private Key gespeichert:\n{path}\n\n"
            "⚠️ Bewahre diese Datei sicher auf!"
        )
        self.progress_label.configure(
            text=f"💾  Private Key gespeichert → {os.path.basename(path)}", fg=GOLD
        )


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    app = KeyGeneratorApp()
    app.mainloop()