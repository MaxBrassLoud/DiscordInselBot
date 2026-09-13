from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.guild_time import get_timezone
from bot.core.settings import get_settings, upsert_settings
from bot.utils.permissions import has_admin_rights


TIMEZONE_CHOICES = [
    app_commands.Choice(name="Deutschland / Mitteleuropa (Europe/Berlin)", value="Europe/Berlin"),
    app_commands.Choice(name="Vereinigtes Koenigreich (Europe/London)", value="Europe/London"),
    app_commands.Choice(name="Osteuropa (Europe/Helsinki)", value="Europe/Helsinki"),
    app_commands.Choice(name="USA Ostkueste (America/New_York)", value="America/New_York"),
    app_commands.Choice(name="USA Westkueste (America/Los_Angeles)", value="America/Los_Angeles"),
    app_commands.Choice(name="UTC", value="UTC"),
]


class TimeCog(commands.Cog):
    """Server-Zeitzone konfigurieren."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="time", description="Setzt oder zeigt die Zeitzone dieses Discord-Servers")
    @app_commands.describe(zeitzone="Zeitzone des Servers")
    @app_commands.choices(zeitzone=TIMEZONE_CHOICES)
    async def time(self, interaction: discord.Interaction,
                   zeitzone: app_commands.Choice[str] | None = None):
        if interaction.guild_id is None:
            await interaction.response.send_message("❌ Dieser Befehl funktioniert nur auf einem Server.", ephemeral=True)
            return
        current = await get_settings(str(interaction.guild_id))
        if zeitzone is not None:
            if not has_admin_rights(interaction):
                await interaction.response.send_message("❌ Nur Admins können die Server-Zeitzone ändern.", ephemeral=True)
                return
            # Ältere Datenbankschemata verlangen diese drei ursprünglichen
            # Spieleabend-Felder noch beim ersten Settings-Eintrag.
            initial_values = {} if current else {
                "ping_role_id": "", "channel_id": "", "delete_role_ids": "",
            }
            await upsert_settings(
                str(interaction.guild_id), {**initial_values, "timezone": zeitzone.value}
            )
            current = {**(current or {}), "timezone": zeitzone.value}

        zone_name = (current or {}).get("timezone", "Europe/Berlin")
        now = datetime.now(get_timezone(zone_name))
        dst = "Sommerzeit" if now.dst() and now.dst().total_seconds() else "Winterzeit / Normalzeit"
        await interaction.response.send_message(
            f"🕐 Server-Zeitzone: **{zone_name}**\\n"
            f"Aktuelle Serverzeit: **{now:%d.%m.%Y %H:%M}** ({dst})\\n"
            "Die Umstellung zwischen Sommer- und Winterzeit erfolgt automatisch.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(TimeCog(bot))
