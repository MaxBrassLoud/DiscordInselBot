"""
bot/tools/import_tickettool.py
===============================
CLI-Tool zum Importieren von TicketTool (tickettool.xyz) Transcripts
in das Insel Bot Datenbanksystem.

ORDNERSTRUKTUR (Eingabe):
    imports/
    └── <SERVER_ID>/
        ├── tickets/
        │   └── <MODUL_ID_ODER_NAME>/
        │       ├── transcript-closed-0001.html
        │       └── ...
        └── applications/
            ├── transcript-closed-0001.html
            └── ...

VERWENDUNG:
    python3 bot/tools/import_tickettool.py --server-id 1253751493513969735
    python3 bot/tools/import_tickettool.py --all
    python3 bot/tools/import_tickettool.py --server-id 1253751493513969735 --dry-run
    python3 bot/tools/import_tickettool.py --server-id 1253751493513969735 --verbose

DATENBANK-MIGRATION (einmalig in Supabase ausführen):
    Siehe migrations/add_imported_flag.sql

HINWEIS:
    - Bereits importierte Dateien werden übersprungen (Dedup via external_channel_id)
    - Bot-Nachrichten (Ticket Tool selbst) werden gefiltert
    - Timestamps werden von Discord-Millisekunden in ISO-Format konvertiert
    - Bearbeitete Nachrichten werden korrekt mit edit_history gespeichert
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Projekt-Root zu sys.path hinzufügen ───────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

from bot.core.supabase_client import init_supabase, get_supabase

# ── Konstanten ────────────────────────────────────────────────────────────────
IMPORTS_DIR   = _ROOT / "imports"
IMPORT_SOURCE = "tickettool"

# Discord-User-IDs die als "Bot-Nachrichten" ignoriert werden sollen
# Ticket Tool Bot hat mehrere bekannte IDs
TICKETTOOL_BOT_IDS = {
    "557628352828014614",   # Ticket Tool#4843 (klassisch)
    "508391840525975553",   # Ticket Tool v2
    "936929561302675456",   # Ticket Tool#0000 (neuere Version)
}

# ══════════════════════════════════════════════════════════════════════════════
# TRANSCRIPT PARSER
# ══════════════════════════════════════════════════════════════════════════════

class TranscriptParseError(Exception):
    pass


def _ms_to_iso(ts_ms: int | None) -> str | None:
    """Konvertiert Discord-Millisekunden-Timestamp zu ISO-String."""
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _extract_b64_data(html_content: str) -> tuple[list, dict, dict]:
    """
    Extrahiert messages, channel und server aus dem TicketTool HTML-Transcript.
    Gibt (messages, channel_info, server_info) zurück.

    TicketTool schreibt die Variablen in dieser Reihenfolge ins Script-Tag:
        let channel = "...";let server = "...";let messages = "..."
    """
    # Reihenfolge 1: channel → server → messages (Standard TicketTool)
    script_match = re.search(
        r'let\s+channel\s*=\s*"([^"]+)"[^;]*;[^l]*'
        r'let\s+server\s*=\s*"([^"]+)"[^;]*;[^l]*'
        r'let\s+messages\s*=\s*"([^"]+)"',
        html_content,
        re.DOTALL,
    )
    if script_match:
        channel_b64  = script_match.group(1)
        server_b64   = script_match.group(2)
        messages_b64 = script_match.group(3)
    else:
        # Reihenfolge 2: messages → channel → server (ältere Versionen)
        script_match = re.search(
            r'let\s+messages\s*=\s*"([^"]+)".*?'
            r'let\s+channel\s*=\s*"([^"]+)".*?'
            r'let\s+server\s*=\s*"([^"]+)"',
            html_content,
            re.DOTALL,
        )
        if script_match:
            messages_b64 = script_match.group(1)
            channel_b64  = script_match.group(2)
            server_b64   = script_match.group(3)
        else:
            raise TranscriptParseError("Keine TicketTool-Daten im Script-Tag gefunden")

    def _decode(b64: str) -> dict | list:
        raw = base64.b64decode(b64 + "==").decode("utf-8")
        # Backtick ist kein gültiger JSON-Escape, bereinigen
        raw = raw.replace("\\`", "`")
        return json.loads(raw)

    try:
        channel_info = _decode(channel_b64)
        server_info  = _decode(server_b64)
        messages     = _decode(messages_b64)
    except Exception as e:
        raise TranscriptParseError(f"Base64/JSON Dekodierung fehlgeschlagen: {e}") from e

    if not isinstance(messages, list):
        raise TranscriptParseError("messages ist keine Liste")

    return messages, channel_info, server_info


def _parse_ticket_number(channel_name: str) -> int | None:
    """
    Extrahiert Ticket-Nummer aus Kanal-Namen.
    Beispiele: 'closed-0104' -> 104, 'ticket-0042-user' -> 42
    """
    # Alle Ziffernfolgen extrahieren, erste verwenden
    nums = re.findall(r'\d+', channel_name)
    if nums:
        return int(nums[0])
    return None


def _filter_messages(messages: list[dict]) -> list[dict]:
    """
    Filtert Bot-Nachrichten (Ticket Tool selbst) heraus.
    Beibehält nur echte User-Nachrichten.
    """
    result = []
    for msg in messages:
        user_id = str(msg.get("user_id", ""))
        is_bot  = msg.get("bot", False)
        # Ticket-Tool-Bot rausfiltern
        if is_bot and user_id in TICKETTOOL_BOT_IDS:
            continue
        # Nachrichten ohne Inhalt und ohne Anhänge überspringen (reine Embeds)
        content  = msg.get("content", "").strip()
        atts     = msg.get("attachments") or []
        if not content and not atts:
            continue
        result.append(msg)
    return result


def _build_message_rows(
    messages: list[dict],
    server_id: str,
    ticket_id: int | None = None,
    app_id: int | None = None,
    entity_type: str = "ticket",
) -> list[dict]:
    """
    Konvertiert TicketTool-Nachrichten in DB-Zeilen für ticket_messages
    oder application_messages.
    """
    rows = []
    for msg in messages:
        user_id   = str(msg.get("user_id", ""))
        username  = msg.get("nick") or msg.get("username") or "?"
        content   = msg.get("content", "")
        created_ms = msg.get("created")
        edited_ms  = msg.get("edited")
        discord_msg_id = str(msg.get("id", ""))

        timestamp = _ms_to_iso(created_ms) or datetime.now(timezone.utc).isoformat()

        # Anhänge
        attachments = []
        for att in (msg.get("attachments") or []):
            url = att.get("url", "")
            if url:
                attachments.append(url)

        # edit_history aufbauen wenn die Nachricht bearbeitet wurde
        edit_history = []
        if edited_ms and edited_ms != created_ms:
            # Wir haben nur den finalen Inhalt – kein "vorheriger" Inhalt verfügbar.
            # Wir tragen einen Platzhalter ein damit sichtbar ist, dass bearbeitet wurde.
            edit_history = [{
                "content":   "[Original-Inhalt nicht verfügbar – importiert von TicketTool]",
                "edited_at": _ms_to_iso(edited_ms) or timestamp,
            }]

        row = {
            "server_id":          server_id,
            "user_name":          username,
            "user_id":            user_id,
            "content":            content,
            "attachments":        attachments,
            "timestamp":          timestamp,
            "discord_message_id": discord_msg_id or None,
            "is_deleted":         False,
            "edit_history":       edit_history,
        }
        if entity_type == "ticket" and ticket_id is not None:
            row["ticket_id"] = ticket_id
        elif entity_type == "application" and app_id is not None:
            row["app_id"] = app_id

        rows.append(row)
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# IMPORT LOGIC
# ══════════════════════════════════════════════════════════════════════════════

class TicketToolImporter:
    def __init__(self, dry_run: bool = False, verbose: bool = False):
        self.dry_run = dry_run
        self.verbose = verbose
        self.stats   = {
            "files_processed": 0,
            "files_skipped":   0,
            "files_error":     0,
            "tickets_created": 0,
            "apps_created":    0,
            "messages_created": 0,
        }

    def _log(self, msg: str, level: str = "INFO"):
        prefix = {
            "INFO":    "ℹ️ ",
            "OK":      "✅ ",
            "SKIP":    "⏭️ ",
            "WARN":    "⚠️ ",
            "ERROR":   "❌ ",
            "DRY":     "🔍 [DRY] ",
            "VERBOSE": "   ",
        }.get(level, "   ")
        if level == "VERBOSE" and not self.verbose:
            return
        print(f"{prefix}{msg}")

    def _next_ticket_id(self, server_id: str) -> int:
        sb = get_supabase()
        r  = sb.table("tickets").select("ticket_id")\
               .eq("server_id", server_id)\
               .order("ticket_id", desc=True)\
               .limit(1).execute()
        return (r.data[0]["ticket_id"] + 1) if r.data else 1

    def _next_app_id(self, server_id: str) -> int:
        sb = get_supabase()
        r  = sb.table("applications").select("app_id")\
               .eq("server_id", server_id)\
               .order("app_id", desc=True)\
               .limit(1).execute()
        return (r.data[0]["app_id"] + 1) if r.data else 1

    def _channel_already_imported(self, server_id: str, channel_id: str, entity_type: str) -> bool:
        """Prüft ob dieser externe Kanal bereits importiert wurde."""
        sb = get_supabase()
        table = "tickets" if entity_type == "ticket" else "applications"
        r = sb.table(table).select("id" if entity_type == "application" else "ticket_id")\
              .eq("server_id", server_id)\
              .eq("external_channel_id", channel_id)\
              .limit(1).execute()
        return bool(r.data)

    def _get_module_name(self, module_folder: str, server_id: str) -> str | None:
        """
        Versucht den Modul-Namen aus der DB zu ermitteln.
        module_folder kann eine Modul-ID oder ein Name sein.
        """
        sb = get_supabase()
        # Erst als ID versuchen
        if module_folder.isdigit():
            r = sb.table("ticket_modules").select("name")\
                  .eq("id", int(module_folder))\
                  .eq("server_id", server_id).execute()
            if r.data:
                return r.data[0]["name"]
        # Als Name-Suche
        r = sb.table("ticket_modules").select("name")\
              .eq("server_id", server_id)\
              .ilike("name", f"%{module_folder}%").execute()
        if r.data:
            return r.data[0]["name"]
        # Fallback: Ordnername als Modul-Name verwenden
        return module_folder

    def import_ticket_file(
        self,
        html_path: Path,
        server_id: str,
        module_folder: str,
    ) -> bool:
        """
        Importiert eine einzelne Ticket-Transcript-Datei.
        Gibt True zurück wenn erfolgreich.
        """
        self.stats["files_processed"] += 1
        self._log(f"Verarbeite: {html_path.name}", "VERBOSE")

        try:
            html_content = html_path.read_text(encoding="utf-8")
        except Exception as e:
            self._log(f"{html_path.name}: Datei konnte nicht gelesen werden – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        try:
            messages, channel_info, server_info = _extract_b64_data(html_content)
        except TranscriptParseError as e:
            self._log(f"{html_path.name}: Parse-Fehler – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        channel_id   = str(channel_info.get("id", ""))
        channel_name = channel_info.get("name", html_path.stem)

        # Dedup-Check
        if channel_id and self._channel_already_imported(server_id, channel_id, "ticket"):
            self._log(f"{html_path.name}: bereits importiert (channel_id={channel_id})", "SKIP")
            self.stats["files_skipped"] += 1
            return True

        # Modul-Name bestimmen
        module_name = self._get_module_name(module_folder, server_id)
        self._log(f"Modul: {module_name}", "VERBOSE")

        # Nachrichten filtern
        user_messages = _filter_messages(messages)
        self._log(f"Nachrichten: {len(messages)} gesamt → {len(user_messages)} User-Nachrichten", "VERBOSE")

        # Creator ermitteln: erste nicht-Bot-Nachricht
        creator_id   = ""
        creator_name = "Unbekannt"
        for msg in user_messages:
            if not msg.get("bot"):
                creator_id   = str(msg.get("user_id", ""))
                creator_name = msg.get("nick") or msg.get("username") or "Unbekannt"
                break

        # Zeitstempel
        first_msg_ts = None
        last_msg_ts  = None
        if user_messages:
            first_msg_ts = _ms_to_iso(user_messages[0].get("created"))
            last_msg_ts  = _ms_to_iso(user_messages[-1].get("created"))

        # Ticket-ID bestimmen
        hint_number  = _parse_ticket_number(channel_name)

        if self.dry_run:
            self._log(
                f"[DRY] Würde Ticket importieren: "
                f"channel={channel_name}, modul={module_name}, "
                f"creator={creator_name}, msgs={len(user_messages)}",
                "DRY",
            )
            return True

        sb        = get_supabase()
        ticket_id = self._next_ticket_id(server_id)

        # Ticket anlegen
        ticket_row = {
            "ticket_id":          ticket_id,
            "server_id":          server_id,
            "module":             module_name,
            "creator_id":         creator_id,
            "creator_name":       creator_name,
            "description":        f"Importiert von TicketTool (Kanal: {channel_name})",
            "status":             "closed",
            "created_at":         first_msg_ts or datetime.now(timezone.utc).isoformat(),
            "closed_at":          last_msg_ts,
            "added_users":        [],
            "imported":           True,
            "import_source":      IMPORT_SOURCE,
            "external_channel_id": channel_id or None,
        }

        try:
            sb.table("tickets").insert(ticket_row).execute()
        except Exception as e:
            self._log(f"{html_path.name}: DB-Fehler beim Ticket-Insert – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        # Nachrichten speichern
        msg_rows = _build_message_rows(
            user_messages, server_id, ticket_id=ticket_id, entity_type="ticket"
        )
        if msg_rows:
            try:
                # In Chunks von 50 einfügen
                for i in range(0, len(msg_rows), 50):
                    sb.table("ticket_messages").insert(msg_rows[i:i+50]).execute()
            except Exception as e:
                self._log(f"{html_path.name}: DB-Fehler beim Nachrichten-Insert – {e}", "WARN")

        self.stats["tickets_created"] += 1
        self.stats["messages_created"] += len(msg_rows)
        self._log(
            f"✓ Ticket #{ticket_id} importiert: {channel_name} → "
            f"Modul={module_name}, {len(msg_rows)} Nachrichten",
            "OK",
        )
        return True

    def import_application_file(self, html_path: Path, server_id: str) -> bool:
        """
        Importiert eine einzelne Bewerbungs-Transcript-Datei.
        """
        self.stats["files_processed"] += 1
        self._log(f"Verarbeite: {html_path.name}", "VERBOSE")

        try:
            html_content = html_path.read_text(encoding="utf-8")
        except Exception as e:
            self._log(f"{html_path.name}: Datei konnte nicht gelesen werden – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        try:
            messages, channel_info, server_info = _extract_b64_data(html_content)
        except TranscriptParseError as e:
            self._log(f"{html_path.name}: Parse-Fehler – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        channel_id   = str(channel_info.get("id", ""))
        channel_name = channel_info.get("name", html_path.stem)

        if channel_id and self._channel_already_imported(server_id, channel_id, "application"):
            self._log(f"{html_path.name}: bereits importiert (channel_id={channel_id})", "SKIP")
            self.stats["files_skipped"] += 1
            return True

        user_messages = _filter_messages(messages)
        self._log(f"Nachrichten: {len(messages)} gesamt → {len(user_messages)} User-Nachrichten", "VERBOSE")

        creator_id      = ""
        creator_name    = "Unbekannt"
        minecraft_name  = ""
        for msg in user_messages:
            if not msg.get("bot"):
                creator_id   = str(msg.get("user_id", ""))
                creator_name = msg.get("nick") or msg.get("username") or "Unbekannt"
                minecraft_name = creator_name  # Fallback: Discord-Name
                break

        first_msg_ts = _ms_to_iso(user_messages[0].get("created")) if user_messages else None
        last_msg_ts  = _ms_to_iso(user_messages[-1].get("created")) if user_messages else None

        if self.dry_run:
            self._log(
                f"[DRY] Würde Bewerbung importieren: "
                f"channel={channel_name}, creator={creator_name}, msgs={len(user_messages)}",
                "DRY",
            )
            return True

        sb     = get_supabase()
        app_id = self._next_app_id(server_id)

        app_row = {
            "app_id":              app_id,
            "server_id":           server_id,
            "creator_id":          creator_id,
            "creator_name":        creator_name,
            "minecraft_name":      minecraft_name,
            "status":              "closed",
            "created_at":          first_msg_ts or datetime.now(timezone.utc).isoformat(),
            "closed_at":           last_msg_ts,
            "imported":            True,
            "import_source":       IMPORT_SOURCE,
            "external_channel_id": channel_id or None,
            "content":             f"Importiert von TicketTool (Kanal: {channel_name})",
        }

        try:
            sb.table("applications").insert(app_row).execute()
        except Exception as e:
            self._log(f"{html_path.name}: DB-Fehler beim Application-Insert – {e}", "ERROR")
            self.stats["files_error"] += 1
            return False

        msg_rows = _build_message_rows(
            user_messages, server_id, app_id=app_id, entity_type="application"
        )
        if msg_rows:
            try:
                for i in range(0, len(msg_rows), 50):
                    sb.table("application_messages").insert(msg_rows[i:i+50]).execute()
            except Exception as e:
                self._log(f"{html_path.name}: DB-Fehler beim Nachrichten-Insert – {e}", "WARN")

        self.stats["apps_created"] += 1
        self.stats["messages_created"] += len(msg_rows)
        self._log(
            f"✓ Bewerbung #{app_id} importiert: {channel_name} → {len(msg_rows)} Nachrichten",
            "OK",
        )
        return True

    def import_server(self, server_id: str) -> None:
        """Importiert alle Dateien für einen Server."""
        server_dir = IMPORTS_DIR / server_id
        if not server_dir.exists():
            self._log(f"Kein Import-Ordner für Server {server_id}", "WARN")
            return

        self._log(f"Starte Import für Server: {server_id}")

        # ── Tickets ───────────────────────────────────────────────────────────
        tickets_dir = server_dir / "tickets"
        if tickets_dir.exists():
            for module_dir in sorted(tickets_dir.iterdir()):
                if not module_dir.is_dir():
                    continue
                module_folder = module_dir.name
                html_files    = sorted(module_dir.glob("*.html"))
                if not html_files:
                    continue
                self._log(f"Ticket-Modul: {module_folder} ({len(html_files)} Dateien)")
                for html_file in html_files:
                    self.import_ticket_file(html_file, server_id, module_folder)
        else:
            self._log("Kein tickets/-Ordner gefunden", "VERBOSE")

        # ── Applications ──────────────────────────────────────────────────────
        apps_dir = server_dir / "applications"
        if apps_dir.exists():
            html_files = sorted(apps_dir.glob("*.html"))
            if html_files:
                self._log(f"Applications: {len(html_files)} Dateien")
                for html_file in html_files:
                    self.import_application_file(html_file, server_id)
        else:
            self._log("Kein applications/-Ordner gefunden", "VERBOSE")

    def import_all(self) -> None:
        """Importiert alle Server-Ordner unter imports/."""
        if not IMPORTS_DIR.exists():
            self._log(f"Import-Ordner nicht gefunden: {IMPORTS_DIR}", "ERROR")
            return
        server_dirs = [d for d in IMPORTS_DIR.iterdir() if d.is_dir() and d.name.isdigit()]
        if not server_dirs:
            self._log("Keine Server-Ordner gefunden", "WARN")
            return
        self._log(f"Gefundene Server: {[d.name for d in server_dirs]}")
        for server_dir in sorted(server_dirs):
            self.import_server(server_dir.name)

    def print_stats(self) -> None:
        print()
        print("═" * 50)
        print("📊 Import-Statistiken")
        print("═" * 50)
        print(f"  Dateien verarbeitet: {self.stats['files_processed']}")
        print(f"  Dateien übersprungen: {self.stats['files_skipped']}")
        print(f"  Fehler:              {self.stats['files_error']}")
        print(f"  Tickets erstellt:    {self.stats['tickets_created']}")
        print(f"  Bewerbungen erstellt:{self.stats['apps_created']}")
        print(f"  Nachrichten erstellt:{self.stats['messages_created']}")
        if self.dry_run:
            print()
            print("  ⚠️  DRY-RUN – Keine Daten wurden in die DB geschrieben!")
        print("═" * 50)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Importiert TicketTool-Transcripts in das Insel Bot System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--server-id",
        help="Discord-Server-ID für den Import",
    )
    group.add_argument(
        "--all",
        action="store_true",
        help="Alle Server unter imports/ importieren",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nur simulieren, nichts in die DB schreiben",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Ausführliche Ausgabe",
    )
    parser.add_argument(
        "--imports-dir",
        help="Pfad zum Import-Ordner (Standard: ./imports)",
    )
    args = parser.parse_args()

    # Import-Verzeichnis überschreiben wenn angegeben
    global IMPORTS_DIR
    if args.imports_dir:
        IMPORTS_DIR = Path(args.imports_dir)

    # Supabase initialisieren
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_KEY")
    if not supabase_url or not supabase_key:
        print("❌ SUPABASE_URL und SUPABASE_KEY müssen in .env gesetzt sein")
        sys.exit(1)
    init_supabase(supabase_url, supabase_key)

    importer = TicketToolImporter(
        dry_run=args.dry_run,
        verbose=args.verbose,
    )

    if args.dry_run:
        print("🔍 DRY-RUN Modus – Keine Daten werden geschrieben")
        print()

    if args.all:
        importer.import_all()
    else:
        importer.import_server(args.server_id)

    importer.print_stats()


if __name__ == "__main__":
    main()