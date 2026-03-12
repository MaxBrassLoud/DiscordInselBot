"""
tickets/manager.py
==================
Ticket-Lifecycle – Erstellen, Übernehmen, Schließen.
Alle Daten werden in Supabase gespeichert; kein lokales Dateisystem mehr.
"""

from __future__ import annotations

import io
import os
import logging
from datetime import datetime, timezone
from html import escape

import discord

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .storage import save_ticket, update_ticket, append_message, load_messages

logger = get_logger("tickets.manager")

WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


def ticket_web_url(server_id: str, ticket_id: int) -> str:
    return f"{WEB_BASE_URL}/dashboard/tickets/{ticket_id}?server_id={server_id}"


# ── HTML log builder (in-memory, no file write) ───────────────────────────────

def _build_log_html(ticket: dict, messages: list[dict], closer: discord.Member) -> bytes:
    ticket_id   = ticket.get("ticket_id", "?")
    module      = escape(str(ticket.get("module", "?")))
    creator     = escape(str(ticket.get("creator_name", "Unbekannt")))
    description = escape(str(ticket.get("description", "")))
    created_at  = (ticket.get("created_at", "")[:19] or "?").replace("T", " ")
    closed_by   = escape(closer.display_name)

    rows = ""
    for msg in messages:
        user    = escape(str(msg.get("user", "?")))
        content = escape(str(msg.get("content") or msg.get("message", "")))
        ts      = str(msg.get("timestamp", ""))[:19].replace("T", " ")
        initials = user[:2].upper()
        attachments_html = ""
        for url in msg.get("attachments", []):
            safe_url = escape(url)
            attachments_html += (
                f'<div style="margin-top:6px">'
                f'<a href="{safe_url}" style="color:#38bdf8;font-size:.82rem">'
                f'📎 {escape(url.split("/")[-1].split("?")[0])}</a></div>'
            )
        rows += f"""
        <div style="display:flex;gap:12px;padding:12px 0;border-bottom:1px solid #1e293b">
          <div style="flex-shrink:0;width:36px;height:36px;border-radius:50%;
            background:linear-gradient(135deg,#38bdf8,#818cf8);
            display:flex;align-items:center;justify-content:center;
            font-weight:700;font-size:.8rem;color:#0f172a">{initials}</div>
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px">
              <span style="font-weight:600;color:#e2e8f0">{user}</span>
              <span style="font-size:.75rem;color:#475569">{ts}</span>
            </div>
            <div style="color:#cbd5e1;line-height:1.5;white-space:pre-wrap;word-break:break-word">{content}</div>
            {attachments_html}
          </div>
        </div>"""

    if not rows:
        rows = '<p style="color:#475569;text-align:center;padding:24px 0">Keine Nachrichten aufgezeichnet.</p>'

    html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Ticket #{ticket_id} – Log</title>
  <style>
    *,*::before,*::after{{box-sizing:border-box}}
    body{{margin:0;font-family:'Segoe UI',Roboto,sans-serif;
      background:#0f172a;color:#f1f5f9;min-height:100vh;padding:24px}}
    .card{{background:#1e293b;border:1px solid #334155;border-radius:16px;
      padding:24px;max-width:800px;margin:0 auto}}
  </style>
</head>
<body>
  <div class="card">
    <div style="display:flex;align-items:center;gap:14px;margin-bottom:20px;
      padding-bottom:20px;border-bottom:1px solid #334155">
      <div style="font-size:2.2rem">🎫</div>
      <div>
        <h1 style="margin:0;font-size:1.4rem;color:#e2e8f0">Ticket #{ticket_id} – Gesprächs-Log</h1>
        <div style="color:#64748b;font-size:.85rem;margin-top:4px">Modul: {module}</div>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:20px">
      <div style="background:#0f172a;border-radius:10px;padding:12px">
        <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">👤 Ersteller</div>
        <div style="font-weight:600">{creator}</div>
      </div>
      <div style="background:#0f172a;border-radius:10px;padding:12px">
        <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">🕐 Erstellt am</div>
        <div style="font-weight:600">{created_at}</div>
      </div>
      <div style="background:#0f172a;border-radius:10px;padding:12px">
        <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">🔒 Geschlossen von</div>
        <div style="font-weight:600">{closed_by}</div>
      </div>
      <div style="background:#0f172a;border-radius:10px;padding:12px">
        <div style="font-size:.75rem;color:#64748b;margin-bottom:4px">💬 Nachrichten</div>
        <div style="font-weight:600">{len(messages)}</div>
      </div>
    </div>
    <div style="background:#0f172a;border-radius:10px;padding:14px;margin-bottom:20px">
      <div style="font-size:.78rem;color:#64748b;margin-bottom:6px">📝 Ursprüngliche Beschreibung</div>
      <div style="color:#cbd5e1;white-space:pre-wrap">{description}</div>
    </div>
    <h2 style="font-size:1rem;color:#94a3b8;margin:0 0 4px">💬 Nachrichten</h2>
    <div>{rows}</div>
    <div style="text-align:center;color:#334155;font-size:.75rem;margin-top:20px">
      Automatisch generiert beim Schließen des Tickets
    </div>
  </div>
</body>
</html>"""
    return html.encode("utf-8")


# ── TicketManager ─────────────────────────────────────────────────────────────

class TicketManager:

    @staticmethod
    async def get_next_ticket_id(server_id: str) -> int:
        supabase = get_supabase()
        result = (
            supabase.table("ticket_servers")
            .select("ticket_counter")
            .eq("server_id", str(server_id))
            .execute()
        )
        if not result.data:
            return 1
        counter = result.data[0].get("ticket_counter", 0) + 1
        supabase.table("ticket_servers").update({"ticket_counter": counter}).eq("server_id", str(server_id)).execute()
        return counter

    @staticmethod
    async def get_open_tickets_for_user(server_id: str, user_id: str, module_name: str) -> int:
        supabase = get_supabase()
        result = (
            supabase.table("tickets")
            .select("ticket_id")
            .eq("server_id", str(server_id))
            .eq("creator_id", str(user_id))
            .eq("module", module_name)
            .eq("status", "open")
            .execute()
        )
        return len(result.data)

    @staticmethod
    async def get_module(module_id: int) -> dict | None:
        supabase = get_supabase()
        result = supabase.table("ticket_modules").select("*").eq("id", module_id).execute()
        if not result.data:
            return None
        module = result.data[0]
        roles  = supabase.table("ticket_module_roles").select("role_id").eq("module_id", module_id).execute()
        module["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
        return module

    @staticmethod
    async def get_server_config(server_id: str) -> dict | None:
        supabase = get_supabase()
        result = supabase.table("ticket_servers").select("*").eq("server_id", str(server_id)).execute()
        return result.data[0] if result.data else None

    @staticmethod
    async def get_server_modules(server_id: str) -> list[dict]:
        supabase = get_supabase()
        result   = supabase.table("ticket_modules").select("*").eq("server_id", str(server_id)).execute()
        modules  = result.data or []
        for mod in modules:
            roles = supabase.table("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute()
            mod["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
        return modules

    @staticmethod
    async def create_ticket(
        guild:       discord.Guild,
        creator:     discord.Member,
        module:      dict,
        description: str,
        category_id: int,
    ) -> tuple[discord.TextChannel, int]:
        supabase  = get_supabase()
        server_id = str(guild.id)
        ticket_id = await TicketManager.get_next_ticket_id(server_id)

        effective_category_id = category_id
        if module.get("category_id"):
            try:
                effective_category_id = int(module["category_id"])
            except (TypeError, ValueError):
                pass

        safe_user   = creator.display_name.lower().replace(" ", "-")[:15]
        safe_module = module["name"].lower().replace(" ", "-")[:10]
        channel_name = f"{ticket_id}-{safe_user}-{safe_module}"

        category   = guild.get_channel(effective_category_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            creator:            discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for role_id in module.get("staff_role_ids", []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        channel = await guild.create_text_channel(
            name=channel_name, category=category, overwrites=overwrites,
            reason=f"Ticket #{ticket_id} erstellt von {creator.display_name}",
        )

        now = datetime.now(timezone.utc).isoformat()
        ticket_data = {
            "ticket_id":    ticket_id,
            "server_id":    server_id,
            "module":       module["name"],
            "creator_id":   str(creator.id),
            "creator_name": creator.display_name,
            "description":  description,
            "created_at":   now,
            "status":       "open",
            "claimed_by":   None,
            "channel_id":   str(channel.id),
            "added_users":  [],
        }
        # save_ticket does an upsert – the INSERT branch will fire here
        save_ticket(server_id, ticket_id, ticket_data)

        server_cfg = await TicketManager.get_server_config(server_id)
        web_url    = ticket_web_url(server_id, ticket_id)

        # ── Log channel ───────────────────────────────────────────────────────
        if server_cfg and server_cfg.get("log_channel_id"):
            log_channel = guild.get_channel(int(server_cfg["log_channel_id"]))
            if log_channel:
                log_embed = discord.Embed(
                    title=f"🎫 Neues Ticket #{ticket_id}",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )
                log_embed.add_field(name="👤 Ersteller",    value=creator.mention,     inline=True)
                log_embed.add_field(name="📂 Modul",        value=module["name"],       inline=True)
                log_embed.add_field(name="💬 Kanal",        value=channel.mention,      inline=True)
                log_embed.add_field(name="🌐 Web-Link",     value=f"[Dashboard öffnen]({web_url})", inline=False)
                log_embed.add_field(name="📝 Beschreibung", value=description[:500],    inline=False)
                try:
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    logger.error(f"[create_ticket] Log-Kanal: {e}")

        # ── Staff ping channel ────────────────────────────────────────────────
        if server_cfg and server_cfg.get("staff_ping_channel_id"):
            ping_channel = guild.get_channel(int(server_cfg["staff_ping_channel_id"]))
            if ping_channel:
                role_mentions = " ".join(
                    f"<@&{rid}>" for rid in module.get("staff_role_ids", [])
                    if guild.get_role(int(rid))
                )
                ping_embed = discord.Embed(
                    title=f"🔔 Neues Ticket: {module['name']} #{ticket_id}",
                    description=description[:800],
                    color=discord.Color.orange(),
                    timestamp=datetime.now(timezone.utc),
                )
                ping_embed.add_field(name="👤 Ersteller",  value=creator.mention,           inline=True)
                ping_embed.add_field(name="💬 Kanal",      value=channel.mention,            inline=True)
                ping_embed.add_field(name="🌐 Dashboard",  value=f"[Ticket öffnen]({web_url})", inline=False)
                ping_embed.set_footer(text=f"Ticket #{ticket_id}")
                try:
                    await ping_channel.send(content=role_mentions or None, embed=ping_embed)
                except Exception as e:
                    logger.error(f"[create_ticket] Staff-Ping: {e}")

        return channel, ticket_id

    @staticmethod
    async def close_ticket(
        guild:   discord.Guild,
        channel: discord.TextChannel,
        ticket:  dict,
        closer:  discord.Member,
    ):
        supabase  = get_supabase()
        server_id = ticket["server_id"]
        ticket_id = ticket["ticket_id"]
        now       = datetime.now(timezone.utc).isoformat()
        web_url   = ticket_web_url(server_id, ticket_id)

        messages = load_messages(server_id, ticket_id)

        update_ticket(server_id, ticket_id, {
            "status":    "closed",
            "closed_at": now,
            "closed_by": str(closer.id),
        })

        # ── DM an Ticket-Ersteller ────────────────────────────────────────────
        creator_id = ticket.get("creator_id")
        if creator_id:
            try:
                creator = guild.get_member(int(creator_id))
                if creator:
                    html_bytes = _build_log_html(ticket, messages, closer)
                    log_file   = discord.File(fp=io.BytesIO(html_bytes), filename=f"ticket-{ticket_id}-log.html")
                    creator_embed = discord.Embed(
                        title=f"🎫 Dein Ticket #{ticket_id} wurde geschlossen",
                        description=(
                            "Im Anhang findest du das vollständige Gesprächs-Log.\n\n"
                            f"[📊 Im Dashboard ansehen]({web_url})"
                        ),
                        color=discord.Color.blurple(),
                        timestamp=datetime.now(timezone.utc),
                    )
                    creator_embed.add_field(name="📂 Modul",           value=ticket.get("module", "?"),    inline=True)
                    creator_embed.add_field(name="🔒 Geschlossen von", value=closer.display_name,          inline=True)
                    creator_embed.add_field(name="💬 Nachrichten",     value=str(len(messages)),            inline=True)
                    creator_embed.set_footer(text=f"Server: {guild.name}")
                    await creator.send(embed=creator_embed, file=log_file)
            except Exception as e:
                logger.warning(f"[close_ticket] DM fehlgeschlagen: {e}")

        # ── Log channel ───────────────────────────────────────────────────────
        server_cfg = await TicketManager.get_server_config(server_id)
        if server_cfg and server_cfg.get("log_channel_id"):
            log_channel = guild.get_channel(int(server_cfg["log_channel_id"]))
            if log_channel:
                close_embed = discord.Embed(
                    title=f"🔒 Ticket #{ticket_id} geschlossen",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                close_embed.add_field(name="🔒 Geschlossen von", value=closer.mention,         inline=True)
                close_embed.add_field(name="📂 Modul",           value=ticket.get("module","?"), inline=True)
                close_embed.add_field(name="💬 Nachrichten",     value=str(len(messages)),       inline=True)
                close_embed.add_field(name="🌐 Web-Link",        value=f"[Dashboard]({web_url})", inline=False)
                try:
                    await log_channel.send(embed=close_embed)
                except Exception as e:
                    logger.error(f"[close_ticket] Log-Kanal: {e}")

        try:
            await channel.delete(reason=f"Ticket #{ticket_id} geschlossen von {closer.display_name}")
        except Exception as e:
            logger.error(f"[close_ticket] Kanal-Löschung: {e}")

        logger.info(f"Ticket #{ticket_id} geschlossen von {closer.display_name}")