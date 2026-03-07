import discord
from datetime import datetime, timedelta, timezone

from bot.core.settings import get_settings
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .views import SpielabendView

logger = get_logger("spieleabend.modal")


class SpielabendModal(discord.ui.Modal, title="Spieleabend erstellen"):
    titel = discord.ui.TextInput(
        label="Titel (Spiel/Aktivität)",
        placeholder="z.B. Valorant, Minecraft, etc.",
        required=True, max_length=100
    )
    uhrzeit = discord.ui.TextInput(
        label="Uhrzeit",
        placeholder="z.B. 20:00 oder 03.01.2026 20:00",
        required=True, max_length=50
    )
    beschreibung = discord.ui.TextInput(
        label="Beschreibung (Optional)",
        placeholder="Weitere Details zum Spieleabend...",
        required=False, style=discord.TextStyle.paragraph, max_length=500
    )

    def __init__(self, bot: discord.Client):
        super().__init__()
        self.bot = bot

    def parse_time(self, time_str: str):
        try:
            tz = timezone(timedelta(hours=1))
            if ":" in time_str and len(time_str.split()) == 1:
                parts = time_str.split(":")
                now = datetime.now(tz)
                target = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
                if target < now:
                    target += timedelta(days=1)
                return target
            for fmt in ["%d.%m.%Y %H:%M", "%d.%m. %H:%M"]:
                try:
                    parsed = datetime.strptime(time_str, fmt)
                    if fmt == "%d.%m. %H:%M":
                        parsed = parsed.replace(year=datetime.now(tz).year)
                    return parsed.replace(tzinfo=tz)
                except Exception:
                    continue
            return None
        except Exception:
            return None

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            config = await get_settings(str(interaction.guild_id))
            if not config:
                await interaction.followup.send("❌ Bitte führe zuerst `/setup_spieleabend` aus!", ephemeral=True)
                return
            channel = self.bot.get_channel(int(config['channel_id']))
            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return
            zeitpunkt = self.parse_time(self.uhrzeit.value)
            embed = discord.Embed(
                title=f"🎮 {self.titel.value}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="⏰ Uhrzeit",     value=self.uhrzeit.value, inline=False)
            if self.beschreibung.value:
                embed.add_field(name="📝 Beschreibung", value=self.beschreibung.value, inline=False)
            embed.add_field(name="✅ Dabei",       value="*Niemand*", inline=False)
            embed.add_field(name="❓ Vielleicht",  value="*Niemand*", inline=False)
            embed.add_field(name="❌ Keine Zeit",  value="*Niemand*", inline=False)
            role = interaction.guild.get_role(int(config['ping_role_id']))
            ping_text = role.mention if role else "@everyone"
            view = SpielabendView()
            message = await channel.send(content=ping_text, embed=embed, view=view)
            thread = await message.create_thread(name=f"💬 {self.titel.value}", auto_archive_duration=1440)
            await thread.send(f"Hier könnt ihr über den Spieleabend **{self.titel.value}** diskutieren! 🎮")
            game_night_data = {
                "guild_id":    str(interaction.guild_id),
                "message_id":  str(message.id),
                "thread_id":   str(thread.id),
                "titel":       self.titel.value,
                "uhrzeit":     self.uhrzeit.value,
                "zeitpunkt":   zeitpunkt.isoformat() if zeitpunkt else None,
                "beschreibung": self.beschreibung.value or None,
                "dabei":       [],
                "vielleicht":  [],
                "keine_zeit":  [],
                "creator_id":  str(interaction.user.id)
            }
            result = supabase.table("game_nights").insert(game_night_data).execute()
            if result.data:
                embed.set_footer(text=f"Spieleabend ID: {result.data[0]['id']}")
                await message.edit(embed=embed)
            await interaction.followup.send(f"✅ Spieleabend erstellt! {message.jump_url}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {str(e)}", ephemeral=True)