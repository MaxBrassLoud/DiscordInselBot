"""
bot/core/web_app/flask_app/ticket_setup_routes.py
==================================================
Web-basiertes Ticket-System Setup & Bearbeitung.
HTML-Templates liegen in templates/ – kein hardcoded HTML mehr in dieser Datei.
"""

from __future__ import annotations

import os
from functools import wraps

from flask import jsonify, request, render_template, session

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
MBL_ID    = os.getenv("MBL", "")


def _is_mbl_user(user: dict) -> bool:
    return bool(MBL_ID and user.get("id") == MBL_ID)


def _user_is_server_admin(user: dict, server_id: str) -> bool:
    if _is_mbl_user(user):
        return True
    server_roles = user.get("server_roles") or {}
    return server_id in server_roles


def register_ticket_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url):

    def _check_server_access(user: dict, server_id: str) -> bool:
        if _is_mbl(user):
            return True
        server_roles = user.get("server_roles") or {}
        return server_id in server_roles

    def _user_is_admin_on_server(user: dict, server_id: str, bot_get_func) -> bool:
        if _is_mbl(user):
            return True
        uid = user.get("id", "")
        if not uid:
            return False
        member = bot_get_func(f"/guilds/{server_id}/members/{uid}")
        if not member:
            return False
        member_role_ids = set(str(r) for r in (member.get("roles") or []))
        guild_roles = bot_get_func(f"/guilds/{server_id}/roles")
        if not guild_roles:
            return False
        for role in guild_roles:
            role_id = str(role.get("id", ""))
            if role_id == server_id or role_id in member_role_ids:
                perms = int(role.get("permissions", 0))
                if perms & 0x8:
                    return True
        return False

    # ── GET /dashboard/setup ─────────────────────────────────────────────────
    @app.route("/dashboard/setup")
    @login_required
    def setup_overview():
        user = session["user"]
        return render_template("setup_overview.html", user=user)

    # ── GET /dashboard/setup/tickets ─────────────────────────────────────────
    @app.route("/dashboard/setup/tickets")
    @login_required
    def setup_tickets():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_tickets.html", user=user, server_id=server_id)

    # ── GET /dashboard/setup/welcomer ─────────────────────────────────────────
    @app.route("/dashboard/setup/welcomer")
    @login_required
    def setup_welcomer():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_welcomer.html", user=user, server_id=server_id)

    # ── GET /dashboard/setup/media ────────────────────────────────────────────
    @app.route("/dashboard/setup/media")
    @login_required
    def setup_media():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_media.html", user=user, server_id=server_id)

    # ── GET /api/setup/guild/<server_id>/channels ─────────────────────────────
    @app.route("/api/setup/guild/<server_id>/channels")
    @login_required
    def api_setup_channels(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        data = _bot_get(f"/guilds/{server_id}/channels")
        if not data:
            return jsonify({"channels": []})
        text_channels = [
            {"id": c["id"], "name": c["name"], "type": c["type"], "parent_id": c.get("parent_id")}
            for c in data if c["type"] in (0, 4, 5)
        ]
        return jsonify({"channels": text_channels})

    # ── GET /api/setup/guild/<server_id>/roles ────────────────────────────────
    @app.route("/api/setup/guild/<server_id>/roles")
    @login_required
    def api_setup_roles(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        data = _bot_get(f"/guilds/{server_id}/roles")
        if not data:
            return jsonify({"roles": []})
        roles = [
            {"id": r["id"], "name": r["name"], "color": r.get("color", 0), "position": r.get("position", 0)}
            for r in sorted(data, key=lambda x: -x.get("position", 0))
            if r["name"] != "@everyone"
        ]
        return jsonify({"roles": roles})

    # ── GET /api/setup/tickets/<server_id> ───────────────────────────────────
    @app.route("/api/setup/tickets/<server_id>", methods=["GET"])
    @login_required
    def api_get_ticket_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            srv = sb.table("ticket_servers").select("*").eq("server_id", server_id).execute()
            config = srv.data[0] if srv.data else None
            modules = []
            if config:
                mods_raw = sb.table("ticket_modules").select("*").eq("server_id", server_id).execute().data or []
                for mod in mods_raw:
                    roles = sb.table("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute()
                    mod["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
                    modules.append(mod)
            guild = _cached_guild(server_id)
            return jsonify({
                "config": config,
                "modules": modules,
                "guild": {
                    "name": guild.get("name", server_id) if guild else server_id,
                    "icon": _guild_icon_url(guild) if guild else None,
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/tickets/<server_id> ──────────────────────────────────
    @app.route("/api/setup/tickets/<server_id>", methods=["POST"])
    @login_required
    def api_save_ticket_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            data = {
                "server_id":             server_id,
                "category_id":           body.get("category_id") or None,
                "panel_channel_id":      body.get("panel_channel_id") or None,
                "log_channel_id":        body.get("log_channel_id") or None,
                "staff_ping_channel_id": body.get("staff_ping_channel_id") or None,
            }
            existing = sb.table("ticket_servers").select("server_id").eq("server_id", server_id).execute()
            if existing.data:
                sb.table("ticket_servers").update(data).eq("server_id", server_id).execute()
            else:
                data["ticket_counter"] = 0
                sb.table("ticket_servers").insert(data).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── GET /api/setup/tickets/<server_id>/modules ───────────────────────────
    @app.route("/api/setup/tickets/<server_id>/modules", methods=["GET"])
    @login_required
    def api_get_modules(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()
            mods = sb.table("ticket_modules").select("*").eq("server_id", server_id).execute().data or []
            for mod in mods:
                roles = sb.table("ticket_module_roles").select("role_id").eq("module_id", mod["id"]).execute()
                mod["staff_role_ids"] = [r["role_id"] for r in (roles.data or [])]
            return jsonify({"modules": mods})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/tickets/<server_id>/modules ──────────────────────────
    @app.route("/api/setup/tickets/<server_id>/modules", methods=["POST"])
    @login_required
    def api_add_module(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            srv = sb.table("ticket_servers").select("server_id").eq("server_id", server_id).execute()
            if not srv.data:
                return jsonify({"error": "Ticket-System nicht eingerichtet. Zuerst Server-Einstellungen speichern."}), 400
            result = sb.table("ticket_modules").insert({
                "server_id":      server_id,
                "name":           body.get("name", "Modul"),
                "description":    body.get("description", ""),
                "max_tickets":    int(body.get("max_tickets", 1)),
                "modal_question": body.get("modal_question", "Bitte beschreibe dein Anliegen."),
                "button_emoji":   body.get("button_emoji", "🎫"),
                "category_id":    body.get("category_id") or None,
            }).execute()
            if not result.data:
                return jsonify({"error": "Modul konnte nicht erstellt werden"}), 500
            mod_id   = result.data[0]["id"]
            role_ids = body.get("staff_role_ids", [])
            for role_id in role_ids:
                sb.table("ticket_module_roles").insert({
                    "module_id": mod_id, "role_id": str(role_id)
                }).execute()
            return jsonify({"ok": True, "module_id": mod_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── PUT /api/setup/tickets/<server_id>/modules/<mod_id> ──────────────────
    @app.route("/api/setup/tickets/<server_id>/modules/<int:mod_id>", methods=["PUT"])
    @login_required
    def api_update_module(server_id, mod_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            sb.table("ticket_modules").update({
                "name":           body.get("name"),
                "description":    body.get("description"),
                "max_tickets":    int(body.get("max_tickets", 1)),
                "modal_question": body.get("modal_question"),
                "button_emoji":   body.get("button_emoji", "🎫"),
                "category_id":    body.get("category_id") or None,
            }).eq("id", mod_id).eq("server_id", server_id).execute()
            sb.table("ticket_module_roles").delete().eq("module_id", mod_id).execute()
            for role_id in body.get("staff_role_ids", []):
                sb.table("ticket_module_roles").insert({
                    "module_id": mod_id, "role_id": str(role_id)
                }).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── DELETE /api/setup/tickets/<server_id>/modules/<mod_id> ───────────────
    @app.route("/api/setup/tickets/<server_id>/modules/<int:mod_id>", methods=["DELETE"])
    @login_required
    def api_delete_module(server_id, mod_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()
            sb.table("ticket_module_roles").delete().eq("module_id", mod_id).execute()
            sb.table("ticket_modules").delete().eq("id", mod_id).eq("server_id", server_id).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/tickets/<server_id>/panel ─────────────────────────────
    @app.route("/api/setup/tickets/<server_id>/panel", methods=["POST"])
    @login_required
    def api_send_panel(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            import requests as req_lib
            sb = get_supabase()
            srv = sb.table("ticket_servers").select("*").eq("server_id", server_id).execute()
            if not srv.data:
                return jsonify({"error": "Ticket-System nicht konfiguriert"}), 400
            config = srv.data[0]
            panel_channel_id = config.get("panel_channel_id")
            if not panel_channel_id:
                return jsonify({"error": "Kein Panel-Channel konfiguriert"}), 400
            mods = sb.table("ticket_modules").select("*").eq("server_id", server_id).execute().data or []
            if not mods:
                return jsonify({"error": "Keine Module konfiguriert"}), 400

            body = request.get_json() or {}
            panel_title = body.get("panel_title", "🎫 Support-Tickets")
            panel_desc  = body.get("panel_desc", "Wähle ein Modul aus den Buttons unten um ein Ticket zu erstellen.")

            DISCORD_API_URL = "https://discord.com/api/v10"
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

            fields = [
                {"name": f"{m.get('button_emoji', '🎫')} {m['name']}", "value": m.get("description", "–") or "–", "inline": False}
                for m in mods
            ]
            embed = {"title": panel_title, "description": panel_desc, "color": 0x4ade80, "fields": fields}
            components = [{"type": 1, "components": [{"type": 2, "style": 1, "label": "🎫 Ticket erstellen", "custom_id": "ticket_panel_open"}]}]
            payload = {"embeds": [embed], "components": components}

            existing_msg_id = config.get("panel_message_id")
            panel_sent      = False
            new_msg_id      = existing_msg_id

            if existing_msg_id:
                edit_r = req_lib.patch(
                    f"{DISCORD_API_URL}/channels/{panel_channel_id}/messages/{existing_msg_id}",
                    headers=headers, json=payload, timeout=8,
                )
                if edit_r.status_code in (200, 204):
                    panel_sent = True
                else:
                    existing_msg_id = None

            if not panel_sent:
                send_r = req_lib.post(
                    f"{DISCORD_API_URL}/channels/{panel_channel_id}/messages",
                    headers=headers, json=payload, timeout=8,
                )
                if send_r.ok:
                    new_msg_id = send_r.json().get("id")
                    panel_sent = True
                else:
                    return jsonify({"error": f"Discord API Fehler {send_r.status_code}: {send_r.text[:300]}"}), 502

            sb.table("ticket_servers").update({
                "panel_message_id": new_msg_id,
                "panel_channel_id": panel_channel_id,
            }).eq("server_id", server_id).execute()

            return jsonify({"ok": True, "panel_sent": panel_sent, "message_id": new_msg_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── GET /api/setup/welcomer/<server_id> ───────────────────────────────────
    @app.route("/api/setup/welcomer/<server_id>", methods=["GET"])
    @login_required
    def api_get_welcomer_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            row = sb.table("settings").select("*").eq("guild_id", server_id).execute()
            config = row.data[0] if row.data else None
            guild  = _cached_guild(server_id)
            return jsonify({
                "config": config,
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/welcomer/<server_id> ──────────────────────────────────
    @app.route("/api/setup/welcomer/<server_id>", methods=["POST"])
    @login_required
    def api_save_welcomer_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            data = {
                "guild_id":           server_id,
                "welcome_channel_id": body.get("welcome_channel_id") or None,
                "goodbye_channel_id": body.get("goodbye_channel_id") or None,
                "welcome_enabled":    bool(body.get("welcome_enabled", True)),
                "goodbye_enabled":    bool(body.get("goodbye_enabled", True)),
            }
            existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
            if existing.data:
                sb.table("settings").update(data).eq("guild_id", server_id).execute()
            else:
                sb.table("settings").insert(data).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── GET /api/setup/media/<server_id> ──────────────────────────────────────
    @app.route("/api/setup/media/<server_id>", methods=["GET"])
    @login_required
    def api_get_media_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            row = sb.table("settings").select("*").eq("guild_id", server_id).execute()
            config = row.data[0] if row.data else None
            guild  = _cached_guild(server_id)
            return jsonify({
                "config": config,
                "guild": {"name": guild.get("name", server_id) if guild else server_id, "icon": _guild_icon_url(guild) if guild else None},
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/media/<server_id> ─────────────────────────────────────
    @app.route("/api/setup/media/<server_id>", methods=["POST"])
    @login_required
    def api_save_media_config(server_id):
        user = session["user"]
        if not _check_server_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}
            data = {
                "guild_id":         server_id,
                "image_channel_id": body.get("image_channel_id") or None,
                "forward_images":   bool(body.get("forward_images", True)),
                "forward_videos":   bool(body.get("forward_videos", True)),
                "forward_youtube":  bool(body.get("forward_youtube", True)),
                "forward_twitch":   bool(body.get("forward_twitch", True)),
            }
            existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
            if existing.data:
                sb.table("settings").update(data).eq("guild_id", server_id).execute()
            else:
                sb.table("settings").insert(data).execute()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── GET /api/setup/servers ────────────────────────────────────────────────
    @app.route("/api/setup/servers")
    @login_required
    def api_setup_servers():
        user         = session["user"]
        server_roles = user.get("server_roles") or {}

        if _is_mbl(user):
            all_ids = _get_all_known_server_ids()
        else:
            all_ids = list(server_roles.keys())

        servers = []
        for sid in all_ids:
            if not _is_mbl(user):
                is_admin = _user_is_admin_on_server(user, sid, _bot_get)
                if not is_admin:
                    continue
            guild = _cached_guild(sid)
            servers.append({
                "server_id": sid,
                "name":      guild.get("name", sid) if guild else sid,
                "icon":      _guild_icon_url(guild) if guild else None,
            })

        return jsonify({"servers": servers})


def _get_all_known_server_ids():
    try:
        from bot.core.supabase_client import get_supabase
        sb  = get_supabase()
        ids = set()
        for row in (sb.table("ticket_servers").select("server_id").execute().data or []):
            ids.add(row["server_id"])
        for row in (sb.table("application_servers").select("server_id").execute().data or []):
            ids.add(row["server_id"])
        return sorted(ids)
    except Exception:
        return []