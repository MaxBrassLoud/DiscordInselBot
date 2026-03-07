import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timedelta, timezone

from bot.core.settings import get_settings, upsert_settings
from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

from .views import SetupSpielabendView, SpielabendView
from .modal import SpielabendModal

logger = get_logger("spieleabend")


class SpielabendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_reminders.start()

    def cog_unload(self):
        self.check_reminders.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(SpielabendView())
        logger.info("✅ SpielabendView registriert")

    @app_commands.command(name="setup_spieleabend", description="Konfiguriere den Spieleabend Bot")
    async def setup_spieleabend(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        embed = discord.Embed(
            title="⚙️ Spieleabend Bot Setup",
            description="Wähle die Einstellungen für den Spieleabend Bot aus:",
            color=discord.Color.blue()
        )
        embed.add_field(name="🔔 Ping Rolle",    value="*Nicht ausgewählt*", inline=False)
        embed.add_field(name="📢 Kanal",         value="*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🗑️ Lösch-Rollen", value="*Nicht ausgewählt*", inline=False)
        embed.set_footer(text="Wähle alle Optionen aus und klicke dann auf Speichern")
        await interaction.response.send_message(embed=embed, view=SetupSpielabendView(interaction.guild_id), ephemeral=True)

    @app_commands.command(name="spieleabend", description="Erstelle einen neuen Spieleabend")
    async def spieleabend(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SpielabendModal(self.bot))

    @app_commands.command(name="spieleabend_loeschen", description="Lösche einen Spieleabend")
    @app_commands.describe(spieleabend_id="Die ID des Spieleabends")
    async def spieleabend_loeschen(self, interaction: discord.Interaction, spieleabend_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            config = await get_settings(str(interaction.guild_id))
            if not config:
                await interaction.followup.send("❌ Keine Einstellungen gefunden!")
                return
            delete_role_ids = config.get("delete_role_ids", "").split(",")
            user_role_ids   = [str(r.id) for r in interaction.user.roles]
            has_permission  = (
                any(rid in delete_role_ids for rid in user_role_ids)
                or interaction.user.guild_permissions.administrator
            )
            result = supabase.table("game_nights").select("*").eq("id", spieleabend_id).execute()
            if not result.data:
                await interaction.followup.send("❌ Spieleabend nicht gefunden!")
                return
            game_night = result.data[0]
            if str(interaction.user.id) == game_night["creator_id"]:
                has_permission = True
            if not has_permission:
                await interaction.followup.send("❌ Keine Berechtigung!")
                return
            try:
                channel = self.bot.get_channel(int(config["channel_id"]))
                message = await channel.fetch_message(int(game_night["message_id"]))
                await message.delete()
                thread = await self.bot.fetch_channel(int(game_night["thread_id"]))
                await thread.delete()
            except Exception:
                pass
            supabase.table("game_nights").delete().eq("id", spieleabend_id).execute()
            await interaction.followup.send("✅ Spieleabend gelöscht!")
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}")

    @tasks.loop(minutes=1)
    async def check_reminders(self):
        try:
            supabase = get_supabase()
            tz  = timezone(timedelta(hours=1))
            now = datetime.now(tz)
            result = supabase.table("game_nights").select("*").execute()
            for gn in result.data:
                if not gn.get('zeitpunkt'):
                    continue
                zeitpunkt = datetime.fromisoformat(gn['zeitpunkt'])
                if zeitpunkt.tzinfo is None:
                    zeitpunkt = zeitpunkt.replace(tzinfo=tz)
                time_diff = (zeitpunkt - now).total_seconds() / 60
                thread = self.bot.get_channel(int(gn['thread_id']))
                if not thread:
                    continue
                if 59 <= time_diff <= 61 and gn.get('vielleicht') and not gn.get('reminded_1h'):
                    mentions = " ".join([f"<@{uid}>" for uid in gn['vielleicht']])
                    await thread.send(f"⏰ **1 Stunde bis zum Start!**\n{mentions} - Habt ihr doch noch Zeit?")
                    supabase.table("game_nights").update({"reminded_1h": True}).eq("id", gn['id']).execute()
                if 9 <= time_diff <= 11 and gn.get('dabei') and not gn.get('reminded_10m'):
                    mentions = " ".join([f"<@{uid}>" for uid in gn['dabei']])
                    await thread.send(f"⏰ **10 Minuten bis zum Start!**\n{mentions}")
                    supabase.table("game_nights").update({"reminded_10m": True}).eq("id", gn['id']).execute()
                if -1 <= time_diff <= 1 and gn.get('dabei') and not gn.get('reminded_start'):
                    mentions = " ".join([f"<@{uid}>" for uid in gn['dabei']])
                    await thread.send(f"🎮 **Es geht los!**\n{mentions}")
                    supabase.table("game_nights").update({"reminded_start": True}).eq("id", gn['id']).execute()
        except Exception as e:
            logger.error(f"[check_reminders] Fehler: {e}")

    @check_reminders.before_loop
    async def before_reminders(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(SpielabendCog(bot))