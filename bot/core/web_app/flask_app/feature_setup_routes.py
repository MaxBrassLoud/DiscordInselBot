"""
bot/core/web_app/flask_app/feature_setup_routes.py
====================================================
Web-Setup Routen für alle restlichen Bot-Features:
Moderation, Spieleabende, Events, Rollen, Level,
Stream-Notifications, Geburtstage, Web-Dashboard.
"""

from __future__ import annotations

import os
from functools import wraps

from flask import jsonify, request, render_template, session

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
MBL_ID    = os.getenv("MBL", "")


def _is_mbl_user(user: dict) -> bool:
    return bool(MBL_ID and user.get("id") == MBL_ID)


def register_feature_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url):

    def _check_server_access(user: dict, server_id: str) -> bool:
        if _is_mbl(user):
            return True
        server_roles = user.get("server_roles") or {}
        return server_id in server_roles

    def _get_settings(sb_func, guild_id: str) -> dict:
        try:
            row = sb_func("settings").select("*").eq("guild_id", guild_id).execute()
            return row.data[0] if row.data else {}
        except Exception:
            return {}

    def _upsert_settings(sb_func, guild_id: str, data: dict):
        from bot.core.supabase_client import get_supabase
        sb = get_supabase()
        existing = sb.table("settings").select("id").eq("guild_id", guild_id).execute()
        if existing.data:
            sb.table("settings").update(data).eq("guild_id", guild_id).execute()
        else:
            data["guild_id"] = guild_id
            sb.table("settings").insert(data).execute()

    # ══════════════════════════════════════════════════════════════════════════
    # MODERATION
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/moderation")
    @login_required
    def setup_moderation():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_moderation.html", user=user, server_id=server_id)

    @app.route("/api/setup/moderation/<server_id>", methods=["GET"])
    @login_required
    def api_get_moderation_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            cfg = _get_settings(sb, server_id)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {"moderation_log_channel_id": cfg.get("moderation_log_channel_id")},
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/moderation/<server_id>", methods=["POST"])
    @login_required
    def api_save_moderation_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            _upsert_settings(sb, server_id, {
                "moderation_log_channel_id": body.get("moderation_log_channel_id") or None,
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # SPIELEABENDE
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/spieleabend")
    @login_required
    def setup_spieleabend():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_spieleabend.html", user=user, server_id=server_id)

    @app.route("/api/setup/spieleabend/<server_id>", methods=["GET"])
    @login_required
    def api_get_spieleabend_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            cfg = _get_settings(sb, server_id)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {
                    "ping_role_id":     cfg.get("ping_role_id"),
                    "channel_id":       cfg.get("channel_id"),
                    "delete_role_ids":  cfg.get("delete_role_ids"),
                },
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/spieleabend/<server_id>", methods=["POST"])
    @login_required
    def api_save_spieleabend_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            _upsert_settings(sb, server_id, {
                "ping_role_id":    body.get("ping_role_id") or None,
                "channel_id":      body.get("channel_id") or None,
                "delete_role_ids": body.get("delete_role_ids") or "",
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # EVENTS
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/events")
    @login_required
    def setup_events():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_events.html", user=user, server_id=server_id)

    @app.route("/api/setup/events/<server_id>", methods=["GET"])
    @login_required
    def api_get_events_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            cfg = _get_settings(sb, server_id)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {
                    "event_channel_id": cfg.get("event_channel_id"),
                    "event_role_ids":   cfg.get("event_role_ids"),
                },
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/events/<server_id>", methods=["POST"])
    @login_required
    def api_save_events_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            _upsert_settings(sb, server_id, {
                "event_channel_id": body.get("event_channel_id") or None,
                "event_role_ids":   body.get("event_role_ids") or "",
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # ROLLEN
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/rollen")
    @login_required
    def setup_rollen():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_rollen.html", user=user, server_id=server_id)

    @app.route("/api/setup/rollen/<server_id>", methods=["GET"])
    @login_required
    def api_get_rollen_modules(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb    = get_supabase()
            rows  = sb.table("role_modules").select("*").eq("guild_id", server_id).execute().data or []
            guild = _cached_guild(server_id)
            return jsonify({
                "modules": rows,
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/rollen/<server_id>", methods=["POST"])
    @login_required
    def api_add_rolle_module(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            result = sb.table("role_modules").insert({
                "guild_id":     server_id,
                "role_id":      body.get("role_id", ""),
                "role_name":    body.get("role_name", ""),
                "role_desc":    body.get("role_desc", ""),
                "channel_id":   body.get("channel_id", ""),
                "display_name": body.get("display_name", ""),
            }).execute()
            return jsonify({"ok": True, "module_id": result.data[0]["id"] if result.data else None})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/rollen/<server_id>/<int:module_id>", methods=["DELETE"])
    @login_required
    def api_delete_rolle_module(server_id, module_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()
            sb.table("role_modules").delete().eq("id", module_id).eq("guild_id", server_id).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # LEVEL
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/level")
    @login_required
    def setup_level():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_level.html", user=user, server_id=server_id)

    @app.route("/api/setup/level/<server_id>", methods=["GET"])
    @login_required
    def api_get_level_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            cfg = _get_settings(sb, server_id)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {
                    "level_channel_id": cfg.get("level_channel_id"),
                    "levels_enabled":   cfg.get("levels_enabled", True),
                },
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/level/<server_id>", methods=["POST"])
    @login_required
    def api_save_level_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            _upsert_settings(sb, server_id, {
                "level_channel_id": body.get("level_channel_id") or None,
                "levels_enabled":   bool(body.get("levels_enabled", True)),
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # STREAM NOTIFICATIONS
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/streamnotifications")
    @login_required
    def setup_streamnotifications():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_streamnotifications.html", user=user, server_id=server_id)

    @app.route("/api/setup/streamnotifications/<server_id>", methods=["GET"])
    @login_required
    def api_get_streamnotifications_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()
            cfg_row = sb.table("stream_notifications_config").select("*").eq("guild_id", server_id).execute()
            cfg = cfg_row.data[0] if cfg_row.data else {}
            accounts = sb.table("stream_notifications_accounts").select("*").eq("guild_id", server_id).execute().data or []
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {
                    "channel_id": cfg.get("channel_id"),
                    "enabled":    cfg.get("enabled", True),
                },
                "accounts": accounts,
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/streamnotifications/<server_id>", methods=["POST"])
    @login_required
    def api_save_streamnotifications_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            data = {
                "guild_id":   server_id,
                "channel_id": body.get("channel_id") or None,
                "enabled":    bool(body.get("enabled", True)),
            }
            existing = sb.table("stream_notifications_config").select("id").eq("guild_id", server_id).execute()
            if existing.data:
                sb.table("stream_notifications_config").update(data).eq("guild_id", server_id).execute()
            else:
                sb.table("stream_notifications_config").insert(data).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # GEBURTSTAGE
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/geburtstage")
    @login_required
    def setup_geburtstage():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_geburtstage.html", user=user, server_id=server_id)

    @app.route("/api/setup/geburtstage/<server_id>", methods=["GET"])
    @login_required
    def api_get_geburtstage_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            cfg = _get_settings(sb, server_id)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {"birthday_channel_id": cfg.get("birthday_channel_id")},
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/geburtstage/<server_id>", methods=["POST"])
    @login_required
    def api_save_geburtstage_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            _upsert_settings(sb, server_id, {
                "birthday_channel_id": body.get("birthday_channel_id") or None,
            })
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ══════════════════════════════════════════════════════════════════════════
    # WEB DASHBOARD
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/setup/webdashboard")
    @login_required
    def setup_webdashboard():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_webdashboard.html", user=user, server_id=server_id)

    @app.route("/api/setup/webdashboard/<server_id>", methods=["GET"])
    @login_required
    def api_get_webdashboard_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()
            t_row = sb.table("ticket_servers").select("web_admin_role_ids").eq("server_id", server_id).execute()
            a_row = sb.table("application_servers").select("web_admin_role_ids").eq("server_id", server_id).execute()
            t_cfg = t_row.data[0] if t_row.data else {}
            a_cfg = a_row.data[0] if a_row.data else {}
            guild = _cached_guild(server_id)
            return jsonify({
                "config": {
                    "ticket_web_admin_role_ids":    t_cfg.get("web_admin_role_ids", ""),
                    "application_web_admin_role_ids": a_cfg.get("web_admin_role_ids", ""),
                },
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/setup/webdashboard/<server_id>", methods=["POST"])
    @login_required
    def api_save_webdashboard_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            role_ids = body.get("web_admin_role_ids") or ""

            # In ticket_servers speichern
            t_exists = sb.table("ticket_servers").select("server_id").eq("server_id", server_id).execute()
            if t_exists.data:
                sb.table("ticket_servers").update({"web_admin_role_ids": role_ids}).eq("server_id", server_id).execute()

            # In application_servers speichern
            a_exists = sb.table("application_servers").select("server_id").eq("server_id", server_id).execute()
            if a_exists.data:
                sb.table("application_servers").update({"web_admin_role_ids": role_ids}).eq("server_id", server_id).execute()

            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
