from __future__ import annotations

import base64
import io
import json
import os
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("database_backup")

DEFAULT_TABLES = [
    "applications",
    "application_messages",
    "application_participants",
    "application_servers",
    "birthdays",
    "events",
    "game_nights",
    "minecraft_names",
    "moderation_logs",
    "role_modules",
    "settings",
    "tickets",
    "ticket_messages",
    "ticket_modules",
    "ticket_module_roles",
    "ticket_participants",
    "ticket_reminders",
    "ticket_servers",
    "user_levels",
    "voice_channels",
    "voice_creator_config",
    "votings",
    "voting_responses",
    "voting_voter_log",
]

RESTORE_TABLE_ORDER = [
    "applications",
    "application_messages",
    "application_participants",
    "application_servers",
    "birthdays",
    "events",
    "game_nights",
    "minecraft_names",
    "moderation_logs",
    "role_modules",
    "settings",
    "tickets",
    "ticket_messages",
    "ticket_modules",
    "ticket_module_roles",
    "ticket_participants",
    "ticket_reminders",
    "ticket_servers",
    "user_levels",
    "voice_channels",
    "voice_creator_config",
    "votings",
    "voting_responses",
    "voting_voter_log",
]

# Alte Backup-Dateien werden beim ersten Aufruf in die DB verschoben.
LEGACY_BACKUP_DIR = Path(
    os.getenv("DB_BACKUP_DIR", "backups/database")
).expanduser()

_legacy_migration_done = False


@dataclass
class BackupResult:
    name: str
    table_counts: dict[str, int]
    created_at: datetime
    content: bytes | None = None

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


@dataclass
class RestoreResult:
    name: str
    table_counts: dict[str, int]
    skipped_counts: dict[str, int]

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())

    @property
    def skipped_rows(self) -> int:
        return sum(self.skipped_counts.values())


# ── BYTEA-Helfer (PostgREST-kompatibel) ──────────────────────────────────────

def _encode_bytea(content: bytes) -> str:
    """
    Kodiert Bytes in das PostgREST-Hex-Format für BYTEA-Spalten.

    PostgREST akzeptiert BYTEA-Werte als `\\x<hexdigits>`.
    Ohne diesen Schritt versucht der Supabase-Client, die rohen bytes
    per json.dumps() zu serialisieren – das schlägt fehl mit
    "Object of type bytes is not JSON serializable".
    """
    return "\\x" + content.hex()


def _decode_bytea(value: Any) -> bytes:
    """
    Dekodiert einen BYTEA-Wert aus der Supabase-Antwort.

    Unterstützt:
      * raw bytes / bytearray / memoryview (falls Treiber direkt liefert)
      * PostgREST-Hex-Format `\\x<hexdigits>` (Standard bei BYTEA)
      * Base64-String (Fallback, z. B. nach manueller Migration auf TEXT)
    """
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, str):
        # PostgREST-Hex-Format
        if value.startswith("\\x"):
            try:
                return bytes.fromhex(value[2:])
            except ValueError as exc:
                logger.warning(
                    f"[backup] Ungueltiges Hex-Format im BYTEA-Feld: {exc}"
                )
                return b""
        # Base64-Fallback (falls Spalte auf TEXT migriert wurde)
        try:
            return base64.b64decode(value, validate=True)
        except Exception:
            # Letzter Ausweg: als Latin-1 kodieren
            return value.encode("latin-1")
    raise ValueError(f"Unerwarteter BYTEA-Typ: {type(value)!r}")


# ── Legacy-Migration ─────────────────────────────────────────────────────────

def _migrate_legacy_files() -> None:
    """Verschiebt vorhandene ZIP-Backups aus dem Dateisystem in die DB."""
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    if not LEGACY_BACKUP_DIR.exists():
        return

    files = sorted(LEGACY_BACKUP_DIR.glob("database-backup-*.zip"))
    if not files:
        try:
            LEGACY_BACKUP_DIR.rmdir()
        except OSError:
            pass
        return

    sb = get_supabase()
    for path in files:
        try:
            existing = (
                sb.table("database_backups")
                .select("id")
                .eq("filename", path.name)
                .execute()
            )
            if existing.data:
                logger.info(
                    f"[backup] Legacy-Datei bereits in DB: {path.name}"
                )
                path.unlink(missing_ok=True)
                continue

            content = path.read_bytes()
            created_ts = datetime.fromtimestamp(
                path.stat().st_mtime, tz=timezone.utc
            ).isoformat()

            sb.table("database_backups").insert(
                {
                    "filename": path.name,
                    "content": _encode_bytea(content),
                    "created_at": created_ts,
                }
            ).execute()
            path.unlink()
            logger.info(
                f"[backup] Legacy-Datei in DB migriert: {path.name}"
            )
        except Exception as exc:
            logger.error(
                f"[backup] Migration von {path} fehlgeschlagen: {exc}"
            )

    try:
        if LEGACY_BACKUP_DIR.exists() and not any(
            LEGACY_BACKUP_DIR.iterdir()
        ):
            LEGACY_BACKUP_DIR.rmdir()
    except OSError:
        pass


# ── Konfig-Helfer ────────────────────────────────────────────────────────────

def get_backup_keep_count() -> int:
    raw = os.getenv("DB_BACKUP_KEEP", "14").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            f"[backup] Ungueltiger DB_BACKUP_KEEP-Wert: {raw!r}"
        )
        return 14


def get_backup_interval_hours() -> float:
    raw = os.getenv("DB_BACKUP_INTERVAL_HOURS", "24").strip()
    try:
        return max(0.25, float(raw))
    except ValueError:
        logger.warning(
            f"[backup] Ungueltiger DB_BACKUP_INTERVAL_HOURS-Wert: "
            f"{raw!r}"
        )
        return 24.0


def get_backup_tables() -> list[str]:
    configured = os.getenv("DB_BACKUP_TABLES", "").strip()
    if not configured:
        return DEFAULT_TABLES.copy()
    tables = [t.strip() for t in configured.split(",") if t.strip()]
    return tables or DEFAULT_TABLES.copy()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        # Innerhalb der Tabelle-JSONs als Hex speichern, damit Restore
        # BYTEA-Spalten korrekt zurückschreiben kann.
        return "\\x" + bytes(value).hex()
    return str(value)


# ── Supabase-Helfer ──────────────────────────────────────────────────────────

def _fetch_table_rows(
    table_name: str, page_size: int = 1000
) -> list[dict[str, Any]]:
    supabase = get_supabase()
    rows: list[dict[str, Any]] = []
    start = 0
    while True:
        end = start + page_size - 1
        response = (
            supabase.table(table_name)
            .select("*")
            .range(start, end)
            .execute()
        )
        batch = response.data or []
        rows.extend(batch)
        if len(batch) < page_size:
            break
        start += page_size
    return rows


def _chunk_rows(rows: list[dict[str, Any]], chunk_size: int = 500):
    for index in range(0, len(rows), chunk_size):
        yield rows[index:index + chunk_size]


def _is_duplicate_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "23505" in text
        or "duplicate key value violates unique constraint" in text
    )


def _insert_rows_skip_duplicates(
    table_name: str, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    supabase = get_supabase()
    inserted = 0
    skipped = 0
    for chunk in _chunk_rows(rows):
        if not chunk:
            continue
        try:
            supabase.table(table_name).insert(chunk).execute()
            inserted += len(chunk)
            continue
        except Exception as exc:
            if not _is_duplicate_error(exc):
                raise
        for row in chunk:
            try:
                supabase.table(table_name).insert(row).execute()
                inserted += 1
            except Exception as exc:
                if _is_duplicate_error(exc):
                    skipped += 1
                    continue
                raise
    return inserted, skipped


def _sort_tables_for_restore(tables: list[str]) -> list[str]:
    known = [t for t in RESTORE_TABLE_ORDER if t in tables]
    unknown = [t for t in tables if t not in RESTORE_TABLE_ORDER]
    return known + unknown


def _load_backup_content(filename: str) -> bytes:
    sb = get_supabase()
    r = (
        sb.table("database_backups")
        .select("content")
        .eq("filename", filename)
        .execute()
    )
    if not r.data:
        raise FileNotFoundError(f"Backup nicht gefunden: {filename}")
    return _decode_bytea(r.data[0]["content"])


# ── Restore ──────────────────────────────────────────────────────────────────

def restore_database_backup(backup_name: str | None = None) -> RestoreResult:
    _migrate_legacy_files()
    sb = get_supabase()

    if backup_name:
        filename = Path(backup_name).name
        r = (
            sb.table("database_backups")
            .select("filename")
            .eq("filename", filename)
            .execute()
        )
        if not r.data:
            raise FileNotFoundError(f"Backup nicht gefunden: {filename}")
    else:
        r = (
            sb.table("database_backups")
            .select("filename")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not r.data:
            raise FileNotFoundError("Es wurde kein Backup gefunden.")
        filename = r.data[0]["filename"]

    content = _load_backup_content(filename)
    table_counts: dict[str, int] = {}
    skipped_counts: dict[str, int] = {}

    with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
        try:
            metadata = json.loads(
                archive.read("metadata.json").decode("utf-8")
            )
            tables = metadata.get("tables") or []
        except KeyError:
            tables = [
                name.removeprefix("tables/").removesuffix(".json")
                for name in archive.namelist()
                if name.startswith("tables/") and name.endswith(".json")
            ]

        for table_name in _sort_tables_for_restore(tables):
            table_file = f"tables/{table_name}.json"
            if table_file not in archive.namelist():
                continue

            rows = json.loads(archive.read(table_file).decode("utf-8"))
            if not isinstance(rows, list):
                raise ValueError(
                    f"Ungueltiges Tabellenformat in {table_file}"
                )

            inserted, skipped = _insert_rows_skip_duplicates(
                table_name, rows
            )
            table_counts[table_name] = inserted
            skipped_counts[table_name] = skipped

    logger.info(
        f"[backup] Datenbank-Backup geladen: {filename} "
        f"({sum(table_counts.values())} eingefuegt, "
        f"{sum(skipped_counts.values())} uebersprungen)"
    )
    return RestoreResult(
        name=filename,
        table_counts=table_counts,
        skipped_counts=skipped_counts,
    )


# ── Create ───────────────────────────────────────────────────────────────────

def create_database_backup(
    *,
    tables: list[str] | None = None,
) -> BackupResult:
    _migrate_legacy_files()

    created_at = datetime.now(timezone.utc)
    selected_tables = tables or get_backup_tables()
    filename = f"database-backup-{created_at.strftime('%Y%m%d-%H%M%S')}.zip"
    table_counts: dict[str, int] = {}
    metadata: dict[str, Any] = {
        "created_at": created_at.isoformat(),
        "tables": selected_tables,
        "format": "supabase-table-json-v1",
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(
        buffer, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for table_name in selected_tables:
            try:
                rows = _fetch_table_rows(table_name)
            except Exception as exc:
                logger.error(
                    f"[backup] Tabelle {table_name!r} konnte nicht "
                    f"gesichert werden: {exc}"
                )
                raise

            table_counts[table_name] = len(rows)
            archive.writestr(
                f"tables/{table_name}.json",
                json.dumps(
                    rows,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                ),
            )

        metadata["row_counts"] = table_counts
        metadata["total_rows"] = sum(table_counts.values())
        archive.writestr(
            "metadata.json",
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
                default=_json_default,
            ),
        )

    content = buffer.getvalue()
    total_rows = sum(table_counts.values())

    # FIX: content als PostgREST-Hex-String übergeben – sonst schlägt
    # der interne json.dumps() im Supabase-Client fehl.
    payload = {
        "filename": filename,
        "content": _encode_bytea(content),
        "table_counts": table_counts,
        "total_rows": total_rows,
        "created_at": created_at.isoformat(),
    }

    sb = get_supabase()
    try:
        existing = (
            sb.table("database_backups")
            .select("id")
            .eq("filename", filename)
            .execute()
        )
        if existing.data:
            (
                sb.table("database_backups")
                .update(payload)
                .eq("filename", filename)
                .execute()
            )
        else:
            sb.table("database_backups").insert(payload).execute()
    except Exception as exc:
        logger.error(
            f"[backup] Backup konnte nicht in DB gespeichert werden: "
            f"{exc}"
        )
        raise

    logger.info(
        f"[backup] Datenbank-Backup erstellt: {filename} "
        f"({total_rows} Zeilen)"
    )
    return BackupResult(
        name=filename,
        table_counts=table_counts,
        created_at=created_at,
        content=content,
    )


# ── List / Prune ─────────────────────────────────────────────────────────────

def list_database_backups() -> list[dict]:
    """Gibt die Backups aus der DB zurück – neueste zuerst."""
    _migrate_legacy_files()
    sb = get_supabase()
    try:
        r = (
            sb.table("database_backups")
            .select("filename, created_at, total_rows")
            .order("created_at", desc=True)
            .execute()
        )
        return [
            {
                "name": row["filename"],
                "created_at": row.get("created_at"),
                "total_rows": row.get("total_rows", 0),
            }
            for row in (r.data or [])
        ]
    except Exception as exc:
        logger.error(
            f"[backup] list_database_backups fehlgeschlagen: {exc}"
        )
        return []


def prune_database_backups(
    keep: int | None = None,
) -> list[str]:
    keep_count = keep if keep is not None else get_backup_keep_count()
    backups = list_database_backups()
    removed: list[str] = []
    sb = get_supabase()

    for entry in backups[keep_count:]:
        try:
            sb.table("database_backups").delete().eq(
                "filename", entry["name"]
            ).execute()
            removed.append(entry["name"])
            logger.info(
                f"[backup] Altes Backup geloescht: {entry['name']}"
            )
        except Exception as exc:
            logger.warning(
                f"[backup] Backup konnte nicht geloescht werden "
                f"({entry['name']}): {exc}"
            )

    return removed