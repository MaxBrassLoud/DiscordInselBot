"""
ticket_edit_views.py  –  /ticket_bearbeiten Command Views
==========================================================
ÄNDERUNGEN:
  - Panel-Kanal wechseln: alte Nachricht löschen, neue im neuen Kanal senden
  - Prominenter "💾 Alle Änderungen speichern"-Button in der Haupt-View
  - ServerSettingsView: Panel-Kanal-Wechsel mit automatischem Umzug der Panel-Nachricht
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
    return v


def _server_overview_embed(server: dict, modules: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title="✏️ Ticket-System bearbeiten",
        color=discord.Color.blurple(),
        description=(
            "Wähle unten, was du bearbeiten möchtest.\n"
            "Klicke **💾 Alle Änderungen speichern** um alle Einstellungen auf einmal zu sichern."
        ),
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
# PANEL UMZUG HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _move_panel_to_new_channel(
    bot: discord.Client,
    guild: discord.Guild,
    server: dict,
    new_channel_id: str,
    modules: list[dict],
) -> str | None:
    """
    Löscht die alte Panel-Nachricht und sendet eine neue im neuen Kanal.
    Gibt die neue message_id zurück oder None bei Fehler.
    """
    from .panel_view import TicketPanelView, build_ticket_panel_embed

    # Alte Nachricht löschen
    old_channel_id  = server.get("panel_channel_id")
    old_message_id  = server.get("panel_message_id")

    if old_channel_id and old_message_id:
        try:
            old_ch = guild.get_channel(int(old_channel_id))
            if old_ch:
                old_msg = await old_ch.fetch_message(int(old_message_id))
                await old_msg.delete()
                logger.info(f"[ticket_edit] Alte Panel-Nachricht gelöscht: {old_message_id}")
        except discord.NotFound:
            logger.info("[ticket_edit] Alte Panel-Nachricht nicht gefunden (evtl. bereits gelöscht)")
        except Exception as e:
            logger.warning(f"[ticket_edit] Alte Panel-Nachricht konnte nicht gelöscht werden: {e}")

    # Neue Nachricht im neuen Kanal senden
    new_ch = guild.get_channel(int(new_channel_id))
    if not new_ch:
        return None

    try:
        embed      = build_ticket_panel_embed(modules)
        panel_view = TicketPanelView(bot=bot)
        new_msg    = await new_ch.send(embed=embed, view=panel_view)
        logger.info(f"[ticket_edit] Neue Panel-Nachricht gesendet: {new_msg.id} in #{new_ch.name}")
        return str(new_msg.id)
    except Exception as e:
        logger.error(f"[ticket_edit] Panel-Nachricht senden fehlgeschlagen: {e}")
        return None


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

        # Reihe 0: Einzel-Aktionen
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

        # Reihe 1
        btn_del = discord.ui.Button(label="🗑️ Modul löschen",
                                    style=discord.ButtonStyle.danger, row=1)
        btn_del.callback = self._cb_delete_module
        self.add_item(btn_del)

        btn_panel = discord.ui.Button(label="✏️ Panel-Text bearbeiten",
                                      style=discord.ButtonStyle.secondary, row=1)
        btn_panel.callback = self._cb_edit_panel
        self.add_item(btn_panel)

        # Reihe 2: Prominenter Speichern-Button
        btn_save_all = discord.ui.Button(
            label="💾 Alle Änderungen speichern",
            style=discord.ButtonStyle.success,
            row=2,
            emoji="💾",
        )
        btn_save_all.callback = self._cb_save_all
        self.add_item(btn_save_all)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    async def _cb_server_settings(self, interaction: discord.Interaction):
        server, modules = self._load_data()
        if not server:
            await interaction.response.send_message("❌ Ticket-System nicht eingerichtet.", ephemeral=True)
            return
        view = ServerSettingsView(server=server, modules=modules, bot=self.bot, parent=self)
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

    async def _cb_save_all(self, interaction: discord.Interaction):
        """Speichert alle aktuellen Einstellungen aus der DB (lädt nochmals) – Bestätigungsbutton."""
        await interaction.response.defer(ephemeral=True)
        # Die DB ist die Quelle der Wahrheit – wir laden frisch und aktualisieren die Panel-Embed
        server, modules = self._load_data()
        if not server:
            await interaction.followup.send("❌ Keine Konfiguration gefunden.", ephemeral=True)
            return

        # Panel-Nachricht aktualisieren falls vorhanden
        updated_panel = False
        if server.get("panel_channel_id") and server.get("panel_message_id") and modules:
            try:
                from .panel_view import TicketPanelView, build_ticket_panel_embed
                ch = interaction.guild.get_channel(int(server["panel_channel_id"]))
                if ch:
                    msg = await ch.fetch_message(int(server["panel_message_id"]))
                    embed = build_ticket_panel_embed(modules)
                    await msg.edit(embed=embed, view=TicketPanelView(bot=self.bot))
                    updated_panel = True
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[save_all] Panel-Update: {e}")

        # Haupt-Übersicht aktualisieren
        await self.refresh()

        panel_hint = " Panel-Nachricht wurde ebenfalls aktualisiert." if updated_panel else ""
        await interaction.followup.send(
            f"✅ Alle Änderungen wurden gespeichert.{panel_hint}",
            ephemeral=True,
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
# SERVER SETTINGS (mit Panel-Kanal-Wechsel)
# ══════════════════════════════════════════════════════════════════════════════

class ServerSettingsView(discord.ui.View):
    def __init__(self, server: dict, modules: list[dict], bot: discord.Client, parent: TicketEditMainView):
        super().__init__(timeout=180)
        self.server  = dict(server)
        self.modules = modules
        self.bot     = bot
        self.parent  = parent
        self._panel_channel_changed = False
        self._build()

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="⚙️ Server-Einstellungen bearbeiten",
            color=discord.Color.blurple(),
            description=(
                "Wähle die Kanäle über die Dropdowns.\n"
                "⚠️ Bei einem **neuen Panel-Kanal** wird die alte Panel-Nachricht gelöscht "
                "und im neuen Kanal neu gesendet."
            ),
        )
        e.add_field(
            name="📢 Panel-Kanal",
            value=f"<#{self.server.get('panel_channel_id')}>" if self.server.get("panel_channel_id") else "*nicht gesetzt*",
            inline=True,
        )
        if self._panel_channel_changed:
            e.add_field(name="⚠️ Panel-Kanal", value="**Wird beim Speichern umgezogen!**", inline=True)
        e.add_field(
            name="📁 Standard-Kategorie",
            value=f"<#{self.server.get('category_id')}>" if self.server.get("category_id") else "*nicht gesetzt*",
            inline=True,
        )
        e.add_field(
            name="📋 Log-Kanal",
            value=f"<#{self.server.get('log_channel_id')}>" if self.server.get("log_channel_id") else "*nicht gesetzt*",
            inline=True,
        )
        e.add_field(
            name="🔔 Staff-Ping Kanal",
            value=f"<#{self.server.get('staff_ping_channel_id')}>" if self.server.get("staff_ping_channel_id") else "*nicht gesetzt*",
            inline=True,
        )
        return e

    def _build(self):
        self.clear_items()

        ch = discord.ui.ChannelSelect(
            placeholder="📢 Panel-Kanal ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text], row=0,
        )
        async def _ch(i):
            new_id = i.data["values"][0]
            if new_id != self.server.get("panel_channel_id"):
                self._panel_channel_changed = True
                self.server["_new_panel_channel_id"] = new_id
            self.server["panel_channel_id"] = new_id
            await i.response.edit_message(embed=self.build_embed(), view=self)
        ch.callback = _ch
        self.add_item(ch)

        cat = discord.ui.ChannelSelect(
            placeholder="📁 Standard-Kategorie ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.category], row=1,
        )
        async def _cat(i):
            self.server["category_id"] = i.data["values"][0]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        cat.callback = _cat
        self.add_item(cat)

        log = discord.ui.ChannelSelect(
            placeholder="📋 Log-Kanal ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text], row=2,
        )
        async def _log(i):
            self.server["log_channel_id"] = i.data["values"][0]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        log.callback = _log
        self.add_item(log)

        ping = discord.ui.ChannelSelect(
            placeholder="🔔 Staff-Ping Kanal ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text], row=3,
        )
        async def _ping(i):
            self.server["staff_ping_channel_id"] = i.data["values"][0]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        ping.callback = _ping
        self.add_item(ping)

        save = discord.ui.Button(
            label="💾 Speichern & Panel umziehen" if self._panel_channel_changed else "💾 Speichern",
            style=discord.ButtonStyle.success,
            row=4,
        )
        save.callback = self._save
        self.add_item(save)

    async def _save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()

            update_data = {
                "category_id":           self.server.get("category_id"),
                "log_channel_id":        self.server.get("log_channel_id"),
                "staff_ping_channel_id": self.server.get("staff_ping_channel_id"),
            }

            new_panel_msg_id = None

            # Panel-Kanal wechseln: alte Nachricht löschen, neue senden
            if self._panel_channel_changed:
                new_channel_id = self.server["panel_channel_id"]
                new_panel_msg_id = await _move_panel_to_new_channel(
                    bot=self.bot,
                    guild=interaction.guild,
                    server=self.server,
                    new_channel_id=new_channel_id,
                    modules=self.modules,
                )
                update_data["panel_channel_id"] = new_channel_id
                if new_panel_msg_id:
                    update_data["panel_message_id"] = new_panel_msg_id
            else:
                update_data["panel_channel_id"] = self.server.get("panel_channel_id")

            supabase.table("ticket_servers").update(update_data)\
                .eq("server_id", self.parent.guild_id).execute()

            embed = self.build_embed()
            embed.title = "✅ Server-Einstellungen gespeichert!"
            embed.color = discord.Color.green()
            if self._panel_channel_changed and new_panel_msg_id:
                embed.add_field(
                    name="✅ Panel-Nachricht umgezogen",
                    value=f"Neue Nachricht in <#{self.server['panel_channel_id']}>",
                    inline=False,
                )
            elif self._panel_channel_changed and not new_panel_msg_id:
                embed.add_field(
                    name="⚠️ Panel-Nachricht",
                    value="Kanal gespeichert, aber neue Panel-Nachricht konnte nicht gesendet werden. Nutze `/ticket_setup`.",
                    inline=False,
                )

            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            logger.error(f"[ServerSettingsView._save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


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

        btn_save = discord.ui.Button(
            label="💾 Alle Änderungen speichern",
            style=discord.ButtonStyle.success, row=3,
        )
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

            for item in self.children:
                item.disabled = True
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
# PANEL EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class PanelEditView(discord.ui.View):
    def __init__(self, server: dict, modules: list[dict], bot: discord.Client,
                 parent: TicketEditMainView):
        super().__init__(timeout=180)
        self.server  = server
        self.modules = modules
        self.bot     = bot
        self.parent  = parent
        self._panel_title = "🎫 Support-Tickets"
        self._panel_desc  = "Wähle ein Modul aus den Buttons unten um ein Ticket zu erstellen."
        self._build()

    def build_preview_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="✏️ Panel-Text bearbeiten",
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
        embed.set_footer(text="Klicke '💾 Änderungen übernehmen' um die Panel-Nachricht zu aktualisieren.")
        return embed

    def _build(self):
        self.clear_items()

        btn_text = discord.ui.Button(label="✏️ Titel & Text bearbeiten",
                                     style=discord.ButtonStyle.primary, row=0)
        btn_text.callback = self._cb_edit_text
        self.add_item(btn_text)

        btn_apply = discord.ui.Button(
            label="💾 Änderungen übernehmen",
            style=discord.ButtonStyle.success, row=0,
        )
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

            from .views import TicketPanelView
            panel_view = TicketPanelView(
                modules=self.modules,
                category_id=int(self.server.get("category_id", 0)),
                bot=self.bot,
            )

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