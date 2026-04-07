"""
bot/core/web_app/flask_app/ticket_setup_routes.py
==================================================
Web-basiertes Ticket-System Setup & Bearbeitung.

ÄNDERUNGEN:
  - /api/setup/servers gibt nur Server zurück wo der User ADMINISTRATOR-Rechte hat
  - Setup-Overview zeigt jetzt auch Welcomer und Media-Weiterleitung als Module
"""

from __future__ import annotations

import os
from functools import wraps

from flask import jsonify, request, render_template_string, session

BOT_TOKEN = os.getenv("DISCORD_TOKEN", "")
MBL_ID    = os.getenv("MBL", "")


def _is_mbl_user(user: dict) -> bool:
    return bool(MBL_ID and user.get("id") == MBL_ID)


def _user_is_server_admin(user: dict, server_id: str) -> bool:
    """Prüft ob der User Admin-Rechte auf dem Server hat."""
    if _is_mbl_user(user):
        return True
    server_roles = user.get("server_roles") or {}
    return server_id in server_roles


def register_ticket_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url):
    """Registriert alle Ticket-Setup-Routen am Flask-App."""

    def _check_server_access(user: dict, server_id: str) -> bool:
        if _is_mbl(user):
            return True
        server_roles = user.get("server_roles") or {}
        return server_id in server_roles

    def _user_is_admin_on_server(user: dict, server_id: str, bot_get_func) -> bool:
        """
        Prüft via Bot-Token ob der User Administrator-Rechte auf diesem Server hat.
        Gibt True zurück wenn:
          - User ist MBL
          - User hat die Administrator-Permission (Bit 0x8) auf dem Server
        """
        if _is_mbl(user):
            return True

        uid = user.get("id", "")
        if not uid:
            return False

        member = bot_get_func(f"/guilds/{server_id}/members/{uid}")
        if not member:
            return False

        # Rollen des Users auf dem Server holen
        member_role_ids = set(str(r) for r in (member.get("roles") or []))

        # Guild-Rollen laden um Permissions zu prüfen
        guild_roles = bot_get_func(f"/guilds/{server_id}/roles")
        if not guild_roles:
            return False

        for role in guild_roles:
            role_id = str(role.get("id", ""))
            # @everyone Rolle (hat dieselbe ID wie der Server)
            if role_id == server_id or role_id in member_role_ids:
                perms = int(role.get("permissions", 0))
                # Bit 0x8 = ADMINISTRATOR
                if perms & 0x8:
                    return True

        return False

    # ── GET /dashboard/setup ─────────────────────────────────────────────────
    @app.route("/dashboard/setup")
    @login_required
    def setup_overview():
        user = session["user"]
        return render_template_string(
            _SETUP_OVERVIEW_HTML,
            user=user,
        )

    # ── GET /dashboard/setup/tickets ─────────────────────────────────────────
    @app.route("/dashboard/setup/tickets")
    @login_required
    def setup_tickets():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template_string(
            _TICKET_SETUP_HTML,
            user=user,
            server_id=server_id,
        )

    # ── GET /dashboard/setup/welcomer ─────────────────────────────────────────
    @app.route("/dashboard/setup/welcomer")
    @login_required
    def setup_welcomer():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template_string(
            _WELCOMER_SETUP_HTML,
            user=user,
            server_id=server_id,
        )

    # ── GET /dashboard/setup/media ────────────────────────────────────────────
    @app.route("/dashboard/setup/media")
    @login_required
    def setup_media():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template_string(
            _MEDIA_SETUP_HTML,
            user=user,
            server_id=server_id,
        )

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

            DISCORD_API = "https://discord.com/api/v10"
            headers = {
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json",
            }

            fields = [
                {
                    "name":   f"{m.get('button_emoji', '🎫')} {m['name']}",
                    "value":  m.get("description", "–") or "–",
                    "inline": False,
                }
                for m in mods
            ]
            embed = {
                "title":       panel_title,
                "description": panel_desc,
                "color":       0x4ade80,
                "fields":      fields,
            }

            components = [
                {
                    "type": 1,
                    "components": [
                        {
                            "type":      2,
                            "style":     1,
                            "label":     "🎫 Ticket erstellen",
                            "custom_id": "ticket_panel_open",
                        }
                    ],
                }
            ]

            payload = {
                "embeds":     [embed],
                "components": components,
            }

            existing_msg_id = config.get("panel_message_id")
            panel_sent      = False
            new_msg_id      = existing_msg_id

            if existing_msg_id:
                edit_r = req_lib.patch(
                    f"{DISCORD_API}/channels/{panel_channel_id}/messages/{existing_msg_id}",
                    headers=headers,
                    json=payload,
                    timeout=8,
                )
                if edit_r.status_code in (200, 204):
                    panel_sent = True
                else:
                    existing_msg_id = None

            if not panel_sent:
                send_r = req_lib.post(
                    f"{DISCORD_API}/channels/{panel_channel_id}/messages",
                    headers=headers,
                    json=payload,
                    timeout=8,
                )
                if send_r.ok:
                    new_msg_id = send_r.json().get("id")
                    panel_sent = True
                else:
                    return jsonify({
                        "error": f"Discord API Fehler {send_r.status_code}: {send_r.text[:300]}"
                    }), 502

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
                "guild": {
                    "name": guild.get("name", server_id) if guild else server_id,
                    "icon": _guild_icon_url(guild) if guild else None,
                },
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
                "guild_id":          server_id,
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
                "guild": {
                    "name": guild.get("name", server_id) if guild else server_id,
                    "icon": _guild_icon_url(guild) if guild else None,
                },
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
        """
        Gibt nur Server zurück auf denen der User ADMINISTRATOR-Rechte hat.
        MBL sieht alle konfigurierten Server.
        """
        user         = session["user"]
        server_roles = user.get("server_roles") or {}

        if _is_mbl(user):
            all_ids = _get_all_known_server_ids()
        else:
            # Nur Server wo User Mitglied ist UND Administrator-Rechte hat
            all_ids = list(server_roles.keys())

        servers = []
        for sid in all_ids:
            # Admin-Check: Prüfe ob der User auf diesem Server Admin ist
            if not _is_mbl(user):
                is_admin = _user_is_admin_on_server(user, sid, _bot_get)
                if not is_admin:
                    continue  # Server nicht anzeigen – kein Admin

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


# ══════════════════════════════════════════════════════════════════════════════
# SETUP OVERVIEW HTML  –  mit Welcomer & Media
# ══════════════════════════════════════════════════════════════════════════════

_SETUP_OVERVIEW_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>System Setup – Insel Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    .setup-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
      gap: 18px;
      margin-top: 28px;
    }
    .setup-card {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--r-lg);
      padding: 24px;
      transition: all var(--mid);
      cursor: pointer;
      text-decoration: none !important;
      display: block;
      position: relative;
      overflow: hidden;
    }
    .setup-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 3px;
      background: var(--green2);
      opacity: 0;
      transition: opacity var(--mid);
    }
    .setup-card:hover {
      border-color: var(--green2);
      transform: translateY(-3px);
      box-shadow: 0 8px 32px rgba(0,0,0,0.4);
    }
    .setup-card:hover::before { opacity: 1; }
    .setup-card.disabled {
      opacity: 0.5;
      cursor: not-allowed;
      pointer-events: none;
    }
    .setup-card-icon {
      width: 48px; height: 48px;
      border-radius: var(--r);
      background: var(--green-g2);
      border: 1px solid rgba(74,222,128,0.2);
      display: flex; align-items: center; justify-content: center;
      font-size: 1.3rem;
      margin-bottom: 14px;
    }
    .setup-card-title {
      font-family: 'Rajdhani', sans-serif;
      font-size: 1.05rem; font-weight: 700;
      color: var(--text); margin-bottom: 6px;
    }
    .setup-card-desc { font-size: 0.82rem; color: var(--text2); line-height: 1.6; }
    .setup-card-status {
      display: inline-flex; align-items: center; gap: 5px;
      margin-top: 12px; padding: 3px 9px;
      border-radius: 20px; font-size: 0.68rem; font-weight: 700;
    }
    .status-ok   { background: rgba(74,222,128,0.1); color: var(--green); border: 1px solid rgba(74,222,128,0.2); }
    .status-none { background: var(--bg-surface); color: var(--text3); border: 1px solid var(--border); }

    .page-content { max-width: 1000px; margin: 0 auto; padding: 28px 22px; }
    .page-hero { margin-bottom: 8px; }
    .page-hero-title {
      font-family: 'Rajdhani', sans-serif;
      font-size: 1.6rem; font-weight: 700; color: var(--text); margin-bottom: 5px;
    }
    .page-hero-sub { font-size: 0.86rem; color: var(--text3); }

    .server-selector {
      display: flex; align-items: center; gap: 8px;
      margin-bottom: 24px; padding: 12px 16px;
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--r-lg);
    }
    .server-selector label { font-size: 0.80rem; color: var(--text3); font-weight: 600; flex-shrink: 0; }
    .server-selector select {
      background: var(--bg-surface); border: 1px solid var(--border);
      color: var(--text); font-family: 'Outfit', sans-serif;
      font-size: 0.85rem; padding: 6px 10px; border-radius: var(--r-sm);
      outline: none; cursor: pointer; flex: 1;
    }
    .server-selector select:focus { border-color: var(--green2); }
    .server-selector select option { background: #1d2128; }

    .no-admin-notice {
      background: var(--bg-card); border: 1px solid var(--border);
      border-left: 3px solid var(--orange); border-radius: var(--r-lg);
      padding: 16px 18px; margin-top: 16px;
      font-size: 0.83rem; color: var(--text2); line-height: 1.6;
    }
    .no-admin-notice strong { color: var(--orange); }

    .section-divider {
      font-size: 0.63rem; font-weight: 700; text-transform: uppercase;
      letter-spacing: 0.1em; color: var(--text3); margin: 28px 0 12px;
      display: flex; align-items: center; gap: 10px;
    }
    .section-divider::after {
      content: ''; flex: 1; height: 1px; background: var(--border);
    }

    .skeleton { animation: shimmer 1.4s ease infinite; border-radius: 4px;
      background: linear-gradient(90deg,var(--bg-surface) 25%,var(--bg-hover) 50%,var(--bg-surface) 75%);
      background-size: 400px 100%; }
    @keyframes shimmer { 0%{background-position:-400px 0} 100%{background-position:400px 0} }
  </style>
</head>
<body class="detail-body">

<div class="detail-topbar">
  <a href="/dashboard" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
      <path d="M15 18l-6-6 6-6"/>
    </svg>
    Dashboard
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">System Setup</span>
</div>

<div class="page-content">
  <div class="page-hero">
    <div class="page-hero-title">⚙️ System Setup</div>
    <div class="page-hero-sub">Konfiguriere alle Bot-Systeme direkt im Browser – kein Discord-Command nötig.<br>
      <span style="color:var(--orange);font-size:0.78rem;">⚠️ Nur Server auf denen du <strong>Administrator-Rechte</strong> hast werden angezeigt.</span>
    </div>
  </div>

  <div class="server-selector">
    <label>🌐 Server:</label>
    <select id="serverSelect" onchange="onServerChange(this.value)">
      <option value="">Lade Server...</option>
    </select>
  </div>

  <div id="noAdminNotice" style="display:none;" class="no-admin-notice">
    <strong>⚠️ Kein Server verfügbar</strong><br>
    Du hast auf keinem der konfigurierten Server Administrator-Rechte.
    Setup-Optionen sind nur für Server-Administratoren zugänglich.
  </div>

  <div id="setupContent" style="display:none;">
    <div class="section-divider">Bot-Systeme</div>
    <div class="setup-grid" id="setupGrid"></div>
  </div>
</div>

<script>
let _serverId = '';

async function loadServers() {
  const sel = document.getElementById('serverSelect');
  sel.innerHTML = '<option value="">Lade Server...</option>';

  try {
    const r = await fetch('/api/setup/servers');
    const d = await r.json();

    if (!d.servers || d.servers.length === 0) {
      sel.innerHTML = '<option value="">Keine Server mit Admin-Rechten gefunden</option>';
      document.getElementById('noAdminNotice').style.display = '';
      return;
    }

    sel.innerHTML = '<option value="">-- Server auswählen --</option>' +
      d.servers.map(s => {
        const icon = s.icon ? '' : '';
        return `<option value="${esc(s.server_id)}">${esc(s.name)}</option>`;
      }).join('');

    if (d.servers.length === 1) {
      sel.value = d.servers[0].server_id;
      onServerChange(d.servers[0].server_id);
    }
  } catch(e) {
    sel.innerHTML = '<option value="">Fehler beim Laden</option>';
    console.error(e);
  }
}

function onServerChange(sid) {
  _serverId = sid;
  const content = document.getElementById('setupContent');
  const notice  = document.getElementById('noAdminNotice');

  if (!sid) {
    content.style.display = 'none';
    return;
  }

  notice.style.display  = 'none';
  content.style.display = '';
  renderSetupGrid(sid);
}

function renderSetupGrid(sid) {
  document.getElementById('setupGrid').innerHTML = `
    <a href="/dashboard/setup/tickets?server_id=${esc(sid)}" class="setup-card">
      <div class="setup-card-icon">🎫</div>
      <div class="setup-card-title">Ticket-System</div>
      <div class="setup-card-desc">
        Module konfigurieren, Kanäle & Kategorien festlegen,
        Panel in Channel senden. Schritt-für-Schritt geführt.
      </div>
      <span class="setup-card-status status-ok">● Verfügbar</span>
    </a>

    <a href="/dashboard/setup/applications?server_id=${esc(sid)}" class="setup-card">
      <div class="setup-card-icon">📋</div>
      <div class="setup-card-title">Bewerbungs-System</div>
      <div class="setup-card-desc">
        Panel-Kanal, Rollen, Texte und Cooldown konfigurieren.
        Bewerber-Flow von Einreichung bis Entscheidung einrichten.
      </div>
      <span class="setup-card-status status-ok">● Verfügbar</span>
    </a>

    <a href="/dashboard/setup/welcomer?server_id=${esc(sid)}" class="setup-card">
      <div class="setup-card-icon">👋</div>
      <div class="setup-card-title">Willkommens-Nachrichten</div>
      <div class="setup-card-desc">
        Willkommens- und Abschieds-Kanal konfigurieren.
        Einzeln an- und abschaltbar.
      </div>
      <span class="setup-card-status status-ok">● Verfügbar</span>
    </a>

    <a href="/dashboard/setup/media?server_id=${esc(sid)}" class="setup-card">
      <div class="setup-card-icon">📷</div>
      <div class="setup-card-title">Medien-Weiterleitung</div>
      <div class="setup-card-desc">
        Bilder, Videos, YouTube- und Twitch-Links automatisch in
        einen Medienkanal weiterleiten. Jede Funktion einzeln steuerbar.
      </div>
      <span class="setup-card-status status-ok">● Verfügbar</span>
    </a>

    <div class="setup-card disabled">
      <div class="setup-card-icon">🎮</div>
      <div class="setup-card-title">Spieleabende</div>
      <div class="setup-card-desc">Spieleabend-Kanal und Rollen konfigurieren.</div>
      <span class="setup-card-status status-none">Bald verfügbar</span>
    </div>

    <div class="setup-card disabled">
      <div class="setup-card-icon">🎙️</div>
      <div class="setup-card-title">Voice Channel Creator</div>
      <div class="setup-card-desc">Automatische Voice-Kanäle einrichten.</div>
      <span class="setup-card-status status-none">Bald verfügbar</span>
    </div>
  `;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

loadServers();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# WELCOMER SETUP HTML
# ══════════════════════════════════════════════════════════════════════════════

_WELCOMER_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Welcomer Setup – Insel Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    .setup-body { max-width: 700px; margin: 0 auto; padding: 24px 22px; }
    .panel-card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--r-lg); padding: 24px 26px; margin-bottom: 18px;
    }
    .panel-title { font-family:'Rajdhani',sans-serif; font-size:1.1rem; font-weight:700; color:var(--text); margin-bottom:4px; }
    .panel-sub   { font-size:0.82rem; color:var(--text3); margin-bottom:20px; }

    .field { margin-bottom:16px; }
    .field label { display:block; font-size:0.63rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--text3); margin-bottom:5px; }
    .field select {
      width:100%; background:var(--bg-surface); border:1px solid var(--border);
      color:var(--text); font-family:'Outfit',sans-serif; font-size:0.85rem;
      padding:9px 12px; border-radius:var(--r-sm); outline:none; transition:border-color var(--fast);
    }
    .field select:focus { border-color:var(--green2); box-shadow:0 0 0 3px rgba(34,197,94,.12); }
    .field select option { background:#1d2128; }
    .field-hint { font-size:0.70rem; color:var(--text3); margin-top:4px; }

    .toggle-row { display:flex; align-items:center; justify-content:space-between; padding:12px 0; border-bottom:1px solid var(--border); }
    .toggle-row:last-child { border-bottom:none; }
    .toggle-label { font-size:0.85rem; color:var(--text); font-weight:500; }
    .toggle-sub   { font-size:0.72rem; color:var(--text3); margin-top:2px; }
    .toggle-switch { position:relative; width:44px; height:24px; flex-shrink:0; }
    .toggle-switch input { opacity:0; width:0; height:0; }
    .toggle-slider {
      position:absolute; inset:0; cursor:pointer;
      background:var(--border2); border-radius:24px; transition:all var(--mid);
    }
    .toggle-slider:before {
      content:''; position:absolute; width:18px; height:18px; border-radius:50%;
      background:#fff; left:3px; top:3px; transition:all var(--mid);
    }
    .toggle-switch input:checked + .toggle-slider { background:var(--green2); }
    .toggle-switch input:checked + .toggle-slider:before { transform:translateX(20px); }

    .btn-group { display:flex; gap:8px; flex-wrap:wrap; margin-top:20px; }
    .btn {
      display:inline-flex; align-items:center; gap:6px;
      padding:9px 20px; border-radius:var(--r-sm);
      font-family:'Rajdhani',sans-serif; font-weight:700; font-size:0.88rem;
      cursor:pointer; border:none; transition:all var(--mid);
    }
    .btn-primary { background:var(--green2); color:#000; }
    .btn-primary:hover:not(:disabled) { filter:brightness(1.1); transform:translateY(-1px); }
    .btn:disabled { opacity:0.4; cursor:not-allowed; }

    .toast-container { position:fixed; bottom:24px; right:24px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
    .toast { background:var(--bg-card); border:1px solid var(--border2); border-radius:var(--r); padding:10px 16px; font-size:0.83rem; color:var(--text); box-shadow:var(--shadow); min-width:200px; animation:fadeDown .2s ease both; display:flex; align-items:center; gap:8px; }
    .toast.ok  { border-left:3px solid var(--green2); }
    .toast.err { border-left:3px solid var(--red); }
  </style>
</head>
<body class="detail-body">
<div class="toast-container" id="toastContainer"></div>

<div class="detail-topbar">
  <a href="/dashboard/setup" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    System Setup
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">👋 Welcomer</span>
  <div id="topbarGuild" style="margin-left:auto;display:flex;align-items:center;gap:8px;"></div>
</div>

<div class="setup-body">
  <div class="panel-card">
    <div class="panel-title">Willkommens- & Abschiedsnachrichten</div>
    <div class="panel-sub">Konfiguriere in welchem Kanal neue Mitglieder begrüßt und verabschiedet werden.</div>

    <div class="field">
      <label>👋 Willkommens-Kanal</label>
      <select id="cfg_welcome_channel">
        <option value="">Lade Kanäle...</option>
      </select>
      <div class="field-hint">Neue Mitglieder werden hier mit einer zufälligen Nachricht begrüßt.</div>
    </div>

    <div class="field">
      <label>🚪 Abschied-Kanal</label>
      <select id="cfg_goodbye_channel">
        <option value="">(Kein Abschied-Kanal)</option>
      </select>
      <div class="field-hint">Wenn ein Mitglied den Server verlässt wird es hier verabschiedet.</div>
    </div>

    <div class="toggle-row">
      <div>
        <div class="toggle-label">👋 Willkommensnachrichten</div>
        <div class="toggle-sub">Neue Mitglieder werden automatisch begrüßt</div>
      </div>
      <label class="toggle-switch">
        <input type="checkbox" id="cfg_welcome_enabled" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <div class="toggle-row">
      <div>
        <div class="toggle-label">🚪 Abschiedsnachrichten</div>
        <div class="toggle-sub">Austretende Mitglieder werden verabschiedet</div>
      </div>
      <label class="toggle-switch">
        <input type="checkbox" id="cfg_goodbye_enabled" checked>
        <span class="toggle-slider"></span>
      </label>
    </div>

    <div class="btn-group">
      <button class="btn btn-primary" onclick="saveConfig()">💾 Einstellungen speichern</button>
    </div>
  </div>
</div>

<script>
const SERVER_ID = {{ server_id | tojson }};
let _channels = [];

async function init() {
  if (!SERVER_ID) { toast('Kein Server ausgewählt', 'err'); return; }
  const [cr] = await Promise.all([
    fetch(`/api/setup/guild/${SERVER_ID}/channels`),
  ]);
  const cd = await cr.json();
  _channels = (cd.channels || []).filter(c => c.type === 0);
  fillChannels();
  await loadConfig();
}

function fillChannels() {
  const opts = '<option value="">(Kein Kanal)</option>' +
    _channels.map(c => `<option value="${esc(c.id)}">#${esc(c.name)}</option>`).join('');
  document.getElementById('cfg_welcome_channel').innerHTML =
    '<option value="">👋 Willkommens-Kanal wählen...</option>' +
    _channels.map(c => `<option value="${esc(c.id)}">#${esc(c.name)}</option>`).join('');
  document.getElementById('cfg_goodbye_channel').innerHTML = opts;
}

async function loadConfig() {
  const r = await fetch(`/api/setup/welcomer/${SERVER_ID}`);
  const d = await r.json();

  if (d.guild) {
    const icon = d.guild.icon ? `<img src="${esc(d.guild.icon)}" style="width:22px;height:22px;border-radius:50%;">` : '';
    document.getElementById('topbarGuild').innerHTML = `${icon}<span style="font-size:.83rem;color:var(--text2);">${esc(d.guild.name)}</span>`;
  }

  if (d.config) {
    const c = d.config;
    setTimeout(() => {
      if (c.welcome_channel_id) document.getElementById('cfg_welcome_channel').value = c.welcome_channel_id;
      if (c.goodbye_channel_id) document.getElementById('cfg_goodbye_channel').value = c.goodbye_channel_id;
      document.getElementById('cfg_welcome_enabled').checked = c.welcome_enabled !== false;
      document.getElementById('cfg_goodbye_enabled').checked = c.goodbye_enabled !== false;
    }, 100);
  }
}

async function saveConfig() {
  const body = {
    welcome_channel_id: document.getElementById('cfg_welcome_channel').value || null,
    goodbye_channel_id: document.getElementById('cfg_goodbye_channel').value || null,
    welcome_enabled:    document.getElementById('cfg_welcome_enabled').checked,
    goodbye_enabled:    document.getElementById('cfg_goodbye_enabled').checked,
  };
  const r = await fetch(`/api/setup/welcomer/${SERVER_ID}`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error || '?'), 'err'); return; }
  toast('Einstellungen gespeichert ✓', 'ok');
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(msg, type='ok') {
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type==='ok'?'✅':'❌'}</span><span>${esc(msg)}</span>`;
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

init();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# MEDIA SETUP HTML
# ══════════════════════════════════════════════════════════════════════════════

_MEDIA_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Medien-Weiterleitung Setup – Insel Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    .setup-body { max-width: 700px; margin: 0 auto; padding: 24px 22px; }
    .panel-card {
      background: var(--bg-card); border: 1px solid var(--border);
      border-radius: var(--r-lg); padding: 24px 26px; margin-bottom: 18px;
    }
    .panel-title { font-family:'Rajdhani',sans-serif; font-size:1.1rem; font-weight:700; color:var(--text); margin-bottom:4px; }
    .panel-sub   { font-size:0.82rem; color:var(--text3); margin-bottom:20px; }

    .field { margin-bottom:16px; }
    .field label { display:block; font-size:0.63rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--text3); margin-bottom:5px; }
    .field select {
      width:100%; background:var(--bg-surface); border:1px solid var(--border);
      color:var(--text); font-family:'Outfit',sans-serif; font-size:0.85rem;
      padding:9px 12px; border-radius:var(--r-sm); outline:none; transition:border-color var(--fast);
    }
    .field select:focus { border-color:var(--green2); box-shadow:0 0 0 3px rgba(34,197,94,.12); }
    .field select option { background:#1d2128; }
    .field-hint { font-size:0.70rem; color:var(--text3); margin-top:4px; }

    .toggle-row { display:flex; align-items:center; justify-content:space-between; padding:14px 0; border-bottom:1px solid var(--border); }
    .toggle-row:last-child { border-bottom:none; }
    .toggle-info { flex:1; min-width:0; }
    .toggle-label { font-size:0.85rem; color:var(--text); font-weight:500; display:flex; align-items:center; gap:7px; }
    .toggle-sub   { font-size:0.72rem; color:var(--text3); margin-top:2px; }
    .toggle-switch { position:relative; width:44px; height:24px; flex-shrink:0; margin-left:12px; }
    .toggle-switch input { opacity:0; width:0; height:0; }
    .toggle-slider {
      position:absolute; inset:0; cursor:pointer;
      background:var(--border2); border-radius:24px; transition:all var(--mid);
    }
    .toggle-slider:before {
      content:''; position:absolute; width:18px; height:18px; border-radius:50%;
      background:#fff; left:3px; top:3px; transition:all var(--mid);
    }
    .toggle-switch input:checked + .toggle-slider { background:var(--green2); }
    .toggle-switch input:checked + .toggle-slider:before { transform:translateX(20px); }

    .btn-group { display:flex; gap:8px; flex-wrap:wrap; margin-top:20px; }
    .btn {
      display:inline-flex; align-items:center; gap:6px;
      padding:9px 20px; border-radius:var(--r-sm);
      font-family:'Rajdhani',sans-serif; font-weight:700; font-size:0.88rem;
      cursor:pointer; border:none; transition:all var(--mid);
    }
    .btn-primary { background:var(--green2); color:#000; }
    .btn-primary:hover:not(:disabled) { filter:brightness(1.1); transform:translateY(-1px); }
    .btn:disabled { opacity:0.4; cursor:not-allowed; }

    .features-section { margin-top:6px; }
    .features-title { font-size:0.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.07em; color:var(--text3); margin-bottom:10px; }

    .toast-container { position:fixed; bottom:24px; right:24px; z-index:9999; display:flex; flex-direction:column; gap:8px; }
    .toast { background:var(--bg-card); border:1px solid var(--border2); border-radius:var(--r); padding:10px 16px; font-size:0.83rem; color:var(--text); box-shadow:var(--shadow); min-width:200px; animation:fadeDown .2s ease both; display:flex; align-items:center; gap:8px; }
    .toast.ok  { border-left:3px solid var(--green2); }
    .toast.err { border-left:3px solid var(--red); }
  </style>
</head>
<body class="detail-body">
<div class="toast-container" id="toastContainer"></div>

<div class="detail-topbar">
  <a href="/dashboard/setup" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    System Setup
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">📷 Medien-Weiterleitung</span>
  <div id="topbarGuild" style="margin-left:auto;display:flex;align-items:center;gap:8px;"></div>
</div>

<div class="setup-body">
  <div class="panel-card">
    <div class="panel-title">Medien-Weiterleitung</div>
    <div class="panel-sub">Bilder, Videos und Links aus anderen Kanälen werden automatisch in einen dedizierten Medienkanal weitergeleitet.</div>

    <div class="field">
      <label>📺 Ziel-Kanal <span style="color:var(--red)">*</span></label>
      <select id="cfg_image_channel">
        <option value="">Lade Kanäle...</option>
      </select>
      <div class="field-hint">Hier werden alle weitergeleiteten Medien gepostet.</div>
    </div>

    <div class="features-section">
      <div class="features-title">🔧 Features</div>

      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-label">📷 Bilder weiterleiten</div>
          <div class="toggle-sub">Bild-Anhänge aus anderen Kanälen</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="cfg_images" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>

      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-label">🎥 Videos weiterleiten</div>
          <div class="toggle-sub">Video-Anhänge (mp4, mov, …)</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="cfg_videos" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>

      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-label">▶️ YouTube-Links weiterleiten</div>
          <div class="toggle-sub">YouTube-URLs mit Thumbnail und Metadaten</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="cfg_youtube" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>

      <div class="toggle-row">
        <div class="toggle-info">
          <div class="toggle-label">💜 Twitch-Clips weiterleiten</div>
          <div class="toggle-sub">Clips von twitch.tv und clips.twitch.tv</div>
        </div>
        <label class="toggle-switch">
          <input type="checkbox" id="cfg_twitch" checked>
          <span class="toggle-slider"></span>
        </label>
      </div>
    </div>

    <div class="btn-group">
      <button class="btn btn-primary" onclick="saveConfig()">💾 Einstellungen speichern</button>
    </div>
  </div>
</div>

<script>
const SERVER_ID = {{ server_id | tojson }};
let _channels = [];

async function init() {
  if (!SERVER_ID) { toast('Kein Server ausgewählt', 'err'); return; }
  const r  = await fetch(`/api/setup/guild/${SERVER_ID}/channels`);
  const d  = await r.json();
  _channels = (d.channels || []).filter(c => c.type === 0);

  document.getElementById('cfg_image_channel').innerHTML =
    '<option value="">📺 Ziel-Kanal wählen *</option>' +
    _channels.map(c => `<option value="${esc(c.id)}">#${esc(c.name)}</option>`).join('');

  await loadConfig();
}

async function loadConfig() {
  const r = await fetch(`/api/setup/media/${SERVER_ID}`);
  const d = await r.json();

  if (d.guild) {
    const icon = d.guild.icon ? `<img src="${esc(d.guild.icon)}" style="width:22px;height:22px;border-radius:50%;">` : '';
    document.getElementById('topbarGuild').innerHTML = `${icon}<span style="font-size:.83rem;color:var(--text2);">${esc(d.guild.name)}</span>`;
  }

  if (d.config) {
    const c = d.config;
    setTimeout(() => {
      if (c.image_channel_id) document.getElementById('cfg_image_channel').value = c.image_channel_id;
      document.getElementById('cfg_images').checked  = c.forward_images  !== false;
      document.getElementById('cfg_videos').checked  = c.forward_videos  !== false;
      document.getElementById('cfg_youtube').checked = c.forward_youtube !== false;
      document.getElementById('cfg_twitch').checked  = c.forward_twitch  !== false;
    }, 100);
  }
}

async function saveConfig() {
  const channelId = document.getElementById('cfg_image_channel').value;
  if (!channelId) { toast('Bitte einen Ziel-Kanal auswählen', 'err'); return; }

  const body = {
    image_channel_id: channelId,
    forward_images:   document.getElementById('cfg_images').checked,
    forward_videos:   document.getElementById('cfg_videos').checked,
    forward_youtube:  document.getElementById('cfg_youtube').checked,
    forward_twitch:   document.getElementById('cfg_twitch').checked,
  };
  const r = await fetch(`/api/setup/media/${SERVER_ID}`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body),
  });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error || '?'), 'err'); return; }
  toast('Einstellungen gespeichert ✓', 'ok');
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function toast(msg, type='ok') {
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type==='ok'?'✅':'❌'}</span><span>${esc(msg)}</span>`;
  document.getElementById('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

init();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# TICKET SETUP PAGE HTML  –  unverändert aus dem Original
# ══════════════════════════════════════════════════════════════════════════════

_TICKET_SETUP_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Ticket-System Setup – Insel Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    .setup-body   { max-width: 1100px; margin: 0 auto; padding: 24px 22px; }
    .setup-cols   { display: grid; grid-template-columns: 260px 1fr; gap: 22px; margin-top: 22px; }
    @media(max-width:820px){ .setup-cols { grid-template-columns: 1fr; } }
    .stepper { position: sticky; top: 70px; }
    .step-item { display: flex; align-items: flex-start; gap: 10px; padding: 9px 0; cursor: pointer; transition: opacity var(--mid); }
    .step-item:not(.done):not(.active) { opacity: 0.5; }
    .step-num { width: 26px; height: 26px; border-radius: 50%; flex-shrink: 0; background: var(--bg-surface); border: 2px solid var(--border2); display: flex; align-items: center; justify-content: center; font-size: 0.7rem; font-weight: 700; color: var(--text3); transition: all var(--mid); }
    .step-item.active .step-num { background: var(--green2); border-color: var(--green2); color: #000; }
    .step-item.done .step-num { background: var(--green-dim); border-color: var(--green); color: var(--green); }
    .step-item.done .step-num::before { content: "✓"; font-size: 0.8rem; }
    .step-item.done .step-num span { display: none; }
    .step-label { font-size: 0.83rem; font-weight: 600; color: var(--text2); padding-top: 3px; }
    .step-item.active .step-label { color: var(--text); }
    .step-connector { width: 2px; height: 16px; background: var(--border); margin-left: 12px; transition: background var(--mid); }
    .step-connector.done { background: var(--green-dim); }
    .panel-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--r-lg); padding: 24px 26px; margin-bottom: 18px; display: none; }
    .panel-card.active { display: block; }
    .panel-title { font-family: 'Rajdhani', sans-serif; font-size: 1.15rem; font-weight: 700; color: var(--text); margin-bottom: 4px; }
    .panel-sub { font-size: 0.82rem; color: var(--text3); margin-bottom: 20px; }
    .field { margin-bottom: 14px; }
    .field label { display: block; font-size: 0.63rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em; color: var(--text3); margin-bottom: 5px; }
    .field select, .field input[type=text], .field input[type=number], .field textarea { width: 100%; background: var(--bg-surface); border: 1px solid var(--border); color: var(--text); font-family: 'Outfit', sans-serif; font-size: 0.85rem; padding: 9px 12px; border-radius: var(--r-sm); outline: none; transition: border-color var(--fast); }
    .field select:focus, .field input:focus, .field textarea:focus { border-color: var(--green2); box-shadow: 0 0 0 3px rgba(34,197,94,0.12); }
    .field select option { background: #1d2128; }
    .field textarea { resize: vertical; min-height: 72px; }
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    @media(max-width:600px){ .field-row { grid-template-columns: 1fr; } }
    .field-hint { font-size: 0.70rem; color: var(--text3); margin-top: 4px; }
    .btn-group { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 18px; }
    .btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 18px; border-radius: var(--r-sm); font-family: 'Rajdhani', sans-serif; font-weight: 700; font-size: 0.88rem; cursor: pointer; border: none; transition: all var(--mid); text-decoration: none !important; }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-primary { background: var(--green2); color: #000; }
    .btn-primary:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
    .btn-outline { background: transparent; color: var(--text2); border: 1px solid var(--border2); }
    .btn-outline:hover:not(:disabled) { border-color: var(--green2); color: var(--green2); }
    .btn-danger { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }
    .btn-ghost { background: var(--bg-surface); color: var(--text2); border: 1px solid var(--border); }
    .btn-ghost:hover:not(:disabled) { border-color: var(--border3); color: var(--text); }
    .module-list { display: flex; flex-direction: column; gap: 10px; margin-bottom: 16px; }
    .module-item { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--r); padding: 13px 15px; display: flex; align-items: center; gap: 12px; transition: all var(--mid); }
    .module-item:hover { border-color: var(--border3); }
    .module-item-icon { font-size: 1.2rem; flex-shrink: 0; }
    .module-item-info { flex: 1; min-width: 0; }
    .module-item-name { font-weight: 700; color: var(--text); font-size: 0.88rem; margin-bottom: 2px; }
    .module-item-meta { font-size: 0.72rem; color: var(--text3); }
    .module-item-actions { display: flex; gap: 6px; flex-shrink: 0; }
    .icon-btn { width: 28px; height: 28px; border-radius: var(--r-sm); display: flex; align-items: center; justify-content: center; cursor: pointer; border: 1px solid var(--border); background: var(--bg-card); color: var(--text3); font-size: 0.9rem; transition: all var(--fast); }
    .icon-btn:hover { border-color: var(--green2); color: var(--green); }
    .icon-btn.del:hover { border-color: var(--red-border); color: var(--red); }
    .modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.7); z-index: 9998; display: none; align-items: center; justify-content: center; }
    .modal-overlay.open { display: flex; }
    .modal-box { background: var(--bg-card); border: 1px solid var(--border2); border-radius: var(--r-xl); padding: 28px 30px; width: 100%; max-width: 560px; max-height: 90vh; overflow-y: auto; box-shadow: var(--shadow-xl); animation: fadeUp 0.2s ease both; }
    .modal-title { font-family: 'Rajdhani', sans-serif; font-size: 1.1rem; font-weight: 700; color: var(--text); margin-bottom: 18px; display: flex; align-items: center; justify-content: space-between; }
    .modal-close { background: none; border: none; color: var(--text3); cursor: pointer; font-size: 1.2rem; padding: 2px 6px; border-radius: 4px; transition: color var(--fast); line-height: 1; }
    .modal-close:hover { color: var(--text); }
    .role-chips { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; min-height: 28px; }
    .role-chip { display: inline-flex; align-items: center; gap: 4px; background: var(--green-g2); border: 1px solid rgba(74,222,128,0.2); color: var(--green); padding: 2px 8px; border-radius: 12px; font-size: 0.73rem; font-weight: 600; }
    .role-chip-del { background: none; border: none; color: rgba(74,222,128,0.6); cursor: pointer; padding: 0 1px; line-height: 1; font-size: 0.85rem; transition: color var(--fast); }
    .role-chip-del:hover { color: var(--red); }
    .toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
    .toast { background: var(--bg-card); border: 1px solid var(--border2); border-radius: var(--r); padding: 10px 16px; font-size: 0.83rem; color: var(--text); box-shadow: var(--shadow); min-width: 220px; animation: fadeDown 0.2s ease both; display: flex; align-items: center; gap: 8px; }
    .toast.ok   { border-left: 3px solid var(--green2); }
    .toast.err  { border-left: 3px solid var(--red); }
    .toast.info { border-left: 3px solid var(--blue); }
    .progress-bar { height: 3px; background: var(--border); border-radius: 2px; margin-bottom: 22px; overflow: hidden; }
    .progress-fill { height: 100%; background: var(--green2); border-radius: 2px; transition: width 0.4s ease; }
    .panel-preview { background: var(--bg-raised); border: 1px solid var(--border2); border-left: 3px solid var(--green2); border-radius: var(--r); padding: 14px 16px; margin-top: 14px; }
    .preview-title { font-family: 'Rajdhani', sans-serif; font-weight: 700; color: var(--text); margin-bottom: 6px; font-size: 0.95rem; }
    .preview-desc  { font-size: 0.82rem; color: var(--text2); margin-bottom: 10px; }
    .preview-field { padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 0.80rem; display: flex; gap: 8px; }
    .preview-field:last-child { border-bottom: none; }
    .preview-field-name  { font-weight: 700; color: var(--text); flex-shrink: 0; }
    .preview-field-value { color: var(--text2); }
    .success-banner { background: rgba(74,222,128,0.08); border: 1px solid rgba(74,222,128,0.25); border-radius: var(--r); padding: 16px 18px; margin-top: 16px; display: none; }
    .success-banner.show { display: block; }
    .success-banner-title { font-family: 'Rajdhani', sans-serif; font-weight: 700; color: var(--green); margin-bottom: 4px; }
    .success-banner-sub { font-size: 0.80rem; color: var(--text2); }
    .info-callout { background: var(--bg-surface); border: 1px solid var(--border); border-left: 3px solid var(--blue); border-radius: var(--r-sm); padding: 10px 14px; font-size: 0.80rem; color: var(--text2); line-height: 1.6; margin-bottom: 16px; }
  </style>
</head>
<body class="detail-body">
<div class="toast-container" id="toastContainer"></div>
<div class="modal-overlay" id="moduleModal">
  <div class="modal-box">
    <div class="modal-title">
      <span id="modalTitle">Modul bearbeiten</span>
      <button class="modal-close" onclick="closeModal()">✕</button>
    </div>
    <div id="modalBody"></div>
  </div>
</div>
<div class="detail-topbar">
  <a href="/dashboard/setup" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    System Setup
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title" id="pageTitle">🎫 Ticket-System</span>
  <div id="topbarGuild" style="margin-left:auto;display:flex;align-items:center;gap:8px;"></div>
</div>
<div class="setup-body">
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:20%"></div></div>
  <div class="setup-cols">
    <div class="stepper" id="stepper">
      <div class="step-item active" id="step-nav-1" onclick="goStep(1)">
        <div class="step-num"><span>1</span></div>
        <div class="step-label">Server-Kanäle</div>
      </div>
      <div class="step-connector" id="conn-1"></div>
      <div class="step-item" id="step-nav-2" onclick="goStep(2)">
        <div class="step-num"><span>2</span></div>
        <div class="step-label">Ticket-Module</div>
      </div>
      <div class="step-connector" id="conn-2"></div>
      <div class="step-item" id="step-nav-3" onclick="goStep(3)">
        <div class="step-num"><span>3</span></div>
        <div class="step-label">Panel senden</div>
      </div>
      <div class="step-connector" id="conn-3"></div>
      <div class="step-item" id="step-nav-4" onclick="goStep(4)">
        <div class="step-num"><span>4</span></div>
        <div class="step-label">Fertig</div>
      </div>
    </div>
    <div id="stepContent">
      <div class="panel-card active" id="step-1">
        <div class="panel-title">Schritt 1 – Server-Kanäle & Kategorie</div>
        <div class="panel-sub">Wähle die Kanäle und Kategorien die das Ticket-System nutzen soll.</div>
        <div class="field">
          <label>📢 Panel-Kanal <span style="color:var(--red)">*</span></label>
          <select id="cfg_panel_channel"><option value="">Lade Kanäle...</option></select>
          <div class="field-hint">Hier wird der Ticket-Erstellungs-Button gesendet.</div>
        </div>
        <div class="field">
          <label>📁 Standard-Kategorie <span style="color:var(--red)">*</span></label>
          <select id="cfg_category"><option value="">Lade Kategorien...</option></select>
          <div class="field-hint">Neue Ticket-Kanäle werden in dieser Kategorie erstellt.</div>
        </div>
        <div class="field-row">
          <div class="field">
            <label>📋 Log-Kanal</label>
            <select id="cfg_log_channel"><option value="">(Optional) Ticket-Logs</option></select>
          </div>
          <div class="field">
            <label>🔔 Staff-Ping Kanal</label>
            <select id="cfg_ping_channel"><option value="">(Optional) Staff-Pings</option></select>
          </div>
        </div>
        <div class="btn-group">
          <button class="btn btn-primary" onclick="saveServerConfig()">Speichern & Weiter →</button>
        </div>
      </div>
      <div class="panel-card" id="step-2">
        <div class="panel-title">Schritt 2 – Ticket-Module</div>
        <div class="panel-sub">Füge die Kategorien / Abteilungen hinzu für die Tickets erstellt werden können.</div>
        <div class="info-callout">Jedes Modul erscheint als eigener Button im Panel. Mitglieder wählen das passende Modul aus und öffnen ein Ticket.</div>
        <div class="module-list" id="moduleList">
          <div style="color:var(--text3);font-size:0.83rem;padding:8px 0;">Noch keine Module. Füge das erste Modul hinzu.</div>
        </div>
        <div class="btn-group">
          <button class="btn btn-primary" onclick="openAddModuleModal()">➕ Modul hinzufügen</button>
          <button class="btn btn-outline" onclick="goStep(1)">← Zurück</button>
          <button class="btn btn-ghost" onclick="goStep(3)" id="nextToPanel">Weiter → Panel senden</button>
        </div>
      </div>
      <div class="panel-card" id="step-3">
        <div class="panel-title">Schritt 3 – Panel senden</div>
        <div class="panel-sub">Sende den Ticket-Erstellungs-Button in den konfigurierten Kanal.</div>
        <div class="field">
          <label>📌 Panel-Titel</label>
          <input type="text" id="panel_title" value="🎫 Support-Tickets">
        </div>
        <div class="field">
          <label>📝 Panel-Beschreibung</label>
          <textarea id="panel_desc">Wähle ein Modul aus den Buttons unten um ein Ticket zu erstellen.</textarea>
        </div>
        <div id="panelPreview" class="panel-preview" style="display:none;"></div>
        <div class="info-callout" style="margin-top:14px;">
          ⚠️ Die interaktiven Buttons werden aktiv sobald der Discord-Bot neu gestartet wird oder <code>/ticket_bearbeiten</code> ausgeführt wird.
        </div>
        <div class="btn-group">
          <button class="btn btn-primary" onclick="sendPanel()">📤 Panel jetzt senden</button>
          <button class="btn btn-outline" onclick="goStep(2)">← Zurück</button>
        </div>
        <div class="success-banner" id="panelSuccess">
          <div class="success-banner-title">✅ Panel gesendet!</div>
          <div class="success-banner-sub">Das Ticket-Panel wurde erfolgreich in den Kanal gesendet.</div>
        </div>
      </div>
      <div class="panel-card" id="step-4">
        <div class="panel-title" style="color:var(--green);">✅ Setup abgeschlossen!</div>
        <div class="panel-sub">Das Ticket-System ist fertig konfiguriert.</div>
        <div style="margin-top:16px;display:flex;flex-direction:column;gap:10px;">
          <div style="background:var(--bg-surface);border:1px solid var(--border);border-radius:var(--r);padding:13px 16px;">
            <div style="font-size:0.80rem;font-weight:700;color:var(--text2);margin-bottom:4px;">Nächste Schritte</div>
            <ul style="font-size:0.80rem;color:var(--text3);line-height:1.8;padding-left:16px;margin:0;">
              <li>Starte den Bot neu oder nutze <code>/ticket_bearbeiten</code> um die Buttons zu aktivieren</li>
              <li>Teste das Panel indem du auf einen Button klickst</li>
              <li>Nutze diese Seite jederzeit um Module zu bearbeiten oder das Panel neu zu senden</li>
            </ul>
          </div>
        </div>
        <div class="btn-group" style="margin-top:20px;">
          <a href="/dashboard/tickets" class="btn btn-primary">→ Zum Ticket-Dashboard</a>
          <button class="btn btn-outline" onclick="goStep(1)">Einstellungen bearbeiten</button>
        </div>
      </div>
    </div>
  </div>
</div>
<script>
const SERVER_ID = {{ server_id | tojson }};
let _step = 1;
let _channels = [];
let _roles = [];
let _modules = [];
let _editModId = null;
let _editRoleIds = [];

async function init() {
  if (!SERVER_ID) { toast('Kein Server ausgewählt', 'err'); return; }
  await Promise.all([loadChannels(), loadConfig()]);
  updateProgress();
}

async function loadChannels() {
  const r  = await fetch(`/api/setup/guild/${SERVER_ID}/channels`);
  const d  = await r.json();
  _channels = d.channels || [];
  const r2 = await fetch(`/api/setup/guild/${SERVER_ID}/roles`);
  const d2 = await r2.json();
  _roles = d2.roles || [];
  fillChannelSelects();
}

function fillChannelSelects() {
  const text = _channels.filter(c => c.type === 0);
  const cats = _channels.filter(c => c.type === 4);
  function opts(arr, blank) { return `<option value="">${blank}</option>` + arr.map(c => `<option value="${esc(c.id)}">#${esc(c.name)}</option>`).join(''); }
  function catOpts(arr) { return `<option value="">📁 Kategorie wählen *</option>` + arr.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join(''); }
  q('cfg_panel_channel').innerHTML = opts(text, '📢 Panel-Kanal wählen *');
  q('cfg_category').innerHTML = catOpts(cats);
  q('cfg_log_channel').innerHTML = opts(text, '(Optional) Log-Kanal');
  q('cfg_ping_channel').innerHTML = opts(text, '(Optional) Staff-Ping Kanal');
}

async function loadConfig() {
  const r = await fetch(`/api/setup/tickets/${SERVER_ID}`);
  const d = await r.json();
  if (d.guild) {
    const icon = d.guild.icon ? `<img src="${esc(d.guild.icon)}" style="width:22px;height:22px;border-radius:50%;">` : '';
    q('topbarGuild').innerHTML = `${icon}<span style="font-size:0.83rem;color:var(--text2);">${esc(d.guild.name)}</span>`;
  }
  if (d.config) {
    const c = d.config;
    setVal('cfg_panel_channel', c.panel_channel_id);
    setVal('cfg_category', c.category_id);
    setVal('cfg_log_channel', c.log_channel_id);
    setVal('cfg_ping_channel', c.staff_ping_channel_id);
  }
  _modules = d.modules || [];
  renderModules();
  if (d.config && _modules.length > 0) goStep(2, false);
}

function setVal(id, val) {
  const el = q(id);
  if (el && val) setTimeout(() => { if (el) el.value = val || ''; }, 100);
}

function goStep(n, animated=true) {
  document.querySelectorAll('.panel-card').forEach(el => el.classList.remove('active'));
  q(`step-${n}`).classList.add('active');
  for (let i = 1; i <= 4; i++) {
    const nav = q(`step-nav-${i}`);
    nav.classList.remove('active', 'done');
    if (i < n) nav.classList.add('done');
    else if (i === n) nav.classList.add('active');
    const conn = q(`conn-${i}`);
    if (conn) conn.classList.toggle('done', i < n);
  }
  _step = n; updateProgress();
  if (n === 3) buildPanelPreview();
}

function updateProgress() {
  const pct = [20, 45, 72, 100][_step - 1] || 20;
  q('progressFill').style.width = pct + '%';
}

async function saveServerConfig() {
  const panel = q('cfg_panel_channel').value;
  const cat   = q('cfg_category').value;
  if (!panel) { toast('Bitte Panel-Kanal auswählen', 'err'); return; }
  if (!cat)   { toast('Bitte Kategorie auswählen', 'err');   return; }
  const body = {
    panel_channel_id: panel, category_id: cat,
    log_channel_id: q('cfg_log_channel').value || null,
    staff_ping_channel_id: q('cfg_ping_channel').value || null,
  };
  const r = await fetch(`/api/setup/tickets/${SERVER_ID}`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error || '?'), 'err'); return; }
  toast('Server-Einstellungen gespeichert ✓', 'ok');
  goStep(2);
}

function renderModules() {
  const list = q('moduleList');
  if (!_modules.length) {
    list.innerHTML = '<div style="color:var(--text3);font-size:0.83rem;padding:8px 0;">Noch keine Module. Füge das erste Modul hinzu.</div>';
    return;
  }
  list.innerHTML = _modules.map(m => {
    const emoji = m.button_emoji && !m.button_emoji.startsWith('<') ? m.button_emoji : '🎫';
    const roleCount = (m.staff_role_ids || []).length;
    return `<div class="module-item">
      <div class="module-item-icon">${esc(emoji)}</div>
      <div class="module-item-info">
        <div class="module-item-name">${esc(m.name)}</div>
        <div class="module-item-meta">${esc(m.description ? m.description.slice(0,60) : '')} · Max: ${m.max_tickets}/User · ${roleCount} Staff-Rollen</div>
      </div>
      <div class="module-item-actions">
        <button class="icon-btn" onclick="openEditModuleModal(${m.id})">✏️</button>
        <button class="icon-btn del" onclick="deleteModule(${m.id})">🗑️</button>
      </div>
    </div>`;
  }).join('');
}

async function deleteModule(modId) {
  if (!confirm('Modul wirklich löschen?')) return;
  const r = await fetch(`/api/setup/tickets/${SERVER_ID}/modules/${modId}`, { method: 'DELETE' });
  if (r.ok) { _modules = _modules.filter(m => m.id !== modId); renderModules(); toast('Modul gelöscht', 'ok'); }
  else { const d = await r.json(); toast('Fehler: ' + (d.error || '?'), 'err'); }
}

function openAddModuleModal() {
  _editModId = null; _editRoleIds = [];
  q('modalTitle').textContent = '➕ Neues Modul';
  renderModalBody({});
  q('moduleModal').classList.add('open');
}

function openEditModuleModal(modId) {
  _editModId = modId;
  const mod = _modules.find(m => m.id === modId);
  if (!mod) return;
  _editRoleIds = [...(mod.staff_role_ids || [])];
  q('modalTitle').textContent = `✏️ Modul bearbeiten: ${mod.name}`;
  renderModalBody(mod);
  q('moduleModal').classList.add('open');
}

function closeModal() { q('moduleModal').classList.remove('open'); }

function renderModalBody(mod = {}) {
  const cats = _channels.filter(c => c.type === 4);
  const catOptions = `<option value="">(Globale Standard-Kategorie)</option>` + cats.map(c => `<option value="${esc(c.id)}" ${mod.category_id === c.id ? 'selected':''}>${esc(c.name)}</option>`).join('');
  q('modalBody').innerHTML = `
    <div class="field-row">
      <div class="field"><label>Modul-Name *</label><input type="text" id="mod_name" value="${esc(mod.name||'')}" placeholder="z.B. Support"></div>
      <div class="field"><label>Button-Emoji</label><input type="text" id="mod_emoji" value="${esc(mod.button_emoji||'🎫')}" placeholder="🎫"></div>
    </div>
    <div class="field"><label>Beschreibung *</label><input type="text" id="mod_desc" value="${esc(mod.description||'')}" placeholder="z.B. Hilfe bei Problemen"></div>
    <div class="field"><label>Modal-Anweisung</label><textarea id="mod_question" placeholder="Was soll der User beschreiben?">${esc(mod.modal_question||'Bitte beschreibe dein Anliegen.')}</textarea></div>
    <div class="field-row">
      <div class="field"><label>Max. Tickets pro User</label><input type="number" id="mod_max" value="${mod.max_tickets||1}" min="1" max="10"></div>
      <div class="field"><label>Eigene Kategorie</label><select id="mod_category">${catOptions}</select></div>
    </div>
    <div class="field">
      <label>Staff-Rollen</label>
      <div class="role-chips" id="modRoleChips"></div>
      <select id="modRoleAdd" onchange="addModuleRole(this)" style="margin-top:8px;width:100%;background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-family:'Outfit',sans-serif;font-size:0.85rem;padding:8px 10px;border-radius:var(--r-sm);outline:none;">
        <option value="">+ Rolle hinzufügen...</option>
        ${_roles.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('')}
      </select>
    </div>
    <div class="btn-group" style="margin-top:6px;">
      <button class="btn btn-primary" onclick="saveModule()">💾 Speichern</button>
      <button class="btn btn-outline" onclick="closeModal()">Abbrechen</button>
    </div>`;
  renderModRoleChips();
}

function renderModRoleChips() {
  const chips = q('modRoleChips');
  if (!chips) return;
  if (!_editRoleIds.length) { chips.innerHTML = '<span style="font-size:.73rem;color:var(--text3);">Keine Staff-Rollen</span>'; return; }
  chips.innerHTML = _editRoleIds.map(rid => {
    const role = _roles.find(r => r.id === rid);
    return `<span class="role-chip">${esc(role?.name || rid)}<button class="role-chip-del" onclick="removeModuleRole('${esc(rid)}')">✕</button></span>`;
  }).join('');
}

function addModuleRole(sel) {
  const val = sel.value;
  if (!val || _editRoleIds.includes(val)) { sel.value=''; return; }
  _editRoleIds.push(val); sel.value=''; renderModRoleChips();
}

function removeModuleRole(rid) { _editRoleIds = _editRoleIds.filter(r => r !== rid); renderModRoleChips(); }

async function saveModule() {
  const name = q('mod_name')?.value.trim();
  const desc = q('mod_desc')?.value.trim();
  if (!name) { toast('Modul-Name fehlt', 'err'); return; }
  if (!desc) { toast('Beschreibung fehlt', 'err'); return; }
  const body = {
    name, description: desc,
    modal_question: q('mod_question')?.value.trim() || 'Bitte beschreibe dein Anliegen.',
    max_tickets: parseInt(q('mod_max')?.value || '1'),
    button_emoji: q('mod_emoji')?.value.trim() || '🎫',
    category_id: q('mod_category')?.value || null,
    staff_role_ids: _editRoleIds,
  };
  let r;
  if (_editModId) {
    r = await fetch(`/api/setup/tickets/${SERVER_ID}/modules/${_editModId}`, { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
  } else {
    r = await fetch(`/api/setup/tickets/${SERVER_ID}/modules`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
  }
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error || '?'), 'err'); return; }
  toast(_editModId ? 'Modul gespeichert ✓' : 'Modul hinzugefügt ✓', 'ok');
  closeModal();
  const mr = await fetch(`/api/setup/tickets/${SERVER_ID}/modules`);
  const md = await mr.json();
  _modules = md.modules || [];
  renderModules();
}

function buildPanelPreview() {
  const title = q('panel_title')?.value || '🎫 Support-Tickets';
  const desc  = q('panel_desc')?.value  || '';
  const prev  = q('panelPreview');
  if (!_modules.length) { prev.style.display='none'; return; }
  prev.style.display = '';
  prev.innerHTML = `
    <div class="preview-title">${esc(title)}</div>
    <div class="preview-desc">${esc(desc)}</div>
    ${_modules.map(m => {
      const emoji = m.button_emoji && !m.button_emoji.startsWith('<') ? m.button_emoji : '🎫';
      return `<div class="preview-field"><span class="preview-field-name">${esc(emoji)} ${esc(m.name)}</span><span class="preview-field-value">${esc(m.description)}</span></div>`;
    }).join('')}`;
}

async function sendPanel() {
  if (!_modules.length) { toast('Bitte erst Module hinzufügen', 'err'); return; }
  const body = {
    panel_title: q('panel_title')?.value || '🎫 Support-Tickets',
    panel_desc:  q('panel_desc')?.value  || '',
  };
  const r = await fetch(`/api/setup/tickets/${SERVER_ID}/panel`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error || '?'), 'err'); return; }
  q('panelSuccess').classList.add('show');
  toast('Panel erfolgreich gesendet ✓', 'ok');
  setTimeout(() => goStep(4), 1500);
}

function toast(msg, type='info') {
  const icons = { ok:'✅', err:'❌', info:'ℹ️' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${icons[type]||''}</span><span>${esc(msg)}</span>`;
  q('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function q(id) { return document.getElementById(id); }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

document.addEventListener('input', e => {
  if (e.target.id === 'panel_title' || e.target.id === 'panel_desc') buildPanelPreview();
});

init();
</script>
</body>
</html>"""