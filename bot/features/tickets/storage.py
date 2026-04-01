"""
tickets/storage.py  (EXTENDED VERSION)
=======================================
Erweiterte Supabase-basierte Speicherung mit:
  - Bearbeitungs-History für Nachrichten (edits)
  - Lösch-Markierung für Nachrichten (deleted)
  - Teilnehmer-Tracking (participants)

Neue / geänderte Supabase-Spalten (einmalig ausführen):
    ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
    ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
    ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS edit_history JSONB DEFAULT '[]';
    ALTER TABLE ticket_messages ADD COLUMN IF NOT EXISTS discord_message_id TEXT;

    -- Teilnehmer-Tabelle
    CREATE TABLE IF NOT EXISTS ticket_participants (
        id          BIGSERIAL PRIMARY KEY,
        ticket_id   INTEGER NOT NULL,
        server_id   TEXT    NOT NULL,
        user_id     TEXT    NOT NULL,
        user_name   TEXT,
        avatar_url  TEXT,
        action      TEXT    DEFAULT 'message',  -- message / added / removed / claimed / closed / created
        first_seen  TIMESTAMPTZ DEFAULT now(),
        last_seen   TIMESTAMPTZ DEFAULT now(),
        message_count INTEGER DEFAULT 0,
        UNIQUE (ticket_id, server_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ticket_participants
        ON ticket_participants (server_id, ticket_id);
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.core.supabase_client import get_supabase

logger = logging.getLogger("tickets.storage")


# ── Ticket CRUD ───────────────────────────────────────────────────────────────

def save_ticket(server_id: str, ticket_id: int, ticket_data: dict):
    supabase = get_supabase()
    row = {
        "ticket_id":   ticket_data.get("ticket_id", ticket_id),
        "server_id":   ticket_data.get("server_id", server_id),
        "module":      ticket_data.get("module"),
        "creator_id":  ticket_data.get("creator_id"),
        "claimed_by":  ticket_data.get("claimed_by"),
        "status":      ticket_data.get("status", "open"),
        "created_at":  ticket_data.get("created_at"),
        "closed_at":   ticket_data.get("closed_at"),
        "channel_id":  ticket_data.get("channel_id"),
        "added_users": ticket_data.get("added_users", []),
        "description":   ticket_data.get("description"),
        "creator_name":  ticket_data.get("creator_name"),
        "closed_by":     ticket_data.get("closed_by"),
    }
    row = {k: v for k, v in row.items() if v is not None or k in ("claimed_by", "closed_at", "closed_by")}

    existing = (
        supabase.table("tickets")
        .select("ticket_id")
        .eq("ticket_id", ticket_id)
        .eq("server_id", server_id)
        .execute()
    )
    if existing.data:
        supabase.table("tickets").update(row).eq("ticket_id", ticket_id).eq("server_id", server_id).execute()
    else:
        supabase.table("tickets").insert(row).execute()

    logger.debug(f"Ticket {ticket_id} gespeichert (Supabase)")


def load_ticket(server_id: str, ticket_id: int) -> dict | None:
    supabase = get_supabase()
    result = (
        supabase.table("tickets")
        .select("*")
        .eq("ticket_id", ticket_id)
        .eq("server_id", server_id)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    row.setdefault("creator_name", row.get("creator_name") or "Unbekannt")
    row.setdefault("description",  row.get("description")  or "")
    row.setdefault("added_users",  row.get("added_users")  or [])
    return row


def update_ticket(server_id: str, ticket_id: int, updates: dict):
    supabase = get_supabase()
    supabase.table("tickets").update(updates).eq("ticket_id", ticket_id).eq("server_id", server_id).execute()


# ── Messages ──────────────────────────────────────────────────────────────────

def save_messages(server_id: str, ticket_id: int, messages: list):
    supabase = get_supabase()
    supabase.table("ticket_messages").delete().eq("ticket_id", ticket_id).eq("server_id", server_id).execute()
    for msg in messages:
        _insert_message(supabase, server_id, ticket_id, msg)


def load_messages(server_id: str, ticket_id: int) -> list[dict]:
    supabase = get_supabase()
    result = (
        supabase.table("ticket_messages")
        .select("*")
        .eq("ticket_id", ticket_id)
        .eq("server_id", server_id)
        .order("timestamp", desc=False)
        .execute()
    )
    rows = result.data or []
    out = []
    for r in rows:
        out.append({
            "id":                 r.get("id"),
            "discord_message_id": r.get("discord_message_id"),
            "timestamp":          r.get("timestamp", ""),
            "user":               r.get("user_name", "?"),
            "user_id":            r.get("user_id", ""),
            "content":            r.get("content", ""),
            "message":            r.get("content", ""),
            "attachments":        r.get("attachments") or [],
            "is_deleted":         r.get("is_deleted", False),
            "deleted_at":         r.get("deleted_at"),
            "edit_history":       r.get("edit_history") or [],
        })
    return out


def append_message(
    server_id: str,
    ticket_id: int,
    user: str,
    user_id: str,
    content: str,
    attachments: list | None = None,
    discord_message_id: str | None = None,
):
    supabase = get_supabase()
    supabase.table("ticket_messages").insert({
        "ticket_id":          ticket_id,
        "server_id":          server_id,
        "user_name":          user,
        "user_id":            user_id,
        "content":            content,
        "attachments":        attachments or [],
        "timestamp":          datetime.now(timezone.utc).isoformat(),
        "discord_message_id": discord_message_id,
        "is_deleted":         False,
        "edit_history":       [],
    }).execute()

    # Teilnehmer updaten
    _upsert_participant(server_id, ticket_id, user_id, user, action="message")


def mark_message_deleted(server_id: str, ticket_id: int, discord_message_id: str):
    """Markiert eine Nachricht als gelöscht anhand der Discord Message ID."""
    supabase = get_supabase()
    supabase.table("ticket_messages").update({
        "is_deleted": True,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }).eq("server_id", server_id)\
      .eq("ticket_id", ticket_id)\
      .eq("discord_message_id", discord_message_id)\
      .execute()


def append_message_edit(
    server_id: str,
    ticket_id: int,
    discord_message_id: str,
    old_content: str,
    new_content: str,
):
    """Fügt einen Edit-Eintrag zur edit_history hinzu und aktualisiert content."""
    supabase = get_supabase()
    result = supabase.table("ticket_messages")\
        .select("id, edit_history, content")\
        .eq("server_id", server_id)\
        .eq("ticket_id", ticket_id)\
        .eq("discord_message_id", discord_message_id)\
        .execute()
    if not result.data:
        return
    row = result.data[0]
    history = row.get("edit_history") or []
    history.append({
        "content":   old_content,
        "edited_at": datetime.now(timezone.utc).isoformat(),
    })
    supabase.table("ticket_messages").update({
        "content":      new_content,
        "edit_history": history,
    }).eq("id", row["id"]).execute()


# ── Participants ──────────────────────────────────────────────────────────────

def _upsert_participant(
    server_id: str,
    ticket_id: int,
    user_id: str,
    user_name: str,
    action: str = "message",
    avatar_url: str | None = None,
):
    """Erstellt oder aktualisiert einen Teilnehmer-Eintrag."""
    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    existing = supabase.table("ticket_participants")\
        .select("id, message_count")\
        .eq("ticket_id", ticket_id)\
        .eq("server_id", server_id)\
        .eq("user_id", user_id)\
        .execute()
    if existing.data:
        row = existing.data[0]
        update_data: dict = {"last_seen": now}
        if action == "message":
            update_data["message_count"] = (row.get("message_count") or 0) + 1
        if avatar_url:
            update_data["avatar_url"] = avatar_url
        if user_name:
            update_data["user_name"] = user_name
        supabase.table("ticket_participants").update(update_data).eq("id", row["id"]).execute()
    else:
        supabase.table("ticket_participants").insert({
            "ticket_id":     ticket_id,
            "server_id":     server_id,
            "user_id":       user_id,
            "user_name":     user_name,
            "avatar_url":    avatar_url,
            "action":        action,
            "first_seen":    now,
            "last_seen":     now,
            "message_count": 1 if action == "message" else 0,
        }).execute()


def load_participants(server_id: str, ticket_id: int) -> list[dict]:
    """Lädt alle Teilnehmer eines Tickets."""
    supabase = get_supabase()
    result = supabase.table("ticket_participants")\
        .select("*")\
        .eq("ticket_id", ticket_id)\
        .eq("server_id", server_id)\
        .order("message_count", desc=True)\
        .execute()
    return result.data or []


def add_participant_event(
    server_id: str,
    ticket_id: int,
    user_id: str,
    user_name: str,
    action: str,
    avatar_url: str | None = None,
):
    """Fügt einen Teilnehmer mit spezifischer Aktion hinzu (added, removed, etc.)."""
    _upsert_participant(server_id, ticket_id, user_id, user_name, action, avatar_url)


# ── Legacy helper ─────────────────────────────────────────────────────────────

def get_all_tickets_for_server(server_id: str) -> list[dict]:
    supabase = get_supabase()
    result = supabase.table("tickets").select("*").eq("server_id", server_id).execute()
    return result.data or []


def _insert_message(supabase, server_id: str, ticket_id: int, msg: dict):
    supabase.table("ticket_messages").insert({
        "ticket_id":          ticket_id,
        "server_id":          server_id,
        "user_name":          msg.get("user", "?"),
        "user_id":            msg.get("user_id", ""),
        "content":            msg.get("content") or msg.get("message", ""),
        "attachments":        msg.get("attachments") or [],
        "timestamp":          msg.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "discord_message_id": msg.get("discord_message_id"),
        "is_deleted":         msg.get("is_deleted", False),
        "edit_history":       msg.get("edit_history") or [],
    }).execute()