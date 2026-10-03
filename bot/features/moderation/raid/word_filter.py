"""
bot/features/moderation/raid/word_filter.py
===========================================
Wortfilter fuer das Raid-Schutz-/Moderationssystem.

Verhalten
---------
Schreibt jemand ein Wort (oder eine Zahl) von der Liste, wird die Nachricht
geloescht und der User standardmaessig **gebannt**.  Pro Eintrag laesst sich
eine andere Aktion waehlen (loeschen, verwarnen, Timeout, Kick, Bann).

Der Filter ist bewusst "fortgeschritten":

  * **Zahlen bleiben Zahlen.**  Ist "123" verboten, trifft "123" - aber nicht
    "1234", "4123" oder "abc123".  Gesucht wird immer der komplette
    Ziffernblock.
  * **Umgehungen werden erkannt:** Gross-/Kleinschreibung, NFKC-Varianten
    (Vollbreite), Leetspeak ("b4d"), Homoglyphe (kyrillisches "а"),
    Zeichenwiederholungen ("baaad"), eingestreute Trenner ("b.a.d").
  * **Vier Trefferarten:** ganzes Wort, ganzes Token, Teilstring, Regex.
  * **Whitelist** gegen Fehlalarme, Kanal- und Rollen-Ausnahmen.
  * **Kontext:** Treffer in URLs und in Code/Backticks werden ignoriert
    (pro Eintrag abschaltbar).
  * **Trockenlauf:** Mit `dry_run` wird nur geloggt, nichts geloescht/gebannt.

Bereitstellung
--------------
1. `sql/word_filter.sql` in Supabase ausfuehren.
2. Cog wird ueber `bot.features.moderation.raid_protection` mitgeladen.
3. `/wordfilter setup` -> Log-Kanal + Default-Aktion waehlen, aktivieren,
   danach `/wordfilter add` fuer die Liste (oder `/wordfilter import`).

Sticker-Bug-Fix
---------------
Discord stellt Custom-Emojis und Mentions als Markup dar, das die Snowflake-ID
enthaelt (z. B. `<:67:1481966896105394237>`).  Eine verbotene Zahl wie "67"
konnte dadurch faelschlich Treffer ausloesen.  Mit `FIX_STICKER_BUG = True`
werden Sticker, Custom-Emojis und Mentions vor der Pruefung entfernt.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.logger import get_logger

from . import word_store
from .matcher import (
    WordMatch,
    build_matcher,
    describe_characters,
    reduce_text,
    validate_pattern,
)
from .permissions import is_moderator
from .word_store import (
    ACTION_LABELS,
    ACTIONS,
    DEFAULT_CONFIG,
    MATCH_TYPE_LABELS,
    MATCH_TYPES,
    WordEntry,
    parse_id_list,
)

logger = get_logger("raid.wordfilter_cog")

# Auswahllisten fuer die Slash-Commands.
MATCH_TYPE_CHOICES = [
    app_commands.Choice(name=MATCH_TYPE_LABELS[value], value=value)
    for value in MATCH_TYPES
]
ACTION_CHOICES = [
    app_commands.Choice(name=ACTION_LABELS[value], value=value) for value in ACTIONS
]

CACHE_TTL_SECONDS = 60
# Schneidet Discord-Reasons auf das erlaubte Mass.
MAX_REASON_LENGTH = 400

# ── Sticker-Bug-Fix ─────────────────────────────────────────────────────────
# Wenn True, werden Discord-Sticker, Custom-Emojis und Mentions vor der
# Filterpruefung aus dem Nachrichteninhalt entfernt.  Damit kann eine
# verbotene Zahl wie "67" nicht mehr ueber einen Sticker
# `<:67:1481966896105394237>` oder eine Mention ausgeloest werden.
#
# Auf False setzen, um das alte Verhalten (Content wird roh geprueft)
# wiederherzustellen.
FIX_STICKER_BUG = False

# Discord-Custom-Emoji / Sticker:  <a:name:id>  oder  <:name:id>
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]+:\d+>")
# User-/Rollen-/Kanal-Mentions:  <@123>  <@!123>  <@&123>  <#123>
_MENTION_RE = re.compile(r"<[@#][!&]?\d+>")


# ── Cache ───────────────────────────────────────────────────────────────────

class WordFilterState:
    """Config + Matcher eines Servers, inklusive Cache-Zeitstempel."""

    def __init__(self, config: Dict[str, Any], matcher, entries: List[WordEntry]):
        self.config = config
        self.matcher = matcher
        self.entries = entries
        self.loaded_at = datetime.now(timezone.utc).timestamp()

    def is_fresh(self) -> bool:
        return (
            datetime.now(timezone.utc).timestamp() - self.loaded_at
        ) < CACHE_TTL_SECONDS

    def exempt_channels(self) -> List[str]:
        return parse_id_list(self.config.get("exempt_channel_ids"))

    def exempt_roles(self) -> List[str]:
        return parse_id_list(self.config.get("exempt_role_ids"))


def _load_state(server_id: str) -> WordFilterState:
    """Synchroner Loader - wird per `asyncio.to_thread` aufgerufen."""
    config, entries, allow = word_store.load_server_state(server_id)
    matcher = build_matcher(entries, allow)
    return WordFilterState(config, matcher, entries)


# ── Treffer-Auswertung ──────────────────────────────────────────────────────

def _effective_action(config: Dict[str, Any], entry: WordEntry) -> str:
    action = entry.action_for(str(config.get("default_action") or "timeout"))
    return action if action in ACTIONS else "timeout"


def _shorten(value: str, limit: int = MAX_REASON_LENGTH) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _render_match(match: WordMatch) -> str:
    return (
        f"Eintrag `{discord.utils.escape_markdown(match.pattern)}` "
        f"({match.method}, {int(match.confidence * 100)} %): "
        f"`{discord.utils.escape_markdown(match.matched_text)}`"
    )


# ── Cog ─────────────────────────────────────────────────────────────────────

class WordFilterCog(commands.Cog):
    """Wortfilter als Teil des Raid-Schutz-Systems."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cache: Dict[str, WordFilterState] = {}
        # user_id -> timestamp: verhindert Mehrfach-Banns in kurzer Folge.
        self._cooldowns: Dict[int, float] = {}

    # ------------------------------------------------------------------
    # Cache-Helfer
    # ------------------------------------------------------------------
    async def get_state(self, guild_id: int, force: bool = False) -> Optional[WordFilterState]:
        server_id = str(guild_id)
        cached = self._cache.get(server_id)
        if cached and cached.is_fresh() and not force:
            return cached
        try:
            state = await asyncio.to_thread(_load_state, server_id)
        except Exception as error:
            logger.warning(f"[wordfilter] Laden fehlgeschlagen fuer {server_id}: {error}")
            return cached
        self._cache[server_id] = state
        return state

    def invalidate(self, guild_id: int) -> None:
        self._cache.pop(str(guild_id), None)

    async def is_moderator(self, user: discord.abc.User, guild: discord.Guild) -> bool:
        raid_cog = self.bot.get_cog("RaidProtectionCog")
        if raid_cog is not None:
            return await raid_cog.is_moderator(user, guild)
        return await is_moderator(user, guild, _MinimalDb())

    def _cooldown_active(self, user_id: int) -> bool:
        stamp = self._cooldowns.get(user_id)
        if not stamp:
            return False
        return (datetime.now(timezone.utc).timestamp() - stamp) < 10

    def _mark_cooldown(self, user_id: int) -> None:
        self._cooldowns[user_id] = datetime.now(timezone.utc).timestamp()
        if len(self._cooldowns) > 5000:
            cutoff = datetime.now(timezone.utc).timestamp() - 60
            self._cooldowns = {
                key: value for key, value in self._cooldowns.items() if value > cutoff
            }

    # ------------------------------------------------------------------
    # Sticker-Bug-Fix
    # ------------------------------------------------------------------
    def _sanitize_content(self, message: discord.Message) -> str:
        """
        Entfernt Discord-Markup (Sticker, Custom-Emojis, Mentions), damit
        IDs und Namen nicht versehentlich den Wortfilter ausloesen.

        Wird nur ausgefuehrt, wenn FIX_STICKER_BUG aktiv ist.  Bei False
        wird der rohe Content zurueckgegeben (altes Verhalten).
        """
        if not FIX_STICKER_BUG:
            return message.content or ""
        content = message.content or ""
        content = _CUSTOM_EMOJI_RE.sub(" ", content)
        content = _MENTION_RE.sub(" ", content)
        return content

    # ------------------------------------------------------------------
    # Berechtigungspruefung fuer Commands
    # ------------------------------------------------------------------
    async def _require_moderator(self, interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "Nur auf Servern nutzbar.", ephemeral=True
            )
            return False
        if not await self.is_moderator(interaction.user, interaction.guild):
            await interaction.response.send_message(
                "Keine Berechtigung. (Admin / Moderator-Rolle / MBL)", ephemeral=True
            )
            return False
        return True

    # ==================================================================
    # on_message
    # ==================================================================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        logger.debug(f"[wordfilter] {message.content}")
        try:
            await self._handle_message(message)
        except Exception as error:  # pragma: no cover - Sicherheitsnetz
            logger.error(f"[wordfilter] Unerwarteter Fehler: {error}", exc_info=True)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content == after.content:
            return
        try:
            state = self._cache.get(str(after.guild.id)) if after.guild else None
            if state and not state.config.get("check_edits", True):
                return
            await self._handle_message(after, is_edit=True)
        except Exception as error:  # pragma: no cover
            logger.error(f"[wordfilter] Fehler bei Edit-Pruefung: {error}", exc_info=True)

    async def _handle_message(self, message: discord.Message, is_edit: bool = False) -> None:
        if not message.guild or message.author.bot:
            return
        if not isinstance(message.author, discord.Member):
            return
        if message.author.is_timed_out():
            return

        state = self._cache.get(str(message.guild.id))
        if state is None or not state.is_fresh():
            state = await self.get_state(message.guild.id)
        if state is None or not state.config.get("enabled", False):
            return
        if state.matcher.entry_count == 0:
            return

        config = state.config

        # Sticker-Bug-Fix: eine Nachricht, die NUR aus Stickern besteht,
        # hat keinen pruefbaren Text.  Ohne diesen Check wuerde der leere
        # Content durch die min_message_length-Pruefung fallen, aber wir
        # wollen hier explizit frueh raus.
        if FIX_STICKER_BUG and not (message.content or "").strip() and message.stickers:
            return

        content = self._sanitize_content(message)
        if len(content.strip()) < int(config.get("min_message_length") or 1):
            return
        if str(message.channel.id) in state.exempt_channels():
            return
        if any(str(role.id) in state.exempt_roles() for role in message.author.roles):
            return

        match = state.matcher.check(content)
        if match is None:
            return

        action = _effective_action(config, match.entry)
        dry_run = bool(config.get("dry_run", False))
        logger.info(
            "[wordfilter] Treffer in guild=%s channel=%s user=%s muster=%r "
            "methode=%s aktion=%s dry_run=%s%s",
            message.guild.id,
            message.channel.id,
            message.author.id,
            match.pattern,
            match.method,
            action,
            dry_run,
            " (edit)" if is_edit else "",
        )

        await asyncio.to_thread(
            word_store.register_hit, str(message.guild.id), match.pattern
        )

        if dry_run:
            await self._report(
                message, match, action, dry_run=True, executed=False
            )
            return

        if self._cooldown_active(message.author.id):
            # Der User wird bereits sanktioniert; nur noch loeschen und loggen.
            await self._delete_message(message, config)
            await self._report(message, match, action, dry_run=False, executed=False)
            return
        self._mark_cooldown(message.author.id)

        executed, error = await self._execute_action(message, match, action, config)
        await self._delete_message(message, config)
        await self._report(
            message, match, action, dry_run=False, executed=executed, error=error
        )

    # ------------------------------------------------------------------
    # Aktionen
    # ------------------------------------------------------------------
    async def _execute_action(
        self,
        message: discord.Message,
        match: WordMatch,
        action: str,
        config: Dict[str, Any],
    ) -> Tuple[bool, Optional[str]]:
        member = message.author
        reason = _shorten(
            f"Wortfilter: `{match.pattern}` erkannt ({match.reason})"
        )
        try:
            if action == "delete" or action == "warn":
                if action == "warn" and config.get("notify_user", True):
                    await self._dm(
                        member,
                        "Verwarnung - verbotenes Wort",
                        (
                            f"Deine Nachricht enthielt `{match.pattern}` und wurde "
                            "gelöscht. Dies ist eine Verwarnung."
                        ),
                        discord.Color.gold(),
                        match,
                    )
                return True, None

            if action == "timeout":
                minutes = int(config.get("timeout_minutes") or 10080)
                minutes = max(1, min(minutes, 40320))  # Discord-Limit: 28 Tage
                until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
                await self._log(
                    message.guild, member, "wordfilter_timeout", reason, until
                )
                if config.get("notify_user", True):
                    await self._dm(
                        member,
                        "Timeout - verbotenes Wort",
                        (
                            f"Deine Nachricht enthielt `{match.pattern}` und wurde "
                            f"gelöscht. Du wurdest für {minutes} Minuten in den "
                            "Timeout versetzt."
                        ),
                        discord.Color.orange(),
                        match,
                    )
                return True, None

            # Unbekannte/entfernte Aktion -> nur löschen, keine Sanktion
            return True, None

        except discord.Forbidden:
            note = f"Fehlende Rechte für Aktion `{action}`."
            logger.warning(f"[wordfilter] {note} guild={message.guild.id}")
            return False, note
        except discord.HTTPException as error:
            note = f"Discord-Fehler bei `{action}`: {error}"
            logger.warning(f"[wordfilter] {note}")
            return False, note

    async def _delete_message(
        self, message: discord.Message, config: Dict[str, Any]
    ) -> bool:
        if not config.get("delete_message", True):
            return False
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return False
        except discord.Forbidden:
            logger.warning(
                f"[wordfilter] Keine Rechte zum Löschen in Kanal {message.channel.id}"
            )
            return False
        except discord.HTTPException as error:
            logger.warning(f"[wordfilter] Löschen fehlgeschlagen: {error}")
            return False

    async def _dm(
        self,
        member: discord.Member,
        title: str,
        description: str,
        color: discord.Color,
        match: WordMatch,
    ) -> None:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Server", value=discord.utils.escape_markdown(member.guild.name), inline=True
        )
        embed.add_field(
            name="Erkannter Eintrag",
            value=f"`{discord.utils.escape_markdown(match.pattern)}`",
            inline=True,
        )
        try:
            await member.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            logger.debug(f"[wordfilter] DM an {member.id} nicht möglich")

    async def _log(
        self,
        guild: discord.Guild,
        member: discord.Member,
        action: str,
        reason: str,
        until: Optional[datetime],
    ) -> None:
        await asyncio.to_thread(
            word_store.log_action,
            str(guild.id),
            action,
            str(member.id),
            str(member),
            str(self.bot.user.id) if self.bot.user else None,
            str(self.bot.user) if self.bot.user else "System",
            reason,
            until,
        )

    async def _report(
        self,
        message: discord.Message,
        match: WordMatch,
        action: str,
        dry_run: bool,
        executed: bool,
        error: Optional[str] = None,
    ) -> None:
        state = self._cache.get(str(message.guild.id))
        config = state.config if state else {}
        channel_id = config.get("log_channel_id")
        channel = None
        if channel_id:
            try:
                channel = message.guild.get_channel(int(channel_id))
            except (TypeError, ValueError):
                channel = None
        if channel is None:
            raid_cog = self.bot.get_cog("RaidProtectionCog")
            if raid_cog is not None:
                try:
                    channel = await raid_cog.db.get_log_channel(message.guild)
                except Exception as error_obj:
                    logger.debug(f"[wordfilter] Log-Kanal-Fallback fehlgeschlagen: {error_obj}")
        if channel is None:
            logger.warning(
                f"[wordfilter] Kein Log-Kanal für Server {message.guild.id} konfiguriert"
            )
            return

        title = "🧪 Wortfilter (Trockenlauf)" if dry_run else "🚫 Wortfilter"
        color = discord.Color.gold() if dry_run else discord.Color.dark_red()
        embed = discord.Embed(
            title=title,
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{message.author.mention}\n`{message.author.id}`", inline=True)
        embed.add_field(name="Kanal", value=message.channel.mention, inline=True)
        embed.add_field(
            name="Aktion",
            value=ACTION_LABELS.get(action, action) if not dry_run else "nur protokolliert",
            inline=True,
        )
        embed.add_field(
            name="Eintrag",
            value=(
                f"`{discord.utils.escape_markdown(match.pattern)}`\n"
                f"Trefferart: {MATCH_TYPE_LABELS.get(match.entry.effective_match_type, match.entry.effective_match_type)}\n"
                f"Erkennung: {match.reason} ({int(match.confidence * 100)} %)"
            ),
            inline=False,
        )
        # Auch hier die bereinigte Sicht verwenden, damit im Log-Embed nicht
        # wieder die Sticker-/Mention-IDs auftauchen.
        snippet = self._sanitize_content(message).strip()
        if len(snippet) > 900:
            snippet = snippet[:900] + "..."
        if snippet:
            embed.add_field(
                name="Nachricht",
                value=f"```\n{discord.utils.escape_markdown(snippet)}\n```",
                inline=False,
            )
        if match.entry.note:
            embed.add_field(name="Notiz", value=str(match.entry.note)[:1000], inline=False)
        if error:
            embed.add_field(name="⚠️ Hinweis", value=error[:1000], inline=False)
        if not dry_run and not executed:
            embed.add_field(
                name="ℹ️ Status",
                value="Nachricht gelöscht, aber keine Sanktion ausgeführt (siehe Hinweis).",
                inline=False,
            )
        embed.set_footer(text=f"User-ID: {message.author.id}")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as send_error:
            logger.warning(f"[wordfilter] Report konnte nicht gesendet werden: {send_error}")

    # ==================================================================
    # Slash-Commands: /wordfilter
    #
    # Eigene Top-Level-Gruppe: discord.py kennt pro Name nur eine
    # Command-Gruppe, und /raid gehoert bereits dem RaidProtectionCog und
    # kann von einem zweiten Cog nicht erweitert werden.
    # ==================================================================
    wordfilter = app_commands.Group(
        name="wordfilter",
        description="Wortfilter: verbotene Wörter/Zahlen",
    )

    # ---------- Setup ----------
    @wordfilter.command(name="setup", description="[Mod] Wortfilter konfigurieren")
    async def wordfilter_setup(self, interaction: discord.Interaction):
        if not await self._require_moderator(interaction):
            return
        state = await self.get_state(interaction.guild_id, force=True)
        config = state.config if state else dict(DEFAULT_CONFIG)
        view = WordFilterSetupView(self, interaction.guild, config)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    @wordfilter.command(name="status", description="[Mod] Status des Wortfilters")
    async def wordfilter_status(self, interaction: discord.Interaction):
        if not await self._require_moderator(interaction):
            return
        state = await self.get_state(interaction.guild_id, force=True)
        if state is None:
            await interaction.response.send_message(
                "❌ Konfiguration konnte nicht geladen werden.", ephemeral=True
            )
            return
        config = state.config
        top = sorted(
            [entry for entry in state.entries if entry.hit_count],
            key=lambda entry: entry.hit_count,
            reverse=True,
        )[:10]
        embed = discord.Embed(
            title="🔎 Wortfilter-Status",
            color=discord.Color.green() if config.get("enabled") else discord.Color.light_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Status",
            value="✅ aktiv" if config.get("enabled") else "❌ deaktiviert",
            inline=True,
        )
        embed.add_field(
            name="Trockenlauf",
            value="ja (nur Log)" if config.get("dry_run") else "nein",
            inline=True,
        )
        embed.add_field(
            name="Einträge",
            value=f"{state.matcher.entry_count} aktiv / {len(state.entries)} gesamt",
            inline=True,
        )
        embed.add_field(
            name="Standard-Aktion",
            value=ACTION_LABELS.get(str(config.get("default_action")), str(config.get("default_action"))),
            inline=True,
        )
        log_channel = config.get("log_channel_id")
        embed.add_field(
            name="Log-Kanal",
            value=f"<#{log_channel}>" if log_channel else "*nicht gesetzt*",
            inline=True,
        )
        embed.add_field(
            name="Ausgenommene Kanäle",
            value=", ".join(f"<#{cid}>" for cid in state.exempt_channels()) or "*keine*",
            inline=False,
        )
        embed.add_field(
            name="Ausgenommene Rollen",
            value=", ".join(f"<@&{rid}>" for rid in state.exempt_roles()) or "*keine*",
            inline=False,
        )
        if top:
            embed.add_field(
                name="Top-Treffer",
                value="\n".join(
                    f"`{discord.utils.escape_markdown(entry.pattern)}` - {entry.hit_count}x"
                    for entry in top
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @wordfilter.command(name="test", description="[Mod] Nachricht gegen den Filter testen (ohne Aktion)")
    @app_commands.describe(text="Der zu prüfende Text")
    async def wordfilter_test(self, interaction: discord.Interaction, text: str):
        if not await self._require_moderator(interaction):
            return
        state = await self.get_state(interaction.guild_id, force=True)
        if state is None or state.matcher.entry_count == 0:
            await interaction.response.send_message(
                "Die Wortliste ist leer.", ephemeral=True
            )
            return

        # Zwei Sichten:
        #   raw       = was der Moderator eingetippt hat
        #   sanitized = was der Filter bei einer echten Nachricht sehen wuerde
        #               (Sticker/Emojis/Mentions entfernt, falls FIX_STICKER_BUG)
        raw_text = text or ""
        if FIX_STICKER_BUG:
            sanitized_text = _CUSTOM_EMOJI_RE.sub(" ", raw_text)
            sanitized_text = _MENTION_RE.sub(" ", sanitized_text)
        else:
            sanitized_text = raw_text

        matches_raw = await asyncio.to_thread(state.matcher.check_all, raw_text)
        if sanitized_text != raw_text:
            matches_effective = await asyncio.to_thread(
                state.matcher.check_all, sanitized_text
            )
        else:
            matches_effective = matches_raw

        embed = discord.Embed(
            title="🧪 Wortfilter-Test",
            color=discord.Color.red() if matches_effective else discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )

        # Eingabe + ggf. bereinigte Sicht
        snippet = raw_text if len(raw_text) <= 900 else raw_text[:900] + "..."
        embed.add_field(name="Eingabe", value=f"```\n{snippet}\n```", inline=False)

        if FIX_STICKER_BUG and sanitized_text != raw_text:
            clean_snippet = (
                sanitized_text if len(sanitized_text) <= 900 else sanitized_text[:900] + "..."
            )
            embed.add_field(
                name="Nach Sticker-/Mention-Fix",
                value=f"```\n{clean_snippet or '-'}\n```",
                inline=False,
            )

        embed.add_field(
            name="Normalisiert",
            value=f"```\n{reduce_text(sanitized_text)[:900] or '-'}\n```",
            inline=False,
        )

        # Treffer-Listen: Roh vs. Effektiv
        def _render_matches(matches) -> str:
            lines = []
            for match in matches[:15]:
                action = _effective_action(state.config, match.entry)
                lines.append(
                    f"• `{discord.utils.escape_markdown(match.pattern)}` "
                    f"({match.method}, {int(match.confidence * 100)} %) "
                    f"→ {ACTION_LABELS.get(action, action)}\n"
                    f"  erkannt als `{discord.utils.escape_markdown(match.matched_text)}`"
                )
            return "\n".join(lines)[:1024]

        if matches_effective:
            embed.add_field(
                name="Treffer (effektiv)",
                value=_render_matches(matches_effective),
                inline=False,
            )
            if matches_raw and sanitized_text != raw_text:
                # Roh-Treffer, die durch den Fix wegfallen, sind fuer Mods
                # interessant - sonst wundert man sich, warum der Bot nicht
                # reagiert, obwohl der Test-Text Treffer hat.
                raw_only = [
                    m for m in matches_raw
                    if m.pattern not in {x.pattern for x in matches_effective}
                ]
                if raw_only:
                    embed.add_field(
                        name="⚠️ Nur im Roh-Text (durch Sticker-Fix entfernt)",
                        value=_render_matches(raw_only),
                        inline=False,
                    )
        else:
            embed.add_field(name="Treffer", value="Kein Treffer.", inline=False)
            if matches_raw and sanitized_text != raw_text:
                embed.add_field(
                    name="ℹ️ Hinweis",
                    value=(
                        "Der rohe Text hätte Treffer, aber der Sticker-/Mention-Fix "
                        "entfernt sie (siehe `FIX_STICKER_BUG`)."
                    ),
                    inline=False,
                )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Einträge ----------
    @wordfilter.command(name="add", description="[Mod] Verbotenes Wort/Zahl hinzufügen")
    @app_commands.describe(
        pattern="Wort, Zahl oder Regex",
        trefferart="Wie soll verglichen werden?",
        aktion="Was soll bei einem Treffer passieren?",
        umgehungen="Umgehungen (Leetspeak/Trenner) erkennen",
        notiz="Optionale Notiz",
    )
    @app_commands.choices(trefferart=MATCH_TYPE_CHOICES, aktion=ACTION_CHOICES)
    async def word_add(
        self,
        interaction: discord.Interaction,
        pattern: str,
        trefferart: Optional[str] = None,
        aktion: Optional[str] = None,
        umgehungen: Optional[bool] = None,
        notiz: Optional[str] = None,
    ):
        if not await self._require_moderator(interaction):
            return
        entry, error = await self._create_entry(
            interaction.guild_id,
            pattern,
            trefferart or "word",
            aktion,
            umgehungen if umgehungen is not None else True,
            notiz,
            str(interaction.user.id),
        )
        if entry is None:
            await interaction.response.send_message(f"❌ {error}", ephemeral=True)
            return
        default_action = str(DEFAULT_CONFIG["default_action"])
        entry_action = entry.action_for(default_action)
        await interaction.response.send_message(
            f"✅ `{entry.pattern}` ist jetzt auf der Liste "
            f"({MATCH_TYPE_LABELS.get(entry.effective_match_type, entry.effective_match_type)}, "
            f"Aktion: {ACTION_LABELS.get(entry_action, entry_action)}).",
            ephemeral=True,
        )

    async def _create_entry(
        self,
        guild_id: int,
        pattern: str,
        match_type: str,
        action: Optional[str],
        fuzzy: bool,
        note: Optional[str],
        created_by: str,
    ) -> Tuple[Optional[WordEntry], Optional[str]]:
        """Legt einen Eintrag an. Rueckgabe: (entry, fehlermeldung)."""
        raw = (pattern or "").strip()
        if match_type not in MATCH_TYPES:
            match_type = "word"
        valid, message = validate_pattern(raw, match_type)
        if not valid:
            return None, message

        try:
            exists = await asyncio.to_thread(
                word_store.entry_exists, str(guild_id), raw
            )
        except Exception as error:
            logger.warning(f"[wordfilter] Duplikatpruefung fehlgeschlagen: {error}")
            exists = False
        if exists:
            return None, "Dieser Eintrag existiert bereits."

        entry = WordEntry(
            pattern=raw,
            match_type=match_type,
            action=action,
            fuzzy=fuzzy,
            note=note,
            created_by=created_by,
        )
        ok = await asyncio.to_thread(word_store.add_entry, str(guild_id), entry)
        if not ok:
            return None, "Speichern in der Datenbank fehlgeschlagen."
        self.invalidate(guild_id)
        return entry, None

    @wordfilter.command(name="remove", description="[Mod] Eintrag entfernen")
    @app_commands.describe(pattern="Der Eintrag, wie er in der Liste steht")
    async def word_remove(self, interaction: discord.Interaction, pattern: str):
        if not await self._require_moderator(interaction):
            return
        removed = await asyncio.to_thread(
            word_store.remove_entry, str(interaction.guild_id), pattern.strip()
        )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"🗑️ `{pattern}` entfernt." if removed else f"`{pattern}` war nicht auf der Liste.",
            ephemeral=True,
        )

    @wordfilter.command(name="enable", description="[Mod] Eintrag aktivieren/deaktivieren")
    @app_commands.describe(pattern="Der Eintrag", aktiv="aktiv oder inaktiv")
    async def word_enable(
        self, interaction: discord.Interaction, pattern: str, aktiv: bool
    ):
        if not await self._require_moderator(interaction):
            return
        ok = await asyncio.to_thread(
            word_store.update_entry,
            str(interaction.guild_id),
            pattern.strip(),
            {"enabled": aktiv},
        )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ `{pattern}` ist jetzt {'aktiv' if aktiv else 'inaktiv'}."
            if ok
            else f"`{pattern}` wurde nicht gefunden.",
            ephemeral=True,
        )

    @wordfilter.command(name="setaction", description="[Mod] Aktion eines Eintrags ändern")
    @app_commands.describe(pattern="Der Eintrag", aktion="Neue Aktion (leer = Standard)")
    @app_commands.choices(aktion=ACTION_CHOICES)
    async def word_setaction(
        self, interaction: discord.Interaction, pattern: str, aktion: Optional[str] = None
    ):
        if not await self._require_moderator(interaction):
            return
        ok = await asyncio.to_thread(
            word_store.update_entry,
            str(interaction.guild_id),
            pattern.strip(),
            {"action": aktion},
        )
        self.invalidate(interaction.guild_id)
        label = ACTION_LABELS.get(aktion, "Standard-Aktion") if aktion else "Standard-Aktion"
        await interaction.response.send_message(
            f"✅ Aktion für `{pattern}`: {label}." if ok else f"`{pattern}` wurde nicht gefunden.",
            ephemeral=True,
        )

    @wordfilter.command(name="list", description="[Mod] Alle Einträge anzeigen")
    @app_commands.describe(suche="Optional: nur Einträge, die diesen Text enthalten")
    async def word_list(self, interaction: discord.Interaction, suche: Optional[str] = None):
        if not await self._require_moderator(interaction):
            return
        state = await self.get_state(interaction.guild_id, force=True)
        entries = state.entries if state else []
        if suche:
            needle = suche.casefold()
            entries = [entry for entry in entries if needle in entry.pattern.casefold()]
        if not entries:
            await interaction.response.send_message(
                "📭 Keine Einträge gefunden.", ephemeral=True
            )
            return
        default_action = str(state.config.get("default_action") or "timeout")
        view = WordListPaginator(entries, default_action)
        await interaction.response.send_message(
            embed=view.build_embed(), view=view, ephemeral=True
        )

    @wordfilter.command(name="import", description="[Mod] Wortliste einfügen (eine Zeile pro Eintrag)")
    @app_commands.describe(
        text="Einträge, durch Zeilenumbrüche getrennt",
        trefferart="Trefferart für alle importierten Einträge",
    )
    @app_commands.choices(trefferart=MATCH_TYPE_CHOICES)
    async def word_import(
        self,
        interaction: discord.Interaction,
        text: str,
        trefferart: Optional[str] = None,
    ):
        if not await self._require_moderator(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        added, skipped, errors = await self._import_text(
            interaction.guild_id, text, trefferart or "word", str(interaction.user.id)
        )
        self.invalidate(interaction.guild_id)
        lines = [f"✅ **{added}** Einträge hinzugefügt."]
        if skipped:
            lines.append(f"⏭️ {skipped} übersprungen (bereits vorhanden).")
        if errors:
            lines.append("⚠️ Fehler:")
            lines.extend(f"• {error}" for error in errors[:10])
        await interaction.followup.send("\n".join(lines)[:1900], ephemeral=True)

    async def _import_text(
        self, guild_id: int, text: str, match_type: str, created_by: str
    ) -> Tuple[int, int, List[str]]:
        raw_lines = [line.strip() for line in (text or "").splitlines()]
        if len(raw_lines) <= 1 and "," in (text or ""):
            raw_lines = [part.strip() for part in (text or "").split(",")]
        candidates = [
            line for line in raw_lines if line and not line.startswith("#")
        ]
        added = 0
        skipped = 0
        errors: List[str] = []
        existing = await asyncio.to_thread(
            word_store.load_entries, str(guild_id), False
        )
        known = {entry.pattern for entry in existing}
        for candidate in candidates:
            if candidate in known:
                skipped += 1
                continue
            valid, message = validate_pattern(candidate, match_type)
            if not valid:
                errors.append(f"`{candidate}`: {message}")
                continue
            entry = WordEntry(
                pattern=candidate,
                match_type=match_type,
                action=None,
                created_by=created_by,
            )
            ok = await asyncio.to_thread(word_store.add_entry, str(guild_id), entry)
            if ok:
                known.add(candidate)
                added += 1
            else:
                errors.append(f"`{candidate}`: Speichern fehlgeschlagen.")
        return added, skipped, errors

    @wordfilter.command(name="clear", description="[Mod] ALLE Einträge des Servers löschen")
    @app_commands.describe(bestaetigen="Wirklich alle Einträge löschen?")
    async def word_clear(self, interaction: discord.Interaction, bestaetigen: bool):
        if not await self._require_moderator(interaction):
            return
        if not bestaetigen:
            await interaction.response.send_message("Abgebrochen.", ephemeral=True)
            return
        count = await asyncio.to_thread(
            word_store.clear_entries, str(interaction.guild_id)
        )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"🗑️ {count} Einträge gelöscht.", ephemeral=True
        )

    # ---------- Whitelist ----------
    @wordfilter.command(name="allow_add", description="[Mod] Wort von der Prüfung ausnehmen")
    @app_commands.describe(pattern="Wort, das nie gefiltert werden soll")
    async def allow_add(self, interaction: discord.Interaction, pattern: str):
        if not await self._require_moderator(interaction):
            return
        normalized = pattern.strip()
        if not normalized:
            await interaction.response.send_message("❌ Leerer Eintrag.", ephemeral=True)
            return
        ok = await asyncio.to_thread(
            word_store.add_allow, str(interaction.guild_id), normalized, str(interaction.user.id)
        )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"✅ `{normalized}` wird nicht mehr gefiltert."
            if ok
            else f"`{normalized}` steht bereits auf der Whitelist.",
            ephemeral=True,
        )

    @wordfilter.command(name="allow_remove", description="[Mod] Wort wieder prüfen")
    async def allow_remove(self, interaction: discord.Interaction, pattern: str):
        if not await self._require_moderator(interaction):
            return
        ok = await asyncio.to_thread(
            word_store.remove_allow, str(interaction.guild_id), pattern.strip()
        )
        self.invalidate(interaction.guild_id)
        await interaction.response.send_message(
            f"🗑️ `{pattern}` wieder aktiv geprüft." if ok else f"`{pattern}` war nicht auf der Whitelist.",
            ephemeral=True,
        )

    @wordfilter.command(name="allow_list", description="[Mod] Whitelist anzeigen")
    async def allow_list(self, interaction: discord.Interaction):
        if not await self._require_moderator(interaction):
            return
        words = await asyncio.to_thread(word_store.list_allow, str(interaction.guild_id))
        embed = discord.Embed(
            title="✅ Wortfilter-Whitelist",
            description="\n".join(f"`{word}`" for word in words)[:4000] or "*leer*",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class _MinimalDb:
    """Fallback, falls das Raid-Cog (noch) nicht geladen ist."""

    async def get_moderator_role_ids(self, guild: discord.Guild):
        return set()

    async def get_log_channel(self, guild: discord.Guild):
        return None


# ── Setup-View ──────────────────────────────────────────────────────────────

class WordFilterSetupView(discord.ui.View):
    """Interaktive Konfiguration des Wortfilters."""

    def __init__(self, cog: WordFilterCog, guild: discord.Guild, config: Dict[str, Any]):
        super().__init__(timeout=600)
        self.cog = cog
        self.guild = guild
        self.config = dict(DEFAULT_CONFIG)
        self.config.update(config or {})
        self.page = 1
        self._rebuild()

    # ---- Anzeige ----
    def build_embed(self) -> discord.Embed:
        config = self.config
        enabled = bool(config.get("enabled"))
        embed = discord.Embed(
            title="⚙️ Wortfilter-Setup",
            color=discord.Color.green() if enabled else discord.Color.light_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Status",
            value="✅ aktiv" if enabled else "❌ deaktiviert",
            inline=True,
        )
        embed.add_field(
            name="Trockenlauf",
            value="🧪 ja (nur Log)" if config.get("dry_run") else "nein",
            inline=True,
        )
        embed.add_field(
            name="Standard-Aktion",
            value=ACTION_LABELS.get(str(config.get("default_action")), str(config.get("default_action"))),
            inline=True,
        )
        embed.add_field(
            name="Timeout-Dauer",
            value=f"{config.get('timeout_minutes')} Minuten",
            inline=True,
        )
        embed.add_field(
            name="Log-Kanal",
            value=f"<#{config.get('log_channel_id')}>" if config.get("log_channel_id") else "*nicht gesetzt*",
            inline=True,
        )
        embed.add_field(
            name="Nachricht löschen",
            value="ja" if config.get("delete_message") else "nein",
            inline=True,
        )
        if self.page == 1:
            embed.add_field(
                name="Zahlen-Regel",
                value=(
                    "Ein verbotenes Zahlwort trifft nur, wenn es als "
                    "eigenständige Zahl vorkommt. `123` trifft **nicht** in "
                    "`1234`, `4123` oder `abc123`."
                ),
                inline=False,
            )
            embed.add_field(
                name="Umgehungen",
                value=(
                    "Leetspeak (`b4d`), Homoglyphe (`bаd` mit kyrillischem а), "
                    "Zeichenwiederholungen (`baaad`) und Trenner (`b.a.d`) "
                    "werden pro Eintrag erkannt."
                ),
                inline=False,
            )
            if FIX_STICKER_BUG:
                embed.add_field(
                    name="Sticker-Fix",
                    value=(
                        "Sticker, Custom-Emojis und Mentions werden vor der "
                        "Prüfung entfernt (`FIX_STICKER_BUG = True`)."
                    ),
                    inline=False,
                )
        else:
            embed.add_field(
                name="Kanäle ohne Prüfung",
                value="\n".join(
                    f"<#{cid}>" for cid in parse_id_list(config.get("exempt_channel_ids"))
                )
                or "*keine*",
                inline=False,
            )
            embed.add_field(
                name="Rollen ohne Prüfung",
                value="\n".join(
                    f"<@&{rid}>" for rid in parse_id_list(config.get("exempt_role_ids"))
                )
                or "*keine*",
                inline=False,
            )
        embed.set_footer(text=f"Seite {self.page}/2 · Änderungen erst mit 💾 Speichern aktiv")
        return embed

    # ---- Aufbau ----
    def _rebuild(self) -> None:
        self.clear_items()
        if self.page == 2:
            self._rebuild_page_two()
            return

        self.add_item(self._toggle_button(
            "enabled",
            "🟢 Aktivieren" if not self.config.get("enabled") else "🔴 Deaktivieren",
            discord.ButtonStyle.success if not self.config.get("enabled") else discord.ButtonStyle.danger,
            row=0,
        ))
        self.add_item(self._toggle_button(
            "dry_run",
            "🧪 Trockenlauf an" if not self.config.get("dry_run") else "🧪 Trockenlauf aus",
            discord.ButtonStyle.secondary,
            row=0,
        ))

        action_select = discord.ui.Select(
            placeholder="Standard-Aktion bei einem Treffer",
            options=[
                discord.SelectOption(
                    label=ACTION_LABELS[value],
                    value=value,
                    default=str(self.config.get("default_action")) == value,
                )
                for value in ACTIONS
            ],
            row=1,
        )
        action_select.callback = self._on_action
        self.add_item(action_select)

        timeout_button = discord.ui.Button(
            label=f"⏱️ Timeout-Dauer ({self.config.get('timeout_minutes')} min)",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        timeout_button.callback = self._on_timeout
        self.add_item(timeout_button)

        self.add_item(self._toggle_button(
            "delete_message",
            "🗑️ Löschen an" if not self.config.get("delete_message") else "🗑️ Löschen aus",
            discord.ButtonStyle.secondary,
            row=2,
        ))
        self.add_item(self._toggle_button(
            "notify_user",
            "✉️ DM an" if not self.config.get("notify_user") else "✉️ DM aus",
            discord.ButtonStyle.secondary,
            row=2,
        ))
        self.add_item(self._toggle_button(
            "check_edits",
            "✏️ Edits prüfen an" if not self.config.get("check_edits") else "✏️ Edits prüfen aus",
            discord.ButtonStyle.secondary,
            row=3,
        ))
        self.add_item(self._page_button(row=3))
        self.add_item(self._save_button(row=4))

    def _rebuild_page_two(self) -> None:
        channel_select = discord.ui.ChannelSelect(
            placeholder="📢 Kanäle ohne Prüfung",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=10,
            row=0,
        )
        channel_select.callback = self._on_channels
        self.add_item(channel_select)

        role_select = discord.ui.RoleSelect(
            placeholder="🎭 Rollen ohne Prüfung",
            min_values=0,
            max_values=10,
            row=1,
        )
        role_select.callback = self._on_roles
        self.add_item(role_select)

        log_select = discord.ui.ChannelSelect(
            placeholder="📋 Log-Kanal für Wortfilter-Meldungen",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
            row=2,
        )
        log_select.callback = self._on_log_channel
        self.add_item(log_select)

        self.add_item(self._page_button(row=3))
        self.add_item(self._save_button(row=4))

    # ---- Bausteine ----
    def _toggle_button(self, key: str, label: str, style: discord.ButtonStyle, row: int) -> discord.ui.Button:
        button = discord.ui.Button(label=label, style=style, row=row)

        async def callback(interaction: discord.Interaction):
            if not await self.cog._require_moderator(interaction):
                return
            self.config[key] = not bool(self.config.get(key))
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        button.callback = callback
        return button

    def _page_button(self, row: int) -> discord.ui.Button:
        button = discord.ui.Button(
            label="➡️ Seite 2 (Ausnahmen)" if self.page == 1 else "⬅️ Seite 1",
            style=discord.ButtonStyle.primary,
            row=row,
        )

        async def callback(interaction: discord.Interaction):
            if not await self.cog._require_moderator(interaction):
                return
            self.page = 2 if self.page == 1 else 1
            self._rebuild()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        button.callback = callback
        return button

    def _save_button(self, row: int) -> discord.ui.Button:
        button = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success, row=row)

        async def callback(interaction: discord.Interaction):
            if not await self.cog._require_moderator(interaction):
                return
            if not self.config.get("log_channel_id"):
                await interaction.response.send_message(
                    "❌ Bitte auf Seite 2 einen Log-Kanal auswählen.", ephemeral=True
                )
                return
            payload = {
                key: self.config.get(key, DEFAULT_CONFIG[key])
                for key in DEFAULT_CONFIG
            }
            ok = await asyncio.to_thread(
                word_store.set_config, str(interaction.guild_id), payload
            )
            if not ok:
                await interaction.response.send_message(
                    "❌ Speichern fehlgeschlagen.", ephemeral=True
                )
                return
            self.cog.invalidate(interaction.guild_id)
            embed = self.build_embed()
            embed.title = "✅ Wortfilter gespeichert"
            embed.color = discord.Color.green()
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)

        button.callback = callback
        return button

    # ---- Callbacks ----
    async def _on_action(self, interaction: discord.Interaction):
        if not await self.cog._require_moderator(interaction):
            return
        self.config["default_action"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_timeout(self, interaction: discord.Interaction):
        if not await self.cog._require_moderator(interaction):
            return
        await interaction.response.send_modal(_TimeoutMinutesModal(self))

    async def _on_channels(self, interaction: discord.Interaction):
        if not await self.cog._require_moderator(interaction):
            return
        self.config["exempt_channel_ids"] = word_store.join_id_list(
            interaction.data["values"]
        )
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_roles(self, interaction: discord.Interaction):
        if not await self.cog._require_moderator(interaction):
            return
        self.config["exempt_role_ids"] = word_store.join_id_list(
            interaction.data["values"]
        )
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_log_channel(self, interaction: discord.Interaction):
        if not await self.cog._require_moderator(interaction):
            return
        self.config["log_channel_id"] = interaction.data["values"][0]
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class _TimeoutMinutesModal(discord.ui.Modal, title="Timeout-Dauer"):
    dauer = discord.ui.TextInput(
        label="Dauer in Minuten (max. 40320 = 28 Tage)",
        placeholder="z. B. 10080",
        required=True,
        max_length=5,
    )

    def __init__(self, parent: WordFilterSetupView):
        super().__init__()
        self.parent = parent
        self.dauer.default = str(parent.config.get("timeout_minutes") or 10080)

    async def on_submit(self, interaction: discord.Interaction):
        if not await self.parent.cog._require_moderator(interaction):
            return
        try:
            minutes = int(str(self.dauer.value).strip())
        except ValueError:
            await interaction.response.send_message(
                "❌ Bitte eine ganze Zahl eingeben.", ephemeral=True
            )
            return
        if minutes < 1 or minutes > 40320:
            await interaction.response.send_message(
                "❌ Bitte zwischen 1 und 40320 Minuten.", ephemeral=True
            )
            return
        self.parent.config["timeout_minutes"] = minutes
        self.parent._rebuild()
        await interaction.response.edit_message(
            embed=self.parent.build_embed(), view=self.parent
        )


# ── Eintrags-Paginierung ────────────────────────────────────────────────────

class WordListPaginator(discord.ui.View):
    def __init__(self, entries: List[WordEntry], default_action: str, per_page: int = 10):
        super().__init__(timeout=300)
        self.entries = entries
        self.default_action = default_action
        self.per_page = per_page
        self.page = 0
        self._refresh()

    @property
    def max_page(self) -> int:
        return max(0, (len(self.entries) - 1) // self.per_page)

    def build_embed(self) -> discord.Embed:
        start = self.page * self.per_page
        page_entries = self.entries[start:start + self.per_page]
        embed = discord.Embed(
            title="📋 Wortfilter-Liste",
            description=f"{len(self.entries)} Einträge · Seite {self.page + 1}/{self.max_page + 1}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        for entry in page_entries:
            action = entry.action_for(self.default_action)
            status = "" if entry.enabled else " · ⏸️ inaktiv"
            note = f"\n_{entry.note[:80]}_" if entry.note else ""
            embed.add_field(
                name=f"{entry.pattern[:200]}",
                value=(
                    f"{MATCH_TYPE_LABELS.get(entry.effective_match_type, entry.effective_match_type)}"
                    f" · {ACTION_LABELS.get(action, action)}{status}"
                    f" · Treffer: {entry.hit_count}{note}"
                ),
                inline=False,
            )
        return embed

    def _refresh(self) -> None:
        self.clear_items()
        prev_button = discord.ui.Button(
            label="⬅️", style=discord.ButtonStyle.secondary, disabled=self.page <= 0
        )
        prev_button.callback = self._prev
        self.add_item(prev_button)
        next_button = discord.ui.Button(
            label="➡️", style=discord.ButtonStyle.secondary, disabled=self.page >= self.max_page
        )
        next_button.callback = self._next
        self.add_item(next_button)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.max_page, self.page + 1)
        self._refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


async def setup(bot: commands.Bot):
    await bot.add_cog(WordFilterCog(bot))