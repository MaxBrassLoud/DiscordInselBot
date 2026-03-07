import discord
from datetime import datetime, timezone
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .storage import save_ticket, update_ticket, append_message, load_messages
from .html_export import save_html_export

logger = get_logger("tickets.manager")

import os

WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


def ticket_web_url(server_id: str, ticket_id: int) -> str:
    return f"{WEB_BASE_URL}/dashboard/tickets/{ticket_id}?server_id={server_id}"


class TicketManager:
    """Handles all ticket creation and lifecycle operations."""

    @staticmethod
    async def get_next_ticket_id(server_id: str) -> int:
        supabase = get_supabase()
        result = supabase.table("ticket_servers").select("ticket_counter").eq("server_id", str(server_id)).execute()
        if not result.data:
            return 1
        counter = result.data[0].get("ticket_counter", 0) + 1
        supabase.table("ticket_servers").update({"ticket_counter": counter}).eq("server_id", str(server_id)).execute()
        return counter

    @staticmethod
    async def get_open_tickets_for_user(server_id: str, user_id: str, module_name: str) -> int:
        supabase = get_supabase()
        result = supabase.table("tickets").select("ticket_id") \
            .eq("server_id", str(server_id)) \
            .eq("creator_id", str(user_id)) \
            .eq("module", module_name) \
            .eq("status", "open").execute()
        return len(result.data)

    @staticmethod
    async def get_module(module_id: int) -> dict | None:
        supabase = get_supabase()
        result = supabase.table("ticket_modules").select("*").eq("id", module_id).execute()
        if not result.data:
            return None
        module = result.data[0]
        roles = supabase.table("ticket_module_roles").select("role_id").eq("module_id", module_id).execute()
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
        result = supabase.table("ticket_modules").select("*").eq("server_id", str(server_id)).execute()
        modules = result.data or []
        for mod in modules:
            roles = supabase.table("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute()
            mod["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
        return modules

    @staticmethod
    async def create_ticket(
            guild: discord.Guild,
            creator: discord.Member,
            module: dict,
            description: str,
            category_id: int,
    ) -> tuple[discord.TextChannel, int]:
        """
        Creates ticket channel, saves to DB and local storage.

        category_id is the global fallback. If the module itself has a
        'category_id' field set (per-module override), that takes priority.
        """
        supabase = get_supabase()
        server_id = str(guild.id)
        ticket_id = await TicketManager.get_next_ticket_id(server_id)

        # ── Per-module category override ───────────────────────────────────────
        effective_category_id = category_id
        if module.get("category_id"):
            try:
                effective_category_id = int(module["category_id"])
            except (TypeError, ValueError):
                pass  # fall back to global

        # ── Kanal-Name ────────────────────────────────────────────────────────
        safe_user = creator.display_name.lower().replace(" ", "-")[:15]
        safe_module = module["name"].lower().replace(" ", "-")[:10]
        channel_name = f"{ticket_id}-{safe_user}-{safe_module}"

        # ── Berechtigungen ────────────────────────────────────────────────────
        category = guild.get_channel(effective_category_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for role_id in module.get("staff_role_ids", []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket #{ticket_id} erstellt von {creator.display_name}",
        )

        # ── Lokale Speicherung ────────────────────────────────────────────────
        now = datetime.now(timezone.utc).isoformat()
        ticket_data = {
            "ticket_id": ticket_id,
            "server_id": server_id,
            "module": module["name"],
            "creator_id": str(creator.id),
            "creator_name": creator.display_name,
            "description": description,
            "created_at": now,
            "status": "open",
            "claimed_by": None,
            "channel_id": str(channel.id),
        }
        save_ticket(server_id, ticket_id, ticket_data)

        # ── Supabase Metadaten ────────────────────────────────────────────────
        supabase.table("tickets").insert({
            "ticket_id": ticket_id,
            "server_id": server_id,
            "module": module["name"],
            "creator_id": str(creator.id),
            "claimed_by": None,
            "status": "open",
            "created_at": now,
            "closed_at": None,
            "channel_id": str(channel.id),
        }).execute()

        # ── Log-Kanal ─────────────────────────────────────────────────────────
        server_cfg = await TicketManager.get_server_config(server_id)
        web_url = ticket_web_url(server_id, ticket_id)

        if server_cfg and server_cfg.get("log_channel_id"):
            log_channel = guild.get_channel(int(server_cfg["log_channel_id"]))
            if log_channel:
                log_embed = discord.Embed(
                    title=f"🎫 Neues Ticket #{ticket_id}",
                    color=discord.Color.blurple(),
                    timestamp=datetime.now(timezone.utc),
                )
                log_embed.add_field(name="👤 Ersteller", value=creator.mention, inline=True)
                log_embed.add_field(name="📂 Modul", value=module["name"], inline=True)
                log_embed.add_field(name="💬 Kanal", value=channel.mention, inline=True)
                log_embed.add_field(name="🌐 Web-Link", value=f"[Dashboard öffnen]({web_url})", inline=False)
                log_embed.add_field(name="📝 Beschreibung", value=description[:500], inline=False)
                try:
                    await log_channel.send(embed=log_embed)
                except Exception as e:
                    logger.error(f"[create_ticket] Log-Kanal Fehler: {e}")

        # ── Staff-Ping Kanal ──────────────────────────────────────────────────
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
                ping_embed.add_field(name="👤 Ersteller", value=creator.mention, inline=True)
                ping_embed.add_field(name="💬 Kanal", value=channel.mention, inline=True)
                ping_embed.add_field(name="🌐 Dashboard", value=f"[Ticket öffnen]({web_url})", inline=False)
                ping_embed.set_footer(text=f"Ticket #{ticket_id}")
                try:
                    content = role_mentions if role_mentions else None
                    await ping_channel.send(content=content, embed=ping_embed)
                except Exception as e:
                    logger.error(f"[create_ticket] Staff-Ping Fehler: {e}")

        return channel, ticket_id

    @staticmethod
    async def close_ticket(
            guild: discord.Guild,
            channel: discord.TextChannel,
            ticket: dict,
            closer: discord.Member,
    ):
        """Closes ticket: exports HTML, updates DB, sends DM with link, deletes channel."""
        supabase = get_supabase()
        server_id = ticket["server_id"]
        ticket_id = ticket["ticket_id"]
        now = datetime.now(timezone.utc).isoformat()
        web_url = ticket_web_url(server_id, ticket_id)

        messages = load_messages(server_id, ticket_id)
        save_html_export(server_id, ticket_id, ticket, messages)

        update_ticket(server_id, ticket_id, {"status": "closed", "closed_at": now, "closed_by": str(closer.id)})

        supabase.table("tickets").update({"status": "closed", "closed_at": now}) \
            .eq("ticket_id", ticket_id).eq("server_id", server_id).execute()

        dm_embed = discord.Embed(
            title=f"🎫 Ticket #{ticket_id} geschlossen",
            description="Dein Ticket wurde geschlossen. Du kannst den Verlauf im Dashboard einsehen.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        dm_embed.add_field(name="📂 Modul", value=ticket.get("module", "?"), inline=True)
        dm_embed.add_field(name="🔒 Geschlossen von", value=closer.display_name, inline=True)
        dm_embed.add_field(name="🌐 Web-Link", value=f"[Dashboard öffnen]({web_url})", inline=False)

        creator_id = ticket.get("creator_id")
        if creator_id:
            try:
                creator = guild.get_member(int(creator_id))
                if creator and creator.id != closer.id:
                    await creator.send(embed=dm_embed)
            except Exception as e:
                logger.warning(f"[close_ticket] DM an Ersteller fehlgeschlagen: {e}")

        try:
            await closer.send(embed=dm_embed)
        except Exception as e:
            logger.warning(f"[close_ticket] DM an Schließer fehlgeschlagen: {e}")

        server_cfg = await TicketManager.get_server_config(server_id)
        if server_cfg and server_cfg.get("log_channel_id"):
            log_channel = guild.get_channel(int(server_cfg["log_channel_id"]))
            if log_channel:
                close_embed = discord.Embed(
                    title=f"🔒 Ticket #{ticket_id} geschlossen",
                    color=discord.Color.red(),
                    timestamp=datetime.now(timezone.utc),
                )
                close_embed.add_field(name="🔒 Geschlossen von", value=closer.mention, inline=True)
                close_embed.add_field(name="📂 Modul", value=ticket.get("module", "?"), inline=True)
                close_embed.add_field(name="🌐 Web-Link", value=f"[Dashboard öffnen]({web_url})", inline=False)
                try:
                    await log_channel.send(embed=close_embed)
                except Exception as e:
                    logger.error(f"[close_ticket] Log-Kanal Fehler: {e}")

        try:
            await channel.delete(reason=f"Ticket #{ticket_id} geschlossen von {closer.display_name}")
        except Exception as e:
            logger.error(f"[close_ticket] Kanal-Löschung fehlgeschlagen: {e}")

        logger.info(f"Ticket #{ticket_id} geschlossen von {closer.display_name}")