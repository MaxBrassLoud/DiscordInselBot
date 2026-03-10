"""applications/cog.py – Discord Cog for the application system."""

import discord
from discord.ext import commands
from discord import app_commands

from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .views import ApplicationSetupView, ApplicationEditView, ApplicationPanelView, ApplicationChannelView
from .manager import ApplicationManager, load_application

logger = get_logger("applications")


class ApplicationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        await self._restore_panel_view()
        await self._restore_channel_views()

    async def _restore_panel_view(self):
        # Register exactly ONE persistent panel view – discord.py routes by custom_id,
        # so a single registration is enough for all servers.
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
        """Assign newbie role automatically when a user joins the server."""
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
                logger.info(f"[on_member_join] Neulings-Rolle vergeben an {member}")
        except Exception as e:
            logger.error(f"[on_member_join] {e}")

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
            from .manager import append_app_message
            append_app_message(
                server_id=server_id, app_id=app_id,
                user=message.author.display_name,
                user_id=str(message.author.id),
                content=message.content or "",
                attachments=[a.url for a in message.attachments],
            )
        except Exception:
            pass

    @app_commands.command(name="bewerbung_setup", description="Richte das Bewerbungs-System ein")
    async def bewerbung_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = ApplicationSetupView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="bewerbung_bearbeiten", description="Bearbeite das Bewerbungs-System")
    async def bewerbung_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        from bot.core.supabase_client import get_supabase
        if not get_supabase().table("application_servers").select("server_id")\
                .eq("server_id", str(interaction.guild_id)).execute().data:
            await interaction.response.send_message(
                "❌ Noch nicht eingerichtet. Nutze zuerst `/bewerbung_setup`.", ephemeral=True)
            return
        view = ApplicationEditView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ApplicationsCog(bot))