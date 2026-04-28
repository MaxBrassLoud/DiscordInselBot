"""
bot/features/levels/cog.py
===========================
Level-System für den Insel Bot.

PUNKTE:
  • Jede Nachricht:                +1 Punkt  (max 1 Punkt pro 60s pro User – Spam-Schutz)
  • Jede Minute im Voice Chat:     +2 Punkte (Voice-Tracking per Task alle 60s)
  • Jede Reaktion auf Nachrichten: +1 Punkt  (wer reagiert bekommt den Punkt)

LEVEL-FORMEL (exponentiell):
  XP für Level N = 10 * (N ^ 1.8)
  → Level 1:   10 XP
  → Level 5:   229 XP
  → Level 10:  631 XP
  → Level 20:  2.512 XP
  → Level 50:  28.900 XP

SETUP:
  /level setup  – Level-Update-Kanal konfigurieren

COMMANDS:
  /level info [@user]  – Level-Card anzeigen
  /level top           – Rangliste (Top 10)
  /level setup         – Admin: Level-Kanal konfigurieren
  /level reset [@user] – Admin: Level zurücksetzen
  /level xp <user> <menge> – Admin: XP manuell ändern

SUPABASE SQL (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS user_levels (
        id            BIGSERIAL PRIMARY KEY,
        user_id       TEXT NOT NULL,
        server_id     TEXT NOT NULL,
        xp            INTEGER DEFAULT 0,
        level         INTEGER DEFAULT 0,
        messages      INTEGER DEFAULT 0,
        voice_minutes INTEGER DEFAULT 0,
        reactions     INTEGER DEFAULT 0,
        last_msg_at   TIMESTAMPTZ,
        updated_at    TIMESTAMPTZ DEFAULT now(),
        UNIQUE (user_id, server_id)
    );
    CREATE INDEX IF NOT EXISTS idx_user_levels_server
        ON user_levels (server_id, xp DESC);

    ALTER TABLE settings
        ADD COLUMN IF NOT EXISTS level_channel_id TEXT;
    ALTER TABLE settings
        ADD COLUMN IF NOT EXISTS levels_enabled BOOLEAN DEFAULT TRUE;
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from math import trunc
from typing import Optional, Dict, Set, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("levels")

# ══════════════════════════════════════════════════════════════════════════════
# KONSTANTEN
# ══════════════════════════════════════════════════════════════════════════════

MSG_COOLDOWN_SECONDS = 0          # Mindestabstand zwischen Nachricht-XP für einen User
VOICE_XP_PER_MINUTE = 2            # XP pro Minute im Voice-Kanal
MSG_XP = 1                         # XP pro Nachricht
REACTION_XP = 1                    # XP pro Reaktion

XP_BASE = 10
XP_EXPONENT = 1.1

# Cache‑Einstellungen
FLUSH_INTERVAL_SECONDS = 600       # 10 Minuten
FLUSH_EVENT_THRESHOLD = 200        # Nach 200 XP-Events zurückschreiben

# ══════════════════════════════════════════════════════════════════════════════
# LEVEL MATH
# ══════════════════════════════════════════════════════════════════════════════

def xp_for_level(level: int) -> int:
    """XP die benötigt werden um Level N zu erreichen (kumulativ)."""
    if level <= 0:
        return 0
    return math.ceil(XP_BASE * (level ** XP_EXPONENT))


def total_xp_for_level(level: int) -> int:
    """Gesamt-XP die benötigt werden um Level N zu erreichen."""
    return sum(xp_for_level(lvl) for lvl in range(1, level + 1))


def level_from_xp(xp: int) -> int:
    """Berechnet das aktuelle Level anhand der Gesamt-XP."""
    level = 0
    needed = 0
    while needed + xp_for_level(level + 1) <= xp:
        level += 1
        needed += xp_for_level(level)
    return level


def xp_progress(xp: int) -> tuple[int, int, int]:
    """
    Gibt (current_level, xp_in_level, xp_needed_for_next) zurück.
    xp_in_level = XP die der User im aktuellen Level bereits hat
    xp_needed   = XP die er für den nächsten Level-Aufstieg braucht
    """
    level = level_from_xp(xp)
    spent = total_xp_for_level(level)
    xp_in_level = xp - spent
    xp_for_next_level = xp_for_level(level + 1)
    return level, xp_in_level, xp_for_next_level


def progress_bar(current: int, total: int, length: int = 15) -> str:
    """Erstellt einen Text-Fortschrittsbalken."""
    filled = int(length * current / max(total, 1))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}]"


# ══════════════════════════════════════════════════════════════════════════════
# DATABASE & CACHE (global für den gesamten Cog)
# ══════════════════════════════════════════════════════════════════════════════

_user_cache: Dict[str, dict] = {}      # key = f"{server_id}:{user_id}"
_dirty_keys: Set[str] = set()          # Keys, die noch nicht in der DB sind
_flush_lock = asyncio.Lock()           # Verhindert parallele Flushes


def _get_user_row(server_id: str, user_id: str) -> Optional[dict]:
    """Liest einen User‑Datensatz aus der Datenbank (ohne Cache)."""
    try:
        r = get_supabase().table("user_levels") \
            .select("*") \
            .eq("server_id", server_id) \
            .eq("user_id", user_id) \
            .execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[levels] _get_user_row: {e}")
        return None


def _load_into_cache(server_id: str, user_id: str) -> Optional[dict]:
    """Lädt einen User aus der DB in den Cache, falls vorhanden."""
    key = f"{server_id}:{user_id}"
    row = _get_user_row(server_id, user_id)
    if row:
        _user_cache[key] = row
        _dirty_keys.discard(key)
    return _user_cache.get(key)


def _get_cached_user(server_id: str, user_id: str) -> Optional[dict]:
    """Holt User aus Cache (lädt bei Bedarf aus DB)."""
    key = f"{server_id}:{user_id}"
    if key not in _user_cache:
        _load_into_cache(server_id, user_id)
    return _user_cache.get(key)


def _upsert_xp(
    server_id: str,
    user_id: str,
    xp_delta: int,
    msg_delta: int = 0,
    voice_delta: int = 0,
    reaction_delta: int = 0,
) -> tuple[int, int, int]:
    """
    Fügt XP hinzu (im Cache) und gibt (old_level, new_level, new_xp) zurück.
    Die Änderungen werden als „dirty“ markiert und später asynchron geschrieben.
    """
    key = f"{server_id}:{user_id}"
    now = datetime.now(timezone.utc).isoformat()
    current = _get_cached_user(server_id, user_id)

    if not current:
        # Neuer User – lege initialen Record an
        new_xp = max(0, xp_delta)
        new_level = level_from_xp(new_xp)
        current = {
            "user_id": user_id,
            "server_id": server_id,
            "xp": new_xp,
            "level": new_level,
            "messages": msg_delta,
            "voice_minutes": voice_delta,
            "reactions": reaction_delta,
            "last_msg_at": now if msg_delta else None,
            "updated_at": now,
        }
        _user_cache[key] = current
        _dirty_keys.add(key)
        old_level = 0
        return old_level, new_level, new_xp

    # Bestehenden User aktualisieren
    old_level = current.get("level", 0)
    current["xp"] = max(0, current.get("xp", 0) + xp_delta)
    current["messages"] = current.get("messages", 0) + msg_delta
    current["voice_minutes"] = current.get("voice_minutes", 0) + voice_delta
    current["reactions"] = current.get("reactions", 0) + reaction_delta
    if msg_delta:
        current["last_msg_at"] = now
    current["updated_at"] = now

    new_level = level_from_xp(current["xp"])
    current["level"] = new_level
    _dirty_keys.add(key)

    return old_level, new_level, current["xp"]


def _get_level_channel(server_id: str) -> Optional[str]:
    """Liest den konfigurierten Level‑Kanal aus der DB (ohne Cache)."""
    try:
        r = get_supabase().table("settings") \
            .select("level_channel_id, levels_enabled") \
            .eq("guild_id", server_id).execute()
        if r.data:
            row = r.data[0]
            if row.get("levels_enabled") is False:
                return None
            return row.get("level_channel_id")
    except Exception as e:
        logger.error(f"[levels] _get_level_channel: {e}")
    return None


def _set_level_channel(server_id: str, channel_id: Optional[str], enabled: bool = True):
    """Speichert die Level‑Kanal‑Konfiguration direkt (kein Cache nötig)."""
    sb = get_supabase()
    existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
    data = {"guild_id": server_id, "level_channel_id": channel_id, "levels_enabled": enabled}
    if existing.data:
        sb.table("settings").update(data).eq("guild_id", server_id).execute()
    else:
        sb.table("settings").insert(data).execute()


def _get_leaderboard(server_id: str, limit: int = 10) -> List[dict]:
    """Rangliste direkt aus der DB (immer aktuell)."""
    try:
        r = get_supabase().table("user_levels") \
            .select("user_id, xp, level, messages, voice_minutes, reactions") \
            .eq("server_id", server_id) \
            .order("xp", desc=True) \
            .limit(limit).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[levels] _get_leaderboard: {e}")
        return []


def _get_rank(server_id: str, user_id: str) -> int:
    """Ermittelt den Rang eines Users (basierend auf der aktuellen DB)."""
    try:
        r = get_supabase().table("user_levels") \
            .select("user_id") \
            .eq("server_id", server_id) \
            .order("xp", desc=True).execute()
        for i, row in enumerate(r.data or [], 1):
            if row["user_id"] == user_id:
                return i
    except Exception as e:
        logger.error(f"[levels] _get_rank: {e}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
# LEVEL-UP EMBED
# ══════════════════════════════════════════════════════════════════════════════

def _build_levelup_embed(member: discord.Member, old_level: int, new_level: int, total_xp: int) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 Level Up!",
        description=(
            f"**{member.display_name}** ist von **Level {old_level}** auf "
            f"**Level {new_level}** aufgestiegen! 🚀"
        ),
        color=discord.Color.from_rgb(74, 222, 128),
    )
    _, xp_in, xp_next = xp_progress(total_xp)
    bar = progress_bar(xp_in, xp_next)
    embed.add_field(
        name="📊 Fortschritt",
        value=f"`{bar}` {xp_in}/{xp_next} XP",
        inline=False,
    )
    embed.add_field(name="⭐ Gesamt-XP", value=f"{total_xp:,}", inline=True)
    embed.add_field(name="🏆 Neues Level", value=str(new_level), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Server: {member.guild.name}")
    embed.timestamp = discord.utils.utcnow()
    return embed


def _build_level_card(
    member: discord.Member,
    row: dict,
    rank: int,
) -> discord.Embed:
    xp = row.get("xp", 0)
    level = row.get("level", 0)
    messages = row.get("messages", 0)
    voice_min = row.get("voice_minutes", 0)
    reactions = row.get("reactions", 0)

    _, xp_in, xp_next = xp_progress(xp)
    bar = progress_bar(xp_in, xp_next)

    embed = discord.Embed(
        title=f"📊 Level-Info – {member.display_name}",
        color=discord.Color.from_rgb(74, 222, 128),
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏆 Level", value=str(level), inline=True)
    embed.add_field(name="📍 Rang", value=f"#{rank}", inline=True)
    embed.add_field(name="⭐ Gesamt-XP", value=f"{xp:,}", inline=True)
    embed.add_field(
        name="📈 Fortschritt zum nächsten Level",
        value=f"`{bar}` {xp_in:,} / {xp_next:,} XP",
        inline=False,
    )
    embed.add_field(name="💬 Nachrichten", value=f"{messages:,}", inline=True)
    embed.add_field(name="🎙️ Voice-Minuten", value=f"{voice_min:,}", inline=True)
    embed.add_field(name="😄 Reaktionen", value=f"{reactions:,}", inline=True)

    next_level_total = total_xp_for_level(level + 1)
    current_total = total_xp_for_level(level)
    embed.add_field(
        name="🎯 Nächstes Level",
        value=f"Level {level + 1} bei {next_level_total:,} XP total ({next_level_total - xp:,} XP noch nötig)",
        inline=False,
    )
    embed.set_footer(text=f"Server: {member.guild.name}")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════

class LevelSetupView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.channel_id: Optional[str] = None
        self.enabled = True
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="⚙️ Level-System Setup",
            color=discord.Color.from_rgb(74, 222, 128),
        )
        e.add_field(
            name="📢 Level-Update Kanal",
            value=f"<#{self.channel_id}>" if self.channel_id else "*Nicht gesetzt – Level-Ups werden still verarbeitet*",
            inline=False,
        )
        e.add_field(
            name="🔧 System",
            value="✅ Aktiv" if self.enabled else "❌ Deaktiviert",
            inline=True,
        )
        e.add_field(
            name="📊 Punkte-Übersicht",
            value=(
                "💬 Nachricht → **+1 XP** (max 1×/Minute)\n"
                "🎙️ Voice-Minute → **+2 XP**\n"
                "😄 Reaktion → **+1 XP**"
            ),
            inline=False,
        )
        return e

    def _rebuild(self):
        self.clear_items()

        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Kanal für Level-Up Nachrichten wählen…",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        ch_sel.callback = self._on_channel
        self.add_item(ch_sel)

        toggle_btn = discord.ui.Button(
            label=f"System: {'AN ✅' if self.enabled else 'AUS ❌'}",
            style=discord.ButtonStyle.success if self.enabled else discord.ButtonStyle.secondary,
            row=1,
        )
        toggle_btn.callback = self._on_toggle
        self.add_item(toggle_btn)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    async def _on_channel(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.channel_id = vals[0] if vals else None
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.enabled = not self.enabled
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        _set_level_channel(self.guild_id, self.channel_id, self.enabled)
        embed = self._build_embed()
        embed.title = "✅ Level-System gespeichert!"
        embed.color = discord.Color.green()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class LevelsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Cooldown‑Cache (reine Speicherung der letzten Nachricht)
        self._msg_cooldown: Dict[str, datetime] = {}
        # Voice‑Tracking (join-Zeiten)
        self._voice_joined: Dict[str, datetime] = {}
        # Event‑Zähler für Flush nach Schwellwert
        self._event_counter = 0

        # Regelmäßigen Flush starten
        self.flush_cache_task.start()

    def cog_unload(self):
        """Beim Herunterfahren des Cogs: Cache in DB schreiben und Tasks stoppen."""
        self.flush_cache_task.cancel()
        self._flush_cache_sync()   # Synchron flush, weil wir im Shutdown sind

    # ═══════════════════════════════════════════════════════════════════════════
    # Flush‑Logik (Cache → Datenbank)
    # ═══════════════════════════════════════════════════════════════════════════

    def _flush_cache_sync(self):
        """Schreibt alle dirty Records in einem Batch in die Datenbank."""
        if not _dirty_keys:
            return

        records_to_upsert = []
        for key in list(_dirty_keys):
            record = _user_cache.get(key)
            if not record:
                _dirty_keys.discard(key)
                continue

            # Nur die Spalten, die in der Tabelle existieren
            clean_record = {
                "user_id": record["user_id"],
                "server_id": record["server_id"],
                "xp": record["xp"],
                "level": record["level"],
                "messages": record["messages"],
                "voice_minutes": record["voice_minutes"],
                "reactions": record["reactions"],
                "updated_at": record["updated_at"],
            }
            # last_msg_at nur hinzufügen, wenn vorhanden
            if record.get("last_msg_at"):
                clean_record["last_msg_at"] = record["last_msg_at"]

            records_to_upsert.append(clean_record)

        if not records_to_upsert:
            return

        try:
            sb = get_supabase()
            # Wichtig: on_conflict auf die beiden Unique-Spalten
            sb.table("user_levels").upsert(
                records_to_upsert,
                on_conflict="user_id,server_id"
            ).execute()
            # Nur bei Erfolg als clean markieren
            _dirty_keys.clear()
            logger.info(f"[levels] Flushed {len(records_to_upsert)} dirty records successfully.")
        except Exception as e:
            logger.error(f"[levels] Fehler beim Flush: {e}", exc_info=True)
            # dirty bleiben – wird beim nächsten Flush erneut versucht

    async def _flush_cache_async(self):
        """Asynchroner Flush mit Lock, um parallele Ausführung zu verhindern."""
        async with _flush_lock:
            await self.bot.loop.run_in_executor(None, self._flush_cache_sync)

    @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
    async def flush_cache_task(self):
        """Periodischer Flush (alle 10 Minuten)."""
        await self._flush_cache_async()

    @flush_cache_task.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

    # ═══════════════════════════════════════════════════════════════════════════
    # Level‑Up Benachrichtigung
    # ═══════════════════════════════════════════════════════════════════════════

    async def _notify_levelup(
        self,
        guild: discord.Guild,
        member: discord.Member,
        old_level: int,
        new_level: int,
        total_xp: int,
    ):
        channel_id = _get_level_channel(str(guild.id))
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return
        embed = _build_levelup_embed(member, old_level, new_level, total_xp)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(f"[levels] Kein Zugriff auf Level-Kanal {channel_id}")
        except Exception as e:
            logger.error(f"[levels] _notify_levelup: {e}")

    # ═══════════════════════════════════════════════════════════════════════════
    # Cooldown‑Helper für Nachrichten
    # ═══════════════════════════════════════════════════════════════════════════

    def _check_msg_cooldown(self, server_id: str, user_id: str) -> bool:
        """Prüft, ob der User für diese Nachricht XP bekommen darf."""
        key = f"{server_id}:{user_id}"
        now = datetime.now(timezone.utc)
        last = self._msg_cooldown.get(key)
        if last and (now - last).total_seconds() < MSG_COOLDOWN_SECONDS:
            return False
        self._msg_cooldown[key] = now
        return True

    # ═══════════════════════════════════════════════════════════════════════════
    # Event‑Listener
    # ═══════════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        server_id = str(message.guild.id)
        user_id = str(message.author.id)

        if not self._check_msg_cooldown(server_id, user_id):
            return

        old_level, new_level, new_xp = _upsert_xp(
            server_id, user_id, MSG_XP, msg_delta=1
        )

        # Level‑Up benachrichtigen
        if new_level > old_level:
            await self._notify_levelup(message.guild, message.author, old_level, new_level, new_xp)

        # Event‑Zähler erhöhen und ggf. Flush auslösen
        self._event_counter += 1
        if self._event_counter >= FLUSH_EVENT_THRESHOLD:
            self._event_counter = 0
            await self._flush_cache_async()

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot:
            return
        guild = reaction.message.guild
        if not guild:
            return

        server_id = str(guild.id)
        user_id = str(user.id)

        old_level, new_level, new_xp = _upsert_xp(
            server_id, user_id, REACTION_XP, reaction_delta=1
        )

        if new_level > old_level:
            member = guild.get_member(user.id)
            if member:
                await self._notify_levelup(guild, member, old_level, new_level, new_xp)

        self._event_counter += 1
        if self._event_counter >= FLUSH_EVENT_THRESHOLD:
            self._event_counter = 0
            await self._flush_cache_async()

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return
        server_id = str(member.guild.id)
        user_id = str(member.id)
        key = f"{server_id}:{user_id}"

        # Kanal betreten
        if after.channel and not before.channel:
            self._voice_joined[key] = datetime.now(timezone.utc)
        # Kanal verlassen / wechseln → Timestamp entfernen (wird beim Wechsel neu gesetzt)
        elif before.channel and not after.channel:
            self._voice_joined.pop(key, None)

    @tasks.loop(seconds=60)
    async def voice_xp_task(self):
        """Alle 60 Sekunden: Voice‑XP für aktive User vergeben."""
        now = datetime.now(timezone.utc)
        for key, joined_at in list(self._voice_joined.items()):
            if (now - joined_at).total_seconds() < 55:
                continue
            try:
                server_id, user_id = key.split(":", 1)
                guild = discord.utils.get(self.bot.guilds, id=int(server_id))
                if not guild:
                    self._voice_joined.pop(key, None)
                    continue
                member = guild.get_member(int(user_id))
                if not member or not member.voice or not member.voice.channel:
                    self._voice_joined.pop(key, None)
                    continue

                # AFK‑Kanal ignorieren
                if guild.afk_channel and member.voice.channel.id == guild.afk_channel.id:
                    continue

                # Allein im Kanal? Keine XP (Anti‑AFK)
                non_bot_members = [m for m in member.voice.channel.members if not m.bot]
                if len(non_bot_members) < 2:
                    continue

                old_level, new_level, new_xp = _upsert_xp(
                    server_id, user_id, VOICE_XP_PER_MINUTE, voice_delta=1
                )

                if new_level > old_level:
                    await self._notify_levelup(guild, member, old_level, new_level, new_xp)

                self._event_counter += 1
                if self._event_counter >= FLUSH_EVENT_THRESHOLD:
                    self._event_counter = 0
                    await self._flush_cache_async()

            except Exception as e:
                logger.error(f"[levels] voice_xp_task key={key}: {e}")

    @voice_xp_task.before_loop
    async def before_voice_xp(self):
        await self.bot.wait_until_ready()

    # ═══════════════════════════════════════════════════════════════════════════
    # Slash Commands
    # ═══════════════════════════════════════════════════════════════════════════

    level = app_commands.Group(name="level", description="Level-System")

    @level.command(name="info", description="Zeige dein Level oder das eines anderen Mitglieds")
    @app_commands.describe(mitglied="Das Mitglied dessen Level angezeigt werden soll")
    async def level_info(
        self,
        interaction: discord.Interaction,
        mitglied: Optional[discord.Member] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        # Cache vor dem Lesen des Rangs in DB schreiben
        await self._flush_cache_async()

        target = mitglied or interaction.user
        server_id = str(interaction.guild_id)
        user_id = str(target.id)

        cached = _get_cached_user(server_id, user_id)
        if cached:
            row = cached
        else:
            row = _get_user_row(server_id, user_id)

        if not row:
            if target == interaction.user:
                await interaction.followup.send(
                    "📊 Du hast noch keine XP gesammelt. Schreib Nachrichten, "
                    "geh in Voice-Kanäle oder reagiere auf Nachrichten um XP zu verdienen!",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"📊 **{target.display_name}** hat noch keine XP gesammelt.",
                    ephemeral=True,
                )
            return

        rank = _get_rank(server_id, user_id)  # jetzt aktuell, weil vorher geflusht
        embed = _build_level_card(target, row, rank)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @level.command(name="top", description="Zeige die Top 10 Mitglieder nach Level")
    async def level_top(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        # Cache vor dem Lesen der Rangliste in DB schreiben
        await self._flush_cache_async()

        server_id = str(interaction.guild_id)
        rows = _get_leaderboard(server_id, limit=10)

        if not rows:
            await interaction.followup.send(
                "📊 Noch keine Daten vorhanden. Sammle XP indem du Nachrichten schreibst!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🏆 Level-Rangliste – {interaction.guild.name}",
            color=discord.Color.from_rgb(74, 222, 128),
        )

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows, 1):
            member = interaction.guild.get_member(int(row["user_id"]))
            name = member.display_name if member else f"<@{row['user_id']}>"
            medal = medals[i - 1] if i <= 3 else f"**#{i}**"
            level = row.get("level", 0)
            xp = row.get("xp", 0)
            lines.append(f"{medal} **{name}** — Level {level} · {xp:,} XP")

        embed.description = "\n".join(lines)
        embed.set_footer(text="XP durch Nachrichten, Voice-Minuten und Reaktionen")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @level.command(name="setup", description="[Admin] Konfiguriere das Level-System")
    async def level_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = LevelSetupView(interaction.guild_id)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @level.command(name="reset", description="[Admin] Setze das Level eines Mitglieds zurück")
    @app_commands.describe(mitglied="Das Mitglied das zurückgesetzt werden soll")
    async def level_reset(self, interaction: discord.Interaction, mitglied: discord.Member):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        server_id = str(interaction.guild_id)
        user_id = str(mitglied.id)

        # Cache‑Eintrag löschen (falls vorhanden) und in DB auf Null setzen
        key = f"{server_id}:{user_id}"
        if key in _user_cache:
            del _user_cache[key]
        _dirty_keys.discard(key)

        try:
            sb = get_supabase()
            existing = _get_user_row(server_id, user_id)
            if existing:
                sb.table("user_levels").update({
                    "xp": 0, "level": 0, "messages": 0,
                    "voice_minutes": 0, "reactions": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("server_id", server_id).eq("user_id", user_id).execute()
                # Nach dem Reset den Cache neu laden (optional, aber sicher)
                _load_into_cache(server_id, user_id)
                await interaction.followup.send(
                    f"✅ Level von **{mitglied.display_name}** wurde zurückgesetzt.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"ℹ️ **{mitglied.display_name}** hat noch keine XP.",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    @level.command(name="xp", description="[Admin] Vergib oder entziehe manuell XP")
    @app_commands.describe(
        mitglied="Das Mitglied",
        menge="XP-Menge (positiv = hinzufügen, negativ = entziehen)",
    )
    async def level_give_xp(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member,
        menge: int,
    ):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        server_id = str(interaction.guild_id)
        user_id = str(mitglied.id)

        old_level, new_level, new_xp = _upsert_xp(server_id, user_id, menge)
        if new_xp < 0:
            # Korrektur: XP nicht unter 0 fallen lassen
            key = f"{server_id}:{user_id}"
            if key in _user_cache:
                _user_cache[key]["xp"] = 0
                _user_cache[key]["level"] = 0
                _dirty_keys.add(key)
            new_xp = 0
            new_level = 0

        direction = "hinzugefügt ➕" if menge >= 0 else "entzogen ➖"
        embed = discord.Embed(
            title="⭐ XP angepasst",
            description=f"**{abs(menge):,} XP** wurden **{mitglied.display_name}** {direction}.",
            color=discord.Color.green() if menge >= 0 else discord.Color.orange(),
        )
        embed.add_field(name="⭐ Neue Gesamt-XP", value=f"{new_xp:,}", inline=True)
        embed.add_field(name="🏆 Neues Level", value=str(new_level), inline=True)

        if new_level > old_level:
            embed.add_field(name="🎉", value=f"Level-Up! {old_level} → {new_level}", inline=False)
            await self._notify_levelup(interaction.guild, mitglied, old_level, new_level, new_xp)

        # Sofort speichern (Admin-Aktion ist wichtig)
        await self._flush_cache_async()

        # Nach dem Speichern den Benutzer aus der DB neu laden, um 100% sicher zu sein
        reloaded = _get_user_row(server_id, user_id)
        if reloaded:
            _user_cache[f"{server_id}:{user_id}"] = reloaded
            _dirty_keys.discard(f"{server_id}:{user_id}")

        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))