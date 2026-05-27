"""
app_edit_views.py  –  /bewerbung_bearbeiten Command Views
==========================================================
ÄNDERUNGEN:
  - Panel-Kanal wechseln: alte Nachricht löschen, neue im neuen Kanal senden
  - Prominenter "💾 Alle Änderungen speichern"-Button in der Haupt-View
  - AppChannelSettingsView: Panel-Kanal-Wechsel mit automatischem Umzug
"""

from __future__ import annotations

import os
import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("applications.edit")

WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _parse_role_ids(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(r).strip() for r in raw if r]
    return [r.strip() for r in str(raw).split(",") if r.strip()]


def _overview_embed(cfg: dict) -> discord.Embed:
    embed = discord.Embed(
        title="✏️ Bewerbungs-System bearbeiten",
        color=discord.Color.green(),
        description=(
            "Wähle unten, was du bearbeiten möchtest.\n"
            "Klicke **💾 Alle Änderungen speichern** um alle Einstellungen auf einmal zu sichern."
        ),
    )

    def _ch(key): return f"<#{cfg[key]}>" if cfg.get(key) else "*nicht gesetzt*"
    def _ro(key):
        ids = _parse_role_ids(cfg.get(key))
        return ", ".join(f"<@&{r}>" for r in ids) if ids else "*nicht gesetzt*"

    embed.add_field(name="📢 Panel-Kanal",        value=_ch("panel_channel_id"),  inline=True)
    embed.add_field(name="📁 Kategorie",           value=_ch("category_id"),       inline=True)
    embed.add_field(name="📋 Log-Kanal",           value=_ch("log_channel_id"),    inline=True)
    embed.add_field(name="⛏️ MC-Log-Kanal",       value=_ch("mc_log_channel_id"), inline=True)
    embed.add_field(name="⏳ Cooldown",            value=f"{cfg.get('rejection_cooldown_hours', 24)}h", inline=True)
    embed.add_field(name="\u200b",                 value="\u200b",                 inline=True)
    embed.add_field(name="🌱 Neulings-Rolle",      value=_ro("newbie_role_id"),    inline=True)
    embed.add_field(name="👥 Mitglieds-Rolle",     value=_ro("member_role_id"),    inline=True)
    embed.add_field(name="\u200b",                 value="\u200b",                 inline=True)
    embed.add_field(name="👮 Staff-Rollen",        value=_ro("staff_role_ids"),    inline=False)
    embed.add_field(name="🌐 Web-Admin Rollen",    value=_ro("web_admin_role_ids"), inline=False)

    panel = cfg.get("panel_message") or cfg.get("welcome_message") or ""
    if panel:
        embed.add_field(name="📝 Panel-Text", value=panel[:200] + ("…" if len(panel) > 200 else ""), inline=False)
    welcome = cfg.get("welcome_message") or ""
    if welcome:
        embed.add_field(name="💬 Ticket-Embed-Text", value=welcome[:200] + ("…" if len(welcome) > 200 else ""), inline=False)
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# PANEL UMZUG HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _move_app_panel_to_new_channel(
    bot: discord.Client,
    guild: discord.Guild,
    cfg: dict,
    new_channel_id: str,
) -> str | None:
    """
    Löscht die alte Panel-Nachricht und sendet eine neue im neuen Kanal.
    Gibt die neue message_id zurück oder None bei Fehler.
    """
    # Alte Nachricht löschen
    old_channel_id = cfg.get("panel_channel_id")
    old_message_id = cfg.get("panel_message_id")

    if old_channel_id and old_message_id:
        try:
            old_ch = guild.get_channel(int(old_channel_id))
            if old_ch:
                old_msg = await old_ch.fetch_message(int(old_message_id))
                await old_msg.delete()
                logger.info(f"[app_edit] Alte Panel-Nachricht gelöscht: {old_message_id}")
        except discord.NotFound:
            logger.info("[app_edit] Alte Panel-Nachricht nicht gefunden (evtl. bereits gelöscht)")
        except Exception as e:
            logger.warning(f"[app_edit] Alte Panel-Nachricht konnte nicht gelöscht werden: {e}")

    # Neue Nachricht senden
    new_ch = guild.get_channel(int(new_channel_id))
    if not new_ch:
        return None

    try:
        from bot.features.applications.views import ApplicationPanelView, _build_panel_embed
        welcome_text = cfg.get("panel_message") or cfg.get("welcome_message") or "Klicke auf den Button um deine Bewerbung einzureichen."
        embed      = _build_panel_embed(welcome_text)
        panel_view = ApplicationPanelView(bot=bot)
        new_msg    = await new_ch.send(embed=embed, view=panel_view)
        logger.info(f"[app_edit] Neue Panel-Nachricht gesendet: {new_msg.id} in #{new_ch.name}")
        return str(new_msg.id)
    except Exception as e:
        logger.error(f"[app_edit] Panel-Nachricht senden fehlgeschlagen: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# MAIN EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class AppEditMainView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self._original_interaction: discord.Interaction | None = None
        self._rebuild()

    def _load_cfg(self) -> dict | None:
        r = get_supabase().table("application_servers") \
            .select("*").eq("server_id", self.guild_id).execute()
        return r.data[0] if r.data else None

    def build_embed(self) -> discord.Embed:
        cfg = self._load_cfg()
        if not cfg:
            return discord.Embed(
                title="❌ Nicht eingerichtet",
                description="Nutze `/bewerbung_setup` um das System einzurichten.",
                color=discord.Color.red(),
            )
        return _overview_embed(cfg)

    def _rebuild(self):
        self.clear_items()

        # Reihe 0
        btn_channels = discord.ui.Button(
            label="⚙️ Kanäle & Cooldown",
            style=discord.ButtonStyle.primary, row=0,
        )
        btn_channels.callback = self._cb_channels
        self.add_item(btn_channels)

        btn_roles = discord.ui.Button(
            label="👮 Rollen bearbeiten",
            style=discord.ButtonStyle.primary, row=0,
        )
        btn_roles.callback = self._cb_roles
        self.add_item(btn_roles)

        btn_texts = discord.ui.Button(
            label="💬 Texte bearbeiten",
            style=discord.ButtonStyle.secondary, row=0,
        )
        btn_texts.callback = self._cb_texts
        self.add_item(btn_texts)

        # Reihe 1
        btn_panel = discord.ui.Button(
            label="📤 Panel bearbeiten",
            style=discord.ButtonStyle.secondary, row=1,
        )
        btn_panel.callback = self._cb_panel
        self.add_item(btn_panel)

        btn_web = discord.ui.Button(
            label="🌐 Im Browser öffnen",
            style=discord.ButtonStyle.secondary, row=1,
        )
        btn_web.callback = self._cb_web
        self.add_item(btn_web)

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

    async def _cb_channels(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg:
            await interaction.response.send_message("❌ Nicht eingerichtet.", ephemeral=True)
            return
        view = AppChannelSettingsView(cfg=cfg, bot=self.bot, parent=self)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    async def _cb_roles(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg:
            await interaction.response.send_message("❌ Nicht eingerichtet.", ephemeral=True)
            return
        view = AppRoleSettingsView(cfg=cfg, parent=self)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    async def _cb_texts(self, interaction: discord.Interaction):
        try:
            cfg = self._load_cfg()
            if not cfg:
                await interaction.response.send_message("❌ Nicht eingerichtet.", ephemeral=True)
                return
            modal = AppTextsModal(cfg, parent=self)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"[AppEditMainView._cb_texts] {e}")
            await interaction.response.send_message(
                f"❌ Texte-Modal konnte nicht geöffnet werden: {e}", ephemeral=True
            )

    async def _cb_panel(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg:
            await interaction.response.send_message("❌ Nicht eingerichtet.", ephemeral=True)
            return
        if not cfg.get("panel_channel_id"):
            await interaction.response.send_message("❌ Kein Panel-Kanal konfiguriert.", ephemeral=True)
            return
        if not cfg.get("panel_message_id"):
            await interaction.response.send_message(
                "❌ Keine Panel-Nachrichten-ID gespeichert.\n"
                "Nutze `/bewerbung_setup` um das Panel neu zu senden.", ephemeral=True
            )
            return
        view = AppPanelEditView(cfg=cfg, bot=self.bot, parent=self)
        await interaction.response.send_message(embed=view.build_preview_embed(), view=view, ephemeral=True)

    async def _cb_web(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🌐 Web-Dashboard",
            description=(
                f"Bearbeite das Bewerbungs-System im Browser:\n\n"
                f"**[→ Dashboard öffnen]({WEB_BASE_URL}/dashboard/setup/applications?server_id={self.guild_id})**"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _cb_save_all(self, interaction: discord.Interaction):
        """Lädt aktuelle Einstellungen aus der DB und aktualisiert die Panel-Embed."""
        await interaction.response.defer(ephemeral=True)
        cfg = self._load_cfg()
        if not cfg:
            await interaction.followup.send("❌ Keine Konfiguration gefunden.", ephemeral=True)
            return

        # Panel-Nachricht aktualisieren falls vorhanden
        updated_panel = False
        if cfg.get("panel_channel_id") and cfg.get("panel_message_id"):
            try:
                from bot.features.applications.views import ApplicationPanelView, _build_panel_embed
                ch = interaction.guild.get_channel(int(cfg["panel_channel_id"]))
                if ch:
                    msg = await ch.fetch_message(int(cfg["panel_message_id"]))
                    welcome_text = cfg.get("panel_message") or cfg.get("welcome_message") or "Klicke auf den Button um deine Bewerbung einzureichen."
                    await msg.edit(
                        embed=_build_panel_embed(welcome_text),
                        view=ApplicationPanelView(bot=self.bot),
                    )
                    updated_panel = True
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[app_save_all] Panel-Update: {e}")

        await self.refresh()

        panel_hint = " Panel-Nachricht wurde ebenfalls aktualisiert." if updated_panel else ""
        await interaction.followup.send(
            f"✅ Alle Änderungen wurden gespeichert.{panel_hint}",
            ephemeral=True,
        )

    async def refresh(self):
        """Aktualisiert die Haupt-Übersicht."""
        if self._original_interaction:
            try:
                await self._original_interaction.edit_original_response(
                    embed=self.build_embed(), view=self
                )
            except Exception as e:
                logger.error(f"[AppEditMainView.refresh] {e}")


# ══════════════════════════════════════════════════════════════════════════════
# KANAL & COOLDOWN EINSTELLUNGEN (mit Panel-Kanal-Wechsel)
# ══════════════════════════════════════════════════════════════════════════════

class AppChannelSettingsView(discord.ui.View):
    def __init__(self, cfg: dict, bot: discord.Client, parent: AppEditMainView):
        super().__init__(timeout=180)
        self.cfg    = dict(cfg)
        self.bot    = bot
        self.parent = parent
        self._panel_channel_changed = False
        self._build()

    def build_embed(self) -> discord.Embed:
        def _ch(key): return f"<#{self.cfg[key]}>" if self.cfg.get(key) else "*nicht gesetzt*"
        e = discord.Embed(
            title="⚙️ Kanäle & Cooldown bearbeiten",
            color=discord.Color.blurple(),
            description=(
                "Wähle die Kanäle über die Dropdowns.\n"
                "⚠️ Bei einem **neuen Panel-Kanal** wird die alte Panel-Nachricht gelöscht "
                "und im neuen Kanal neu gesendet."
            ),
        )
        e.add_field(name="📢 Panel-Kanal",     value=_ch("panel_channel_id"),  inline=True)
        if self._panel_channel_changed:
            e.add_field(name="⚠️ Panel-Kanal", value="**Wird beim Speichern umgezogen!**", inline=True)
        e.add_field(name="📁 Kategorie",        value=_ch("category_id"),       inline=True)
        e.add_field(name="📋 Log-Kanal",        value=_ch("log_channel_id"),    inline=True)
        e.add_field(name="⛏️ MC-Log-Kanal",    value=_ch("mc_log_channel_id"), inline=True)
        e.add_field(name="⏳ Cooldown",         value=f"{self.cfg.get('rejection_cooldown_hours', 24)} Stunden", inline=True)
        e.set_footer(text="Wähle Kanäle aus den Dropdowns und klicke 💾 Speichern.")
        return e

    def _build(self):
        self.clear_items()

        panel_sel = discord.ui.ChannelSelect(
            placeholder="📢 Panel-Kanal ändern",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text], row=0,
        )
        async def _panel(i):
            new_id = i.data["values"][0]
            if new_id != self.cfg.get("panel_channel_id"):
                self._panel_channel_changed = True
                self.cfg["_new_panel_channel_id"] = new_id
            self.cfg["panel_channel_id"] = new_id
            self._build()
            await i.response.edit_message(embed=self.build_embed(), view=self)
        panel_sel.callback = _panel
        self.add_item(panel_sel)

        for row_idx, (placeholder, key, types) in enumerate([
            ("📁 Kategorie ändern",         "category_id",       [discord.ChannelType.category]),
            ("📋 Log-Kanal ändern",         "log_channel_id",    [discord.ChannelType.text]),
            ("⛏️ MC-Log-Kanal ändern",     "mc_log_channel_id", [discord.ChannelType.text]),
        ], start=1):
            sel = discord.ui.ChannelSelect(
                placeholder=placeholder, min_values=1, max_values=1,
                channel_types=types, row=row_idx,
            )
            async def _cb(i, k=key):
                self.cfg[k] = i.data["values"][0]
                await i.response.edit_message(embed=self.build_embed(), view=self)
            sel.callback = _cb
            self.add_item(sel)

        btn_cooldown = discord.ui.Button(
            label=f"⏳ Cooldown: {self.cfg.get('rejection_cooldown_hours', 24)}h",
            style=discord.ButtonStyle.secondary, row=4,
        )
        btn_cooldown.callback = self._cb_cooldown
        self.add_item(btn_cooldown)

        save_label = "💾 Speichern & Panel umziehen" if self._panel_channel_changed else "💾 Speichern"
        btn_save = discord.ui.Button(
            label=save_label,
            style=discord.ButtonStyle.success, row=4,
        )
        btn_save.callback = self._save
        self.add_item(btn_save)

    async def _cb_cooldown(self, interaction: discord.Interaction):
        await interaction.response.send_modal(CooldownModal(cfg=self.cfg, view=self))

    async def _save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            update_data = {
                "category_id":              self.cfg.get("category_id"),
                "log_channel_id":           self.cfg.get("log_channel_id"),
                "mc_log_channel_id":        self.cfg.get("mc_log_channel_id"),
                "rejection_cooldown_hours": int(self.cfg.get("rejection_cooldown_hours", 24)),
            }

            new_panel_msg_id = None

            # Panel-Kanal wechseln
            if self._panel_channel_changed:
                new_channel_id = self.cfg["panel_channel_id"]
                new_panel_msg_id = await _move_app_panel_to_new_channel(
                    bot=self.bot,
                    guild=interaction.guild,
                    cfg=self.cfg,
                    new_channel_id=new_channel_id,
                )
                update_data["panel_channel_id"] = new_channel_id
                if new_panel_msg_id:
                    update_data["panel_message_id"] = new_panel_msg_id
            else:
                update_data["panel_channel_id"] = self.cfg.get("panel_channel_id")

            get_supabase().table("application_servers").update(update_data)\
                .eq("server_id", self.parent.guild_id).execute()

            embed = self.build_embed()
            embed.title = "✅ Gespeichert!"
            embed.color = discord.Color.green()
            if self._panel_channel_changed and new_panel_msg_id:
                embed.add_field(
                    name="✅ Panel-Nachricht umgezogen",
                    value=f"Neue Nachricht in <#{self.cfg['panel_channel_id']}>",
                    inline=False,
                )
            elif self._panel_channel_changed and not new_panel_msg_id:
                embed.add_field(
                    name="⚠️ Panel-Nachricht",
                    value="Kanal gespeichert, aber neue Panel-Nachricht konnte nicht gesendet werden. Nutze `/bewerbung_setup`.",
                    inline=False,
                )

            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            logger.error(f"[AppChannelSettingsView._save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class CooldownModal(discord.ui.Modal, title="Cooldown nach Ablehnung"):
    hours = discord.ui.TextInput(
        label="Wartezeit in Stunden (0 = kein Cooldown)",
        placeholder="24",
        required=True, max_length=6,
    )

    def __init__(self, cfg: dict, view: "AppChannelSettingsView"):
        super().__init__()
        self._cfg  = cfg
        self._view = view
        self.hours.default = str(cfg.get("rejection_cooldown_hours", 24))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            val = max(0, int(self.hours.value))
        except ValueError:
            val = 24
        self._cfg["rejection_cooldown_hours"] = val
        self._view._build()
        await interaction.response.edit_message(embed=self._view.build_embed(), view=self._view)


# ══════════════════════════════════════════════════════════════════════════════
# ROLLEN EINSTELLUNGEN
# ══════════════════════════════════════════════════════════════════════════════

class AppRoleSettingsView(discord.ui.View):
    def __init__(self, cfg: dict, parent: AppEditMainView):
        super().__init__(timeout=180)
        self.cfg            = dict(cfg)
        self.parent         = parent
        self._staff_ids     = _parse_role_ids(cfg.get("staff_role_ids"))
        self._web_admin_ids = _parse_role_ids(cfg.get("web_admin_role_ids"))
        self._build()

    def build_embed(self) -> discord.Embed:
        def _ro(ids): return ", ".join(f"<@&{r}>" for r in ids) if ids else "*nicht gesetzt*"
        e = discord.Embed(
            title="🎭 Rollen bearbeiten",
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="🌱 Neulings-Rolle",
            value=f"<@&{self.cfg['newbie_role_id']}>" if self.cfg.get("newbie_role_id") else "*nicht gesetzt*",
            inline=True,
        )
        e.add_field(
            name="👥 Mitglieds-Rolle",
            value=f"<@&{self.cfg['member_role_id']}>" if self.cfg.get("member_role_id") else "*nicht gesetzt*",
            inline=True,
        )
        e.add_field(name="\u200b", value="\u200b", inline=True)
        e.add_field(name="👮 Staff-Rollen",     value=_ro(self._staff_ids),     inline=False)
        e.add_field(name="🌐 Web-Admin Rollen", value=_ro(self._web_admin_ids), inline=False)
        e.set_footer(text="Wähle Rollen und klicke 💾 Speichern – Änderungen werden sofort übernommen.")
        return e

    def _build(self):
        self.clear_items()

        newbie_sel = discord.ui.RoleSelect(
            placeholder="🌱 Neulings-Rolle ändern",
            min_values=1, max_values=1, row=0,
        )
        async def _newbie(i):
            self.cfg["newbie_role_id"] = i.data["values"][0]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        newbie_sel.callback = _newbie
        self.add_item(newbie_sel)

        member_sel = discord.ui.RoleSelect(
            placeholder="👥 Mitglieds-Rolle ändern",
            min_values=1, max_values=1, row=1,
        )
        async def _member(i):
            self.cfg["member_role_id"] = i.data["values"][0]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        member_sel.callback = _member
        self.add_item(member_sel)

        staff_sel = discord.ui.RoleSelect(
            placeholder="👮 Staff-Rollen neu setzen (alle auswählen)",
            min_values=1, max_values=10, row=2,
        )
        async def _staff(i):
            self._staff_ids = i.data["values"]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        staff_sel.callback = _staff
        self.add_item(staff_sel)

        web_sel = discord.ui.RoleSelect(
            placeholder="🌐 Web-Admin Rollen neu setzen",
            min_values=0, max_values=10, row=3,
        )
        async def _web(i):
            self._web_admin_ids = i.data["values"]
            await i.response.edit_message(embed=self.build_embed(), view=self)
        web_sel.callback = _web
        self.add_item(web_sel)

        btn_save = discord.ui.Button(
            label="💾 Alle Änderungen speichern",
            style=discord.ButtonStyle.success, row=4,
        )
        btn_save.callback = self._save
        self.add_item(btn_save)

    async def _save(self, interaction: discord.Interaction):
        try:
            get_supabase().table("application_servers").update({
                "newbie_role_id":      self.cfg.get("newbie_role_id"),
                "member_role_id":      self.cfg.get("member_role_id"),
                "staff_role_ids":      ",".join(self._staff_ids),
                "web_admin_role_ids":  ",".join(self._web_admin_ids),
            }).eq("server_id", self.parent.guild_id).execute()

            embed = self.build_embed()
            embed.title = "✅ Rollen gespeichert!"
            embed.color = discord.Color.green()
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# TEXTE BEARBEITEN (Modal)
# ══════════════════════════════════════════════════════════════════════════════

class AppTextsModal(discord.ui.Modal, title="Texte bearbeiten"):
    panel_msg = discord.ui.TextInput(
        label="Panel-Text",
        style=discord.TextStyle.paragraph,
        required=True, max_length=800,
    )
    welcome_msg = discord.ui.TextInput(
        label="Ticket-Embed-Text ({player}, {mc})",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1000,
    )
    instruction_msg = discord.ui.TextInput(
        label="Anweisungs-Text (im Bewerbungskanal)",
        style=discord.TextStyle.paragraph,
        required=False, max_length=1000,
        placeholder="📋 Willkommen! Schreibe hier deine Bewerbung…",
    )

    def __init__(self, cfg: dict, parent: "AppEditMainView"):
        super().__init__()
        self._cfg    = cfg
        self._parent = parent
        self.panel_msg.default      = (cfg.get("panel_message") or cfg.get("welcome_message") or "")[:800]
        self.welcome_msg.default     = (cfg.get("welcome_message") or "")[:1000]
        self.instruction_msg.default = (cfg.get("instruction_message") or "")[:1000]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            get_supabase().table("application_servers").update({
                "panel_message":       self.panel_msg.value,
                "welcome_message":     self.welcome_msg.value,
                "instruction_message": self.instruction_msg.value or "",
            }).eq("server_id", self._parent.guild_id).execute()

            await interaction.response.send_message(
                embed=discord.Embed(
                    title="✅ Texte gespeichert!",
                    description=(
                        f"**Panel-Text:**\n{self.panel_msg.value[:300]}\n\n"
                        f"**Ticket-Embed-Text:**\n{self.welcome_msg.value[:300]}\n\n"
                        f"**Anweisungs-Text:**\n{(self.instruction_msg.value or '–')[:300]}"
                    ),
                    color=discord.Color.green(),
                ),
                ephemeral=True,
            )
            await self._parent.refresh()
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# PANEL BEARBEITEN (in-place)
# ══════════════════════════════════════════════════════════════════════════════

class AppPanelEditView(discord.ui.View):
    def __init__(self, cfg: dict, bot: discord.Client, parent: AppEditMainView):
        super().__init__(timeout=180)
        self.cfg    = cfg
        self.bot    = bot
        self.parent = parent
        self._title = "⛏️ Bewerbung einreichen"
        self._desc  = cfg.get("panel_message") or cfg.get("welcome_message") or "Klicke auf den Button um deine Bewerbung einzureichen."
        self._build()

    def build_preview_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="📌 Panel bearbeiten",
            description=(
                "**Vorschau:**\n\n"
                f"**{self._title}**\n"
                f"{self._desc[:400]}"
            ),
            color=discord.Color.green(),
        )
        e.set_footer(text="Klicke '💾 Änderungen übernehmen' um die Panel-Nachricht zu aktualisieren.")
        return e

    def _build(self):
        self.clear_items()

        btn_text = discord.ui.Button(
            label="✏️ Titel & Text bearbeiten",
            style=discord.ButtonStyle.primary, row=0,
        )
        btn_text.callback = self._cb_text
        self.add_item(btn_text)

        btn_apply = discord.ui.Button(
            label="💾 Änderungen übernehmen",
            style=discord.ButtonStyle.success, row=0,
        )
        btn_apply.callback = self._cb_apply
        self.add_item(btn_apply)

    async def _cb_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AppPanelTextModal(
                current_title=self._title,
                current_desc=self._desc,
                panel_view=self,
            )
        )

    async def _cb_apply(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            panel_ch = self.bot.get_channel(int(self.cfg["panel_channel_id"]))
            if not panel_ch:
                await interaction.followup.send("❌ Panel-Kanal nicht gefunden.", ephemeral=True)
                return

            msg_id = self.cfg.get("panel_message_id")
            if not msg_id:
                await interaction.followup.send(
                    "❌ Keine Panel-Nachrichten-ID gespeichert.\n"
                    "Nutze `/bewerbung_setup` um das Panel neu zu senden.", ephemeral=True
                )
                return

            try:
                panel_msg = await panel_ch.fetch_message(int(msg_id))
            except discord.NotFound:
                await interaction.followup.send(
                    "❌ Die Panel-Nachricht wurde nicht gefunden (evtl. gelöscht).\n"
                    "Nutze `/bewerbung_setup` um das Panel neu zu senden.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title=self._title,
                description=self._desc,
                color=discord.Color.green(),
            )

            from bot.features.applications.views import ApplicationPanelView
            panel_view = ApplicationPanelView(bot=self.bot)
            await panel_msg.edit(embed=embed, view=panel_view)

            # Panel-Text getrennt vom Ticket-Embed-Text speichern
            get_supabase().table("application_servers").update({
                "panel_message": self._desc,
            }).eq("server_id", self.parent.guild_id).execute()

            for item in self.children:
                item.disabled = True
            await interaction.followup.send("✅ Panel-Nachricht wurde aktualisiert!", ephemeral=True)
            await self.parent.refresh()
        except Exception as e:
            logger.error(f"[AppPanelEditView._cb_apply] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class AppPanelTextModal(discord.ui.Modal, title="Panel-Text bearbeiten"):
    panel_title = discord.ui.TextInput(
        label="Panel-Titel",
        placeholder="⛏️ Bewerbung einreichen",
        required=True, max_length=100,
    )
    panel_desc = discord.ui.TextInput(
        label="Panel-Text",
        style=discord.TextStyle.paragraph,
        required=True, max_length=800,
        placeholder="Klicke auf den Button um deine Bewerbung einzureichen.",
    )

    def __init__(self, current_title: str, current_desc: str, panel_view: "AppPanelEditView"):
        super().__init__()
        self._panel_view          = panel_view
        self.panel_title.default = current_title
        self.panel_desc.default  = current_desc

    async def on_submit(self, interaction: discord.Interaction):
        self._panel_view._title = self.panel_title.value
        self._panel_view._desc  = self.panel_desc.value
        await interaction.response.edit_message(
            embed=self._panel_view.build_preview_embed(),
            view=self._panel_view,
        )
