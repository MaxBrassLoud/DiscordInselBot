# bot/core/web_app/flask_app/open_api_routes.py
"""
open_api_routes.py
===================
Öffentliche API mit API-Key-Authentifizierung.

ENDPUNKTE:
  GET  /open-api/users?role=<role_id>&online=<true|false>
  GET  /open-api/users?role=<role_id>&guild_id=<guild_id>&online=true
  GET  /open-api/guild?guild_id=<guild_id>
  GET  /open-api/guild/roles?guild_id=<guild_id>
  GET  /open-api/leaderboard?guild_id=<guild_id>&limit=25&offset=0
  GET  /open-api/events?guild_id=<guild_id>&status=upcoming&limit=10
  GET  /open-api/version
  GET  /open-api/whoami
  GET  /open-api/health
  GET  /open-api/_debug/bot          (Diagnose)

AUTHENTIFIZIERUNG:
  Header:  X-API-Key: insel_xxxxxxxxxxxx
  ODER:    Authorization: Bearer insel_xxxxxxxxxxxx
  ODER:    ?api_key=insel_xxxxxxxxxxxx   (nur wenn Header nicht möglich)

RATE-LIMIT:
  Pro API-Key, Standard 60 Anfragen/Minute (konfigurierbar pro Key).

BEISPIEL:
  curl -H "X-API-Key: insel_..." \\
       "https://domain.com/open-api/users?role=1410628676499800196&online=true"
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from functools import wraps

from flask import jsonify, request

from bot.core.supabase_client import get_supabase

log = logging.getLogger("open_api")

# API-Version – bei inkompatiblen Änderungen erhöhen
API_VERSION  = "1.0.0"
BOT_VERSION  = "1.4.2"


# ══════════════════════════════════════════════════════════════════════════════
# BOT-INSTANZ AUFLÖSEN
# ══════════════════════════════════════════════════════════════════════════════

def _get_bot():
    """Holt die Bot-Instanz über den Getter aus app.py (lazy import)."""
    try:
        from bot.core.web_app.flask_app.app import get_bot_instance
    except Exception as e:
        log.warning(f"[open_api] Konnte get_bot_instance nicht importieren: {e}")
        return None
    try:
        return get_bot_instance()
    except Exception as e:
        log.warning(f"[open_api] get_bot_instance() warf Exception: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# RATE-LIMIT (in-memory, pro API-Key)
# ══════════════════════════════════════════════════════════════════════════════

_rate_buckets: dict[str, list[float]] = defaultdict(list)
_rate_lock = threading.Lock()
_RATE_WINDOW = 60.0


def _check_rate_limit(key_hash: str, limit_per_minute: int) -> bool:
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[key_hash]
        bucket[:] = [t for t in bucket if now - t < _RATE_WINDOW]
        if len(bucket) >= limit_per_minute:
            return False
        bucket.append(now)
        return True


# ══════════════════════════════════════════════════════════════════════════════
# API-KEY HANDLING
# ══════════════════════════════════════════════════════════════════════════════

def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _extract_api_key() -> str | None:
    key = request.headers.get("X-API-Key")
    if key:
        return key.strip()
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    key = request.args.get("api_key")
    if key:
        return key.strip()
    return None


def _load_api_key(key_hash: str) -> dict | None:
    try:
        sb = get_supabase()
        r = sb.table("api_keys").select("*") \
            .eq("key_hash", key_hash) \
            .eq("enabled", True) \
            .limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        log.error(f"[open_api] DB-Fehler beim Laden des API-Keys: {e}")
        return None


def _update_last_used(key_id: int):
    try:
        get_supabase().table("api_keys").update({
            "last_used_at": datetime.now(timezone.utc).isoformat()
        }).eq("id", key_id).execute()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# DECORATOR
# ══════════════════════════════════════════════════════════════════════════════

def require_api_key(scope: str = "users.read"):
    """Decorator: prüft API-Key, Rate-Limit und Scope."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            raw = _extract_api_key()
            if not raw:
                return jsonify({
                    "error": "API key required",
                    "hint":  "Sende 'X-API-Key: insel_...' oder '?api_key=insel_...'",
                }), 401

            key_hash = _hash_key(raw)
            data = _load_api_key(key_hash)
            if not data:
                time.sleep(0.05)
                return jsonify({"error": "Invalid or disabled API key"}), 401

            if data.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp < datetime.now(timezone.utc):
                        return jsonify({"error": "API key expired"}), 401
                except Exception:
                    pass

            limit = int(data.get("rate_limit") or 60)
            if not _check_rate_limit(key_hash, limit):
                return jsonify({
                    "error": "Rate limit exceeded",
                    "limit_per_minute": limit,
                }), 429

            scopes = {s.strip() for s in (data.get("scopes") or "").split(",") if s.strip()}
            if scope and scope not in scopes and "admin" not in scopes:
                return jsonify({
                    "error": f"Missing scope '{scope}'",
                    "your_scopes": sorted(scopes),
                }), 403

            _update_last_used(data["id"])
            request.api_key_data = data  # type: ignore[attr-defined]
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _guild_icon_url(guild) -> str | None:
    """Icon-URL eines discord.Guild-Objekts."""
    if guild is None or guild.icon is None:
        return None
    return str(guild.icon.url)


def _resolve_guild_id(requested: str | None, api_key: dict) -> tuple[str | None, tuple | None]:
    """
    Bestimmt die effektive guild_id.

    Rückgabe: (guild_id, error_response)
      - guild_id gesetzt   → (id, None)
      - error_response     → (None, (jsonify(...), status))
    """
    key_guild = api_key.get("guild_id")

    if key_guild and requested and str(key_guild) != str(requested):
        return None, (jsonify({
            "error": "API key is not valid for this guild",
            "key_guild": key_guild,
        }), 403)

    gid = str(requested or key_guild or "").strip()
    if not gid:
        return None, (jsonify({
            "error": "Missing parameter 'guild_id' (API key is not guild-bound)",
        }), 400)

    if not gid.isdigit():
        return None, (jsonify({
            "error": "'guild_id' must be a numeric Discord snowflake",
        }), 400)

    return gid, None


def _parse_positive_int(raw, default: int, minimum: int = 0, maximum: int = 100) -> int:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, v))


# ══════════════════════════════════════════════════════════════════════════════
# ROUTE-REGISTRIERUNG
# ══════════════════════════════════════════════════════════════════════════════

def register_open_api_routes(app):
    """Registriert die öffentlichen API-Routen bei der Flask-App."""

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/users
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/users", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_users():
        api_key = request.api_key_data  # type: ignore[attr-defined]

        role_id  = (request.args.get("role") or "").strip()
        online_s = (request.args.get("online") or "").strip().lower()
        guild_id_req = (request.args.get("guild_id") or "").strip()

        if not role_id or not role_id.isdigit():
            return jsonify({
                "error": "Missing or invalid parameter 'role'",
                "example": "/open-api/users?role=1410628676499800196&online=true",
            }), 400

        guild_id, err = _resolve_guild_id(guild_id_req, api_key)
        if err:
            return err

        online_only = online_s in ("1", "true", "yes", "ja")

        bot = _get_bot()
        if bot is None:
            return jsonify({
                "error": "Bot not initialized",
                "hint": "Die Discord-Bot-Instanz wurde noch nicht an die Flask-App übergeben.",
            }), 503

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return jsonify({
                "error": "Guild not found or bot not on this guild",
                "guild_id": guild_id,
            }), 404

        role = guild.get_role(int(role_id))
        if role is None:
            return jsonify({
                "error": "Role not found",
                "role_id": role_id,
                "guild_id": guild_id,
            }), 404

        try:
            result = []
            for m in guild.members:
                if role not in m.roles:
                    continue
                status = str(m.status)
                is_online = status in ("online", "idle", "dnd")
                if online_only and not is_online:
                    continue
                result.append({
                    "id":           str(m.id),
                    "username":     m.name,
                    "display_name": m.display_name,
                    "global_name":  getattr(m, "global_name", None),
                    "nickname":     m.nick,
                    "avatar_url":   str(m.display_avatar.url) if m.display_avatar else None,
                    "status":       status,
                    "is_online":    is_online,
                    "joined_at":    m.joined_at.isoformat() if m.joined_at else None,
                    "role_ids":     [str(r.id) for r in m.roles if r.name != "@everyone"],
                    "is_bot":       m.bot,
                })

            result.sort(key=lambda x: (not x["is_online"], x["display_name"].lower()))

            return jsonify({
                "ok":           True,
                "guild_id":     guild_id,
                "guild_name":   guild.name,
                "role_id":      role_id,
                "role_name":    role.name,
                "online_only":  online_only,
                "count":        len(result),
                "users":        result,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            log.exception("[open_api] Fehler beim Filtern der Mitglieder")
            return jsonify({"error": f"Internal error: {e}"}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/guild   (Route 1)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/guild", methods=["GET"])
    @require_api_key(scope="guild.read")
    def open_api_guild():
        api_key = request.api_key_data  # type: ignore[attr-defined]
        guild_id_req = (request.args.get("guild_id") or "").strip()

        guild_id, err = _resolve_guild_id(guild_id_req, api_key)
        if err:
            return err

        bot = _get_bot()
        if bot is None:
            return jsonify({"error": "Bot not initialized"}), 503

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return jsonify({
                "error": "Guild not found or bot not on this guild",
                "guild_id": guild_id,
            }), 404

        # Online-Zähler
        online_count = sum(
            1 for m in guild.members
            if str(m.status) in ("online", "idle", "dnd") and not m.bot
        )

        # Boost-Tier lesen (discord.py: guild.premium_tier)
        boost_tier = getattr(guild, "premium_tier", 0)
        boost_count = getattr(guild, "premium_subscription_count", 0) or 0

        owner_id = str(guild.owner_id) if guild.owner_id else None

        return jsonify({
            "ok":            True,
            "id":            str(guild.id),
            "name":          guild.name,
            "description":   guild.description,
            "icon_url":      _guild_icon_url(guild),
            "member_count":  guild.member_count,
            "human_count":   sum(1 for m in guild.members if not m.bot),
            "bot_count":     sum(1 for m in guild.members if m.bot),
            "online_count":  online_count,
            "boost_tier":    boost_tier,
            "boost_count":   boost_count,
            "created_at":    guild.created_at.isoformat() if guild.created_at else None,
            "owner_id":      owner_id,
            "owner_name":    str(guild.owner) if guild.owner else None,
            "preferred_locale": str(guild.preferred_locale) if guild.preferred_locale else None,
            "verification_level": str(guild.verification_level),
            "features":      list(guild.features) if guild.features else [],
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/guild/roles   (Route 2)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/guild/roles", methods=["GET"])
    @require_api_key(scope="guild.read")
    def open_api_guild_roles():
        api_key = request.api_key_data  # type: ignore[attr-defined]
        guild_id_req = (request.args.get("guild_id") or "").strip()
        single_role  = (request.args.get("role_id") or "").strip()

        guild_id, err = _resolve_guild_id(guild_id_req, api_key)
        if err:
            return err

        bot = _get_bot()
        if bot is None:
            return jsonify({"error": "Bot not initialized"}), 503

        guild = bot.get_guild(int(guild_id))
        if guild is None:
            return jsonify({
                "error": "Guild not found or bot not on this guild",
                "guild_id": guild_id,
            }), 404

        # Anzahl Mitglieder pro Rolle in einem Durchlauf
        role_counts: dict[str, int] = {}
        for m in guild.members:
            for r in m.roles:
                role_counts[str(r.id)] = role_counts.get(str(r.id), 0) + 1

        def _role_payload(r) -> dict:
            return {
                "id":           str(r.id),
                "name":         r.name,
                "color":        f"#{r.color.value:06X}" if r.color.value else None,
                "position":     r.position,
                "member_count": role_counts.get(str(r.id), 0),
                "mentionable":  r.mentionable,
                "hoist":        r.hoist,
                "is_managed":   r.managed,
                "is_default":   r.is_default(),
                "created_at":   r.created_at.isoformat() if r.created_at else None,
            }

        # Einzelne Rolle
        if single_role:
            if not single_role.isdigit():
                return jsonify({"error": "'role_id' must be numeric"}), 400
            role = guild.get_role(int(single_role))
            if role is None:
                return jsonify({"error": "Role not found", "role_id": single_role}), 404
            return jsonify({"ok": True, "role": _role_payload(role)})

        # Alle Rollen (@everyone zuerst ausschließen, dann nach Position desc)
        roles = [r for r in guild.roles if r.name != "@everyone"]
        roles.sort(key=lambda r: r.position, reverse=True)

        return jsonify({
            "ok":           True,
            "guild_id":     guild_id,
            "count":        len(roles),
            "roles":        [_role_payload(r) for r in roles],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/leaderboard   (Route 5)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/leaderboard", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_leaderboard():
        api_key = request.api_key_data  # type: ignore[attr-defined]
        guild_id_req = (request.args.get("guild_id") or "").strip()

        guild_id, err = _resolve_guild_id(guild_id_req, api_key)
        if err:
            return err

        limit  = _parse_positive_int(request.args.get("limit"),  default=25, minimum=1, maximum=100)
        offset = _parse_positive_int(request.args.get("offset"), default=0,  minimum=0, maximum=10000)

        try:
            sb = get_supabase()
            rows = (
                sb.table("user_levels")
                .select("user_id,xp,level,messages,voice_minutes,reactions")
                .eq("server_id", guild_id)
                .order("xp", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
                .data or []
            )

            # Gesamtzahl für Pagination
            try:
                total_r = (
                    sb.table("user_levels")
                    .select("id", count="exact")
                    .eq("server_id", guild_id)
                    .execute()
                )
                total_entries = total_r.count or len(rows)
            except Exception:
                total_entries = len(rows)

        except Exception as e:
            log.error(f"[open_api] leaderboard DB-Fehler: {e}")
            return jsonify({"error": "Leaderboard konnte nicht geladen werden."}), 500

        bot = _get_bot()
        guild = bot.get_guild(int(guild_id)) if bot else None

        # Discord-Display-Namen anreichern
        out = []
        for idx, row in enumerate(rows):
            uid = str(row.get("user_id") or "")
            rank = offset + idx + 1
            display_name = None
            avatar_url   = None
            if guild and uid.isdigit():
                m = guild.get_member(int(uid))
                if m:
                    display_name = m.display_name
                    avatar_url   = str(m.display_avatar.url) if m.display_avatar else None

            out.append({
                "rank":          rank,
                "user_id":       uid,
                "display_name":  display_name or uid,
                "avatar_url":    avatar_url,
                "level":         int(row.get("level") or 0),
                "xp":            int(row.get("xp") or 0),
                "messages":      int(row.get("messages") or 0),
                "voice_minutes": int(row.get("voice_minutes") or 0),
                "reactions":     int(row.get("reactions") or 0),
            })

        return jsonify({
            "ok":            True,
            "guild_id":      guild_id,
            "limit":         limit,
            "offset":        offset,
            "count":         len(out),
            "total_entries": total_entries,
            "top":           out,
            "generated_at":  datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/events   (Route 6)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/events", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_events():
        api_key = request.api_key_data  # type: ignore[attr-defined]
        guild_id_req = (request.args.get("guild_id") or "").strip()
        status_f     = (request.args.get("status") or "").strip().lower()

        guild_id, err = _resolve_guild_id(guild_id_req, api_key)
        if err:
            return err

        limit = _parse_positive_int(request.args.get("limit"), default=10, minimum=1, maximum=50)

        valid_statuses = {"upcoming", "live", "open_end", "delayed", "cancelled", "ended", "tba"}
        if status_f and status_f not in valid_statuses:
            return jsonify({
                "error": f"Invalid status '{status_f}'",
                "valid_statuses": sorted(valid_statuses),
            }), 400

        try:
            sb = get_supabase()
            q = sb.table("events").select(
                "id,guild_id,title,description,start_time,end_time,status,followers,"
                "creator_id,archived,end_open,channel_id,thread_id,message_id"
            ).eq("guild_id", guild_id).eq("archived", False)

            if status_f:
                q = q.eq("status", status_f)

            # Für "upcoming": nach Startzeit aufsteigend (nächste zuerst)
            # Sonst: neueste zuerst
            if status_f in ("upcoming", "tba") or not status_f:
                q = q.order("start_time", desc=False)
            else:
                q = q.order("start_time", desc=True)

            rows = q.limit(limit).execute().data or []
        except Exception as e:
            log.error(f"[open_api] events DB-Fehler: {e}")
            return jsonify({"error": "Events konnten nicht geladen werden."}), 500

        out = []
        now = datetime.now(timezone.utc)
        for ev in rows:
            start_iso = ev.get("start_time")
            end_iso   = ev.get("end_time")
            start_dt  = None
            end_dt    = None
            try:
                if start_iso:
                    start_dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
            except Exception:
                start_dt = None
            try:
                if end_iso:
                    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                    if end_dt.tzinfo is None:
                        end_dt = end_dt.replace(tzinfo=timezone.utc)
            except Exception:
                end_dt = None

            followers = ev.get("followers") or []
            if isinstance(followers, str):
                followers = []

            out.append({
                "id":             str(ev.get("id")),
                "title":          ev.get("title"),
                "description":    ev.get("description") or "",
                "status":         ev.get("status") or "upcoming",
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat() if end_dt else None,
                "end_open":       bool(ev.get("end_open")),
                "follower_count": len(followers),
                "starts_in_seconds": int((start_dt - now).total_seconds()) if start_dt and start_dt > now else None,
                "has_started":    bool(start_dt and start_dt <= now),
                "created_at":     ev.get("created_at"),
            })

        return jsonify({
            "ok":           True,
            "guild_id":     guild_id,
            "status_filter": status_f or None,
            "count":        len(out),
            "events":       out,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/version   (Route 14)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/version", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_version():
        return jsonify({
            "ok":               True,
            "api_version":      API_VERSION,
            "bot_version":      BOT_VERSION,
            "features": [
                "users",
                "guild",
                "guild.roles",
                "leaderboard",
                "events",
            ],
            "scopes_available": [
                "users.read",
                "guild.read",
                "stats.read",
                "admin",
            ],
            "endpoints": [
                "/open-api/users",
                "/open-api/guild",
                "/open-api/guild/roles",
                "/open-api/leaderboard",
                "/open-api/events",
                "/open-api/version",
                "/open-api/whoami",
                "/open-api/health",
            ],
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/whoami   (Route 15)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/whoami", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_whoami():
        api_key = request.api_key_data  # type: ignore[attr-defined]
        scopes = sorted({s.strip() for s in (api_key.get("scopes") or "").split(",") if s.strip()})

        return jsonify({
            "ok":              True,
            "label":           api_key.get("label"),
            "guild_bound":     api_key.get("guild_id"),
            "scopes":          scopes,
            "rate_limit":      int(api_key.get("rate_limit") or 60),
            "enabled":         bool(api_key.get("enabled", True)),
            "expires_at":      api_key.get("expires_at"),
            "last_used_at":    api_key.get("last_used_at"),
            "created_at":      api_key.get("created_at"),
            "generated_at":    datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/health
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/health", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_health():
        bot = _get_bot()
        return jsonify({
            "ok":          True,
            "bot_ready":   bool(bot and bot.is_ready()),
            "bot_user":    str(bot.user) if (bot and bot.user) else None,
            "guild_count": len(bot.guilds) if bot else 0,
            "api_version": API_VERSION,
            "bot_version": BOT_VERSION,
            "time":        datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/_debug/bot
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/_debug/bot", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_debug_bot():
        import importlib
        info: dict = {
            "app_attrs":   {},
            "module_vars": {},
            "resolved":    None,
        }

        for attr in ("_bot_instance", "bot", "discord_bot", "bot_instance"):
            b = getattr(app, attr, None)
            info["app_attrs"][attr] = repr(b) if b is not None else None

        try:
            app_mod = importlib.import_module("bot.core.web_app.flask_app.app")
            for attr in ("_bot_instance", "bot", "bot_instance"):
                b = getattr(app_mod, attr, None)
                info["module_vars"][attr] = repr(b) if b is not None else None
        except Exception as e:
            info["module_vars"]["error"] = str(e)

        resolved = _get_bot()
        info["resolved"]    = repr(resolved) if resolved is not None else None
        info["resolved_ok"] = resolved is not None

        return jsonify(info)
    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/endpoints
    # Liefert eine maschinenlesbare Beschreibung aller Endpunkte.
    # Wird vom API-Tester genutzt, um sein Dropdown zu füllen.
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/endpoints", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_endpoints():
        """
        Strukturierte Endpunkt-Beschreibung.  Ein Eintrag enthält alles,
        was ein generischer Client braucht, um die Route aufzurufen:

          id          – eindeutiger Bezeichner
          label       – menschenlesbarer Name (für Dropdown)
          method      – HTTP-Methode
          path        – Pfad relativ zur Basis-URL (kann <platzhalter> enthalten)
          scope       – benötigter Scope
          description – kurze Erklärung
          params      – Liste von Query-Parametern
                          name, required, type, default, description, example
          placeholders – (nur bei Pfad-Platzhaltern) gleiche Struktur wie params
          tags        – Kategorien wie ['users','admin']
        """
        endpoints = [
            # ── Users ─────────────────────────────────────────────────────
            {
                "id":          "users.list",
                "label":       "👥  Users mit Rolle",
                "method":      "GET",
                "path":        "/open-api/users",
                "scope":       "users.read",
                "description": "Liste aller User mit einer bestimmten Rolle. "
                               "Optional nur die, die gerade online sind.",
                "tags":        ["users"],
                "params": [
                    {"name": "role",     "required": True,  "type": "snowflake",
                     "description": "Discord-Rollen-ID",
                     "example": "1410628676499800196"},
                    {"name": "online",   "required": False, "type": "bool",
                     "default": "false",
                     "description": "Nur online User zurückgeben",
                     "example": "true"},
                    {"name": "guild_id", "required": False, "type": "snowflake",
                     "description": "Server-ID (nur nötig, wenn Key nicht servergebunden)",
                     "example": "1253751493513969735"},
                ],
                "placeholders": [],
            },
            # ── Guild ─────────────────────────────────────────────────────
            {
                "id":          "guild.info",
                "label":       "🏝️  Server-Info",
                "method":      "GET",
                "path":        "/open-api/guild",
                "scope":       "guild.read",
                "description": "Grundlegende Informationen zum Discord-Server.",
                "tags":        ["guild"],
                "params": [
                    {"name": "guild_id", "required": False, "type": "snowflake",
                     "description": "Server-ID",
                     "example": "1253751493513969735"},
                ],
                "placeholders": [],
            },
            {
                "id":          "guild.roles",
                "label":       "🎭  Rollen-Liste",
                "method":      "GET",
                "path":        "/open-api/guild/roles",
                "scope":       "guild.read",
                "description": "Alle Rollen des Servers inkl. Mitgliederzahl pro Rolle. "
                               "Mit 'role_id' wird nur diese eine Rolle zurückgegeben.",
                "tags":        ["guild"],
                "params": [
                    {"name": "guild_id", "required": False, "type": "snowflake",
                     "description": "Server-ID",
                     "example": "1253751493513969735"},
                    {"name": "role_id",  "required": False, "type": "snowflake",
                     "description": "Optional: nur diese Rolle zurückgeben",
                     "example": "1410628676499800196"},
                ],
                "placeholders": [],
            },
            # ── Community ─────────────────────────────────────────────────
            {
                "id":          "leaderboard.list",
                "label":       "🏆  Level-Leaderboard",
                "method":      "GET",
                "path":        "/open-api/leaderboard",
                "scope":       "users.read",
                "description": "Top-Mitglieder nach XP.",
                "tags":        ["community"],
                "params": [
                    {"name": "guild_id", "required": False, "type": "snowflake",
                     "description": "Server-ID", "example": "1253751493513969735"},
                    {"name": "limit",    "required": False, "type": "int",
                     "default": "25", "description": "Anzahl (1-100)", "example": "25"},
                    {"name": "offset",   "required": False, "type": "int",
                     "default": "0",  "description": "Pagination-Offset", "example": "0"},
                ],
                "placeholders": [],
            },
            {
                "id":          "events.list",
                "label":       "📅  Events",
                "method":      "GET",
                "path":        "/open-api/events",
                "scope":       "users.read",
                "description": "Events des Servers, optional nach Status gefiltert.",
                "tags":        ["community"],
                "params": [
                    {"name": "guild_id", "required": False, "type": "snowflake",
                     "description": "Server-ID", "example": "1253751493513969735"},
                    {"name": "status",   "required": False, "type": "enum",
                     "description": "upcoming | live | open_end | delayed | cancelled | ended | tba",
                     "example": "upcoming"},
                    {"name": "limit",    "required": False, "type": "int",
                     "default": "10", "description": "Anzahl (1-50)", "example": "10"},
                ],
                "placeholders": [],
            },
            # ── Meta / Diagnose ───────────────────────────────────────────
            {
                "id":          "meta.version",
                "label":       "📌  API-Version",
                "method":      "GET",
                "path":        "/open-api/version",
                "scope":       "users.read",
                "description": "API- und Bot-Version, Liste aller verfügbaren Endpunkte.",
                "tags":        ["meta"],
                "params": [],
                "placeholders": [],
            },
            {
                "id":          "meta.whoami",
                "label":       "🔑  Wer bin ich?",
                "method":      "GET",
                "path":        "/open-api/whoami",
                "scope":       "users.read",
                "description": "Zeigt die Metadaten des verwendeten API-Keys.",
                "tags":        ["meta"],
                "params": [],
                "placeholders": [],
            },
            {
                "id":          "meta.health",
                "label":       "💚  Health-Check",
                "method":      "GET",
                "path":        "/open-api/health",
                "scope":       "users.read",
                "description": "Bot-Status und Verbindungs-Check.",
                "tags":        ["meta"],
                "params": [],
                "placeholders": [],
            },
            {
                "id":          "meta.endpoints",
                "label":       "📋  Endpunkt-Liste",
                "method":      "GET",
                "path":        "/open-api/endpoints",
                "scope":       "users.read",
                "description": "Diese Übersicht – maschinenlesbare Endpunkt-Beschreibung.",
                "tags":        ["meta"],
                "params": [],
                "placeholders": [],
            },
            {
                "id":          "meta.debug_bot",
                "label":       "🐞  Debug: Bot-Instanz",
                "method":      "GET",
                "path":        "/open-api/_debug/bot",
                "scope":       "users.read",
                "description": "Zeigt, wo die Bot-Instanz im Flask-Kontext liegt.",
                "tags":        ["meta", "debug"],
                "params": [],
                "placeholders": [],
            },
        ]

        return jsonify({
            "ok":           True,
            "api_version":  API_VERSION,
            "bot_version":  BOT_VERSION,
            "base_url":     request.host_url.rstrip("/"),
            "count":        len(endpoints),
            "endpoints":    endpoints,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })

    log.info(
        "✅ Open-API Routen registriert: "
        "/open-api/users, /open-api/guild, /open-api/guild/roles, "
        "/open-api/leaderboard, /open-api/events, /open-api/version, "
        "/open-api/whoami, /open-api/health, /open-api/_debug/bot"
    )