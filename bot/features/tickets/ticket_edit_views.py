"""
ticket_edit_views.py  –  /ticket_bearbeiten Command Views
==========================================================
Ermöglicht das nachträgliche Bearbeiten des Ticket-Systems:
  • Server-Einstellungen ändern (Kanäle, Kategorie)
  • Module bearbeiten (Name, Beschreibung, Max-Tickets, Frage, Kategorie, Emoji, Staff-Rollen)
  • Module löschen
  • Neue Module hinzufügen
  • Panel-Nachricht direkt bearbeiten (kein Neu-Senden)
"""

from __future__ import annotations

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("tickets.edit")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_emoji(value: str) -> discord.PartialEmoji | str | None:
    """
    Parse emoji input from a TextInput.
    Supports unicode emoji (🎫) and custom emoji (<:name:id> / <a:name:id>).
    Returns None if blank or unparseable.
    """
    v = value.strip()
    if not v:
        return None
    if v.startswith("<") and v.endswith(">"):
        try:
            animated = v.startswith("<a:")
            inner    = v[3:].rstrip(">") if animated else v[2:].rstrip(">")
            parts    = inner.rsplit(":", 1)
            name     = parts[0]
            emoji_id = int(parts[1])
            return discord.PartialEmoji(name=name, id=emoji_id, animated=animated)
        except Exception:
            return None
    return v   # plain unicode string


def _server_overview_embed(server: dict, modules: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="✏️ Ticket-System bearbeiten",
        color=discord.Color.blurple(),
        description="Wähle unten, was du bearbeiten möchtest.",
    )
    embed.add_field(
        name="📢 Panel-Kanal",
        value=f"<#{server.get('panel_channel_id')}>" if server.get("panel_channel_id") else "*nicht gesetzt*",
        inline=True,
    )
    embed.add_field(
        name="📁 Standard-Kategorie",
        value=f"<#{server.get('category_id')}>" if server.get("category_id") else "*nicht gesetzt*",
        inline=True,
    )
    embed.add_field(
        name="📋 Log-Kanal",
        value=f"<#{server.get('log_channel_id')}>" if server.get("log_channel_id") else "*nicht gesetzt*",
        inline=True,
    )
    embed.add_field(
        name="🔔 Staff-Ping Kanal",
        value=f"<#{server.get('staff_ping_channel_id')}>" if server.get("staff_ping_channel_id") else "*nicht gesetzt*",
        inline=True,
    )
    embed.add_field(name="\u200b", value="\u200b", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if modules:
        for mod in modules:
            roles = ", ".join(f"<@&{r['role_id']}>" for r in mod.get("roles", [])) or "*keine*"
            cat   = f"<#{mod['category_id']}>" if mod.get("category_id") else "*global*"
            emoji = mod.get("button_emoji") or "🎫"
            embed.add_field(
                name=f"{emoji} {mod['name']}",
                value=(
                    f"Beschreibung: {mod['description'][:80]}\n"
                    f"Max Tickets: {mod['max_tickets']}\n"
                    f"Kategorie: {cat}\n"
                    f"Staff: {roles[:200]}"
                ),
                inline=False,
            )
    else:
        embed.add_field(name="📂 Module", value="*Keine Module vorhanden*", inline=False)

    return embed


# ══════════════════════════════════════════════════════════════════════════════
# MAIN EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class TicketEditMainView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self._original_interaction: discord.Interaction | None = None
        self._rebuild()

    def _load_data(self) -> tuple[dict | None, list[dict]]:
        supabase = get_supabase()
        srv = supabase.table("ticket_servers").select("*").eq("server_id", self.guild_id).execute()
        server = srv.data[0] if srv.data else None
        mods_raw = supabase.table("ticket_modules").select("*").eq("server_id", self.guild_id).execute().data or []
        modules = []
        for mod in mods_raw:
            roles = supabase.table("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute()
            mod["roles"] = roles.data or []
            modules.append(mod)
        return server, modules

    def build_embed(self) -> discord.Embed:
        server, modules = self._load_data()
        if not server:
            return discord.Embed(
                title="❌ Nicht eingerichtet",
                description="Nutze `/ticket_setup` um das Ticket-System zuerst einzurichten.",
                color=discord.Color.red(),
            )
        return _server_overview_embed(server, modules)

    def _rebuild(self):
        self.clear_items()

        btn_server = discord.ui.Button(label="⚙️ Server-Einstellungen",
                                       style=discord.ButtonStyle.primary, row=0)
        btn_server.callback = self._cb_server_settings
        self.add_item(btn_server)

        btn_modules = discord.ui.Button(label="📂 Modul bearbeiten",
                                        style=discord.ButtonStyle.primary, row=0)
        btn_modules.callback = self._cb_edit_module
        self.add_item(btn_modules)

        btn_add = discord.ui.Button(label="➕ Modul hinzufügen",
                                    style=discord.ButtonStyle.success, row=0)
        btn_add.callback = self._cb_add_module
        self.add_item(btn_add)

        btn_del = discord.ui.Button(label="🗑️ Modul löschen",
                                    style=discord.ButtonStyle.danger, row=1)
        btn_del.callback = self._cb_delete_module
        self.add_item(btn_del)

        btn_panel = discord.ui.Button(label="✏️ Panel bearbeiten",
                                      style=discord.ButtonStyle.secondary, row=1)
        btn_panel.callback = self._cb_edit_panel
        self.add_item(btn_panel)

    async def _cb_server_settings(self, interaction: discord.Interaction):
        server, _ = self._load_data()
        if not server:
            await interaction.response.send_message("❌ Ticket-System nicht eingerichtet.", ephemeral=True)
            return
        view = ServerSettingsView(server=server, parent=self)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    async def _cb_edit_module(self, interaction: discord.Interaction):
        _, modules = self._load_data()
        if not modules:
            await interaction.response.send_message("❌ Keine Module vorhanden.", ephemeral=True)
            return
        view = ModuleSelectView(modules=modules, action="edit", parent=self)
        await interaction.response.send_message(
            embed=discord.Embed(title="📂 Modul auswählen",
                                description="Welches Modul möchtest du bearbeiten?",
                                color=discord.Color.blurple()),
            view=view, ephemeral=True,
        )

    async def _cb_add_module(self, interaction: discord.Interaction):
        from .setup_views import AddTicketModuleModal
        shim = _AddModuleShim(guild_id=self.guild_id, parent=self)
        await interaction.response.send_modal(AddTicketModuleModal(shim))

    async def _cb_delete_module(self, interaction: discord.Interaction):
        _, modules = self._load_data()
        if not modules:
            await interaction.response.send_message("❌ Keine Module vorhanden.", ephemeral=True)
            return
        view = ModuleSelectView(modules=modules, action="delete", parent=self)
        await interaction.response.send_message(
            embed=discord.Embed(title="🗑️ Modul löschen",
                                description="Welches Modul soll gelöscht werden?",
                                color=discord.Color.red()),
            view=view, ephemeral=True,
        )

    async def _cb_edit_panel(self, interaction: discord.Interaction):
        server, modules = self._load_data()
        if not server or not server.get("panel_channel_id"):
            await interaction.response.send_message("❌ Panel-Kanal nicht konfiguriert.", ephemeral=True)
            return
        if not modules:
            await interaction.response.send_message("❌ Keine Module vorhanden.", ephemeral=True)
            return
        if not server.get("panel_message_id"):
            await interaction.response.send_message(
                "❌ Keine Panel-Nachricht-ID gespeichert.\n"
                "Bitte richte das System einmal neu ein mit `/ticket_setup`.",
                ephemeral=True,
            )
            return
        view = PanelEditView(server=server, modules=modules, bot=self.bot, parent=self)
        await interaction.response.send_message(
            embed=view.build_preview_embed(), view=view, ephemeral=True,
        )

    async def refresh(self):
        if self._original_interaction:
            try:
                await self._original_interaction.edit_original_response(
                    embed=self.build_embed(), view=self
                )
            except Exception as e:
                logger.error(f"[TicketEditMainView.refresh] {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SHIM
# ══════════════════════════════════════════════════════════════════════════════

class _AddModuleShim:
    def __init__(self, guild_id: str, parent: TicketEditMainView):
        self.guild_id              = guild_id
        self.pending_modules: list = []
        self._original_interaction = parent._original_interaction
        self._parent               = parent

    def _rebuild(self):
        pass

    def _build_embed(self):
        return self._parent.build_embed()

    async def _flush_module(self, mod: dict):
        supabase = get_supabase()
        result = supabase.table("ticket_modules").insert({
            "server_id":      self.guild_id,
            "name":           mod["name"],
            "description":    mod["description"],
            "max_tickets":    mod["max_tickets"],
            "modal_question": mod["modal_question"],
            "category_id":    mod.get("category_id"),
            "button_emoji":   mod.get("button_emoji", "🎫"),
        }).execute()
        if result.data:
            mod_id = result.data[0]["id"]
            for role_id in mod.get("staff_role_ids", []):
                supabase.table("ticket_module_roles").insert({
                    "module_id": mod_id, "role_id": str(role_id)
                }).execute()
        await self._parent.refresh()

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)
        if name == "pending_modules" and isinstance(value, list) and value:
            import asyncio
            asyncio.ensure_future(self._flush_module(value[-1]))


# ══════════════════════════════════════════════════════════════════════════════
# SERVER SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

class ServerSettingsView(discord.ui.View):
    def __init__(self, server: dict, parent: TicketEditMainView):
        super().__init__(timeout=180)
        self.server = dict(server)
        self.parent = parent
        self._build()

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(title="⚙️ Server-Einstellungen bearbeiten", color=discord.Color.blurple())
        e.add_field(name="📢 Panel-Kanal",
                    value=f"<#{self.server.get('panel_channel_id')}>" if self.server.get("panel_channel_id") else "*nicht gesetzt*", inline=True)
        e.add_field(name="📁 Standard-Kategorie",
                    value=f"<#{self.server.get('category_id')}>" if self.server.get("category_id") else "*nicht gesetzt*", inline=True)
        e.add_field(name="📋 Log-Kanal",
                    value=f"<#{self.server.get('log_channel_id')}>" if self.server.get("log_channel_id") else "*nicht gesetzt*", inline=True)
        e.add_field(name="🔔 Staff-Ping Kanal",
                    value=f"<#{self.server.get('staff_ping_channel_id')}>" if self.server.get("staff_ping_channel_id") else "*nicht gesetzt*", inline=True)
        return e

    def _build(self):
        self.clear_items()

        ch = discord.ui.ChannelSelect(placeholder="📢 Panel-Kanal ändern", min_values=1, max_values=1,
                                      channel_types=[discord.ChannelType.text], row=0)
        async def _ch(i): self.server["panel_channel_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        ch.callback = _ch; self.add_item(ch)

        cat = discord.ui.ChannelSelect(placeholder="📁 Standard-Kategorie ändern", min_values=1, max_values=1,
                                       channel_types=[discord.ChannelType.category], row=1)
        async def _cat(i): self.server["category_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        cat.callback = _cat; self.add_item(cat)

        log = discord.ui.ChannelSelect(placeholder="📋 Log-Kanal ändern", min_values=1, max_values=1,
                                       channel_types=[discord.ChannelType.text], row=2)
        async def _log(i): self.server["log_channel_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        log.callback = _log; self.add_item(log)

        ping = discord.ui.ChannelSelect(placeholder="🔔 Staff-Ping Kanal ändern", min_values=1, max_values=1,
                                        channel_types=[discord.ChannelType.text], row=3)
        async def _ping(i): self.server["staff_ping_channel_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        ping.callback = _ping; self.add_item(ping)

        save = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success, row=4)
        save.callback = self._save; self.add_item(save)

    async def _save(self, interaction: discord.Interaction):
        try:
            get_supabase().table("ticket_servers").update({
                "panel_channel_id":      self.server.get("panel_channel_id"),
                "category_id":           self.server.get("category_id"),
                "log_channel_id":        self.server.get("log_channel_id"),
                "staff_ping_channel_id": self.server.get("staff_ping_channel_id"),
            }).eq("server_id", self.parent.guild_id).execute()
            embed = self.build_embed()
            embed.title = "✅ Server-Einstellungen gespeichert!"
            embed.color = discord.Color.green()
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# MODULE SELECT
# ══════════════════════════════════════════════════════════════════════════════

class ModuleSelectView(discord.ui.View):
    def __init__(self, modules: list[dict], action: str, parent: TicketEditMainView):
        super().__init__(timeout=120)
        self.modules = modules
        self.action  = action
        self.parent  = parent

        options = [
            discord.SelectOption(
                label=mod["name"][:100],
                description=(mod.get("description") or "")[:50],
                value=str(mod["id"]),
                emoji=mod.get("button_emoji") or "📂",
            )
            for mod in modules[:25]
        ]
        sel = discord.ui.Select(placeholder="Modul auswählen…", options=options)
        sel.callback = self._selected
        self.add_item(sel)

    async def _selected(self, interaction: discord.Interaction):
        mod_id = int(interaction.data["values"][0])
        mod    = next((m for m in self.modules if m["id"] == mod_id), None)
        if not mod:
            await interaction.response.send_message("❌ Modul nicht gefunden.", ephemeral=True)
            return
        if self.action == "edit":
            view = ModuleEditView(module=mod, parent=self.parent)
            await interaction.response.edit_message(embed=view.build_embed(), view=view)
        elif self.action == "delete":
            view = ModuleDeleteConfirmView(module=mod, parent=self.parent)
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"🗑️ Modul «{mod['name']}» löschen?",
                    description="⚠️ Das Modul wird dauerhaft gelöscht.\nBestehende Tickets bleiben erhalten.",
                    color=discord.Color.red(),
                ),
                view=view,
            )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ModuleEditView(discord.ui.View):
    def __init__(self, module: dict, parent: TicketEditMainView):
        super().__init__(timeout=300)
        self.module = dict(module)
        self.parent = parent
        self._build()

    def build_embed(self) -> discord.Embed:
        roles = ", ".join(f"<@&{r['role_id']}>" for r in self.module.get("roles", [])) or "*keine*"
        cat   = f"<#{self.module['category_id']}>" if self.module.get("category_id") else "*global (Standard)*"
        emoji = self.module.get("button_emoji") or "🎫"
        e = discord.Embed(
            title=f"✏️ Modul bearbeiten: {emoji} {self.module['name']}",
            color=discord.Color.blurple(),
        )
        e.add_field(name="📛 Name",           value=self.module["name"],            inline=True)
        e.add_field(name="🔢 Max Tickets",    value=str(self.module["max_tickets"]), inline=True)
        e.add_field(name="😀 Button-Emoji",   value=emoji,                          inline=True)
        e.add_field(name="📁 Kategorie",      value=cat,                            inline=True)
        e.add_field(name="\u200b",            value="\u200b",                       inline=True)
        e.add_field(name="\u200b",            value="\u200b",                       inline=True)
        e.add_field(name="📝 Beschreibung",   value=self.module.get("description", "–")[:200], inline=False)
        e.add_field(name="❓ Modal-Anweisung", value=self.module.get("modal_question", "–")[:200], inline=False)
        e.add_field(name="👮 Staff-Rollen",   value=roles[:400], inline=False)
        return e

    def _build(self):
        self.clear_items()

        btn_text = discord.ui.Button(label="✏️ Texte & Emoji bearbeiten",
                                     style=discord.ButtonStyle.primary, row=0)
        btn_text.callback = self._cb_text
        self.add_item(btn_text)

        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Eigene Kategorie ändern (leer = global)",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.category], row=1,
        )
        cat_sel.callback = self._cb_category
        self.add_item(cat_sel)

        role_sel = discord.ui.RoleSelect(
            placeholder="👮 Staff-Rollen neu setzen",
            min_values=1, max_values=10, row=2,
        )
        role_sel.callback = self._cb_roles
        self.add_item(role_sel)

        btn_save = discord.ui.Button(label="💾 Alle Änderungen speichern",
                                     style=discord.ButtonStyle.success, row=3)
        btn_save.callback = self._cb_save
        self.add_item(btn_save)

        btn_back = discord.ui.Button(label="← Zurück",
                                     style=discord.ButtonStyle.secondary, row=3)
        btn_back.callback = self._cb_back
        self.add_item(btn_back)

    async def _cb_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ModuleTextEditModal(module=self.module, view=self))

    async def _cb_category(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.module["category_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _cb_roles(self, interaction: discord.Interaction):
        self.module["_new_role_ids"] = interaction.data["values"]
        self.module["roles"] = [{"role_id": r} for r in self.module["_new_role_ids"]]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _cb_save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase  = get_supabase()
            module_id = self.module["id"]
            supabase.table("ticket_modules").update({
                "name":           self.module["name"],
                "description":    self.module.get("description", ""),
                "max_tickets":    self.module.get("max_tickets", 1),
                "modal_question": self.module.get("modal_question", ""),
                "category_id":    self.module.get("category_id"),
                "button_emoji":   self.module.get("button_emoji", "🎫"),
            }).eq("id", module_id).execute()

            if "_new_role_ids" in self.module:
                supabase.table("ticket_module_roles").delete().eq("module_id", module_id).execute()
                for role_id in self.module["_new_role_ids"]:
                    supabase.table("ticket_module_roles").insert({
                        "module_id": module_id, "role_id": str(role_id)
                    }).execute()
                del self.module["_new_role_ids"]

            for item in self.children: item.disabled = True
            embed = self.build_embed()
            embed.title = f"✅ Modul «{self.module['name']}» gespeichert!"
            embed.color = discord.Color.green()
            await interaction.edit_original_response(content=None)
            await interaction.followup.send(embed=embed, ephemeral=True)
            await self.parent.refresh()
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    async def _cb_back(self, interaction: discord.Interaction):
        _, modules = self.parent._load_data()
        view = ModuleSelectView(modules=modules, action="edit", parent=self.parent)
        await interaction.response.edit_message(
            embed=discord.Embed(title="📂 Modul auswählen",
                                description="Welches Modul möchtest du bearbeiten?",
                                color=discord.Color.blurple()),
            view=view,
        )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE TEXT + EMOJI EDIT MODAL
# ══════════════════════════════════════════════════════════════════════════════

class ModuleTextEditModal(discord.ui.Modal, title="Modul bearbeiten"):
    mod_name     = discord.ui.TextInput(label="Name", required=True, max_length=50)
    mod_desc     = discord.ui.TextInput(label="Beschreibung", required=True, max_length=200,
                                        style=discord.TextStyle.paragraph)
    mod_max      = discord.ui.TextInput(label="Max. Tickets pro User", required=True, max_length=2)
    mod_question = discord.ui.TextInput(label="Modal-Anweisung", required=True, max_length=300,
                                        style=discord.TextStyle.paragraph)
    mod_emoji    = discord.ui.TextInput(
        label="Button-Emoji  (Unicode oder <:name:id>)",
        placeholder="🎫  oder  <:support:123456789>  oder leer lassen",
        required=False, max_length=100,
    )

    def __init__(self, module: dict, view: "ModuleEditView"):
        super().__init__()
        self.mod_view             = view
        self.mod_name.default     = module.get("name", "")
        self.mod_desc.default     = module.get("description", "")
        self.mod_max.default      = str(module.get("max_tickets", 1))
        self.mod_question.default = module.get("modal_question", "")
        self.mod_emoji.default    = module.get("button_emoji", "🎫")

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_t = int(self.mod_max.value)
        except ValueError:
            max_t = 1

        raw_emoji = self.mod_emoji.value.strip() or "🎫"
        # validate – if custom emoji syntax is broken, fall back
        if raw_emoji.startswith("<"):
            if _parse_emoji(raw_emoji) is None:
                raw_emoji = "🎫"

        self.mod_view.module["name"]           = self.mod_name.value
        self.mod_view.module["description"]    = self.mod_desc.value
        self.mod_view.module["max_tickets"]    = max_t
        self.mod_view.module["modal_question"] = self.mod_question.value
        self.mod_view.module["button_emoji"]   = raw_emoji
        await interaction.response.edit_message(embed=self.mod_view.build_embed(), view=self.mod_view)


# ══════════════════════════════════════════════════════════════════════════════
# PANEL EDIT VIEW  –  edits existing panel message in place
# ══════════════════════════════════════════════════════════════════════════════

class PanelEditView(discord.ui.View):
    """
    Allows editing the existing panel message directly.
    Changes title/description via modal, then patches the live Discord message.
    No new message is sent – the panel stays in its original position.
    """

    def __init__(self, server: dict, modules: list[dict], bot: discord.Client,
                 parent: TicketEditMainView):
        super().__init__(timeout=180)
        self.server  = server
        self.modules = modules
        self.bot     = bot
        self.parent  = parent
        # Start with sensible defaults; user can change them
        self._panel_title = "🎫 Support-Tickets"
        self._panel_desc  = "Wähle ein Modul aus den Buttons unten um ein Ticket zu erstellen."
        self._build()

    def build_preview_embed(self) -> discord.Embed:
        """Admin-side preview embed showing how the panel will look."""
        embed = discord.Embed(
            title="✏️ Panel bearbeiten",
            description=(
                "**Vorschau der Panel-Nachricht:**\n\n"
                f"**{self._panel_title}**\n"
                f"{self._panel_desc}"
            ),
            color=discord.Color.blurple(),
        )
        for mod in self.modules:
            emoji = mod.get("button_emoji") or "🎫"
            embed.add_field(
                name=f"{emoji} {mod['name']}",
                value=mod.get("description") or "–",
                inline=False,
            )
        embed.set_footer(text="Klicke 'Änderungen übernehmen' um die Panel-Nachricht zu aktualisieren.")
        return embed

    def _build(self):
        self.clear_items()

        btn_text = discord.ui.Button(label="✏️ Titel & Text bearbeiten",
                                     style=discord.ButtonStyle.primary, row=0)
        btn_text.callback = self._cb_edit_text
        self.add_item(btn_text)

        btn_apply = discord.ui.Button(label="✅ Änderungen übernehmen",
                                      style=discord.ButtonStyle.success, row=0)
        btn_apply.callback = self._cb_apply
        self.add_item(btn_apply)

    async def _cb_edit_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            PanelTextEditModal(
                current_title=self._panel_title,
                current_desc=self._panel_desc,
                panel_view=self,
            )
        )

    async def _cb_apply(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            panel_channel = self.bot.get_channel(int(self.server["panel_channel_id"]))
            if not panel_channel:
                await interaction.followup.send("❌ Panel-Kanal nicht gefunden.", ephemeral=True)
                return

            panel_msg_id = self.server.get("panel_message_id")
            if not panel_msg_id:
                await interaction.followup.send(
                    "❌ Keine Panel-Nachrichten-ID gespeichert.\n"
                    "Bitte richte das System neu ein mit `/ticket_setup`.",
                    ephemeral=True,
                )
                return

            try:
                panel_msg = await panel_channel.fetch_message(int(panel_msg_id))
            except discord.NotFound:
                await interaction.followup.send(
                    "❌ Die Panel-Nachricht wurde nicht gefunden (evtl. gelöscht).\n"
                    "Nutze `/ticket_setup` um das Panel neu zu senden.",
                    ephemeral=True,
                )
                return

            # Build the updated panel embed – NO category note, just name + description
            new_embed = discord.Embed(
                title=self._panel_title,
                description=self._panel_desc,
                color=discord.Color.blurple(),
            )
            for mod in self.modules:
                emoji = mod.get("button_emoji") or "🎫"
                new_embed.add_field(
                    name=f"{emoji} {mod['name']}",
                    value=mod.get("description") or "–",
                    inline=False,
                )

            # Rebuild the view so button emojis are up to date
            from .views import TicketPanelView
            panel_view = TicketPanelView(
                modules=self.modules,
                category_id=int(self.server.get("category_id", 0)),
                bot=self.bot,
            )

            # Edit the existing message IN PLACE
            await panel_msg.edit(embed=new_embed, view=panel_view)

            for item in self.children:
                item.disabled = True
            await interaction.followup.send("✅ Panel-Nachricht wurde aktualisiert!", ephemeral=True)
        except Exception as e:
            logger.error(f"[PanelEditView._cb_apply] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class PanelTextEditModal(discord.ui.Modal, title="Panel-Text bearbeiten"):
    panel_title = discord.ui.TextInput(
        label="Panel-Titel",
        placeholder="z.B. 🎫 Support-Tickets",
        required=True, max_length=100,
    )
    panel_desc = discord.ui.TextInput(
        label="Panel-Beschreibung",
        placeholder="z.B. Wähle ein Modul aus den Buttons unten…",
        style=discord.TextStyle.paragraph,
        required=True, max_length=500,
    )

    def __init__(self, current_title: str, current_desc: str, panel_view: "PanelEditView"):
        super().__init__()
        self.panel_view          = panel_view
        self.panel_title.default = current_title
        self.panel_desc.default  = current_desc

    async def on_submit(self, interaction: discord.Interaction):
        self.panel_view._panel_title = self.panel_title.value
        self.panel_view._panel_desc  = self.panel_desc.value
        await interaction.response.edit_message(
            embed=self.panel_view.build_preview_embed(),
            view=self.panel_view,
        )


# ══════════════════════════════════════════════════════════════════════════════
# MODULE DELETE CONFIRM
# ══════════════════════════════════════════════════════════════════════════════

class ModuleDeleteConfirmView(discord.ui.View):
    def __init__(self, module: dict, parent: TicketEditMainView):
        super().__init__(timeout=60)
        self.module = module
        self.parent = parent

    @discord.ui.button(label="🗑️ Ja, löschen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase  = get_supabase()
            module_id = self.module["id"]
            supabase.table("ticket_module_roles").delete().eq("module_id", module_id).execute()
            supabase.table("ticket_modules").delete().eq("id", module_id).execute()
            self.stop()
            await interaction.followup.send(
                f"✅ Modul **{self.module['name']}** wurde gelöscht.", ephemeral=True
            )
            await self.parent.refresh()
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            embed=discord.Embed(title="Abgebrochen",
                                description="Das Modul wurde nicht gelöscht.",
                                color=discord.Color.green()),
            view=None,
        )
        self.stop()