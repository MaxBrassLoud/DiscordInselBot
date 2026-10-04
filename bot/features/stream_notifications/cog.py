"""
bot/features/stream_notifications/cog.py
==========================================
Benachrichtigt einen Discord-Kanal, wenn ein registrierter YouTube-Kanal
ein neues Video hochlädt oder ein registrierter Twitch-User live geht.

Jeder Account hat seinen eigenen Benachrichtigungskanal und optional eine
Rolle die gepingt wird.

Zusätzlich:
- Verlinkte Accounts bekommen automatisch eine Streamer-Rolle.
- Wenn der Streamer live geht, bekommt er eine Live-Rolle + 🔴 vor dem Namen.
- Beim Bot-Neustart wird der Live-Status re-evaluiert.
- Ein User kann mehrere Accounts (YT + Twitch) verlinken.

Supabase SQL (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS stream_notifications_config (
        id                  BIGSERIAL PRIMARY KEY,
        guild_id            TEXT NOT NULL UNIQUE,
        channel_id          TEXT,
        streamer_role_id    TEXT,
        live_role_id        TEXT,
        enabled             BOOLEAN DEFAULT TRUE,
        created_at          TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS stream_notifications_accounts (
        id                      BIGSERIAL PRIMARY KEY,
        guild_id                TEXT NOT NULL,
        platform                TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
        account_id              TEXT NOT NULL,
        account_name            TEXT,
        channel_id              TEXT,
        role_id                 TEXT,
        discord_user_id         TEXT,
        last_known_id           TEXT,
        is_live                 BOOLEAN DEFAULT FALSE,
        live_role_given         BOOLEAN DEFAULT FALSE,
        added_at                TIMESTAMPTZ DEFAULT now(),
        UNIQUE (guild_id, platform, account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_guild
        ON stream_notifications_accounts (guild_id);
    CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_user
        ON stream_notifications_accounts (guild_id, discord_user_id);

    -- Falls die Tabellen schon existieren:
    ALTER TABLE stream_notifications_config
        ADD COLUMN IF NOT EXISTS streamer_role_id TEXT,
        ADD COLUMN IF NOT EXISTS live_role_id     TEXT;
    ALTER TABLE stream_notifications_accounts
        ADD COLUMN IF NOT EXISTS discord_user_id TEXT,
        ADD COLUMN IF NOT EXISTS live_role_given BOOLEAN DEFAULT FALSE;

ENV-Variablen:
    YOUTUBE_API_KEY       – Google API Key (YouTube Data API v3)
    TWITCH_CLIENT_ID      – Twitch Helix Client ID
    TWITCH_CLIENT_SECRET  – Twitch Helix Client Secret
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("stream_notifications")

TWITCH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
TWITCH_API_BASE = "https://api.twitch.tv/helix"

YT_API_KEY = os.getenv("YOUTUBE_API_KEY")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

LIVE_EMOJI = "🔴"
MAX_EMBED_FIELDS = 25

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS – CONFIG
# ══════════════════════════════════════════════════════════════════════════════


def _get_config(guild_id: str) -> dict | None:
    try:
        r = get_supabase().table("stream_notifications_config") \
            .select("*").eq("guild_id", guild_id).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[stream_notif] _get_config: {e}")
        return None


def _set_config(guild_id: str, **fields):
    """Upsert der Config in einem Call (vermeidet Race Conditions)."""
    try:
        get_supabase().table("stream_notifications_config").upsert(
            {"guild_id": guild_id, **fields},
            on_conflict="guild_id",
        ).execute()
    except Exception as e:
        logger.error(f"[stream_notif] _set_config: {e}")


def _toggle_config(guild_id: str, enabled: bool):
    try:
        get_supabase().table("stream_notifications_config") \
            .update({"enabled": enabled}).eq("guild_id", guild_id).execute()
    except Exception as e:
        logger.error(f"[stream_notif] _toggle_config: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS – ACCOUNTS
# ══════════════════════════════════════════════════════════════════════════════


def _get_accounts(guild_id: str) -> list[dict]:
    try:
        r = get_supabase().table("stream_notifications_accounts") \
            .select("*").eq("guild_id", guild_id) \
            .order("added_at", desc=False).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[stream_notif] _get_accounts: {e}")
        return []


def _get_accounts_for_user(guild_id: str, discord_user_id: str) -> list[dict]:
    """Alle mit dem User verlinkten Accounts."""
    try:
        r = get_supabase().table("stream_notifications_accounts") \
            .select("*").eq("guild_id", guild_id) \
            .eq("discord_user_id", discord_user_id).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[stream_notif] _get_accounts_for_user: {e}")
        return []


def _find_account_by_name_or_id(guild_id: str, query: str) -> dict | None:
    """Sucht Account per ID oder Name (case-insensitive)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    for acc in _get_accounts(guild_id):
        if acc["account_id"].lower() == q:
            return acc
        if (acc.get("account_name") or "").lower() == q:
            return acc
    return None


def _add_account(guild_id: str, platform: str, account_id: str,
                 account_name: str | None = None, channel_id: str | None = None,
                 role_id: str | None = None, last_known_id: str | None = None,
                 discord_user_id: str | None = None) -> dict | None:
    """Upsert – verhindert Race Conditions und doppelte Einträge."""
    try:
        r = get_supabase().table("stream_notifications_accounts").upsert({
            "guild_id": guild_id,
            "platform": platform,
            "account_id": account_id,
            "account_name": account_name,
            "channel_id": channel_id,
            "role_id": role_id,
            "last_known_id": last_known_id,
            "discord_user_id": discord_user_id,
        }, on_conflict="guild_id,platform,account_id").execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[stream_notif] _add_account: {e}")
        return None


def _remove_account(account_db_id: int):
    try:
        get_supabase().table("stream_notifications_accounts") \
            .delete().eq("id", account_db_id).execute()
    except Exception as e:
        logger.error(f"[stream_notif] _remove_account: {e}")


def _update_account(account_db_id: int, **kwargs):
    try:
        get_supabase().table("stream_notifications_accounts") \
            .update(kwargs).eq("id", account_db_id).execute()
    except Exception as e:
        logger.error(f"[stream_notif] _update_account: {e}")


def _resolve_channel(guild: discord.Guild, acc: dict) -> discord.TextChannel | None:
    """Findet den Kanal: erst Account-eigener, dann globaler Fallback."""
    if acc.get("channel_id"):
        ch = guild.get_channel(int(acc["channel_id"]))
        if ch:
            return ch
    cfg = _get_config(str(guild.id))
    if cfg and cfg.get("channel_id"):
        ch = guild.get_channel(int(cfg["channel_id"]))
        if ch:
            return ch
    return None


def _env_status() -> tuple[bool, bool]:
    """(youtube_ok, twitch_ok)"""
    return bool(YT_API_KEY), bool(TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET)


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS – NICK / ROLES
# ══════════════════════════════════════════════════════════════════════════════


def _strip_live_prefix(nick: str | None) -> str:
    if not nick:
        return ""
    return re.sub(r"^\s*🔴\s*", "", nick).strip()


def _has_live_prefix(nick: str | None) -> bool:
    return bool(nick and re.match(r"^\s*🔴", nick))


async def _apply_streamer_role(member: discord.Member, guild_id: str) -> str | None:
    """Vergibt die Streamer-Rolle. Gibt Warnung zurück, falls Fehler."""
    cfg = _get_config(guild_id)
    if not cfg or not cfg.get("streamer_role_id"):
        return None
    role = member.guild.get_role(int(cfg["streamer_role_id"]))
    if not role:
        return "Streamer-Rolle existiert nicht mehr"
    if role in member.roles:
        return None
    try:
        await member.add_roles(role, reason="Stream-Account verlinkt")
        return None
    except discord.Forbidden:
        logger.warning(f"[roles] Keine Berechtigung für Streamer-Rolle in {member.guild.name}")
        return "Keine Berechtigung für Streamer-Rolle"


async def _remove_streamer_role(member: discord.Member, guild_id: str):
    cfg = _get_config(guild_id)
    if not cfg or not cfg.get("streamer_role_id"):
        return
    role = member.guild.get_role(int(cfg["streamer_role_id"]))
    if role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Stream-Account entlinkt")
        except discord.Forbidden:
            logger.warning(f"[roles] Keine Berechtigung zum Entfernen der Streamer-Rolle in {member.guild.name}")


async def _set_live_state(member: discord.Member, guild_id: str, live: bool):
    """Vergibt/entfernt Live-Rolle und 🔴-Präfix im Nicknamen."""
    cfg = _get_config(guild_id)
    if not cfg:
        return

    live_role = None
    if cfg.get("live_role_id"):
        live_role = member.guild.get_role(int(cfg["live_role_id"]))

    if live:
        if live_role and live_role not in member.roles:
            try:
                await member.add_roles(live_role, reason="Streamer ist live")
            except discord.Forbidden:
                logger.warning(f"[roles] Keine Berechtigung für Live-Rolle in {member.guild.name}")

        current = member.nick or member.display_name
        if not _has_live_prefix(current):
            new_nick = f"{LIVE_EMOJI} {current}"[:32]
            try:
                await member.edit(nick=new_nick, reason="Streamer ist live")
            except discord.Forbidden:
                pass
    else:
        if live_role and live_role in member.roles:
            try:
                await member.remove_roles(live_role, reason="Streamer ist offline")
            except discord.Forbidden:
                logger.warning(f"[roles] Keine Berechtigung zum Entfernen der Live-Rolle in {member.guild.name}")

        if _has_live_prefix(member.nick):
            clean = _strip_live_prefix(member.nick)
            try:
                await member.edit(nick=clean or None, reason="Streamer ist offline")
            except discord.Forbidden:
                pass


async def _account_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete für Account-Namen/IDs."""
    if not interaction.guild_id:
        return []
    accounts = _get_accounts(str(interaction.guild_id))
    cur = (current or "").lower()
    out: list[app_commands.Choice[str]] = []
    for acc in accounts:
        name = acc.get("account_name") or acc["account_id"]
        platform = acc["platform"].capitalize()
        label = f"{name} ({platform})"
        if cur and cur not in label.lower() and cur not in acc["account_id"].lower():
            continue
        out.append(app_commands.Choice(name=label[:100], value=acc["account_id"]))
        if len(out) >= 25:
            break
    return out


# ══════════════════════════════════════════════════════════════════════════════
# API CALLS
# ══════════════════════════════════════════════════════════════════════════════


async def _fetch_youtube_channel_name(channel_id: str) -> str | None:
    if not YT_API_KEY:
        return None
    url = f"{YOUTUBE_API_BASE}/channels"
    params = {"part": "snippet", "id": channel_id, "key": YT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                return items[0]["snippet"]["title"] if items else None
    except Exception as e:
        logger.warning(f"[yt] channel name fetch: {e}")
        return None


async def _resolve_youtube_handle(handle: str) -> str | None:
    """Resolve @handle → Channel ID. Versucht forHandle, dann Search-API."""
    if not YT_API_KEY:
        return None

    clean = handle.lstrip("@")

    url = f"{YOUTUBE_API_BASE}/channels"
    params = {"part": "id", "forHandle": f"@{clean}", "key": YT_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logger.warning(f"[yt] forHandle {resp.status}: {data}")
                    return None
                items = data.get("items", [])
                if items:
                    return items[0]["id"]
                logger.info(f"[yt] forHandle @{clean}: keine Items, versuche Search")
    except Exception as e:
        logger.warning(f"[yt] forHandle resolve: {e}")

    search_url = f"{YOUTUBE_API_BASE}/search"
    search_params = {
        "part": "snippet",
        "q": clean,
        "type": "channel",
        "maxResults": 5,
        "key": YT_API_KEY,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(search_url, params=search_params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                for item in items:
                    channel_title = item.get("snippet", {}).get("title", "").lower()
                    channel_id = item.get("id", {}).get("channelId", "")
                    if channel_title == clean.lower() and channel_id:
                        return channel_id
                if items:
                    return items[0].get("id", {}).get("channelId")
    except Exception as e:
        logger.warning(f"[yt] search resolve: {e}")

    return None


def _parse_youtube_url(url: str) -> dict | None:
    url = url.strip()
    if url.startswith("@") and not url.startswith("http"):
        return {"type": "handle", "value": url}
    if not url.startswith("http") and re.match(r"^UC[\w-]{22}$", url):
        return {"type": "id", "value": url}
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().replace("www.", "")
    if host not in ("youtube.com", "m.youtube.com", "youtu.be"):
        return None
    path = parsed.path.strip("/")
    m = re.match(r"^channel/(UC[\w-]{22})$", path)
    if m:
        return {"type": "id", "value": m.group(1)}
    m = re.match(r"^@([\w.-]+)$", path)
    if m:
        return {"type": "handle", "value": f"@{m.group(1)}"}
    m = re.match(r"^c/([\w.-]+)$", path)
    if m:
        return {"type": "handle", "value": f"@{m.group(1)}"}
    m = re.match(r"^user/([\w.-]+)$", path)
    if m:
        return {"type": "handle", "value": f"@{m.group(1)}"}
    return None


def _parse_twitch_url(url: str) -> str | None:
    url = url.strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().replace("www.", "")
    if host == "twitch.tv":
        login = parsed.path.strip("/").split("/")[0]
        if login:
            return login.lower()
    if not url.startswith("http") and re.match(r"^[\w]+$", url):
        return url.lower()
    return None


async def _fetch_latest_youtube_video(channel_id: str) -> dict | None:
    """Fetch uploads through the channel playlist (2 low-quota calls)."""
    if not YT_API_KEY:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{YOUTUBE_API_BASE}/channels", params={
                "part": "contentDetails,snippet", "id": channel_id, "key": YT_API_KEY,
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                if not items:
                    return None
                channel = items[0]
                uploads_id = channel.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
                if not uploads_id:
                    return None
            async with session.get(f"{YOUTUBE_API_BASE}/playlistItems", params={
                "part": "snippet,contentDetails", "playlistId": uploads_id,
                "maxResults": 1, "key": YT_API_KEY,
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                items = (await resp.json()).get("items", [])
                if not items:
                    return None
                video = items[0]
                video_id = video.get("contentDetails", {}).get("videoId")
                snippet = video.get("snippet", {})
                if not video_id:
                    return None
                return {
                    "video_id": video_id,
                    "title": snippet.get("title", "Neues Video"),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "channel_name": snippet.get("channelTitle", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                }
    except Exception as e:
        logger.warning(f"[yt] latest video fetch: {e}")
        return None


_twitch_oauth_token: str | None = None
_twitch_oauth_expires_at: datetime | None = None


def _invalidate_twitch_token() -> None:
    global _twitch_oauth_token, _twitch_oauth_expires_at
    _twitch_oauth_token = None
    _twitch_oauth_expires_at = None


async def _get_twitch_token() -> str | None:
    global _twitch_oauth_token, _twitch_oauth_expires_at
    if _twitch_oauth_token and _twitch_oauth_expires_at and _twitch_oauth_expires_at > datetime.now(timezone.utc):
        return _twitch_oauth_token
    if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(TWITCH_TOKEN_URL, params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials",
            }, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                _twitch_oauth_token = data.get("access_token")
                _twitch_oauth_expires_at = datetime.now(timezone.utc) + timedelta(
                    seconds=max(0, int(data.get("expires_in", 0)) - 60)
                )
                return _twitch_oauth_token
    except Exception as e:
        logger.warning(f"[twitch] token fetch: {e}")
        return None


async def _resolve_twitch_login(login: str) -> dict | None:
    token = await _get_twitch_token()
    if not token:
        return None
    url = f"{TWITCH_API_BASE}/users"
    headers = {"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"login": login.lower()},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401:
                    _invalidate_twitch_token()
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()
                users = data.get("data", [])
                return users[0] if users else None
    except Exception as e:
        logger.warning(f"[twitch] resolve login: {e}")
        return None


async def _check_twitch_live(user_id: str) -> dict | None:
    token = await _get_twitch_token()
    if not token:
        return None
    url = f"{TWITCH_API_BASE}/streams"
    headers = {"Client-Id": TWITCH_CLIENT_ID, "Authorization": f"Bearer {token}"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, params={"user_id": user_id},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 401:
                    _invalidate_twitch_token()
                    return None
                if resp.status != 200:
                    return None
                data = await resp.json()
                streams = data.get("data", [])
                if streams:
                    s = streams[0]
                    thumb = s.get("thumbnail_url", "")
                    thumb = thumb.replace("{width}", "440").replace("{height}", "248") if thumb else ""
                    return {
                        "stream_id": s["id"],
                        "title": s.get("title", ""),
                        "game": s.get("game_name", ""),
                        "viewer_count": s.get("viewer_count", 0),
                        "thumbnail": thumb,
                        "url": f"https://twitch.tv/{s.get('user_login', '')}",
                        "login": s.get("user_login", ""),
                    }
                return None
    except Exception as e:
        logger.warning(f"[twitch] live check: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# BACKGROUND LOOP
# ══════════════════════════════════════════════════════════════════════════════


class StreamNotificationLoop(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._loop_lock = asyncio.Lock()
        self.startup_live_check.start()
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()
        self.startup_live_check.cancel()

    # ────────────────────────────── Startup ──────────────────────────────

    @tasks.loop(count=1)
    async def startup_live_check(self):
        """Beim Bot-Start: Live-Status aller Twitch-Accounts prüfen +
        Streamer-Rolle für alle verlinkten Accounts sicherstellen."""
        await self.bot.wait_until_ready()
        try:
            sb = get_supabase()
            configs = sb.table("stream_notifications_config") \
                .select("*").eq("enabled", True).execute().data or []

            for cfg in configs:
                guild = self.bot.get_guild(int(cfg["guild_id"]))
                if not guild:
                    continue
                accounts = _get_accounts(cfg["guild_id"])

                # 1) Streamer-Rolle für alle verlinkten Accounts sicherstellen
                for acc in accounts:
                    if not acc.get("discord_user_id"):
                        continue
                    member = guild.get_member(int(acc["discord_user_id"]))
                    if member:
                        await _apply_streamer_role(member, str(guild.id))

                # 2) Live-Status re-evaluieren (nur Twitch)
                for acc in accounts:
                    if acc["platform"] != "twitch" or not acc.get("discord_user_id"):
                        continue
                    member = guild.get_member(int(acc["discord_user_id"]))
                    if not member:
                        continue

                    stream = await _check_twitch_live(acc["account_id"])
                    actually_live = stream is not None
                    db_says_live = bool(acc.get("is_live", False))

                    if actually_live != db_says_live:
                        _update_account(
                            acc["id"],
                            is_live=actually_live,
                            live_role_given=actually_live,
                        )
                        logger.info(
                            f"[startup] {acc.get('account_name')} -> "
                            f"{'LIVE' if actually_live else 'OFFLINE'} korrigiert"
                        )
                    # Sync immer ausführen
                    await _set_live_state(member, str(guild.id), actually_live)
        except Exception as e:
            logger.error(f"[startup_live_check] {e}")

    @startup_live_check.before_loop
    async def before_startup_live_check(self):
        await self.bot.wait_until_ready()

    # ────────────────────────────── Loop ──────────────────────────────

    @tasks.loop(minutes=5)
    async def check_loop(self):
        await self.bot.wait_until_ready()
        if self._loop_lock.locked():
            logger.warning("[stream_notif] Vorheriger Loop läuft noch – skip")
            return
        async with self._loop_lock:
            await self._run_check()

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

    async def _run_check(self):
        try:
            sb = get_supabase()
            configs = sb.table("stream_notifications_config") \
                .select("*").eq("enabled", True).execute().data or []
            if not configs:
                return

            for cfg in configs:
                guild_id = cfg["guild_id"]
                guild = self.bot.get_guild(int(guild_id))
                if not guild:
                    continue

                accounts = _get_accounts(guild_id)
                for acc in accounts:
                    try:
                        channel = _resolve_channel(guild, acc)
                        if not channel:
                            continue
                        if acc["platform"] == "youtube":
                            await self._check_youtube(guild, channel, acc)
                        elif acc["platform"] == "twitch":
                            await self._check_twitch(guild, channel, acc)
                    except Exception as e:
                        logger.error(
                            f"[stream_notif] Error checking "
                            f"{acc['platform']}/{acc['account_id']}: {e}"
                        )
        except Exception as e:
            logger.error(f"[stream_notif] check_loop error: {e}")

    def _build_role_mention(self, guild: discord.Guild, acc: dict) -> str:
        if acc.get("role_id"):
            role = guild.get_role(int(acc["role_id"]))
            if role:
                return role.mention
        return ""

    async def _check_youtube(self, guild: discord.Guild, channel: discord.TextChannel, acc: dict):
        video = await _fetch_latest_youtube_video(acc["account_id"])
        if not video:
            return
        last_id = acc.get("last_known_id")
        if last_id == video["video_id"]:
            return

        name = acc.get("account_name") or video.get("channel_name") or acc["account_id"]
        role_mention = self._build_role_mention(guild, acc)

        embed = discord.Embed(
            title="🎥 Neues YouTube-Video!",
            description=f"**{name}** hat ein neues Video hochgeladen!\n\n"
                        f"**{video['title']}**\n{video['url']}",
            color=discord.Color.red(),
            url=video["url"],
            timestamp=datetime.now(timezone.utc),
        )
        if video.get("thumbnail"):
            embed.set_image(url=video["thumbnail"])
        embed.set_footer(text="YouTube · Stream Notifications")
        try:
            await channel.send(content=role_mention or None, embed=embed)
        except discord.Forbidden:
            logger.warning(f"[yt] Keine Berechtigung in {guild.name}/{channel.name}")
            return

        _update_account(acc["id"], last_known_id=video["video_id"])
        logger.info(f"[yt] Neues Video von {name} in {guild.name}: {video['title']}")

    async def _check_twitch(self, guild: discord.Guild, channel: discord.TextChannel, acc: dict):
        stream = await _check_twitch_live(acc["account_id"])
        name = acc.get("account_name") or acc["account_id"]
        was_live = acc.get("is_live", False)

        member = None
        if acc.get("discord_user_id"):
            member = guild.get_member(int(acc["discord_user_id"]))

        if stream and not was_live:
            role_mention = self._build_role_mention(guild, acc)
            embed = discord.Embed(
                title="🔴 Twitch Live!",
                description=f"**{name}** ist jetzt live!\n\n"
                            f"**{stream['title']}**\n"
                            f"Spiel: {stream['game']}\n"
                            f"Zuschauer: {stream['viewer_count']}\n"
                            f"{stream['url']}",
                color=discord.Color.purple(),
                url=stream["url"],
                timestamp=datetime.now(timezone.utc),
            )
            if stream.get("thumbnail"):
                embed.set_image(url=stream["thumbnail"])
            embed.set_footer(text="Twitch · Stream Notifications")
            try:
                await channel.send(content=role_mention or None, embed=embed)
            except discord.Forbidden:
                logger.warning(f"[twitch] Keine Berechtigung in {guild.name}/{channel.name}")

            _update_account(acc["id"], is_live=True, live_role_given=True)
            if member:
                await _set_live_state(member, str(guild.id), True)
            logger.info(f"[twitch] {name} ist live in {guild.name}")

        elif not stream and was_live:
            _update_account(acc["id"], is_live=False, live_role_given=False)
            if member:
                await _set_live_state(member, str(guild.id), False)
            logger.info(f"[twitch] {name} ist offline in {guild.name}")


# ══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════


class StreamNotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    sn = app_commands.Group(
        name="streamnotifications",
        description="YouTube & Twitch Benachrichtigungen",
    )

    # ──────────────────────────────── setup ────────────────────────────────

    @sn.command(name="setup", description="Kanal & Rollen für Stream-Notifications festlegen")
    async def sn_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        current = f"<#{cfg['channel_id']}>" if cfg and cfg.get("channel_id") else "*Nicht gesetzt*"
        enabled = "✅ Aktiviert" if cfg and cfg.get("enabled") else "❌ Deaktiviert"
        streamer_role_text = (
            f"<@&{cfg['streamer_role_id']}>"
            if cfg and cfg.get("streamer_role_id") else "*Nicht gesetzt*"
        )
        live_role_text = (
            f"<@&{cfg['live_role_id']}>"
            if cfg and cfg.get("live_role_id") else "*Nicht gesetzt*"
        )

        embed = discord.Embed(
            title="⚙️ Stream Notifications Setup",
            description=(
                "Wähle den **globalen Fallback-Kanal** – Accounts ohne eigenen Kanal senden hierhin.\n\n"
                "**Streamer-Rolle:** wird jedem verlinkten Account automatisch gegeben.\n"
                "**Live-Rolle:** wird nur vergeben, solange der Streamer live ist – "
                "zusammen mit einem 🔴 vor dem Namen.\n\n"
                "User verknüpfen sich mit `/streamnotifications link`."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="📋 Fallback-Kanal", value=current, inline=True)
        embed.add_field(name="🔄 Status", value=enabled, inline=True)
        embed.add_field(name="🎭 Streamer-Rolle", value=streamer_role_text, inline=True)
        embed.add_field(name="🔴 Live-Rolle", value=live_role_text, inline=True)

        yt_ok, tw_ok = _env_status()
        warnings = []
        if not yt_ok:
            warnings.append("⚠️ `YOUTUBE_API_KEY` fehlt")
        if not tw_ok:
            warnings.append("⚠️ `TWITCH_CLIENT_ID`/`SECRET` fehlen")
        if warnings:
            embed.add_field(name="⚠️ API-Status", value="\n".join(warnings), inline=False)

        view = StreamSetupView(interaction.guild_id, cfg)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    # ──────────────────────────────── add ────────────────────────────────

    @sn.command(name="add", description="Einen YouTube- oder Twitch-Account hinzufügen")
    @app_commands.describe(
        url="YouTube- oder Twitch-URL (z.B. https://youtube.com/@name oder https://twitch.tv/name)",
        kanal="Eigener Benachrichtigungskanal (optional, sonst Fallback)",
        rolle="Rolle die gepingt wird (optional)",
    )
    async def sn_add(self, interaction: discord.Interaction, url: str,
                     kanal: discord.TextChannel | None = None,
                     rolle: discord.Role | None = None):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        if not cfg and not kanal:
            await interaction.response.send_message(
                "❌ Kein Kanal konfiguriert. Nutze `/streamnotifications setup` "
                "oder gib bei `add` einen Kanal an.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        yt = _parse_youtube_url(url)
        tw_login = _parse_twitch_url(url)
        channel_id = str(kanal.id) if kanal else None
        role_id = str(rolle.id) if rolle else None

        if yt:
            if not YT_API_KEY:
                await interaction.followup.send(
                    "❌ YouTube API Key nicht konfiguriert (`YOUTUBE_API_KEY`).",
                    ephemeral=True,
                )
                return

            if yt["type"] == "id":
                yt_channel_id = yt["value"]
            else:
                yt_channel_id = await _resolve_youtube_handle(yt["value"])
                if not yt_channel_id:
                    await interaction.followup.send(
                        f"❌ YouTube-Kanal `{yt['value']}` nicht gefunden.",
                        ephemeral=True,
                    )
                    return

            name = await _fetch_youtube_channel_name(yt_channel_id)
            if not name:
                await interaction.followup.send(
                    f"❌ YouTube-Kanal mit ID `{yt_channel_id}` nicht gefunden.",
                    ephemeral=True,
                )
                return

            existing = _get_accounts(str(interaction.guild_id))
            for acc in existing:
                if acc["platform"] == "youtube" and acc["account_id"] == yt_channel_id:
                    await interaction.followup.send(
                        f"ℹ️ **{name}** (YouTube) ist bereits registriert.",
                        ephemeral=True,
                    )
                    return

            latest = await _fetch_latest_youtube_video(yt_channel_id)
            _add_account(
                str(interaction.guild_id), "youtube", yt_channel_id, name,
                channel_id, role_id, latest.get("video_id") if latest else None,
            )
            ch_text = f" in <#{channel_id}>" if kanal else ""
            role_text = f" + {rolle.mention}" if rolle else ""
            await interaction.followup.send(
                f"🎥 **{name}** (YouTube) wurde hinzugefügt!{ch_text}{role_text}\n"
                f"💡 Der Streamer kann sich jetzt mit "
                f"`/streamnotifications link {name}` verknüpfen.",
                ephemeral=True,
            )

        elif tw_login:
            if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
                await interaction.followup.send(
                    "❌ Twitch API nicht konfiguriert "
                    "(`TWITCH_CLIENT_ID`/`SECRET`).",
                    ephemeral=True,
                )
                return

            user = await _resolve_twitch_login(tw_login)
            if not user:
                await interaction.followup.send(
                    f"❌ Twitch-User `{tw_login}` nicht gefunden.",
                    ephemeral=True,
                )
                return

            twitch_id = user["id"]
            name = user["display_name"]

            existing = _get_accounts(str(interaction.guild_id))
            for acc in existing:
                if acc["platform"] == "twitch" and acc["account_id"] == twitch_id:
                    await interaction.followup.send(
                        f"ℹ️ **{name}** (Twitch) ist bereits registriert.",
                        ephemeral=True,
                    )
                    return

            _add_account(str(interaction.guild_id), "twitch", twitch_id, name, channel_id, role_id)
            ch_text = f" in <#{channel_id}>" if kanal else ""
            role_text = f" + {rolle.mention}" if rolle else ""
            await interaction.followup.send(
                f"🔴 **{name}** (Twitch) wurde hinzugefügt!{ch_text}{role_text}\n"
                f"💡 Der Streamer kann sich jetzt mit "
                f"`/streamnotifications link {name}` verknüpfen.",
                ephemeral=True,
            )

        else:
            await interaction.followup.send(
                "❌ URL nicht erkannt. Unterstützte Formate:\n"
                "• YouTube: `https://youtube.com/@name`, "
                "`https://youtube.com/channel/UCxxxxx`\n"
                "• Twitch: `https://twitch.tv/name`\n"
                "Oder gib einfach den Namen ein: `@name` (YT) oder `name` (Twitch)",
                ephemeral=True,
            )

    # ──────────────────────────────── link / unlink / my ────────────────────────────────

    @sn.command(name="link", description="Verlinke deinen Discord-Account mit einem Stream-Account")
    @app_commands.describe(account="Name oder ID des registrierten Accounts")
    @app_commands.autocomplete(account=_account_autocomplete)
    async def sn_link(self, interaction: discord.Interaction, account: str):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Nur in einem Server möglich.", ephemeral=True)
            return

        target = _find_account_by_name_or_id(str(interaction.guild_id), account)
        if not target:
            await interaction.response.send_message(
                f"❌ Account `{account}` nicht gefunden. Ein Admin muss ihn mit "
                f"`/streamnotifications add` registrieren.",
                ephemeral=True,
            )
            return

        if target.get("discord_user_id") and target["discord_user_id"] != str(interaction.user.id):
            await interaction.response.send_message(
                "❌ Dieser Account ist bereits mit einem anderen User verlinkt. "
                "Bitte wende dich an einen Admin.",
                ephemeral=True,
            )
            return

        if target.get("discord_user_id") == str(interaction.user.id):
            await interaction.response.send_message(
                f"ℹ️ Du bist bereits mit **{target.get('account_name') or target['account_id']}** verlinkt.",
                ephemeral=True,
            )
            return

        _update_account(target["id"], discord_user_id=str(interaction.user.id))

        role_warning = ""
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            warn = await _apply_streamer_role(member, str(interaction.guild_id))
            if warn:
                role_warning = f"\n⚠️ {warn}"

            if target["platform"] == "twitch":
                stream = await _check_twitch_live(target["account_id"])
                if stream:
                    _update_account(target["id"], is_live=True, live_role_given=True)
                    await _set_live_state(member, str(interaction.guild_id), True)

        acc_name = target.get("account_name") or target["account_id"]
        await interaction.response.send_message(
            f"✅ Verlinkt mit **{acc_name}** ({target['platform'].capitalize()})."
            f"{role_warning}",
            ephemeral=True,
        )

    @sn.command(name="my", description="Zeigt deine verlinkten Stream-Accounts")
    async def sn_my(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Nur in einem Server möglich.", ephemeral=True)
            return
        accs = _get_accounts_for_user(str(interaction.guild_id), str(interaction.user.id))
        if not accs:
            await interaction.response.send_message(
                "ℹ️ Du hast noch keinen Account verlinkt. "
                "Nutze `/streamnotifications link`.",
                ephemeral=True,
            )
            return

        lines = []
        for a in accs:
            name = a.get("account_name") or a["account_id"]
            live = " 🔴 LIVE" if a.get("is_live") else ""
            lines.append(f"• **{name}** ({a['platform'].capitalize()}){live}")

        await interaction.response.send_message(
            "🔗 **Deine verlinkten Accounts:**\n" + "\n".join(lines),
            ephemeral=True,
        )

    @sn.command(name="unlink", description="Verlinkung eines deiner Accounts entfernen")
    @app_commands.describe(account="Optional: nur diesen Account entlinken (leer = alle)")
    @app_commands.autocomplete(account=_account_autocomplete)
    async def sn_unlink(self, interaction: discord.Interaction, account: str | None = None):
        if interaction.guild is None:
            await interaction.response.send_message("❌ Nur in einem Server möglich.", ephemeral=True)
            return

        my_accs = _get_accounts_for_user(str(interaction.guild_id), str(interaction.user.id))
        if not my_accs:
            await interaction.response.send_message("ℹ️ Du hast keine Verlinkung.", ephemeral=True)
            return

        if account:
            target = next(
                (a for a in my_accs
                 if a["account_id"] == account
                 or (a.get("account_name") or "").lower() == account.lower()),
                None,
            )
            if not target:
                await interaction.response.send_message(
                    f"❌ Account `{account}` gehört nicht zu dir.", ephemeral=True
                )
                return
            await self._unlink_one(interaction, target, my_accs)
        else:
            removed = []
            for acc in my_accs:
                _update_account(acc["id"], discord_user_id=None, live_role_given=False)
                removed.append(acc.get("account_name") or acc["account_id"])

            member = interaction.guild.get_member(interaction.user.id)
            if member:
                await _set_live_state(member, str(interaction.guild_id), False)
                await _remove_streamer_role(member, str(interaction.guild_id))

            await interaction.response.send_message(
                f"🔓 Entlinkt: {', '.join(removed)}", ephemeral=True
            )

    async def _unlink_one(self, interaction: discord.Interaction, target: dict, all_accs: list[dict]):
        member = interaction.guild.get_member(interaction.user.id)
        remaining = [a for a in all_accs if a["id"] != target["id"]]

        if member and not remaining:
            await _set_live_state(member, str(interaction.guild_id), False)
            await _remove_streamer_role(member, str(interaction.guild_id))

        _update_account(target["id"], discord_user_id=None, live_role_given=False)
        name = target.get("account_name") or target["account_id"]
        await interaction.response.send_message(f"🔓 **{name}** entlinkt.", ephemeral=True)

    # ──────────────────────────────── edit / remove ────────────────────────────────

    @sn.command(name="edit", description="Kanal/Rolle eines Accounts ändern oder entfernen")
    @app_commands.describe(
        account="Name oder ID des Accounts",
        kanal="Neuer Kanal (leer = unverändert)",
        rolle="Neue Rolle (leer = unverändert)",
        kanal_entfernen="Kanal auf globalen Fallback zurücksetzen",
        rolle_entfernen="Rolle entfernen",
    )
    @app_commands.autocomplete(account=_account_autocomplete)
    async def sn_edit(self, interaction: discord.Interaction, account: str,
                      kanal: discord.TextChannel | None = None,
                      rolle: discord.Role | None = None,
                      kanal_entfernen: bool = False,
                      rolle_entfernen: bool = False):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        target = _find_account_by_name_or_id(str(interaction.guild_id), account)
        if not target:
            await interaction.response.send_message(
                f"❌ Account `{account}` nicht gefunden.", ephemeral=True
            )
            return

        updates: dict = {}
        if kanal_entfernen:
            updates["channel_id"] = None
        elif kanal is not None:
            updates["channel_id"] = str(kanal.id)
        if rolle_entfernen:
            updates["role_id"] = None
        elif rolle is not None:
            updates["role_id"] = str(rolle.id)

        if not updates:
            await interaction.response.send_message(
                "❌ Gib mindestens eine Änderung an (Kanal, Rolle oder `*_entfernen`).",
                ephemeral=True,
            )
            return

        _update_account(target["id"], **updates)
        acc_name = target.get("account_name") or target["account_id"]
        parts = []
        if "channel_id" in updates:
            parts.append(
                f"Kanal: <#{updates['channel_id']}>" if updates["channel_id"]
                else "Kanal: *entfernt (Fallback)*"
            )
        if "role_id" in updates:
            if updates["role_id"]:
                role = interaction.guild.get_role(int(updates["role_id"]))
                parts.append(f"Rolle: {role.mention if role else updates['role_id']}")
            else:
                parts.append("Rolle: *entfernt*")

        await interaction.response.send_message(
            f"✏️ **{acc_name}** aktualisiert: {', '.join(parts)}",
            ephemeral=True,
        )

    @sn.command(name="remove", description="Einen Account entfernen")
    @app_commands.describe(account="Die ID oder der Name des Accounts")
    @app_commands.autocomplete(account=_account_autocomplete)
    async def sn_remove(self, interaction: discord.Interaction, account: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        target = _find_account_by_name_or_id(str(interaction.guild_id), account)
        if not target:
            await interaction.response.send_message(
                f"❌ Account `{account}` nicht gefunden.", ephemeral=True
            )
            return

        # Aufräumen: Live-Status & Streamer-Rolle entfernen, falls verlinkt
        if target.get("discord_user_id"):
            member = interaction.guild.get_member(int(target["discord_user_id"]))
            if member:
                await _set_live_state(member, str(interaction.guild_id), False)
                # Nur Streamer-Rolle entfernen, wenn keine anderen Accounts mehr
                remaining = [
                    a for a in _get_accounts(str(interaction.guild_id))
                    if a["id"] != target["id"]
                    and a.get("discord_user_id") == target["discord_user_id"]
                ]
                if not remaining:
                    await _remove_streamer_role(member, str(interaction.guild_id))

        _remove_account(target["id"])
        acc_name = target.get("account_name") or target["account_id"]
        await interaction.response.send_message(
            f"🗑️ **{acc_name}** ({target['platform'].capitalize()}) wurde entfernt.",
            ephemeral=True,
        )

    # ──────────────────────────────── list / info / toggle ────────────────────────────────

    @sn.command(name="list", description="Alle registrierten Accounts anzeigen")
    async def sn_list(self, interaction: discord.Interaction):
        accounts = _get_accounts(str(interaction.guild_id))
        if not accounts:
            await interaction.response.send_message("📭 Keine Accounts registriert.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        enabled = cfg and cfg.get("enabled", True)

        embed = discord.Embed(
            title="📡 Stream Notifications – Accounts",
            description=f"Status: {'✅ Aktiviert' if enabled else '❌ Deaktiviert'}",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        yt_accounts = [a for a in accounts if a["platform"] == "youtube"]
        tw_accounts = [a for a in accounts if a["platform"] == "twitch"]

        if yt_accounts:
            lines = []
            for a in yt_accounts:
                acc_name = a.get("account_name") or a["account_id"]
                ch = f" → <#{a['channel_id']}>" if a.get("channel_id") else ""
                role = f" + <@&{a['role_id']}>" if a.get("role_id") else ""
                linked = f" 🔗 <@{a['discord_user_id']}>" if a.get("discord_user_id") else ""
                lines.append(f"• **{acc_name}**{ch}{role}{linked}")
            embed.add_field(name="🎥 YouTube", value="\n".join(lines)[:1024], inline=False)

        if tw_accounts:
            lines = []
            for a in tw_accounts:
                acc_name = a.get("account_name") or a["account_id"]
                ch = f" → <#{a['channel_id']}>" if a.get("channel_id") else ""
                role = f" + <@&{a['role_id']}>" if a.get("role_id") else ""
                live = " 🔴 LIVE" if a.get("is_live") else ""
                linked = f" 🔗 <@{a['discord_user_id']}>" if a.get("discord_user_id") else ""
                lines.append(f"• **{acc_name}**{ch}{role}{linked}{live}")
            embed.add_field(name="🔴 Twitch", value="\n".join(lines)[:1024], inline=False)

        if cfg and cfg.get("channel_id"):
            embed.set_footer(text=f"Fallback: <#{cfg['channel_id']}> · {len(accounts)} Account(s)")
        else:
            embed.set_footer(text=f"{len(accounts)} Account(s)")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @sn.command(name="info", description="Zeigt den aktuellen Status aller registrierten Accounts")
    async def sn_info(self, interaction: discord.Interaction):
        accounts = _get_accounts(str(interaction.guild_id))
        if not accounts:
            await interaction.response.send_message("📭 Keine Accounts registriert.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        embed = discord.Embed(
            title="📊 Stream Notifications – Status",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        field_count = 0
        for acc in accounts:
            if field_count >= MAX_EMBED_FIELDS:
                embed.set_footer(
                    text=f"… und {len(accounts) - MAX_EMBED_FIELDS} weitere Accounts"
                )
                break

            acc_name = acc.get("account_name") or acc["account_id"]
            platform = acc["platform"]

            ch_text = f" → <#{acc['channel_id']}>" if acc.get("channel_id") else ""
            role_text = f" | Rolle: <@&{acc['role_id']}>" if acc.get("role_id") else ""
            linked_text = (
                f" | Verlinkt: <@{acc['discord_user_id']}>"
                if acc.get("discord_user_id") else ""
            )

            if platform == "youtube":
                video = await _fetch_latest_youtube_video(acc["account_id"])
                if video:
                    value = (
                        f"**Letztes Video:** {video['title']}\n"
                        f"**Link:** [Video ansehen]({video['url']})\n"
                        f"**Kanal:**{ch_text}{role_text}{linked_text}"
                    )
                    embed.add_field(name=f"🎥 {acc_name}", value=value[:1024], inline=False)
                else:
                    embed.add_field(
                        name=f"🎥 {acc_name}",
                        value=f"Keine Videos gefunden oder API-Key fehlt.{ch_text}{role_text}{linked_text}"[:1024],
                        inline=False,
                    )
            elif platform == "twitch":
                stream = await _check_twitch_live(acc["account_id"])
                if stream:
                    value = (
                        f"🔴 **LIVE**\n"
                        f"**Stream:** {stream['title']}\n"
                        f"**Spiel:** {stream['game']}\n"
                        f"**Zuschauer:** {stream['viewer_count']}\n"
                        f"**Link:** [Stream ansehen]({stream['url']})\n"
                        f"**Kanal:**{ch_text}{role_text}{linked_text}"
                    )
                    embed.add_field(name=f"🔴 {acc_name}", value=value[:1024], inline=False)
                else:
                    embed.add_field(
                        name=f"⚫ {acc_name}",
                        value=f"Offline{ch_text}{role_text}{linked_text}"[:1024],
                        inline=False,
                    )
            field_count += 1

        await interaction.followup.send(embed=embed, ephemeral=True)

    @sn.command(name="toggle", description="Stream-Notifications ein- oder ausschalten")
    async def sn_toggle(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        if not cfg:
            await interaction.response.send_message(
                "❌ Nutze zuerst `/streamnotifications setup`.",
                ephemeral=True,
            )
            return

        new_state = not cfg.get("enabled", True)
        _toggle_config(str(interaction.guild_id), new_state)
        status = "✅ Aktiviert" if new_state else "❌ Deaktiviert"
        await interaction.response.send_message(
            f"🔄 Stream-Notifications: {status}", ephemeral=True
        )


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════


class StreamSetupView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.channel_id: str | None = (
            str(current_config["channel_id"])
            if current_config and current_config.get("channel_id") else None
        )
        self.streamer_role_id: str | None = (
            str(current_config["streamer_role_id"])
            if current_config and current_config.get("streamer_role_id") else None
        )
        self.live_role_id: str | None = (
            str(current_config["live_role_id"])
            if current_config and current_config.get("live_role_id") else None
        )
        self._rebuild()

    def _rebuild(self):
        self.clear_items()

        ch_sel = discord.ui.ChannelSelect(
            placeholder="📋 Fallback-Kanal auswählen…",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0,
        )
        ch_sel.callback = self._on_channel
        self.add_item(ch_sel)

        streamer_sel = discord.ui.RoleSelect(
            placeholder="🎭 Streamer-Rolle (für verlinkte Accounts)…",
            min_values=0, max_values=1, row=1,
        )
        streamer_sel.callback = self._on_streamer_role
        self.add_item(streamer_sel)

        live_sel = discord.ui.RoleSelect(
            placeholder="🔴 Live-Rolle (wird nur bei Live vergeben)…",
            min_values=0, max_values=1, row=2,
        )
        live_sel.callback = self._on_live_role
        self.add_item(live_sel)

        test_btn = discord.ui.Button(
            label="🧪 Test-Nachricht",
            style=discord.ButtonStyle.primary,
            disabled=self.channel_id is None,
            row=3,
        )
        test_btn.callback = self._on_test
        self.add_item(test_btn)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.success,
            disabled=self.channel_id is None,
            row=3,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    def _upsert_embed_field(self, embed: discord.Embed, name: str, value: str):
        for i, f in enumerate(embed.fields):
            if f.name == name:
                embed.set_field_at(i, name=name, value=value, inline=True)
                return
        embed.add_field(name=name, value=value, inline=True)

    async def _on_channel(self, interaction: discord.Interaction):
        self.channel_id = interaction.data["values"][0]
        self._rebuild()
        embed = interaction.message.embeds[0]
        self._upsert_embed_field(embed, "📋 Fallback-Kanal", f"<#{self.channel_id}>")
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_streamer_role(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.streamer_role_id = vals[0] if vals else None
        embed = interaction.message.embeds[0]
        value = f"<@&{self.streamer_role_id}>" if self.streamer_role_id else "*Nicht gesetzt*"
        self._upsert_embed_field(embed, "🎭 Streamer-Rolle", value)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_live_role(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.live_role_id = vals[0] if vals else None
        embed = interaction.message.embeds[0]
        value = f"<@&{self.live_role_id}>" if self.live_role_id else "*Nicht gesetzt*"
        self._upsert_embed_field(embed, "🔴 Live-Rolle", value)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_test(self, interaction: discord.Interaction):
        if not self.channel_id:
            await interaction.response.send_message("❌ Kein Kanal gesetzt.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(int(self.channel_id))
        if not ch:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        embed = discord.Embed(
            title="🧪 Test – Stream Notifications",
            description="Wenn du das siehst, funktioniert die Konfiguration!",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc),
        )
        try:
            await ch.send(embed=embed)
            await interaction.response.send_message(
                f"✅ Test gesendet in {ch.mention}.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ Keine Berechtigung in {ch.mention}.", ephemeral=True
            )

    async def _on_save(self, interaction: discord.Interaction):
        if not self.channel_id:
            await interaction.response.send_message("❌ Kein Kanal gesetzt.", ephemeral=True)
            return
        _set_config(
            self.guild_id,
            channel_id=self.channel_id,
            streamer_role_id=self.streamer_role_id,
            live_role_id=self.live_role_id,
            enabled=True,
        )
        embed = interaction.message.embeds[0]
        embed.title = "✅ Stream Notifications Setup gespeichert!"
        embed.color = discord.Color.green()
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP
# ══════════════════════════════════════════════════════════════════════════════


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamNotificationsCog(bot))
    await bot.add_cog(StreamNotificationLoop(bot))