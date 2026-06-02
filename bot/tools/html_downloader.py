#!/usr/bin/env python3
"""
Discord HTML-Datei-Downloader
Lädt alle HTML-Dateianhänge aus einem bestimmten Discord-Kanal herunter.
Mit audioop-Mock für Python 3.13+ Kompatibilität.
"""

import os
import asyncio
import sys
import types
from pathlib import Path
from typing import Set
import aiohttp

# ---------- audioop-Mock für Python 3.13+ (da audioop in Python 3.13 entfernt wurde) ----------
if 'audioop' not in sys.modules:
    audioop = types.ModuleType('audioop')
    # Dummy-Funktionen für alle von discord.py verwendeten audioop-Funktionen
    def dummy_bytes(*args, **kwargs):
        return b''
    def dummy_int(*args, **kwargs):
        return 0
    def dummy_tuple(*args, **kwargs):
        return (b'', (0, 0))

    for name in ['add', 'alaw2lin', 'bias', 'cross', 'findfactor', 'findfit', 'findmax',
                 'getsample', 'lin2alaw', 'lin2ulaw', 'mul', 'reverse', 'tomono', 'tostereo', 'ulaw2lin']:
        setattr(audioop, name, dummy_bytes)
    for name in ['avg', 'max', 'maxpp', 'minmax', 'rms']:
        setattr(audioop, name, dummy_int)
    # Spezialfall für adpcm-Funktionen, die ein Tupel zurückgeben
    setattr(audioop, 'adpcm2lin', dummy_tuple)
    setattr(audioop, 'lin2adpcm', dummy_tuple)
    setattr(audioop, 'ratecv', dummy_tuple)

    sys.modules['audioop'] = audioop
# ---------------------------------------------------------------------------------------------

import discord
from discord.ext import commands

# ========== KONFIGURATION ==========
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", 0))  # Kanal-ID als Integer
OUTPUT_DIR = Path("downloaded_html")  # Zielordner für die HTML-Dateien

HTML_EXTENSIONS = {".html", ".htm"}  # als HTML betrachtete Dateiendungen
# ===================================

intents = discord.Intents.default()
intents.message_content = True  # Notwendig, um Nachrichtenanhänge zu lesen
bot = commands.Bot(command_prefix="!", intents=intents)


def is_html_attachment(attachment: discord.Attachment) -> bool:
    """Prüft, ob ein Dateianhang eine HTML-Datei ist (anhand der Endung)."""
    filename = attachment.filename.lower()
    return any(filename.endswith(ext) for ext in HTML_EXTENSIONS)


async def download_file(session: aiohttp.ClientSession, url: str, filepath: Path) -> bool:
    """Lädt eine Datei herunter und speichert sie unter `filepath`."""
    try:
        async with session.get(url) as response:
            if response.status == 200:
                filepath.parent.mkdir(parents=True, exist_ok=True)
                with open(filepath, "wb") as f:
                    f.write(await response.read())
                return True
            else:
                print(f"  Fehler beim Herunterladen {url}: HTTP {response.status}")
                return False
    except Exception as e:
        print(f"  Ausnahme beim Herunterladen {url}: {e}")
        return False


@bot.event
async def on_ready():
    print(f"✅ Bot eingeloggt als {bot.user}")
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        print(f"❌ Kanal mit ID {CHANNEL_ID} nicht gefunden. Stelle sicher, dass der Bot Zugriff hat.")
        await bot.close()
        return

    print(f"📁 Durchsuche Kanal: {channel.name} (ID: {channel.id})")
    print(f"📂 HTML-Dateien werden gespeichert in: {OUTPUT_DIR.absolute()}")

    downloaded_filenames: Set[str] = set()
    total_attachments = 0
    downloaded_count = 0

    async with aiohttp.ClientSession() as session:
        async for message in channel.history(limit=None, oldest_first=True):
            if not message.attachments:
                continue

            for attachment in message.attachments:
                total_attachments += 1
                if not is_html_attachment(attachment):
                    continue

                base_filename = attachment.filename
                file_stem, file_ext = os.path.splitext(base_filename)
                safe_stem = "".join(c if c.isalnum() or c in "._- " else "_" for c in file_stem)
                safe_filename = f"{safe_stem}{file_ext}"

                if safe_filename in downloaded_filenames:
                    safe_filename = f"{message.id}_{safe_filename}"
                downloaded_filenames.add(safe_filename)

                save_path = OUTPUT_DIR / safe_filename

                print(f"⬇️  Lade herunter: {attachment.filename} (von Nachricht {message.id})")
                success = await download_file(session, attachment.url, save_path)
                if success:
                    downloaded_count += 1
                    print(f"   ✅ Gespeichert als {save_path}")
                else:
                    print(f"   ❌ Herunterladen fehlgeschlagen")

    print(f"\n📊 Zusammenfassung: {downloaded_count} von {total_attachments} HTML-Dateien heruntergeladen.")
    await bot.close()


def main():
    if not BOT_TOKEN or BOT_TOKEN == "DEIN_BOT_TOKEN_HIER":
        print("❌ Fehler: Kein gültiger Bot-Token angegeben.")
        print("Setze die Umgebungsvariable DISCORD_BOT_TOKEN oder trage den Token direkt im Skript ein.")
        return
    if CHANNEL_ID == 0:
        print("❌ Fehler: Keine gültige Kanal-ID angegeben.")
        print("Setze die Umgebungsvariable DISCORD_CHANNEL_ID oder trage die ID direkt im Skript ein.")
        return

    try:
        asyncio.run(bot.start(BOT_TOKEN))
    except KeyboardInterrupt:
        print("\n⚠️  Abbruch durch Benutzer.")
    except discord.LoginFailure:
        print("❌ Fehler: Ungültiger Bot-Token. Überprüfe den Token.")
    except Exception as e:
        print(f"❌ Ein unerwarteter Fehler ist aufgetreten: {e}")


if __name__ == "__main__":
    main()