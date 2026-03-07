import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .setup_views import TicketSetupView
from .storage import append_message

logger = get_logger("tickets")


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Log all messages in ticket channels to messages.json."""
        if message.author.bot:
            return
        if not message.guild:
            return
        try:
            # Check if this is a ticket channel by looking at channel name pattern
            # e.g. "15-max-support"
            parts = message.channel.name.split("-")
            if len(parts) < 3:
                return
            if not parts[0].isdigit():
                return
            ticket_id = int(parts[0])
            server_id = str(message.guild.id)
            attachments = [a.url for a in message.attachments]
            append_message(
                server_id=server_id,
                ticket_id=ticket_id,
                user=message.author.display_name,
                user_id=str(message.author.id),
                content=message.content or "",
                attachments=attachments,
            )
        except Exception:
            pass  # Not a ticket channel, ignore

    @app_commands.command(name="ticket_setup", description="Richte das Ticket-System ein")
    async def ticket_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = TicketSetupView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @app_commands.command(name="ticket_info", description="Zeigt Informationen über das Ticket-System")
    async def ticket_info(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            supabase  = get_supabase()
            server_id = str(interaction.guild_id)
            server    = supabase.table("ticket_servers").select("*").eq("server_id", server_id).execute()
            modules   = supabase.table("ticket_modules").select("*").eq("server_id", server_id).execute()
            tickets   = supabase.table("tickets").select("ticket_id,status").eq("server_id", server_id).execute()

            embed = discord.Embed(title="🎫 Ticket-System Info", color=discord.Color.blurple())
            if server.data:
                s = server.data[0]
                embed.add_field(name="📢 Panel-Kanal", value=f"<#{s.get('panel_channel_id', '?')}>" if s.get("panel_channel_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="📁 Kategorie",   value=f"<#{s.get('category_id', '?')}>" if s.get("category_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="🔢 Tickets gesamt", value=str(s.get("ticket_counter", 0)), inline=True)
            else:
                embed.add_field(name="Status", value="❌ Nicht eingerichtet. Nutze `/ticket_setup`.", inline=False)

            if modules.data:
                for mod in modules.data:
                    embed.add_field(name=f"📂 {mod['name']}", value=f"Max/User: {mod['max_tickets']}", inline=True)

            if tickets.data:
                open_t   = sum(1 for t in tickets.data if t["status"] == "open")
                closed_t = sum(1 for t in tickets.data if t["status"] == "closed")
                embed.add_field(name="📊 Offene Tickets",      value=str(open_t),   inline=True)
                embed.add_field(name="✅ Geschlossene Tickets", value=str(closed_t), inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketsCog(bot))