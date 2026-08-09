"""
bot/core/web_app/flask_app/app.py
===================================
FIXES:
  - [CRITICAL] FLASK_SECRET_KEY: App-Start wird abgebrochen wenn Key fehlt
    oder der unsichere Fallback (token_hex) verwendet wird
  - [CRITICAL] Open Redirect: session["next"] wird auf same-origin geprüft
    bevor redirect ausgeführt wird
  - [CRITICAL] Thread-safe Caches mit threading.RLock
  - [MEDIUM]   added_users NULL-Check in Supabase-Query
"""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from functools import wraps
from urllib.parse import urlencode, urlsplit
try:
    from werkzeug.urls import url_parse
except ImportError:
    from urllib.parse import urlparse as url_parse

import requests
from flask import (
    Flask, render_template, redirect, url_for,
    request, session, jsonify,
)

# ══════════════════════════════════════════════════════════════════════════════
# Konfiguration
# ══════════════════════════════════════════════════════════════════════════════

app = Flask(__name__)

# ── [FIX CRITICAL] Secret-Key Pflichtprüfung ─────────────────────────────────
_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")
if not _SECRET_KEY:
    print(
        "[FATAL] FLASK_SECRET_KEY ist nicht gesetzt!\n"
        "Setze einen sicheren Wert in der .env Datei:\n"
        "  FLASK_SECRET_KEY=" + secrets.token_hex(32) + "\n"
        "App wird beendet.",
        file=sys.stderr,
    )
    sys.exit(1)

if len(_SECRET_KEY) < 32:
    print(
        "[FATAL] FLASK_SECRET_KEY muss mindestens 32 Zeichen lang sein.\n"
        "App wird beendet.",
        file=sys.stderr,
    )
    sys.exit(1)

app.secret_key = _SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Local HTTP development must keep working; production HTTPS cookies are
    # never sent over an unencrypted connection.
    SESSION_COOKIE_SECURE=os.getenv("WEB_BASE_URL", "").startswith("https://"),
)

# ══════════════════════════════════════════════════════════════════════════════
# Rate Limiter (in-memory, pro IP)
# ══════════════════════════════════════════════════════════════════════════════
_rate_limits: dict[str, list[float]] = {}
_rate_limit_lock = threading.Lock()
RATE_LIMIT_WINDOW = 30  # Sekunden
RATE_LIMIT_MAX_REQUESTS = 480  # pro Fenster

def _check_rate_limit(ip: str) -> bool:
    """Prüft ob die IP das Rate-Limit überschritten hat. Gibt True zurück wenn erlaubt."""
    now = time.time()
    with _rate_limit_lock:
        timestamps = _rate_limits.get(ip, [])
        # Alte Einträge entfernen
        timestamps = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
        if len(timestamps) >= RATE_LIMIT_MAX_REQUESTS:
            return False
        timestamps.append(now)
        _rate_limits[ip] = timestamps
    return True

@app.before_request
def _enforce_rate_limit():
    if request.path.startswith("/static/"):
        return
    ip = request.remote_addr or "unknown"
    if not _check_rate_limit(ip):
        return jsonify({"error": "Zu viele Anfragen. Bitte später erneut versuchen."}), 429


@app.before_request
def _reject_cross_site_writes():
    """Block browser requests that try to mutate a signed-in session cross-site."""
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    origin = request.headers.get("Origin")
    if origin and urlsplit(origin).netloc != request.host:
        return jsonify({"error": "Cross-site request blocked"}), 403

DISCORD_API   = "https://discord.com/api/v10"
CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
BOT_TOKEN     = os.getenv("DISCORD_TOKEN", "")
WEB_BASE_URL  = os.getenv("WEB_BASE_URL", "http://localhost:5000").rstrip("/")
# MBL is kept as a backwards-compatible name.  Prefer the explicit name in
# deployments so it is clear that this is a Discord snowflake, not a username.
MBL_ID        = os.getenv("MBL_DISCORD_ID", "").strip() or os.getenv("MBL", "").strip()
SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")

MEMBER_CACHE_TTL = 300
GUILD_CACHE_TTL  = 300
PERM_CACHE_TTL   = 60
ROLES_REFRESH    = 120

import logging
log = logging.getLogger("insel_web")
log.setLevel(logging.DEBUG)
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S"))
    log.addHandler(_h)

# ══════════════════════════════════════════════════════════════════════════════
# Supabase Singleton
# ══════════════════════════════════════════════════════════════════════════════

_supabase = None
_supabase_lock = threading.Lock()

def get_supabase():
    global _supabase
    if _supabase is None:
        with _supabase_lock:
            if _supabase is None:
                from supabase import create_client
                _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

def sb(table: str):
    return get_supabase().table(table)

# ══════════════════════════════════════════════════════════════════════════════
# Thread-safe Caches
# ══════════════════════════════════════════════════════════════════════════════

# [FIX CRITICAL] RLock um Dict-Corruption bei concurrent writes zu verhindern
_cache_lock    = threading.RLock()
_member_cache: dict[str, dict] = {}
_guild_cache:  dict[str, dict] = {}
_perm_cache:   dict[str, dict] = {}
_MISS = object()

def _cget(store: dict, key: str, ttl: int):
    with _cache_lock:
        e = store.get(key)
        if e is not None and time.time() - e["ts"] < ttl:
            return e["data"]
    return _MISS

def _cset(store: dict, key: str, data):
    with _cache_lock:
        store[key] = {"data": data, "ts": time.time()}
    return data

# ══════════════════════════════════════════════════════════════════════════════
# HTTP Helpers
# ══════════════════════════════════════════════════════════════════════════════

_http = requests.Session()

def _bot_get(path: str) -> dict | list | None:
    if not BOT_TOKEN:
        log.warning(f"[bot_get] BOT_TOKEN fehlt – überspringe {path}")
        return None
    try:
        r = _http.get(f"{DISCORD_API}{path}",
                      headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=5)
        if r.status_code == 429:
            # Rate Limited – warten und nochmal versuchen
            data = r.json()
            wait = data.get("retry_after", 1.0)
            log.warning(f"[bot_get] Rate Limited – warte {wait}s für {path}")
            import time
            time.sleep(wait)
            r = _http.get(f"{DISCORD_API}{path}",
                          headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=5)
        if r.status_code == 404:
            # Guild nicht gefunden – zur Invalid-Liste hinzufügen
            if "/guilds/" in path and "/members/" in path:
                parts = path.split("/guilds/")
                if len(parts) > 1:
                    guild_id = parts[1].split("/")[0]
                    _invalid_guilds.add(guild_id)
                    log.debug(f"[bot_get] Guild {guild_id} nicht gefunden – zur Invalid-Liste hinzugefügt")
            return None
        if not r.ok:
            log.warning(f"[bot_get] {path} -> {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"[bot_get] {path} -> Exception: {e}")
        return None

def _discord_post(path: str, data: dict) -> dict | None:
    try:
        r = _http.post(f"{DISCORD_API}{path}", data=data, timeout=8)
        return r.json() if r.ok else None
    except Exception:
        return None

def _bearer_get(path: str, token: str) -> dict | list | None:
    try:
        r = _http.get(f"{DISCORD_API}{path}",
                      headers={"Authorization": f"Bearer {token}"}, timeout=8)
        return r.json() if r.ok else None
    except Exception:
        return None

# ══════════════════════════════════════════════════════════════════════════════
# Discord Daten-Helpers
# ══════════════════════════════════════════════════════════════════════════════

def _guild_icon_url(guild: dict | None) -> str | None:
    if not guild:
        return None
    icon, gid = guild.get("icon"), guild.get("id")
    if icon and gid:
        ext = "gif" if icon.startswith("a_") else "png"
        return f"https://cdn.discordapp.com/icons/{gid}/{icon}.{ext}?size=64"
    return None

def _member_name(member: dict | None, fallback: str = "Unbekannt") -> str:
    if not member:
        return fallback
    if member.get("nick"):
        return member["nick"]
    u = member.get("user", {})
    return u.get("global_name") or u.get("username") or fallback

def _member_avatar(member: dict | None, uid: str = "") -> str | None:
    if not member:
        return None
    u   = member.get("user", {})
    id_ = u.get("id") or uid
    av  = u.get("avatar")
    if av and id_ and id_.isdigit():
        return f"https://cdn.discordapp.com/avatars/{id_}/{av}.png?size=64"
    return None

_invalid_guilds: set[str] = set()

def _cached_member(guild_id: str, user_id: str) -> dict | None:
    # Überspringe Guilds die wir wissen dass sie nicht existieren
    if guild_id in _invalid_guilds:
        return None
    key = f"{guild_id}:{user_id}"
    hit = _cget(_member_cache, key, MEMBER_CACHE_TTL)
    if hit is not _MISS:
        return hit
    try:
        data = _bot_get(f"/guilds/{guild_id}/members/{user_id}")
        return _cset(_member_cache, key, data)
    except Exception as e:
        log.error(f"[member] Fehler beim Laden von Member {user_id} auf Guild {guild_id}: {e}")
        return _cset(_member_cache, key, None)

def _cached_guild(guild_id: str) -> dict | None:
    # Überspringe Guilds die wir wissen dass sie nicht existieren
    if guild_id in _invalid_guilds:
        return None
    hit = _cget(_guild_cache, guild_id, GUILD_CACHE_TTL)
    if hit is not _MISS:
        return hit
    try:
        r = _http.get(f"{DISCORD_API}/guilds/{guild_id}",
                      headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=5)
        if r.status_code == 404:
            # Guild existiert nicht für diesen Bot – in Invalid-Liste setzen
            _invalid_guilds.add(guild_id)
            log.debug(f"[guild] Guild {guild_id} nicht gefunden (404) – zur Invalid-Liste hinzugefügt")
            return _cset(_guild_cache, guild_id, None)
        if r.ok:
            data = r.json()
            _cset(_guild_cache, guild_id, data)
            return data
        log.warning(f"[guild] Guild {guild_id} -> {r.status_code}")
        return None
    except Exception as e:
        log.error(f"[guild] Fehler beim Laden von Guild {guild_id}: {e}")
        return None

# ══════════════════════════════════════════════════════════════════════════════
# Daten-Parsing
# ══════════════════════════════════════════════════════════════════════════════

def _parse_role_ids(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(r).strip() for r in raw if str(r).strip()]
    s = str(raw).strip()
    return [r.strip() for r in s.split(",") if r.strip()] if s else []

def _parse_added_users(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(u).strip() for u in raw if u]
    return []

# ══════════════════════════════════════════════════════════════════════════════
# DB: Konfigurierte Server laden (gecacht)
# ══════════════════════════════════════════════════════════════════════════════

def _load_all_server_ids() -> list[str]:
    hit = _cget(_perm_cache, "all_server_ids", PERM_CACHE_TTL)
    if hit is not _MISS:
        return hit
    ids: set[str] = set()

    # 1. Bot-Guilds direkt von Discord laden
    bot_guild_ids = set()
    if BOT_TOKEN:
        try:
            guilds = _bot_get("/users/@me/guilds")
            if isinstance(guilds, list):
                for g in guilds:
                    gid = g.get("id")
                    if gid:
                        bot_guild_ids.add(gid)
                        ids.add(gid)
                log.info(f"[db] Bot ist auf {len(bot_guild_ids)} Guilds (Discord API)")
            else:
                log.warning(f"[db] Bot-Guilds API lieferte keine Liste: {type(guilds)}")
        except Exception as e:
            log.error(f"[db] Bot-Guilds laden fehlgeschlagen: {e}")
    else:
        log.warning("[db] BOT_TOKEN fehlt – kann Guilds nicht von Discord laden")

    # 2. DB-Tabellen durchsuchen – aber nur Server die der Bot kennt
    server_id_tables = [
        "ticket_servers", "application_servers",
        "voice_creator_config", "raid_ignored_roles",
        "birthdays", "minecraft_names",
    ]
    guild_id_tables = [
        "settings", "stream_notifications_config",
    ]
    db_ids = set()
    for table in server_id_tables:
        try:
            for row in (sb(table).select("server_id").execute().data or []):
                sid = row.get("server_id")
                if sid:
                    db_ids.add(sid)
        except Exception:
            pass
    for table in guild_id_tables:
        try:
            for row in (sb(table).select("guild_id").execute().data or []):
                gid = row.get("guild_id")
                if gid:
                    db_ids.add(gid)
        except Exception:
            pass

    # Nur Server hinzufügen die der Bot auch tatsächlich kennt
    unknown_servers = db_ids - bot_guild_ids
    if unknown_servers:
        log.warning(f"[db] {len(unknown_servers)} Server in DB aber Bot ist nicht drauf: {unknown_servers}")
    ids.update(db_ids)  # Trotzdem alle hinzufügen (MBL braucht sie für Setup)

    result = sorted(ids)
    log.info(f"[db] Gesamt {len(result)} Server-IDs gefunden ({len(bot_guild_ids)} vom Bot, {len(db_ids)} aus DB)")
    return _cset(_perm_cache, "all_server_ids", result)

# ══════════════════════════════════════════════════════════════════════════════
# DB: Berechtigungen pro Server laden (gecacht)
# ══════════════════════════════════════════════════════════════════════════════

def _load_ticket_server_perms(server_id: str) -> dict:
    key = f"tsp:{server_id}"
    hit = _cget(_perm_cache, key, PERM_CACHE_TTL)
    if hit is not _MISS:
        return hit
    try:
        row  = sb("ticket_servers").select("web_admin_role_ids").eq("server_id", server_id).execute()
        data = row.data[0] if row.data else {}
        result = {"web_admin_role_ids": _parse_role_ids(data.get("web_admin_role_ids"))}
    except Exception as e:
        log.error(f"[db] ticket_server_perms({server_id}): {e}")
        result = {"web_admin_role_ids": []}
    return _cset(_perm_cache, key, result)

def _load_module_staff_map(server_id: str) -> dict[str, list[str]]:
    key = f"msm:{server_id}"
    hit = _cget(_perm_cache, key, PERM_CACHE_TTL)
    if hit is not _MISS:
        return hit
    try:
        mods   = sb("ticket_modules").select("id,name").eq("server_id", server_id).execute().data or []
        result = {}
        for mod in mods:
            roles = sb("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute().data or []
            result[mod["name"]] = [r["role_id"] for r in roles]
    except Exception as e:
        log.error(f"[db] module_staff_map({server_id}): {e}")
        result = {}
    return _cset(_perm_cache, key, result)

def _load_app_server_perms(server_id: str) -> dict:
    key = f"asp:{server_id}"
    hit = _cget(_perm_cache, key, PERM_CACHE_TTL)
    if hit is not _MISS:
        return hit
    try:
        row  = sb("application_servers").select("web_admin_role_ids,staff_role_ids").eq("server_id", server_id).execute()
        data = row.data[0] if row.data else {}
        result = {
            "web_admin_role_ids": _parse_role_ids(data.get("web_admin_role_ids")),
            "staff_role_ids":     _parse_role_ids(data.get("staff_role_ids")),
        }
    except Exception as e:
        log.error(f"[db] app_server_perms({server_id}): {e}")
        result = {"web_admin_role_ids": [], "staff_role_ids": []}
    return _cset(_perm_cache, key, result)

# ══════════════════════════════════════════════════════════════════════════════
# Session-Rollen
# ══════════════════════════════════════════════════════════════════════════════

def _get_user_roles_for_server(user: dict, server_id: str) -> set[str]:
    server_roles = user.get("server_roles") or {}
    roles = server_roles.get(server_id, [])
    return {str(r).strip() for r in roles if r}

def _is_mbl(user: dict) -> bool:
    return bool(MBL_ID and str(user.get("id", "")) == MBL_ID)

# ══════════════════════════════════════════════════════════════════════════════
# Login-Ablauf
# ══════════════════════════════════════════════════════════════════════════════

def _build_server_roles(uid: str) -> dict[str, list[str]]:
    db_server_ids = _load_all_server_ids()
    result: dict[str, list[str]] = {}

    # MBL hat automatisch Zugriff auf ALLE Server
    if MBL_ID and str(uid) == MBL_ID:
        log.info(f"[auth] MBL erkannt – gebe Zugriff auf alle {len(db_server_ids)} Server")
        for sid in db_server_ids:
            result[sid] = ["MBL_FULL_ACCESS"]
        return result

    if not BOT_TOKEN:
        log.warning("[auth] BOT_TOKEN fehlt — keine Rollen ladbar")
        return result

    for sid in db_server_ids:
        try:
            # Zuerst prüfen ob der Bot auf dem Server ist
            guild = _cached_guild(sid)
            if guild is None:
                log.debug(f"[auth] Überspringe Server {sid} – Bot ist nicht auf diesem Server")
                continue
            member = _bot_get(f"/guilds/{sid}/members/{uid}")
            if member and isinstance(member.get("roles"), list):
                roles = [str(r) for r in member["roles"]]
                result[sid] = roles
            else:
                log.debug(f"[auth] User {uid} nicht auf Server {sid} oder keine Member-Daten")
        except Exception as e:
            log.error(f"[auth] Fehler beim Laden der Member-Daten für Server {sid}: {e}")

    log.info(f"[auth] User {uid} ist auf {len(result)}/{len(db_server_ids)} konfigurierten Servern")
    return result


def _user_has_dashboard_access(uid: str, server_roles: dict[str, list[str]]) -> bool:
    if _is_mbl({"id": uid}):
        return True

    for sid, roles in server_roles.items():
        role_set = {str(r) for r in roles}
        if not role_set:
            continue

        t_perms = _load_ticket_server_perms(sid)
        if role_set & set(t_perms["web_admin_role_ids"]):
            return True

        staff_map = _load_module_staff_map(sid)
        for role_list in staff_map.values():
            if role_set & set(role_list):
                return True

        a_perms = _load_app_server_perms(sid)
        if role_set & set(a_perms["web_admin_role_ids"]):
            return True
        if role_set & set(a_perms["staff_role_ids"]):
            return True

    try:
        if sb("tickets").select("ticket_id").eq("creator_id", uid).limit(1).execute().data:
            return True
    except Exception as e:
        log.error(f"[auth] Ticket-check: {e}")

    try:
        if sb("applications").select("app_id").eq("creator_id", uid).limit(1).execute().data:
            return True
    except Exception as e:
        log.error(f"[auth] Application-check: {e}")

    try:
        # [FIX MEDIUM] NULL-Check vor .contains() um unerwartete Ergebnisse zu vermeiden
        if (sb("tickets").select("ticket_id")
                .not_.is_("added_users", "null")
                .contains("added_users", [uid])
                .limit(1)
                .execute().data):
            return True
    except Exception as e:
        log.error(f"[auth] added_users-check: {e}")

    log.warning(f"[auth] DENY: {uid} hat keinen Zugriff")
    return False

# ══════════════════════════════════════════════════════════════════════════════
# Berechtigungsprüfungen pro Server
# ══════════════════════════════════════════════════════════════════════════════

def can_see_ticket(user: dict, ticket: dict, server_id: str) -> bool:
    if _is_mbl(user):
        return True

    roles = _get_user_roles_for_server(user, server_id)
    uid   = user.get("id", "")
    tid   = ticket.get("ticket_id") or ticket.get("id", "?")

    t_perms       = _load_ticket_server_perms(server_id)
    web_admin_ids = set(t_perms["web_admin_role_ids"])
    if web_admin_ids and roles & web_admin_ids:
        return True

    module_name  = ticket.get("module", "")
    staff_map    = _load_module_staff_map(server_id)
    module_staff = set(staff_map.get(module_name, []))
    if module_staff and roles & module_staff:
        return True

    if uid and str(ticket.get("creator_id", "")) == uid:
        return True

    if uid and uid in _parse_added_users(ticket.get("added_users")):
        return True

    return False


def can_see_application(user: dict, app: dict, server_id: str) -> bool:
    if _is_mbl(user):
        return True

    roles = _get_user_roles_for_server(user, server_id)
    uid   = user.get("id", "")

    a_perms = _load_app_server_perms(server_id)

    web_admin_ids = set(a_perms["web_admin_role_ids"])
    if web_admin_ids and roles & web_admin_ids:
        return True

    staff_ids = set(a_perms["staff_role_ids"])
    if staff_ids and roles & staff_ids:
        return True

    if uid and str(app.get("creator_id", "")) == uid:
        return True

    return False

# ══════════════════════════════════════════════════════════════════════════════
# Auth Decorator
# ══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized"}), 401
            # [FIX CRITICAL] Nur same-origin URLs in session["next"] speichern
            next_url = request.url
            parsed = url_parse(next_url)
            if parsed.netloc == "" or parsed.netloc == url_parse(WEB_BASE_URL).netloc:
                session["next"] = next_url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

# ══════════════════════════════════════════════════════════════════════════════
# Rollen automatisch frisch halten
# ══════════════════════════════════════════════════════════════════════════════

@app.before_request
def ensure_fresh_roles():
    if "user" not in session:
        return

    user         = session["user"]
    uid          = user.get("id", "")
    roles_loaded = user.get("_roles_loaded_at", 0)
    roles_empty  = not user.get("server_roles")
    roles_stale  = (time.time() - roles_loaded) > ROLES_REFRESH

    if not uid:
        return
    # MBL: Immer Rollen laden (auch ohne BOT_TOKEN)
    is_mbl = _is_mbl(user)
    if not is_mbl and not BOT_TOKEN:
        return
    if not (roles_empty or roles_stale):
        return

    with _cache_lock:
        old_server_roles = user.get("server_roles") or {}
        for sid in old_server_roles:
            _member_cache.pop(f"{sid}:{uid}", None)

    new_server_roles = _build_server_roles(uid)

    session["user"] = {
        **user,
        "server_roles":     new_server_roles,
        "_roles_loaded_at": time.time(),
    }
    session.modified = True

# ══════════════════════════════════════════════════════════════════════════════
# Login / OAuth
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    server_ids = _load_all_server_ids()
    guild = _cached_guild(server_ids[0]) if server_ids else None
    return render_template("index.html",
        user=session.get("user"),
        guild=guild,
        guild_icon=_guild_icon_url(guild),
    )

@app.route("/login")
def login():
    if "user" in session:
        return redirect(url_for("dashboard"))
    if not CLIENT_ID or not CLIENT_SECRET:
        return render_template("login.html", error="Konfigurationsfehler.", oauth_url=None)
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    params = urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  f"{WEB_BASE_URL}/auth/callback",
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    return render_template("login.html",
        oauth_url=f"https://discord.com/oauth2/authorize?{params}", error=None)

@app.route("/auth/callback")
def auth_callback():
    state = request.args.get("state", "")
    if state != session.pop("oauth_state", ""):
        return render_template("login.html", error="Ungültiger State.", oauth_url=None)
    if "error" in request.args:
        return render_template("login.html",
            error=request.args.get("error_description", "Zugriff verweigert."), oauth_url=None)
    code = request.args.get("code", "")
    if not code:
        return render_template("login.html", error="Kein Code erhalten.", oauth_url=None)

    tok = _discord_post("/oauth2/token", {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  f"{WEB_BASE_URL}/auth/callback",
    })
    if not tok or "access_token" not in tok:
        return render_template("login.html", error="Token-Austausch fehlgeschlagen.", oauth_url=None)

    discord_user = _bearer_get("/users/@me", tok["access_token"])
    if not discord_user or "id" not in discord_user:
        return render_template("login.html", error="Profil konnte nicht geladen werden.", oauth_url=None)

    uid  = discord_user["id"]
    av   = discord_user.get("avatar")
    disc = int(discord_user.get("discriminator") or 0) % 5

    server_roles = _build_server_roles(uid)

    if not _is_mbl({"id": uid}) and not _user_has_dashboard_access(uid, server_roles):
        return render_template("login.html",
            error="Kein Zugriff. Du bist auf keinem konfigurierten Server Staff-Mitglied "
                  "und hast keine eigenen Tickets oder Bewerbungen.",
            oauth_url=None)

    display_name = discord_user.get("global_name") or discord_user.get("username", "?")
    for sid in server_roles:
        m = _cached_member(sid, uid)
        if m and m.get("nick"):
            display_name = m["nick"]
            break

    session["user"] = {
        "id":               uid,
        "username":         discord_user.get("global_name") or discord_user.get("username", "?"),
        "display_name":     display_name,
        "avatar":           (f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=64"
                             if av else f"https://cdn.discordapp.com/embed/avatars/{disc}.png"),
        "server_roles":     server_roles,
        "_roles_loaded_at": time.time(),
    }

    # [FIX CRITICAL] Sicher aus session["next"] redirect – nur same-origin
    next_url = session.pop("next", None)
    if next_url:
        parsed = url_parse(next_url)
        wb_parsed = url_parse(WEB_BASE_URL)
        if parsed.netloc and parsed.netloc != wb_parsed.netloc:
            log.warning(f"[auth] Open-Redirect-Versuch blockiert: {next_url}")
            next_url = None

    return redirect(next_url or url_for("dashboard"))

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ══════════════════════════════════════════════════════════════════════════════
# Seiten-Routen
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/dashboard")
@login_required
def dashboard():
    return redirect(url_for("tickets"))

@app.route("/dashboard/tickets")
@login_required
def tickets():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    return render_template("dashboard.html",
        user=user, active_tab="tickets", server_id=server_id)

@app.route("/dashboard/applications")
@login_required
def applications():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    return render_template("dashboard.html",
        user=user, active_tab="applications", server_id=server_id)

@app.route("/dashboard/tickets/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id):
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    return render_template("ticket_view.html",
        user=user, ticket_id=ticket_id, server_id=server_id)

@app.route("/dashboard/applications/<int:app_id>")
@login_required
def application_detail(app_id):
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    return render_template("application_view.html",
        user=user, app_id=app_id, server_id=server_id)

@app.route("/dashboard/user")
@login_required
def user_profile_page():
    user = session["user"]
    return render_template("user_profile.html", user=user)

def _first_accessible_server(user: dict) -> str:
    server_roles = user.get("server_roles") or {}
    if server_roles:
        return next(iter(server_roles))
    return ""

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/guild
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/guild")
@login_required
def api_guild():
    user         = session["user"]
    server_roles = user.get("server_roles") or {}
    user_servers = set(server_roles.keys())

    if _is_mbl(user):
        user_servers = set(_load_all_server_ids())

    def _enrich(rows):
        out = []
        for row in rows:
            sid = row.get("server_id", "")
            if not _is_mbl(user) and sid not in user_servers:
                continue
            g = _cached_guild(sid)
            out.append({
                "server_id":  sid,
                "guild_name": g.get("name", sid) if g else sid,
                "guild_icon": _guild_icon_url(g),
            })
        return out

    try:
        ticket_rows = sb("ticket_servers").select("server_id").execute().data or []
        app_rows    = sb("application_servers").select("server_id").execute().data or []
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        "ticket_servers": _enrich(ticket_rows),
        "app_servers":    _enrich(app_rows),
    })

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/tickets
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tickets")
@login_required
def api_tickets():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    status_f  = request.args.get("status", "")
    module_f  = request.args.get("module", "")
    sort_f    = request.args.get("sort", "newest")
    legacy_f  = request.args.get("legacy", "")

    if not server_id:
        return jsonify({"tickets": [], "modules": []})

    if not _is_mbl(user) and server_id not in (user.get("server_roles") or {}):
        return jsonify({"tickets": [], "modules": [], "error": "Kein Zugriff auf diesen Server"})

    q = sb("tickets").select(
        "ticket_id,title,module,status,creator_id,creator_name,added_users,created_at,imported,import_source"
    ).eq("server_id", server_id)

    if status_f:
        q = q.eq("status", status_f)
    if module_f:
        q = q.eq("module", module_f)

    if legacy_f in ("0", "1"):
        try:
            q = q.eq("imported", legacy_f == "1")
        except Exception as e:
            log.warning(f"[api_tickets] Legacy-Filter fehlgeschlagen: {e}")

    if sort_f == "oldest":
        q = q.order("created_at", desc=False)
    elif sort_f == "name_asc":
        q = q.order("creator_name", desc=False)
    elif sort_f == "name_desc":
        q = q.order("creator_name", desc=True)
    else:
        q = q.order("created_at", desc=True)

    try:
        rows = q.execute().data or []
    except Exception as e:
        log.error(f"[api_tickets] DB-Fehler: {e}")
        if legacy_f in ("0", "1"):
            try:
                q2 = sb("tickets").select(
                    "ticket_id,title,module,status,creator_id,creator_name,added_users,created_at,imported,import_source"
                ).eq("server_id", server_id)
                if status_f:
                    q2 = q2.eq("status", status_f)
                if module_f:
                    q2 = q2.eq("module", module_f)
                if sort_f == "oldest":
                    q2 = q2.order("created_at", desc=False)
                elif sort_f == "name_asc":
                    q2 = q2.order("creator_name", desc=False)
                elif sort_f == "name_desc":
                    q2 = q2.order("creator_name", desc=True)
                else:
                    q2 = q2.order("created_at", desc=True)
                rows = q2.execute().data or []
            except Exception as e2:
                return jsonify({"error": str(e2)}), 500
        else:
            return jsonify({"error": str(e)}), 500

    out     = []
    modules = set()
    for t in rows:
        if t.get("module"):
            modules.add(t["module"])
        if not can_see_ticket(user, t, server_id):
            continue
        tid = t.get("ticket_id")
        out.append({
            "ticket_id":     tid,
            "title":         t.get("title") or f"Ticket #{tid}",
            "module":        t.get("module", ""),
            "status":        t.get("status", "open"),
            "creator_id":    t.get("creator_id", ""),
            "creator_name":  t.get("creator_name") or "Unbekannt",
            "created_at":    (t.get("created_at") or "")[:10],
            "imported":      bool(t.get("imported", False)),
            "import_source": t.get("import_source") or "",
        })

    return jsonify({"tickets": out, "modules": sorted(modules)})

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/tickets/search
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tickets/search")
@login_required
def api_tickets_search():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    q_str     = request.args.get("q", "").strip()

    if not server_id:
        return jsonify({"tickets": [], "error": "Kein Server ausgewählt"})
    if len(q_str) < 2:
        return jsonify({"tickets": []})
    if not _is_mbl(user) and server_id not in (user.get("server_roles") or {}):
        return jsonify({"tickets": [], "error": "Kein Zugriff auf diesen Server"})

    search_pattern = f"%{q_str}%"
    found_ticket_ids: set[int] = set()
    ticket_map: dict[int, dict] = {}

    try:
        for field in ["creator_name", "title", "description"]:
            r = sb("tickets").select(
                "ticket_id,title,module,status,creator_id,creator_name,added_users,created_at,imported,import_source"
            ).eq("server_id", server_id).ilike(field, search_pattern).execute()
            for t in (r.data or []):
                tid = t["ticket_id"]
                found_ticket_ids.add(tid)
                ticket_map[tid] = t
    except Exception as e:
        log.error(f"[tickets_search] Metadaten-Suche: {e}")
        return jsonify({"error": str(e)}), 500

    try:
        r4 = sb("ticket_messages").select("ticket_id")\
            .eq("server_id", server_id).ilike("content", search_pattern).execute()
        msg_ticket_ids = list({row["ticket_id"] for row in (r4.data or [])
                               if row["ticket_id"] not in found_ticket_ids})
        for tid in msg_ticket_ids[:50]:
            if tid in ticket_map:
                continue
            try:
                tr = sb("tickets").select(
                    "ticket_id,title,module,status,creator_id,creator_name,added_users,created_at,imported,import_source"
                ).eq("server_id", server_id).eq("ticket_id", tid).execute()
                if tr.data:
                    ticket_map[tid] = tr.data[0]
                    found_ticket_ids.add(tid)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[tickets_search] Nachrichten-Suche: {e}")

    out = []
    for tid in sorted(found_ticket_ids, reverse=True):
        t = ticket_map.get(tid)
        if not t or not can_see_ticket(user, t, server_id):
            continue
        out.append({
            "ticket_id":     tid,
            "title":         t.get("title") or f"Ticket #{tid}",
            "module":        t.get("module", ""),
            "status":        t.get("status", "open"),
            "creator_id":    t.get("creator_id", ""),
            "creator_name":  t.get("creator_name") or "Unbekannt",
            "created_at":    (t.get("created_at") or "")[:10],
            "imported":      bool(t.get("imported", False)),
            "import_source": t.get("import_source") or "",
        })

    return jsonify({"tickets": out, "query": q_str, "total": len(out)})

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/tickets/<id>
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/tickets/<int:ticket_id>")
@login_required
def api_ticket_detail(ticket_id):
    from bot.features.tickets.storage import load_ticket, load_messages, load_participants
    user = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)

    if not server_id:
        return jsonify({"error": "server_id fehlt"}), 400

    ticket = load_ticket(server_id, ticket_id)
    if not ticket:
        return jsonify({"error": "Ticket nicht gefunden"}), 404

    if not can_see_ticket(user, ticket, server_id):
        return jsonify({"error": "Kein Zugriff auf dieses Ticket"}), 403

    messages = []
    for m in load_messages(server_id, ticket_id):
        messages.append({
            "id": m.get("id"),
            "discord_message_id": m.get("discord_message_id"),
            "timestamp": (m.get("timestamp") or "")[:16].replace("T", " "),
            "content": m.get("content") or m.get("message", ""),
            "attachments": m.get("attachments") or [],
            "user_name": m.get("user", "?"),
            "user_id": m.get("user_id", ""),
            "is_deleted": bool(m.get("is_deleted", False)),
            "deleted_at": (m.get("deleted_at") or "")[:16].replace("T", " ")
            if m.get("deleted_at") else None,
            "edit_history": [
                {
                    "content": e.get("content", ""),
                    "edited_at": (e.get("edited_at") or "")[:16].replace("T", " "),
                }
                for e in (m.get("edit_history") or [])
            ],
        })

    try:
        participants_raw = load_participants(server_id, ticket_id)
        participants = [
            {
                "user_id": p.get("user_id", ""),
                "user_name": p.get("user_name", "?"),
                "avatar_url": p.get("avatar_url"),
                "action": p.get("action", "message"),
                "message_count": p.get("message_count", 0),
                "first_seen": (p.get("first_seen") or "")[:16].replace("T", " "),
                "last_seen": (p.get("last_seen") or "")[:16].replace("T", " "),
            }
            for p in participants_raw
        ]
    except Exception as e:
        log.warning(f"[api_ticket_detail] Participants-Load fehlgeschlagen: {e}")
        participants = []

    tid = ticket.get("ticket_id") or ticket.get("id")
    return jsonify({
        "ticket": {
            "ticket_id": tid,
            "title": ticket.get("title") or f"Ticket #{tid}",
            "module": ticket.get("module", ""),
            "status": ticket.get("status", "open"),
            "description": ticket.get("description", ""),
            "creator_id": ticket.get("creator_id", ""),
            "creator_name": ticket.get("creator_name") or "Unbekannt",
            "claimed_by": ticket.get("claimed_by"),
            "added_users": ticket.get("added_users") or [],
            "created_at": (ticket.get("created_at") or "")[:16].replace("T", " "),
            "closed_at": (ticket.get("closed_at") or "")[:16].replace("T", " "),
            "imported": bool(ticket.get("imported", False)),
            "import_source": ticket.get("import_source") or "",
        },
        "messages": messages,
        "participants": participants,
        "server_id": server_id,
    })

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/applications
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/applications")
@login_required
def api_applications():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    status_f  = request.args.get("status", "")
    sort_f    = request.args.get("sort", "newest")
    legacy_f  = request.args.get("legacy", "")

    if not server_id:
        return jsonify({"applications": []})
    if not _is_mbl(user) and server_id not in (user.get("server_roles") or {}):
        return jsonify({"applications": [], "error": "Kein Zugriff auf diesen Server"})

    q = sb("applications").select(
        "app_id,creator_id,creator_name,minecraft_name,status,created_at,imported,import_source"
    ).eq("server_id", server_id)

    if status_f:
        q = q.eq("status", status_f)
    if legacy_f in ("0", "1"):
        try:
            q = q.eq("imported", legacy_f == "1")
        except Exception as e:
            log.warning(f"[api_applications] Legacy-Filter fehlgeschlagen: {e}")

    if sort_f == "oldest":
        q = q.order("created_at", desc=False)
    elif sort_f == "name_asc":
        q = q.order("creator_name", desc=False)
    elif sort_f == "name_desc":
        q = q.order("creator_name", desc=True)
    else:
        q = q.order("created_at", desc=True)

    try:
        rows = q.execute().data or []
    except Exception as e:
        log.error(f"[api_applications] DB-Fehler: {e}")
        if legacy_f in ("0", "1"):
            try:
                q2 = sb("applications").select(
                    "app_id,creator_id,creator_name,minecraft_name,status,created_at,imported,import_source"
                ).eq("server_id", server_id)
                if status_f:
                    q2 = q2.eq("status", status_f)
                if sort_f == "oldest":
                    q2 = q2.order("created_at", desc=False)
                elif sort_f == "name_asc":
                    q2 = q2.order("creator_name", desc=False)
                elif sort_f == "name_desc":
                    q2 = q2.order("creator_name", desc=True)
                else:
                    q2 = q2.order("created_at", desc=True)
                rows = q2.execute().data or []
            except Exception as e2:
                return jsonify({"error": str(e2)}), 500
        else:
            return jsonify({"error": str(e)}), 500

    out = []
    for a in rows:
        if not can_see_application(user, a, server_id):
            continue
        aid = a.get("app_id") or a.get("id")
        out.append({
            "app_id":         aid,
            "creator_id":     a.get("creator_id", ""),
            "creator_name":   a.get("creator_name") or "Unbekannt",
            "minecraft_name": a.get("minecraft_name", ""),
            "status":         a.get("status", "open"),
            "created_at":     (a.get("created_at") or "")[:10],
            "imported":       bool(a.get("imported", False)),
            "import_source":  a.get("import_source") or "",
        })
    return jsonify({"applications": out})

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/applications/search
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/applications/search")
@login_required
def api_applications_search():
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    q_str     = request.args.get("q", "").strip()

    if not server_id:
        return jsonify({"applications": [], "error": "Kein Server ausgewählt"})
    if len(q_str) < 2:
        return jsonify({"applications": []})
    if not _is_mbl(user) and server_id not in (user.get("server_roles") or {}):
        return jsonify({"applications": [], "error": "Kein Zugriff auf diesen Server"})

    search_pattern = f"%{q_str}%"
    found_app_ids: set[int] = set()
    app_map: dict[int, dict] = {}

    for field in ["creator_name", "minecraft_name", "content"]:
        try:
            r = sb("applications").select(
                "app_id,creator_id,creator_name,minecraft_name,status,created_at,imported,import_source"
            ).eq("server_id", server_id).ilike(field, search_pattern).execute()
            for a in (r.data or []):
                aid = a["app_id"]
                found_app_ids.add(aid)
                app_map[aid] = a
        except Exception as e:
            log.warning(f"[applications_search] Suche in {field}: {e}")

    try:
        r_msg = sb("application_messages").select("app_id")\
            .eq("server_id", server_id).ilike("content", search_pattern).execute()
        msg_app_ids = list({row["app_id"] for row in (r_msg.data or [])
                            if row["app_id"] not in found_app_ids})
        for aid in msg_app_ids[:50]:
            if aid in app_map:
                continue
            try:
                ar = sb("applications").select(
                    "app_id,creator_id,creator_name,minecraft_name,status,created_at,imported,import_source"
                ).eq("server_id", server_id).eq("app_id", aid).execute()
                if ar.data:
                    app_map[aid] = ar.data[0]
                    found_app_ids.add(aid)
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[applications_search] Nachrichten-Suche: {e}")

    out = []
    for aid in sorted(found_app_ids, reverse=True):
        a = app_map.get(aid)
        if not a or not can_see_application(user, a, server_id):
            continue
        out.append({
            "app_id":         aid,
            "creator_id":     a.get("creator_id", ""),
            "creator_name":   a.get("creator_name") or "Unbekannt",
            "minecraft_name": a.get("minecraft_name", ""),
            "status":         a.get("status", "open"),
            "created_at":     (a.get("created_at") or "")[:10],
            "imported":       bool(a.get("imported", False)),
            "import_source":  a.get("import_source") or "",
        })

    return jsonify({"applications": out, "query": q_str, "total": len(out)})

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/applications/<id>
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/applications/<int:app_id>")
@login_required
def api_application_detail(app_id):
    from bot.features.applications.manager import (
        load_application, load_app_messages, load_app_participants
    )
    user = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)

    if not server_id:
        return jsonify({"error": "server_id fehlt"}), 400

    app_data = load_application(server_id, app_id)
    if not app_data:
        return jsonify({"error": "Bewerbung nicht gefunden"}), 404

    if not can_see_application(user, app_data, server_id):
        return jsonify({"error": "Kein Zugriff auf diese Bewerbung"}), 403

    messages = []
    for m in load_app_messages(server_id, app_id):
        messages.append({
            "id": m.get("id"),
            "discord_message_id": m.get("discord_message_id"),
            "timestamp": (m.get("timestamp") or "")[:16].replace("T", " "),
            "content": m.get("content") or m.get("message", ""),
            "attachments": m.get("attachments") or [],
            "user_name": m.get("user", "?"),
            "user_id": m.get("user_id", ""),
            "is_deleted": bool(m.get("is_deleted", False)),
            "deleted_at": (m.get("deleted_at") or "")[:16].replace("T", " ")
            if m.get("deleted_at") else None,
            "edit_history": [
                {
                    "content": e.get("content", ""),
                    "edited_at": (e.get("edited_at") or "")[:16].replace("T", " "),
                }
                for e in (m.get("edit_history") or [])
            ],
        })

    try:
        participants_raw = load_app_participants(server_id, app_id)
        participants = [
            {
                "user_id": p.get("user_id", ""),
                "user_name": p.get("user_name", "?"),
                "avatar_url": p.get("avatar_url"),
                "action": p.get("action", "message"),
                "message_count": p.get("message_count", 0),
                "first_seen": (p.get("first_seen") or "")[:16].replace("T", " "),
                "last_seen": (p.get("last_seen") or "")[:16].replace("T", " "),
            }
            for p in participants_raw
        ]
    except Exception as e:
        log.warning(f"[api_application_detail] Participants-Load fehlgeschlagen: {e}")
        participants = []

    aid = app_data.get("app_id") or app_data.get("id")
    return jsonify({
        "app": {
            "app_id": aid,
            "creator_id": app_data.get("creator_id", ""),
            "creator_name": app_data.get("creator_name") or "Unbekannt",
            "minecraft_name": app_data.get("minecraft_name", ""),
            "status": app_data.get("status", "open"),
            "rejection_reason": app_data.get("rejection_reason", ""),
            "answers": app_data.get("answers") or [],
            "content": app_data.get("content", ""),
            "created_at": (app_data.get("created_at") or "")[:10],
            "closed_at": (app_data.get("closed_at") or "")[:10],
            "claimed_by": app_data.get("claimed_by"),
            "imported": bool(app_data.get("imported", False)),
            "import_source": app_data.get("import_source") or "",
        },
        "messages": messages,
        "participants": participants,
        "server_id": server_id,
    })

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/member
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/member/<user_id>")
@login_required
def api_member(user_id):
    server_id = request.args.get("server_id") or _first_accessible_server(session["user"])
    if not server_id:
        return jsonify({"id": user_id, "name": None, "avatar": None})
    if not _is_mbl(session["user"]) and server_id not in (session["user"].get("server_roles") or {}):
        return jsonify({"id": user_id, "name": None, "avatar": None})
    member    = _cached_member(server_id, user_id) if server_id else None
    return jsonify({
        "id":     user_id,
        "name":   _member_name(member) if member else None,
        "avatar": _member_avatar(member, user_id),
    })

@app.route("/api/members/search")
@login_required
def api_members_search():
    from urllib.parse import quote
    q         = request.args.get("q", "").strip()
    user      = session["user"]
    server_id = request.args.get("server_id") or _first_accessible_server(user)
    if len(q) < 2 or not server_id:
        return jsonify([])
    if not _is_mbl(user) and server_id not in (user.get("server_roles") or {}):
        return jsonify([])

    # MBL searches the entire data set, not just members currently present in
    # the first guild.  This also finds users who left a server but still have
    # tickets, applications or moderation records in the system.
    if _is_mbl(user):
        matches: dict[str, dict] = {}

        def add_match(uid, display, source):
            uid = str(uid or "").strip()
            if not uid or not uid.isdigit():
                return
            entry = matches.setdefault(uid, {"id": uid, "display": "Unbekannt", "avatar": None, "sources": set()})
            if display and entry["display"] == "Unbekannt":
                entry["display"] = str(display)
            entry["sources"].add(source)

        # A numeric Discord ID is a useful direct lookup even if it has not
        # yet occurred in a display-name field.
        if q.isdigit():
            add_match(q, q, "Discord-ID")
        try:
            for row in (sb("tickets").select("creator_id,creator_name").ilike("creator_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("creator_id"), row.get("creator_name"), "Tickets")
            for row in (sb("applications").select("creator_id,creator_name").ilike("creator_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("creator_id"), row.get("creator_name"), "Bewerbungen")
            for row in (sb("moderation_logs").select("target_id,target_name").ilike("target_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("target_id"), row.get("target_name"), "Moderation")
            for row in (sb("minecraft_names").select("user_id,minecraft_name").ilike("minecraft_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("user_id"), row.get("minecraft_name"), "Minecraft")
            for row in (sb("ticket_messages").select("user_id,user_name").ilike("user_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("user_id"), row.get("user_name"), "Ticket-Nachrichten")
            for row in (sb("application_messages").select("user_id,user_name").ilike("user_name", f"%{q}%").limit(20).execute().data or []):
                add_match(row.get("user_id"), row.get("user_name"), "Bewerbungs-Nachrichten")
        except Exception as e:
            log.warning(f"[user_search] Datenbanksuche fehlgeschlagen: {e}")

        # Enrich historical matches from Discord where possible, without
        # dropping records whose Discord member object no longer exists.
        for entry in matches.values():
            for sid in _load_all_server_ids():
                member = _cached_member(sid, entry["id"])
                if member:
                    entry["display"] = _member_name(member, entry["display"])
                    entry["avatar"] = _member_avatar(member, entry["id"])
                    break
            entry["sources"] = sorted(entry["sources"])
        return jsonify(sorted(matches.values(), key=lambda item: item["display"].lower())[:20])

    encoded_q = quote(q, safe="")
    data = _bot_get(f"/guilds/{server_id}/members/search?query={encoded_q}&limit=12")
    if not isinstance(data, list):
        return jsonify([])
    return jsonify([{
        "id":      (m.get("user") or {}).get("id", ""),
        "display": _member_name(m),
        "avatar":  _member_avatar(m),
    } for m in data])

# ══════════════════════════════════════════════════════════════════════════════
# API: Debug (nur MBL)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/debug/permissions")
@login_required
def api_debug_permissions():
    user = session["user"]
    if not _is_mbl(user):
        return jsonify({"error": "Nur für MBL zugänglich"}), 403

    server_roles = user.get("server_roles") or {}
    result       = {}

    for sid in _load_all_server_ids():
        user_role_set = {str(r) for r in server_roles.get(sid, [])}
        t_perms   = _load_ticket_server_perms(sid)
        mod_map   = _load_module_staff_map(sid)
        a_perms   = _load_app_server_perms(sid)
        g         = _cached_guild(sid)

        result[sid] = {
            "guild_name":     g.get("name", sid) if g else sid,
            "user_on_server": sid in server_roles,
            "user_roles":     list(user_role_set),
            "ticket": {
                "web_admin_role_ids": t_perms["web_admin_role_ids"],
                "user_is_web_admin":  bool(user_role_set & set(t_perms["web_admin_role_ids"])),
                "modules": {
                    name: {
                        "staff_role_ids": rids,
                        "user_is_staff":  bool(user_role_set & set(rids)),
                    }
                    for name, rids in mod_map.items()
                },
            },
            "application": {
                "web_admin_role_ids": a_perms["web_admin_role_ids"],
                "staff_role_ids":     a_perms["staff_role_ids"],
                "user_is_web_admin":  bool(user_role_set & set(a_perms["web_admin_role_ids"])),
                "user_is_staff":      bool(user_role_set & set(a_perms["staff_role_ids"])),
            },
        }

    return jsonify({
        "user_id":       user.get("id"),
        "username":      user.get("username"),
        "server_count":  len(server_roles),
        "servers":       result,
    })

@app.route("/api/debug/refresh_roles")
@login_required
def api_refresh_roles():
    user = session["user"]
    if not _is_mbl(user):
        return jsonify({"error": "Nur für MBL zugänglich"}), 403
    uid  = user.get("id", "")

    with _cache_lock:
        for sid in (user.get("server_roles") or {}):
            _member_cache.pop(f"{sid}:{uid}", None)
        _perm_cache.pop("all_server_ids", None)

    new_server_roles = _build_server_roles(uid)
    session["user"] = {
        **user,
        "server_roles":     new_server_roles,
        "_roles_loaded_at": time.time(),
    }
    session.modified = True

    return jsonify({
        "refreshed": True,
        "servers":   {
            sid: {"role_count": len(roles), "roles": roles}
            for sid, roles in new_server_roles.items()
        },
    })

@app.route("/api/debug/servers")
@login_required
def api_debug_servers():
    user = session["user"]
    if not _is_mbl(user):
        return jsonify({"error": "Nur für MBL zugänglich"}), 403
    uid  = user.get("id", "")

    # Cache leeren für frische Daten
    with _cache_lock:
        _perm_cache.pop("all_server_ids", None)
        for sid in (user.get("server_roles") or {}):
            _member_cache.pop(f"{sid}:{uid}", None)
            _guild_cache.pop(sid, None)

    all_ids = _load_all_server_ids()
    server_roles = user.get("server_roles") or {}
    bot_guilds = []
    if BOT_TOKEN:
        try:
            guilds = _bot_get("/users/@me/guilds")
            if isinstance(guilds, list):
                bot_guilds = [{"id": g.get("id"), "name": g.get("name"), "icon": g.get("icon")} for g in guilds]
        except Exception:
            pass

    return jsonify({
        "user_id": uid,
        "username": user.get("username"),
        "db_server_count": len(all_ids),
        "db_server_ids": all_ids,
        "session_server_count": len(server_roles),
        "session_server_ids": list(server_roles.keys()),
        "bot_guild_count": len(bot_guilds),
        "bot_guilds": bot_guilds,
    })

# ══════════════════════════════════════════════════════════════════════════════
# API: /api/user/profile – Komplettes Nutzer-Profil über alle Server
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/api/user/profile")
@login_required
def api_user_profile():
    user      = session["user"]
    target_id = request.args.get("user_id", "").strip()
    if not target_id:
        return jsonify({"error": "user_id fehlt"}), 400

    # Basis-Discord-Daten laden
    target_member = None
    target_user_data = {"id": target_id, "name": None, "avatar": None, "servers": []}

    all_server_ids = _load_all_server_ids()
    server_roles = user.get("server_roles") or {}
    accessible = set(server_roles.keys())
    if _is_mbl(user):
        accessible = set(all_server_ids)

    def set_historical_name(name) -> None:
        """Keep profiles useful when the person has already left Discord."""
        if name and not target_user_data.get("name"):
            target_user_data["name"] = str(name)

    for sid in all_server_ids:
        if sid not in accessible:
            continue
        member = _cached_member(sid, target_id)
        if member:
            if not target_member:
                target_member = member
                u = member.get("user", {})
                av = u.get("avatar")
                disc = int(u.get("discriminator") or 0) % 5
                target_user_data["name"] = member.get("nick") or u.get("global_name") or u.get("username", "Unbekannt")
                target_user_data["avatar"] = (
                    f"https://cdn.discordapp.com/avatars/{target_id}/{av}.png?size=128"
                    if av else f"https://cdn.discordapp.com/embed/avatars/{disc}.png"
                )
                target_user_data["username"] = u.get("username", "")
                target_user_data["global_name"] = u.get("global_name", "")
            g = _cached_guild(sid)
            roles_on_server = [str(r) for r in (member.get("roles") or []) if str(r) != sid]
            role_names = []
            guild_roles = _bot_get(f"/guilds/{sid}/roles") or []
            role_map = {str(r["id"]): r["name"] for r in guild_roles}
            for rid in roles_on_server:
                rn = role_map.get(rid)
                if rn and rn != "@everyone":
                    role_names.append(rn)
            target_user_data["servers"].append({
                "server_id":  sid,
                "server_name": g.get("name", sid) if g else sid,
                "server_icon": _guild_icon_url(g) if g else None,
                "nick":       member.get("nick"),
                "roles":      role_names[:15],
                "joined_at":  member.get("joined_at", "")[:10],
            })

    # ── Tickets (alle Server) ────────────────────────────────────────────────
    tickets_created = []
    tickets_participated = []
    try:
        for sid in accessible:
            # Erstellte Tickets
            rows = sb("tickets").select(
                "ticket_id,title,module,status,creator_id,creator_name,created_at,closed_at,server_id"
            ).eq("server_id", sid).eq("creator_id", target_id).execute().data or []
            for t in rows:
                set_historical_name(t.get("creator_name"))
                g = _cached_guild(sid)
                t["_server_name"] = g.get("name", sid) if g else sid
                tickets_created.append(t)
            # Hinzugefügte Tickets
            rows2 = sb("tickets").select(
                "ticket_id,title,module,status,creator_id,creator_name,added_users,created_at,server_id"
            ).eq("server_id", sid).not_.is_("added_users", "null").execute().data or []
            for t in rows2:
                added = t.get("added_users") or []
                if isinstance(added, str):
                    added = [a.strip() for a in added.split(",") if a.strip()]
                if target_id in added:
                    g = _cached_guild(sid)
                    t["_server_name"] = g.get("name", sid) if g else sid
                    tickets_participated.append(t)
    except Exception as e:
        log.error(f"[user_profile] tickets: {e}")

    # ── Bewerbungen (alle Server) ────────────────────────────────────────────
    applications = []
    try:
        for sid in accessible:
            rows = sb("applications").select(
                "app_id,creator_id,creator_name,minecraft_name,status,created_at,closed_at,server_id"
            ).eq("server_id", sid).eq("creator_id", target_id).execute().data or []
            for a in rows:
                set_historical_name(a.get("creator_name"))
                g = _cached_guild(sid)
                a["_server_name"] = g.get("name", sid) if g else sid
                applications.append(a)
    except Exception as e:
        log.error(f"[user_profile] applications: {e}")

    # ── Moderation (alle Server) ─────────────────────────────────────────────
    moderation = []
    try:
        for sid in accessible:
            rows = sb("moderation_logs").select(
                "id,server_id,action,target_id,target_name,moderator_id,moderator_name,reason,duration_seconds,until,created_at"
            ).eq("server_id", sid).eq("target_id", target_id).order("created_at", desc=True).limit(50).execute().data or []
            for m in rows:
                set_historical_name(m.get("target_name"))
                g = _cached_guild(sid)
                m["_server_name"] = g.get("name", sid) if g else sid
                moderation.append(m)
    except Exception as e:
        log.error(f"[user_profile] moderation: {e}")

    # Cross-server moderation is intentionally represented as aggregate data.
    # It allows MBL to recognise repeat offences without exposing the details
    # of actions from another server in the summary itself.
    system_moderation_summary = {
        "total_actions": len(moderation),
        "ban_count": sum(1 for row in moderation if str(row.get("action", "")).lower() == "ban"),
        "server_count": len({row.get("server_id") for row in moderation if row.get("server_id")}),
    }
    if _is_mbl(user):
        system_moderation_summary = {"total_actions": 0, "ban_count": 0, "server_count": 0}
        try:
            for sid in all_server_ids:
                rows = (sb("moderation_logs").select("action")
                        .eq("server_id", sid).eq("target_id", target_id).execute().data or [])
                if rows:
                    system_moderation_summary["server_count"] += 1
                    system_moderation_summary["total_actions"] += len(rows)
                    system_moderation_summary["ban_count"] += sum(
                        1 for row in rows if str(row.get("action", "")).lower() == "ban"
                    )
        except Exception as e:
            log.error(f"[user_profile] system moderation summary: {e}")

    activity_summary = {"ticket_messages": 0, "application_messages": 0}
    try:
        for sid in accessible:
            ticket_result = (sb("ticket_messages").select("id", count="exact")
                             .eq("server_id", sid).eq("user_id", target_id).execute())
            app_result = (sb("application_messages").select("id", count="exact")
                          .eq("server_id", sid).eq("user_id", target_id).execute())
            activity_summary["ticket_messages"] += ticket_result.count or 0
            activity_summary["application_messages"] += app_result.count or 0
    except Exception as e:
        log.error(f"[user_profile] activity summary: {e}")

    # ── Level (alle Server) ──────────────────────────────────────────────────
    levels = []
    try:
        for sid in accessible:
            row = sb("user_levels").select(
                "user_id,server_id,xp,level,messages,voice_minutes,reactions,updated_at"
            ).eq("user_id", target_id).eq("server_id", sid).execute().data
            if row:
                g = _cached_guild(sid)
                lvl = row[0]
                lvl["_server_name"] = g.get("name", sid) if g else sid
                levels.append(lvl)
    except Exception as e:
        log.error(f"[user_profile] levels: {e}")

    # ── Geburtstage ──────────────────────────────────────────────────────────
    birthdays = []
    try:
        for sid in accessible:
            row = sb("birthdays").select(
                "user_id,server_id,birthday"
            ).eq("user_id", target_id).eq("server_id", sid).execute().data
            if row:
                g = _cached_guild(sid)
                bday = row[0]
                bday["_server_name"] = g.get("name", sid) if g else sid
                birthdays.append(bday)
    except Exception as e:
        log.error(f"[user_profile] birthdays: {e}")

    # ── Minecraft-Namen ──────────────────────────────────────────────────────
    mc_names = []
    try:
        for sid in accessible:
            row = sb("minecraft_names").select(
                "user_id,server_id,minecraft_name,created_at"
            ).eq("user_id", target_id).eq("server_id", sid).execute().data
            if row:
                g = _cached_guild(sid)
                mc = row[0]
                mc["_server_name"] = g.get("name", sid) if g else sid
                mc_names.append(mc)
    except Exception as e:
        log.error(f"[user_profile] mc_names: {e}")

    target_user_data["tickets_created"]     = tickets_created
    target_user_data["tickets_participated"] = tickets_participated
    target_user_data["applications"]         = applications
    target_user_data["moderation"]           = moderation
    target_user_data["system_moderation_summary"] = system_moderation_summary
    target_user_data["activity_summary"]     = activity_summary
    target_user_data["levels"]               = levels
    target_user_data["birthdays"]            = birthdays
    target_user_data["mc_names"]             = mc_names

    return jsonify(target_user_data)

# ══════════════════════════════════════════════════════════════════════════════
# Error Handler
# ══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(403)
def forbidden(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Kein Zugriff"}), 403
    return render_template("error.html", code=403, title="Kein Zugriff",
                           icon="🚫", msg="Du hast keine Berechtigung."), 403

@app.errorhandler(404)
def not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Nicht gefunden"}), 404
    return render_template("error.html", code=404, title="Nicht gefunden",
                           icon="🔍", msg="Diese Seite existiert nicht."), 404


from .import_routes import register_import_routes
register_import_routes(app, login_required, _is_mbl, MBL_ID)

from .ticket_setup_routes import register_ticket_setup_routes
register_ticket_setup_routes(
    app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url,
)

from .application_setup_routes import register_application_setup_routes
register_application_setup_routes(
    app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url,
)

from .voice_setup_routes import register_voice_setup_routes
register_voice_setup_routes(
    app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url,
)
from .voting_routes import register_voting_routes
register_voting_routes(app)

from .legal_routes import register_legal_routes
register_legal_routes(app)

from .feature_setup_routes import register_feature_setup_routes
register_feature_setup_routes(
    app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url,
)
