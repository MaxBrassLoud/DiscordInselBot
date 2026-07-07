import discord
from discord import app_commands
from discord.ext import commands
from datetime import datetime
from bot.core.settings import upsert_settings
from bot.utils.timezone_utils import get_guild_timezone

TIMEZONE_CHOICES = [
    app_commands.Choice(name="UTC (London)", value="Europe/London"),
    app_commands.Choice(name="Mitteleuropa (Berlin)", value="Europe/Berlin"),
    app_commands.Choice(name="Osteuropa (Athen)", value="Europe/Athens"),
    app_commands.Choice(name="US-Ostküste (New York)", value="America/New_York"),
    app_commands.Choice(name="US-Zentral (Chicago)", value="America/Chicago"),
    app_commands.Choice(name="US-Westküste (Los Angeles)", value="America/Los_Angeles"),
    app_commands.Choice(name="Japan (Tokio)", value="Asia/Tokyo"),
    app_commands.Choice(name="Australien (Sydney)", value="Australia/Sydney"),
]

class TimezoneCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="timezone", description="Zeitzonen-Einstellungen für diesen Server")
    @app_commands.default_permissions(administrator=True)
    async def timezone_group(self, interaction: discord.Interaction):
        pass

    @timezone_group.command(name="setup", description="Legt die Zeitzone für diesen Server fest")
    @app_commands.choices(zone=TIMEZONE_CHOICES)
    async def timezone_setup(self, interaction: discord.Interaction, zone: app_commands.Choice[str]):
        guild_id = str(interaction.guild_id)
        await upsert_settings(guild_id, {"timezone": zone.value})
        tz = await get_guild_timezone(interaction.guild_id)
        now = datetime.now(tz).strftime("%H:%M Uhr")
        embed = discord.Embed(
            title="✅ Zeitzone gesetzt",
            description=f"Server-Zeitzone: **{zone.name}**\nAktuelle Zeit: **{now}**",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    await bot.add_cog(TimezoneCog(bot))