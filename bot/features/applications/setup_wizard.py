"""
bot/features/applications/setup_wizard.py
==========================================
Schritt-für-Schritt Setup-Wizard für das Bewerbungs-System via Discord.

FLOW (4 Schritte):
  /bewerbung_setup
    → Step 1: Panel-Kanal + Bewerbungs-Kategorie
    → Step 2: Rollen (Neuling, Mitglied, Staff)
    → Step 3: Optionale Kanäle + Texte & Cooldown
    → Step 4: Bestätigung & Panel senden

Ersetze die bestehende ApplicationSetupView in views.py durch:
    from .setup_wizard import start_application_wizard
    await start_application_wizard(interaction, bot, existing_config)
"""

from __future__ import annotations

import os
import discord
from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("applications.wizard")

STEP_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


# ══════════════════════════════════════════════════════════════════════════════
# SHARED HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _step_bar(current: int, total: int = 4) -> str:
    parts = []
    for i in range(1, total + 1):
        if i < current:
            parts.append("✅")
        elif i == current:
            parts.append(f"**{STEP_EMOJIS[i-1]}**")
        else:
            parts.append("⬜")
    return " ".join(parts)


def _build_embed(
    step: int,
    title: str,
    description: str,
    fields: list[tuple[str, str, bool]] | None = None,
    color: discord.Color | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 Bewerbungs-Setup – Schritt {step}/4",
        color=color or discord.Color.green(),
    )
    embed.add_field(name="Fortschritt", value=_step_bar(step), inline=False)
    embed.add_field(name=f"{STEP_EMOJIS[step-1]} {title}", value=description, inline=False)
    if fields:
        for name, value, inline in fields:
            embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Jederzeit neu konfigurieren mit /bewerbung_setup")
    return embed


async def _save_config(guild_id: str, state: dict):
    """Speichert oder aktualisiert die Bewerbungs-Konfiguration in Supabase."""
    supabase = get_supabase()
    data = {
        "server_id":                str(guild_id),
        "panel_channel_id":         state.get("panel_channel_id"),
        "category_id":              state.get("category_id"),
        "newbie_role_id":           state.get("newbie_role_id"),
        "member_role_id":           state.get("member_role_id"),
        "staff_role_ids":           ",".join(state.get("staff_role_ids", [])),
        "log_channel_id":           state.get("log_channel_id"),
        "mc_log_channel_id":        state.get("mc_log_channel_id"),
        "welcome_message":          state.get("welcome_message", _DEFAULT_WELCOME),
        "instruction_message":      state.get("instruction_message", _DEFAULT_INSTRUCTION),
        "rejection_cooldown_hours": int(state.get("rejection_cooldown_hours", 24)),
        "web_admin_role_ids":       ",".join(state.get("web_admin_role_ids", [])),
        "panel_message":            state.get("panel_message", state.get("welcome_message", _DEFAULT_WELCOME)),
    }
    existing = supabase.table("application_servers").select("server_id").eq("server_id", str(guild_id)).execute()
    if existing.data:
        supabase.table("application_servers").update(data).eq("server_id", str(guild_id)).execute()
    else:
        data["app_counter"] = 0
        supabase.table("application_servers").insert(data).execute()


_DEFAULT_WELCOME = (
    "Willkommen {player}! Schreibe einen kurzen Text in dem du uns mitteilst "
    "wie wir dich nennen dürfen, was du gerne in Minecraft machst, "
    "wie lange du schon Minecraft spielst und warum du unserem Clan beitreten möchtest. 😊"
)
_DEFAULT_INSTRUCTION = (
    "📋 **Willkommen in deinem Bewerbungskanal!**\n"
    "Schreibe hier deine Bewerbung. Unser Staff wird sie so schnell wie möglich bearbeiten. "
    "Bitte sei geduldig und beantworte alle Fragen ehrlich. Viel Erfolg! 🍀"
)
_DEFAULT_PANEL_MESSAGE = "Klicke auf den Button um deine Bewerbung einzureichen."


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 – Panel-Kanal & Kategorie
# ══════════════════════════════════════════════════════════════════════════════

class AppWizardStep1View(discord.ui.View):
    """Schritt 1: Panel-Kanal und Bewerbungs-Kategorie wählen."""

    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        panel_sel = discord.ui.ChannelSelect(
            placeholder="📢 Panel-Kanal – wo der Bewerben-Button erscheint (Pflicht)",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        panel_sel.callback = self._on_panel
        self.add_item(panel_sel)

        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Bewerbungs-Kategorie – wo neue Kanäle erstellt werden (Pflicht)",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.category],
            row=1,
        )
        cat_sel.callback = self._on_cat
        self.add_item(cat_sel)

        ready = bool(self.state.get("panel_channel_id") and self.state.get("category_id"))
        next_btn = discord.ui.Button(
            label="Weiter → Rollen konfigurieren",
            style=discord.ButtonStyle.success,
            disabled=not ready,
            row=2,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        web_btn = discord.ui.Button(
            label="🌐 Im Browser einrichten",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        web_btn.callback = self._on_web
        self.add_item(web_btn)

    def build_embed(self) -> discord.Embed:
        panel = self.state.get("panel_channel_id")
        cat   = self.state.get("category_id")
        return _build_embed(
            step=1,
            title="Panel-Kanal & Kategorie",
            description=(
                "Wähle den Kanal für den **Bewerben-Button** und die Kategorie "
                "in der neue Bewerbungskanäle erstellt werden.\n\n"
                "💡 Empfehlung: `#bewerben` als Panel-Kanal und eine "
                "separate Kategorie `📋 Bewerbungen`."
            ),
            fields=[
                ("📢 Panel-Kanal",   f"<#{panel}>" if panel else "*nicht gesetzt*", True),
                ("📁 Kategorie",     f"<#{cat}>"   if cat   else "*nicht gesetzt*", True),
            ],
        )

    async def _on_panel(self, interaction: discord.Interaction):
        self.state["panel_channel_id"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_cat(self, interaction: discord.Interaction):
        self.state["category_id"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        view = AppWizardStep2View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_web(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🌐 Web-Setup",
            description=(
                f"Richte das Bewerbungs-System im Browser ein:\n\n"
                f"**[→ {WEB_BASE_URL}/dashboard/setup/applications?server_id={self.guild_id}]"
                f"({WEB_BASE_URL}/dashboard/setup/applications?server_id={self.guild_id})**"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 – Rollen
# ══════════════════════════════════════════════════════════════════════════════

class AppWizardStep2View(discord.ui.View):
    """Schritt 2: Neulings-, Mitglieds- und Staff-Rollen wählen."""

    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        newbie_sel = discord.ui.RoleSelect(
            placeholder="🌱 Neulings-Rolle – wird beim Beitreten vergeben (Pflicht)",
            min_values=1, max_values=1,
            row=0,
        )
        newbie_sel.callback = self._on_newbie
        self.add_item(newbie_sel)

        member_sel = discord.ui.RoleSelect(
            placeholder="👥 Mitglieds-Rolle – wird nach Annahme vergeben (Pflicht)",
            min_values=1, max_values=1,
            row=1,
        )
        member_sel.callback = self._on_member
        self.add_item(member_sel)

        staff_sel = discord.ui.RoleSelect(
            placeholder="👮 Staff-Rollen – Zugriff auf Bewerbungskanäle (Pflicht)",
            min_values=1, max_values=10,
            row=2,
        )
        staff_sel.callback = self._on_staff
        self.add_item(staff_sel)

        ready = bool(
            self.state.get("newbie_role_id")
            and self.state.get("member_role_id")
            and self.state.get("staff_role_ids")
        )
        next_btn = discord.ui.Button(
            label="Weiter → Optionale Einstellungen",
            style=discord.ButtonStyle.success,
            disabled=not ready,
            row=3,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        back_btn = discord.ui.Button(
            label="← Zurück",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        newbie = self.state.get("newbie_role_id")
        member = self.state.get("member_role_id")
        staff  = self.state.get("staff_role_ids", [])
        return _build_embed(
            step=2,
            title="Rollen konfigurieren",
            description=(
                "Wähle die drei Rollentypen für das Bewerbungs-System:\n\n"
                "🌱 **Neulings-Rolle** – automatisch beim Serverbeitritt vergeben, "
                "nur Neulings können sich bewerben.\n"
                "👥 **Mitglieds-Rolle** – wird nach erfolgreicher Bewerbung vergeben.\n"
                "👮 **Staff-Rollen** – sehen und bearbeiten Bewerbungskanäle."
            ),
            fields=[
                ("🌱 Neuling",  f"<@&{newbie}>" if newbie else "*nicht gesetzt*",                             True),
                ("👥 Mitglied", f"<@&{member}>" if member else "*nicht gesetzt*",                             True),
                ("👮 Staff",    ", ".join(f"<@&{r}>" for r in staff) if staff else "*nicht gesetzt*",         False),
            ],
        )

    async def _on_newbie(self, interaction: discord.Interaction):
        self.state["newbie_role_id"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_member(self, interaction: discord.Interaction):
        self.state["member_role_id"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_staff(self, interaction: discord.Interaction):
        self.state["staff_role_ids"] = interaction.data["values"]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_next(self, interaction: discord.Interaction):
        view = AppWizardStep3View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        view = AppWizardStep1View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 – Optionale Kanäle, Texte & Cooldown
# ══════════════════════════════════════════════════════════════════════════════

class AppWizardStep3View(discord.ui.View):
    """Schritt 3: Log-Kanäle, Texte und Cooldown (alles optional)."""

    def __init__(self, guild_id: int, bot: discord.Client, state: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.bot      = bot
        self.state    = state
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        log_sel = discord.ui.ChannelSelect(
            placeholder="📋 Log-Kanal (optional) – Bewerbungs-Protokolle",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        log_sel.callback = self._on_log
        self.add_item(log_sel)

        mc_sel = discord.ui.ChannelSelect(
            placeholder="⛏️ MC-Namen Log-Kanal (optional) – registrierte Minecraft-Namen",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=1,
        )
        mc_sel.callback = self._on_mc
        self.add_item(mc_sel)

        web_admin_sel = discord.ui.RoleSelect(
            placeholder="🌐 Web-Admin Rollen (optional) – Dashboard-Vollzugriff",
            min_values=0, max_values=10,
            row=2,
        )
        web_admin_sel.callback = self._on_web_admin
        self.add_item(web_admin_sel)

        texts_btn = discord.ui.Button(
            label="✏️ Texte & Cooldown bearbeiten",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        texts_btn.callback = self._on_texts
        self.add_item(texts_btn)

        next_btn = discord.ui.Button(
            label="Weiter → Abschließen",
            style=discord.ButtonStyle.success,
            row=3,
        )
        next_btn.callback = self._on_next
        self.add_item(next_btn)

        back_btn = discord.ui.Button(
            label="← Zurück",
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        log    = self.state.get("log_channel_id")
        mc_log = self.state.get("mc_log_channel_id")
        cooldown = self.state.get("rejection_cooldown_hours", 24)
        web_admins = self.state.get("web_admin_role_ids", [])
        return _build_embed(
            step=3,
            title="Optionale Einstellungen",
            description=(
                "Diese Einstellungen sind **optional** – klicke *Weiter* um zu überspringen.\n\n"
                "**📋 Log-Kanal:** Links zu allen Bewerbungen.\n"
                "**⛏️ MC-Log:** Minecraft-Namen der Bewerber werden hier gepostet.\n"
                "**🌐 Web-Admin:** Diese Rollen sehen alle Bewerbungen im Dashboard.\n"
                "**✏️ Texte:** Willkommens- und Anweisungstext anpassen."
            ),
            fields=[
                ("📋 Log-Kanal",      f"<#{log}>" if log else "*nicht gesetzt*",                              True),
                ("⛏️ MC-Log",         f"<#{mc_log}>" if mc_log else "*nicht gesetzt*",                        True),
                ("⏳ Cooldown",        f"{cooldown} Stunden nach Ablehnung",                                   True),
                ("🌐 Web-Admins",     ", ".join(f"<@&{r}>" for r in web_admins) if web_admins else "*keine*", False),
                ("📝 Panel-Text",      (self.state.get("panel_message") or _DEFAULT_PANEL_MESSAGE)[:300],       False),
            ],
        )

    async def _on_log(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.state["log_channel_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_mc(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.state["mc_log_channel_id"] = vals[0] if vals else None
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_web_admin(self, interaction: discord.Interaction):
        self.state["web_admin_role_ids"] = interaction.data.get("values", [])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_texts(self, interaction: discord.Interaction):
        await interaction.response.send_modal(AppTextsModal(self))

    async def _on_next(self, interaction: discord.Interaction):
        view = AppWizardStep4View(self.guild_id, self.bot, self.state, original_interaction=interaction)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _on_back(self, interaction: discord.Interaction):
        view = AppWizardStep2View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class AppTextsModal(discord.ui.Modal, title="Texte & Cooldown bearbeiten"):
    panel_msg = discord.ui.TextInput(
        label="Panel-Text (öffentlicher Button)",
        style=discord.TextStyle.paragraph,
        required=True, max_length=800,
        placeholder="Klicke auf den Button um deine Bewerbung einzureichen.",
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
    cooldown = discord.ui.TextInput(
        label="Cooldown nach Ablehnung (Stunden, 0 = keiner)",
        placeholder="24",
        required=True, max_length=6,
    )

    def __init__(self, parent: AppWizardStep3View):
        super().__init__()
        self._parent = parent
        self.panel_msg.default      = parent.state.get("panel_message", _DEFAULT_PANEL_MESSAGE)
        self.welcome_msg.default     = parent.state.get("welcome_message", _DEFAULT_WELCOME)
        self.instruction_msg.default = parent.state.get("instruction_message", _DEFAULT_INSTRUCTION)
        self.cooldown.default        = str(parent.state.get("rejection_cooldown_hours", 24))

    async def on_submit(self, interaction: discord.Interaction):
        self._parent.state["panel_message"]            = self.panel_msg.value
        self._parent.state["welcome_message"]          = self.welcome_msg.value
        self._parent.state["instruction_message"]      = self.instruction_msg.value or ""
        try:
            self._parent.state["rejection_cooldown_hours"] = max(0, int(self.cooldown.value))
        except ValueError:
            self._parent.state["rejection_cooldown_hours"] = 24
        await interaction.response.edit_message(embed=self._parent.build_embed(), view=self._parent)


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 – Bestätigung & Panel senden
# ══════════════════════════════════════════════════════════════════════════════

class AppWizardStep4View(discord.ui.View):
    """Schritt 4: Zusammenfassung, speichern und Panel senden."""

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

        back_btn = discord.ui.Button(
            label="← Zurück",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        back_btn.callback = self._on_back
        self.add_item(back_btn)

    def build_embed(self) -> discord.Embed:
        s = self.state
        staff = ", ".join(f"<@&{r}>" for r in s.get("staff_role_ids", [])) or "*keine*"
        web_admins = ", ".join(f"<@&{r}>" for r in s.get("web_admin_role_ids", [])) or "*keine*"
        return _build_embed(
            step=4,
            title="Alles bereit – Setup abschließen",
            description=(
                "Überprüfe deine Konfiguration und klicke **Setup abschließen** "
                "um das Bewerbungs-Panel in deinen konfigurierten Kanal zu senden."
            ),
            fields=[
                ("📢 Panel-Kanal",   f"<#{s.get('panel_channel_id','?')}>",                True),
                ("📁 Kategorie",     f"<#{s.get('category_id','?')}>",                     True),
                ("🌱 Neuling",       f"<@&{s.get('newbie_role_id','?')}>",                 True),
                ("👥 Mitglied",      f"<@&{s.get('member_role_id','?')}>",                 True),
                ("⏳ Cooldown",      f"{s.get('rejection_cooldown_hours', 24)}h",           True),
                ("📋 Log-Kanal",     f"<#{s.get('log_channel_id')}>" if s.get("log_channel_id") else "*–*", True),
                ("👮 Staff",         staff,                                                  False),
                ("🌐 Web-Admins",    web_admins,                                             False),
            ],
            color=discord.Color.green(),
        )

    async def _on_finish(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            from .views import ApplicationPanelView, _build_panel_embed

            guild    = interaction.guild
            guild_id = str(guild.id)
            state    = self.state

            # ── Konfiguration speichern ───────────────────────────────────────
            await _save_config(guild_id, state)

            # ── Panel senden ──────────────────────────────────────────────────
            panel_channel = guild.get_channel(int(state["panel_channel_id"]))
            if not panel_channel:
                await interaction.followup.send("❌ Panel-Kanal nicht gefunden!", ephemeral=True)
                return

            panel_text = state.get("panel_message") or state.get("welcome_message", _DEFAULT_WELCOME)
            embed      = _build_panel_embed(panel_text)
            panel_view   = ApplicationPanelView(bot=self.bot)
            panel_msg    = await panel_channel.send(embed=embed, view=panel_view)

            # Panel-Message-ID speichern
            supabase = get_supabase()
            supabase.table("application_servers").update({
                "panel_message_id": str(panel_msg.id),
            }).eq("server_id", guild_id).execute()

            # ── Erfolg ────────────────────────────────────────────────────────
            success_embed = discord.Embed(
                title="🎉 Bewerbungs-System eingerichtet!",
                description=(
                    f"✅ Panel in <#{state['panel_channel_id']}> gesendet!\n\n"
                    f"Neue Mitglieder bekommen automatisch die Neulings-Rolle "
                    f"<@&{state.get('newbie_role_id', '?')}> und können sich bewerben.\n\n"
                    f"**Tipp:** Alle Einstellungen im Browser anpassen:\n"
                    f"[→ {WEB_BASE_URL}/dashboard/setup/applications]"
                    f"({WEB_BASE_URL}/dashboard/setup/applications)"
                ),
                color=discord.Color.green(),
            )
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=success_embed, view=self)

        except Exception as e:
            logger.error(f"[AppWizard Step4] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    async def _on_web(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🌐 Web-Setup",
            description=(
                f"Verfeinere das Setup im Browser:\n\n"
                f"**[→ {WEB_BASE_URL}/dashboard/setup/applications?server_id={self.guild_id}]"
                f"({WEB_BASE_URL}/dashboard/setup/applications?server_id={self.guild_id})**"
            ),
            color=discord.Color.green(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _on_back(self, interaction: discord.Interaction):
        view = AppWizardStep3View(self.guild_id, self.bot, self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

async def start_application_wizard(
    interaction: discord.Interaction,
    bot: discord.Client,
    existing_config: dict | None = None,
) -> None:
    """
    Startet den Bewerbungs-Setup-Wizard.
    Wenn existing_config vorhanden, werden die Felder vorausgefüllt.
    """
    state: dict = {
        "panel_channel_id":         None,
        "category_id":              None,
        "newbie_role_id":           None,
        "member_role_id":           None,
        "staff_role_ids":           [],
        "log_channel_id":           None,
        "mc_log_channel_id":        None,
        "web_admin_role_ids":       [],
        "welcome_message":          _DEFAULT_WELCOME,
        "instruction_message":      _DEFAULT_INSTRUCTION,
        "panel_message":            _DEFAULT_PANEL_MESSAGE,
        "rejection_cooldown_hours": 24,
    }

    if existing_config:
        for key in [
            "panel_channel_id", "category_id", "newbie_role_id",
            "member_role_id", "log_channel_id", "mc_log_channel_id",
            "welcome_message", "instruction_message", "panel_message",
        ]:
            if existing_config.get(key):
                state[key] = existing_config[key]
        if not existing_config.get("panel_message") and existing_config.get("welcome_message"):
            state["panel_message"] = existing_config["welcome_message"]

        # Staff-Rollen als Liste
        raw_staff = existing_config.get("staff_role_ids", "")
        if isinstance(raw_staff, str):
            state["staff_role_ids"] = [r.strip() for r in raw_staff.split(",") if r.strip()]
        elif isinstance(raw_staff, list):
            state["staff_role_ids"] = raw_staff

        # Web-Admin-Rollen als Liste
        raw_web = existing_config.get("web_admin_role_ids", "")
        if isinstance(raw_web, str):
            state["web_admin_role_ids"] = [r.strip() for r in raw_web.split(",") if r.strip()]
        elif isinstance(raw_web, list):
            state["web_admin_role_ids"] = raw_web

        try:
            state["rejection_cooldown_hours"] = int(existing_config.get("rejection_cooldown_hours", 24))
        except (TypeError, ValueError):
            state["rejection_cooldown_hours"] = 24

    view = AppWizardStep1View(
        guild_id=interaction.guild_id,
        bot=bot,
        state=state,
    )

    await interaction.response.send_message(
        embed=view.build_embed(),
        view=view,
        ephemeral=True,
    )
