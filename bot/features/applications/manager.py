"""
applications/manager.py
========================
Bewerbungs-Lifecycle – Erstellen, Annehmen, Ablehnen.
Alle Daten werden in Supabase gespeichert; kein lokales Dateisystem mehr.

Supabase-Tabellen:
  applications         – Bewerbungs-Metadaten (bereits vorhanden)
  application_messages – Nachrichten / Kommentare

SQL für application_messages (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS application_messages (
        id          BIGSERIAL PRIMARY KEY,
        app_id      INTEGER NOT NULL,
        server_id   TEXT    NOT NULL,
        user_name   TEXT,
        user_id     TEXT,
        content     TEXT,
        attachments JSONB   DEFAULT '[]',
        timestamp   TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_app_messages_app
        ON application_messages (server_id, app_id);
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
    """Upsert application metadata into Supabase."""
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
        # extra fields
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
    """Load application metadata from Supabase."""
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
    """Partial update of application columns."""
    supabase = get_supabase()
    supabase.table("applications").update(fields).eq("app_id", app_id).eq("server_id", server_id).execute()


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
            "timestamp":   r.get("timestamp", ""),
            "user":        r.get("user_name", "?"),
            "user_id":     r.get("user_id", ""),
            "content":     r.get("content", ""),
            "attachments": r.get("attachments") or [],
            # legacy keys
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
        "app_id":      app_id,
        "server_id":   server_id,
        "user_name":   kwargs.get("user", "?"),
        "user_id":     kwargs.get("user_id", ""),
        "content":     kwargs.get("content", ""),
        "attachments": kwargs.get("attachments") or [],
        "timestamp":   kwargs.get("timestamp") or datetime.now(timezone.utc).isoformat(),
    }).execute()


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
# HTML log builder (in-memory)
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
        except discord.Forbidden:
            logger.warning(f"[create_application] Konnte Nickname nicht setzen für {applicant}")

        safe_mc      = minecraft_name.lower().replace(" ", "-")[:20]
        channel_name = f"{app_id}-{safe_mc}-bewerbung"

        category_id = cfg.get("category_id")
        category    = guild.get_channel(int(category_id)) if category_id else None

        staff_role_ids = [r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()]
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            applicant:          discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_roles=True),
        }
        for rid in staff_role_ids:
            role = guild.get_role(int(rid))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name, category=category, overwrites=overwrites,
            reason=f"Bewerbung #{app_id} von {applicant.display_name}",
        )

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

        if applicant:
            try:
                messages   = load_app_messages(server_id, app_id)
                html_bytes = build_app_log_html(app, messages, acceptor.display_name)
                log_file   = discord.File(fp=io.BytesIO(html_bytes), filename=f"bewerbung-{app_id}-log.html")
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
                log_file           = discord.File(fp=io.BytesIO(html_bytes), filename=f"bewerbung-{app_id}-log.html")
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