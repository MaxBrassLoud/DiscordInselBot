"""applications/cog.py – Discord Cog for the application system (EXTENDED)."""

import discord
from discord.ext import commands
from discord import app_commands

from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .views import ApplicationSetupView, ApplicationEditView, ApplicationPanelView, ApplicationChannelView
from .manager import (
    ApplicationManager, load_application,
    mark_app_message_deleted, append_app_message_edit,
    append_app_message,
)

logger = get_logger("applications")


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_panel_view()
        await self._restore_channel_views()

    async def _restore_panel_view(self):
        self.bot.add_view(ApplicationPanelView(bot=self.bot))
        logger.info("✅ ApplicationPanelView wiederhergestellt")

    async def _restore_channel_views(self):
        from bot.core.supabase_client import get_supabase
        try:
            supabase = get_supabase()
            apps = supabase.table("applications").select("*").eq("status", "open").execute().data or []
            count = 0
            for app in apps:
                cfg_r = supabase.table("application_servers")\
                    .select("*").eq("server_id", app["server_id"]).execute()
                cfg = cfg_r.data[0] if cfg_r.data else {}
                local = load_application(app["server_id"], app["app_id"])
                view = ApplicationChannelView(
                    app_id=app["app_id"], server_id=app["server_id"],
                    applicant_id=app["creator_id"], cfg=cfg, bot=self.bot,
                )
                if local and local.get("claimed_by"):
                    view._claimed_by = local["claimed_by"]
                self.bot.add_view(view)
                count += 1
            logger.info(f"✅ {count} ApplicationChannelView(s) wiederhergestellt")
        except Exception as e:
            logger.error(f"[_restore_channel_views] {e}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        from bot.core.supabase_client import get_supabase
        try:
            supabase  = get_supabase()
            server_id = str(member.guild.id)
            cfg_r = supabase.table("application_servers").select("newbie_role_id")\
                .eq("server_id", server_id).execute()
            if not cfg_r.data:
                return
            newbie_role_id = cfg_r.data[0].get("newbie_role_id")
            if not newbie_role_id:
                return
            role = member.guild.get_role(int(newbie_role_id))
            if role:
                await member.add_roles(role, reason="Automatisch beim Beitreten vergeben")
        except Exception as e:
            logger.error(f"[on_member_join] {e}")

    # ── Message Logging ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Log messages in application channels."""
        if message.author.bot or not message.guild:
            return
        try:
            parts = message.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(message.guild.id)
            append_app_message(
                server_id=server_id, app_id=app_id,
                user=message.author.display_name,
                user_id=str(message.author.id),
                content=message.content or "",
                attachments=[a.url for a in message.attachments],
                discord_message_id=str(message.id),
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        """Track message edits in application channels."""
        if after.author.bot or not after.guild:
            return
        if before.content == after.content:
            return
        try:
            parts = after.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(after.guild.id)
            append_app_message_edit(
                server_id=server_id,
                app_id=app_id,
                discord_message_id=str(after.id),
                old_content=before.content or "",
                new_content=after.content or "",
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        """Track message deletions in application channels."""
        if message.author.bot or not message.guild:
            return
        try:
            parts = message.channel.name.split("-")
            if len(parts) < 3 or not parts[0].isdigit() or parts[-1] != "bewerbung":
                return
            app_id    = int(parts[0])
            server_id = str(message.guild.id)
            mark_app_message_deleted(
                server_id=server_id,
                app_id=app_id,
                discord_message_id=str(message.id),
            )
        except Exception:
            pass

    # ── Commands ──────────────────────────────────────────────────────────────

    bewerbung = app_commands.Group(
        name="bewerbung",
        description="Bewerbungs System",
    )

    @bewerbung.command(name="setup", description="Richte das Bewerbungs-System ein")
    async def bewerbung_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        from .setup_wizard import start_application_wizard

        # Bestehende Konfiguration laden (falls vorhanden)
        from bot.core.supabase_client import get_supabase
        sb = get_supabase()
        existing = sb.table("application_servers").select("*").eq("server_id", str(interaction.guild_id)).execute()
        existing_config = existing.data[0] if existing.data else None

        await start_application_wizard(
            interaction=interaction,
            bot=self.bot,
            existing_config=existing_config,
        )

    @bewerbung.command(name="bearbeiten", description="Bearbeite das Bewerbungs-System")
    async def bewerbung_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        from bot.core.supabase_client import get_supabase
        from bot.features.applications.app_edit_views import AppEditMainView
        import os

        sb = get_supabase()
        if not sb.table("application_servers").select("server_id") \
                .eq("server_id", str(interaction.guild_id)).execute().data:
            await interaction.response.send_message(
                "❌ Das Bewerbungs-System ist noch nicht eingerichtet.\n"
                "Nutze zuerst `/bewerbung_setup`.", ephemeral=True
            )
            return

        web_base = os.getenv("WEB_BASE_URL", "http://localhost:5000")

        view = AppEditMainView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction

        embed = view.build_embed()
        embed.add_field(
            name="🌐 Web-Dashboard",
            value=(
                f"[→ Bewerbungs-Setup im Browser öffnen]"
                f"({web_base}/dashboard/setup/applications?server_id={interaction.guild_id})"
            ),
            inline=False,
        )

        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )



async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))