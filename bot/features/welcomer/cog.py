import random
import discord
from discord.ext import commands
from discord import app_commands

from bot.core.settings import get_settings, upsert_settings
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

logger = get_logger("welcomer")

WELCOME_MESSAGES = [
    "**{mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>**",
    "**wow, wie toll! {mention} ist jetzt hier! <:welcome:1362364322772160513>**",
    "**{mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>**",
    "**Juhu, {mention} hat zur Insel gefunden!**",
    "**Kuckt mal wer hier ist: {mention} ! <:pepehappy:1362364194967781598>**",
    "**Herzlich Willkommen {mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>**",
    "**{mention} ist dem Insel-Discord beigetreten! 🫡**",
    "**Endlich! {mention} ist hier! 😇**",
    "**Huhu {mention} . Willkommen 🙂**",
    "**Ein wildes  {mention} ist auf die Insel geschlittert 😄**",
    "**Wilkommen {mention} bei der Insel! <:pepehappy:1362364194967781598>**",
    "**{mention}, was geht yallah <:welcome:1362364322772160513>**",
    "**Oh halloo! {mention} 🙂 **",
    "**Heyyyy was geeeht {mention} 😀 **",
    "**{mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!**",
    "**Seht Seht {mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>**",
    "**Boar das schmeckt, {mention} ist nun hier!🙃**",
]


class SetupWelcomerView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        cfg = current_config or {}
        self.welcome_channel_id: str | None = str(cfg["welcome_channel_id"]) if cfg.get("welcome_channel_id") else None
        self.goodbye_channel_id: str | None = str(cfg["goodbye_channel_id"]) if cfg.get("goodbye_channel_id") else None
        self.welcome_enabled: bool = bool(cfg.get("welcome_enabled", True))
        self.goodbye_enabled: bool = bool(cfg.get("goodbye_enabled", True))

        ch_welcome = discord.ui.ChannelSelect(
            placeholder="👋 Willkommens-Kanal auswählen",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.text],
        )
        ch_welcome.callback = self.welcome_channel_callback
        self.add_item(ch_welcome)

        ch_goodbye = discord.ui.ChannelSelect(
            placeholder="🚪 Abschied-Kanal auswählen",
            min_values=1, max_values=1, channel_types=[discord.ChannelType.text],
        )
        ch_goodbye.callback = self.goodbye_channel_callback
        self.add_item(ch_goodbye)

        self._add_buttons()

    def _add_buttons(self):
        for item in [i for i in self.children if isinstance(i, discord.ui.Button)]:
            self.remove_item(item)

        welcome_btn = discord.ui.Button(
            label=f"Willkommen: {'AN ✅' if self.welcome_enabled else 'AUS ❌'}",
            emoji="👋",
            style=discord.ButtonStyle.success if self.welcome_enabled else discord.ButtonStyle.secondary,
        )
        async def toggle_welcome(interaction: discord.Interaction):
            self.welcome_enabled = not self.welcome_enabled
            self._add_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        welcome_btn.callback = toggle_welcome
        self.add_item(welcome_btn)

        goodbye_btn = discord.ui.Button(
            label=f"Abschied: {'AN ✅' if self.goodbye_enabled else 'AUS ❌'}",
            emoji="🚪",
            style=discord.ButtonStyle.success if self.goodbye_enabled else discord.ButtonStyle.secondary,
        )
        async def toggle_goodbye(interaction: discord.Interaction):
            self.goodbye_enabled = not self.goodbye_enabled
            self._add_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        goodbye_btn.callback = toggle_goodbye
        self.add_item(goodbye_btn)

        save_btn = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.primary)
        save_btn.callback = self.save_callback
        self.add_item(save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ Welcomer Setup", color=discord.Color.blurple())
        embed.add_field(name="👋 Willkommens-Kanal",
                        value=f"<#{self.welcome_channel_id}>" if self.welcome_channel_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="📊 Willkommen", value="✅ AN" if self.welcome_enabled else "❌ AUS", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="🚪 Abschied-Kanal",
                        value=f"<#{self.goodbye_channel_id}>" if self.goodbye_channel_id else "*Nicht ausgewählt*", inline=True)
        embed.add_field(name="📊 Abschied", value="✅ AN" if self.goodbye_enabled else "❌ AUS", inline=True)
        return embed

    async def welcome_channel_callback(self, interaction: discord.Interaction):
        self.welcome_channel_id = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def goodbye_channel_callback(self, interaction: discord.Interaction):
        self.goodbye_channel_id = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            await upsert_settings(str(self.guild_id), {
                "welcome_channel_id": self.welcome_channel_id,
                "goodbye_channel_id": self.goodbye_channel_id,
                "welcome_enabled":    self.welcome_enabled,
                "goodbye_enabled":    self.goodbye_enabled,
            })
            embed = self._build_embed()
            embed.color = discord.Color.green()
            embed.title = "✅ Welcomer gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class WelcomerCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    welcomer = app_commands.Group(
        name="welcomer",
        description="Welcomer System",
    )

    @welcomer.command(name="setup", description="Konfiguriere Willkommens- und Abschiedsnachrichten")
    async def setup_welcomer(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        current = await get_settings(str(interaction.guild_id))
        view = SetupWelcomerView(interaction.guild_id, current)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            config = await get_settings(str(member.guild.id))
            if not config:
                return
            if config.get("welcome_enabled", True) and config.get("welcome_channel_id"):
                channel = self.bot.get_channel(int(config["welcome_channel_id"]))
                if channel:
                    embed = discord.Embed(
                        title=f"Wilkommen {member.display_name}!",
                        description=random.choice(WELCOME_MESSAGES).format(mention=member.mention),
                        color=discord.Color.green()
                    )
                    #msg = random.choice(WELCOME_MESSAGES).format(mention=member.mention)
                    await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[on_member_join] Fehler: {e}")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            config = await get_settings(str(member.guild.id))
            if not config:
                return
            if config.get("goodbye_enabled", True) and config.get("goodbye_channel_id"):
                channel = self.bot.get_channel(int(config["goodbye_channel_id"]))
                if channel:
                    embed = discord.Embed(
                        title=f"👋 Goodbye {member.display_name}!",
                        description=f"{member.mention} hat den Server verlassen.",
                        color=discord.Color.orange()
                    )

                    embed.add_field(
                        name="📅 Mitglied seit",
                        value=discord.utils.format_dt(member.joined_at, style="F") if member.joined_at else "Unbekannt",
                        inline=False
                    )

                    top_role = member.top_role
                    if top_role.name != "@everyone":
                        embed.add_field(
                            name="🔝 Höchste Rolle",
                            value=top_role.mention,
                            inline=True
                        )

                    roles = [role.mention for role in member.roles if role.name != "@everyone"]
                    embed.add_field(
                        name=f"🎭 Rollen ({len(roles)})",
                        value="\n".join(roles) if roles else "Keine",
                        inline=False
                    )

                    embed.add_field(
                        name="📆 Account erstellt",
                        value=discord.utils.format_dt(member.created_at, style="F"),
                        inline=True
                    )
                    embed.set_thumbnail(url=member.display_avatar.url)

                    embed.timestamp = discord.utils.utcnow()
                    embed.set_footer(text=f"User-ID: {member.id}")


                    if member.top_role.color.value != 0:  # 0 = Standardfarbe
                        embed.color = member.top_role.color

                    await channel.send(embed=embed)


        except Exception as e:
            logger.error(f"[on_member_remove] Fehler: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomerCog(bot))