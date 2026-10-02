"""
bot/features/moderation/raid/word_store.py
==========================================
Datenmodell und Datenbankzugriff fuer den Wortfilter.

Tabellen (siehe sql/word_filter.sql):
  word_filter_config        Server-Konfiguration (an/aus, Aktionen, Kanaele)
  word_filter_entries       verbotene Woerter/Zahlen/Regex
  word_filter_allow         Whitelist gegen Fehlalarme

Die Supabase-Aufrufe sind synchron und werden von den Cogs ueber
`asyncio.to_thread` ausgefuehrt, damit der Discord-Eventloop frei bleibt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("raid.word_store")

TABLE_CONFIG = "word_filter_config"
TABLE_ENTRIES = "word_filter_entries"
TABLE_ALLOW = "word_filter_allow"
TABLE_LOGS = "moderation_logs"

# Aktionen, die ein Treffer ausloesen kann.
ACTIONS = ("delete", "warn", "timeout")

ACTION_LABELS = {
    "delete": "Nachricht löschen",
    "warn": "Löschen + Verwarnung",
    "timeout": "Löschen + Timeout",
}

# Trefferarten
MATCH_TYPES = ("word", "token", "substring", "regex")

MATCH_TYPE_LABELS = {
    "word": "Ganzes Wort (Standard)",
    "token": "Ganzes Token (auch Zahlen)",
    "substring": "Teilstring (irgendwo im Text)",
    "regex": "Regulärer Ausdruck",
}

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,
    "dry_run": False,
    "default_action": "timeout",
    "timeout_minutes": 30,
    "log_channel_id": None,
    "exempt_channel_ids": "",
    "exempt_role_ids": "",
    "check_edits": True,
    "delete_message": True,
    "notify_user": True,
    "min_message_length": 1,
}


@dataclass
class WordEntry:
    """Ein Filter-Eintrag."""

    pattern: str
    match_type: str = "word"
    action: Optional[str] = None
    severity: int = 1
    fuzzy: bool = True
    bypass_urls: bool = True
    bypass_code: bool = True
    note: Optional[str] = None
    enabled: bool = True
    entry_id: Optional[int] = None
    hit_count: int = 0
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # Laufzeitdaten (nicht in der DB)
    normalized: str = ""
    keywords: tuple[str, ...] = field(default=(), repr=False)
    whitelist: tuple[str, ...] = field(default=(), repr=False)

    # ---- Komfort ----
    @property
    def effective_match_type(self) -> str:
        return self.match_type or "word"

    def action_for(self, default: str = "timeout") -> str:
        return self.action or default

    @property
    def is_numeric(self) -> bool:
        return bool(self.normalized) and self.normalized.isdigit()

    def to_db(self) -> Dict[str, Any]:
        return {
            "pattern": self.pattern,
            "match_type": self.effective_match_type,
            "action": self.action,
            "severity": self.severity,
            "fuzzy": self.fuzzy,
            "bypass_urls": self.bypass_urls,
            "bypass_code": self.bypass_code,
            "note": self.note,
            "enabled": self.enabled,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "WordEntry":
        return cls(
            pattern=str(row.get("pattern", "")),
            match_type=str(row.get("match_type") or "word"),
            action=row.get("action"),
            severity=int(row.get("severity") or 1),
            fuzzy=bool(row.get("fuzzy", True)),
            bypass_urls=bool(row.get("bypass_urls", True)),
            bypass_code=bool(row.get("bypass_code", True)),
            note=row.get("note"),
            enabled=bool(row.get("enabled", True)),
            entry_id=row.get("id"),
            hit_count=int(row.get("hit_count") or 0),
            created_by=row.get("created_by"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
        )


# ── Konfiguration ───────────────────────────────────────────────────────────

def _config_row(server_id: str) -> Optional[Dict[str, Any]]:
    response = (
        get_supabase()
        .table(TABLE_CONFIG)
        .select("*")
        .eq("server_id", str(server_id))
        .execute()
    )
    return response.data[0] if response.data else None


def get_config(server_id: str) -> Dict[str, Any]:
    """Liefert die Server-Konfiguration inklusive Defaults."""
    config = dict(DEFAULT_CONFIG)
    try:
        row = _config_row(server_id)
    except Exception as error:  # pragma: no cover - DB-Ausfall
        logger.warning(f"[wordfilter] get_config fehlgeschlagen: {error}")
        return config
    if not row:
        return config
    for key in DEFAULT_CONFIG:
        if key in row and row[key] is not None:
            config[key] = row[key]
    return config


def set_config(server_id: str, data: Dict[str, Any]) -> bool:
    """Upsert der Server-Konfiguration (nur bekannte Felder)."""
    payload = {
        key: value for key, value in data.items() if key in DEFAULT_CONFIG
    }
    payload["server_id"] = str(server_id)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        existing = _config_row(server_id)
        if existing:
            (
                get_supabase()
                .table(TABLE_CONFIG)
                .update(payload)
                .eq("server_id", str(server_id))
                .execute()
            )
        else:
            get_supabase().table(TABLE_CONFIG).insert(payload).execute()
        return True
    except Exception as error:
        logger.error(f"[wordfilter] set_config fehlgeschlagen: {error}")
        return False


# ── Eintraege ───────────────────────────────────────────────────────────────

def list_entries(server_id: str, only_enabled: bool = False) -> List[WordEntry]:
    try:
        query = get_supabase().table(TABLE_ENTRIES).select("*").eq(
            "server_id", str(server_id)
        )
        if only_enabled:
            query = query.eq("enabled", True)
        response = query.order("id").execute()
    except Exception as error:
        logger.warning(f"[wordfilter] list_entries fehlgeschlagen: {error}")
        return []
    return [WordEntry.from_row(row) for row in (response.data or [])]


def load_entries(server_id: str, only_enabled: bool = False) -> List[WordEntry]:
    """Alias mit sprechendem Namen fuer den Matcher-Cache."""
    return list_entries(server_id, only_enabled=only_enabled)


def load_server_state(server_id: str, only_enabled: bool = True) -> tuple[Dict[str, Any], List[WordEntry], List[str]]:
    """
    Laedt alles, was der Matcher fuer einen Server braucht, in EINEM Durchlauf:
    (config, entries, whitelist).

    Der Import des Matchers passiert bewusst hier und lazy, damit dieses Modul
    ohne den Matcher nutzbar bleibt.
    """
    from .matcher import refresh_entries_cache

    config = get_config(server_id)
    entries = list_entries(server_id, only_enabled=only_enabled)
    allow = list_allow(server_id)
    refresh_entries_cache(entries, allow)
    return config, entries, allow


def get_entry(server_id: str, pattern: str) -> Optional[WordEntry]:
    try:
        response = (
            get_supabase()
            .table(TABLE_ENTRIES)
            .select("*")
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
    except Exception as error:
        logger.warning(f"[wordfilter] get_entry fehlgeschlagen: {error}")
        return None
    return WordEntry.from_row(response.data[0]) if response.data else None


def entry_exists(server_id: str, pattern: str) -> bool:
    try:
        response = (
            get_supabase()
            .table(TABLE_ENTRIES)
            .select("id")
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
        return bool(response.data)
    except Exception as error:
        logger.warning(f"[wordfilter] entry_exists fehlgeschlagen: {error}")
        return False


def add_entry(server_id: str, entry: WordEntry) -> bool:
    payload = entry.to_db()
    payload["server_id"] = str(server_id)
    payload["created_by"] = entry.created_by
    payload["created_at"] = datetime.now(timezone.utc).isoformat()
    payload["hit_count"] = 0
    try:
        get_supabase().table(TABLE_ENTRIES).insert(payload).execute()
        return True
    except Exception as error:
        logger.error(f"[wordfilter] add_entry fehlgeschlagen: {error}")
        return False


def update_entry(server_id: str, pattern: str, data: Dict[str, Any]) -> bool:
    payload = dict(data)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        response = (
            get_supabase()
            .table(TABLE_ENTRIES)
            .update(payload)
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
        return bool(response.data)
    except Exception as error:
        logger.error(f"[wordfilter] update_entry fehlgeschlagen: {error}")
        return False


def remove_entry(server_id: str, pattern: str) -> bool:
    try:
        response = (
            get_supabase()
            .table(TABLE_ENTRIES)
            .delete()
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
        return bool(response.data)
    except Exception as error:
        logger.error(f"[wordfilter] remove_entry fehlgeschlagen: {error}")
        return False


def clear_entries(server_id: str) -> int:
    try:
        response = (
            get_supabase()
            .table(TABLE_ENTRIES)
            .delete()
            .eq("server_id", str(server_id))
            .execute()
        )
        return len(response.data or [])
    except Exception as error:
        logger.error(f"[wordfilter] clear_entries fehlgeschlagen: {error}")
        return 0


def register_hit(server_id: str, pattern: str, amount: int = 1) -> None:
    """Zaehlt einen Treffer hoch (best effort, Fehler werden geloggt)."""
    try:
        entry = get_entry(server_id, pattern)
        if not entry:
            return
        (
            get_supabase()
            .table(TABLE_ENTRIES)
            .update(
                {
                    "hit_count": (entry.hit_count or 0) + amount,
                    "last_hit_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
    except Exception as error:
        logger.debug(f"[wordfilter] register_hit fehlgeschlagen: {error}")


# ── Whitelist ───────────────────────────────────────────────────────────────

def list_allow(server_id: str) -> List[str]:
    try:
        response = (
            get_supabase()
            .table(TABLE_ALLOW)
            .select("pattern")
            .eq("server_id", str(server_id))
            .execute()
        )
    except Exception as error:
        logger.warning(f"[wordfilter] list_allow fehlgeschlagen: {error}")
        return []
    return [str(row.get("pattern", "")) for row in (response.data or []) if row.get("pattern")]


def add_allow(server_id: str, pattern: str, created_by: Optional[str] = None) -> bool:
    try:
        existing = (
            get_supabase()
            .table(TABLE_ALLOW)
            .select("pattern")
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
        if existing.data:
            return False
        get_supabase().table(TABLE_ALLOW).insert(
            {
                "server_id": str(server_id),
                "pattern": pattern,
                "created_by": created_by,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
        return True
    except Exception as error:
        logger.error(f"[wordfilter] add_allow fehlgeschlagen: {error}")
        return False


def remove_allow(server_id: str, pattern: str) -> bool:
    try:
        response = (
            get_supabase()
            .table(TABLE_ALLOW)
            .delete()
            .eq("server_id", str(server_id))
            .eq("pattern", pattern)
            .execute()
        )
        return bool(response.data)
    except Exception as error:
        logger.error(f"[wordfilter] remove_allow fehlgeschlagen: {error}")
        return False


# ── Moderationslog ──────────────────────────────────────────────────────────

def log_action(
    server_id: str,
    action: str,
    target_id: str,
    target_name: str,
    moderator_id: Optional[str],
    moderator_name: Optional[str],
    reason: str,
    until: Optional[datetime] = None,
) -> None:
    try:
        get_supabase().table(TABLE_LOGS).insert(
            {
                "server_id": str(server_id),
                "action": action,
                "target_id": str(target_id),
                "target_name": target_name,
                "moderator_id": moderator_id,
                "moderator_name": moderator_name,
                "reason": reason,
                "until": until.isoformat() if until else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except Exception as error:
        logger.warning(f"[wordfilter] log_action fehlgeschlagen: {error}")


# ── Hilfsfunktionen fuer Kanal-/Rollenlisten ───────────────────────────────

def parse_id_list(value: Any) -> List[str]:
    """'1,2 3' -> ['1', '2', '3']"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).replace(";", ",").split(",") if part.strip()]


def join_id_list(values: Iterable[Any]) -> str:
    seen: List[str] = []
    for value in values:
        text = str(value).strip()
        if text and text not in seen:
            seen.append(text)
    return ",".join(seen)
