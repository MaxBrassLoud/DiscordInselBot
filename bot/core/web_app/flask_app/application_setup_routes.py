"""
bot/core/web_app/flask_app/application_setup_routes.py
=======================================================
Web-basiertes Bewerbungs-System Setup & Bearbeitung.
HTML-Templates liegen in templates/ – kein hardcoded HTML mehr in dieser Datei.

In app.py einbinden:
    from .application_setup_routes import register_application_setup_routes
    register_application_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url)

ROUTEN:
  GET  /dashboard/setup/applications            – Setup-Seite (Template)
  GET  /api/setup/applications/<server_id>      – Konfiguration laden
  POST /api/setup/applications/<server_id>      – Konfiguration speichern
  POST /api/setup/applications/<server_id>/panel – Panel senden
"""

from __future__ import annotations

import os
from flask import jsonify, request, render_template, session

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
MBL_ID    = os.getenv("MBL", "")


def register_application_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url):

    def _check_access(user: dict, server_id: str) -> bool:
        if _is_mbl(user):
            return True
        return server_id in (user.get("server_roles") or {})

    def _parse_role_ids(raw) -> list[str]:
        if not raw:
            return []
        if isinstance(raw, list):
            return [str(r).strip() for r in raw if r]
        return [r.strip() for r in str(raw).split(",") if r.strip()]

    # ── GET /dashboard/setup/applications ────────────────────────────────────
    @app.route("/dashboard/setup/applications")
    @login_required
    def setup_applications():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_applications.html", user=user, server_id=server_id)

    # ── GET /api/setup/applications/<server_id> ───────────────────────────────
    @app.route("/api/setup/applications/<server_id>", methods=["GET"])
    @login_required
    def api_get_app_config(server_id):
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb    = get_supabase()
            row   = sb.table("application_servers").select("*").eq("server_id", server_id).execute()
            config = row.data[0] if row.data else None
            guild  = _cached_guild(server_id)
            return jsonify({
                "config": config,
                "guild": {
                    "name": guild.get("name", server_id) if guild else server_id,
                    "icon": _guild_icon_url(guild) if guild else None,
                },
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/applications/<server_id> ──────────────────────────────
    @app.route("/api/setup/applications/<server_id>", methods=["POST"])
    @login_required
    def api_save_app_config(server_id):
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}

            staff_ids   = body.get("staff_role_ids", [])
            web_adm_ids = body.get("web_admin_role_ids", [])
            if isinstance(staff_ids, list):
                staff_ids = ",".join(staff_ids)
            if isinstance(web_adm_ids, list):
                web_adm_ids = ",".join(web_adm_ids)

            data = {
                "server_id":                server_id,
                "panel_channel_id":         body.get("panel_channel_id") or None,
                "category_id":              body.get("category_id") or None,
                "newbie_role_id":           body.get("newbie_role_id") or None,
                "member_role_id":           body.get("member_role_id") or None,
                "staff_role_ids":           staff_ids,
                "log_channel_id":           body.get("log_channel_id") or None,
                "mc_log_channel_id":        body.get("mc_log_channel_id") or None,
                "web_admin_role_ids":       web_adm_ids,
                "panel_message":            body.get("panel_message", ""),
                "welcome_message":          body.get("welcome_message", ""),
                "instruction_message":      body.get("instruction_message", ""),
                "rejection_cooldown_hours": int(body.get("rejection_cooldown_hours", 24)),
            }

            existing = sb.table("application_servers").select("server_id").eq("server_id", server_id).execute()
            if existing.data:
                sb.table("application_servers").update(data).eq("server_id", server_id).execute()
            else:
                data["app_counter"] = 0
                sb.table("application_servers").insert(data).execute()

            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── POST /api/setup/applications/<server_id>/panel ────────────────────────
    @app.route("/api/setup/applications/<server_id>/panel", methods=["POST"])
    @login_required
    def api_send_app_panel(server_id):
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            import requests as req_lib
            sb = get_supabase()

            row = sb.table("application_servers").select("*").eq("server_id", server_id).execute()
            if not row.data:
                return jsonify({"error": "Bewerbungs-System nicht konfiguriert"}), 400
            config = row.data[0]

            panel_ch_id = config.get("panel_channel_id")
            if not panel_ch_id:
                return jsonify({"error": "Kein Panel-Channel konfiguriert"}), 400

            body        = request.get_json() or {}
            panel_title = body.get("panel_title", "⛏️ Bewerbung einreichen")
            panel_desc  = body.get("panel_desc") or config.get("panel_message") or config.get("welcome_message") or (
                "Klicke auf den Button um deine Bewerbung einzureichen."
            )

            DISCORD_API_URL = "https://discord.com/api/v10"
            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

            embed = {"title": panel_title, "description": panel_desc, "color": 0x22c55e}
            components = [{"type": 1, "components": [{"type": 2, "style": 3, "label": "📝 Bewerben", "custom_id": "app_apply_button", "emoji": {"name": "⛏️"}}]}]
            payload = {"embeds": [embed], "components": components}

            existing_msg_id = config.get("panel_message_id")
            panel_sent      = False
            new_msg_id      = existing_msg_id

            if existing_msg_id:
                edit_r = req_lib.patch(
                    f"{DISCORD_API_URL}/channels/{panel_ch_id}/messages/{existing_msg_id}",
                    headers=headers, json=payload, timeout=8,
                )
                if edit_r.status_code in (200, 204):
                    panel_sent = True
                else:
                    existing_msg_id = None

            if not panel_sent:
                send_r = req_lib.post(
                    f"{DISCORD_API_URL}/channels/{panel_ch_id}/messages",
                    headers=headers, json=payload, timeout=8,
                )
                if send_r.ok:
                    new_msg_id = send_r.json().get("id")
                    panel_sent = True
                else:
                    return jsonify({"error": f"Discord API Fehler {send_r.status_code}: {send_r.text[:300]}"}), 502

            sb.table("application_servers").update({
                "panel_message_id": new_msg_id,
                "panel_message": panel_desc,
            }).eq("server_id", server_id).execute()

            return jsonify({"ok": True, "panel_sent": panel_sent, "message_id": new_msg_id})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
