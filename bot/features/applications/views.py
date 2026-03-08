"""
applications/views.py
=====================
All Discord UI views for the application system:
  • ApplicationSetupView + modals  (/bewerbung_setup)
  • ApplicationEditView             (/bewerbung_bearbeiten)
  • ApplicationPanelView            – the public "Bewerben" button
  • ApplicationChannelView          – buttons inside the application channel
  • Reject modal
"""

from __future__ import annotations

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .manager import ApplicationManager, load_application, load_app_messages

logger = get_logger("applications.views")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationSetupView(discord.ui.View):
    """
    /bewerbung_setup  – configure the application system.
    Selects: panel channel, application category, newbie role,
             member role, staff roles, log channel, welcome message text.
    """

    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=600)
        self.guild_id              = str(guild_id)
        self.bot                   = bot
        self._original_interaction = None

        # Config state
        self.panel_channel_id: str | None    = None
        self.category_id: str | None         = None
        self.newbie_role_id: str | None      = None
        self.member_role_id: str | None      = None
        self.log_channel_id: str | None      = None
        self.staff_role_ids: list[str]       = []
        self.welcome_message: str            = (
            "Willkommen {player}! Schreibe einen kurzen Text in dem du uns mitteilst "
            "wie wir dich nennen dürfen, was du gerne in Minecraft machst, "
            "wie lange du schon Minecraft spielst und warum du unserem Clan beitreten möchtest. 😊"
        )
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(title="⚙️ Bewerbungs-System Setup", color=discord.Color.blurple(),
                          description="Konfiguriere das Bewerbungs-System für diesen Server.")
        e.add_field(name="📢 Panel-Kanal",       value=f"<#{self.panel_channel_id}>"  if self.panel_channel_id  else "*nicht gesetzt*", inline=True)
        e.add_field(name="📁 Bewerbungs-Kategorie", value=f"<#{self.category_id}>"    if self.category_id       else "*nicht gesetzt*", inline=True)
        e.add_field(name="📋 Log-Kanal",         value=f"<#{self.log_channel_id}>"   if self.log_channel_id    else "*nicht gesetzt*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",    value=f"<@&{self.newbie_role_id}>"  if self.newbie_role_id    else "*nicht gesetzt*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle",   value=f"<@&{self.member_role_id}>"  if self.member_role_id    else "*nicht gesetzt*", inline=True)
        staff = ", ".join(f"<@&{r}>" for r in self.staff_role_ids) if self.staff_role_ids else "*nicht gesetzt*"
        e.add_field(name="👮 Staff-Rollen",      value=staff[:300],                  inline=False)
        e.add_field(name="💬 Willkommens-Text",  value=self.welcome_message[:200],   inline=False)
        return e

    def _rebuild(self):
        self.clear_items()

        # Panel channel
        ch = discord.ui.ChannelSelect(placeholder="📢 Panel-Kanal auswählen",
                                      min_values=1, max_values=1,
                                      channel_types=[discord.ChannelType.text], row=0)
        async def _ch(i): self.panel_channel_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        ch.callback = _ch; self.add_item(ch)

        # Category
        cat = discord.ui.ChannelSelect(placeholder="📁 Bewerbungs-Kategorie auswählen",
                                       min_values=1, max_values=1,
                                       channel_types=[discord.ChannelType.category], row=1)
        async def _cat(i): self.category_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        cat.callback = _cat; self.add_item(cat)

        # Newbie role
        nr = discord.ui.RoleSelect(placeholder="🌱 Neulings-Rolle auswählen",
                                   min_values=1, max_values=1, row=2)
        async def _nr(i): self.newbie_role_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        nr.callback = _nr; self.add_item(nr)

        # Member role + staff roles on row 3
        mr = discord.ui.RoleSelect(placeholder="👥 Mitglieds-Rolle auswählen",
                                   min_values=1, max_values=1, row=3)
        async def _mr(i): self.member_role_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        mr.callback = _mr; self.add_item(mr)

        # Buttons on row 4
        btn_staff = discord.ui.Button(label="👮 Staff-Rollen & Text", style=discord.ButtonStyle.secondary, row=4)
        btn_staff.callback = self._cb_staff_modal
        self.add_item(btn_staff)

        ready = all([self.panel_channel_id, self.category_id, self.newbie_role_id, self.member_role_id])
        btn_save = discord.ui.Button(label="🚀 Setup abschließen", style=discord.ButtonStyle.success,
                                     disabled=not ready, row=4)
        btn_save.callback = self._cb_save
        self.add_item(btn_save)

    async def _cb_staff_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(StaffAndMessageModal(setup_view=self))

    async def _cb_save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            cfg = {
                "server_id":       self.guild_id,
                "panel_channel_id": self.panel_channel_id,
                "category_id":     self.category_id,
                "newbie_role_id":  self.newbie_role_id,
                "member_role_id":  self.member_role_id,
                "log_channel_id":  self.log_channel_id,
                "staff_role_ids":  ",".join(self.staff_role_ids),
                "welcome_message": self.welcome_message,
                "app_counter":     0,
            }
            existing = supabase.table("application_servers").select("server_id").eq("server_id", self.guild_id).execute()
            if existing.data:
                supabase.table("application_servers").update(cfg).eq("server_id", self.guild_id).execute()
            else:
                supabase.table("application_servers").insert(cfg).execute()

            # Send panel
            panel_channel = self.bot.get_channel(int(self.panel_channel_id))
            if panel_channel:
                embed = _build_panel_embed(self.welcome_message)
                panel_view = ApplicationPanelView(bot=self.bot)
                panel_msg = await panel_channel.send(embed=embed, view=panel_view)
                supabase.table("application_servers").update(
                    {"panel_message_id": str(panel_msg.id)}
                ).eq("server_id", self.guild_id).execute()

            for item in self.children: item.disabled = True
            await interaction.followup.send(
                f"✅ Bewerbungs-System eingerichtet! Panel in <#{self.panel_channel_id}> gesendet.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"[ApplicationSetupView.save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class StaffAndMessageModal(discord.ui.Modal, title="Staff & Willkommens-Text"):
    staff_roles = discord.ui.TextInput(
        label="Staff-Rollen IDs (kommagetrennt)",
        placeholder="123456789,987654321",
        required=False, max_length=500,
    )
    log_channel = discord.ui.TextInput(
        label="Log-Kanal ID (optional)",
        placeholder="Kanal-ID einfügen",
        required=False, max_length=30,
    )
    welcome_msg = discord.ui.TextInput(
        label="Willkommens-Text ({player} = Minecraft-Name)",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1000,
        default=(
            "Willkommen {player}! Schreibe einen kurzen Text in dem du uns mitteilst "
            "wie wir dich nennen dürfen, was du gerne in Minecraft machst, "
            "wie lange du schon Minecraft spielst und warum du unserem Clan beitreten möchtest. 😊"
        ),
    )

    def __init__(self, setup_view: ApplicationSetupView):
        super().__init__()
        self._setup = setup_view
        self.welcome_msg.default = setup_view.welcome_message

    async def on_submit(self, interaction: discord.Interaction):
        self._setup.staff_role_ids = [r.strip() for r in self.staff_roles.value.split(",") if r.strip()]
        self._setup.log_channel_id = self.log_channel.value.strip() or None
        self._setup.welcome_message = self.welcome_msg.value
        self._setup._rebuild()
        await interaction.response.edit_message(embed=self._setup._build_embed(), view=self._setup)


# ══════════════════════════════════════════════════════════════════════════════
# EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationEditView(discord.ui.View):
    """
    /bewerbung_bearbeiten  – edit all application system settings.
    Same structure as TicketEditMainView.
    """

    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self._original_interaction = None
        self._rebuild()

    def _load_cfg(self) -> dict | None:
        supabase = get_supabase()
        r = supabase.table("application_servers").select("*").eq("server_id", self.guild_id).execute()
        return r.data[0] if r.data else None

    def build_embed(self) -> discord.Embed:
        cfg = self._load_cfg()
        if not cfg:
            return discord.Embed(title="❌ Nicht eingerichtet",
                                 description="Nutze `/bewerbung_setup` zuerst.", color=discord.Color.red())
        e = discord.Embed(title="✏️ Bewerbungs-System bearbeiten", color=discord.Color.blurple())
        e.add_field(name="📢 Panel-Kanal",       value=f"<#{cfg.get('panel_channel_id')}>" if cfg.get("panel_channel_id") else "*–*", inline=True)
        e.add_field(name="📁 Kategorie",         value=f"<#{cfg.get('category_id')}>"      if cfg.get("category_id")      else "*–*", inline=True)
        e.add_field(name="📋 Log-Kanal",         value=f"<#{cfg.get('log_channel_id')}>"   if cfg.get("log_channel_id")   else "*–*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",    value=f"<@&{cfg.get('newbie_role_id')}>"  if cfg.get("newbie_role_id")   else "*–*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle",   value=f"<@&{cfg.get('member_role_id')}>"  if cfg.get("member_role_id")   else "*–*", inline=True)
        staff_ids = [r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()]
        staff = ", ".join(f"<@&{r}>" for r in staff_ids) or "*–*"
        e.add_field(name="👮 Staff-Rollen", value=staff[:300], inline=False)
        e.add_field(name="💬 Willkommens-Text", value=(cfg.get("welcome_message") or "")[:200], inline=False)
        return e

    def _rebuild(self):
        self.clear_items()

        btn_channels = discord.ui.Button(label="⚙️ Kanäle & Rollen", style=discord.ButtonStyle.primary, row=0)
        btn_channels.callback = self._cb_channels
        self.add_item(btn_channels)

        btn_msg = discord.ui.Button(label="💬 Text bearbeiten", style=discord.ButtonStyle.secondary, row=0)
        btn_msg.callback = self._cb_message
        self.add_item(btn_msg)

        btn_panel = discord.ui.Button(label="✏️ Panel bearbeiten", style=discord.ButtonStyle.secondary, row=0)
        btn_panel.callback = self._cb_panel
        self.add_item(btn_panel)

    async def _cb_channels(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg:
            await interaction.response.send_message("❌ Nicht eingerichtet.", ephemeral=True)
            return
        view = AppChannelSettingsView(cfg=cfg, parent=self)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    async def _cb_message(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        await interaction.response.send_modal(AppMessageEditModal(cfg=cfg or {}, parent=self))

    async def _cb_panel(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg or not cfg.get("panel_message_id"):
            await interaction.response.send_message(
                "❌ Keine Panel-Nachrichten-ID gefunden. Nutze `/bewerbung_setup`.", ephemeral=True)
            return
        view = AppPanelEditView(cfg=cfg, bot=self.bot, parent=self)
        await interaction.response.send_message(embed=view.build_preview_embed(), view=view, ephemeral=True)

    async def refresh(self):
        if self._original_interaction:
            try:
                await self._original_interaction.edit_original_response(embed=self.build_embed(), view=self)
            except Exception as e:
                logger.error(f"[AppEditView.refresh] {e}")


class AppChannelSettingsView(discord.ui.View):
    def __init__(self, cfg: dict, parent: ApplicationEditView):
        super().__init__(timeout=180)
        self.cfg    = dict(cfg)
        self.parent = parent
        self._build()

    def build_embed(self) -> discord.Embed:
        e = discord.Embed(title="⚙️ Kanäle & Rollen bearbeiten", color=discord.Color.blurple())
        e.add_field(name="📢 Panel-Kanal",     value=f"<#{self.cfg.get('panel_channel_id')}>" if self.cfg.get("panel_channel_id") else "*–*", inline=True)
        e.add_field(name="📁 Kategorie",       value=f"<#{self.cfg.get('category_id')}>"      if self.cfg.get("category_id")      else "*–*", inline=True)
        e.add_field(name="📋 Log-Kanal",       value=f"<#{self.cfg.get('log_channel_id')}>"   if self.cfg.get("log_channel_id")   else "*–*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",  value=f"<@&{self.cfg.get('newbie_role_id')}>"  if self.cfg.get("newbie_role_id")   else "*–*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle", value=f"<@&{self.cfg.get('member_role_id')}>"  if self.cfg.get("member_role_id")   else "*–*", inline=True)
        return e

    def _build(self):
        self.clear_items()
        for row_idx, (placeholder, key, types) in enumerate([
            ("📢 Panel-Kanal",       "panel_channel_id", [discord.ChannelType.text]),
            ("📁 Kategorie",         "category_id",      [discord.ChannelType.category]),
            ("📋 Log-Kanal",         "log_channel_id",   [discord.ChannelType.text]),
        ]):
            sel = discord.ui.ChannelSelect(placeholder=placeholder, min_values=1, max_values=1,
                                           channel_types=types, row=row_idx)
            async def _cb(i, k=key):
                self.cfg[k] = i.data["values"][0]
                await i.response.edit_message(embed=self.build_embed(), view=self)
            sel.callback = _cb; self.add_item(sel)

        nr = discord.ui.RoleSelect(placeholder="🌱 Neulings-Rolle", min_values=1, max_values=1, row=3)
        async def _nr(i): self.cfg["newbie_role_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        nr.callback = _nr; self.add_item(nr)

        save = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success, row=4)
        save.callback = self._save; self.add_item(save)

    async def _save(self, interaction: discord.Interaction):
        try:
            get_supabase().table("application_servers").update({
                "panel_channel_id": self.cfg.get("panel_channel_id"),
                "category_id":      self.cfg.get("category_id"),
                "log_channel_id":   self.cfg.get("log_channel_id"),
                "newbie_role_id":   self.cfg.get("newbie_role_id"),
                "member_role_id":   self.cfg.get("member_role_id"),
            }).eq("server_id", self.parent.guild_id).execute()
            embed = self.build_embed(); embed.title = "✅ Gespeichert!"; embed.color = discord.Color.green()
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class AppMessageEditModal(discord.ui.Modal, title="Willkommens-Text bearbeiten"):
    welcome_msg = discord.ui.TextInput(
        label="Willkommens-Text ({player} = Minecraft-Name)",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1000,
    )
    staff_roles = discord.ui.TextInput(
        label="Staff-Rollen IDs (kommagetrennt)",
        placeholder="123456789,987654321",
        required=False, max_length=500,
    )

    def __init__(self, cfg: dict, parent: ApplicationEditView):
        super().__init__()
        self._parent = parent
        self.welcome_msg.default = cfg.get("welcome_message", "")
        self.staff_roles.default = cfg.get("staff_role_ids", "")

    async def on_submit(self, interaction: discord.Interaction):
        get_supabase().table("application_servers").update({
            "welcome_message": self.welcome_msg.value,
            "staff_role_ids":  self.staff_roles.value.strip(),
        }).eq("server_id", self._parent.guild_id).execute()
        await interaction.response.send_message("✅ Text und Staff-Rollen gespeichert!", ephemeral=True)
        await self._parent.refresh()


class AppPanelEditView(discord.ui.View):
    def __init__(self, cfg: dict, bot: discord.Client, parent: ApplicationEditView):
        super().__init__(timeout=180)
        self.cfg    = cfg
        self.bot    = bot
        self.parent = parent
        self._title = "⛏️ Bewerbung einreichen"
        self._desc  = cfg.get("welcome_message", "")
        self._build()

    def build_preview_embed(self) -> discord.Embed:
        e = discord.Embed(title="✏️ Panel bearbeiten",
                          description=f"**{self._title}**\n{self._desc[:300]}",
                          color=discord.Color.blurple())
        e.set_footer(text="Klicke 'Änderungen übernehmen' um das Panel zu aktualisieren.")
        return e

    def _build(self):
        self.clear_items()
        btn_text = discord.ui.Button(label="✏️ Titel & Text", style=discord.ButtonStyle.primary, row=0)
        btn_text.callback = lambda i: i.response.send_modal(AppPanelTextModal(view=self))
        self.add_item(btn_text)
        btn_apply = discord.ui.Button(label="✅ Übernehmen", style=discord.ButtonStyle.success, row=0)
        btn_apply.callback = self._apply; self.add_item(btn_apply)

    async def _apply(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            ch = self.bot.get_channel(int(self.cfg["panel_channel_id"]))
            msg = await ch.fetch_message(int(self.cfg["panel_message_id"]))
            embed = _build_panel_embed(self._desc, title=self._title)
            panel_view = ApplicationPanelView(bot=self.bot)
            await msg.edit(embed=embed, view=panel_view)
            for item in self.children: item.disabled = True
            await interaction.followup.send("✅ Panel aktualisiert!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class AppPanelTextModal(discord.ui.Modal, title="Panel-Text bearbeiten"):
    panel_title = discord.ui.TextInput(label="Titel", required=True, max_length=100)
    panel_desc  = discord.ui.TextInput(label="Beschreibung", style=discord.TextStyle.paragraph,
                                       required=True, max_length=800)

    def __init__(self, view: AppPanelEditView):
        super().__init__()
        self._view = view
        self.panel_title.default = view._title
        self.panel_desc.default  = view._desc

    async def on_submit(self, interaction: discord.Interaction):
        self._view._title = self.panel_title.value
        self._view._desc  = self.panel_desc.value
        await interaction.response.edit_message(embed=self._view.build_preview_embed(), view=self._view)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC PANEL  –  single "Bewerben" button
# ══════════════════════════════════════════════════════════════════════════════

def _build_panel_embed(welcome_text: str, title: str = "⛏️ Bewerbung einreichen") -> discord.Embed:
    return discord.Embed(
        title=title,
        description=welcome_text,
        color=discord.Color.green(),
    )


class ApplicationPanelView(discord.ui.View):
    """Persistent single-button panel. custom_id is stable."""

    def __init__(self, bot: discord.Client):
        super().__init__(timeout=None)
        self.bot = bot
        btn = discord.ui.Button(
            label="📝 Bewerben",
            style=discord.ButtonStyle.success,
            custom_id="app_apply_button",
            emoji="⛏️",
        )
        btn.callback = self._cb_apply
        self.add_item(btn)

    async def _cb_apply(self, interaction: discord.Interaction):
        from bot.core.supabase_client import get_supabase
        supabase  = get_supabase()
        server_id = str(interaction.guild_id)
        cfg = supabase.table("application_servers").select("*").eq("server_id", server_id).execute()
        if not cfg.data:
            await interaction.response.send_message("❌ Bewerbungs-System nicht eingerichtet.", ephemeral=True)
            return
        cfg = cfg.data[0]

        # Check newbie role
        newbie_role_id = cfg.get("newbie_role_id")
        if newbie_role_id:
            if not any(str(r.id) == str(newbie_role_id) for r in interaction.user.roles):
                await interaction.response.send_message(
                    "❌ Du benötigst die Neulings-Rolle um dich zu bewerben.", ephemeral=True
                )
                return

        await interaction.response.send_modal(MinecraftNameModal(cfg=cfg, bot=self.bot))


# ── Minecraft Name Modal ──────────────────────────────────────────────────────

class MinecraftNameModal(discord.ui.Modal, title="Bewerbung einreichen"):
    mc_name = discord.ui.TextInput(
        label="Dein Minecraft Name",
        placeholder="z.B. Steve123",
        required=True, max_length=50,
    )

    def __init__(self, cfg: dict, bot: discord.Client):
        super().__init__()
        self.cfg = cfg
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            guild     = interaction.guild
            applicant = interaction.user
            mc        = self.mc_name.value.strip()

            channel, app_id = await ApplicationManager.create_application(
                guild=guild, applicant=applicant,
                minecraft_name=mc, cfg=self.cfg,
            )

            # Build welcome message in the new channel
            welcome_text = (self.cfg.get("welcome_message") or "").replace("{player}", mc)
            embed = discord.Embed(
                title=f"⛏️ Bewerbung von {mc}",
                description=welcome_text,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Bewerbung #{app_id} · Discord: {applicant.display_name}")

            view = ApplicationChannelView(
                app_id=app_id, server_id=str(guild.id),
                applicant_id=str(applicant.id), cfg=self.cfg, bot=self.bot,
            )
            await channel.send(embed=embed, view=view)

            # Post to log channel if configured
            if self.cfg.get("log_channel_id"):
                log_ch = guild.get_channel(int(self.cfg["log_channel_id"]))
                if log_ch:
                    log_embed = discord.Embed(
                        title=f"📋 Neue Bewerbung #{app_id}",
                        color=discord.Color.blurple(),
                    )
                    log_embed.add_field(name="⛏️ Minecraft", value=mc, inline=True)
                    log_embed.add_field(name="👤 Discord",   value=applicant.mention, inline=True)
                    log_embed.add_field(name="💬 Kanal",     value=channel.mention,   inline=True)
                    try:
                        await log_ch.send(embed=log_embed)
                    except Exception:
                        pass

            await interaction.followup.send(
                f"✅ Deine Bewerbung wurde eingereicht! {channel.mention}", ephemeral=True
            )
        except Exception as e:
            logger.error(f"[MinecraftNameModal] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION CHANNEL VIEW  –  buttons inside the application channel
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationChannelView(discord.ui.View):
    """
    3 buttons: Claim/Unclaim · Accept · Reject
    Persists via custom_id so it survives bot restarts.
    """

    def __init__(self, app_id: int, server_id: str, applicant_id: str,
                 cfg: dict, bot: discord.Client):
        super().__init__(timeout=None)
        self.app_id       = app_id
        self.server_id    = server_id
        self.applicant_id = applicant_id
        self.cfg          = cfg
        self.bot          = bot
        self._claimed_by: str | None = None
        self._build()

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_ids = {r.strip() for r in (self.cfg.get("staff_role_ids") or "").split(",") if r.strip()}
        return bool(staff_ids & {str(r.id) for r in member.roles})

    def _build(self):
        self.clear_items()
        aid = self.app_id

        if self._claimed_by:
            btn_claim = discord.ui.Button(label="🔄 Abgeben",
                                          style=discord.ButtonStyle.secondary,
                                          custom_id=f"app_unclaim_{aid}")
            btn_claim.callback = self._unclaim
        else:
            btn_claim = discord.ui.Button(label="📥 Übernehmen",
                                          style=discord.ButtonStyle.primary,
                                          custom_id=f"app_claim_{aid}")
            btn_claim.callback = self._claim
        self.add_item(btn_claim)

        btn_accept = discord.ui.Button(label="✅ Annehmen",
                                       style=discord.ButtonStyle.success,
                                       custom_id=f"app_accept_{aid}")
        btn_accept.callback = self._accept
        self.add_item(btn_accept)

        btn_reject = discord.ui.Button(label="❌ Ablehnen",
                                       style=discord.ButtonStyle.danger,
                                       custom_id=f"app_reject_{aid}")
        btn_reject.callback = self._reject
        self.add_item(btn_reject)

    async def _claim(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True); return
        self._claimed_by = str(interaction.user.id)
        from .manager import update_application
        update_application(self.server_id, self.app_id, {"claimed_by": self._claimed_by})
        self._build()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✅ {interaction.user.mention} hat die Bewerbung übernommen.")

    async def _unclaim(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self._claimed_by and not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur der Bearbeiter.", ephemeral=True); return
        self._claimed_by = None
        from .manager import update_application
        update_application(self.server_id, self.app_id, {"claimed_by": None})
        self._build()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send("🔄 Bewerbung freigegeben.")

    async def _accept(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True); return
        app = load_application(self.server_id, self.app_id)
        if not app:
            await interaction.response.send_message("❌ Bewerbung nicht gefunden.", ephemeral=True); return
        await interaction.response.defer(ephemeral=True)
        await ApplicationManager.accept_application(
            guild=interaction.guild, channel=interaction.channel,
            app=app, acceptor=interaction.user, cfg=self.cfg,
        )

    async def _reject(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True); return
        app = load_application(self.server_id, self.app_id)
        if not app:
            await interaction.response.send_message("❌ Bewerbung nicht gefunden.", ephemeral=True); return
        await interaction.response.send_modal(
            RejectModal(app=app, cfg=self.cfg, bot=self.bot, channel=interaction.channel)
        )


class RejectModal(discord.ui.Modal, title="Bewerbung ablehnen"):
    reason = discord.ui.TextInput(
        label="Begründung",
        placeholder="Warum wird die Bewerbung abgelehnt?",
        style=discord.TextStyle.paragraph,
        required=True, max_length=500,
    )

    def __init__(self, app: dict, cfg: dict, bot: discord.Client, channel):
        super().__init__()
        self.app     = app
        self.cfg     = cfg
        self.bot     = bot
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await ApplicationManager.reject_application(
            guild=interaction.guild, channel=self.channel,
            app=self.app, rejector=interaction.user,
            reason=self.reason.value, cfg=self.cfg,
        )