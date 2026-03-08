"""
web_app/discord_api.py  –  Discord OAuth2 + Bot API helpers
"""
from __future__ import annotations
import logging, os, time
import aiohttp

log = logging.getLogger("web.discord")
DISCORD_API = "https://discord.com/api/v10"


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def client_id()     -> str:   return _cfg("DISCORD_CLIENT_ID")
def client_secret() -> str:   return _cfg("DISCORD_CLIENT_SECRET")
def guild_id()      -> str:   return _cfg("DISCORD_GUILD_ID")

def allowed_roles() -> set[str]:
    return {r.strip() for r in _cfg("DISCORD_ALLOWED_ROLE_IDS").split(",") if r.strip()}

def redirect_uri() -> str:
    base = _cfg("WEB_BASE_URL", "https://localhost:5000").rstrip("/")
    return f"{base}/auth/callback"


# ── OAuth2 ────────────────────────────────────────────────────────────────────

async def exchange_code(code: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        resp = await s.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     client_id(),
                "client_secret": client_secret(),
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  redirect_uri(),
            },
        )
        if not resp.ok:
            log.error(f"[OAuth] Token exchange failed: {resp.status} {await resp.text()}")
            return None
        return await resp.json()


async def discord_get(path: str, access_token: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        resp = await s.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return await resp.json() if resp.ok else None


# ── Authorization checks ──────────────────────────────────────────────────────

def is_authorized(member: dict | None) -> bool:
    if not member or ("roles" not in member and "user" not in member):
        return False
    ar = allowed_roles()
    if not ar:
        return True
    return bool(set(member.get("roles", [])) & ar)


def user_has_server_access(user: dict, server_id: str) -> bool:
    if not server_id:
        return False
    if user.get("guild_id") and user["guild_id"] != server_id:
        return False
    ar = allowed_roles()
    if not ar:
        return True
    return bool(set(user.get("roles", [])) & ar)


def user_can_see_ticket(user: dict, ticket: dict, server_id: str, staff_role_ids: list[str]) -> bool:
    uid = user.get("id", "")
    if uid and str(ticket.get("creator_id", "")) == uid:
        return True
    user_roles = set(user.get("roles", []))
    if staff_role_ids and user_roles & set(staff_role_ids):
        return True
    ar = allowed_roles()
    if ar and user_roles & ar:
        return True
    return False


# ── Bot API ───────────────────────────────────────────────────────────────────

async def bot_get(path: str) -> dict | list | None:
    token = _cfg("DISCORD_TOKEN")
    if not token:
        return None
    async with aiohttp.ClientSession() as s:
        resp = await s.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"Bot {token}"},
        )
        return await resp.json() if resp.ok else None


async def get_guild_info(gid: str) -> dict | None:
    return await bot_get(f"/guilds/{gid}")


async def get_guild_members(gid: str, query: str = "", limit: int = 10) -> list:
    if query:
        data = await bot_get(f"/guilds/{gid}/members/search?query={query}&limit={limit}")
    else:
        data = await bot_get(f"/guilds/{gid}/members?limit={limit}")
    return data if isinstance(data, list) else []


async def get_member(gid: str, user_id: str) -> dict | None:
    return await bot_get(f"/guilds/{gid}/members/{user_id}")


# ── Display helpers ───────────────────────────────────────────────────────────

def guild_icon_url(guild: dict) -> str | None:
    if not guild:
        return None
    icon = guild.get("icon")
    gid  = guild.get("id")
    if icon and gid:
        ext = "gif" if icon.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/icons/{gid}/{icon}.{ext}?size=64"
    return None


def member_display_name(member: dict) -> str:
    if not member:
        return "Unbekannt"
    nick = member.get("nick")
    if nick:
        return nick
    user = member.get("user", {})
    return user.get("global_name") or user.get("username") or "Unbekannt"


def member_avatar_url(member: dict) -> str | None:
    if not member:
        return None
    user = member.get("user", {})
    uid  = user.get("id")
    guild_avatar = member.get("avatar")
    if guild_avatar and uid:
        return f"https://cdn.discordapp.com/guilds/{uid}/users/{uid}/avatars/{guild_avatar}.png?size=32"
    avatar = user.get("avatar")
    if avatar and uid:
        return f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=32"
    disc = int(user.get("discriminator") or 0) % 5
    return f"https://cdn.discordapp.com/embed/avatars/{disc}.png"


# ── Guild cache (5 min) ───────────────────────────────────────────────────────

_guild_cache: dict[str, dict]  = {}
_guild_cache_ts: dict[str, float] = {}
_GUILD_CACHE_TTL = 300


async def cached_guild_info(gid: str) -> dict | None:
    now = time.time()
    if gid in _guild_cache and (now - _guild_cache_ts.get(gid, 0)) < _GUILD_CACHE_TTL:
        return _guild_cache[gid]
    info = await get_guild_info(gid)
    if info:
        _guild_cache[gid]    = info
        _guild_cache_ts[gid] = now
    return info