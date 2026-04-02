"""
applications/manager.py  (EXTENDED VERSION)
============================================
Erweitert um:
  - discord_message_id Tracking für Nachrichten
  - mark_app_message_deleted() – Lösch-Markierung
  - append_app_message_edit()  – Bearbeitungs-History
  - load_app_participants()    – Teilnehmer-Tracking

Neue Supabase-Spalten (einmalig ausführen):
    ALTER TABLE application_messages ADD COLUMN IF NOT EXISTS is_deleted    BOOLEAN     DEFAULT FALSE;
    ALTER TABLE application_messages ADD COLUMN IF NOT EXISTS deleted_at    TIMESTAMPTZ;
    ALTER TABLE application_messages ADD COLUMN IF NOT EXISTS edit_history  JSONB       DEFAULT '[]';
    ALTER TABLE application_messages ADD COLUMN IF NOT EXISTS discord_message_id TEXT;

    CREATE TABLE IF NOT EXISTS application_participants (
        id            BIGSERIAL PRIMARY KEY,
        app_id        INTEGER NOT NULL,
        server_id     TEXT    NOT NULL,
        user_id       TEXT    NOT NULL,
        user_name     TEXT,
        avatar_url    TEXT,
        action        TEXT    DEFAULT 'message',
        first_seen    TIMESTAMPTZ DEFAULT now(),
        last_seen     TIMESTAMPTZ DEFAULT now(),
        message_count INTEGER DEFAULT 0,
        UNIQUE (app_id, server_id, user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_app_participants
        ON application_participants (server_id, app_id);
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timedelta, timezone
from html import escape

import discord

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("applications.manager")

WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


# ══════════════════════════════════════════════════════════════════════════════
# Storage helpers
# ══════════════════════════════════════════════════════════════════════════════

def save_application(server_id: str, app_id: int, data: dict):
    supabase = get_supabase()
    row = {
        "app_id":            data.get("app_id", app_id),
        "server_id":         data.get("server_id", server_id),
        "creator_id":        data.get("creator_id"),
        "minecraft_name":    data.get("minecraft_name"),
        "status":            data.get("status", "open"),
        "claimed_by":        data.get("claimed_by"),
        "created_at":        data.get("created_at"),
        "closed_at":         data.get("closed_at"),
        "channel_id":        data.get("channel_id"),
        "rejection_reason":  data.get("rejection_reason"),
        "creator_name":      data.get("creator_name"),
        "closed_by":         data.get("closed_by"),
    }
    row = {k: v for k, v in row.items() if v is not None or k in ("claimed_by", "closed_at", "rejection_reason", "closed_by")}

    existing = (
        supabase.table("applications")
        .select("app_id")
        .eq("app_id", app_id)
        .eq("server_id", server_id)
        .execute()
    )
    if existing.data:
        supabase.table("applications").update(row).eq("app_id", app_id).eq("server_id", server_id).execute()
    else:
        supabase.table("applications").insert(row).execute()


def load_application(server_id: str, app_id: int) -> dict | None:
    supabase = get_supabase()
    result = (
        supabase.table("applications")
        .select("*")
        .eq("app_id", app_id)
        .eq("server_id", server_id)
        .execute()
    )
    if not result.data:
        return None
    row = result.data[0]
    row.setdefault("creator_name", "Unbekannt")
    return row


def update_application(server_id: str, app_id: int, fields: dict):
    supabase = get_supabase()
    supabase.table("applications").update(fields).eq("app_id", app_id).eq("server_id", server_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# Message helpers (extended)
# ══════════════════════════════════════════════════════════════════════════════

def load_app_messages(server_id: str, app_id: int) -> list[dict]:
    """Load all messages for an application, ordered by timestamp."""
    supabase = get_supabase()
    result = (
        supabase.table("application_messages")
        .select("*")
        .eq("app_id", app_id)
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
            "attachments":        r.get("attachments") or [],
            "is_deleted":         bool(r.get("is_deleted", False)),
            "deleted_at":         r.get("deleted_at"),
            "edit_history":       r.get("edit_history") or [],
            # legacy keys
            "message": r.get("content", ""),
            "author": {
                "username":    r.get("user_name", "?"),
                "id":          r.get("user_id", ""),
                "global_name": r.get("user_name", "?"),
                "bot":         False,
            },
        })
    return out


def append_app_message(server_id: str, app_id: int, **kwargs):
    """Append a single message to application_messages."""
    supabase = get_supabase()
    supabase.table("application_messages").insert({
        "app_id":             app_id,
        "server_id":          server_id,
        "user_name":          kwargs.get("user", "?"),
        "user_id":            kwargs.get("user_id", ""),
        "content":            kwargs.get("content", ""),
        "attachments":        kwargs.get("attachments") or [],
        "timestamp":          kwargs.get("timestamp") or datetime.now(timezone.utc).isoformat(),
        "discord_message_id": kwargs.get("discord_message_id"),
        "is_deleted":         False,
        "edit_history":       [],
    }).execute()

    # Teilnehmer tracken
    uid = kwargs.get("user_id", "")
    uname = kwargs.get("user", "?")
    if uid:
        _upsert_app_participant(server_id, app_id, uid, uname, action="message")


def mark_app_message_deleted(server_id: str, app_id: int, discord_message_id: str):
    """Markiert eine Bewerbungs-Nachricht als gelöscht."""
    supabase = get_supabase()
    supabase.table("application_messages").update({
        "is_deleted": True,
        "deleted_at": datetime.now(timezone.utc).isoformat(),
    }).eq("server_id", server_id)\
      .eq("app_id", app_id)\
      .eq("discord_message_id", discord_message_id)\
      .execute()


def append_app_message_edit(
    server_id: str,
    app_id: int,
    discord_message_id: str,
    old_content: str,
    new_content: str,
):
    """Fügt einen Edit-Eintrag zur edit_history hinzu und aktualisiert content."""
    supabase = get_supabase()
    result = supabase.table("application_messages")\
        .select("id, edit_history, content")\
        .eq("server_id", server_id)\
        .eq("app_id", app_id)\
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
    supabase.table("application_messages").update({
        "content":      new_content,
        "edit_history": history,
    }).eq("id", row["id"]).execute()


# ══════════════════════════════════════════════════════════════════════════════
# Participants
# ══════════════════════════════════════════════════════════════════════════════

def _upsert_app_participant(
    server_id: str,
    app_id: int,
    user_id: str,
    user_name: str,
    action: str = "message",
    avatar_url: str | None = None,
):
    supabase = get_supabase()
    now = datetime.now(timezone.utc).isoformat()
    try:
        existing = supabase.table("application_participants")\
            .select("id, message_count")\
            .eq("app_id", app_id)\
            .eq("server_id", server_id)\
            .eq("user_id", user_id)\
            .execute()
        if existing.data:
            row = existing.data[0]
            upd: dict = {"last_seen": now}
            if action == "message":
                upd["message_count"] = (row.get("message_count") or 0) + 1
            if avatar_url:
                upd["avatar_url"] = avatar_url
            if user_name:
                upd["user_name"] = user_name
            supabase.table("application_participants").update(upd).eq("id", row["id"]).execute()
        else:
            supabase.table("application_participants").insert({
                "app_id":        app_id,
                "server_id":     server_id,
                "user_id":       user_id,
                "user_name":     user_name,
                "avatar_url":    avatar_url,
                "action":        action,
                "first_seen":    now,
                "last_seen":     now,
                "message_count": 1 if action == "message" else 0,
            }).execute()
    except Exception as e:
        logger.warning(f"[_upsert_app_participant] {e}")


def load_app_participants(server_id: str, app_id: int) -> list[dict]:
    supabase = get_supabase()
    try:
        result = supabase.table("application_participants")\
            .select("*")\
            .eq("app_id", app_id)\
            .eq("server_id", server_id)\
            .order("message_count", desc=True)\
            .execute()
        return result.data or []
    except Exception as e:
        logger.warning(f"[load_app_participants] {e}")
        return []


def add_app_participant_event(
    server_id: str,
    app_id: int,
    user_id: str,
    user_name: str,
    action: str,
    avatar_url: str | None = None,
):
    _upsert_app_participant(server_id, app_id, user_id, user_name, action, avatar_url)


def app_web_url(server_id: str, app_id: int) -> str:
    return f"{WEB_BASE_URL}/dashboard/applications/{app_id}?server_id={server_id}"


# ══════════════════════════════════════════════════════════════════════════════
# Rejection cooldown
# ══════════════════════════════════════════════════════════════════════════════

async def check_rejection_cooldown(
    server_id: str,
    user_id: str,
    cooldown_hours: int,
) -> tuple[bool, timedelta]:
    if cooldown_hours <= 0:
        return False, timedelta(0)

    supabase = get_supabase()
    result = (
        supabase.table("applications")
        .select("closed_at")
        .eq("server_id", server_id)
        .eq("creator_id", user_id)
        .eq("status", "rejected")
        .order("closed_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return False, timedelta(0)

    closed_at_str = result.data[0].get("closed_at")
    if not closed_at_str:
        return False, timedelta(0)

    try:
        closed_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
    except Exception:
        return False, timedelta(0)

    now          = datetime.now(timezone.utc)
    cooldown_end = closed_at + timedelta(hours=cooldown_hours)
    if now < cooldown_end:
        return True, cooldown_end - now
    return False, timedelta(0)


# ══════════════════════════════════════════════════════════════════════════════
# HTML log builder
# ══════════════════════════════════════════════════════════════════════════════

def build_app_log_html(app: dict, messages: list[dict], actor_name: str) -> bytes:
    app_id     = app.get("app_id", "?")
    mc_name    = escape(str(app.get("minecraft_name", "?")))
    creator    = escape(str(app.get("creator_name", "?")))
    status     = app.get("status", "open")
    created_at = str(app.get("created_at", ""))[:19].replace("T", " ")
    reason     = escape(str(app.get("rejection_reason", "")))
    status_label = {"open": "⏳ Offen", "accepted": "✅ Angenommen", "rejected": "❌ Abgelehnt"}.get(status, status)
    status_color = {"open": "#38bdf8", "accepted": "#4ade80", "rejected": "#f87171"}.get(status, "#94a3b8")

    rows = ""
    for msg in messages:
        if msg.get("is_deleted"):
            continue  # Gelöschte Nachrichten im HTML-Log weglassen
        user    = escape(str(msg.get("user", "?")))
        content = escape(str(msg.get("content", "")))
        ts      = str(msg.get("timestamp", ""))[:19].replace("T", " ")
        initials = user[:2].upper()
        rows += f"""
        <div style="display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #1e293b">
          <div style="flex-shrink:0;width:36px;height:36px;border-radius:50%;
            background:linear-gradient(135deg,#38bdf8,#818cf8);
            display:flex;align-items:center;justify-content:center;
            font-weight:700;font-size:.8rem;color:#0f172a">{initials}</div>
          <div style="flex:1">
            <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px">
              <span style="font-weight:600;color:#e2e8f0">{user}</span>
              <span style="font-size:.75rem;color:#475569">{ts}</span>
            </div>
            <div style="color:#cbd5e1;white-space:pre-wrap;word-break:break-word">{content}</div>
          </div>
        </div>"""

    if not rows:
        rows = '<p style="color:#475569;text-align:center;padding:24px 0">Keine Nachrichten aufgezeichnet.</p>'

    rejection_section = ""
    if reason:
        rejection_section = f"""
        <div style="background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);
          border-radius:10px;padding:14px;margin-bottom:20px">
          <div style="font-size:.78rem;color:#f87171;margin-bottom:6px">❌ Ablehnungsgrund</div>
          <div style="color:#fca5a5;white-space:pre-wrap">{reason}</div>
        </div>"""

    return f"""<!doctype html>
<html lang="de">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bewerbung #{app_id} – Log</title>
<style>
  *,*::before,*::after{{box-sizing:border-box}}
  body{{margin:0;font-family:'Segoe UI',Roboto,sans-serif;background:#0f172a;color:#f1f5f9;min-height:100vh;padding:24px}}
  .card{{background:#1e293b;border:1px solid #334155;border-radius:16px;padding:24px;max-width:800px;margin:0 auto}}
</style>
</head>
<body><div class="card">
  <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;padding-bottom:20px;border-bottom:1px solid #334155">
    <div style="font-size:2.2rem">⛏️</div>
    <div>
      <h1 style="margin:0;font-size:1.4rem;color:#e2e8f0">Bewerbung #{app_id} – {mc_name}</h1>
      <div style="margin-top:6px">
        <span style="background:{status_color}22;color:{status_color};border:1px solid {status_color}44;
          padding:2px 10px;border-radius:20px;font-size:.78rem;font-weight:600">{status_label}</span>
      </div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px">
    <div style="background:#0f172a;border-radius:10px;padding:12px">
      <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">⛏️ Minecraft Name</div>
      <div style="font-weight:600">{mc_name}</div>
    </div>
    <div style="background:#0f172a;border-radius:10px;padding:12px">
      <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">👤 Discord</div>
      <div style="font-weight:600">{creator}</div>
    </div>
    <div style="background:#0f172a;border-radius:10px;padding:12px">
      <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">🕐 Eingereicht</div>
      <div style="font-weight:600">{created_at}</div>
    </div>
    <div style="background:#0f172a;border-radius:10px;padding:12px">
      <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">👮 Bearbeitet von</div>
      <div style="font-weight:600">{escape(actor_name)}</div>
    </div>
  </div>
  {rejection_section}
  <h2 style="font-size:1rem;color:#94a3b8;margin:0 0 4px">💬 Gespräch</h2>
  <div>{rows}</div>
  <div style="text-align:center;color:#334155;font-size:.75rem;margin-top:20px">Automatisch generiert</div>
</div></body></html>""".encode("utf-8")


# ══════════════════════════════════════════════════════════════════════════════
# ApplicationManager
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationManager:

    @staticmethod
    async def get_server_config(server_id: str) -> dict | None:
        supabase = get_supabase()
        r = supabase.table("application_servers").select("*").eq("server_id", str(server_id)).execute()
        return r.data[0] if r.data else None

    @staticmethod
    async def get_next_app_id(server_id: str) -> int:
        supabase = get_supabase()
        r = (
            supabase.table("applications")
            .select("app_id")
            .eq("server_id", str(server_id))
            .order("app_id", desc=True)
            .limit(1)
            .execute()
        )
        next_id = (r.data[0]["app_id"] + 1) if r.data else 1
        supabase.table("application_servers").update({"app_counter": next_id}).eq("server_id", str(server_id)).execute()
        return next_id

    @staticmethod
    async def create_application(
        guild:          discord.Guild,
        applicant:      discord.Member,
        minecraft_name: str,
        cfg:            dict,
    ) -> tuple[discord.TextChannel, int]:
        supabase  = get_supabase()
        server_id = str(guild.id)
        app_id    = await ApplicationManager.get_next_app_id(server_id)

        try:
            await applicant.edit(nick=minecraft_name, reason="Bewerbung eingereicht")
        except Exception as e:
            logger.warning(f"[create_application] Nickname-Fehler: {type(e).__name__}: {e}")

        safe_mc      = minecraft_name.lower().replace(" ", "-")[:20]
        channel_name = f"{app_id}-{safe_mc}-bewerbung"

        category_id = cfg.get("category_id")
        category    = guild.get_channel(int(category_id)) if category_id else None

        staff_role_ids = [r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            applicant:          discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for rid in staff_role_ids:
            role = guild.get_role(int(rid))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        try:
            channel = await guild.create_text_channel(
                name=channel_name, category=category, overwrites=overwrites,
                reason=f"Bewerbung #{app_id} von {applicant.display_name}",
            )
        except Exception as e:
            logger.error(f"[create_application] Channel-Erstellung fehlgeschlagen: {type(e).__name__}: {e}")
            raise

        now = datetime.now(timezone.utc).isoformat()
        app_data = {
            "app_id":          app_id,
            "server_id":       server_id,
            "creator_id":      str(applicant.id),
            "creator_name":    applicant.display_name,
            "minecraft_name":  minecraft_name,
            "created_at":      now,
            "status":          "open",
            "claimed_by":      None,
            "channel_id":      str(channel.id),
            "rejection_reason": None,
        }
        save_application(server_id, app_id, app_data)

        # Ersteller als ersten Teilnehmer eintragen
        _upsert_app_participant(
            server_id, app_id,
            str(applicant.id), applicant.display_name,
            action="created",
            avatar_url=str(applicant.display_avatar.url) if applicant.display_avatar else None,
        )

        return channel, app_id

    @staticmethod
    async def accept_application(
        guild:    discord.Guild,
        channel:  discord.TextChannel,
        app:      dict,
        acceptor: discord.Member,
        cfg:      dict,
    ):
        supabase  = get_supabase()
        server_id = app["server_id"]
        app_id    = app["app_id"]
        now       = datetime.now(timezone.utc).isoformat()
        web_url   = app_web_url(server_id, app_id)

        member_role_id = cfg.get("member_role_id")
        newbie_role_id = cfg.get("newbie_role_id")
        applicant      = guild.get_member(int(app["creator_id"]))

        if applicant:
            if member_role_id:
                role = guild.get_role(int(member_role_id))
                if role:
                    try:
                        await applicant.add_roles(role, reason="Bewerbung angenommen")
                    except Exception as e:
                        logger.error(f"[accept] Mitglied-Rolle: {e}")
            if newbie_role_id:
                role = guild.get_role(int(newbie_role_id))
                if role:
                    try:
                        await applicant.remove_roles(role, reason="Bewerbung angenommen")
                    except Exception as e:
                        logger.error(f"[accept] Neulings-Rolle entfernen: {e}")

        update_application(server_id, app_id, {"status": "accepted", "closed_at": now, "closed_by": str(acceptor.id)})

        # Acceptor als Teilnehmer tracken
        _upsert_app_participant(
            server_id, app_id,
            str(acceptor.id), acceptor.display_name,
            action="closed",
            avatar_url=str(acceptor.display_avatar.url) if acceptor.display_avatar else None,
        )

        if applicant:
            try:
                messages   = load_app_messages(server_id, app_id)
                html_bytes = build_app_log_html(app, messages, acceptor.display_name)
                log_file   = discord.File(fp=__import__('io').BytesIO(html_bytes), filename=f"bewerbung-{app_id}-log.html")
                embed = discord.Embed(
                    title="🎉 Deine Bewerbung wurde angenommen!",
                    description=(
                        f"Willkommen im Clan, **{app['minecraft_name']}**! 🎮\n\n"
                        f"Im Anhang findest du das Log eurer Unterhaltung.\n"
                        f"[📊 Im Dashboard ansehen]({web_url})"
                    ),
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text=f"Server: {guild.name} · Angenommen von {acceptor.display_name}")
                await applicant.send(embed=embed, file=log_file)
            except Exception as e:
                logger.warning(f"[accept] DM fehlgeschlagen: {e}")

        try:
            await channel.delete(reason=f"Bewerbung #{app_id} angenommen von {acceptor.display_name}")
        except Exception as e:
            logger.error(f"[accept] Kanal-Löschung: {e}")

    @staticmethod
    async def reject_application(
        guild:    discord.Guild,
        channel:  discord.TextChannel,
        app:      dict,
        rejector: discord.Member,
        reason:   str,
        cfg:      dict,
    ):
        supabase  = get_supabase()
        server_id = app["server_id"]
        app_id    = app["app_id"]
        now       = datetime.now(timezone.utc).isoformat()
        web_url   = app_web_url(server_id, app_id)

        update_application(server_id, app_id, {
            "status":           "rejected",
            "closed_at":        now,
            "closed_by":        str(rejector.id),
            "rejection_reason": reason,
        })

        # Rejector als Teilnehmer tracken
        _upsert_app_participant(
            server_id, app_id,
            str(rejector.id), rejector.display_name,
            action="closed",
            avatar_url=str(rejector.display_avatar.url) if rejector.display_avatar else None,
        )

        applicant      = guild.get_member(int(app["creator_id"]))
        cooldown_hours = int(cfg.get("rejection_cooldown_hours") or 0)
        cooldown_text  = ""
        if cooldown_hours > 0:
            cooldown_end = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
            cooldown_text = (
                f"\n\n⏳ **Wartezeit:** Du kannst dich frühestens am "
                f"<t:{int(cooldown_end.timestamp())}:F> wieder bewerben "
                f"(<t:{int(cooldown_end.timestamp())}:R>)."
            )

        if applicant:
            try:
                messages           = load_app_messages(server_id, app_id)
                app["rejection_reason"] = reason
                html_bytes         = build_app_log_html(app, messages, rejector.display_name)
                log_file           = discord.File(fp=__import__('io').BytesIO(html_bytes), filename=f"bewerbung-{app_id}-log.html")
                embed = discord.Embed(
                    title="❌ Deine Bewerbung wurde abgelehnt",
                    description=(
                        f"Leider wurde deine Bewerbung abgelehnt.\n\n"
                        f"**Begründung:** {reason}"
                        f"{cooldown_text}\n\n"
                        f"Im Anhang findest du das Log eurer Unterhaltung.\n"
                        f"[📊 Im Dashboard ansehen]({web_url})"
                    ),
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                embed.set_footer(text=f"Server: {guild.name} · Abgelehnt von {rejector.display_name}")
                await applicant.send(embed=embed, file=log_file)
            except Exception as e:
                logger.warning(f"[reject] DM fehlgeschlagen: {e}")

        try:
            await channel.delete(reason=f"Bewerbung #{app_id} abgelehnt von {rejector.display_name}")
        except Exception as e:
            logger.error(f"[reject] Kanal-Löschung: {e}")