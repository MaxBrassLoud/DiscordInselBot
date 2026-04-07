"""
bot/features/tickets/setup_wizard.py
=====================================
Schritt-für-Schritt Setup-Wizard für das Ticket-System via Discord.
Ersetzt den alten TicketSetupView in setup_views.py durch eine
geführte, klar strukturierte Erfahrung.

FLOW:
  /ticket_setup
    → Step 1: Panel-Kanal wählen
    → Step 2: Standard-Kategorie wählen
    → Step 3: Log-Kanal & Ping-Kanal (optional)
    → Step 4: Module hinzufügen (wiederholbar)
    → Step 5: Bestätigung & Panel senden

  Fortschritt wird in der Embed angezeigt.
  Jeder Schritt ist einzeln lösbar/überspringbar.
"""

from __future__ import annotations

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("tickets.wizard")

MAX_MODULES = 10
STEP_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]


def _step_bar(current: int, total: int = 5) -> str:
    """Generates a visual step progress string."""
    parts = []
    for i in range(1, total + 1):
        if i < current:
            parts.append("✅")
        elif i == current:
            parts.append(f"**{STEP_EMOJIS[i-1]}**")
        else:
            parts.append("⬜")
    return " ".join(parts)


def _build_wizard_embed(
    step: int,
    title: str,
    description: str,
    fields: list[tuple[str, str, bool]] | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🎫 Ticket-Setup – Schritt {step}/5",
        color=color or discord.Color.blurple(),
    )
    embed.add_field(
        name="Fortschritt",
        value=_step_bar(step),
        inline=False,
    )
    embed.add_field(
        name=f"{STEP_EMOJIS[step-1]} {title}",
        value=description,
        inline=False,
    )
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Du kannst jederzeit neu konfigurieren mit /ticket_setup")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 – Panel-Kanal
# ══════════════════════════════════════════════════════════════════════════════

class WizardStep1View(discord.ui.View):
    """Schritt 1: Panel-Kanal auswählen."""

    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state  # shared state dict

        web_url = f"Web-Setup verfügbar: Gehe zu `/dashboard/setup/tickets` im Browser."

        sel = discord.ui.ChannelSelect(
            placeholder="📢 Panel-Kanal auswählen (Pflicht)…",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        sel.callback = self._on_channel
        self.add_item(sel)

        web_btn = discord.ui.Button(
            label="🌐 Stattdessen im Browser einrichten",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        web_btn.callback = self._on_web
        self.add_item(web_btn)

    def build_embed(self) -> discord.Embed:
        current = self.state.get("panel_channel_id")
        return _build_wizard_embed(
            step=1,
            title="Panel-Kanal wählen",
            description=(
                "Wähle den Kanal in dem der **Ticket-Erstellungs-Button** erscheinen soll.\n\n"
                "Mitglieder drücken dort auf den Button um ein Ticket zu öffnen.\n"
                "Empfehlung: Ein `#support` oder `#hilfe` Kanal."
            ),
            fields=[
                ("Aktuell",
                 f"<#{current}>" if current else "*Noch nicht gesetzt*",
                 True),
            ],
        )

    async def _on_channel(self, interaction: discord.Interaction):
        self.state["panel_channel_id"] = interaction.data["values"][0]
        view = WizardStep2View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_web(self, interaction: discord.Interaction):
        import os
        web_base = os.getenv("WEB_BASE_URL", "http://localhost:5000")
        embed = discord.Embed(
            title="🌐 Web-Setup",
            description=(
                f"Das Ticket-System kann auch vollständig im Browser eingerichtet werden:\n\n"
                f"**[→ {web_base}/dashboard/setup/tickets]"
                f"({web_base}/dashboard/setup/tickets)**\n\n"
                "Logge dich mit deinem Discord-Account ein und folge dem geführten Setup."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 – Kategorie
# ══════════════════════════════════════════════════════════════════════════════

class WizardStep2View(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state

        sel = discord.ui.ChannelSelect(
            placeholder="📁 Standard-Kategorie für neue Ticket-Kanäle (Pflicht)…",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.category],
            row=0,
        )
        sel.callback = self._on_cat
        self.add_item(sel)

        back = discord.ui.Button(label="← Zurück", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        current = self.state.get("category_id")
        return _build_wizard_embed(
            step=2,
            title="Kategorie wählen",
            description=(
                "Wähle die **Kategorie** in der neue Ticket-Kanäle erstellt werden.\n\n"
                "Tipp: Erstelle eine separate Kategorie `🎫 Tickets` dafür.\n"
                "Einzelne Module können später eigene Kategorien bekommen."
            ),
            fields=[
                ("Aktuell", f"<#{current}>" if current else "*Noch nicht gesetzt*", True),
                ("Panel-Kanal", f"<#{self.state.get('panel_channel_id', '?')}>", True),
            ],
        )

    async def _on_cat(self, interaction: discord.Interaction):
        self.state["category_id"] = interaction.data["values"][0]
        view = WizardStep3View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        view = WizardStep1View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 – Log- & Ping-Kanäle (optional)
# ══════════════════════════════════════════════════════════════════════════════

class WizardStep3View(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        log_sel = discord.ui.ChannelSelect(
            placeholder="📋 Log-Kanal (optional) – Ticket-Protokolle",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        log_sel.callback = self._on_log
        self.add_item(log_sel)

        ping_sel = discord.ui.ChannelSelect(
            placeholder="🔔 Staff-Ping Kanal (optional) – Neue Ticket Benachrichtigungen",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=1,
        )
        ping_sel.callback = self._on_ping
        self.add_item(ping_sel)

        next_btn = discord.ui.Button(
            label="Weiter → Module hinzufügen",
            style=discord.ButtonStyle.success,
            row=2,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        back = discord.ui.Button(label="← Zurück", style=discord.ButtonStyle.secondary, row=2)
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        log_id  = self.state.get("log_channel_id")
        ping_id = self.state.get("staff_ping_channel_id")
        return _build_wizard_embed(
            step=3,
            title="Log- & Benachrichtigungs-Kanäle (optional)",
            description=(
                "Diese Kanäle sind **optional** aber empfohlen:\n\n"
                "**📋 Log-Kanal:** Hier erscheinen Links zu allen neuen und geschlossenen Tickets.\n"
                "**🔔 Staff-Ping:** Hier werden Staff-Rollen bei neuen Tickets gepingt.\n\n"
                "Klicke *Weiter* um diesen Schritt zu überspringen."
            ),
            fields=[
                ("📋 Log-Kanal",  f"<#{log_id}>" if log_id else "*nicht gesetzt*",  True),
                ("🔔 Staff-Ping", f"<#{ping_id}>" if ping_id else "*nicht gesetzt*", True),
            ],
        )

    async def _on_log(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.state["log_channel_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_ping(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.state["staff_ping_channel_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        # Save server config to DB
        try:
            await _save_server_config(self.guild_id, self.state)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Speichern: {e}", ephemeral=True)
            return
        view = WizardStep4View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        view = WizardStep2View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 – Module hinzufügen
# ══════════════════════════════════════════════════════════════════════════════

class WizardStep4View(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client, state: dict, original_interaction: discord.Interaction | None = None):
        super().__init__(timeout=300)
        self.guild_id             = str(guild_id)
        self.bot                  = bot
        self.state                = state
        self._original_interaction = original_interaction  # stored so sub-views can refresh the main message
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        mods = self.state.get("modules", [])

        if len(mods) < MAX_MODULES:
            add_btn = discord.ui.Button(
                label=f"➕ Modul hinzufügen ({len(mods)}/{MAX_MODULES})",
                style=discord.ButtonStyle.primary,
                row=0,
            )
            add_btn.callback = self._on_add
            self.add_item(add_btn)

        if mods:
            remove_btn = discord.ui.Button(
                label=f"🗑️ Letztes Modul entfernen",
                style=discord.ButtonStyle.secondary,
                row=0,
            )
            remove_btn.callback = self._on_remove
            self.add_item(remove_btn)

            next_btn = discord.ui.Button(
                label="Weiter → Abschließen",
                style=discord.ButtonStyle.success,
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        back = discord.ui.Button(label="← Zurück", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        mods = self.state.get("modules", [])
        mod_text = "\n".join(
            f"{m.get('button_emoji','🎫')} **{m['name']}** – {m.get('description','')[:50]}"
            for m in mods
        ) if mods else "*Noch keine Module*"

        return _build_wizard_embed(
            step=4,
            title="Ticket-Module hinzufügen",
            description=(
                "Füge die **Kategorien** hinzu für die Tickets erstellt werden können.\n\n"
                "Beispiele: `🔧 Support`, `💰 Kauf/Verkauf`, `📋 Bewerbung`\n\n"
                "Jedes Modul bekommt einen eigenen Button. Füge mindestens **1 Modul** hinzu."
            ),
            fields=[
                (f"Module ({len(mods)}/{MAX_MODULES})", mod_text, False),
            ],
        )

    async def _on_add(self, interaction: discord.Interaction):
        # Store the interaction token so WizardModuleRolePicker can refresh this message
        # Each new interaction replaces the previous token
        self._original_interaction = interaction
        modal = WizardModuleModal(self)
        await interaction.response.send_modal(modal)

    async def _on_remove(self, interaction: discord.Interaction):
        mods = self.state.get("modules", [])
        if mods:
            mods.pop()
            self.state["modules"] = mods
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        view = WizardStep5View(self.guild_id, self.bot, self.state, original_interaction=interaction)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        view = WizardStep3View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class WizardModuleModal(discord.ui.Modal, title="Ticket-Modul hinzufügen"):
    mod_name     = discord.ui.TextInput(label="Modul-Name", placeholder="z.B. Support", required=True, max_length=50)
    mod_desc     = discord.ui.TextInput(label="Kurze Beschreibung", placeholder="z.B. Hilfe bei Problemen", required=True, max_length=200)
    mod_question = discord.ui.TextInput(
        label="Was soll der User beschreiben?",
        style=discord.TextStyle.paragraph,
        placeholder="z.B. Beschreibe dein Anliegen so detailliert wie möglich.",
        required=True, max_length=300,
    )
    mod_emoji    = discord.ui.TextInput(
        label="Button-Emoji (optional)", placeholder="🎫", required=False, max_length=100
    )
    mod_max      = discord.ui.TextInput(
        label="Max. offene Tickets pro User", placeholder="1", required=True, max_length=2, default="1"
    )

    def __init__(self, parent_view: WizardStep4View):
        super().__init__()
        self.parent = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            max_t = max(1, int(self.mod_max.value))
        except ValueError:
            max_t = 1

        raw_emoji = self.mod_emoji.value.strip() or "🎫"

        mod_data = {
            "name":           self.mod_name.value,
            "description":    self.mod_desc.value,
            "modal_question": self.mod_question.value,
            "button_emoji":   raw_emoji,
            "max_tickets":    max_t,
            "staff_role_ids": [],
            "category_id":    None,
        }
        view = WizardModuleRolePicker(mod_data=mod_data, parent_view=self.parent)
        # Respond to the modal interaction with an ephemeral message
        await interaction.response.send_message(
            embed=_module_role_embed(mod_data),
            view=view,
            ephemeral=True,
        )
        # Store this followup message reference so confirm can edit it
        view._sub_message_interaction = interaction


def _module_role_embed(mod_data: dict) -> discord.Embed:
    emoji = mod_data.get("button_emoji", "🎫")
    if str(emoji).startswith("<"):
        emoji = "🎫"
    roles = ", ".join(f"<@&{r}>" for r in mod_data.get("staff_role_ids", [])) or "*noch nicht gewählt*"
    cat   = f"<#{mod_data['category_id']}>" if mod_data.get("category_id") else "*globale Standard-Kategorie*"
    embed = discord.Embed(
        title=f"{emoji} Modul: {mod_data['name']}",
        description=(
            f"**Beschreibung:** {mod_data['description']}\n"
            f"**Max. Tickets:** {mod_data['max_tickets']}/User\n\n"
            "Wähle nun die **Staff-Rollen** die Zugriff auf dieses Modul haben.\n"
            "Optional: Wähle eine eigene Kategorie für die Ticket-Kanäle dieses Moduls."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="👮 Staff-Rollen",  value=roles, inline=True)
    embed.add_field(name="📁 Kategorie",     value=cat,   inline=True)
    return embed


class WizardModuleRolePicker(discord.ui.View):
    def __init__(self, mod_data: dict, parent_view: WizardStep4View):
        super().__init__(timeout=180)
        self.mod_data    = mod_data
        self.parent_view = parent_view

        role_sel = discord.ui.RoleSelect(
            placeholder="👮 Staff-Rollen für dieses Modul (Pflicht)…",
            min_values=1, max_values=10,
            row=0,
        )
        role_sel.callback = self._on_roles
        self.add_item(role_sel)

        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Eigene Kategorie (optional – leer = globale Standard)",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.category],
            row=1,
        )
        cat_sel.callback = self._on_cat
        self.add_item(cat_sel)

        self._confirm = discord.ui.Button(
            label="✅ Modul speichern",
            style=discord.ButtonStyle.success,
            disabled=True,
            row=2,
        )
        self._confirm.callback = self._on_confirm
        self.add_item(self._confirm)

    async def _on_roles(self, interaction: discord.Interaction):
        self.mod_data["staff_role_ids"] = interaction.data["values"]
        self._confirm.disabled = False
        await interaction.response.edit_message(embed=_module_role_embed(self.mod_data), view=self)

    async def _on_cat(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.mod_data["category_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=_module_role_embed(self.mod_data), view=self)

    async def _on_confirm(self, interaction: discord.Interaction):
        # Add module to parent state
        mods = self.parent_view.state.setdefault("modules", [])
        mods.append(self.mod_data)
        self.parent_view._rebuild()

        # 1. Edit the sub (ephemeral) message to show success
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"✅ Modul gespeichert: {self.mod_data['name']}",
                description=(
                    f"Das Modul **{self.mod_data['name']}** wurde hinzugefügt.\n"
                    f"Schließe diese Nachricht – die Modulliste wurde aktualisiert."
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )

        # 2. Refresh the MAIN wizard message via the stored original_interaction
        # The parent_view._original_interaction was set when the user pressed "➕ Modul hinzufügen"
        orig = self.parent_view._original_interaction
        if orig is not None:
            try:
                await orig.edit_original_response(
                    embed=self.parent_view.build_embed(),
                    view=self.parent_view,
                )
            except Exception as e:
                logger.warning(f"[wizard] Hauptnachricht konnte nicht aktualisiert werden: {e}")
                # Fallback: send a followup telling user to click "➕" again to see updated list
                try:
                    await interaction.followup.send(
                        f"✅ **{self.mod_data['name']}** hinzugefügt! "
                        "Die Übersicht wird beim nächsten Klick aktualisiert.",
                        ephemeral=True,
                    )
                except Exception:
                    pass


# ══════════════════════════════════════════════════════════════════════════════
# STEP 5 – Abschließen & Panel senden
# ══════════════════════════════════════════════════════════════════════════════

class WizardStep5View(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client, state: dict, original_interaction: discord.Interaction | None = None):
        super().__init__(timeout=300)
        self.guild_id             = str(guild_id)
        self.bot                  = bot
        self.state                = state
        self._original_interaction = original_interaction
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        finish_btn = discord.ui.Button(
            label="🚀 Setup abschließen & Panel senden",
            style=discord.ButtonStyle.success,
            row=0,
        )
        finish_btn.callback = self._on_finish
        self.add_item(finish_btn)

        web_btn = discord.ui.Button(
            label="🌐 Im Browser verfeinern",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        web_btn.callback = self._on_web
        self.add_item(web_btn)

        back = discord.ui.Button(label="← Zurück", style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    def build_embed(self) -> discord.Embed:
        mods = self.state.get("modules", [])
        mod_text = "\n".join(
            f"{m.get('button_emoji','🎫')} **{m['name']}** ({len(m.get('staff_role_ids',[]))} Staff-Rollen)"
            for m in mods
        )
        return _build_wizard_embed(
            step=5,
            title="Alles bereit – Setup abschließen",
            description=(
                "Überprüfe deine Konfiguration und klicke auf **Setup abschließen** um das Panel zu senden.\n\n"
                "Das Ticket-Panel wird in deinen konfigurierten Panel-Kanal gesendet.\n"
                "Du kannst das Setup jederzeit über `/ticket_bearbeiten` oder das Web-Dashboard anpassen."
            ),
            fields=[
                ("📢 Panel-Kanal",    f"<#{self.state.get('panel_channel_id','?')}>",                True),
                ("📁 Kategorie",      f"<#{self.state.get('category_id','?')}>",                     True),
                ("📋 Log-Kanal",      f"<#{self.state.get('log_channel_id')}>" if self.state.get("log_channel_id") else "*–*",  True),
                ("🔔 Staff-Ping",     f"<#{self.state.get('staff_ping_channel_id')}>" if self.state.get("staff_ping_channel_id") else "*–*", True),
                (f"📂 Module ({len(mods)})", mod_text or "*keine*",                                  False),
            ],
            color=discord.Color.green(),
        )

    async def _on_finish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from .panel_view import TicketPanelView, build_ticket_panel_embed

            supabase  = get_supabase()
            guild     = interaction.guild
            guild_id  = str(guild.id)
            state     = self.state

            # ── Save server config ────────────────────────────────────────────
            await _save_server_config(guild_id, state)

            # ── Save modules ──────────────────────────────────────────────────
            old_mods = supabase.table("ticket_modules").select("id").eq("server_id", guild_id).execute()
            for mod in (old_mods.data or []):
                supabase.table("ticket_module_roles").delete().eq("module_id", mod["id"]).execute()
            supabase.table("ticket_modules").delete().eq("server_id", guild_id).execute()

            db_modules = []
            for mod in state.get("modules", []):
                effective_cat = mod.get("category_id") or state.get("category_id")
                result = supabase.table("ticket_modules").insert({
                    "server_id":      guild_id,
                    "name":           mod["name"],
                    "description":    mod["description"],
                    "max_tickets":    mod["max_tickets"],
                    "modal_question": mod["modal_question"],
                    "button_emoji":   mod.get("button_emoji", "🎫"),
                    "category_id":    effective_cat,
                }).execute()

                if result.data:
                    mod_id = result.data[0]["id"]
                    mod["id"] = mod_id
                    mod["effective_category"] = effective_cat
                    for role_id in mod.get("staff_role_ids", []):
                        supabase.table("ticket_module_roles").insert({
                            "module_id": mod_id, "role_id": str(role_id)
                        }).execute()
                    db_modules.append(mod)

            # ── Send panel ────────────────────────────────────────────────────
            panel_channel = guild.get_channel(int(state["panel_channel_id"]))
            if not panel_channel:
                await interaction.followup.send("❌ Panel-Kanal nicht gefunden!", ephemeral=True)
                return

            # Persistente View mit statischer custom_id – kein Neustart nötig
            embed      = build_ticket_panel_embed(db_modules)
            panel_view = TicketPanelView(bot=self.bot)
            panel_msg  = await panel_channel.send(embed=embed, view=panel_view)

            supabase.table("ticket_servers").update({
                "panel_message_id": str(panel_msg.id),
            }).eq("server_id", guild_id).execute()

            # ── Success ───────────────────────────────────────────────────────
            import os
            web_base = os.getenv("WEB_BASE_URL", "http://localhost:5000")
            success_embed = discord.Embed(
                title="🎉 Ticket-System eingerichtet!",
                description=(
                    f"✅ Panel in <#{state['panel_channel_id']}> gesendet!\n\n"
                    f"**{len(db_modules)} Module** wurden konfiguriert.\n\n"
                    f"Mitglieder können jetzt Tickets erstellen.\n\n"
                    f"**Tipp:** Über das Web-Dashboard kannst du alles jederzeit anpassen:\n"
                    f"[→ {web_base}/dashboard/setup/tickets]({web_base}/dashboard/setup/tickets)"
                ),
                color=discord.Color.green(),
            )
            success_embed.set_footer(text="Ticket-System by Insel Bot")
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=success_embed, view=self)

        except Exception as e:
            logger.error(f"[WizardStep5] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    async def _on_web(self, interaction: discord.Interaction):
        import os
        web_base = os.getenv("WEB_BASE_URL", "http://localhost:5000")
        embed = discord.Embed(
            title="🌐 Web-Setup",
            description=(
                f"Verfeinere das Setup im Browser:\n\n"
                f"**[→ {web_base}/dashboard/setup/tickets?server_id={interaction.guild_id}]"
                f"({web_base}/dashboard/setup/tickets?server_id={interaction.guild_id})**"
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_back(self, interaction: discord.Interaction):
        view = WizardStep4View(self.guild_id, self.bot, self.state, original_interaction=interaction)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
# SHARED: save server config
# ══════════════════════════════════════════════════════════════════════════════

async def _save_server_config(guild_id: str, state: dict):
    supabase = get_supabase()
    data = {
        "server_id":             guild_id,
        "category_id":           state.get("category_id"),
        "panel_channel_id":      state.get("panel_channel_id"),
        "log_channel_id":        state.get("log_channel_id"),
        "staff_ping_channel_id": state.get("staff_ping_channel_id"),
    }
    existing = supabase.table("ticket_servers").select("server_id").eq("server_id", guild_id).execute()
    if existing.data:
        supabase.table("ticket_servers").update(data).eq("server_id", guild_id).execute()
    else:
        data["ticket_counter"] = 0
        supabase.table("ticket_servers").insert(data).execute()


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT: create initial wizard
# ══════════════════════════════════════════════════════════════════════════════

async def start_ticket_wizard(
    interaction: discord.Interaction,
    bot: discord.Client,
    existing_config: dict | None = None,
) -> None:
    """
    Startet den Ticket-Setup-Wizard.
    Wenn existing_config vorhanden, wird von den aktuellen Werten vorausgefüllt.
    """
    state: dict = {
        "panel_channel_id":      None,
        "category_id":           None,
        "log_channel_id":        None,
        "staff_ping_channel_id": None,
        "modules":               [],
    }

    # Pre-fill from existing config
    if existing_config:
        for key in ["panel_channel_id", "category_id", "log_channel_id", "staff_ping_channel_id"]:
            if existing_config.get(key):
                state[key] = existing_config[key]

    view = WizardStep1View(
        guild_id=interaction.guild_id,
        bot=bot,
        state=state,
    )
    # Store original interaction so steps can update the original message
    view._original_interaction = interaction

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )