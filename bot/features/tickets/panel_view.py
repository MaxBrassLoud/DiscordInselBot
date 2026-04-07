"""
bot/features/tickets/panel_view.py
====================================
Ersetzt TicketPanelView in views.py durch ein System das genauso
funktioniert wie ApplicationPanelView:

  • Eine EINZIGE persistente View mit custom_id="ticket_panel_select"
  • Beim Klick: StringSelect mit allen Modulen des Servers → User wählt Modul
  • Dann: Modal mit Beschreibung öffnen → Ticket erstellen

WARUM:
  Der alte Ansatz hatte custom_id="ticket_open_<module_id>" pro Button.
  Das funktioniert nicht zuverlässig weil:
  1. Nach Bot-Neustart sind die Views nicht mehr registriert
  2. Beim Web-Setup kennt der Bot die module_ids nicht zur Laufzeit
  3. Discord.py braucht exakt übereinstimmende custom_ids beim add_view()

DER NEUE ANSATZ (wie Applications):
  custom_id="ticket_panel_select" → immer gleich → immer registriert
  Beim Klick wird die DB abgefragt → kein Neustart nötig

INTEGRATION:
  1. Diese Datei nach bot/features/tickets/panel_view.py kopieren
  2. In cog.py on_ready:
       from .panel_view import TicketPanelView
       self.bot.add_view(TicketPanelView(bot=self.bot))
  3. In views.py: TicketPanelView durch die neue Version aus panel_view.py ersetzen
  4. In setup_views.py und setup_wizard.py: Import von views.TicketPanelView
     auf panel_view.TicketPanelView umstellen
  5. In ticket_setup_routes.py: custom_id im Panel-Button auf
     "ticket_panel_select" setzen (bereits korrekt unten dokumentiert)
"""

from __future__ import annotations

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("tickets.panel")


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENTE PANEL VIEW
# ══════════════════════════════════════════════════════════════════════════════

class TicketPanelView(discord.ui.View):
    """
    Persistente Panel-View mit einer EINZIGEN statischen custom_id.
    Funktioniert nach Bot-Neustart ohne re-registrierung der Module.
    Identisch zum Mechanismus von ApplicationPanelView.
    """

    def __init__(self, bot: discord.Client):
        super().__init__(timeout=None)
        self.bot = bot

        btn = discord.ui.Button(
            label="🎫 Ticket erstellen",
            style=discord.ButtonStyle.primary,
            custom_id="ticket_panel_open",   # STATISCHE ID — immer gleich
            emoji="🎫",
        )
        btn.callback = self._cb_open
        self.add_item(btn)

    async def _cb_open(self, interaction: discord.Interaction):
        """
        Lädt die Module des Servers live aus der DB und zeigt ein
        StringSelect-Dropdown an. Kein Neustart nötig wenn Module geändert werden.
        """
        server_id = str(interaction.guild_id)
        try:
            supabase = get_supabase()
            mods_raw = (
                supabase.table("ticket_modules")
                .select("*")
                .eq("server_id", server_id)
                .execute()
                .data or []
            )
        except Exception as e:
            logger.error(f"[TicketPanelView] DB-Fehler: {e}")
            await interaction.response.send_message(
                "❌ Ticket-System konnte nicht geladen werden. Bitte versuche es später.",
                ephemeral=True,
            )
            return

        if not mods_raw:
            await interaction.response.send_message(
                "❌ Keine Ticket-Module konfiguriert. Bitte kontaktiere einen Admin.",
                ephemeral=True,
            )
            return

        # Rollen für alle Module laden
        for mod in mods_raw:
            roles = (
                supabase.table("ticket_module_roles")
                .select("role_id")
                .eq("module_id", mod["id"])
                .execute()
                .data or []
            )
            mod["staff_role_ids"] = [r["role_id"] for r in roles]

        if len(mods_raw) == 1:
            # Nur ein Modul → direkt Modal öffnen, kein Select nötig
            mod = mods_raw[0]
            await _open_ticket_modal(interaction, mod, self.bot)
        else:
            # Mehrere Module → Select zeigen
            view = ModuleSelectView(modules=mods_raw, bot=self.bot)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="🎫 Ticket erstellen",
                    description="Wähle die Kategorie die zu deinem Anliegen passt:",
                    color=discord.Color.blurple(),
                ),
                view=view,
                ephemeral=True,
            )


# ══════════════════════════════════════════════════════════════════════════════
# MODUL-AUSWAHL (StringSelect)
# ══════════════════════════════════════════════════════════════════════════════

class ModuleSelectView(discord.ui.View):
    """
    Zeigt alle Module des Servers als Dropdown.
    Öffnet nach Auswahl das Ticket-Modal.
    """

    def __init__(self, modules: list[dict], bot: discord.Client):
        super().__init__(timeout=120)
        self.bot     = bot
        self.modules = {str(m["id"]): m for m in modules}

        options = []
        for mod in modules[:25]:  # Discord-Limit: 25 Optionen
            emoji_raw = mod.get("button_emoji") or "🎫"
            # Custom-Emoji (<:name:id>) können bei SelectOption nicht direkt genutzt werden
            # → nur Unicode-Emoji verwenden, sonst weglassen
            if emoji_raw.startswith("<"):
                emoji_raw = "🎫"
            options.append(discord.SelectOption(
                label=mod["name"][:100],
                description=(mod.get("description") or "")[:100],
                value=str(mod["id"]),
                emoji=emoji_raw,
            ))

        sel = discord.ui.Select(
            placeholder="Modul auswählen…",
            options=options,
            min_values=1,
            max_values=1,
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction):
        mod_id = interaction.data["values"][0]
        mod    = self.modules.get(mod_id)
        if not mod:
            await interaction.response.send_message("❌ Modul nicht gefunden.", ephemeral=True)
            return
        await _open_ticket_modal(interaction, mod, self.bot)


# ══════════════════════════════════════════════════════════════════════════════
# MODAL ÖFFNEN (shared helper)
# ══════════════════════════════════════════════════════════════════════════════

async def _open_ticket_modal(
    interaction: discord.Interaction,
    module: dict,
    bot: discord.Client,
) -> None:
    """
    Prüft ob der User bereits zu viele offene Tickets hat,
    dann öffnet das Beschreibungs-Modal.
    """
    from .manager import TicketManager

    server_id = str(interaction.guild_id)
    user_id   = str(interaction.user.id)

    max_tickets = module.get("max_tickets", 1)
    open_count  = await TicketManager.get_open_tickets_for_user(
        server_id, user_id, module["name"]
    )
    if open_count >= max_tickets:
        await interaction.response.send_message(
            f"❌ Du hast bereits **{open_count}/{max_tickets}** offene Tickets "
            f"für **{module['name']}**.",
            ephemeral=True,
        )
        return

    # Server-Config für category_id laden
    server_cfg = await TicketManager.get_server_config(server_id)
    category_id = int(server_cfg["category_id"]) if server_cfg and server_cfg.get("category_id") else 0

    modal = TicketDescriptionModal(module=module, category_id=category_id, bot=bot)
    await interaction.response.send_modal(modal)


# ══════════════════════════════════════════════════════════════════════════════
# TICKET-ERSTELLUNGS-MODAL (aus views.py übernommen, hier zentralisiert)
# ══════════════════════════════════════════════════════════════════════════════

class TicketDescriptionModal(discord.ui.Modal):
    beschreibung = discord.ui.TextInput(
        label="Beschreibung",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=1000,
    )

    def __init__(self, module: dict, category_id: int, bot: discord.Client):
        super().__init__(title=f"Ticket: {module['name'][:40]}")
        self.module      = module
        self.category_id = category_id
        self.bot         = bot
        self.beschreibung.placeholder = module.get(
            "modal_question", "Bitte beschreibe dein Anliegen."
        )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from .manager import TicketManager
            from .views import TicketChannelView

            channel, ticket_id = await TicketManager.create_ticket(
                guild=interaction.guild,
                creator=interaction.user,
                module=self.module,
                description=self.beschreibung.value,
                category_id=self.category_id,
            )

            embed = discord.Embed(
                title=f"🎫 Ticket #{ticket_id}",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="👤 Erstellt von", value=interaction.user.mention, inline=True)
            embed.add_field(name="📂 Modul",        value=self.module["name"],      inline=True)
            embed.add_field(name="📝 Beschreibung", value=self.beschreibung.value,  inline=False)

            view = TicketChannelView(
                ticket_id=ticket_id,
                server_id=str(interaction.guild_id),
                creator_id=str(interaction.user.id),
                module=self.module,
                bot=self.bot,
            )
            await channel.send(embed=embed, view=view)
            await interaction.followup.send(
                f"✅ Ticket erstellt: {channel.mention}", ephemeral=True
            )
        except Exception as e:
            logger.error(f"[TicketDescriptionModal] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# PANEL-EMBED BUILDER (für Setup-Wizard und Web-Setup)
# ══════════════════════════════════════════════════════════════════════════════

def build_ticket_panel_embed(
    modules: list[dict],
    title: str = "🎫 Support-Tickets",
    description: str = "Klicke auf den Button um ein Ticket zu erstellen.",
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
    )
    for mod in modules:
        emoji = mod.get("button_emoji") or "🎫"
        if str(emoji).startswith("<"):
            emoji = "🎫"
        embed.add_field(
            name=f"{emoji} {mod['name']}",
            value=mod.get("description") or "–",
            inline=False,
        )
    return embed