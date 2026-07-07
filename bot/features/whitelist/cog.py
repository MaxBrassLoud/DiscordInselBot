import discord
from discord import app_commands
from discord.ext import commands
from bot.tools.whw_bridge import WHWClient
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("whitelist_cog")

class MinecraftCog(commands.GroupCog, name="minecraft"):
    """Alle Minecraft-Server Befehle"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = WHWClient()
        super().__init__()

    # ---- Whitelist ----
    @app_commands.command(name="whitelist_add", description="Fügt Spieler zur Whitelist hinzu")
    @app_commands.describe(player="Minecraft-Name", duration="Dauer z.B. 10m, 2h, 1d (leer = permanent)")
    async def whitelist_add(self, interaction: discord.Interaction, player: str, duration: str = None):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.add_whitelist(player, duration)
        if result and result.get("status") == "success":
            await interaction.followup.send(f"✅ {player} wurde zur Whitelist hinzugefügt.")
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler: {error_msg}")

    @app_commands.command(name="whitelist_remove", description="Entfernt Spieler von der Whitelist")
    @app_commands.describe(player="Minecraft-Name")
    async def whitelist_remove(self, interaction: discord.Interaction, player: str):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.remove_whitelist(player)
        if result and result.get("status") == "success":
            await interaction.followup.send(f"✅ {player} wurde von der Whitelist entfernt.")
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler: {error_msg}")

    @app_commands.command(name="whitelist_list", description="Zeigt die aktuelle Whitelist")
    async def whitelist_list(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.get_whitelist()
        if result and result.get("status") == "success":
            data = result.get("whitelist", {})
            if not data:
                await interaction.followup.send("📭 Die Whitelist ist leer.")
            else:
                lines = [f"**{name}** → {expiry}" for name, expiry in data.items()]
                await interaction.followup.send("**Aktuelle Whitelist:**\n" + "\n".join(lines))
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler beim Abrufen der Whitelist: {error_msg}")

    # ---- Bann ----
    @app_commands.command(name="ban", description="Bannt Spieler auf dem Minecraft-Server")
    @app_commands.describe(player="Minecraft-Name", duration="Dauer z.B. 10m, 2h, 1d", reason="Ban-Grund")
    async def ban(self, interaction: discord.Interaction, player: str, duration: str = None, reason: str = None):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.add_ban(player, duration, reason)
        if result and result.get("status") == "success":
            await interaction.followup.send(f"✅ {player} wurde gebannt.")
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler: {error_msg}")

    @app_commands.command(name="unban", description="Entbannt Spieler auf dem Minecraft-Server")
    @app_commands.describe(player="Minecraft-Name")
    async def unban(self, interaction: discord.Interaction, player: str):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.remove_ban(player)
        if result and result.get("status") == "success":
            await interaction.followup.send(f"✅ {player} wurde entbannt.")
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler: {error_msg}")

    @app_commands.command(name="ban_list", description="Zeigt die aktuelle Bannliste")
    async def ban_list(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.get_bans()
        if result and result.get("status") == "success":
            data = result.get("bans", {})
            if not data:
                await interaction.followup.send("📭 Keine aktiven Bans.")
            else:
                lines = [f"**{name}** → Grund: {info.get('reason', '?')}, Ablauf: {info.get('expiry', '?')}" for name, info in data.items()]
                await interaction.followup.send("**Aktuelle Bans:**\n" + "\n".join(lines))
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler beim Abrufen der Bans: {error_msg}")

    # ---- Kick ----
    @app_commands.command(name="kick", description="Kickt Spieler vom Minecraft-Server")
    @app_commands.describe(player="Minecraft-Name", reason="Kick-Grund")
    async def kick(self, interaction: discord.Interaction, player: str, reason: str = None):
        if not has_admin_rights(interaction):
            return await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        result = self.client.kick(player, reason)
        if result and result.get("status") == "success":
            await interaction.followup.send(f"✅ {player} wurde gekickt.")
        else:
            error_msg = result.get("message", "Unbekannt") if result else "API nicht erreichbar"
            await interaction.followup.send(f"❌ Fehler: {error_msg}")

async def setup(bot: commands.Bot):
    await bot.add_cog(MinecraftCog(bot))