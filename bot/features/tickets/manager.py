import discord
from datetime import datetime, timezone
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .storage import save_ticket, update_ticket, append_message, load_messages
from .html_export import save_html_export

logger = get_logger("tickets.manager")


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
        result = supabase.table("tickets").select("ticket_id")\
            .eq("server_id", str(server_id))\
            .eq("creator_id", str(user_id))\
            .eq("module", module_name)\
            .eq("status", "open").execute()
        return len(result.data)

    @staticmethod
    async def get_module(module_id: int) -> dict | None:
        supabase = get_supabase()
        result   = supabase.table("ticket_modules").select("*").eq("id", module_id).execute()
        if not result.data:
            return None
        module = result.data[0]
        roles  = supabase.table("ticket_module_roles").select("role_id").eq("module_id", module_id).execute()
        module["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
        return module

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
        guild: discord.Guild,
        creator: discord.Member,
        module: dict,
        description: str,
        category_id: int,
    ) -> tuple[discord.TextChannel, int]:
        """Creates ticket channel, saves to DB and local storage. Returns (channel, ticket_id)."""
        supabase  = get_supabase()
        server_id = str(guild.id)
        ticket_id = await TicketManager.get_next_ticket_id(server_id)

        # ── Kanal-Name ────────────────────────────────────────────────────────
        safe_user   = creator.display_name.lower().replace(" ", "-")[:15]
        safe_module = module["name"].lower().replace(" ", "-")[:10]
        channel_name = f"{ticket_id}-{safe_user}-{safe_module}"

        # ── Berechtigungen ────────────────────────────────────────────────────
        category = guild.get_channel(category_id)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            creator: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
        }
        for role_id in module.get("staff_role_ids", []):
            role = guild.get_role(int(role_id))
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)

        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Ticket #{ticket_id} erstellt von {creator.display_name}",
        )

        # ── Lokale Speicherung ────────────────────────────────────────────────
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
        }
        save_ticket(server_id, ticket_id, ticket_data)

        # ── Supabase Metadaten ────────────────────────────────────────────────
        supabase.table("tickets").insert({
            "ticket_id":  ticket_id,
            "server_id":  server_id,
            "module":     module["name"],
            "creator_id": str(creator.id),
            "claimed_by": None,
            "status":     "open",
            "created_at": now,
            "closed_at":  None,
            "channel_id": str(channel.id),
        }).execute()

        return channel, ticket_id

    @staticmethod
    async def close_ticket(
        guild: discord.Guild,
        channel: discord.TextChannel,
        ticket: dict,
        closer: discord.Member,
    ):
        """Closes ticket: exports HTML, updates DB, deletes channel."""
        supabase  = get_supabase()
        server_id = ticket["server_id"]
        ticket_id = ticket["ticket_id"]
        now       = datetime.now(timezone.utc).isoformat()

        # ── HTML Export ───────────────────────────────────────────────────────
        messages = load_messages(server_id, ticket_id)
        save_html_export(server_id, ticket_id, ticket, messages)

        # ── Lokale Speicherung aktualisieren ──────────────────────────────────
        update_ticket(server_id, ticket_id, {"status": "closed", "closed_at": now, "closed_by": str(closer.id)})

        # ── Supabase aktualisieren ────────────────────────────────────────────
        supabase.table("tickets").update({"status": "closed", "closed_at": now})\
            .eq("ticket_id", ticket_id).eq("server_id", server_id).execute()

        # ── Kanal löschen ─────────────────────────────────────────────────────
        try:
            await channel.delete(reason=f"Ticket #{ticket_id} geschlossen von {closer.display_name}")
        except Exception as e:
            logger.error(f"[close_ticket] Kanal-Löschung fehlgeschlagen: {e}")

        logger.info(f"Ticket #{ticket_id} geschlossen von {closer.display_name}")