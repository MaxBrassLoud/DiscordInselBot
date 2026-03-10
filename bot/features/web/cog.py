"""bot/features/web/cog.py – /web_setup Command"""

import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

logger = get_logger("web")


class WebCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="web_setup",
        description="Zeigt wer Zugriff auf das Web-Dashboard hat"
    )
    async def web_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            supabase  = get_supabase()
            server_id = str(interaction.guild_id)
            guild     = interaction.guild

            embed = discord.Embed(
                title="🌐 Web-Dashboard Berechtigungen",
                description=(
                    "Übersicht wer auf das Dashboard zugreifen kann und was sichtbar ist.\n"
                    "Berechtigungen werden automatisch durch das Ticket- und Bewerbungs-System gesetzt."
                ),
                color=discord.Color.blurple(),
            )

            # ── Vollzugriff: Application Staff ────────────────────────────────
            app_cfg = supabase.table("application_servers")\
                .select("staff_role_ids")\
                .eq("server_id", server_id)\
                .execute()

            if app_cfg.data and app_cfg.data[0].get("staff_role_ids"):
                raw = app_cfg.data[0]["staff_role_ids"]
                # staff_role_ids is stored as comma-separated string or JSON array
                if isinstance(raw, str):
                    role_ids = [r.strip() for r in raw.split(",") if r.strip()]
                else:
                    role_ids = [str(r) for r in raw]

                if role_ids:
                    role_mentions = []
                    for rid in role_ids:
                        role = guild.get_role(int(rid))
                        role_mentions.append(role.mention if role else f"<@&{rid}>")

                    embed.add_field(
                        name="👑 Voller Zugriff – Alle Bewerbungen",
                        value=(
                            "\n".join(role_mentions) + "\n\n"
                            "✅ Können **alle Bewerbungen** dieses Servers im Dashboard sehen."
                        ),
                        inline=False,
                    )
                else:
                    embed.add_field(
                        name="👑 Voller Zugriff – Bewerbungen",
                        value="*Keine Staff-Rollen konfiguriert*\nNutze `/bewerbung_bearbeiten` um Rollen festzulegen.",
                        inline=False,
                    )
            else:
                embed.add_field(
                    name="👑 Voller Zugriff – Bewerbungen",
                    value="*Bewerbungs-System nicht eingerichtet*\nNutze `/bewerbung_setup`.",
                    inline=False,
                )

            # ── Ticket-Module: Staff pro Modul ────────────────────────────────
            modules = supabase.table("ticket_modules")\
                .select("id, name, button_emoji")\
                .eq("server_id", server_id)\
                .execute().data or []

            if modules:
                embed.add_field(
                    name="\u200b",
                    value="**🎫 Ticket-Zugriff nach Modul**\nJede Rolle sieht nur Tickets ihrer Kategorie.",
                    inline=False,
                )

                for mod in modules:
                    mod_roles = supabase.table("ticket_module_roles")\
                        .select("role_id")\
                        .eq("module_id", mod["id"])\
                        .execute().data or []

                    emoji = mod.get("button_emoji") or "🎫"
                    # strip custom emoji for field name safety
                    if str(emoji).startswith("<"):
                        emoji = "🎫"

                    if mod_roles:
                        role_mentions = []
                        for r in mod_roles:
                            role = guild.get_role(int(r["role_id"]))
                            role_mentions.append(role.mention if role else f"<@&{r['role_id']}>")
                        value = "\n".join(role_mentions)
                    else:
                        value = "*Keine Staff-Rollen*"

                    embed.add_field(
                        name=f"{emoji} {mod['name']}",
                        value=value,
                        inline=True,
                    )
            else:
                embed.add_field(
                    name="🎫 Ticket-Zugriff",
                    value="*Ticket-System nicht eingerichtet*\nNutze `/ticket_setup`.",
                    inline=False,
                )

            embed.set_footer(text=f"Server-ID: {server_id}")
            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"[web_setup] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebCog(bot))