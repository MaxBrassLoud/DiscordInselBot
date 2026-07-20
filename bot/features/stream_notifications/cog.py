"""
bot/features/stream_notifications/cog.py
==========================================
Benachrichtigt einen Discord-Kanal, wenn ein registrierter YouTube-Kanal
ein neues Video hochlädt oder ein registrierter Twitch-User live geht.

Jeder Account hat seinen eigenen Benachrichtigungskanal und optional eine
Rolle die gepingt wird.

Supabase SQL (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS stream_notifications_config (
        id          BIGSERIAL PRIMARY KEY,
        guild_id    TEXT NOT NULL UNIQUE,
        channel_id  TEXT,
        enabled     BOOLEAN DEFAULT TRUE,
        created_at  TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS stream_notifications_accounts (
        id              BIGSERIAL PRIMARY KEY,
        guild_id        TEXT NOT NULL,
        platform        TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
        account_id      TEXT NOT NULL,
        account_name    TEXT,
        channel_id      TEXT,
        role_id         TEXT,
        last_known_id   TEXT,
        is_live         BOOLEAN DEFAULT FALSE,
        added_at        TIMESTAMPTZ DEFAULT now(),
        UNIQUE (guild_id, platform, account_id)
    );
    CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_guild
        ON stream_notifications_accounts (guild_id);

ENV-Variablen:
    YOUTUBE_API_KEY       – Google API Key (YouTube Data API v3)
    TWITCH_CLIENT_ID      – Twitch Helix Client ID
    TWITCH_CLIENT_SECRET  – Twitch Helix Client Secret
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
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

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════


def _get_config(guild_id: str) -> dict | None:
    try:
        r = get_supabase().table("stream_notifications_config") \
            .select("*").eq("guild_id", guild_id).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[stream_notif] _get_config: {e}")
        return None


def _upsert_config(guild_id: str, channel_id: str):
    sb = get_supabase()
    existing = sb.table("stream_notifications_config") \
        .select("id").eq("guild_id", guild_id).execute()
    if existing.data:
        sb.table("stream_notifications_config") \
            .update({"channel_id": channel_id, "enabled": True}) \
            .eq("guild_id", guild_id).execute()
    else:
        sb.table("stream_notifications_config").insert({
            "guild_id": guild_id,
            "channel_id": channel_id,
            "enabled": True,
        }).execute()


def _toggle_config(guild_id: str, enabled: bool):
    get_supabase().table("stream_notifications_config") \
        .update({"enabled": enabled}).eq("guild_id", guild_id).execute()


def _get_accounts(guild_id: str) -> list[dict]:
    try:
        r = get_supabase().table("stream_notifications_accounts") \
            .select("*").eq("guild_id", guild_id) \
            .order("added_at", desc=False).execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[stream_notif] _get_accounts: {e}")
        return []


def _add_account(guild_id: str, platform: str, account_id: str,
                 account_name: str | None = None, channel_id: str | None = None,
                 role_id: str | None = None):
    get_supabase().table("stream_notifications_accounts").insert({
        "guild_id": guild_id,
        "platform": platform,
        "account_id": account_id,
        "account_name": account_name,
        "channel_id": channel_id,
        "role_id": role_id,
    }).execute()


def _remove_account(account_db_id: int):
    get_supabase().table("stream_notifications_accounts") \
        .delete().eq("id", account_db_id).execute()


def _update_account(account_db_id: int, **kwargs):
    get_supabase().table("stream_notifications_accounts") \
        .update(kwargs).eq("id", account_db_id).execute()


def _resolve_channel(guild: discord.Guild, acc: dict) -> discord.TextChannel | None:
    """Findet den Kanal für einen Account: erst den eigenen, dann den globalen Fallback."""
    # 1) Eigener Kanal des Accounts
    if acc.get("channel_id"):
        ch = guild.get_channel(int(acc["channel_id"]))
        if ch:
            return ch
    # 2) Globaler Fallback aus config
    cfg = _get_config(str(guild.id))
    if cfg and cfg.get("channel_id"):
        ch = guild.get_channel(int(cfg["channel_id"]))
        if ch:
            return ch
    return None


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
    """Resolve @handle → Channel ID. Versucht zuerst forHandle, dann Search-API."""
    if not YT_API_KEY:
        return None

    clean = handle.lstrip("@")

    # Versuch 1: forHandle
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

    # Versuch 2: Search nach Channel-Name
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
                # Fallback: erstes Ergebnis nehmen wenn der Name ähnlich ist
                if items:
                    return items[0].get("id", {}).get("channelId")
    except Exception as e:
        logger.warning(f"[yt] search resolve: {e}")

    return None


def _parse_youtube_url(url: str) -> str | None:
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
    if not YT_API_KEY:
        return None
    url = f"{YOUTUBE_API_BASE}/search"
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "order": "date",
        "maxResults": 1,
        "type": "video",
        "key": YT_API_KEY,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                items = data.get("items", [])
                if not items:
                    return None
                video = items[0]
                video_id = video["id"]["videoId"]
                snippet = video["snippet"]
                return {
                    "video_id": video_id,
                    "title": snippet["title"],
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "channel_name": snippet.get("channelTitle", ""),
                    "thumbnail": snippet.get("thumbnails", {}).get("high", {}).get("url", ""),
                }
    except Exception as e:
        logger.warning(f"[yt] latest video fetch: {e}")
        return None


_twitch_oauth_token: str | None = None


async def _get_twitch_token() -> str | None:
    global _twitch_oauth_token
    if _twitch_oauth_token:
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
                if resp.status != 200:
                    return None
                data = await resp.json()
                streams = data.get("data", [])
                if streams:
                    s = streams[0]
                    return {
                        "stream_id": s["id"],
                        "title": s.get("title", ""),
                        "game": s.get("game_name", ""),
                        "viewer_count": s.get("viewer_count", 0),
                        "thumbnail": s.get("thumbnail_url", "{width}x{height}").replace("{width}", "440").replace("{height}", "248"),
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
        self.check_loop.start()

    def cog_unload(self):
        self.check_loop.cancel()

    @tasks.loop(minutes=5)
    async def check_loop(self):
        await self.bot.wait_until_ready()
        try:
            sb = get_supabase()
            configs_r = sb.table("stream_notifications_config") \
                .select("*").eq("enabled", True).execute()
            configs = configs_r.data or []
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
                        logger.error(f"[stream_notif] Error checking {acc['platform']}/{acc['account_id']}: {e}")
        except Exception as e:
            logger.error(f"[stream_notif] check_loop error: {e}")

    @check_loop.before_loop
    async def before_check_loop(self):
        await self.bot.wait_until_ready()

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
                return
            _update_account(acc["id"], is_live=True)
            logger.info(f"[twitch] {name} ist live in {guild.name}")

        elif not stream and was_live:
            _update_account(acc["id"], is_live=False)
            logger.info(f"[twitch] {name} ist offline in {guild.name}")


# ══════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ══════════════════════════════════════════════════════════════════════════════


class StreamNotificationsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    sn = app_commands.Group(name="streamnotifications", description="YouTube & Twitch Benachrichtigungen")

    @sn.command(name="setup", description="Globalen Fallback-Kanal für Stream-Notifications festlegen")
    async def sn_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        current = f"<#{cfg['channel_id']}>" if cfg and cfg.get("channel_id") else "*Nicht gesetzt*"
        enabled = "✅ Aktiviert" if cfg and cfg.get("enabled") else "❌ Deaktiviert"

        embed = discord.Embed(
            title="⚙️ Stream Notifications Setup",
            description=(
                "Wähle den **globalen Fallback-Kanal** – Accounts ohne eigenen Kanal senden hierhin.\n\n"
                "Du kannst bei `/streamnotifications add` auch einen **eigenen Kanal** und eine **Rolle** pro Account angeben."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="📋 Fallback-Kanal", value=current, inline=True)
        embed.add_field(name="🔄 Status", value=enabled, inline=True)

        view = StreamSetupView(interaction.guild_id, cfg)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @sn.command(name="add", description="Einen YouTube- oder Twitch-Account hinzufügen")
    @app_commands.describe(
        url="YouTube- oder Twitch-URL (z.B. https://youtube.com/@name oder https://twitch.tv/name)",
        kanal="Eigener Benachrichtigungskanal (optional, sonst Fallback)",
        rolle="Rolle die gepingt wird (optional)"
    )
    async def sn_add(self, interaction: discord.Interaction, url: str,
                     kanal: discord.TextChannel | None = None, rolle: discord.Role | None = None):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        cfg = _get_config(str(interaction.guild_id))
        if not cfg and not kanal:
            await interaction.response.send_message(
                "❌ Kein Kanal konfiguriert. Nutze `/streamnotifications setup` oder gib bei `add` einen Kanal an.",
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
                await interaction.followup.send("❌ YouTube API Key nicht konfiguriert (YOUTUBE_API_KEY).", ephemeral=True)
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

            _add_account(str(interaction.guild_id), "youtube", yt_channel_id, name, channel_id, role_id)
            ch_text = f" in <#{channel_id}>" if kanal else ""
            role_text = f" + {rolle.mention}" if rolle else ""
            await interaction.followup.send(
                f"🎥 **{name}** (YouTube) wurde hinzugefügt!{ch_text}{role_text}",
                ephemeral=True,
            )

        elif tw_login:
            if not TWITCH_CLIENT_ID or not TWITCH_CLIENT_SECRET:
                await interaction.followup.send("❌ Twitch API nicht konfiguriert (TWITCH_CLIENT_ID/SECRET).", ephemeral=True)
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
                f"🔴 **{name}** (Twitch) wurde hinzugefügt!{ch_text}{role_text}",
                ephemeral=True,
            )

        else:
            await interaction.followup.send(
                "❌ URL nicht erkannt. Unterstützte Formate:\n"
                "• YouTube: `https://youtube.com/@name`, `https://youtube.com/channel/UCxxxxx`\n"
                "• Twitch: `https://twitch.tv/name`\n"
                "Oder gib einfach den Namen ein: `@name` (YT) oder `name` (Twitch)",
                ephemeral=True,
            )

    @sn.command(name="edit", description="Kanal oder Rolle eines Accounts ändern")
    @app_commands.describe(
        name="Name oder ID des Accounts",
        kanal="Neuer Kanal (leer lassen zum Entfernen)",
        rolle="Neue Rolle (leer lassen zum Entfernen)"
    )
    async def sn_edit(self, interaction: discord.Interaction, name: str,
                      kanal: discord.TextChannel | None = None, rolle: discord.Role | None = None):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        accounts = _get_accounts(str(interaction.guild_id))
        target = None
        for acc in accounts:
            if acc["account_id"] == name or (acc.get("account_name") or "").lower() == name.lower():
                target = acc
                break

        if not target:
            await interaction.response.send_message(f"❌ Account `{name}` nicht gefunden.", ephemeral=True)
            return

        updates = {}
        if kanal is not None:
            updates["channel_id"] = str(kanal.id)
        if rolle is not None:
            updates["role_id"] = str(rolle.id)

        if not updates:
            await interaction.response.send_message("❌ Gib mindestens einen Kanal oder eine Rolle an.", ephemeral=True)
            return

        _update_account(target["id"], **updates)
        acc_name = target.get("account_name") or target["account_id"]
        parts = []
        if "channel_id" in updates:
            parts.append(f"Kanal: <#{updates['channel_id']}>")
        if "role_id" in updates:
            role = interaction.guild.get_role(int(updates["role_id"]))
            parts.append(f"Rolle: {role.mention if role else updates['role_id']}")
        await interaction.response.send_message(
            f"✏️ **{acc_name}** aktualisiert: {', '.join(parts)}",
            ephemeral=True,
        )

    @sn.command(name="remove", description="Einen Account entfernen")
    @app_commands.describe(account_id="Die ID oder der Name des Accounts")
    async def sn_remove(self, interaction: discord.Interaction, account_id: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        accounts = _get_accounts(str(interaction.guild_id))
        for acc in accounts:
            if acc["account_id"] == account_id or (acc.get("account_name") or "").lower() == account_id.lower():
                _remove_account(acc["id"])
                acc_name = acc.get("account_name") or acc["account_id"]
                await interaction.response.send_message(
                    f"🗑️ **{acc_name}** ({acc['platform'].capitalize()}) wurde entfernt.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_message(f"❌ Account `{account_id}` nicht gefunden.", ephemeral=True)

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
                lines.append(f"• **{acc_name}**{ch}{role}")
            embed.add_field(name="🎥 YouTube", value="\n".join(lines), inline=False)

        if tw_accounts:
            lines = []
            for a in tw_accounts:
                acc_name = a.get("account_name") or a["account_id"]
                ch = f" → <#{a['channel_id']}>" if a.get("channel_id") else ""
                role = f" + <@&{a['role_id']}>" if a.get("role_id") else ""
                live = " 🔴 LIVE" if a.get("is_live") else ""
                lines.append(f"• **{acc_name}**{ch}{role}{live}")
            embed.add_field(name="🔴 Twitch", value="\n".join(lines), inline=False)

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

        for acc in accounts:
            acc_name = acc.get("account_name") or acc["account_id"]
            platform = acc["platform"]

            # Kanal-Info
            ch_text = ""
            if acc.get("channel_id"):
                ch_text = f" → <#{acc['channel_id']}>"
            role_text = ""
            if acc.get("role_id"):
                role_text = f" | Rolle: <@&{acc['role_id']}>"

            if platform == "youtube":
                video = await _fetch_latest_youtube_video(acc["account_id"])
                if video:
                    value = (
                        f"**Letztes Video:** {video['title']}\n"
                        f"**Link:** [Video ansehen]({video['url']})\n"
                        f"**Kanal:**{ch_text}{role_text}"
                    )
                    embed.add_field(name=f"🎥 {acc_name}", value=value, inline=False)
                else:
                    embed.add_field(
                        name=f"🎥 {acc_name}",
                        value=f"Keine Videos gefunden oder API-Key fehlt.{ch_text}{role_text}",
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
                        f"**Kanal:**{ch_text}{role_text}"
                    )
                    embed.add_field(name=f"🔴 {acc_name}", value=value, inline=False)
                else:
                    embed.add_field(
                        name=f"⚫ {acc_name}",
                        value=f"Offline{ch_text}{role_text}",
                        inline=False,
                    )

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
        await interaction.response.send_message(f"🔄 Stream-Notifications: {status}", ephemeral=True)


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════


class StreamSetupView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.channel_id: str | None = (
            str(current_config["channel_id"]) if current_config and current_config.get("channel_id") else None
        )
        self._rebuild()

    def _rebuild(self):
        self.clear_items()
        sel = discord.ui.ChannelSelect(
            placeholder="📋 Fallback-Kanal auswählen…",
            min_values=1, max_values=1,
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
        embed = interaction.message.embeds[0]
        embed.set_field_at(0, name="📋 Fallback-Kanal", value=f"<#{self.channel_id}>", inline=True)
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_save(self, interaction: discord.Interaction):
        _upsert_config(self.guild_id, self.channel_id)
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
