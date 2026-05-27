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
    """

    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=600)
        self.guild_id              = str(guild_id)
        self.bot                   = bot
        self._original_interaction = None
        self._buttons_sent = False

        self.panel_channel_id: str | None    = None
        self.category_id: str | None         = None
        self.newbie_role_id: str | None      = None
        self.member_role_id: str | None      = None
        self.log_channel_id: str | None      = None
        self.mc_log_channel_id: str | None   = None
        self.staff_role_ids: list[str]       = []
        self.rejection_cooldown_hours: int   = 24
        self.panel_message: str              = "Klicke auf den Button um deine Bewerbung einzureichen."
        self.welcome_message: str            = (
            "Willkommen {player}! Schreibe einen kurzen Text in dem du uns mitteilst "
            "wie wir dich nennen dürfen, was du gerne in Minecraft machst, "
            "wie lange du schon Minecraft spielst und warum du unserem Clan beitreten möchtest. 😊"
        )
        self.instruction_message: str        = (
            "📋 **Willkommen in deinem Bewerbungskanal!**\n"
            "Schreibe hier deine Bewerbung. Unser Staff wird sie so schnell wie möglich bearbeiten. "
            "Bitte sei geduldig und beantworte alle Fragen ehrlich. Viel Erfolg! 🍀"
        )
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(title="⚙️ Bewerbungs-System Setup", color=discord.Color.blurple(),
                          description="Konfiguriere das Bewerbungs-System für diesen Server.")
        e.add_field(name="📢 Panel-Kanal",         value=f"<#{self.panel_channel_id}>"    if self.panel_channel_id    else "*nicht gesetzt*", inline=True)
        e.add_field(name="📁 Bewerbungs-Kategorie", value=f"<#{self.category_id}>"        if self.category_id         else "*nicht gesetzt*", inline=True)
        e.add_field(name="📋 Log-Kanal",            value=f"<#{self.log_channel_id}>"     if self.log_channel_id      else "*nicht gesetzt*", inline=True)
        e.add_field(name="⛏️ MC-Name Log-Kanal",   value=f"<#{self.mc_log_channel_id}>"  if self.mc_log_channel_id   else "*nicht gesetzt*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",       value=f"<@&{self.newbie_role_id}>"    if self.newbie_role_id      else "*nicht gesetzt*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle",      value=f"<@&{self.member_role_id}>"    if self.member_role_id      else "*nicht gesetzt*", inline=True)
        e.add_field(name="⏳ Cooldown nach Ablehnung", value=f"{self.rejection_cooldown_hours} Stunden", inline=True)
        staff = ", ".join(f"<@&{r}>" for r in self.staff_role_ids) if self.staff_role_ids else "*nicht gesetzt*"
        e.add_field(name="👮 Staff-Rollen",         value=staff[:300],                    inline=False)
        e.add_field(name="📝 Panel-Text",           value=self.panel_message[:200],       inline=False)
        e.add_field(name="💬 Willkommens-Text",     value=self.welcome_message[:200],     inline=False)
        e.add_field(name="📌 Anweisungs-Text (im Bewerbungskanal)", value=self.instruction_message[:200], inline=False)
        return e

    def _rebuild(self):
        self.clear_items()

        ch = discord.ui.ChannelSelect(placeholder="📢 Panel-Kanal auswählen",
                                      min_values=1, max_values=1,
                                      channel_types=[discord.ChannelType.text], row=0)
        async def _ch(i): self.panel_channel_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        ch.callback = _ch; self.add_item(ch)

        cat = discord.ui.ChannelSelect(placeholder="📁 Bewerbungs-Kategorie auswählen",
                                       min_values=1, max_values=1,
                                       channel_types=[discord.ChannelType.category], row=1)
        async def _cat(i): self.category_id = i.data["values"][0]; self._rebuild(); await i.response.edit_message(embed=self._build_embed(), view=self)
        cat.callback = _cat; self.add_item(cat)

        lc = discord.ui.ChannelSelect(placeholder="📋 Log-Kanal auswählen (optional)",
                                      min_values=0, max_values=1,
                                      channel_types=[discord.ChannelType.text], row=2)
        async def _lc(i):
            vals = i.data.get("values", [])
            self.log_channel_id = vals[0] if vals else None
            await i.response.edit_message(embed=self._build_embed(), view=self)
            await self._send_buttons_once(i)
        lc.callback = _lc; self.add_item(lc)

        nr = discord.ui.RoleSelect(placeholder="🌱 Neulings-Rolle auswählen",
                                   min_values=1, max_values=1, row=3)
        async def _nr(i):
            self.newbie_role_id = i.data["values"][0]
            await i.response.edit_message(embed=self._build_embed(), view=self)
            await self._send_buttons_once(i)
        nr.callback = _nr; self.add_item(nr)

        mr = discord.ui.RoleSelect(placeholder="👥 Mitglieds-Rolle auswählen",
                                   min_values=1, max_values=1, row=4)
        async def _mr(i):
            self.member_role_id = i.data["values"][0]
            await i.response.edit_message(embed=self._build_embed(), view=self)
            await self._send_buttons_once(i)
        mr.callback = _mr; self.add_item(mr)

    async def _send_buttons_once(self, interaction: discord.Interaction):
        if self._buttons_sent:
            return
        self._buttons_sent = True
        await interaction.followup.send(
            "👇 Wenn du fertig bist:",
            view=self.make_buttons_view(),
            ephemeral=True,
        )

    def make_buttons_view(self) -> discord.ui.View:
        v = discord.ui.View(timeout=600)

        btn_staff = discord.ui.Button(
            label="\U0001f46e Staff-Rollen & Texte",
            style=discord.ButtonStyle.secondary,
        )
        async def _open_staff(i: discord.Interaction):
            picker = StaffRolePickerView(setup_view=self)
            await i.response.send_message(embed=picker._build_embed(), view=picker, ephemeral=True)
        btn_staff.callback = _open_staff
        v.add_item(btn_staff)

        ready = all([self.panel_channel_id, self.category_id, self.newbie_role_id, self.member_role_id])
        btn_save = discord.ui.Button(
            label="\U0001f680 Setup abschlie\u00dfen",
            style=discord.ButtonStyle.success,
            disabled=not ready,
        )
        btn_save.callback = self._cb_save
        v.add_item(btn_save)

        return v

    async def _cb_save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            cfg = {
                "server_id":                self.guild_id,
                "panel_channel_id":         self.panel_channel_id,
                "category_id":              self.category_id,
                "newbie_role_id":           self.newbie_role_id,
                "member_role_id":           self.member_role_id,
                "log_channel_id":           self.log_channel_id,
                "mc_log_channel_id":        self.mc_log_channel_id,
                "staff_role_ids":           ",".join(self.staff_role_ids),
                "panel_message":            self.panel_message,
                "welcome_message":          self.welcome_message,
                "instruction_message":      self.instruction_message,
                "rejection_cooldown_hours": self.rejection_cooldown_hours,
                "app_counter":              0,
            }
            existing = supabase.table("application_servers").select("server_id").eq("server_id", self.guild_id).execute()
            if existing.data:
                supabase.table("application_servers").update(cfg).eq("server_id", self.guild_id).execute()
            else:
                supabase.table("application_servers").insert(cfg).execute()

            panel_channel = self.bot.get_channel(int(self.panel_channel_id))
            if panel_channel:
                embed = _build_panel_embed(self.panel_message)
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


# ── Staff Role Picker View ────────────────────────────────────────────────────

class StaffRolePickerView(discord.ui.View):
    def __init__(self, setup_view):
        super().__init__(timeout=180)
        self._setup = setup_view

        role_sel = discord.ui.RoleSelect(
            placeholder="👮 Staff-Rollen auswählen…",
            min_values=1, max_values=10,
            row=0,
        )
        role_sel.callback = self._roles_selected
        self.add_item(role_sel)

        mc_ch_sel = discord.ui.ChannelSelect(
            placeholder="⛏️ MC-Name Log-Kanal auswählen (optional)",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=1,
        )
        mc_ch_sel.callback = self._mc_channel_selected
        self.add_item(mc_ch_sel)

        btn_text = discord.ui.Button(
            label="💬 Texte & Cooldown bearbeiten",
            style=discord.ButtonStyle.secondary, row=2,
        )
        btn_text.callback = self._cb_text
        self.add_item(btn_text)

        is_setup = hasattr(setup_view, "_cb_save")
        if is_setup:
            btn_save = discord.ui.Button(
                label="🚀 Setup abschließen",
                style=discord.ButtonStyle.success, row=2,
            )
            btn_save.callback = self._cb_setup_save
            self.add_item(btn_save)
        else:
            btn_done = discord.ui.Button(
                label="✅ Fertig",
                style=discord.ButtonStyle.success, row=2,
            )
            btn_done.callback = self._cb_done
            self.add_item(btn_done)

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="👮 Staff-Rollen & Texte",
            color=discord.Color.blurple(),
            description="Wähle die Staff-Rollen aus und konfiguriere die Texte und den Cooldown.",
        )
        staff = (
            ", ".join(f"<@&{r}>" for r in self._setup.staff_role_ids)
            if self._setup.staff_role_ids else "*noch nicht ausgewählt*"
        )
        e.add_field(name="👮 Aktuelle Staff-Rollen", value=staff, inline=False)
        mc_ch = getattr(self._setup, "mc_log_channel_id", None)
        e.add_field(
            name="⛏️ MC-Name Log-Kanal",
            value=f"<#{mc_ch}>" if mc_ch else "*nicht gesetzt*",
            inline=False,
        )
        e.add_field(name="📝 Panel-Text",           value=self._setup.panel_message[:300],       inline=False)
        e.add_field(name="💬 Willkommens-Text",     value=self._setup.welcome_message[:300],     inline=False)
        e.add_field(name="📌 Anweisungs-Text (im Bewerbungskanal)", value=self._setup.instruction_message[:300], inline=False)
        cooldown = getattr(self._setup, "rejection_cooldown_hours", 24)
        e.add_field(name="⏳ Cooldown nach Ablehnung", value=f"{cooldown} Stunden", inline=False)
        return e

    async def _roles_selected(self, interaction: discord.Interaction):
        self._setup.staff_role_ids = interaction.data["values"]
        if hasattr(self._setup, "_rebuild"):
            self._setup._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _mc_channel_selected(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self._setup.mc_log_channel_id = vals[0] if vals else None
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _cb_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(TextsAndCooldownModal(self._setup))

    async def _cb_done(self, interaction: discord.Interaction):
        try:
            get_supabase().table("application_servers").update({
                "mc_log_channel_id": getattr(self._setup, "mc_log_channel_id", None),
            }).eq("server_id", self._setup.guild_id).execute()
        except Exception as e:
            logger.error(f"[StaffRolePickerView._cb_done] DB save mc_log_channel_id: {e}")

        if hasattr(self._setup, "_original_interaction") and self._setup._original_interaction:
            try:
                if hasattr(self._setup, "_rebuild"):
                    self._setup._rebuild()
                embed = self._setup._build_embed() if hasattr(self._setup, "_build_embed") else self._setup.build_embed()
                await self._setup._original_interaction.edit_original_response(embed=embed, view=self._setup)
            except Exception as e:
                logger.error(f"[StaffRolePickerView._cb_done] {e}")
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Einstellungen gespeichert",
                description=(
                    ", ".join(f"<@&{r}>" for r in self._setup.staff_role_ids) or "*keine*"
                ),
                color=discord.Color.green(),
            ),
            view=None,
        )

    async def _cb_setup_save(self, interaction: discord.Interaction):
        s = self._setup
        missing = []
        if not s.panel_channel_id: missing.append("📢 Panel-Kanal")
        if not s.category_id:      missing.append("📁 Kategorie")
        if not s.newbie_role_id:   missing.append("🌱 Neulings-Rolle")
        if not s.member_role_id:   missing.append("👥 Mitglieds-Rolle")
        if missing:
            await interaction.response.send_message(
                f"❌ Bitte zuerst ausfüllen: {', '.join(missing)}", ephemeral=True
            )
            return
        await s._cb_save(interaction)


# ── Texts & Cooldown Modal ────────────────────────────────────────────────────

class TextsAndCooldownModal(discord.ui.Modal, title="Texte & Cooldown bearbeiten"):
    panel_msg = discord.ui.TextInput(
        label="Panel-Text (öffentlicher Button)",
        style=discord.TextStyle.paragraph,
        required=True, max_length=800,
    )
    welcome_msg = discord.ui.TextInput(
        label="Embed-Text im Ticket ({player}, {mc})",
        style=discord.TextStyle.paragraph,
        required=True, max_length=1000,
    )
    instruction_msg = discord.ui.TextInput(
        label="Anweisungs-Text (im Bewerbungskanal)",
        style=discord.TextStyle.paragraph,
        required=False, max_length=1000,
        placeholder="z.B. 📋 Willkommen! Schreibe hier deine Bewerbung...",
    )
    cooldown_hours = discord.ui.TextInput(
        label="Cooldown nach Ablehnung (in Stunden)",
        placeholder="z.B. 24  (0 = kein Cooldown)",
        required=True, max_length=6,
    )

    def __init__(self, setup_view):
        super().__init__()
        self._setup = setup_view
        self.panel_msg.default = getattr(setup_view, "panel_message", "")
        self.welcome_msg.default = getattr(setup_view, "welcome_message", "")
        self.instruction_msg.default = getattr(setup_view, "instruction_message", "")
        cooldown = getattr(setup_view, "rejection_cooldown_hours", 24)
        self.cooldown_hours.default = str(cooldown)

    async def on_submit(self, interaction: discord.Interaction):
        self._setup.panel_message = self.panel_msg.value
        self._setup.welcome_message = self.welcome_msg.value
        self._setup.instruction_message = self.instruction_msg.value or ""
        try:
            hours = max(0, int(self.cooldown_hours.value))
        except ValueError:
            hours = 24
        self._setup.rejection_cooldown_hours = hours

        if hasattr(self._setup, "_rebuild"):
            self._setup._rebuild()

        picker = StaffRolePickerView(setup_view=self._setup)
        await interaction.response.edit_message(embed=picker._build_embed(), view=picker)


WelcomeMessageModal = TextsAndCooldownModal


# ══════════════════════════════════════════════════════════════════════════════
# EDIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationEditView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self._original_interaction = None
        cfg = self._load_cfg() or {}
        self.staff_role_ids: list[str] = [
            r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()
        ]
        self.panel_message: str = cfg.get("panel_message") or cfg.get("welcome_message", "")
        self.welcome_message: str = cfg.get("welcome_message", "")
        self.instruction_message: str = cfg.get("instruction_message", "")
        self.rejection_cooldown_hours: int = int(cfg.get("rejection_cooldown_hours") or 24)
        self.mc_log_channel_id: str | None = cfg.get("mc_log_channel_id")
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
        e.add_field(name="⛏️ MC-Name Log-Kanal", value=f"<#{cfg.get('mc_log_channel_id')}>" if cfg.get("mc_log_channel_id") else "*–*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",    value=f"<@&{cfg.get('newbie_role_id')}>"  if cfg.get("newbie_role_id")   else "*–*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle",   value=f"<@&{cfg.get('member_role_id')}>"  if cfg.get("member_role_id")   else "*–*", inline=True)
        e.add_field(name="⏳ Cooldown",          value=f"{cfg.get('rejection_cooldown_hours', 24)} Std.", inline=True)
        staff_ids = [r.strip() for r in (cfg.get("staff_role_ids") or "").split(",") if r.strip()]
        staff = ", ".join(f"<@&{r}>" for r in staff_ids) or "*–*"
        e.add_field(name="👮 Staff-Rollen", value=staff[:300], inline=False)
        e.add_field(name="📝 Panel-Text", value=(cfg.get("panel_message") or cfg.get("welcome_message") or "")[:200], inline=False)
        e.add_field(name="💬 Willkommens-Text", value=(cfg.get("welcome_message") or "")[:200], inline=False)
        instr = cfg.get("instruction_message") or ""
        e.add_field(name="📌 Anweisungs-Text (im Bewerbungskanal)", value=instr[:200] if instr else "*–*", inline=False)
        return e

    def _build_embed(self) -> discord.Embed:
        return self.build_embed()

    def _rebuild(self):
        self.clear_items()

        btn_channels = discord.ui.Button(label="⚙️ Kanäle & Rollen", style=discord.ButtonStyle.primary, row=0)
        btn_channels.callback = self._cb_channels
        self.add_item(btn_channels)

        btn_staff = discord.ui.Button(label="👮 Staff & Texte", style=discord.ButtonStyle.primary, row=0)
        btn_staff.callback = self._cb_staff
        self.add_item(btn_staff)

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

    async def _cb_staff(self, interaction: discord.Interaction):
        view = StaffRolePickerView(setup_view=self)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    async def _cb_panel(self, interaction: discord.Interaction):
        cfg = self._load_cfg()
        if not cfg or not cfg.get("panel_message_id"):
            await interaction.response.send_message(
                "❌ Keine Panel-Nachrichten-ID gefunden. Nutze `/bewerbung_setup`.", ephemeral=True)
            return
        view = AppPanelEditView(cfg=cfg, bot=self.bot, parent=self)
        await interaction.response.send_message(embed=view.build_preview_embed(), view=view, ephemeral=True)

    async def refresh(self):
        try:
            cfg = self._load_cfg()
            if cfg:
                get_supabase().table("application_servers").update({
                    "staff_role_ids":           ",".join(self.staff_role_ids),
                    "panel_message":            self.panel_message,
                    "welcome_message":          self.welcome_message,
                    "instruction_message":      self.instruction_message,
                    "rejection_cooldown_hours": self.rejection_cooldown_hours,
                    "mc_log_channel_id":        self.mc_log_channel_id,
                }).eq("server_id", self.guild_id).execute()
        except Exception as e:
            logger.error(f"[AppEditView.refresh save] {e}")

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
        e.add_field(name="📢 Panel-Kanal",        value=f"<#{self.cfg.get('panel_channel_id')}>"    if self.cfg.get("panel_channel_id")    else "*–*", inline=True)
        e.add_field(name="📁 Kategorie",          value=f"<#{self.cfg.get('category_id')}>"         if self.cfg.get("category_id")         else "*–*", inline=True)
        e.add_field(name="📋 Log-Kanal",          value=f"<#{self.cfg.get('log_channel_id')}>"      if self.cfg.get("log_channel_id")      else "*–*", inline=True)
        e.add_field(name="⛏️ MC-Name Log-Kanal",  value=f"<#{self.cfg.get('mc_log_channel_id')}>"   if self.cfg.get("mc_log_channel_id")   else "*–*", inline=True)
        e.add_field(name="🌱 Neulings-Rolle",     value=f"<@&{self.cfg.get('newbie_role_id')}>"     if self.cfg.get("newbie_role_id")      else "*–*", inline=True)
        e.add_field(name="👥 Mitglieds-Rolle",    value=f"<@&{self.cfg.get('member_role_id')}>"     if self.cfg.get("member_role_id")      else "*–*", inline=True)
        return e

    def _build(self):
        self.clear_items()
        for row_idx, (placeholder, key, types) in enumerate([
            ("📢 Panel-Kanal",          "panel_channel_id",  [discord.ChannelType.text]),
            ("📁 Kategorie",            "category_id",       [discord.ChannelType.category]),
            ("📋 Log-Kanal",            "log_channel_id",    [discord.ChannelType.text]),
            ("⛏️ MC-Name Log-Kanal",   "mc_log_channel_id", [discord.ChannelType.text]),
        ]):
            sel = discord.ui.ChannelSelect(placeholder=placeholder, min_values=1, max_values=1,
                                           channel_types=types, row=row_idx)
            async def _cb(i, k=key):
                self.cfg[k] = i.data["values"][0]
                await i.response.edit_message(embed=self.build_embed(), view=self)
            sel.callback = _cb; self.add_item(sel)

        nr = discord.ui.RoleSelect(placeholder="🌱 Neulings-Rolle", min_values=1, max_values=1, row=4)
        async def _nr(i): self.cfg["newbie_role_id"] = i.data["values"][0]; await i.response.edit_message(embed=self.build_embed(), view=self)
        nr.callback = _nr; self.add_item(nr)

        self._add_save_button()

    def _add_save_button(self):
        save = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success, row=4)
        save.callback = self._save
        self.add_item(save)

    async def _save(self, interaction: discord.Interaction):
        try:
            get_supabase().table("application_servers").update({
                "panel_channel_id":  self.cfg.get("panel_channel_id"),
                "category_id":       self.cfg.get("category_id"),
                "log_channel_id":    self.cfg.get("log_channel_id"),
                "mc_log_channel_id": self.cfg.get("mc_log_channel_id"),
                "newbie_role_id":    self.cfg.get("newbie_role_id"),
                "member_role_id":    self.cfg.get("member_role_id"),
            }).eq("server_id", self.parent.guild_id).execute()
            embed = self.build_embed(); embed.title = "✅ Gespeichert!"; embed.color = discord.Color.green()
            for item in self.children: item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
            await self.parent.refresh()
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class AppPanelEditView(discord.ui.View):
    def __init__(self, cfg: dict, bot: discord.Client, parent: ApplicationEditView):
        super().__init__(timeout=180)
        self.cfg    = cfg
        self.bot    = bot
        self.parent = parent
        self._title = "⛏️ Bewerbung einreichen"
        self._desc  = cfg.get("panel_message") or cfg.get("welcome_message", "")
        self._instruction = cfg.get("instruction_message", "")
        self._build()

    def build_preview_embed(self) -> discord.Embed:
        e = discord.Embed(title="✏️ Panel bearbeiten",
                          description=f"**{self._title}**\n{self._desc[:300]}",
                          color=discord.Color.blurple())
        if self._instruction:
            e.add_field(name="📌 Anweisungs-Text (im Bewerbungskanal)", value=self._instruction[:300], inline=False)
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

            if self._instruction is not None:
                get_supabase().table("application_servers").update({
                    "panel_message":       self._desc,
                    "instruction_message": self._instruction,
                }).eq("server_id", self.parent.guild_id).execute()

            for item in self.children: item.disabled = True
            await interaction.followup.send("✅ Panel aktualisiert!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class AppPanelTextModal(discord.ui.Modal, title="Panel-Text bearbeiten"):
    panel_title = discord.ui.TextInput(label="Titel", required=True, max_length=100)
    panel_desc  = discord.ui.TextInput(label="Panel-Text", style=discord.TextStyle.paragraph,
                                       required=True, max_length=800)
    panel_instr = discord.ui.TextInput(label="Anweisungs-Text (im Bewerbungskanal)", style=discord.TextStyle.paragraph,
                                       required=False, max_length=800,
                                       placeholder="z.B. 📋 Willkommen! Schreibe hier deine Bewerbung...")

    def __init__(self, view: AppPanelEditView):
        super().__init__()
        self._view = view
        self.panel_title.default = view._title
        self.panel_desc.default  = view._desc
        self.panel_instr.default = view._instruction or ""

    async def on_submit(self, interaction: discord.Interaction):
        self._view._title = self.panel_title.value
        self._view._desc  = self.panel_desc.value
        self._view._instruction = self.panel_instr.value or ""
        await interaction.response.edit_message(embed=self._view.build_preview_embed(), view=self._view)


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC PANEL
# ══════════════════════════════════════════════════════════════════════════════

def _build_panel_embed(welcome_text: str, title: str = "⛏️ Bewerbung einreichen") -> discord.Embed:
    return discord.Embed(
        title=title,
        description=welcome_text,
        color=discord.Color.green(),
    )


class ApplicationPanelView(discord.ui.View):
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

        newbie_role_id = cfg.get("newbie_role_id")
        if newbie_role_id:
            if not any(str(r.id) == str(newbie_role_id) for r in interaction.user.roles):
                await interaction.response.send_message(
                    "❌ Du benötigst die Neulings-Rolle um dich zu bewerben.", ephemeral=True
                )
                return

        cooldown_hours = int(cfg.get("rejection_cooldown_hours") or 0)
        if cooldown_hours > 0:
            from .manager import check_rejection_cooldown
            blocked, remaining = await check_rejection_cooldown(
                server_id=server_id,
                user_id=str(interaction.user.id),
                cooldown_hours=cooldown_hours,
            )
            if blocked:
                hours_left = int(remaining.total_seconds() // 3600)
                minutes_left = int((remaining.total_seconds() % 3600) // 60)
                await interaction.response.send_message(
                    f"❌ Du wurdest kürzlich abgelehnt. Du kannst dich erst in "
                    f"**{hours_left}h {minutes_left}m** wieder bewerben.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(MinecraftNameModal(cfg=cfg, bot=self.bot))


# ── Minecraft Name Modal ──────────────────────────────────────────────────────

# ═══════════════════════════════════════════════════════════════════
# ÄNDERUNGEN FÜR: bot/features/applications/views.py
# ═══════════════════════════════════════════════════════════════════
#
# Suche die Klasse `MinecraftNameModal` und ersetze die gesamte
# `on_submit`-Methode durch den folgenden Code.
#
# FIXES:
#   1. Kein doppelter MC-Log wenn /name bereits benutzt wurde
#      (update_minecraft_name editiert dann nur die bestehende Nachricht)
#   2. {player} im welcome_message und instruction_message wird durch
#      das Discord-Mention des Bewerbers ersetzt (statt MC-Name)
#   3. Log-Kanal-Embed bekommt einen Dashboard-Link
# ═══════════════════════════════════════════════════════════════════

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

            # ── FIX 1: MC-Name via zentraler Logik speichern ────────────────
            # Prüfen ob der Name schon via /name eingetragen wurde.
            # Falls ja → update_minecraft_name() editiert nur die bestehende
            # Nachricht im MC-Log-Kanal (kein neues Embed, kein zweiter Post).
            # Falls nein → wird eine neue Nachricht gepostet.
            try:
                from bot.core.supabase_client import get_supabase as _get_sb
                _existing = _get_sb().table("minecraft_names") \
                    .select("message_id") \
                    .eq("server_id", str(guild.id)) \
                    .eq("user_id", str(applicant.id)) \
                    .execute()
                _already_registered = bool(_existing.data)
            except Exception:
                _already_registered = False

            try:
                from bot.features.minecraft_names.cog import update_minecraft_name
                new_msg_id = await update_minecraft_name(
                    guild=guild,
                    member=applicant,
                    mc_name=mc,
                    set_nickname=False,  # Nickname bereits in create_application gesetzt
                )
                logger.info(
                    f"[MinecraftNameModal] MC-Name '{mc}' für {applicant} gespeichert "
                    f"(message_id={new_msg_id}, bereits_eingetragen={_already_registered})"
                )
            except Exception as e:
                logger.warning(f"[MinecraftNameModal] update_minecraft_name fehlgeschlagen: {e}")
            # ─────────────────────────────────────────────────────────────────

            # ── FIX 2: Welcome message – {player} = Mention, {mc} = MC-Name ──
            welcome_text = (self.cfg.get("welcome_message") or "") \
                .replace("{player}", applicant.mention) \
                .replace("{mc}", mc)

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

            # ── FIX 2: Instruction-Text – {player} = Mention, {mc} = MC-Name ─
            instruction = (self.cfg.get("instruction_message") or "").strip() \
                .replace("{player}", applicant.mention) \
                .replace("{mc}", mc)
            if instruction:
                await channel.send(instruction)

            # ── FIX 3: Log channel mit Dashboard-Link ────────────────────────
            if self.cfg.get("log_channel_id"):
                log_ch = guild.get_channel(int(self.cfg["log_channel_id"]))
                if log_ch:
                    from .manager import app_web_url
                    web_url = app_web_url(str(guild.id), app_id)
                    log_embed = discord.Embed(
                        title=f"📋 Neue Bewerbung #{app_id}",
                        color=discord.Color.blurple(),
                    )
                    log_embed.add_field(name="⛏️ Minecraft", value=mc,                inline=True)
                    log_embed.add_field(name="👤 Discord",   value=applicant.mention, inline=True)
                    log_embed.add_field(name="💬 Kanal",     value=channel.mention,   inline=True)
                    log_embed.add_field(
                        name="🌐 Dashboard",
                        value=f"[Bewerbung öffnen]({web_url})",
                        inline=False,
                    )
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


def _build_mc_log_embed(
    mc_name: str,
    applicant: discord.Member,
    app_id: int,
    channel: discord.TextChannel,
) -> discord.Embed:
    embed = discord.Embed(
        title="⛏️ Neuer Minecraft-Name registriert",
        color=discord.Color.from_rgb(89, 197, 98),
    )
    embed.add_field(name="🎮 Minecraft-Name", value=f"```{mc_name}```", inline=False)
    embed.add_field(name="👤 Discord-Nutzer", value=f"Name: {applicant.mention}", inline=True)
    embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{mc_name}/128")
    embed.set_footer(
        text=f"Bewerbung #{app_id}",
        icon_url=applicant.display_avatar.url if applicant.display_avatar else discord.Embed.Empty,
    )
    embed.timestamp = discord.utils.utcnow()
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION CHANNEL VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationChannelView(discord.ui.View):
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
        view = AcceptConfirmView(
            app=app,
            cfg=self.cfg,
            bot=self.bot,
            channel=interaction.channel,
            requester_id=str(interaction.user.id),
        )
        await interaction.response.send_message("Bestätigung wurde in den Bewerbungskanal gesendet.", ephemeral=True)
        await interaction.channel.send(
            f"⚠️ {interaction.user.mention}, bitte bestätige die Annahme von "
            f"**{app.get('minecraft_name', 'dieser Bewerbung')}**.",
            view=view,
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


class AcceptConfirmView(discord.ui.View):
    def __init__(self, app: dict, cfg: dict, bot: discord.Client, channel, requester_id: str):
        super().__init__(timeout=120)
        self.app = app
        self.cfg = cfg
        self.bot = bot
        self.channel = channel
        self.requester_id = requester_id

    async def _reject_wrong_user(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self.requester_id:
            return False
        await interaction.response.send_message(
            "❌ Nur der Mod, der auf Annehmen geklickt hat, kann diese Bestätigung nutzen.",
            ephemeral=True,
        )
        return True

    @discord.ui.button(label="✅ Annahme bestätigen", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        for item in self.children:
            item.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass
        await ApplicationManager.accept_application(
            guild=interaction.guild,
            channel=self.channel,
            app=self.app,
            acceptor=interaction.user,
            cfg=self.cfg,
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._reject_wrong_user(interaction):
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Annahme abgebrochen.", view=self)


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
