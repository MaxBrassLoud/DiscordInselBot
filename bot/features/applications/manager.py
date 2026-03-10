"""
applications/manager.py
========================
Handles creation, storage and closing of member applications.
Applications are stored in Supabase (applications table) and
locally in data/applications/{server_id}/{app_id}.json
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("applications.manager")

WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")
_DATA_DIR = Path("data/applications")


# ══════════════════════════════════════════════════════════════════════════════
# Storage helpers
# ══════════════════════════════════════════════════════════════════════════════

def _app_path(server_id: str, app_id: int) -> Path:
    p = _DATA_DIR / str(server_id)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{app_id}.json"


def save_application(server_id: str, app_id: int, data: dict):
    _app_path(server_id, app_id).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_application(server_id: str, app_id: int) -> dict | None:
    p = _app_path(server_id, app_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_application(server_id: str, app_id: int, fields: dict):
    data = load_application(server_id, app_id) or {}
    data.update(fields)
    save_application(server_id, app_id, data)


def load_app_messages(server_id: str, app_id: int) -> list[dict]:
    p = _DATA_DIR / str(server_id) / f"{app_id}_messages.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []


def append_app_message(server_id: str, app_id: int, **kwargs):
    msgs = load_app_messages(server_id, app_id)
    kwargs.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    msgs.append(kwargs)
    p = _DATA_DIR / str(server_id) / f"{app_id}_messages.json"
    p.write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding="utf-8")


def app_web_url(server_id: str, app_id: int) -> str:
    return f"{WEB_BASE_URL}/dashboard/applications/{app_id}?server_id={server_id}"


# ══════════════════════════════════════════════════════════════════════════════
# HTML Log Builder
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
        """Atomically increment app_counter for the server and return the new value.

        Uses the actual MAX(app_id) from the applications table as source of truth
        to avoid race conditions from concurrent reads.
        """
        supabase = get_supabase()
        # Use MAX from existing applications as source of truth (race-condition safe)
        r = supabase.table("applications")\
            .select("app_id")\
            .eq("server_id", str(server_id))\
            .order("app_id", desc=True)\
            .limit(1)\
            .execute()
        next_id = (r.data[0]["app_id"] + 1) if r.data else 1
        # Keep app_counter in sync for reference
        supabase.table("application_servers")\
            .update({"app_counter": next_id})\
            .eq("server_id", str(server_id))\
            .execute()
        return next_id

    @staticmethod
    async def create_application(
        guild: discord.Guild,
        applicant: discord.Member,
        minecraft_name: str,
        cfg: dict,
    ) -> tuple[discord.TextChannel, int]:
        """
        Creates an application channel, renames applicant to their MC name,
        saves to DB and local storage.
        """
        supabase  = get_supabase()
        server_id = str(guild.id)
        app_id    = await ApplicationManager.get_next_app_id(server_id)

        # ── Rename applicant to Minecraft name ────────────────────────────────
        try:
            await applicant.edit(nick=minecraft_name, reason="Bewerbung eingereicht")
        except discord.Forbidden:
            logger.warning(f"[create_application] Konnte Nickname nicht setzen für {applicant}")

        # ── Channel name (same schema as tickets) ─────────────────────────────
        safe_mc   = minecraft_name.lower().replace(" ", "-")[:20]
        channel_name = f"{app_id}-{safe_mc}-bewerbung"

        # ── Category + Permissions ────────────────────────────────────────────
        category_id = cfg.get("category_id")
        category    = guild.get_channel(int(category_id)) if category_id else None

        # Staff roles from config
        staff_role_ids = [r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()]

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            applicant: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_roles=True),
        }
        for rid in staff_role_ids:
            role = guild.get_role(int(rid))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Bewerbung #{app_id} von {applicant.display_name}",
        )

        # ── Save locally ──────────────────────────────────────────────────────
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

        # ── Save to Supabase ──────────────────────────────────────────────────
        supabase.table("applications").insert({
            "app_id":         app_id,
            "server_id":      server_id,
            "creator_id":     str(applicant.id),
            "minecraft_name": minecraft_name,
            "status":         "open",
            "claimed_by":     None,
            "created_at":     now,
            "closed_at":      None,
            "channel_id":     str(channel.id),
        }).execute()

        return channel, app_id

    @staticmethod
    async def accept_application(
        guild: discord.Guild,
        channel: discord.TextChannel,
        app: dict,
        acceptor: discord.Member,
        cfg: dict,
    ):
        """Grant member role, DM applicant, delete channel."""
        supabase  = get_supabase()
        server_id = app["server_id"]
        app_id    = app["app_id"]
        now       = datetime.now(timezone.utc).isoformat()
        web_url   = app_web_url(server_id, app_id)

        # Grant member role, remove newbie role
        member_role_id = cfg.get("member_role_id")
        newbie_role_id = cfg.get("newbie_role_id")
        applicant = guild.get_member(int(app["creator_id"]))

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
        supabase.table("applications").update({"status": "accepted", "closed_at": now})\
            .eq("app_id", app_id).eq("server_id", server_id).execute()

        # DM to applicant
        if applicant:
            try:
                messages  = load_app_messages(server_id, app_id)
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
        guild: discord.Guild,
        channel: discord.TextChannel,
        app: dict,
        rejector: discord.Member,
        reason: str,
        cfg: dict,
    ):
        """DM applicant with rejection reason + log, delete channel."""
        supabase  = get_supabase()
        server_id = app["server_id"]
        app_id    = app["app_id"]
        now       = datetime.now(timezone.utc).isoformat()
        web_url   = app_web_url(server_id, app_id)

        update_application(server_id, app_id, {
            "status": "rejected", "closed_at": now,
            "closed_by": str(rejector.id), "rejection_reason": reason,
        })
        supabase.table("applications").update({
            "status": "rejected", "closed_at": now, "rejection_reason": reason,
        }).eq("app_id", app_id).eq("server_id", server_id).execute()

        applicant = guild.get_member(int(app["creator_id"]))
        if applicant:
            try:
                messages  = load_app_messages(server_id, app_id)
                app["rejection_reason"] = reason   # include in HTML
                html_bytes = build_app_log_html(app, messages, rejector.display_name)
                log_file   = discord.File(fp=io.BytesIO(html_bytes), filename=f"bewerbung-{app_id}-log.html")
                embed = discord.Embed(
                    title="❌ Deine Bewerbung wurde abgelehnt",
                    description=(
                        f"Leider wurde deine Bewerbung abgelehnt.\n\n"
                        f"**Begründung:** {reason}\n\n"
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