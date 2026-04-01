import discord
from discord.ext import commands
from discord import app_commands

from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .setup_views import TicketSetupView
from .storage import append_message, load_ticket
from .manager import TicketManager, ticket_web_url

logger = get_logger("tickets")


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Restore all persistent ticket views on bot restart."""
        await self._restore_panel_views()
        await self._restore_channel_views()

    async def _restore_panel_views(self):
        """Re-register TicketPanelView for every server."""
        from .views import TicketPanelView
        try:
            supabase = get_supabase()
            servers  = supabase.table("ticket_servers").select("*").execute().data or []
            count    = 0
            for srv in servers:
                modules = await TicketManager.get_server_modules(srv["server_id"])
                if not modules:
                    continue
                category_id = int(srv.get("category_id", 0))
                view = TicketPanelView(modules=modules, category_id=category_id, bot=self.bot)
                self.bot.add_view(view)
                count += 1
            logger.info(f"✅ {count} TicketPanelView(s) wiederhergestellt")
        except Exception as e:
            logger.error(f"[_restore_panel_views] {e}")

    async def _restore_channel_views(self):
        """Re-register TicketChannelView for all open tickets."""
        from .views import TicketChannelView
        try:
            supabase = get_supabase()
            tickets  = supabase.table("tickets").select("*").eq("status", "open").execute().data or []
            count    = 0
            for t in tickets:
                try:
                    module = None
                    mods = supabase.table("ticket_modules")\
                        .select("*")\
                        .eq("server_id", t["server_id"])\
                        .eq("name", t["module"])\
                        .execute().data
                    if mods:
                        mod_id = mods[0]["id"]
                        module = await TicketManager.get_module(mod_id)
                    if not module:
                        module = {"name": t["module"], "staff_role_ids": []}

                    view = TicketChannelView(
                        ticket_id=t["ticket_id"],
                        server_id=t["server_id"],
                        creator_id=t.get("creator_id", ""),
                        module=module,
                        bot=self.bot,
                    )
                    local = load_ticket(t["server_id"], t["ticket_id"])
                    if local and local.get("claimed_by"):
                        view._claimed_by_id = local["claimed_by"]

                    self.bot.add_view(view)
                    count += 1
                except Exception as inner_e:
                    logger.error(f"[_restore_channel_views] Ticket {t.get('ticket_id')}: {inner_e}")
            logger.info(f"✅ {count} TicketChannelView(s) wiederhergestellt")
        except Exception as e:
            logger.error(f"[_restore_channel_views] {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Log all messages in ticket channels to ticket_messages table."""
        if message.author.bot:
            return
        if not message.guild:
            return
        try:
            server_id  = str(message.guild.id)
            channel_id = str(message.channel.id)

            supabase = get_supabase()
            result = (
                supabase.table("tickets")
                .select("ticket_id")
                .eq("server_id", server_id)
                .eq("channel_id", channel_id)
                .eq("status", "open")
                .limit(1)
                .execute()
            )
            if not result.data:
                return

            ticket_id   = result.data[0]["ticket_id"]
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
            pass

    # ── /ticket_setup ─────────────────────────────────────────────────────────

    @app_commands.command(name="ticket_setup", description="Richte das Ticket-System ein")
    async def ticket_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = TicketSetupView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    # ── /ticket_bearbeiten ────────────────────────────────────────────────────

    @app_commands.command(
        name="ticket_bearbeiten",
        description="Bearbeite das Ticket-System (Module, Kanäle, Kategorie, Panel)",
    )
    async def ticket_bearbeiten(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        from .ticket_edit_views import TicketEditMainView

        supabase = get_supabase()
        srv = supabase.table("ticket_servers").select("server_id")\
            .eq("server_id", str(interaction.guild_id)).execute()
        if not srv.data:
            await interaction.response.send_message(
                "❌ Das Ticket-System ist noch nicht eingerichtet. Nutze zuerst `/ticket_setup`.",
                ephemeral=True,
            )
            return

        view = TicketEditMainView(guild_id=interaction.guild_id, bot=self.bot)
        view._original_interaction = interaction
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    # ── /ticket_fuer ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="ticket_fuer",
        description="[Staff] Erstelle ein Ticket im Namen eines anderen Mitglieds",
    )
    @app_commands.describe(
        mitglied="Für welches Mitglied soll das Ticket erstellt werden?",
        modul="In welchem Modul soll das Ticket erstellt werden? (Nur Module auf die du Staff-Zugriff hast)",
    )
    async def ticket_fuer(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        modul: str,
    ):
        from .views import ProxyTicketModal

        server_id = str(interaction.guild_id)

        # Sich selbst kann man nicht vertreten – dann einfach normal /ticket nutzen
        if mitglied.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Du kannst kein Ticket im Namen von dir selbst erstellen. Nutze dafür das normale Panel.",
                ephemeral=True,
            )
            return

        # Modul aus DB laden
        try:
            supabase = get_supabase()
            mods = (
                supabase.table("ticket_modules")
                .select("*")
                .eq("server_id", server_id)
                .eq("name", modul)
                .execute()
                .data
            )
            if not mods:
                await interaction.response.send_message(
                    f"❌ Modul **{modul}** nicht gefunden. Nutze die Autocomplete-Liste.",
                    ephemeral=True,
                )
                return
            mod_id = mods[0]["id"]
            module = await TicketManager.get_module(mod_id)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Laden des Moduls: {e}", ephemeral=True)
            return

        if not module:
            await interaction.response.send_message("❌ Modul konnte nicht geladen werden.", ephemeral=True)
            return

        # Staff-Prüfung: nur Staff für *dieses* Modul oder Admin darf den Command nutzen
        staff_role_ids = set(module.get("staff_role_ids", []))
        user_role_ids  = {str(r.id) for r in interaction.user.roles}
        is_admin       = interaction.user.guild_permissions.administrator

        if not is_admin and not (staff_role_ids & user_role_ids):
            await interaction.response.send_message(
                f"❌ Du hast keinen Staff-Zugang für das Modul **{module['name']}**.\n"
                f"Nur Staff-Mitglieder dieses Moduls können Tickets dafür erstellen.",
                ephemeral=True,
            )
            return

        # Server-Config für globale Kategorie
        server_cfg = await TicketManager.get_server_config(server_id)
        global_cat = int(server_cfg["category_id"]) if server_cfg and server_cfg.get("category_id") else 0

        # Modal öffnen – Staff füllt es aus
        modal = ProxyTicketModal(
            module=module,
            category_id=global_cat,
            bot=self.bot,
            behalf_of=mitglied,
        )
        await interaction.response.send_modal(modal)

    @ticket_fuer.autocomplete("modul")
    async def ticket_fuer_modul_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[str]]:
        """
        Zeigt nur Module an, für die der ausführende User Staff-Zugang hat.
        Admins sehen alle Module.
        """
        try:
            supabase      = get_supabase()
            server_id     = str(interaction.guild_id)
            user_role_ids = {str(r.id) for r in interaction.user.roles}
            is_admin      = interaction.user.guild_permissions.administrator

            mods = (
                supabase.table("ticket_modules")
                .select("id, name, button_emoji")
                .eq("server_id", server_id)
                .execute()
                .data
                or []
            )

            choices = []
            for m in mods:
                # Filtern nach Suchbegriff
                if current and current.lower() not in m["name"].lower():
                    continue

                # Staff-Check pro Modul (Admins überspringen)
                if not is_admin:
                    roles = (
                        supabase.table("ticket_module_roles")
                        .select("role_id")
                        .eq("module_id", m["id"])
                        .execute()
                        .data
                        or []
                    )
                    mod_role_ids = {r["role_id"] for r in roles}
                    if not (mod_role_ids & user_role_ids):
                        continue  # kein Staff-Zugriff → nicht anzeigen

                emoji = m.get("button_emoji") or ""
                # Benutzerdefinierte Emojis (<:name:id>) im Autocomplete-Label weglassen
                if emoji.startswith("<"):
                    emoji = "🎫"
                label = f"{emoji} {m['name']}".strip()[:100]
                choices.append(app_commands.Choice(name=label, value=m["name"]))

            return choices[:25]
        except Exception as e:
            logger.error(f"[ticket_fuer_autocomplete] {e}")
            return []

    # ── /ticket_info ──────────────────────────────────────────────────────────

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
                embed.add_field(name="📢 Panel-Kanal",
                                value=f"<#{s.get('panel_channel_id')}>" if s.get("panel_channel_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="📁 Kategorie",
                                value=f"<#{s.get('category_id')}>" if s.get("category_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="📋 Log-Kanal",
                                value=f"<#{s.get('log_channel_id')}>" if s.get("log_channel_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="🔔 Staff-Ping Kanal",
                                value=f"<#{s.get('staff_ping_channel_id')}>" if s.get("staff_ping_channel_id") else "*nicht gesetzt*", inline=True)
                embed.add_field(name="🔢 Tickets gesamt",
                                value=str(s.get("ticket_counter", 0)), inline=True)
            else:
                embed.add_field(name="Status", value="❌ Nicht eingerichtet. Nutze `/ticket_setup`.", inline=False)

            if modules.data:
                for mod in modules.data:
                    cat = f"<#{mod['category_id']}>" if mod.get("category_id") else "*global*"
                    embed.add_field(
                        name=f"📂 {mod['name']}",
                        value=f"Max/User: {mod['max_tickets']} | Kategorie: {cat}",
                        inline=True,
                    )

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