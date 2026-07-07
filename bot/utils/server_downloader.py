import tkinter as tk
from tkinter import scrolledtext, messagebox
import requests
import re
import os

class DiscordIconDownloader:
    def __init__(self, root):
        self.root = root
        self.root.title("Discord Server-Icon Downloader")
        self.root.geometry("500x400")
        self.root.resizable(False, False)

        # Eingabefelder
        tk.Label(root, text="Bot Token:").pack(pady=(10,0))
        self.token_entry = tk.Entry(root, width=50, show="*")
        self.token_entry.pack(pady=(0,10))

        tk.Label(root, text="Server-ID:").pack()
        self.guild_entry = tk.Entry(root, width=50)
        self.guild_entry.pack(pady=(0,10))

        # Button
        self.download_btn = tk.Button(root, text="Icon herunterladen", command=self.download_icon)
        self.download_btn.pack(pady=5)

        # Log-Ausgabe
        self.log_text = scrolledtext.ScrolledText(root, height=15, width=60, state='normal')
        self.log_text.pack(pady=10, padx=10)
        self.log_text.insert(tk.END, "Bereit zum Download...\n")

    def log(self, message):
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.root.update()

    def download_icon(self):
        token = self.token_entry.get().strip()
        guild_id = self.guild_entry.get().strip()

        if not token or not guild_id:
            messagebox.showerror("Fehler", "Bitte Token und Server-ID angeben.")
            return

        self.download_btn.config(state='disabled')
        self.log("Starte Download...")

        try:
            # API-Anfrage mit Bot-Token
            url = f"https://discord.com/api/v10/guilds/{guild_id}"
            headers = {"Authorization": f"Bot {token}"}
            response = requests.get(url, headers=headers)

            if response.status_code != 200:
                self.log(f"Fehler {response.status_code}: {response.text}")
                messagebox.showerror("API-Fehler", f"Status {response.status_code}\n{response.text[:200]}")
                return

            data = response.json()
            icon_hash = data.get('icon')
            if not icon_hash:
                self.log("Dieser Server hat kein Icon gesetzt.")
                messagebox.showinfo("Info", "Server hat kein Icon.")
                return

            server_name = data.get('name', guild_id)

            # Icon-URL bestimmen
            if icon_hash.startswith('a_'):
                icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.gif"
                ext = 'gif'
            else:
                icon_url = f"https://cdn.discordapp.com/icons/{guild_id}/{icon_hash}.png"
                ext = 'png'

            self.log(f"Lade herunter: {icon_url}")

            # Bild herunterladen
            img_response = requests.get(icon_url, stream=True)
            if img_response.status_code != 200:
                self.log(f"Fehler beim Bilddownload: {img_response.status_code}")
                return

            # Dateinamen erstellen (Sonderzeichen entfernen)
            safe_name = re.sub(r'[^a-zA-Z0-9 _.-]', '', f"{server_name}_{guild_id}")
            filename = f"discord_icon_{safe_name}.{ext}"

            with open(filename, 'wb') as f:
                for chunk in img_response.iter_content(1024):
                    f.write(chunk)

            self.log(f"✅ Erfolg! Icon gespeichert als: {filename}")
            messagebox.showinfo("Erfolg", f"Icon gespeichert unter:\n{os.path.abspath(filename)}")

        except Exception as e:
            self.log(f"❌ Fehler: {str(e)}")
            messagebox.showerror("Fehler", str(e))
        finally:
            self.download_btn.config(state='normal')

if __name__ == "__main__":
    root = tk.Tk()
    app = DiscordIconDownloader(root)
    root.mainloop()