"""
bot/features/moderation/cog.py
================================
Moderations-System mit:
  - /moderation setup      – Log-Kanal für Moderationsaktionen festlegen
  - /moderation timeout    – User timeout (Dauer oder bis zu einem Zeitpunkt)
  - /moderation untimeout  – Timeout manuell aufheben
  - Automatisches Logging wenn Discord-Timeout (nativ) gesetzt/aufgehoben wird
  - Logging von nativen Kicks, Bans, Unbans, Voice-Kicks
  - Verwarnungen (Slash-Command + Kontextmenü) mit DM, Log und DB-Eintrag
  - /moderation logs       – Zeigt alle Moderationslogs an (mit Paginierung)

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
    CREATE INDEX IF NOT EXISTS idx_moderation_logs_target
        ON moderation_logs (server_id, target_id, created_at DESC);
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("moderation")

TZ_BERLIN = timezone(timedelta(hours=2))


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
    """Legacy-Funktion für Timeout-Logs (synchron)."""
    try:
        duration_sec = None
        if until:
            delta = until - datetime.now(timezone.utc)
            duration_sec = max(0, int(delta.total_seconds()))

        get_supabase().table("moderation_logs").insert({
            "server_id":        server_id,
            "action":           action,
            "target_id":        str(target.id),
            "target_name":      str(target),
            "moderator_id":     str(moderator.id) if moderator else None,
            "moderator_name":   str(moderator) if moderator else None,
            "reason":           reason,
            "duration_seconds": duration_sec,
            "until":            until.isoformat() if until else None,
            "created_at":       datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"[moderation] _log_action DB: {e}")


def _format_duration(seconds: int) -> str:
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
    """Legacy-Embed für Timeout."""
    log_ch_id = _get_log_channel_id(str(guild.id))
    if not log_ch_id:
        return
    log_ch = guild.get_channel(int(log_ch_id))
    if not log_ch:
        return

    if removed:
        color = discord.Color.green()
        title = "🔓 Timeout aufgehoben"
    else:
        color = discord.Color.orange()
        title = "🔇 Timeout"

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
        embed.add_field(name="⏰ Dauer", value=_format_duration(delta_sec), inline=True)
        embed.add_field(name="📅 Bis", value=f"<t:{int(until.timestamp())}:F> (<t:{int(until.timestamp())}:R>)", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="📝 Grund", value=reason or "*Kein Grund angegeben*", inline=False)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.set_footer(text=f"User-ID: {target.id}")

    try:
        await log_ch.send(embed=embed)
    except Exception as e:
        logger.error(f"[moderation] _send_log_embed: {e}")


async def _dm_user(
    target: discord.Member,
    guild: discord.Guild,
    reason: str | None,
    until: datetime | None,
    removed: bool = False,
):
    try:
        if removed:
            embed = discord.Embed(
                title="🔓 Dein Timeout wurde aufgehoben",
                description=f"Dein Timeout auf **{guild.name}** wurde aufgehoben.\nDu kannst wieder an Unterhaltungen teilnehmen.",
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
        elif until is None:
            embed = discord.Embed(
                title="⚠️ Verwarnung",
                description=f"Du wurdest auf **{guild.name}** verwarnt.",
                color=discord.Color.gold(),
                timestamp=datetime.now(timezone.utc),
            )
        else:
            desc = f"Du wurdest auf **{guild.name}** stummgeschaltet (Timeout)."
            if until:
                delta_sec = max(0, int((until - datetime.now(timezone.utc)).total_seconds()))
                desc += f"\n\n**Dauer:** {_format_duration(delta_sec)}\n**Bis:** <t:{int(until.timestamp())}:F>"
            embed = discord.Embed(
                title="🔇 Du wurdest in den Timeout versetzt",
                description=desc,
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )

        embed.add_field(name="📝 Grund", value=reason or "*Kein Grund angegeben*", inline=False)
        embed.set_footer(text=f"Server: {guild.name}")
        await target.send(embed=embed)
    except discord.Forbidden:
        logger.debug(f"[moderation] DM an {target} nicht möglich")
    except Exception as e:
        logger.warning(f"[moderation] DM fehlgeschlagen: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# ALLGEMEINE LOG-FUNKTIONEN (für alle Aktionen)
# ══════════════════════════════════════════════════════════════════════════════

async def _send_log_embed_general(
    guild: discord.Guild,
    action: str,
    target: discord.User | discord.Member,
    moderator: discord.Member | discord.User | None,
    reason: str | None,
    additional_fields: dict[str, str] | None = None,
) -> None:
    log_ch_id = _get_log_channel_id(str(guild.id))
    if not log_ch_id:
        logger.debug(f"[moderation] Kein Log-Kanal für {guild.id}")
        return
    log_ch = guild.get_channel(int(log_ch_id))
    if not log_ch:
        logger.debug(f"[moderation] Log-Kanal {log_ch_id} nicht gefunden")
        return

    meta = {
        "timeout":       ("🔇 Timeout", discord.Color.orange()),
        "untimeout":     ("🔓 Timeout aufgehoben", discord.Color.green()),
        "warn":          ("⚠️ Verwarnung", discord.Color.gold()),
        "kick":          ("👢 Kick", discord.Color.red()),
        "ban":           ("🔨 Ban", discord.Color.dark_red()),
        "unban":         ("🔓 Entbannung", discord.Color.green()),
        "voice_kick":    ("🎤 Voice-Kick", discord.Color.purple()),
    }
    title, color = meta.get(action, (f"📌 {action.capitalize()}", discord.Color.blurple()))
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())

    embed.add_field(name="👤 Benutzer", value=f"{target.mention} (`{target}`)", inline=True)
    if moderator:
        embed.add_field(name="👮 Moderator", value=f"{moderator.mention} (`{moderator}`)", inline=True)
    else:
        embed.add_field(name="👮 Moderator", value="*Unbekannt / System*", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    if additional_fields:
        for name, val in additional_fields.items():
            embed.add_field(name=name, value=val, inline=False)
    embed.add_field(name="📝 Grund", value=reason or "*Kein Grund angegeben*", inline=False)

    embed.set_thumbnail(url=getattr(target, "display_avatar", target.default_avatar).url)
    embed.set_footer(text=f"ID: {target.id}")

    try:
        await log_ch.send(embed=embed)
        logger.debug(f"[moderation] Log-Embed für '{action}' gesendet")
    except Exception as e:
        logger.error(f"[moderation] _send_log_embed_general: {e}")


async def _log_action_general(
    server_id: str,
    action: str,
    target_id: str,
    target_name: str,
    moderator_id: str | None,
    moderator_name: str | None,
    reason: str | None,
    duration_seconds: int | None = None,
    until: datetime | None = None,
) -> None:
    try:
        get_supabase().table("moderation_logs").insert({
            "server_id":        server_id,
            "action":           action,
            "target_id":        target_id,
            "target_name":      target_name,
            "moderator_id":     moderator_id,
            "moderator_name":   moderator_name,
            "reason":           reason,
            "duration_seconds": duration_seconds,
            "until":            until.isoformat() if until else None,
            "created_at":       datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.warning(f"[moderation] _log_action_general: {e}")


async def _fetch_audit_info_for(
    guild: discord.Guild,
    target_id: int,
    action_type: discord.AuditLogAction,
    *,
    max_age_seconds: float = 10.0,
) -> tuple[discord.Member | None, str | None]:
    """Robuste Audit-Log-Abfrage mit mehreren Versuchen."""
    try:
        for attempt in range(3):
            async for entry in guild.audit_logs(limit=20, action=action_type):
                if entry.target and entry.target.id == target_id:
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if age <= max_age_seconds:
                        moderator = guild.get_member(entry.user.id) if entry.user else None
                        logger.info(f"[AUDIT] Gefunden: {action_type}, Mod={moderator}, Reason={entry.reason}, Age={age:.1f}s")
                        return moderator, entry.reason
            if attempt < 2:
                await asyncio.sleep(1)
        logger.debug(f"[AUDIT] Kein Eintrag für {action_type} nach 3 Versuchen (ID {target_id})")
    except Exception as e:
        logger.error(f"[AUDIT] Fehler: {e}")
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# PAGINIERUNG FÜR LOGS
# ══════════════════════════════════════════════════════════════════════════════

class LogsPaginator(discord.ui.View):
    def __init__(self, entries: List[Dict[str, Any]], items_per_page: int = 5):
        super().__init__(timeout=60)
        self.entries = entries
        self.items_per_page = items_per_page
        self.current_page = 0
        self.total_pages = max(1, (len(entries) + items_per_page - 1) // items_per_page)

    def _get_embed(self) -> discord.Embed:
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_entries = self.entries[start:end]

        embed = discord.Embed(
            title="📜 Moderations-Logs",
            description=f"Seite {self.current_page + 1} von {self.total_pages}",
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow()
        )

        for log in page_entries:
            action_emoji = {
                "timeout": "🔇", "timeout_native": "🔇", "untimeout": "🔓", "untimeout_native": "🔓",
                "warn": "⚠️", "kick": "👢", "ban": "🔨", "unban": "🔓", "voice_kick": "🎤"
            }.get(log["action"], "📌")
            timestamp = log["created_at"]
            if timestamp:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                ts_str = f"<t:{int(dt.timestamp())}:d> <t:{int(dt.timestamp())}:t>"
            else:
                ts_str = "Unbekannt"

            target = log["target_name"] or log["target_id"]
            mod = log["moderator_name"] or log["moderator_id"] or "System"
            reason = log["reason"] or "Kein Grund"
            field_name = f"{action_emoji} {log['action'].replace('_native', '').capitalize()} – {ts_str}"
            field_value = f"**User:** {target}\n**Mod:** {mod}\n**Grund:** {reason}"
            if log.get("duration_seconds"):
                field_value += f"\n**Dauer:** {_format_duration(log['duration_seconds'])}"
            embed.add_field(name=field_name, value=field_value, inline=False)

        if not self.entries:
            embed.description = "Keine Logeinträge gefunden."
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, disabled=True)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self._get_embed(), view=self)

    def update_buttons(self):
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page >= self.total_pages - 1

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# ══════════════════════════════════════════════════════════════════════════════
# TIMEOUT MODALS & VIEWS
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
                "🔓 Timeout aufgehoben\n⚠️ Verwarnungen\n👢 Kicks\n🔨 Bans / Entbannungen\n🎤 Voice-Kicks\n"
                "📝 Grund und Moderator werden immer mitgeloggt."
            ),
            color=discord.Color.blurple(),
        )
        e.add_field(name="📋 Log-Kanal", value=f"<#{self.channel_id}>" if self.channel_id else "*Nicht gesetzt*", inline=False)
        return e

    def _rebuild(self):
        self.clear_items()
        sel = discord.ui.ChannelSelect(
            placeholder="📋 Moderations-Log-Kanal auswählen…",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text], row=0
        )
        sel.callback = self._on_channel
        self.add_item(sel)
        save_btn = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success, disabled=self.channel_id is None, row=1)
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


class TimeoutModeView(discord.ui.View):
    def __init__(self, target: discord.Member):
        super().__init__(timeout=120)
        self.target = target

    @discord.ui.button(label="⏱️ Gesamtdauer", style=discord.ButtonStyle.primary, row=0)
    async def btn_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutDurationModal(self.target))

    @discord.ui.button(label="📅 Bis zu einem Zeitpunkt", style=discord.ButtonStyle.secondary, row=0)
    async def btn_until(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimeoutUntilModal(self.target))


class TimeoutDurationModal(discord.ui.Modal, title="Timeout – Gesamtdauer"):
    dauer = discord.ui.TextInput(label="Dauer", placeholder="z.B. 10m / 2h / 1d 12h / 30s", required=True, max_length=50)
    grund = discord.ui.TextInput(label="Grund", style=discord.TextStyle.paragraph, placeholder="Warum wird der User in den Timeout versetzt?", required=False, max_length=500)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    def _parse_duration(self, raw: str) -> timedelta | None:
        import re
        raw = raw.strip().lower()
        matches = re.findall(r'(\d+)\s*([smhd])', raw)
        if not matches:
            return None
        total = timedelta()
        for val, unit in matches:
            val = int(val)
            if unit == 's': total += timedelta(seconds=val)
            elif unit == 'm': total += timedelta(minutes=val)
            elif unit == 'h': total += timedelta(hours=val)
            elif unit == 'd': total += timedelta(days=val)
        if total.total_seconds() <= 0 or total.total_seconds() > 28 * 86400:
            return None
        return total

    async def on_submit(self, interaction: discord.Interaction):
        delta = self._parse_duration(self.dauer.value)
        if delta is None:
            await interaction.response.send_message("❌ Ungültiges Dauerformat.\nBeispiele: `30s`, `10m`, `2h`, `1d`, `1d 12h 30m`", ephemeral=True)
            return
        until = datetime.now(timezone.utc) + delta
        reason = self.grund.value.strip() or None
        await _apply_timeout(interaction, self.target, until, reason)


class TimeoutUntilModal(discord.ui.Modal, title="Timeout – bis zu einem Zeitpunkt"):
    zeitpunkt = discord.ui.TextInput(label="Zeitpunkt (Datum & Uhrzeit)", placeholder="z.B. 25.12.2025 20:00  oder  31.01. 08:30", required=True, max_length=30)
    grund = discord.ui.TextInput(label="Grund", style=discord.TextStyle.paragraph, placeholder="Warum wird der User in den Timeout versetzt?", required=False, max_length=500)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    def _parse_until(self, raw: str) -> datetime | None:
        from datetime import datetime as dt
        raw = raw.strip()
        for fmt in ["%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m. %H:%M", "%d.%m %H:%M"]:
            try:
                parsed = dt.strptime(raw, fmt)
                if "%Y" not in fmt:
                    parsed = parsed.replace(year=dt.now().year)
                local = parsed.replace(tzinfo=TZ_BERLIN)
                return local.astimezone(timezone.utc)
            except ValueError:
                continue
        return None

    async def on_submit(self, interaction: discord.Interaction):
        until = self._parse_until(self.zeitpunkt.value)
        if until is None:
            await interaction.response.send_message("❌ Ungültiges Datumsformat.\nBeispiele: `25.12.2025 20:00` oder `31.01. 08:30`", ephemeral=True)
            return
        now = datetime.now(timezone.utc)
        if until <= now or (until - now).total_seconds() > 28 * 86400:
            await interaction.response.send_message("❌ Zeitpunkt liegt in der Vergangenheit oder überschreitet 28 Tage.", ephemeral=True)
            return
        reason = self.grund.value.strip() or None
        await _apply_timeout(interaction, self.target, until, reason)


async def _apply_timeout(interaction: discord.Interaction, target: discord.Member, until: datetime, reason: str | None):
    await interaction.response.defer(ephemeral=True)
    try:
        await target.timeout(until, reason=reason or "Kein Grund angegeben")
    except discord.Forbidden:
        await interaction.followup.send("❌ Keine Berechtigung. Meine Rolle muss höher sein als die des Users.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Discord-Fehler: {e}", ephemeral=True)
        return

    guild = interaction.guild
    now = datetime.now(timezone.utc)
    delta_sec = max(0, int((until - now).total_seconds()))

    await _dm_user(target, guild, reason, until, removed=False)
    await _send_log_embed(guild, "timeout", target, interaction.user, reason, until, removed=False)
    _log_action(str(guild.id), "timeout", target, interaction.user, reason, until)

    embed = discord.Embed(title="🔇 Timeout gesetzt", color=discord.Color.orange(), timestamp=now)
    embed.add_field(name="👤 Benutzer", value=target.mention, inline=True)
    embed.add_field(name="⏰ Dauer", value=_format_duration(delta_sec), inline=True)
    embed.add_field(name="📅 Bis", value=f"<t:{int(until.timestamp())}:F>", inline=False)
    embed.add_field(name="📝 Grund", value=reason or "*Kein Grund angegeben*", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# VERWARNUNGEN
# ══════════════════════════════════════════════════════════════════════════════

class WarnModal(discord.ui.Modal, title="Verwarnung aussprechen"):
    grund = discord.ui.TextInput(label="Grund", style=discord.TextStyle.paragraph, placeholder="Warum wird der User verwarnt?", required=False, max_length=500)

    def __init__(self, target: discord.Member):
        super().__init__()
        self.target = target

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.grund.value.strip() or None
        await _apply_warning(interaction, self.target, reason)


async def _apply_warning(interaction: discord.Interaction, target: discord.Member, reason: str | None):
    await interaction.response.defer(ephemeral=True)

    await _dm_user(target, interaction.guild, reason, None, removed=False)
    await _send_log_embed_general(interaction.guild, "warn", target, interaction.user, reason)
    await _log_action_general(
        server_id=str(interaction.guild.id),
        action="warn",
        target_id=str(target.id),
        target_name=str(target),
        moderator_id=str(interaction.user.id),
        moderator_name=str(interaction.user),
        reason=reason,
    )

    embed = discord.Embed(title="⚠️ Verwarnung ausgesprochen", description=f"{target.mention} wurde verwarnt.", color=discord.Color.gold())
    if reason:
        embed.add_field(name="Grund", value=reason, inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _list_warnings(interaction: discord.Interaction, member: discord.Member):
    try:
        resp = get_supabase().table("moderation_logs") \
            .select("reason, created_at, moderator_name") \
            .eq("server_id", str(interaction.guild.id)) \
            .eq("target_id", str(member.id)) \
            .eq("action", "warn") \
            .order("created_at", desc=True) \
            .execute()
        warnings = resp.data
    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler beim Abrufen: {e}", ephemeral=True)
        return

    if not warnings:
        await interaction.response.send_message(f"ℹ️ {member.mention} hat keine Verwarnungen.", ephemeral=True)
        return

    embed = discord.Embed(title=f"⚠️ Verwarnungen für {member.display_name}", color=discord.Color.gold(), timestamp=discord.utils.utcnow())
    for i, w in enumerate(warnings[:10], start=1):
        ts = w["created_at"]
        if ts:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
            ts_str = f"<t:{int(dt.timestamp())}:d>"
        else:
            ts_str = "Unbekannt"
        reason = w["reason"] or "Kein Grund"
        mod = w["moderator_name"] or "Unbekannt"
        embed.add_field(name=f"{i}. {ts_str}", value=f"**Mod:** {mod}\n**Grund:** {reason}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# LOGS ANZEIGEN
# ══════════════════════════════════════════════════════════════════════════════

async def _fetch_moderation_logs(guild_id: str, target_id: str | None = None, limit: int = 20) -> List[Dict[str, Any]]:
    try:
        query = get_supabase().table("moderation_logs") \
            .select("*") \
            .eq("server_id", guild_id) \
            .order("created_at", desc=True) \
            .limit(limit)
        if target_id:
            query = query.eq("target_id", target_id)
        resp = query.execute()
        return resp.data
    except Exception as e:
        logger.error(f"[moderation] _fetch_moderation_logs: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    moderation = app_commands.Group(name="moderation", description="Moderations-System")

    @moderation.command(name="setup", description="Konfiguriere den Moderations-Log-Kanal")
    async def moderation_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = ModerationSetupView(interaction.guild_id)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @moderation.command(name="timeout", description="Versetzt einen User in den Timeout")
    @app_commands.describe(mitglied="Der User der in den Timeout versetzt werden soll")
    async def moderation_timeout(self, interaction: discord.Interaction, mitglied: discord.Member):
        if not (interaction.user.guild_permissions.moderate_members or has_admin_rights(interaction)):
            await interaction.response.send_message("❌ Du benötigst die Berechtigung **Mitglieder moderieren**.", ephemeral=True)
            return
        if mitglied.id == interaction.user.id or mitglied.id == self.bot.user.id:
            await interaction.response.send_message("❌ Du kannst dich selbst oder den Bot nicht timeouten.", ephemeral=True)
            return
        if mitglied.top_role >= interaction.user.top_role and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Du kannst keine User mit gleicher oder höherer Rolle timeouten.", ephemeral=True)
            return

        embed = discord.Embed(title="🔇 Timeout – Modus wählen", description=f"Wie lange soll **{mitglied.display_name}** in den Timeout?\n\n**⏱️ Gesamtdauer** – z.B. `2h`, `1d 12h`, `30m`\n**📅 Bis zu einem Zeitpunkt** – z.B. `25.12.2025 20:00`", color=discord.Color.orange())
        embed.set_thumbnail(url=mitglied.display_avatar.url)
        await interaction.response.send_message(embed=embed, view=TimeoutModeView(mitglied), ephemeral=True)

    @moderation.command(name="untimeout", description="Hebt den Timeout eines Users auf")
    @app_commands.describe(mitglied="Der User dessen Timeout aufgehoben werden soll", grund="Grund für die Aufhebung (optional)")
    async def moderation_untimeout(self, interaction: discord.Interaction, mitglied: discord.Member, grund: str | None = None):
        if not (interaction.user.guild_permissions.moderate_members or has_admin_rights(interaction)):
            await interaction.response.send_message("❌ Du benötigst die Berechtigung **Mitglieder moderieren**.", ephemeral=True)
            return
        if not mitglied.is_timed_out():
            await interaction.response.send_message(f"ℹ️ {mitglied.mention} ist aktuell nicht im Timeout.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await mitglied.timeout(None, reason=grund or "Timeout aufgehoben")
        except discord.Forbidden:
            await interaction.followup.send("❌ Keine Berechtigung.", ephemeral=True)
            return

        reason = grund.strip() if grund else None
        await _dm_user(mitglied, interaction.guild, reason, None, removed=True)
        await _send_log_embed(interaction.guild, "untimeout", mitglied, interaction.user, reason, None, removed=True)
        _log_action(str(interaction.guild_id), "untimeout", mitglied, interaction.user, reason, None)

        embed = discord.Embed(title="🔓 Timeout aufgehoben", description=f"Der Timeout von {mitglied.mention} wurde aufgehoben.", color=discord.Color.green(), timestamp=datetime.now(timezone.utc))
        if reason:
            embed.add_field(name="📝 Grund", value=reason, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @moderation.command(name="warn", description="Verwarnt einen User (Log + DM)")
    @app_commands.describe(mitglied="Der User, der verwarnt werden soll")
    async def moderation_warn(self, interaction: discord.Interaction, mitglied: discord.Member):
        if not has_admin_rights(interaction) and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.send_modal(WarnModal(mitglied))

    @moderation.command(name="warnings", description="Zeigt alle Verwarnungen eines Users")
    @app_commands.describe(mitglied="Der User, dessen Verwarnungen angezeigt werden sollen")
    async def moderation_warnings(self, interaction: discord.Interaction, mitglied: discord.Member):
        if not has_admin_rights(interaction) and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await _list_warnings(interaction, mitglied)

    @moderation.command(name="logs", description="Zeigt die letzten Moderations-Logs an")
    @app_commands.describe(
        user="Optional: Zeige nur Logs für einen bestimmten User",
        limit="Anzahl der Logs (max. 50, Standard 20)"
    )
    async def moderation_logs(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
        limit: int = 20
    ):
        if not has_admin_rights(interaction) and not interaction.user.guild_permissions.kick_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if limit < 1:
            limit = 1
        if limit > 50:
            limit = 50

        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild.id)
        target_id = str(user.id) if user else None
        logs = await _fetch_moderation_logs(guild_id, target_id, limit)

        if not logs:
            await interaction.followup.send("📭 Keine Moderations-Logs gefunden.", ephemeral=True)
            return

        paginator = LogsPaginator(logs, items_per_page=5)
        await interaction.followup.send(embed=paginator._get_embed(), view=paginator, ephemeral=True)

    # ═══════════════════════════════════════════════════════════════════════════
    # VERBESSERTE LISTENER FÜR KICKS UND VOICE-KICKS
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Erkennt Kicks (keine Bans) und loggt sie."""
        guild = member.guild
        logger.info(f"[KICK] on_member_remove: {member} (ID {member.id}) verlässt {guild.name}")

        # Längere Wartezeit, damit das Audit‑Log den Eintrag erhält
        await asyncio.sleep(3)

        # Prüfen, ob es ein Ban war – dann wird on_member_ban feuern, also hier ignorieren
        try:
            ban_entry = await guild.fetch_ban(member)
            if ban_entry:
                logger.info(f"[KICK] {member} wurde gebannt – kein Kick-Log")
                return
        except discord.NotFound:
            pass
        except discord.Forbidden:
            logger.warning(f"[KICK] Keine Berechtigung, Bans zu prüfen in {guild.name}")
        except Exception as e:
            logger.error(f"[KICK] Fehler bei fetch_ban: {e}")

        # Audit‑Log nach Kick durchsuchen
        try:
            async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == member.id:  # Sicherstellen, dass target existiert
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if age <= 5.0:  # Nur aktuelle Einträge
                        moderator = guild.get_member(entry.user.id) if entry.user else None
                        reason = entry.reason
                        logger.info(f"[KICK] Kick erkannt: {member} von {moderator} (Grund: {reason})")
                        await _send_log_embed_general(guild, "kick", member, moderator, reason)
                        await _log_action_general(
                            server_id=str(guild.id),
                            action="kick",
                            target_id=str(member.id),
                            target_name=str(member),
                            moderator_id=str(moderator.id) if moderator else None,
                            moderator_name=str(moderator) if moderator else None,
                            reason=reason,
                        )
                        return
        except discord.Forbidden:
            logger.warning(f"[KICK] Keine Audit‑Log‑Rechte in {guild.name} – kann Kicks nicht erkennen")
        except Exception as e:
            logger.error(f"[KICK] Fehler beim Lesen des Audit‑Logs: {e}")

        logger.info(f"[KICK] Kein Kick-Eintrag für {member} – vermutlich freiwilliger Austritt")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        """Erkennt Voice‑Kicks (Moderator trennt Verbindung)."""
        # Nur wenn der Benutzer einen Sprachkanal verlassen hat
        if before.channel is not None and after.channel is None:
            guild = member.guild
            logger.info(f"[VOICE] {member} hat {before.channel.name} verlassen – prüfe auf Kick")

            # Wartezeit für Audit‑Log
            await asyncio.sleep(2)

            try:
                async for entry in guild.audit_logs(limit=10, action=discord.AuditLogAction.member_disconnect):
                    if entry.target is None or entry.target.id != member.id:
                        continue
                    age = (discord.utils.utcnow() - entry.created_at).total_seconds()
                    if age < 10:  # nur frische Einträge
                        moderator = guild.get_member(entry.user.id) if entry.user else None

                        # Eigene Bot‑Aktionen ignorieren
                        if moderator == guild.me:
                            logger.info(f"[VOICE] Voice-Kick durch Bot selbst – ignoriert")
                            return

                        reason = entry.reason
                        logger.info(f"[VOICE] Voice-Kick erkannt: {member} von {moderator} (Grund: {reason})")
                        await _send_log_embed_general(guild, "voice_kick", member, moderator, reason)
                        await _log_action_general(
                            server_id=str(guild.id),
                            action="voice_kick",
                            target_id=str(member.id),
                            target_name=str(member),
                            moderator_id=str(moderator.id) if moderator else None,
                            moderator_name=str(moderator) if moderator else None,
                            reason=reason,
                        )
                        return
            except discord.Forbidden:
                logger.warning(f"[VOICE] Keine Audit‑Log‑Rechte in {guild.name}")
            except Exception as e:
                logger.error(f"[VOICE] Fehler: {e}")

            logger.info(f"[VOICE] Kein Voice-Kick-Eintrag für {member} – freiwillige Trennung")

    # ═══════════════════════════════════════════════════════════════════════════
    # WEITERE LISTENER (Timeout, Ban, Unban)
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.timed_out_until == after.timed_out_until:
            return
        guild = after.guild
        now = datetime.now(timezone.utc)

        if after.timed_out_until is not None and (before.timed_out_until is None or after.timed_out_until > now):
            until = after.timed_out_until
            moderator, reason = await _fetch_audit_info_for(guild, after.id, discord.AuditLogAction.member_update, max_age_seconds=5.0)
            if moderator and moderator == guild.me:
                return
            await _send_log_embed_general(guild, "timeout", after, moderator, reason, additional_fields={"⏰ Bis": f"<t:{int(until.timestamp())}:F>"})
            await _log_action_general(str(guild.id), "timeout_native", str(after.id), str(after), str(moderator.id) if moderator else None, str(moderator) if moderator else None, reason, until=until)
            await _dm_user(after, guild, reason, until, removed=False)

        elif before.timed_out_until is not None and after.timed_out_until is None:
            moderator, reason = await _fetch_audit_info_for(guild, after.id, discord.AuditLogAction.member_update, max_age_seconds=5.0)
            if moderator and moderator == guild.me:
                return
            await _send_log_embed_general(guild, "untimeout", after, moderator, reason)
            await _log_action_general(str(guild.id), "untimeout_native", str(after.id), str(after), str(moderator.id) if moderator else None, str(moderator) if moderator else None, reason)
            await _dm_user(after, guild, reason, None, removed=True)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        moderator, reason = await _fetch_audit_info_for(guild, user.id, discord.AuditLogAction.ban, max_age_seconds=3.0)
        if moderator and moderator == guild.me:
            return
        await _send_log_embed_general(guild, "ban", user, moderator, reason)
        await _log_action_general(str(guild.id), "ban", str(user.id), str(user), str(moderator.id) if moderator else None, str(moderator) if moderator else None, reason)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        moderator, reason = await _fetch_audit_info_for(guild, user.id, discord.AuditLogAction.unban, max_age_seconds=3.0)
        if moderator and moderator == guild.me:
            return
        await _send_log_embed_general(guild, "unban", user, moderator, reason)
        await _log_action_general(str(guild.id), "unban", str(user.id), str(user), str(moderator.id) if moderator else None, str(moderator) if moderator else None, reason)


# ══════════════════════════════════════════════════════════════════════════════
# KONTEXTMENÜ
# ══════════════════════════════════════════════════════════════════════════════

@app_commands.context_menu(name="Verwarnen")
async def context_warn(interaction: discord.Interaction, member: discord.Member):
    if not has_admin_rights(interaction) and not interaction.user.guild_permissions.kick_members:
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    await interaction.response.send_modal(WarnModal(member))


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════

async def setup(bot: commands.Bot):
    cog = ModerationCog(bot)
    await bot.add_cog(cog)
    bot.tree.add_command(context_warn)