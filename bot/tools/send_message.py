import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import requests
import threading

class DiscordEmbedSender:
    def __init__(self, root):
        self.root = root
        self.root.title("Discord Embed Sender – Kanal als ID oder Dropdown")
        self.root.geometry("600x720")
        self.root.resizable(False, False)

        # Variablen
        self.token_var = tk.StringVar()
        self.guild_var = tk.StringVar()
        self.channel_var = tk.StringVar()
        self.role_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.color_var = tk.StringVar(value="#00ff00")
        self.extra_text_var = tk.StringVar()

        # Daten für Dropdowns
        self.guilds = {}
        self.channels = {}        # {channel_name: channel_id}
        self.roles = {}
        self.role_var.set("Keine Rolle")

        self.create_widgets()

    def create_widgets(self):
        # Token
        tk.Label(self.root, text="Bot Token:").pack(pady=(10,0), anchor="w", padx=10)
        token_entry = tk.Entry(self.root, textvariable=self.token_var, width=70, show="*")
        token_entry.pack(pady=5, padx=10, fill="x")

        # Server laden Button
        self.load_guilds_btn = tk.Button(self.root, text="Server laden", command=self.load_guilds_thread)
        self.load_guilds_btn.pack(pady=5)

        # Server Dropdown
        tk.Label(self.root, text="Server (Guild):").pack(anchor="w", padx=10)
        self.guild_combo = ttk.Combobox(self.root, textvariable=self.guild_var, state="readonly", width=60)
        self.guild_combo.pack(pady=5, padx=10, fill="x")
        self.guild_combo.bind("<<ComboboxSelected>>", self.on_guild_select)

        # Kanal – jetzt als Eingabe + Dropdown (state="normal")
        tk.Label(self.root, text="Textkanal (Name aus Liste ODER ID eingeben):").pack(anchor="w", padx=10)
        self.channel_combo = ttk.Combobox(self.root, textvariable=self.channel_var, state="normal", width=60)
        self.channel_combo.pack(pady=5, padx=10, fill="x")
        tk.Label(self.root, text="Tipp: Nach Laden der Kanäle erscheinen hier die Namen; Sie können auch direkt eine Kanal-ID eintippen.", font=("Arial", 8), fg="gray").pack(anchor="w", padx=10)

        # Rollen Dropdown (weiterhin nur Auswahl)
        tk.Label(self.root, text="Rolle erwähnen (optional):").pack(anchor="w", padx=10)
        self.role_combo = ttk.Combobox(self.root, textvariable=self.role_var, state="readonly", width=60)
        self.role_combo.pack(pady=5, padx=10, fill="x")

        # Text nach der Rolle
        tk.Label(self.root, text="Text nach der Rolle (optional, erscheint direkt dahinter):").pack(anchor="w", padx=10)
        self.extra_text_entry = tk.Entry(self.root, textvariable=self.extra_text_var, width=70)
        self.extra_text_entry.pack(pady=5, padx=10, fill="x")
        tk.Label(self.root, text="(z. B. 'Hallo zusammen!' – wird unmittelbar nach der Rollenerwähnung eingefügt)", font=("Arial", 8), fg="gray").pack(anchor="w", padx=10)

        # Embed Überschrift
        tk.Label(self.root, text="Embed Titel:").pack(anchor="w", padx=10)
        title_entry = tk.Entry(self.root, textvariable=self.title_var, width=70)
        title_entry.pack(pady=5, padx=10, fill="x")

        # Embed Inhalt
        tk.Label(self.root, text="Embed Inhalt (Beschreibung):").pack(anchor="w", padx=10)
        self.desc_text = scrolledtext.ScrolledText(self.root, height=8, width=70)
        self.desc_text.pack(pady=5, padx=10, fill="x")

        # Embed Farbe
        tk.Label(self.root, text="Farbe (Hex, z.B. #ff0000):").pack(anchor="w", padx=10)
        color_frame = tk.Frame(self.root)
        color_frame.pack(pady=5, padx=10, fill="x")
        color_entry = tk.Entry(color_frame, textvariable=self.color_var, width=10)
        color_entry.pack(side="left")
        tk.Label(color_frame, text="  Beispiel: #00ff00 für Grün").pack(side="left", padx=10)

        # Senden Button
        self.send_btn = tk.Button(self.root, text="Embed senden", command=self.send_embed_thread, bg="lightgreen")
        self.send_btn.pack(pady=15)

        # Status Label
        self.status_label = tk.Label(self.root, text="Bereit", fg="blue")
        self.status_label.pack(pady=5)

        # Initialisierung
        self.guild_combo["values"] = []
        self.channel_combo["values"] = []
        self.role_combo["values"] = ["Keine Rolle"]

    def load_guilds_thread(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Fehler", "Bitte geben Sie den Bot-Token ein.")
            return
        self.load_guilds_btn.config(state="disabled", text="Lade Server...")
        self.status_label.config(text="Lade Server...", fg="orange")
        threading.Thread(target=self._load_guilds, args=(token,), daemon=True).start()

    def _load_guilds(self, token):
        url = "https://discord.com/api/v10/users/@me/guilds"
        headers = {"Authorization": f"Bot {token}"}
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                guild_list = resp.json()
                self.guilds = {g["name"]: g["id"] for g in guild_list}
                self.root.after(0, self._update_guild_dropdown)
                self.root.after(0, self.status_label.config, {"text": "Server geladen. Wähle einen Server aus.", "fg": "green"})
            else:
                error_text = f"Fehler {resp.status_code}: {resp.text}"
                self.root.after(0, messagebox.showerror, "Server laden fehlgeschlagen", error_text)
                self.root.after(0, self.status_label.config, {"text": "Fehler beim Laden", "fg": "red"})
        except Exception as e:
            self.root.after(0, messagebox.showerror, "Fehler", str(e))
            self.root.after(0, self.status_label.config, {"text": "Verbindungsfehler", "fg": "red"})
        finally:
            self.root.after(0, lambda: self.load_guilds_btn.config(state="normal", text="Server laden"))

    def _update_guild_dropdown(self):
        guild_names = list(self.guilds.keys())
        self.guild_combo["values"] = guild_names
        if guild_names:
            self.guild_combo.set(guild_names[0])
            self.on_guild_select()

    def on_guild_select(self, event=None):
        guild_name = self.guild_var.get()
        if not guild_name or guild_name not in self.guilds:
            return
        guild_id = self.guilds[guild_name]
        token = self.token_var.get().strip()
        if not token:
            return
        self.status_label.config(text="Lade Kanäle und Rollen...", fg="orange")
        self.channel_combo["values"] = ["Lade..."]
        self.role_combo["values"] = ["Lade..."]
        threading.Thread(target=self._load_channels, args=(token, guild_id), daemon=True).start()
        threading.Thread(target=self._load_roles, args=(token, guild_id), daemon=True).start()

    def _load_channels(self, token, guild_id):
        url = f"https://discord.com/api/v10/guilds/{guild_id}/channels"
        headers = {"Authorization": f"Bot {token}"}
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                channels = resp.json()
                self.channels = {c["name"]: c["id"] for c in channels if c["type"] == 0}
                self.root.after(0, self._update_channel_dropdown)
            else:
                self.root.after(0, messagebox.showerror, "Kanäle laden fehlgeschlagen", f"Status {resp.status_code}")
        except Exception as e:
            self.root.after(0, messagebox.showerror, "Fehler", str(e))

    def _update_channel_dropdown(self):
        channel_names = list(self.channels.keys())
        self.channel_combo["values"] = channel_names
        if channel_names:
            # Nicht automatisch setzen, weil der Benutzer evtl. eine ID eingetragen hat – lassen wir das Feld unverändert
            # Falls das Feld leer ist, können wir den ersten Kanal vorschlagen:
            if not self.channel_var.get():
                self.channel_combo.set(channel_names[0])
        else:
            self.channel_combo.set("Keine Textkanäle gefunden")

    def _load_roles(self, token, guild_id):
        url = f"https://discord.com/api/v10/guilds/{guild_id}/roles"
        headers = {"Authorization": f"Bot {token}"}
        try:
            resp = requests.get(url, headers=headers)
            if resp.status_code == 200:
                roles = resp.json()
                self.roles = {r["name"]: r["id"] for r in roles if r["name"] != "@everyone"}
                self.root.after(0, self._update_role_dropdown)
            else:
                self.root.after(0, messagebox.showerror, "Rollen laden fehlgeschlagen", f"Status {resp.status_code}")
        except Exception as e:
            self.root.after(0, messagebox.showerror, "Fehler", str(e))

    def _update_role_dropdown(self):
        role_names = list(self.roles.keys())
        role_names.sort()
        role_names.insert(0, "Keine Rolle")
        self.role_combo["values"] = role_names
        self.role_var.set("Keine Rolle")
        self.status_label.config(text="Kanäle und Rollen geladen", fg="green")

    def send_embed_thread(self):
        token = self.token_var.get().strip()
        if not token:
            messagebox.showerror("Fehler", "Bot Token fehlt.")
            return

        # Kanal-Eingabe auswerten: entweder Name aus der Liste oder direkte ID
        channel_input = self.channel_var.get().strip()
        if not channel_input:
            messagebox.showerror("Fehler", "Bitte geben Sie einen Kanalnamen oder eine Kanal-ID ein.")
            return

        # Versuche, die Kanal-ID zu bestimmen
        channel_id = None
        # 1) Falls die Eingabe exakt einem Namen aus dem geladenen channels-Dictionary entspricht
        if channel_input in self.channels:
            channel_id = self.channels[channel_input]
        # 2) Sonst prüfen, ob die Eingabe eine numerische ID ist (snowflake)
        elif channel_input.isdigit():
            channel_id = channel_input
        else:
            # 3) Kein Name und keine Zahl -> Fehler
            messagebox.showerror("Fehler", f"'{channel_input}' ist kein gültiger Kanalname (aus der Liste) und keine numerische ID.")
            return

        title = self.title_var.get().strip()
        description = self.desc_text.get("1.0", tk.END).strip()
        if not title and not description:
            messagebox.showerror("Fehler", "Bitte mindestens Titel oder Inhalt des Embeds angeben.")
            return

        self.send_btn.config(state="disabled", text="Senden...")
        self.status_label.config(text="Sende Nachricht...", fg="orange")
        threading.Thread(target=self._send_embed, args=(token, channel_id), daemon=True).start()

    def _send_embed(self, token, channel_id):
        # Rolle + Text zusammenbauen
        role_mention = ""
        selected_role = self.role_var.get()
        if selected_role != "Keine Rolle" and selected_role in self.roles:
            role_id = self.roles[selected_role]
            role_mention = f"<@&{role_id}>"
        extra_text = self.extra_text_var.get()
        content = role_mention + extra_text

        # Embed
        embed = {}
        title = self.title_var.get().strip()
        if title:
            embed["title"] = title
        description = self.desc_text.get("1.0", tk.END).strip()
        if description:
            embed["description"] = description

        color_hex = self.color_var.get().strip().lstrip('#')
        try:
            color_int = int(color_hex, 16)
        except ValueError:
            color_int = 0x00ff00
        embed["color"] = color_int

        payload = {
            "content": content,
            "embeds": [embed]
        }
        if not title and not description:
            payload.pop("embeds")

        url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
        headers = {
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json"
        }

        try:
            resp = requests.post(url, headers=headers, json=payload)
            if resp.status_code in (200, 201):
                self.root.after(0, self._send_success)
            else:
                error_msg = f"Fehler {resp.status_code}: {resp.text}"
                self.root.after(0, self._send_error, error_msg)
        except Exception as e:
            self.root.after(0, self._send_error, str(e))

    def _send_success(self):
        self.status_label.config(text="✅ Embed erfolgreich gesendet!", fg="green")
        messagebox.showinfo("Erfolg", "Die Nachricht wurde versendet.")
        self.send_btn.config(state="normal", text="Embed senden")

    def _send_error(self, error_msg):
        self.status_label.config(text="❌ Fehler beim Senden", fg="red")
        messagebox.showerror("Fehler beim Senden", error_msg)
        self.send_btn.config(state="normal", text="Embed senden")

if __name__ == "__main__":
    root = tk.Tk()
    app = DiscordEmbedSender(root)
    root.mainloop()