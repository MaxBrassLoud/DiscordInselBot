from __future__ import annotations

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
    "application_messages",
    "application_participants",
    "application_servers",
    "applications",
    "birthdays",
    "events",
    "game_nights",
    "minecraft_names",
    "moderation_logs",
    "role_modules",
    "settings",
    "ticket_messages",
    "ticket_module_roles",
    "ticket_modules",
    "ticket_participants",
    "ticket_reminders",
    "ticket_servers",
    "tickets",
    "user_levels",
    "voice_channels",
    "voice_creator_config",
    "voting_responses",
    "voting_voter_log",
    "votings",
]


@dataclass
class BackupResult:
    path: Path
    table_counts: dict[str, int]
    created_at: datetime

    @property
    def total_rows(self) -> int:
        return sum(self.table_counts.values())


def get_backup_dir() -> Path:
    configured = os.getenv("DB_BACKUP_DIR", "backups/database").strip()
    return Path(configured).expanduser()


def get_backup_keep_count() -> int:
    raw = os.getenv("DB_BACKUP_KEEP", "14").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(f"[backup] Ungueltiger DB_BACKUP_KEEP-Wert: {raw!r}")
        return 14


def get_backup_interval_hours() -> float:
    raw = os.getenv("DB_BACKUP_INTERVAL_HOURS", "24").strip()
    try:
        return max(0.25, float(raw))
    except ValueError:
        logger.warning(f"[backup] Ungueltiger DB_BACKUP_INTERVAL_HOURS-Wert: {raw!r}")
        return 24.0


def get_backup_tables() -> list[str]:
    configured = os.getenv("DB_BACKUP_TABLES", "").strip()
    if not configured:
        return DEFAULT_TABLES.copy()
    tables = [table.strip() for table in configured.split(",") if table.strip()]
    return tables or DEFAULT_TABLES.copy()


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _fetch_table_rows(table_name: str, page_size: int = 1000) -> list[dict[str, Any]]:
    supabase = get_supabase()
    rows: list[dict[str, Any]] = []
    start = 0

    while True:
        end = start + page_size - 1
        response = supabase.table(table_name).select("*").range(start, end).execute()
        batch = response.data or []
        rows.extend(batch)

        if len(batch) < page_size:
            break
        start += page_size

    return rows


def create_database_backup(
    *,
    backup_dir: Path | None = None,
    tables: list[str] | None = None,
) -> BackupResult:
    created_at = datetime.now(timezone.utc)
    selected_tables = tables or get_backup_tables()
    target_dir = backup_dir or get_backup_dir()
    target_dir.mkdir(parents=True, exist_ok=True)

    filename = f"database-backup-{created_at.strftime('%Y%m%d-%H%M%S')}.zip"
    target_path = target_dir / filename
    table_counts: dict[str, int] = {}
    metadata: dict[str, Any] = {
        "created_at": created_at.isoformat(),
        "tables": selected_tables,
        "format": "supabase-table-json-v1",
    }

    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for table_name in selected_tables:
            try:
                rows = _fetch_table_rows(table_name)
            except Exception as exc:
                logger.error(f"[backup] Tabelle {table_name!r} konnte nicht gesichert werden: {exc}")
                raise

            table_counts[table_name] = len(rows)
            archive.writestr(
                f"tables/{table_name}.json",
                json.dumps(rows, ensure_ascii=False, indent=2, default=_json_default),
            )

        metadata["row_counts"] = table_counts
        metadata["total_rows"] = sum(table_counts.values())
        archive.writestr(
            "metadata.json",
            json.dumps(metadata, ensure_ascii=False, indent=2, default=_json_default),
        )

    logger.info(
        f"[backup] Datenbank-Backup erstellt: {target_path} "
        f"({sum(table_counts.values())} Zeilen)"
    )
    return BackupResult(path=target_path, table_counts=table_counts, created_at=created_at)


def list_database_backups(backup_dir: Path | None = None) -> list[Path]:
    target_dir = backup_dir or get_backup_dir()
    if not target_dir.exists():
        return []
    return sorted(
        target_dir.glob("database-backup-*.zip"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def prune_database_backups(keep: int | None = None, backup_dir: Path | None = None) -> list[Path]:
    keep_count = keep if keep is not None else get_backup_keep_count()
    backups = list_database_backups(backup_dir)
    removed: list[Path] = []

    for path in backups[keep_count:]:
        try:
            path.unlink()
            removed.append(path)
            logger.info(f"[backup] Altes Backup geloescht: {path}")
        except OSError as exc:
            logger.warning(f"[backup] Backup konnte nicht geloescht werden ({path}): {exc}")

    return removed
