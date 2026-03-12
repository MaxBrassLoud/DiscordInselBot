"""
web_app/discord_api.py  –  Discord OAuth2 + Bot API helpers
"""
from __future__ import annotations
import logging, os, time
import aiohttp

log = logging.getLogger("web.discord")
DISCORD_API = "https://discord.com/api/v10"

# ── Shared session (einmalig erstellt, sauber geschlossen) ────────────────────

_session: aiohttp.ClientSession | None = None


def get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


async def close_session() -> None:
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None


# ── Config ────────────────────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def client_id()     -> str:   return _cfg("DISCORD_CLIENT_ID")
def client_secret() -> str:   return _cfg("DISCORD_CLIENT_SECRET")
def guild_id()      -> str:   return _cfg("DISCORD_GUILD_ID")
def mbl_id()        -> str:   return _cfg("MBL", "")

def allowed_roles() -> set[str]:
    return {r.strip() for r in _cfg("DISCORD_ALLOWED_ROLE_IDS").split(",") if r.strip()}

def redirect_uri() -> str:
    base = _cfg("WEB_BASE_URL", "https://localhost:5000").rstrip("/")
    return f"{base}/auth/callback"


# ── OAuth2 ────────────────────────────────────────────────────────────────────

async def exchange_code(code: str) -> dict | None:
    s = get_session()
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
    s = get_session()
    resp = await s.get(
        f"{DISCORD_API}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    return await resp.json() if resp.ok else None


# ── Role ID parsing helper ────────────────────────────────────────────────────

def _parse_role_ids(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(r) for r in raw if r]
    return [r.strip() for r in str(raw).split(",") if r.strip()]


def _user_roles(user: dict) -> set[str]:
    return set(user.get("roles", []))


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


# ── Granular permission helpers ───────────────────────────────────────────────

def is_mbl(user: dict) -> bool:
    mid = mbl_id()
    return bool(mid and user.get("id") == mid)


def user_is_web_admin(user: dict, web_admin_role_ids: list[str]) -> bool:
    if is_mbl(user):
        return True
    if not web_admin_role_ids:
        return False
    return bool(_user_roles(user) & set(web_admin_role_ids))


def user_is_ticket_staff_for_module(user: dict, module_staff_role_ids: list[str]) -> bool:
    if not module_staff_role_ids:
        return False
    return bool(_user_roles(user) & set(module_staff_role_ids))


def user_is_application_staff(user: dict, app_staff_role_ids: list[str]) -> bool:
    if not app_staff_role_ids:
        return False
    return bool(_user_roles(user) & set(app_staff_role_ids))


# ── Ticket visibility ─────────────────────────────────────────────────────────

def user_can_see_ticket(
    user: dict,
    ticket: dict,
    server_id: str,
    staff_role_ids: list[str],
    web_admin_role_ids: list[str] | None = None,
) -> bool:
    uid = user.get("id", "")
    if is_mbl(user):                                                      return True
    if web_admin_role_ids and user_is_web_admin(user, web_admin_role_ids): return True
    if staff_role_ids and user_is_ticket_staff_for_module(user, staff_role_ids): return True
    if uid and str(ticket.get("creator_id", "")) == uid:                  return True
    added = ticket.get("added_users") or []
    if uid and uid in [str(u) for u in added]:                            return True
    return False


def user_can_see_application(
    user: dict,
    application: dict,
    server_id: str,
    app_staff_role_ids: list[str],
    web_admin_role_ids: list[str] | None = None,
) -> bool:
    uid = user.get("id", "")
    if is_mbl(user):                                                           return True
    if web_admin_role_ids and user_is_web_admin(user, web_admin_role_ids):     return True
    if app_staff_role_ids and user_is_application_staff(user, app_staff_role_ids): return True
    if uid and str(application.get("creator_id", "")) == uid:                 return True
    return False


# ── Bot API ───────────────────────────────────────────────────────────────────

async def bot_get(path: str) -> dict | list | None:
    token = _cfg("DISCORD_TOKEN")
    if not token:
        return None
    s = get_session()
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

_guild_cache: dict[str, dict]     = {}
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