"""
bot/core/web_app/flask_app/voice_setup_routes.py
=================================================
Web-basiertes Voice Channel Creator Setup.

In app.py einbinden:
    from .voice_setup_routes import register_voice_setup_routes
    register_voice_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url)

ROUTEN:
  GET  /dashboard/setup/voice               – Setup-Seite (Template)
  GET  /api/setup/voice/<server_id>         – Konfiguration laden
  POST /api/setup/voice/<server_id>         – Konfiguration speichern + Kanal erstellen
  DELETE /api/setup/voice/<server_id>       – Voice Creator deaktivieren + Kanäle löschen
  GET  /api/setup/voice/<server_id>/permissions  – Bot-Berechtigungen prüfen
  GET  /api/setup/voice/<server_id>/channels    – Aktive VC-Kanäle auflisten
"""

from __future__ import annotations

import os
import requests as req_lib
from flask import jsonify, request, render_template, session

BOT_TOKEN   = os.getenv("DISCORD_TOKEN", "")
DISCORD_API = "https://discord.com/api/v10"


def register_voice_setup_routes(app, login_required, _is_mbl, _bot_get, _cached_guild, _guild_icon_url):

    def _check_access(user: dict, server_id: str) -> bool:
        if _is_mbl(user):
            return True
        return server_id in (user.get("server_roles") or {})

    # ── GET /dashboard/setup/voice ────────────────────────────────────────────
    @app.route("/dashboard/setup/voice")
    @login_required
    def setup_voice():
        user      = session["user"]
        server_id = request.args.get("server_id", "")
        return render_template("setup_voice.html", user=user, server_id=server_id)

    # ── GET /api/setup/voice/<server_id> ──────────────────────────────────────
    @app.route("/api/setup/voice/<server_id>", methods=["GET"])
    @login_required
    def api_get_voice_config(server_id):
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            row = sb.table("voice_creator_config").select("*").eq("server_id", server_id).execute()
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

    # ── POST /api/setup/voice/<server_id> ─────────────────────────────────────
    @app.route("/api/setup/voice/<server_id>", methods=["POST"])
    @login_required
    def api_save_voice_config(server_id):
        """
        Speichert die Voice-Creator-Konfiguration und erstellt / aktualisiert
        den Erstell-Kanal auf Discord.
        """
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403

        if not BOT_TOKEN:
            return jsonify({"error": "Bot-Token nicht konfiguriert"}), 500

        try:
            from bot.core.supabase_client import get_supabase
            sb   = get_supabase()
            body = request.get_json() or {}

            category_id      = body.get("category_id") or None
            channel_name     = body.get("channel_name", "➕  Kanal erstellen").strip() or "➕  Kanal erstellen"
            empty_timeout    = max(10, int(body.get("empty_timeout", 30)))
            creator_role_ids = body.get("creator_role_ids", [])
            allowed_role_ids = body.get("allowed_role_ids", [])

            if isinstance(creator_role_ids, list):
                creator_role_ids = ",".join(creator_role_ids)
            if isinstance(allowed_role_ids, list):
                allowed_role_ids = ",".join(allowed_role_ids)

            headers = {
                "Authorization": f"Bot {BOT_TOKEN}",
                "Content-Type": "application/json",
            }

            # ── Bestehende Konfiguration laden ────────────────────────────────
            existing_row = sb.table("voice_creator_config").select("*").eq("server_id", server_id).execute()
            existing_cfg = existing_row.data[0] if existing_row.data else None
            old_channel_id = existing_cfg.get("channel_id") if existing_cfg else None

            # ── Alten Erstell-Kanal löschen wenn vorhanden ────────────────────
            if old_channel_id:
                try:
                    req_lib.delete(
                        f"{DISCORD_API}/channels/{old_channel_id}",
                        headers=headers, timeout=8,
                    )
                except Exception:
                    pass  # Kanal existiert vielleicht nicht mehr

            # ── Berechtigungen für den neuen Erstell-Kanal aufbauen ───────────
            # @everyone darf sehen + beitreten, aber nicht sprechen
            overwrites = [
                {
                    "id":    server_id,   # @everyone
                    "type":  0,           # role
                    "allow": str(0x100400),  # VIEW_CHANNEL + CONNECT
                    "deny":  str(0x200000),  # SPEAK
                },
                {
                    "id":    _get_bot_user_id(headers),
                    "type":  1,           # member
                    "allow": str(0x100400 | 0x10000000 | 0x400),  # VIEW + CONNECT + MOVE + MANAGE_CHANNELS
                    "deny":  "0",
                },
            ]
            # Panel-Rollen ebenfalls berechtigen
            for role_id in (allowed_role_ids or "").split(","):
                role_id = role_id.strip()
                if role_id:
                    overwrites.append({
                        "id":    role_id,
                        "type":  0,
                        "allow": str(0x100400),
                        "deny":  "0",
                    })

            # ── Neuen Erstell-Kanal erstellen ─────────────────────────────────
            channel_payload = {
                "name":                  channel_name,
                "type":                  2,           # GUILD_VOICE
                "permission_overwrites": overwrites,
            }
            if category_id:
                channel_payload["parent_id"] = category_id

            create_r = req_lib.post(
                f"{DISCORD_API}/guilds/{server_id}/channels",
                headers=headers,
                json=channel_payload,
                timeout=10,
            )

            if not create_r.ok:
                return jsonify({
                    "error": f"Kanal konnte nicht erstellt werden: {create_r.status_code} – {create_r.text[:200]}"
                }), 502

            new_channel_id = create_r.json().get("id")

            # ── Config in DB speichern ────────────────────────────────────────
            data = {
                "server_id":       server_id,
                "category_id":     category_id,
                "channel_id":      new_channel_id,
                "channel_name":    channel_name,
                "empty_timeout":   empty_timeout,
                "creator_role_ids": creator_role_ids,
                "allowed_role_ids": allowed_role_ids,
            }

            if existing_cfg:
                sb.table("voice_creator_config").update(data).eq("server_id", server_id).execute()
            else:
                sb.table("voice_creator_config").insert(data).execute()

            return jsonify({
                "ok":          True,
                "channel_id":  new_channel_id,
                "channel_name": channel_name,
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── DELETE /api/setup/voice/<server_id> ───────────────────────────────────
    @app.route("/api/setup/voice/<server_id>", methods=["DELETE"])
    @login_required
    def api_delete_voice_config(server_id):
        """
        Deaktiviert den Voice Creator:
          - Löscht den Erstell-Kanal
          - Löscht alle aktiven Voice-Kanäle dieses Servers
          - Entfernt die Konfiguration aus der DB
        """
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403

        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()

            headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

            # Konfiguration laden
            cfg_row = sb.table("voice_creator_config").select("*").eq("server_id", server_id).execute()
            if cfg_row.data:
                cfg = cfg_row.data[0]

                # Erstell-Kanal löschen
                if cfg.get("channel_id"):
                    try:
                        req_lib.delete(f"{DISCORD_API}/channels/{cfg['channel_id']}", headers=headers, timeout=8)
                    except Exception:
                        pass

                sb.table("voice_creator_config").delete().eq("server_id", server_id).execute()

            # Aktive VC-Kanäle löschen
            vcs = sb.table("voice_channels").select("*").eq("server_id", server_id).execute().data or []
            for vc in vcs:
                for ch_id in filter(None, [vc.get("main_channel_id"), vc.get("wait_channel_id")]):
                    try:
                        req_lib.delete(f"{DISCORD_API}/channels/{ch_id}", headers=headers, timeout=8)
                    except Exception:
                        pass
                # Zugangs-Rolle löschen
                if vc.get("access_role_id"):
                    try:
                        req_lib.delete(
                            f"{DISCORD_API}/guilds/{server_id}/roles/{vc['access_role_id']}",
                            headers=headers, timeout=8,
                        )
                    except Exception:
                        pass
            sb.table("voice_channels").delete().eq("server_id", server_id).execute()

            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── GET /api/setup/voice/<server_id>/permissions ──────────────────────────
    @app.route("/api/setup/voice/<server_id>/permissions")
    @login_required
    def api_voice_permissions(server_id):
        """Prüft ob der Bot die nötigen Berechtigungen auf dem Server hat."""
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403

        try:
            guild_data = _bot_get(f"/guilds/{server_id}")
            if not guild_data:
                return jsonify({"permissions": {}, "error": "Guild nicht gefunden"}), 404

            # Bot-Member-Daten laden
            bot_user_id = _get_bot_user_id({"Authorization": f"Bot {BOT_TOKEN}"})
            member_data = _bot_get(f"/guilds/{server_id}/members/{bot_user_id}")
            if not member_data:
                return jsonify({"permissions": {}}), 200

            # Rollen des Bots und deren Permissions
            role_ids    = set(str(r) for r in (member_data.get("roles") or []))
            guild_roles = guild_data.get("roles") or _bot_get(f"/guilds/{server_id}/roles") or []

            combined_perms = 0
            for role in guild_roles:
                if str(role.get("id")) == server_id or str(role.get("id")) in role_ids:
                    combined_perms |= int(role.get("permissions", 0))

            has_admin = bool(combined_perms & 0x8)

            perms = {
                "manage_roles":    has_admin or bool(combined_perms & 0x10000000),
                "manage_channels": has_admin or bool(combined_perms & 0x10),
                "move_members":    has_admin or bool(combined_perms & 0x1000000),
                "view_channel":    has_admin or bool(combined_perms & 0x400),
                "connect":         has_admin or bool(combined_perms & 0x100000),
                "send_messages":   has_admin or bool(combined_perms & 0x800),
                "administrator":   has_admin,
            }
            return jsonify({"permissions": perms})
        except Exception as e:
            return jsonify({"permissions": {}, "error": str(e)}), 500

    # ── GET /api/setup/voice/<server_id>/channels ─────────────────────────────
    @app.route("/api/setup/voice/<server_id>/channels")
    @login_required
    def api_voice_active_channels(server_id):
        """Gibt alle aktiven Voice-Kanäle dieses Servers zurück."""
        user = session["user"]
        if not _check_access(user, server_id):
            return jsonify({"error": "Kein Zugriff"}), 403

        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            vcs = sb.table("voice_channels").select("*").eq("server_id", server_id).execute().data or []

            # Owner-Namen über Bot-Token anreichern
            result = []
            for vc in vcs:
                owner_id   = vc.get("owner_id", "")
                owner_name = owner_id
                if owner_id:
                    member = _bot_get(f"/guilds/{server_id}/members/{owner_id}")
                    if member:
                        u = member.get("user", {})
                        owner_name = member.get("nick") or u.get("global_name") or u.get("username") or owner_id

                result.append({
                    "owner_id":        owner_id,
                    "owner_name":      owner_name,
                    "main_channel_id": vc.get("main_channel_id"),
                    "wait_channel_id": vc.get("wait_channel_id"),
                    "is_open":         vc.get("is_open", True),
                    "user_limit":      vc.get("user_limit", 0),
                    "created_at":      (vc.get("created_at") or "")[:16].replace("T", " "),
                })

            return jsonify({"channels": result})
        except Exception as e:
            return jsonify({"error": str(e)}), 500


# ── Helper ─────────────────────────────────────────────────────────────────────

def _get_bot_user_id(headers: dict) -> str:
    """Holt die Bot-User-ID über die Discord-API (@me)."""
    try:
        r = req_lib.get(
            f"{DISCORD_API}/users/@me",
            headers=headers,
            timeout=5,
        )
        if r.ok:
            return r.json().get("id", "")
    except Exception:
        pass
    return ""