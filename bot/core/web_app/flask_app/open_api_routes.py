# bot/core/web_app/flask_app/open_api_routes.py
"""
open_api_routes.py
===================
Öffentliche API mit API-Key-Authentifizierung.

ENDPUNKTE:
  GET  /open-api/users?role=<role_id>&online=<true|false>
  GET  /open-api/users?role=<role_id>&guild_id=<guild_id>&online=true
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


# ══════════════════════════════════════════════════════════════════════════════
# BOT-INSTANZ AUFLÖSEN
# ══════════════════════════════════════════════════════════════════════════════

def _get_bot():
    """
    Liefert die Discord-Bot-Instanz über den offiziellen Getter aus app.py.

    WICHTIG: 'app.py' importiert dieses Modul beim Start.  Ein Import auf
    Modulebene würde einen Zyklus bilden – deshalb wird der Getter hier
    verzögert (lazy) aufgelöst.  Beim ersten Request ist 'app.py' vollständig
    geladen und der Getter liefert die Instanz.

    Der Getter heißt in app.py 'get_bot_instance()'.  Falls das Projekt
    umbenannt wird, kann zusätzlich ein '__getattr__'-Fallback genutzt werden.
    """
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
_RATE_WINDOW = 60.0  # Sekunden


def _check_rate_limit(key_hash: str, limit_per_minute: int) -> bool:
    """True = erlaubt, False = Limit überschritten."""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets[key_hash]
        # Alte Einträge entfernen
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
    """Liest den API-Key aus Header oder Query-Parameter."""
    # 1. X-API-Key Header
    key = request.headers.get("X-API-Key")
    if key:
        return key.strip()
    # 2. Authorization: Bearer <key>
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    # 3. Query-Parameter (Fallback)
    key = request.args.get("api_key")
    if key:
        return key.strip()
    return None


def _load_api_key(key_hash: str) -> dict | None:
    """Lädt den API-Key-Datensatz aus der DB."""
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
    """
    Decorator: prüft API-Key, Rate-Limit und Scope.
    Setzt request.api_key_data = {id, guild_id, scopes, ...}
    """
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
                # Verzögerung gegen Timing-Angriffe
                time.sleep(0.05)
                return jsonify({"error": "Invalid or disabled API key"}), 401

            # Ablaufdatum
            if data.get("expires_at"):
                try:
                    exp = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
                    if exp.tzinfo is None:
                        exp = exp.replace(tzinfo=timezone.utc)
                    if exp < datetime.now(timezone.utc):
                        return jsonify({"error": "API key expired"}), 401
                except Exception:
                    pass

            # Rate-Limit
            limit = int(data.get("rate_limit") or 60)
            if not _check_rate_limit(key_hash, limit):
                return jsonify({
                    "error": "Rate limit exceeded",
                    "limit_per_minute": limit,
                }), 429

            # Scope-Prüfung
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
# ROUTE-REGISTRIERUNG
# ══════════════════════════════════════════════════════════════════════════════

def register_open_api_routes(app):
    """Registriert die öffentlichen API-Routen bei der Flask-App."""

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/users
    # Query-Parameter:
    #   role      (Pflicht)  Discord-Rollen-ID
    #   online    (optional) true = nur online, false/leer = alle mit Rolle
    #   guild_id  (optional) Discord-Server-ID (nur nötig, wenn API-Key für alle Server)
    # ══════════════════════════════════════════════════════════════════════════
    @app.route("/open-api/users", methods=["GET"])
    @require_api_key(scope="users.read")
    def open_api_users():
        api_key = request.api_key_data  # type: ignore[attr-defined]

        # ── Parameter lesen ───────────────────────────────────────────────────
        role_id  = (request.args.get("role") or "").strip()
        online_s = (request.args.get("online") or "").strip().lower()
        guild_id = (request.args.get("guild_id") or "").strip()

        if not role_id:
            return jsonify({
                "error": "Missing parameter 'role'",
                "example": "/open-api/users?role=1410628676499800196&online=true",
            }), 400

        # Wenn der API-Key nur für einen Server gilt, diesen verwenden
        key_guild = api_key.get("guild_id")
        if key_guild and guild_id and str(key_guild) != str(guild_id):
            return jsonify({
                "error": "API key is not valid for this guild",
                "key_guild": key_guild,
            }), 403
        if key_guild and not guild_id:
            guild_id = str(key_guild)

        if not guild_id:
            return jsonify({
                "error": "Missing parameter 'guild_id' (API key is not guild-bound)",
            }), 400

        # Online-Filter (Default: false = alle)
        online_only = online_s in ("1", "true", "yes", "ja")

        # ── Discord-Bot holen ─────────────────────────────────────────────────
        bot = _get_bot()
        if bot is None:
            return jsonify({
                "error": "Bot not initialized",
                "hint": "Die Discord-Bot-Instanz wurde noch nicht an die Flask-App übergeben.",
            }), 503

        # guild_id muss numerisch sein
        try:
            guild_id_int = int(guild_id)
            role_id_int  = int(role_id)
        except ValueError:
            return jsonify({"error": "guild_id and role must be numeric Discord snowflakes"}), 400

        guild = bot.get_guild(guild_id_int)
        if guild is None:
            return jsonify({
                "error": "Guild not found or bot not on this guild",
                "guild_id": guild_id,
            }), 404

        role = guild.get_role(role_id_int)
        if role is None:
            return jsonify({
                "error": "Role not found",
                "role_id": role_id,
                "guild_id": guild_id,
            }), 404

        # ── Mitglieder filtern ────────────────────────────────────────────────
        try:
            # Der lokale Cache enthält alle Member, wenn der Bot den
            # Server Members Intent hat und der Cache warm ist.
            members = list(guild.members)

            result = []
            for m in members:
                if role not in m.roles:
                    continue

                status = str(m.status)  # "online", "idle", "dnd", "offline"
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

            # Nach Online-Status und Name sortieren
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
            "time":        datetime.now(timezone.utc).isoformat(),
        })

    # ══════════════════════════════════════════════════════════════════════════
    # GET /open-api/_debug/bot
    # Diagnose: zeigt, wo die Bot-Instanz liegt.
    # Nur mit gültigem API-Key erreichbar.
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

        # Attribute auf der App
        for attr in ("_bot_instance", "bot", "discord_bot", "bot_instance"):
            b = getattr(app, attr, None)
            info["app_attrs"][attr] = repr(b) if b is not None else None

        # Modulvariablen in app.py
        try:
            app_mod = importlib.import_module("bot.core.web_app.flask_app.app")
            for attr in ("_bot_instance", "bot", "bot_instance"):
                b = getattr(app_mod, attr, None)
                info["module_vars"][attr] = repr(b) if b is not None else None
        except Exception as e:
            info["module_vars"]["error"] = str(e)

        # Was unser Resolver liefert
        resolved = _get_bot()
        info["resolved"] = repr(resolved) if resolved is not None else None
        info["resolved_ok"] = resolved is not None

        return jsonify(info)

    log.info("✅ Open-API Routen registriert: /open-api/users, /open-api/health, /open-api/_debug/bot")