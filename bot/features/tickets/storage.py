"""
tickets/storage.py
==================
Vollständig Supabase-basierte Speicherung – kein lokales Dateisystem mehr.

Supabase-Tabellen:
  tickets         – Ticket-Metadaten (bereits vorhanden)
  ticket_messages – Alle Nachrichten eines Tickets

SQL für ticket_messages (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS ticket_messages (
        id          BIGSERIAL PRIMARY KEY,
        ticket_id   INTEGER NOT NULL,
        server_id   TEXT    NOT NULL,
        user_name   TEXT,
        user_id     TEXT,
        content     TEXT,
        attachments JSONB   DEFAULT '[]',
        timestamp   TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket
        ON ticket_messages (server_id, ticket_id);
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bot.core.supabase_client import get_supabase

logger = logging.getLogger("tickets.storage")


# ── Ticket CRUD ───────────────────────────────────────────────────────────────

def save_ticket(server_id: str, ticket_id: int, ticket_data: dict):
    """Upsert ticket metadata into Supabase."""
    supabase = get_supabase()
    # Map local field names → DB column names where they differ
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
        # Extra fields stored as metadata
        "description":   ticket_data.get("description"),
        "creator_name":  ticket_data.get("creator_name"),
        "closed_by":     ticket_data.get("closed_by"),
    }
    # Remove None-valued optional columns so Supabase doesn't complain
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
    """Load ticket metadata from Supabase."""
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
    # Normalise: some columns may live under different names in older rows
    row.setdefault("creator_name", row.get("creator_name") or "Unbekannt")
    row.setdefault("description",  row.get("description")  or "")
    row.setdefault("added_users",  row.get("added_users")  or [])
    return row


def update_ticket(server_id: str, ticket_id: int, updates: dict):
    """Partial update of ticket columns."""
    supabase = get_supabase()
    supabase.table("tickets").update(updates).eq("ticket_id", ticket_id).eq("server_id", server_id).execute()


# ── Messages ──────────────────────────────────────────────────────────────────

def save_messages(server_id: str, ticket_id: int, messages: list):
    """Replace all messages for a ticket (used for bulk imports if needed)."""
    supabase = get_supabase()
    # Delete existing then re-insert
    supabase.table("ticket_messages").delete().eq("ticket_id", ticket_id).eq("server_id", server_id).execute()
    for msg in messages:
        _insert_message(supabase, server_id, ticket_id, msg)


def load_messages(server_id: str, ticket_id: int) -> list[dict]:
    """Load all messages for a ticket, ordered by timestamp."""
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
    # Normalise column names for the rest of the codebase
    out = []
    for r in rows:
        out.append({
            "timestamp":   r.get("timestamp", ""),
            "user":        r.get("user_name", "?"),
            "user_id":     r.get("user_id", ""),
            "content":     r.get("content", ""),
            # legacy key used by html_export
            "message":     r.get("content", ""),
            "attachments": r.get("attachments") or [],
        })
    return out


def append_message(
    server_id: str,
    ticket_id: int,
    user: str,
    user_id: str,
    content: str,
    attachments: list | None = None,
):
    """Append a single message to ticket_messages."""
    supabase = get_supabase()
    supabase.table("ticket_messages").insert({
        "ticket_id":   ticket_id,
        "server_id":   server_id,
        "user_name":   user,
        "user_id":     user_id,
        "content":     content,
        "attachments": attachments or [],
        "timestamp":   datetime.now(timezone.utc).isoformat(),
    }).execute()


def _insert_message(supabase, server_id: str, ticket_id: int, msg: dict):
    supabase.table("ticket_messages").insert({
        "ticket_id":   ticket_id,
        "server_id":   server_id,
        "user_name":   msg.get("user", "?"),
        "user_id":     msg.get("user_id", ""),
        "content":     msg.get("content") or msg.get("message", ""),
        "attachments": msg.get("attachments") or [],
        "timestamp":   msg.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── Legacy helper (used by dashboard route) ───────────────────────────────────

def get_all_tickets_for_server(server_id: str) -> list[dict]:
    """Return all tickets for a server (no file system walk needed)."""
    supabase = get_supabase()
    result = supabase.table("tickets").select("*").eq("server_id", server_id).execute()
    return result.data or []