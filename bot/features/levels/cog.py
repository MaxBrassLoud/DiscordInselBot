"""
bot/features/levels/cog.py
===========================
Level-System für den Insel Bot.

PUNKTE:
  • Jede Nachricht:                +1 Punkt  (max 1 Punkt pro 60s pro User – Spam-Schutz)
  • Jede Minute im Voice Chat:     +2 Punkte (nur wenn NICHT taub; Stumm ist erlaubt)
  • Jede Reaktion auf Nachrichten: +1 Punkt  (wer reagiert bekommt den Punkt)

LEVEL-FORMEL (exponentiell, kumulativ, auf 50 gerundet):
  XP für Level N = 65 * (N ^ 1.94)

COMMANDS:
  /level info [@user]  – Level-Card anzeigen
  /level top           – Rangliste (Top 10)
  /level setup         – Admin: Level-Kanal konfigurieren
  /level reset [@user] – Admin: Level zurücksetzen
  /level xp <user> <menge> – Admin: XP manuell ändern
  /level debug         – Zeigt, welche User gerade für Voice-XP getrackt werden
"""

from __future__ import annotations

import atexit
import asyncio
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Set, List

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("levels")

# ══════════════════════════════════════════════════════════════════════════════
# KONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MSG_COOLDOWN_SECONDS = 0           # 1 Nachrichten-XP pro User pro Minute
VOICE_XP_PER_MINUTE = 2             # XP pro Minute im Voice (wenn nicht taub)
MSG_XP = 30
REACTION_XP = 5

VOICE_SOLO_XP_ENABLED = True        # True = auch allein im Voice XP sammeln, False = nur mit min. 1 anderen Person

FLUSH_INTERVAL_SECONDS = 600
FLUSH_EVENT_THRESHOLD = 200
VOICE_TRACKING_CACHE_FILE = Path(__file__).resolve().parents[3] / "data" / "voice_tracking_cache.json"


def _delete_voice_tracking_cache_file():
    try:
        VOICE_TRACKING_CACHE_FILE.unlink(missing_ok=True)
        VOICE_TRACKING_CACHE_FILE.with_suffix(".json.tmp").unlink(missing_ok=True)
    except Exception:
        pass


atexit.register(_delete_voice_tracking_cache_file)

# ══════════════════════════════════════════════════════════════════════════════
# LEVEL MATH (NEU: kumulativ, 65 * level^1.94, gerundet auf 50)
# ══════════════════════════════════════════════════════════════════════════════

def xp_for_level(level: int) -> int:
    """Kumulative XP, die benötigt werden, um Level `level` zu erreichen."""
    if level <= 0:
        return 0
    raw = 65 * (level ** 1.94)
    return round(raw / 50) * 50

def level_from_xp(xp: int) -> int:
    """Ermittelt das aktuelle Level aus der Gesamt-XP."""
    if xp <= 0:
        return 0
    # Erste Schätzung über die inverse Funktion
    level = int((xp / 65) ** (1 / 1.94))
    # Wegen Rundung auf 50 korrigieren
    while xp >= xp_for_level(level + 1):
        level += 1
    while level > 0 and xp < xp_for_level(level):
        level -= 1
    return level

def xp_progress(xp: int) -> tuple[int, int, int]:
    """
    Gibt zurück:
        - aktuelles Level
        - XP innerhalb des aktuellen Levels
        - benötigte XP bis zum nächsten Level
    """
    level = level_from_xp(xp)
    current_level_xp = xp_for_level(level)       # kumulative XP bis Level
    next_level_xp = xp_for_level(level + 1)      # kumulative XP bis Level+1
    xp_in_level = xp - current_level_xp
    xp_needed = next_level_xp - current_level_xp
    return level, xp_in_level, xp_needed

def progress_bar(current: int, total: int, length: int = 15) -> str:
    filled = int(length * current / max(total, 1))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}]"

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE & CACHE
# ══════════════════════════════════════════════════════════════════════════════

_user_cache: Dict[str, dict] = {}
_dirty_keys: Set[str] = set()
_flush_lock = asyncio.Lock()

def _get_user_row(server_id: str, user_id: str) -> Optional[dict]:
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
    key = f"{server_id}:{user_id}"
    row = _get_user_row(server_id, user_id)
    if row:
        _user_cache[key] = row
        _dirty_keys.discard(key)
    return _user_cache.get(key)

def _get_cached_user(server_id: str, user_id: str) -> Optional[dict]:
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
    key = f"{server_id}:{user_id}"
    now = datetime.now(timezone.utc).isoformat()
    current = _get_cached_user(server_id, user_id)

    if not current:
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
        return 0, new_level, new_xp

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
    sb = get_supabase()
    existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
    data = {"guild_id": server_id, "level_channel_id": channel_id, "levels_enabled": enabled}
    if existing.data:
        sb.table("settings").update(data).eq("guild_id", server_id).execute()
    else:
        sb.table("settings").insert(data).execute()

def _get_leaderboard(server_id: str, limit: int = 10) -> List[dict]:
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
# EMBEDS
# ══════════════════════════════════════════════════════════════════════════════

def _build_levelup_embed(member: discord.Member, old_level: int, new_level: int, total_xp: int) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 Level Up!",
        description=f"**{member.display_name}** ist von **Level {old_level}** auf **Level {new_level}** aufgestiegen! 🚀",
        color=discord.Color.from_rgb(74, 222, 128),
    )
    _, xp_in, xp_next = xp_progress(total_xp)
    bar = progress_bar(xp_in, xp_next)
    embed.add_field(name="📊 Fortschritt", value=f"`{bar}` {xp_in}/{xp_next} XP", inline=False)
    embed.add_field(name="⭐ Gesamt-XP", value=f"{total_xp:,}", inline=True)
    embed.add_field(name="🏆 Neues Level", value=str(new_level), inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"Server: {member.guild.name}")
    embed.timestamp = discord.utils.utcnow()
    return embed

def _build_level_card(member: discord.Member, row: dict, rank: int) -> discord.Embed:
    xp = row.get("xp", 0)
    level = row.get("level", 0)
    messages = row.get("messages", 0)
    voice_min = row.get("voice_minutes", 0)
    reactions = row.get("reactions", 0)

    _, xp_in, xp_next = xp_progress(xp)
    bar = progress_bar(xp_in, xp_next)

    embed = discord.Embed(title=f"📊 Level-Info – {member.display_name}", color=discord.Color.from_rgb(74, 222, 128))
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏆 Level", value=str(level), inline=True)
    embed.add_field(name="📍 Rang", value=f"#{rank}", inline=True)
    embed.add_field(name="⭐ Gesamt-XP", value=f"{xp:,}", inline=True)
    embed.add_field(name="📈 Fortschritt", value=f"`{bar}` {xp_in:,} / {xp_next:,} XP", inline=False)
    embed.add_field(name="💬 Nachrichten", value=f"{messages:,}", inline=True)
    embed.add_field(name="🎙️ Voice-Minuten", value=f"{voice_min:,}", inline=True)
    embed.add_field(name="😄 Reaktionen", value=f"{reactions:,}", inline=True)
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
        e = discord.Embed(title="⚙️ Level-System Setup", color=discord.Color.from_rgb(74, 222, 128))
        e.add_field(name="📢 Level-Update Kanal", value=f"<#{self.channel_id}>" if self.channel_id else "*Nicht gesetzt*", inline=False)
        e.add_field(name="🔧 System", value="✅ Aktiv" if self.enabled else "❌ Deaktiviert", inline=True)
        solo_text = "✅ Alleine XP" if VOICE_SOLO_XP_ENABLED else "👥 Nur mit anderen"
        e.add_field(name="📊 Punkte-Übersicht",
                    value=(f"💬 Nachricht → +1 XP (max 1×/Minute)\n"
                           f"🎙️ Voice-Minute → +2 XP (nicht taub) [{solo_text}]\n"
                           f"😄 Reaktion → +1 XP"),
                    inline=False)
        return e

    def _rebuild(self):
        self.clear_items()
        ch_sel = discord.ui.ChannelSelect(placeholder="📢 Kanal wählen…", min_values=0, max_values=1,
                                          channel_types=[discord.ChannelType.text], row=0)
        ch_sel.callback = self._on_channel
        self.add_item(ch_sel)
        toggle_btn = discord.ui.Button(label=f"System: {'AN ✅' if self.enabled else 'AUS ❌'}",
                                       style=discord.ButtonStyle.success if self.enabled else discord.ButtonStyle.secondary,
                                       row=1)
        toggle_btn.callback = self._on_toggle
        self.add_item(toggle_btn)
        save_btn = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.primary, row=1)
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
        self._msg_cooldown: Dict[str, datetime] = {}
        self._voice_joined: Dict[str, datetime] = self._load_voice_tracking_cache()
        self._event_counter = 0
        self._initial_voice_sync_done = False
        self.flush_cache_task.start()
        self.voice_xp_task.start()

    def cog_unload(self):
        self.flush_cache_task.cancel()
        self.voice_xp_task.cancel()
        self._flush_cache_sync()
        self._delete_voice_tracking_cache()

    # Voice tracking cache --------------------------------------------------
    def _load_voice_tracking_cache(self) -> Dict[str, datetime]:
        if not VOICE_TRACKING_CACHE_FILE.exists():
            return {}
        try:
            with VOICE_TRACKING_CACHE_FILE.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
            sessions = {
                key: datetime.fromisoformat(value)
                for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str)
            }
            logger.info(f"[voice] Voice-Tracking-Cache geladen: {len(sessions)} Sessions")
            return sessions
        except Exception as e:
            logger.warning(f"[voice] Voice-Tracking-Cache konnte nicht geladen werden: {e}")
            return {}

    def _save_voice_tracking_cache(self):
        try:
            VOICE_TRACKING_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = VOICE_TRACKING_CACHE_FILE.with_suffix(".json.tmp")
            payload = {
                key: joined_at.isoformat()
                for key, joined_at in self._voice_joined.items()
            }
            with tmp_file.open("w", encoding="utf-8") as fp:
                json.dump(payload, fp)
            tmp_file.replace(VOICE_TRACKING_CACHE_FILE)
        except Exception as e:
            logger.warning(f"[voice] Voice-Tracking-Cache konnte nicht gespeichert werden: {e}")

    def _delete_voice_tracking_cache(self):
        try:
            _delete_voice_tracking_cache_file()
        except Exception as e:
            logger.warning(f"[voice] Voice-Tracking-Cache konnte nicht gelöscht werden: {e}")

    def _start_voice_tracking(self, key: str, joined_at: Optional[datetime] = None):
        self._voice_joined[key] = joined_at or datetime.now(timezone.utc)
        self._save_voice_tracking_cache()

    def _stop_voice_tracking(self, key: str) -> Optional[datetime]:
        removed = self._voice_joined.pop(key, None)
        if removed:
            self._save_voice_tracking_cache()
        return removed

    # Flush -----------------------------------------------------------------
    def _flush_cache_sync(self):
        if not _dirty_keys:
            return
        records_to_upsert = []
        for key in list(_dirty_keys):
            record = _user_cache.get(key)
            if not record:
                _dirty_keys.discard(key)
                continue
            clean = {
                "user_id": record["user_id"],
                "server_id": record["server_id"],
                "xp": record["xp"],
                "level": record["level"],
                "messages": record["messages"],
                "voice_minutes": record["voice_minutes"],
                "reactions": record["reactions"],
                "updated_at": record["updated_at"],
            }
            if record.get("last_msg_at"):
                clean["last_msg_at"] = record["last_msg_at"]
            records_to_upsert.append(clean)
        if records_to_upsert:
            try:
                get_supabase().table("user_levels").upsert(
                    records_to_upsert, on_conflict="user_id,server_id"
                ).execute()
                _dirty_keys.clear()
                logger.info(f"[levels] Flushed {len(records_to_upsert)} records")
            except Exception as e:
                logger.error(f"[levels] Flush error: {e}")

    async def _flush_cache_async(self):
        async with _flush_lock:
            await self.bot.loop.run_in_executor(None, self._flush_cache_sync)

    @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
    async def flush_cache_task(self):
        await self._flush_cache_async()

    @flush_cache_task.before_loop
    async def before_flush(self):
        await self.bot.wait_until_ready()

    # Level-Up Notify -------------------------------------------------------
    async def _notify_levelup(self, guild, member, old_level, new_level, total_xp):
        channel_id = _get_level_channel(str(guild.id))
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if not channel:
            return
        embed = _build_levelup_embed(member, old_level, new_level, total_xp)
        try:
            await channel.send(embed=embed)
        except Exception as e:
            logger.error(f"[levels] notify error: {e}")

    # Cooldown --------------------------------------------------------------
    def _check_msg_cooldown(self, server_id: str, user_id: str) -> bool:
        key = f"{server_id}:{user_id}"
        now = datetime.now(timezone.utc)
        last = self._msg_cooldown.get(key)
        if last and (now - last).total_seconds() < MSG_COOLDOWN_SECONDS:
            return False
        self._msg_cooldown[key] = now
        return True

    # Event Listener: Messages ----------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not self._check_msg_cooldown(str(message.guild.id), str(message.author.id)):
            return
        old, new, xp = _upsert_xp(str(message.guild.id), str(message.author.id), MSG_XP, msg_delta=1)
        if new > old:
            await self._notify_levelup(message.guild, message.author, old, new, xp)
        self._event_counter += 1
        if self._event_counter >= FLUSH_EVENT_THRESHOLD:
            self._event_counter = 0
            await self._flush_cache_async()

    # Event Listener: Reactions ---------------------------------------------
    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User):
        if user.bot or not reaction.message.guild:
            return
        old, new, xp = _upsert_xp(str(reaction.message.guild.id), str(user.id), REACTION_XP, reaction_delta=1)
        if new > old:
            member = reaction.message.guild.get_member(user.id)
            if member:
                await self._notify_levelup(reaction.message.guild, member, old, new, xp)
        self._event_counter += 1
        if self._event_counter >= FLUSH_EVENT_THRESHOLD:
            self._event_counter = 0
            await self._flush_cache_async()

    # Voice State Tracking --------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        key = f"{member.guild.id}:{member.id}"

        # Voice verlassen
        if before.channel and not after.channel:
            removed = self._stop_voice_tracking(key)
            if removed:
                logger.info(f"[voice] {member} hat Voice verlassen → Tracking beendet.")
            return

        # Voice betreten
        if after.channel and not before.channel:
            if after.deaf or after.self_deaf:
                logger.info(f"[voice] {member} betritt Voice, ist aber TAUB → kein Tracking.")
            else:
                self._start_voice_tracking(key)
                logger.info(f"[voice] {member} betritt Voice (nicht taub) → Tracking gestartet.")
            return

        # Status-Änderung innerhalb eines Channels
        if after.channel and before.channel:
            was_deaf = before.deaf or before.self_deaf
            is_deaf = after.deaf or after.self_deaf
            if not was_deaf and is_deaf:
                self._stop_voice_tracking(key)
                logger.info(f"[voice] {member} wurde taub → Tracking gestoppt.")
            elif was_deaf and not is_deaf:
                self._start_voice_tracking(key)
                logger.info(f"[voice] {member} nicht mehr taub → Tracking neu gestartet.")

    # Initialer Voice-Sync beim Bot-Start -----------------------------------
    @commands.Cog.listener()
    async def on_ready(self):
        if self._initial_voice_sync_done:
            return
        self._initial_voice_sync_done = True
        logger.info("[voice] Führe initialen Voice-Sync durch...")
        count = 0
        for guild in self.bot.guilds:
            for vc in guild.voice_channels:
                # Falls Solo-XP deaktiviert ist, nur mit anderen tracken
                if not VOICE_SOLO_XP_ENABLED:
                    non_bot = [m for m in vc.members if not m.bot]
                    if len(non_bot) < 2:
                        continue

                for member in vc.members:
                    if member.bot:
                        continue
                    voice = member.voice
                    if voice and not (voice.deaf or voice.self_deaf):
                        # AFK-Channel ausschließen
                        if guild.afk_channel and voice.channel.id == guild.afk_channel.id:
                            continue
                        key = f"{guild.id}:{member.id}"
                        if key not in self._voice_joined:
                            self._start_voice_tracking(key)
                            count += 1
        logger.info(f"[voice] Initialer Sync abgeschlossen – {count} User werden getrackt.")

    # Voice XP Loop (jede Minute) -------------------------------------------
    @tasks.loop(seconds=60)
    async def voice_xp_task(self):
        now = datetime.now(timezone.utc)
        logger.debug(f"[voice] Task läuft. {len(self._voice_joined)} User im Tracking.")

        for key, joined_at in list(self._voice_joined.items()):
            if (now - joined_at).total_seconds() < 55:
                continue

            try:
                server_id, user_id = key.split(":", 1)
                guild = self.bot.get_guild(int(server_id))
                if not guild:
                    self._stop_voice_tracking(key)
                    continue
                member = guild.get_member(int(user_id))
                if not member or not member.voice or not member.voice.channel:
                    self._stop_voice_tracking(key)
                    continue

                voice = member.voice
                if guild.afk_channel and voice.channel.id == guild.afk_channel.id:
                    continue
                if voice.deaf or voice.self_deaf:
                    self._stop_voice_tracking(key)
                    continue

                if not VOICE_SOLO_XP_ENABLED:
                    non_bot = [m for m in voice.channel.members if not m.bot]
                    if len(non_bot) < 2:
                        logger.debug(f"[voice] {member} allein im Kanal → keine XP (Solo-XP deaktiviert).")
                        continue

                old, new, xp = _upsert_xp(server_id, user_id, VOICE_XP_PER_MINUTE, voice_delta=1)
                logger.info(f"[voice] +{VOICE_XP_PER_MINUTE} XP an {member} (Level {old}→{new})")

                if new > old:
                    await self._notify_levelup(guild, member, old, new, xp)

                self._event_counter += 1
                if self._event_counter >= FLUSH_EVENT_THRESHOLD:
                    self._event_counter = 0
                    await self._flush_cache_async()

            except Exception as e:
                logger.error(f"[voice] Fehler bei {key}: {e}")

    @voice_xp_task.before_loop
    async def before_voice_xp(self):
        await self.bot.wait_until_ready()

    # Hilfsfunktion: Sichere Ephemeral-Antwort --------------------------------
    async def _safe_defer(self, interaction: discord.Interaction, ephemeral: bool = True):
        """
        Defert die Interaktion sicher und fängt ab, wenn sie bereits abgelaufen ist.
        Gibt False zurück, wenn die Interaktion nicht mehr gültig ist.
        """
        try:
            await interaction.response.defer(ephemeral=ephemeral)
            return True
        except (discord.NotFound, discord.HTTPException):
            return False

    # ═══════════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ═══════════════════════════════════════════════════════════════════════════

    level = app_commands.Group(name="level", description="Level-System")

    @level.command(name="info")
    @app_commands.describe(mitglied="Mitglied anzeigen")
    async def level_info(self, interaction: discord.Interaction, mitglied: Optional[discord.Member] = None):
        if not await self._safe_defer(interaction, ephemeral=True):
            return  # Interaktion existiert nicht mehr

        await self._flush_cache_async()
        target = mitglied or interaction.user
        row = _get_cached_user(str(interaction.guild_id), str(target.id)) or _get_user_row(str(interaction.guild_id), str(target.id))
        if not row:
            msg = "Du hast noch keine XP." if target == interaction.user else f"{target.display_name} hat noch keine XP."
            await interaction.followup.send(msg, ephemeral=True)
            return
        rank = _get_rank(str(interaction.guild_id), str(target.id))
        embed = _build_level_card(target, row, rank)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @level.command(name="top")
    async def level_top(self, interaction: discord.Interaction):
        if not await self._safe_defer(interaction, ephemeral=True):
            return

        await self._flush_cache_async()
        rows = _get_leaderboard(str(interaction.guild_id))
        if not rows:
            await interaction.followup.send("Noch keine Daten.", ephemeral=True)
            return
        embed = discord.Embed(title=f"🏆 Rangliste – {interaction.guild.name}", color=discord.Color.from_rgb(74, 222, 128))
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows, 1):
            member = interaction.guild.get_member(int(row["user_id"]))
            name = member.display_name if member else f"<@{row['user_id']}>"
            medal = medals[i - 1] if i <= 3 else f"**#{i}**"
            lines.append(f"{medal} **{name}** — Level {row['level']} · {row['xp']:,} XP")
        embed.description = "\n".join(lines)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @level.command(name="setup")
    async def level_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = LevelSetupView(interaction.guild_id)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @level.command(name="reset")
    @app_commands.describe(mitglied="User zurücksetzen")
    async def level_reset(self, interaction: discord.Interaction, mitglied: discord.Member):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not await self._safe_defer(interaction, ephemeral=True):
            return
        server_id = str(interaction.guild_id)
        user_id = str(mitglied.id)
        key = f"{server_id}:{user_id}"
        _user_cache.pop(key, None)
        _dirty_keys.discard(key)
        try:
            get_supabase().table("user_levels").update({
                "xp": 0, "level": 0, "messages": 0, "voice_minutes": 0, "reactions": 0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("server_id", server_id).eq("user_id", user_id).execute()
            await interaction.followup.send(f"✅ Level von {mitglied.display_name} zurückgesetzt.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    @level.command(name="xp")
    @app_commands.describe(mitglied="User", menge="Menge (+/-)")
    async def level_xp(self, interaction: discord.Interaction, mitglied: discord.Member, menge: int):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not await self._safe_defer(interaction, ephemeral=True):
            return
        old, new, xp = _upsert_xp(str(interaction.guild_id), str(mitglied.id), menge)
        direction = "hinzugefügt" if menge >= 0 else "entzogen"
        embed = discord.Embed(title="⭐ XP angepasst",
                              description=f"{abs(menge)} XP {direction} bei {mitglied.display_name}.",
                              color=discord.Color.green() if menge >= 0 else discord.Color.orange())
        embed.add_field(name="Neue XP", value=f"{xp:,}", inline=True)
        embed.add_field(name="Level", value=str(new), inline=True)
        if new > old:
            embed.add_field(name="🎉", value=f"Level Up! {old} → {new}", inline=False)
            await self._notify_levelup(interaction.guild, mitglied, old, new, xp)
        await self._flush_cache_async()
        await interaction.followup.send(embed=embed, ephemeral=True)

    # DEBUG COMMAND ---------------------------------------------------------
    @level.command(name="debug", description="[Admin] Zeigt aktuelles Voice-Tracking")
    async def level_debug(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not await self._safe_defer(interaction, ephemeral=True):
            return
        if not self._voice_joined:
            await interaction.followup.send("🔎 Keine User im Voice-Tracking.", ephemeral=True)
            return
        lines = []
        now = datetime.now(timezone.utc)
        for key, joined in self._voice_joined.items():
            server_id, user_id = key.split(":")
            member = interaction.guild.get_member(int(user_id)) if interaction.guild.id == int(server_id) else None
            name = member.display_name if member else f"Unbekannt ({user_id})"
            seconds = int((now - joined).total_seconds())
            lines.append(f"• **{name}** – seit {seconds}s im Voice (nicht taub)")
        embed = discord.Embed(title="🎙️ Voice-Tracking Debug", description="\n".join(lines),
                              color=discord.Color.blue())
        await interaction.followup.send(embed=embed, ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(LevelsCog(bot))