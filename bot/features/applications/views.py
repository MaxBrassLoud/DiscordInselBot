"""
applications/views.py
=====================
All Discord UI views for the application system.
"""

from __future__ import annotations

import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from .manager import ApplicationManager, load_application, load_app_messages

logger = get_logger("applications.views")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW (Legacy – wird durch setup_wizard ersetzt, aber noch vorhanden)
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
        self.acceptance_message: str = (
            "🎉 Herzlichen Glückwunsch {player}! Deine Bewerbung wurde angenommen. "
            "Ein Teammitglied wird dich bei Gelegenheit in den Mitgliederbereich einladen."
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
        e.add_field(name="🎉 Annahme-Text (im Ticket)", value=self.acceptance_message[:200], inline=False)
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
                "acceptance_message":       self.acceptance_message,
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
        e.add_field(name="🎉 Annahme-Text (im Ticket)", value=self._setup.acceptance_message[:300], inline=False)
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
    acceptance_msg = discord.ui.TextInput(
        label="Annahme-Text (im Ticket, {player}, {mc})",
        style=discord.TextStyle.paragraph,
        required=False, max_length=1000,
        placeholder="🎉 Herzlichen Glückwunsch {player}! Du wurdest angenommen...",
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
        self.acceptance_msg.default = getattr(setup_view, "acceptance_message", "")
        cooldown = getattr(setup_view, "rejection_cooldown_hours", 24)
        self.cooldown_hours.default = str(cooldown)

    async def on_submit(self, interaction: discord.Interaction):
        self._setup.panel_message = self.panel_msg.value
        self._setup.welcome_message = self.welcome_msg.value
        self._setup.instruction_message = self.instruction_msg.value or ""
        self._setup.acceptance_message = self.acceptance_msg.value or ""
        try:
            hours = max(0, int(self.cooldown_hours.value))
        except ValueError:
            hours = 24
        self._setup.rejection_cooldown_hours = hours

        if hasattr(self._setup, "_rebuild"):
            self._setup._rebuild()

        picker = StaffRolePickerView(setup_view=self._setup)
        await interaction.response.edit_message(embed=picker._build_embed(), view=picker)


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
                minecraft_name=mc, cfg=self.cfg, bot=self.bot,
            )

            # MC-Name via zentraler Logik speichern (optional)
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
                    set_nickname=False,
                )
                logger.info(f"[MinecraftNameModal] MC-Name '{mc}' für {applicant} gespeichert (message_id={new_msg_id})")
            except Exception as e:
                logger.warning(f"[MinecraftNameModal] update_minecraft_name fehlgeschlagen: {e}")

            # Welcome message
            welcome_text = (self.cfg.get("welcome_message") or "") \
                .replace("{player}", applicant.mention) \
                .replace("{mc}", mc)
            embed = discord.Embed(
                title=f"⛏️ Bewerbung von {mc}",
                description=welcome_text,
                color=discord.Color.blurple(),
            )
            embed.set_footer(text=f"Bewerbung #{app_id} · Discord: {applicant.display_name}")

            await channel.send(embed=embed)

            instruction = (self.cfg.get("instruction_message") or "").strip() \
                .replace("{player}", applicant.mention) \
                .replace("{mc}", mc)
            if instruction:
                await channel.send(instruction)

            # Log-Kanal
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
                    await log_ch.send(embed=log_embed)

            await interaction.followup.send(
                f"✅ Deine Bewerbung wurde eingereicht! {channel.mention}", ephemeral=True
            )
        except Exception as e:
            logger.error(f"[MinecraftNameModal] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION CHANNEL VIEW (dynamisch)
# ══════════════════════════════════════════════════════════════════════════════

class ApplicationChannelView(discord.ui.View):
    def __init__(self, app_id: int, server_id: str, applicant_id: str,
                 cfg: dict, bot: discord.Client, status: str = "open"):
        super().__init__(timeout=None)
        self.app_id       = app_id
        self.server_id    = server_id
        self.applicant_id = applicant_id
        self.cfg          = cfg
        self.bot          = bot
        self.status       = status          # "open" oder "accepted"
        self._claimed_by: str | None = None
        self._build()

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_ids = {r.strip() for r in (self.cfg.get("staff_role_ids") or "").split(",") if r.strip()}
        return bool(staff_ids & {str(r.id) for r in member.roles})

    def _build(self):
        self.clear_items()
        if self.status == "accepted":
            # Nur Schließen-Button
            btn_close = discord.ui.Button(
                label="🔒 Ticket schließen",
                style=discord.ButtonStyle.danger,
                custom_id=f"app_close_{self.app_id}",
            )
            btn_close.callback = self._close
            self.add_item(btn_close)
            return

        # Normale Buttons für offene Bewerbungen
        if self._claimed_by:
            btn_claim = discord.ui.Button(label="🔄 Abgeben",
                                          style=discord.ButtonStyle.secondary,
                                          custom_id=f"app_unclaim_{self.app_id}")
            btn_claim.callback = self._unclaim
        else:
            btn_claim = discord.ui.Button(label="📥 Übernehmen",
                                          style=discord.ButtonStyle.primary,
                                          custom_id=f"app_claim_{self.app_id}")
            btn_claim.callback = self._claim
        self.add_item(btn_claim)

        btn_accept = discord.ui.Button(label="✅ Annehmen",
                                       style=discord.ButtonStyle.success,
                                       custom_id=f"app_accept_{self.app_id}")
        btn_accept.callback = self._accept
        self.add_item(btn_accept)

        btn_reject = discord.ui.Button(label="❌ Ablehnen",
                                       style=discord.ButtonStyle.danger,
                                       custom_id=f"app_reject_{self.app_id}")
        btn_reject.callback = self._reject
        self.add_item(btn_reject)

    async def _claim(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        self._claimed_by = str(interaction.user.id)
        from .manager import update_application
        update_application(self.server_id, self.app_id, {"claimed_by": self._claimed_by})
        self._build()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"✅ {interaction.user.mention} hat die Bewerbung übernommen.")

    async def _unclaim(self, interaction: discord.Interaction):
        if str(interaction.user.id) != self._claimed_by and not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur der Bearbeiter.", ephemeral=True)
            return
        self._claimed_by = None
        from .manager import update_application
        update_application(self.server_id, self.app_id, {"claimed_by": None})
        self._build()
        await interaction.response.edit_message(view=self)
        await interaction.channel.send("🔄 Bewerbung freigegeben.")

    async def _accept(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        app = load_application(self.server_id, self.app_id)
        if not app:
            await interaction.response.send_message("❌ Bewerbung nicht gefunden.", ephemeral=True)
            return
        view = AcceptConfirmView(
            app=app,
            cfg=self.cfg,
            bot=self.bot,
            channel=interaction.channel,
        )
        await interaction.response.send_message("Bestätigung wurde in den Bewerbungskanal gesendet.", ephemeral=True)
        # Neutrale Nachricht ohne Erwähnung des auslösenden Staffs
        await interaction.channel.send(
            "⚠️ Bitte bestätige die Annahme dieser Bewerbung.",
            view=view,
        )

    async def _reject(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        app = load_application(self.server_id, self.app_id)
        if not app:
            await interaction.response.send_message("❌ Bewerbung nicht gefunden.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RejectModal(app=app, cfg=self.cfg, bot=self.bot, channel=interaction.channel)
        )

    async def _close(self, interaction: discord.Interaction):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff kann das Ticket schließen.", ephemeral=True)
            return
        view = CloseConfirmView(self.app_id, self.server_id, self.cfg, self.bot, interaction.channel)
        await interaction.response.send_message("Bist du sicher, dass du das Ticket schließen willst?", view=view, ephemeral=True)


# ── ANONYME ANNAHMEBESTÄTIGUNG ────────────────────────────────────────────────

class AcceptConfirmView(discord.ui.View):
    def __init__(self, app: dict, cfg: dict, bot: discord.Client, channel):
        super().__init__(timeout=120)
        self.app = app
        self.cfg = cfg
        self.bot = bot
        self.channel = channel
        # Keine requester_id mehr – jeder Staff kann bestätigen

    @discord.ui.button(label="✅ Bewerbung bestätigen", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Prüfe, ob der Benutzer Staff ist
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff kann bestätigen.", ephemeral=True)
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
            bot=self.bot,
        )

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff kann abbrechen.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(content="Annahme abgebrochen.", view=self)

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_ids = {r.strip() for r in (self.cfg.get("staff_role_ids") or "").split(",") if r.strip()}
        return bool(staff_ids & {str(r.id) for r in member.roles})


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


class CloseConfirmView(discord.ui.View):
    def __init__(self, app_id: int, server_id: str, cfg: dict, bot: discord.Client, channel):
        super().__init__(timeout=60)
        self.app_id = app_id
        self.server_id = server_id
        self.cfg = cfg
        self.bot = bot
        self.channel = channel

    @discord.ui.button(label="Ja, Ticket schließen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        app = load_application(self.server_id, self.app_id)
        if not app:
            await interaction.followup.send("Bewerbung nicht gefunden.", ephemeral=True)
            return
        await ApplicationManager.close_application_channel(
            guild=interaction.guild,
            channel=self.channel,
            app=app,
            closer=interaction.user,
        )
        await interaction.followup.send("Ticket wird geschlossen...", ephemeral=True)

    @discord.ui.button(label="Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._is_staff(interaction.user):
            await interaction.response.send_message("❌ Nur Staff.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Vorgang abgebrochen.", view=None)

    def _is_staff(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        staff_ids = {r.strip() for r in (self.cfg.get("staff_role_ids") or "").split(",") if r.strip()}
        return bool(staff_ids & {str(r.id) for r in member.roles})