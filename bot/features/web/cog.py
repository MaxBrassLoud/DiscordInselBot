"""bot/features/web/cog.py – /web_setup Command"""

import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger

logger = get_logger("web")


# ── WebAdmin Setup View ───────────────────────────────────────────────────────

class WebAdminSetupView(discord.ui.View):
    """
    Lets admins choose which roles receive full WebAdmin access
    (see all tickets + all applications) for this server.
    Saves web_admin_role_ids to both ticket_servers and application_servers.
    """

    def __init__(self, guild_id: int, current_ticket_admin_ids: list[str],
                 current_app_admin_ids: list[str]):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.ticket_admin_ids  = list(current_ticket_admin_ids)
        self.app_admin_ids     = list(current_app_admin_ids)
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🌐 Web-Dashboard – WebAdmin Einstellungen",
            description=(
                "WebAdmins können **alle** Tickets und Bewerbungen dieses Servers "
                "im Dashboard einsehen, unabhängig von Modul-Staff-Rollen.\n\n"
                "Wähle die Rollen aus und klicke **Speichern**."
            ),
            color=discord.Color.blurple(),
        )
        ticket_val = (
            ", ".join(f"<@&{r}>" for r in self.ticket_admin_ids)
            if self.ticket_admin_ids else "*keine*"
        )
        app_val = (
            ", ".join(f"<@&{r}>" for r in self.app_admin_ids)
            if self.app_admin_ids else "*keine*"
        )
        e.add_field(name="🎫 WebAdmin Rollen (Tickets)",     value=ticket_val, inline=False)
        e.add_field(name="⛏️ WebAdmin Rollen (Bewerbungen)", value=app_val,   inline=False)
        return e

    def _rebuild(self):
        self.clear_items()

        ticket_sel = discord.ui.RoleSelect(
            placeholder="🎫 WebAdmin-Rollen für Tickets wählen…",
            min_values=0, max_values=10, row=0,
        )
        ticket_sel.callback = self._ticket_roles
        self.add_item(ticket_sel)

        app_sel = discord.ui.RoleSelect(
            placeholder="⛏️ WebAdmin-Rollen für Bewerbungen wählen…",
            min_values=0, max_values=10, row=1,
        )
        app_sel.callback = self._app_roles
        self.add_item(app_sel)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.success,
            row=2,
        )
        save_btn.callback = self._save
        self.add_item(save_btn)

    async def _ticket_roles(self, interaction: discord.Interaction):
        self.ticket_admin_ids = interaction.data.get("values", [])
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _app_roles(self, interaction: discord.Interaction):
        self.app_admin_ids = interaction.data.get("values", [])
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            ticket_ids_str = ",".join(self.ticket_admin_ids)
            app_ids_str    = ",".join(self.app_admin_ids)

            # Update ticket_servers if record exists
            if supabase.table("ticket_servers").select("server_id")\
                    .eq("server_id", self.guild_id).execute().data:
                supabase.table("ticket_servers").update(
                    {"web_admin_role_ids": ticket_ids_str}
                ).eq("server_id", self.guild_id).execute()

            # Update application_servers if record exists
            if supabase.table("application_servers").select("server_id")\
                    .eq("server_id", self.guild_id).execute().data:
                supabase.table("application_servers").update(
                    {"web_admin_role_ids": app_ids_str}
                ).eq("server_id", self.guild_id).execute()

            embed = self._build_embed()
            embed.title = "✅ WebAdmin-Einstellungen gespeichert!"
            embed.color = discord.Color.green()
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=embed, view=self)
            await interaction.followup.send(
                "✅ WebAdmin-Rollen wurden gespeichert.", ephemeral=True
            )
        except Exception as e:
            logger.error(f"[WebAdminSetupView._save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ── Cog ───────────────────────────────────────────────────────────────────────

class WebCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="web_setup",
        description="Konfiguriere Web-Dashboard Berechtigungen (WebAdmin-Rollen)"
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

            # ── Load current WebAdmin IDs ─────────────────────────────────────
            def _parse(raw) -> list[str]:
                if not raw:
                    return []
                if isinstance(raw, list):
                    return [str(r) for r in raw if r]
                return [r.strip() for r in str(raw).split(",") if r.strip()]

            ticket_cfg = supabase.table("ticket_servers").select("web_admin_role_ids")\
                .eq("server_id", server_id).execute()
            current_ticket_admins = _parse(
                ticket_cfg.data[0].get("web_admin_role_ids") if ticket_cfg.data else ""
            )

            app_cfg = supabase.table("application_servers").select("web_admin_role_ids")\
                .eq("server_id", server_id).execute()
            current_app_admins = _parse(
                app_cfg.data[0].get("web_admin_role_ids") if app_cfg.data else ""
            )

            # ── Build overview embed ──────────────────────────────────────────
            embed = discord.Embed(
                title="🌐 Web-Dashboard Berechtigungen",
                description=(
                    "Übersicht aller Dashboard-Berechtigungen.\n\n"
                    "**WebAdmins** können alles sehen.\n"
                    "**Modul-Staff** sieht nur Tickets ihres Moduls.\n"
                    "**Bewerbungs-Staff** sieht alle Bewerbungen.\n"
                    "**Ticket-Ersteller / Bewerbungs-Ersteller** sehen ihre eigenen Einträge.\n"
                    "**MBL** (env-var) sieht alles auf allen Servern."
                ),
                color=discord.Color.blurple(),
            )

            # WebAdmin roles (Tickets)
            if current_ticket_admins:
                mentions = [
                    (guild.get_role(int(r)).mention if guild.get_role(int(r)) else f"<@&{r}>")
                    for r in current_ticket_admins
                ]
                embed.add_field(
                    name="👑 WebAdmin – Tickets (alle Tickets sehen)",
                    value="\n".join(mentions),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="👑 WebAdmin – Tickets",
                    value="*Keine WebAdmin-Rollen konfiguriert*",
                    inline=False,
                )

            # WebAdmin roles (Applications)
            if current_app_admins:
                mentions = [
                    (guild.get_role(int(r)).mention if guild.get_role(int(r)) else f"<@&{r}>")
                    for r in current_app_admins
                ]
                embed.add_field(
                    name="👑 WebAdmin – Bewerbungen (alle Bewerbungen sehen)",
                    value="\n".join(mentions),
                    inline=False,
                )
            else:
                embed.add_field(
                    name="👑 WebAdmin – Bewerbungen",
                    value="*Keine WebAdmin-Rollen konfiguriert*",
                    inline=False,
                )

            embed.add_field(name="\u200b", value="─" * 30, inline=False)

            # Ticket module staff breakdown
            modules = supabase.table("ticket_modules")\
                .select("id, name, button_emoji")\
                .eq("server_id", server_id)\
                .execute().data or []

            if modules:
                embed.add_field(
                    name="🎫 Ticket-Modul Berechtigungen",
                    value="Jede Rolle sieht nur Tickets ihres Moduls.",
                    inline=False,
                )
                for mod in modules:
                    mod_roles = supabase.table("ticket_module_roles")\
                        .select("role_id").eq("module_id", mod["id"]).execute().data or []
                    emoji = mod.get("button_emoji") or "🎫"
                    if str(emoji).startswith("<"):
                        emoji = "🎫"
                    if mod_roles:
                        role_mentions = [
                            (guild.get_role(int(r["role_id"])).mention
                             if guild.get_role(int(r["role_id"])) else f"<@&{r['role_id']}>")
                            for r in mod_roles
                        ]
                        value = "\n".join(role_mentions)
                    else:
                        value = "*Keine Staff-Rollen*"
                    embed.add_field(name=f"{emoji} {mod['name']}", value=value, inline=True)
            else:
                embed.add_field(
                    name="🎫 Ticket-Modul Berechtigungen",
                    value="*Ticket-System nicht eingerichtet* – nutze `/ticket_setup`.",
                    inline=False,
                )

            # Application staff
            app_srv = supabase.table("application_servers")\
                .select("staff_role_ids").eq("server_id", server_id).execute()
            if app_srv.data and app_srv.data[0].get("staff_role_ids"):
                raw = app_srv.data[0]["staff_role_ids"]
                role_ids = _parse(raw)
                if role_ids:
                    role_mentions = [
                        (guild.get_role(int(r)).mention if guild.get_role(int(r)) else f"<@&{r}>")
                        for r in role_ids
                    ]
                    embed.add_field(
                        name="⛏️ Bewerbungs-Staff (alle Bewerbungen sehen)",
                        value="\n".join(role_mentions),
                        inline=False,
                    )
            else:
                embed.add_field(
                    name="⛏️ Bewerbungs-Staff",
                    value="*Bewerbungs-System nicht eingerichtet* – nutze `/bewerbung_setup`.",
                    inline=False,
                )

            embed.set_footer(text=f"Server-ID: {server_id}")

            # Send overview + WebAdmin setup view
            setup_view = WebAdminSetupView(
                guild_id=interaction.guild_id,
                current_ticket_admin_ids=current_ticket_admins,
                current_app_admin_ids=current_app_admins,
            )
            await interaction.followup.send(embed=embed, view=setup_view, ephemeral=True)

        except Exception as e:
            logger.error(f"[web_setup] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WebCog(bot))