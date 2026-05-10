"""
bot/features/moderation/cog.py
================================
Moderations-System mit:
  - /moderation setup      – Log-Kanal für Moderationsaktionen festlegen
  - /moderation timeout    – User timeout (Dauer oder bis zu einem Zeitpunkt)
  - Automatisches Logging wenn Discord-Timeout (nativ) gesetzt/aufgehoben wird

SUPABASE SQL (einmalig ausführen):
    ALTER TABLE settings
        ADD COLUMN IF NOT EXISTS moderation_log_channel_id TEXT;

    CREATE TABLE IF NOT EXISTS moderation_logs (
        id          BIGSERIAL PRIMARY KEY,
        server_id   TEXT NOT NULL,
        action      TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        target_name TEXT,
        moderator_id   TEXT,
        moderator_name TEXT,
        reason      TEXT,
        duration_seconds INTEGER,
        until       TIMESTAMPTZ,
        created_at  TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS idx_moderation_logs_server
        ON moderation_logs (server_id, created_at DESC);
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("moderation")

TZ_BERLIN = timezone(timedelta(hours=2))  # CEST; fällt auf CET zurück – reicht für Anzeige


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_log_channel_id(server_id: str) -> str | None:
    try:
        r = get_supabase().table("settings") \
            .select("moderation_log_channel_id") \
            .eq("guild_id", server_id).execute()
        if r.data:
            return r.data[0].get("moderation_log_channel_id") or None
    except Exception as e:
        logger.error(f"[moderation] _get_log_channel_id: {e}")
    return None


def _set_log_channel_id(server_id: str, channel_id: str):
    sb = get_supabase()
    existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
    data = {"guild_id": server_id, "moderation_log_channel_id": channel_id}
    if existing.data:
        sb.table("settings").update(data).eq("guild_id", server_id).execute()
    else:
        sb.table("settings").insert(data).execute()


def _log_action(
    server_id: str,
    action: str,
    target: discord.Member,
    moderator: discord.Member | None,
    reason: str | None,
    until: datetime | None,
):
    try:
        duration_sec = None
        if until:
            delta = until - datetime.now(timezone.utc)
            duration_sec = max(0, int(delta.total_seconds()))

        get_supabase().table("moderation_logs").insert({
            "server_id":       server_id,
            "action":          action,
            "target_id":       str(target.id),
            "target_name":     str(target),
            "moderator_id":    str(moderator.id) if moderator else None,
            "moderator_name":  str(moderator) if moderator else None,
            "reason":          reason,
            "duration_seconds": duration_sec,
            "until":           until.isoformat() if until else None,
            "created_at":      datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"[moderation] _log_action DB: {e}")


def _format_duration(seconds: int) -> str:
    """Formatiert Sekunden als lesbare Zeitspanne."""
    if seconds <= 0:
        return "0 Sekunden"
    parts = []
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        parts.append(f"{days} Tag{'e' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} Stunde{'n' if hours != 1 else ''}")
    if minutes:
        parts.append(f"{minutes} Minute{'n' if minutes != 1 else ''}")
    if secs and not days:
        parts.append(f"{secs} Sekunde{'n' if secs != 1 else ''}")
    return ", ".join(parts) or "wenige Sekunden"


async def _send_log_embed(
    guild: discord.Guild,
    action: str,
    target: discord.Member,
    moderator: discord.Member | None,
    reason: str | None,
    until: datetime | None,
    removed: bool = False,
):
    """Sendet ein Embed in den konfigurierten Log-Kanal."""
    log_ch_id = _get_log_channel_id(str(guild.id))
    if not log_ch_id:
        return
    log_ch = guild.get_channel(int(log_ch_id))
    if not log_ch:
        return

    if removed:
        color = discord.Color.green()
        title = "🔓 Timeout aufgehoben"
        emoji = "🔓"
    else:
        color = discord.Color.orange()
        title = "🔇 Timeout"
        emoji = "🔇"

    embed = discord.Embed(title=title, color=color, timestamp=datetime.now(timezone.utc))
    embed.add_field(name="👤 Benutzer", value=f"{target.mention} (`{target}`)", inline=True)

    if moderator:
        embed.add_field(name="👮 Moderator", value=f"{moderator.mention} (`{moderator}`)", inline=True)
    else:
        embed.add_field(name="👮 Moderator", value="*Unbekannt / System*", inline=True)

    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if not removed and until:
        now_utc = datetime.now(timezone.utc)
        delta_sec = max(0, int((until - now_utc).total_seconds()))
        embed.add_field(
            name="⏰ Dauer",
            value=_format_duration(delta_sec),
            inline=True,
        )
        embed.add_field(
            name="📅 Bis",
            value=f"<t:{int(until.timestamp())}:F> (<t:{int(until.timestamp())}:R>)",
            inline=True,
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(
        name="📝 Grund",
        value=reason or "*Kein Grund angegeben*",
        inline=False,
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"User-ID: {target.id}")

    try:
        await log_ch.send(embed=embed)
    except discord.Forbidden:
        logger.warning(f"[moderation] Kein Zugriff auf Log-Kanal {log_ch_id}")
    except Exception as e:
        logger.error(f"[moderation] _send_log_embed: {e}")


async def _dm_user(
    target: discord.Member,
    guild: discord.Guild,
    reason: str | None,
    until: datetime | None,
    removed: bool = False,
):
    """Sendet dem betroffenen User eine DM."""
    try:
        if removed:
            embed = discord.Embed(
                title="🔓 Dein Timeout wurde aufgehoben",
                description=(
                    f"Dein Timeout auf **{guild.name}** wurde aufgehoben.\n"
                    "Du kannst wieder an Unterhaltungen teilnehmen."
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
        else:
            desc = f"Du wurdest auf **{guild.name}** stummgeschaltet (Timeout)."
            if until:
                delta_sec = max(0, int((until - datetime.now(timezone.utc)).total_seconds()))
                desc += (
                    f"\n\n**Dauer:** {_format_duration(delta_sec)}\n"
                    f"**Bis:** <t:{int(until.timestamp())}:F>"
                )
            embed = discord.Embed(
                title="🔇 Du wurdest in den Timeout versetzt",
                description=desc,
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )

        embed.add_field(
            name="📝 Grund",
            value=reason or "*Kein Grund angegeben*",
            inline=False,
        )
        embed.set_footer(text=f"Server: {guild.name}")
        await target.send(embed=embed)
    except discord.Forbidden:
        logger.debug(f"[moderation] DM an {target} nicht möglich (gesperrt)")
    except Exception as e:
        logger.warning(f"[moderation] DM fehlgeschlagen: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════

class ModerationSetupView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id   = str(guild_id)
        self.channel_id: str | None = None
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="⚙️ Moderations-System Setup",
            description=(
                "Konfiguriere den Log-Kanal für alle Moderationsaktionen.\n\n"
                "Folgende Ereignisse werden geloggt:\n"
                "🔇 Timeout gesetzt (Bot-Command oder natives Discord)\n"
                "🔓 Timeout aufgehoben\n"
                "📝 Grund und Moderator werden immer mitgeloggt."
            ),
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="📋 Log-Kanal",
            value=f"<#{self.channel_id}>" if self.channel_id else "*Nicht gesetzt*",
            inline=False,
        )
        return e

    def _rebuild(self):
        self.clear_items()

        sel = discord.ui.ChannelSelect(
            placeholder="📋 Moderations-Log-Kanal auswählen…",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        sel.callback = self._on_channel
        self.add_item(sel)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.success,
            disabled=self.channel_id is None,
            row=1,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    async def _on_channel(self, interaction: discord.Interaction):
        self.channel_id = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        _set_log_channel_id(self.guild_id, self.channel_id)
        embed = self._build_embed()
        embed.title = "✅ Moderations-Setup gespeichert!"
        embed.color = discord.Color.green()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


# ══════════════════════════════════════════════════════════════════════════════
# TIMEOUT – MODUS WAHL VIEW
# ══════════════════════════════════════════════════════════════════════════════

class TimeoutModeView(discord.ui.View):
    """Erste Auswahl: Gesamtdauer oder bis zu einem Zeitpunkt."""

    def __init__(self, target: discord.Member):
        super().__init__(timeout=120)
        self.target = target

    @discord.ui.button(label="⏱️ Gesamtdauer", style=discord.ButtonStyle.primary, row=0)
    async def btn_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutDurationModal(self.target))

    @discord.ui.button(label="📅 Bis zu einem Zeitpunkt", style=discord.ButtonStyle.secondary, row=0)
    async def btn_until(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutUntilModal(self.target))


# ══════════════════════════════════════════════════════════════════════════════
# TIMEOUT MODAL – DAUER
# ══════════════════════════════════════════════════════════════════════════════

class TimeoutDurationModal(discord.ui.Modal, title="Timeout – Gesamtdauer"):
    dauer = discord.ui.TextInput(
        label="Dauer",
        placeholder="z.B. 10m  /  2h  /  1d 12h  /  30s",
        required=True,
        max_length=50,
    )
    grund = discord.ui.TextInput(
        label="Grund",
        style=discord.TextStyle.paragraph,
        placeholder="Warum wird der User in den Timeout versetzt?",
        required=False,
        max_length=500,
    )

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    def _parse_duration(self, raw: str) -> timedelta | None:
        """
        Parst Dauern wie: 30s, 10m, 2h, 1d, 1d12h, 1d 12h 30m
        Gibt None zurück wenn ungültig.
        """
        import re
        raw = raw.strip().lower()
        pattern = re.compile(r'(\d+)\s*([smhd])')
        matches = pattern.findall(raw)
        if not matches:
            return None
        total = timedelta()
        for val, unit in matches:
            val = int(val)
            if unit == 's':
                total += timedelta(seconds=val)
            elif unit == 'm':
                total += timedelta(minutes=val)
            elif unit == 'h':
                total += timedelta(hours=val)
            elif unit == 'd':
                total += timedelta(days=val)
        if total.total_seconds() <= 0:
            return None
        # Discord-Limit: max 28 Tage
        if total.total_seconds() > 28 * 86400:
            total = timedelta(days=28)
        return total

    async def on_submit(self, interaction: discord.Interaction):
        delta = self._parse_duration(self.dauer.value)
        if delta is None:
            await interaction.response.send_message(
                "❌ Ungültiges Dauerformat.\n"
                "Beispiele: `30s`, `10m`, `2h`, `1d`, `1d 12h 30m`",
                ephemeral=True,
            )
            return

        until = datetime.now(timezone.utc) + delta
        reason = self.grund.value.strip() or None

        await _apply_timeout(interaction, self.target, until, reason)


# ══════════════════════════════════════════════════════════════════════════════
# TIMEOUT MODAL – BIS ZU EINEM ZEITPUNKT
# ══════════════════════════════════════════════════════════════════════════════

class TimeoutUntilModal(discord.ui.Modal, title="Timeout – bis zu einem Zeitpunkt"):
    zeitpunkt = discord.ui.TextInput(
        label="Zeitpunkt (Datum & Uhrzeit)",
        placeholder="z.B. 25.12.2025 20:00  oder  31.01. 08:30",
        required=True,
        max_length=30,
    )
    grund = discord.ui.TextInput(
        label="Grund",
        style=discord.TextStyle.paragraph,
        placeholder="Warum wird der User in den Timeout versetzt?",
        required=False,
        max_length=500,
    )

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    def _parse_until(self, raw: str) -> datetime | None:
        """Parst Datum+Uhrzeit, gibt aware UTC datetime zurück."""
        from datetime import datetime as dt
        raw = raw.strip()
        for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m. %H:%M", "%d.%m %H:%M"]:
            try:
                parsed = dt.strptime(raw, fmt)
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=dt.now().year)
                # Als Berliner Zeit interpretieren → UTC
                local = parsed.replace(tzinfo=TZ_BERLIN)
                return local.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    async def on_submit(self, interaction: discord.Interaction):
        until = self._parse_until(self.zeitpunkt.value)
        if until is None:
            await interaction.response.send_message(
                "❌ Ungültiges Datumsformat.\n"
                "Beispiele: `25.12.2025 20:00` oder `31.01. 08:30`",
                ephemeral=True,
            )
            return

        now = datetime.now(timezone.utc)
        if until <= now:
            await interaction.response.send_message(
                "❌ Der Zeitpunkt liegt in der Vergangenheit.",
                ephemeral=True,
            )
            return

        if (until - now).total_seconds() > 28 * 86400:
            await interaction.response.send_message(
                "❌ Discord erlaubt maximal **28 Tage** Timeout.",
                ephemeral=True,
            )
            return

        reason = self.grund.value.strip() or None
        await _apply_timeout(interaction, self.target, until, reason)


# ══════════════════════════════════════════════════════════════════════════════
# TIMEOUT ANWENDEN
# ══════════════════════════════════════════════════════════════════════════════

async def _apply_timeout(
    interaction: discord.Interaction,
    target: discord.Member,
    until: datetime,
    reason: str | None,
):
    """Setzt den Timeout, sendet DM, loggt die Aktion."""
    await interaction.response.defer(ephemeral=True)

    try:
        await target.timeout(until, reason=reason or "Kein Grund angegeben")
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Ich habe keine Berechtigung diesen User zu timeouten.\n"
            "Stelle sicher, dass meine Rolle über der des Users liegt.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Discord-Fehler: {e}", ephemeral=True)
        return

    guild = interaction.guild
    now   = datetime.now(timezone.utc)
    delta_sec = max(0, int((until - now).total_seconds()))

    # DM an User
    await _dm_user(target, guild, reason, until, removed=False)

    # Log-Embed
    await _send_log_embed(
        guild=guild,
        action="timeout",
        target=target,
        moderator=interaction.user,
        reason=reason,
        until=until,
        removed=False,
    )

    # DB-Log
    _log_action(
        server_id=str(guild.id),
        action="timeout",
        target=target,
        moderator=interaction.user,
        reason=reason,
        until=until,
    )

    # Bestätigung
    embed = discord.Embed(
        title="🔇 Timeout gesetzt",
        color=discord.Color.orange(),
        timestamp=now,
    )
    embed.add_field(name="👤 Benutzer", value=target.mention, inline=True)
    embed.add_field(name="⏰ Dauer",    value=_format_duration(delta_sec), inline=True)
    embed.add_field(
        name="📅 Bis",
        value=f"<t:{int(until.timestamp())}:F>",
        inline=False,
    )
    embed.add_field(name="📝 Grund", value=reason or "*Kein Grund angegeben*", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    moderation = app_commands.Group(
        name="moderation",
        description="Moderations-System",
    )

    # ── /moderation setup ─────────────────────────────────────────────────────

    @moderation.command(name="setup", description="Konfiguriere den Moderations-Log-Kanal")
    async def moderation_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = ModerationSetupView(interaction.guild_id)
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

    # ── /moderation timeout ───────────────────────────────────────────────────

    @moderation.command(name="timeout", description="Versetzt einen User in den Timeout")
    @app_commands.describe(mitglied="Der User der in den Timeout versetzt werden soll")
    async def moderation_timeout(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
    ):
        # Berechtigungen prüfen
        if not (
            interaction.user.guild_permissions.moderate_members
            or has_admin_rights(interaction)
        ):
            await interaction.response.send_message(
                "❌ Du benötigst die Berechtigung **Mitglieder moderieren**.",
                ephemeral=True,
            )
            return

        # Sich selbst oder den Bot timeouten verhindern
        if mitglied.id == interaction.user.id:
            await interaction.response.send_message(
                "❌ Du kannst dich nicht selbst in den Timeout versetzen.", ephemeral=True
            )
            return
        if mitglied.id == self.bot.user.id:
            await interaction.response.send_message(
                "❌ Den Bot kann ich nicht in den Timeout versetzen.", ephemeral=True
            )
            return

        # Rollenhierarchie prüfen
        if mitglied.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "❌ Du kannst keine User timeouten die dieselbe oder eine höhere Rolle haben.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="🔇 Timeout – Modus wählen",
            description=(
                f"Wie lange soll **{mitglied.display_name}** in den Timeout?\n\n"
                "**⏱️ Gesamtdauer** – z.B. `2h`, `1d 12h`, `30m`\n"
                "**📅 Bis zu einem Zeitpunkt** – z.B. `25.12.2025 20:00`"
            ),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=mitglied.display_avatar.url)
        await interaction.response.send_message(
            embed=embed,
            view=TimeoutModeView(mitglied),
            ephemeral=True,
        )

    # ── /moderation untimeout ─────────────────────────────────────────────────

    @moderation.command(name="untimeout", description="Hebt den Timeout eines Users auf")
    @app_commands.describe(
        mitglied="Der User dessen Timeout aufgehoben werden soll",
        grund="Grund für die Aufhebung (optional)",
    )
    async def moderation_untimeout(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        grund: str | None = None,
    ):
        if not (
            interaction.user.guild_permissions.moderate_members
            or has_admin_rights(interaction)
        ):
            await interaction.response.send_message(
                "❌ Du benötigst die Berechtigung **Mitglieder moderieren**.", ephemeral=True
            )
            return

        if not mitglied.is_timed_out():
            await interaction.response.send_message(
                f"ℹ️ {mitglied.mention} ist aktuell nicht im Timeout.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            await mitglied.timeout(None, reason=grund or "Timeout aufgehoben")
        except discord.Forbidden:
            await interaction.followup.send("❌ Keine Berechtigung.", ephemeral=True)
            return

        reason = grund.strip() if grund else None

        await _dm_user(mitglied, interaction.guild, reason, None, removed=True)
        await _send_log_embed(
            guild=interaction.guild,
            action="untimeout",
            target=mitglied,
            moderator=interaction.user,
            reason=reason,
            until=None,
            removed=True,
        )
        _log_action(
            server_id=str(interaction.guild_id),
            action="untimeout",
            target=mitglied,
            moderator=interaction.user,
            reason=reason,
            until=None,
        )

        embed = discord.Embed(
            title="🔓 Timeout aufgehoben",
            description=f"Der Timeout von {mitglied.mention} wurde aufgehoben.",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        if reason:
            embed.add_field(name="📝 Grund", value=reason, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── Audit-Log Listener: nativer Discord-Timeout ────────────────────────────

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """
        Erkennt native Discord-Timeouts (über Rechtsklick oder andere Bots)
        und loggt sie.
        """
        if before.communication_disabled_until == after.communication_disabled_until:
            return

        guild  = after.guild
        now    = datetime.now(timezone.utc)

        # Timeout wurde GESETZT
        if (
            after.communication_disabled_until is not None
            and (before.communication_disabled_until is None
                 or after.communication_disabled_until > now)
        ):
            until = after.communication_disabled_until
            moderator, reason = await _fetch_audit_info(guild, after, "timeout")

            await _send_log_embed(
                guild=guild,
                action="timeout",
                target=after,
                moderator=moderator,
                reason=reason,
                until=until,
                removed=False,
            )
            _log_action(
                server_id=str(guild.id),
                action="timeout_native",
                target=after,
                moderator=moderator,
                reason=reason,
                until=until,
            )
            # DM nur wenn kein Grund angegeben (Bot-Command schickt DM separat)
            if not reason or reason == "Kein Grund angegeben":
                await _dm_user(after, guild, reason, until, removed=False)

        # Timeout wurde AUFGEHOBEN
        elif (
            before.communication_disabled_until is not None
            and after.communication_disabled_until is None
        ):
            moderator, reason = await _fetch_audit_info(guild, after, "untimeout")
            await _send_log_embed(
                guild=guild,
                action="untimeout",
                target=after,
                moderator=moderator,
                reason=reason,
                until=None,
                removed=True,
            )
            _log_action(
                server_id=str(guild.id),
                action="untimeout_native",
                target=after,
                moderator=moderator,
                reason=reason,
                until=None,
            )


# ══════════════════════════════════════════════════════════════════════════════
# AUDIT LOG HELPER
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_audit_info(
    guild: discord.Guild,
    target: discord.Member,
    action_type: str,
) -> tuple[discord.Member | None, str | None]:
    """
    Versucht Moderator und Grund aus dem Audit-Log zu lesen.
    Gibt (moderator, reason) zurück.
    """
    try:
        discord_action = discord.AuditLogAction.member_update
        async for entry in guild.audit_logs(limit=10, action=discord_action):
            if entry.target and entry.target.id == target.id:
                # Einträge die höchstens 5 Sekunden alt sind berücksichtigen
                age = (datetime.now(timezone.utc) - entry.created_at).total_seconds()
                if age > 5:
                    continue
                moderator = guild.get_member(entry.user.id) if entry.user else None
                reason    = entry.reason or None
                return moderator, reason
    except (discord.Forbidden, discord.HTTPException):
        pass
    return None, None


async def setup(bot: commands.Bot):
    await bot.add_cog(ModerationCog(bot))