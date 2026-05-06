"""
bot/core/web_app/flask_app/voting_routes.py
=============================================
NEUE FEATURES:
  - Voter-Log: Wer hat abgestimmt (Discord-User-ID + Name, NICHT was)
  - Voting Creator: Seite für MBL zum Erstellen/Bearbeiten von Abstimmungs-JSONs
  - Voter-Liste Route: /vote/<voting_id>/voters (nur MBL + ausgewählte Viewer)
  - API zum Verwalten von JSON-Dateien im Dateisystem

NEUE SUPABASE TABLE:
    CREATE TABLE IF NOT EXISTS voting_voter_log (
        id           BIGSERIAL PRIMARY KEY,
        voting_id    TEXT NOT NULL REFERENCES votings(id),
        user_id      TEXT NOT NULL,
        display_name TEXT,
        username     TEXT,
        avatar_url   TEXT,
        submitted_at TIMESTAMPTZ DEFAULT now(),
        UNIQUE (voting_id, user_id)
    );
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from flask import (
    jsonify, render_template_string, request, redirect, session, Response
)

VOTER_SALT   = os.getenv("VOTER_SALT", secrets.token_hex(16))
MBL_ID       = os.getenv("MBL", "")
BOT_TOKEN    = os.getenv("DISCORD_TOKEN", "")
DISCORD_API  = "https://discord.com/api/v10"

CLIENT_ID     = os.getenv("DISCORD_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
WEB_BASE_URL  = os.getenv("WEB_BASE_URL", "http://localhost:5000").rstrip("/")

# Verzeichnis für Abstimmungs-JSON-Dateien
_BOT_ROOT   = Path(__file__).resolve().parent.parent.parent.parent.parent
VOTINGS_DIR = _BOT_ROOT / "votings"
RESULTS_DIR = _BOT_ROOT / "results"

import requests as req_lib
import logging
log = logging.getLogger("voting_routes")


def _voter_hash(user_id: str, voting_id: str) -> str:
    raw = f"{user_id}:{voting_id}:{VOTER_SALT}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _bot_get(path: str):
    if not BOT_TOKEN:
        return None
    try:
        r = req_lib.get(f"{DISCORD_API}{path}",
                        headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=8)
        return r.json() if r.ok else None
    except Exception:
        return None


def _get_guild_members(guild_id: str) -> list[dict]:
    members = _bot_get(f"/guilds/{guild_id}/members?limit=1000") or []
    result = []
    for m in members:
        u = m.get("user", {})
        if u.get("bot"):
            continue
        result.append({
            "id":      u.get("id", ""),
            "display": m.get("nick") or u.get("global_name") or u.get("username") or "?",
            "username": u.get("username", ""),
            "avatar": (
                f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=32"
                if u.get("avatar") else None
            ),
        })
    return result


def _discord_oauth_url(voting_id: str, random_part: str) -> str:
    state = f"{voting_id}:{random_part}"
    params = urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  f"{WEB_BASE_URL}/vote/callback",
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    return f"https://discord.com/oauth2/authorize?{params}"


def _exchange_code(voting_id: str, code: str) -> dict | None:
    try:
        r = req_lib.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  f"{WEB_BASE_URL}/vote/callback",
            },
            timeout=8,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def _get_discord_user(access_token: str) -> dict | None:
    try:
        r = req_lib.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def _safe_filename(name: str) -> str:
    """Sanitize filename."""
    name = re.sub(r'[^\w\-_. äöüÄÖÜß]', '', name).strip()
    if not name.endswith(".json"):
        name += ".json"
    return name or "abstimmung.json"


def register_voting_routes(app, login_required=None, _is_mbl_fn=None, _bot_get_fn=None):
    from bot.core.supabase_client import get_supabase

    def _is_mbl_user(user: dict) -> bool:
        return bool(MBL_ID and user.get("id") == MBL_ID)

    def _can_see_voters(user: dict, voting: dict) -> bool:
        """MBL oder explizit als viewer eingetragen."""
        if _is_mbl_user(user):
            return True
        uid = user.get("id", "")
        allowed = voting.get("allowed_viewers") or []
        if isinstance(allowed, str):
            allowed = [x.strip() for x in allowed.split(",") if x.strip()]
        return uid in allowed

    # ── GET /vote/<voting_id> ─────────────────────────────────────────────────
    @app.route("/vote/<voting_id>")
    def vote_page(voting_id):
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)
        voting = r.data[0]

        if not voting.get("is_active", True):
            return _render_error(
                "Abstimmung geschlossen",
                "Diese Abstimmung ist nicht mehr aktiv.",
                403,
                show_results_link=f"/vote/{voting_id}/results",
            )

        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return redirect(f"/vote/{voting_id}/login")

        user_id   = voter_session["user_id"]
        user_name = voter_session.get("display_name", "?")

        voter_hash = _voter_hash(user_id, voting_id)
        existing = sb.table("voting_responses")\
            .select("id")\
            .eq("voting_id", voting_id)\
            .eq("voter_hash", voter_hash)\
            .execute()
        if existing.data:
            return _render_already_voted(voting, voting_id, user_name)

        return render_template_string(
            _VOTE_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            user_name=user_name,
            enumerate=enumerate,
        )

    @app.route("/vote/<voting_id>/login")
    def vote_login(voting_id):
        sb = get_supabase()
        r = sb.table("votings").select("title, is_active").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)
        voting = r.data[0]

        if not voting.get("is_active", True):
            return _render_error("Abstimmung geschlossen", "Diese Abstimmung ist nicht mehr aktiv.", 403)

        if not CLIENT_ID or not CLIENT_SECRET:
            return _render_error("Konfigurationsfehler", "Discord OAuth2 ist nicht konfiguriert.", 500)

        random_part = secrets.token_urlsafe(16)
        oauth_url = _discord_oauth_url(voting_id, random_part)
        session["vote_oauth_state"] = f"{voting_id}:{random_part}"

        return render_template_string(_LOGIN_TEMPLATE, voting=voting, voting_id=voting_id, oauth_url=oauth_url)

    @app.route("/vote/callback")
    def vote_callback():
        sb = get_supabase()

        stored_state = session.pop("vote_oauth_state", "")
        given_state  = request.args.get("state", "")

        if not stored_state or stored_state != given_state:
            return _render_error("Ungültiger State", "Bitte versuche es erneut.", 400, show_login_link="/vote")

        if "error" in request.args:
            return _render_error("Zugriff verweigert", "Discord-Login wurde abgelehnt.", 403, show_login_link="/vote")

        code = request.args.get("code", "")
        if not code:
            return _render_error("Kein Code", "OAuth-Code fehlt.", 400, show_login_link="/vote")

        voting_id = given_state.split(":", 1)[0]

        r = sb.table("votings").select("is_active").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)

        tok = _exchange_code(voting_id, code)
        if not tok or "access_token" not in tok:
            return _render_error("Token-Fehler", "Discord-Login fehlgeschlagen.", 500,
                                 show_login_link=f"/vote/{voting_id}/login")

        discord_user = _get_discord_user(tok["access_token"])
        if not discord_user or "id" not in discord_user:
            return _render_error("Profil-Fehler", "Nutzerdaten konnten nicht geladen werden.", 500,
                                 show_login_link=f"/vote/{voting_id}/login")

        user_id      = discord_user["id"]
        display_name = discord_user.get("global_name") or discord_user.get("username") or "?"
        username     = discord_user.get("username", "")
        av           = discord_user.get("avatar")
        disc         = int(discord_user.get("discriminator") or 0) % 5
        avatar_url   = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{av}.png?size=64"
            if av else f"https://cdn.discordapp.com/embed/avatars/{disc}.png"
        )

        session[f"voter_{voting_id}"] = {
            "user_id":      user_id,
            "display_name": display_name,
            "username":     username,
            "avatar_url":   avatar_url,
        }

        # Prüfen, ob bereits abgestimmt
        voter_hash_val = _voter_hash(user_id, voting_id)
        existing = sb.table("voting_responses")\
            .select("id")\
            .eq("voting_id", voting_id)\
            .eq("voter_hash", voter_hash_val)\
            .execute()
        if existing.data:
            voting_data = sb.table("votings").select("*").eq("id", voting_id).execute().data[0]
            return _render_already_voted(voting_data, voting_id, display_name)

        return redirect(f"/vote/{voting_id}")

    @app.route("/vote/<voting_id>/logout")
    def vote_logout(voting_id):
        session.pop(f"voter_{voting_id}", None)
        return redirect(f"/vote/{voting_id}/login")

    @app.route("/vote/<voting_id>/submit", methods=["POST"])
    def vote_submit(voting_id):
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Abstimmung nicht gefunden"}), 404
        voting = r.data[0]

        if not voting.get("is_active", True):
            return jsonify({"error": "Abstimmung ist geschlossen"}), 403

        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return jsonify({"error": "Nicht eingeloggt"}), 401

        user_id      = voter_session["user_id"]
        display_name = voter_session.get("display_name", "?")
        username     = voter_session.get("username", "")
        avatar_url   = voter_session.get("avatar_url")
        voter_hash_val = _voter_hash(user_id, voting_id)

        # Doppelte Abstimmung prüfen
        existing = sb.table("voting_responses")\
            .select("id")\
            .eq("voting_id", voting_id)\
            .eq("voter_hash", voter_hash_val)\
            .execute()
        if existing.data:
            return jsonify({"error": "Du hast bereits abgestimmt"}), 409

        body          = request.get_json() or {}
        answers       = body.get("answers", {})
        encrypted_data = body.get("encrypted_data", None)
        is_encrypted  = bool(encrypted_data)
        stored_data   = encrypted_data if is_encrypted else json.dumps(answers, ensure_ascii=False)

        try:
            sb.table("voting_responses").insert({
                "voting_id":    voting_id,
                "voter_hash":   voter_hash_val,
                "answers":      stored_data,
                "is_encrypted": is_encrypted,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            log.error(f"[vote_submit] {e}")
            return jsonify({"error": "Fehler beim Speichern"}), 500

        # ── NEU: Voter-Log eintragen (wer hat abgestimmt, nicht was) ──────────
        try:
            sb.table("voting_voter_log").upsert({
                "voting_id":    voting_id,
                "user_id":      user_id,
                "display_name": display_name,
                "username":     username,
                "avatar_url":   avatar_url,
                "submitted_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="voting_id,user_id").execute()
        except Exception as e:
            log.warning(f"[vote_submit] voter_log insert failed: {e}")

        # Session nach Abstimmung löschen
        session.pop(f"voter_{voting_id}", None)

        return jsonify({"ok": True, "message": "Danke für deine Teilnahme!"})

    # ── GET /vote/<voting_id>/voters ──────────────────────────────────────────
    @app.route("/vote/<voting_id>/voters")
    def vote_voters(voting_id):
        """Zeigt wer abgestimmt hat (nur MBL + allowed_viewers)."""
        sb   = get_supabase()
        r    = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "", 404)
        voting = r.data[0]

        # Zugriff prüfen: eingeloggter Flask-User oder voter_session
        flask_user = session.get("user", {})
        if not _can_see_voters(flask_user, voting):
            return _render_error("Kein Zugriff", "Du hast keinen Zugriff auf diese Seite.", 403)

        # Voter-Log laden
        try:
            voters_r = sb.table("voting_voter_log")\
                .select("*")\
                .eq("voting_id", voting_id)\
                .order("submitted_at", desc=False)\
                .execute()
            voters = voters_r.data or []
        except Exception as e:
            log.error(f"[vote_voters] {e}")
            voters = []

        total_resp = len(sb.table("voting_responses").select("id").eq("voting_id", voting_id).execute().data or [])

        return render_template_string(
            _VOTERS_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            voters=voters,
            total_responses=total_resp,
        )

    # ── GET /vote/<voting_id>/results ─────────────────────────────────────────
    @app.route("/vote/<voting_id>/results")
    def vote_results(voting_id):
        from flask import session as flask_session
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)
        voting = r.data[0]

        user    = flask_session.get("user", {})
        is_mbl  = bool(MBL_ID and user.get("id") == MBL_ID)

        if voting.get("is_active") and not is_mbl:
            return _render_error("Zugriff verweigert", "Die Ergebnisse sind erst nach Ende der Abstimmung verfügbar.", 403)

        responses = sb.table("voting_responses")\
            .select("answers, is_encrypted, submitted_at")\
            .eq("voting_id", voting_id)\
            .execute().data or []

        public_results_exist = (RESULTS_DIR / f"{voting_id}.json").exists()

        return render_template_string(
            _RESULTS_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            responses=responses,
            is_mbl=is_mbl,
            public_results_exist=public_results_exist,
        )

    @app.route("/vote/<voting_id>/reconstruct")
    def vote_reconstruct(voting_id):
        from flask import session as flask_session
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "", 404)
        voting = r.data[0]

        if not voting.get("public_key"):
            return _render_error("Keine Verschlüsselung", "Diese Abstimmung verwendet keine Verschlüsselung.", 400)

        user   = flask_session.get("user", {})
        is_mbl = bool(MBL_ID and user.get("id") == MBL_ID)

        responses = []
        if not voting.get("is_active") or is_mbl:
            responses = sb.table("voting_responses")\
                .select("answers, is_encrypted, submitted_at")\
                .eq("voting_id", voting_id)\
                .execute().data or []

        results_file      = RESULTS_DIR / f"{voting_id}.json"
        already_published = results_file.exists()

        return render_template_string(
            _RECONSTRUCT_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            responses=responses,
            is_mbl=is_mbl,
            already_published=already_published,
        )

    @app.route("/api/vote/<voting_id>/members")
    def api_vote_members(voting_id):
        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return jsonify({"error": "Nicht eingeloggt"}), 401

        sb = get_supabase()
        r  = sb.table("votings").select("server_id, allowed_users").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Nicht gefunden"}), 404
        voting    = r.data[0]
        server_id = voting.get("server_id", "")
        members   = _get_guild_members(server_id)
        return jsonify({"members": members})

    # ── API: Voter-Log (JSON) ─────────────────────────────────────────────────
    @app.route("/api/vote/<voting_id>/voters")
    def api_vote_voters(voting_id):
        """JSON-API: Wer hat abgestimmt (nur MBL + allowed_viewers)."""
        user = session.get("user", {})
        sb   = get_supabase()
        r    = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Nicht gefunden"}), 404
        voting = r.data[0]

        if not _can_see_voters(user, voting):
            return jsonify({"error": "Kein Zugriff"}), 403

        try:
            voters_r = sb.table("voting_voter_log")\
                .select("user_id, display_name, username, avatar_url, submitted_at")\
                .eq("voting_id", voting_id)\
                .order("submitted_at", desc=False)\
                .execute()
            voters = voters_r.data or []
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        total = len(sb.table("voting_responses").select("id").eq("voting_id", voting_id).execute().data or [])
        return jsonify({"voters": voters, "total_responses": total, "tracked": len(voters)})

    # ── POST /api/vote/<id>/publish-results  (nur MBL) ────────────────────────
    @app.route("/api/vote/<voting_id>/publish-results", methods=["POST"])
    def api_publish_results(voting_id):
        """Speichert entschlüsselte Ergebnisse als öffentliche JSON-Datei."""
        from flask import session as flask_session
        import json as _json, datetime as _dt
        user   = flask_session.get("user", {})
        is_mbl = bool(MBL_ID and user.get("id") == MBL_ID)
        if not is_mbl:
            return jsonify({"error": "Kein Zugriff"}), 403

        sb = get_supabase()
        r  = sb.table("votings").select("id, title, questions").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Abstimmung nicht gefunden"}), 404

        payload = request.get_json(silent=True)
        if not payload or "results" not in payload:
            return jsonify({"error": "Keine Ergebnisse übergeben"}), 400

        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = {
            "voting_id":    voting_id,
            "title":        r.data[0].get("title", ""),
            "questions":    r.data[0].get("questions", []),
            "published_at": _dt.datetime.utcnow().isoformat() + "Z",
            "entries":      payload["results"],
        }
        result_path = RESULTS_DIR / f"{voting_id}.json"
        result_path.write_text(_json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

        public_url = f"{WEB_BASE_URL}/vote/{voting_id}/public-results"
        return jsonify({"ok": True, "url": public_url})

    # ── GET /vote/<id>/public-results  (öffentlich für alle) ─────────────────
    @app.route("/vote/<voting_id>/public-results")
    def vote_public_results(voting_id):
        """Zeigt veröffentlichte, entschlüsselte Ergebnisse – für jeden zugänglich."""
        import json as _json
        result_path = RESULTS_DIR / f"{voting_id}.json"
        if not result_path.exists():
            return _render_error(
                "Keine Ergebnisse veröffentlicht",
                "Für diese Abstimmung wurden noch keine Ergebnisse veröffentlicht.",
                404,
            )
        data = _json.loads(result_path.read_text(encoding="utf-8"))
        return render_template_string(_PUBLIC_RESULTS_TEMPLATE, data=data, voting_id=voting_id)


    # ══════════════════════════════════════════════════════════════════════════
    # VOTING CREATOR (nur MBL)
    # ══════════════════════════════════════════════════════════════════════════

    def _mbl_required(f):
        from functools import wraps
        @wraps(f)
        def decorated(*args, **kwargs):
            user = session.get("user", {})
            if not user:
                return redirect("/login")
            if not _is_mbl_user(user):
                return _render_error("Kein Zugriff", "Nur MBL hat Zugriff auf diese Seite.", 403)
            return f(*args, **kwargs)
        return decorated

    @app.route("/dashboard/voting-creator")
    @_mbl_required
    def voting_creator():
        """Seite für MBL zum Erstellen/Bearbeiten von Abstimmungs-JSONs."""
        user = session["user"]
        return render_template_string(_VOTING_CREATOR_TEMPLATE, user=user)

    # ── API: JSON-Dateien auflisten ───────────────────────────────────────────
    @app.route("/api/voting/files", methods=["GET"])
    @_mbl_required
    def api_voting_files_list():
        VOTINGS_DIR.mkdir(parents=True, exist_ok=True)
        files = []
        for f in sorted(VOTINGS_DIR.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                files.append({
                    "filename": f.name,
                    "path":     str(f.relative_to(_BOT_ROOT)),
                    "abs_path": str(f),
                    "title":    data.get("Kategorie", f.stem),
                    "size":     f.stat().st_size,
                    "modified": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                })
            except Exception as e:
                files.append({"filename": f.name, "path": str(f.relative_to(_BOT_ROOT)),
                              "abs_path": str(f), "title": f.stem, "error": str(e)})
        return jsonify({"files": files, "votings_dir": str(VOTINGS_DIR)})

    # ── API: Einzelne Datei laden ─────────────────────────────────────────────
    @app.route("/api/voting/files/<path:filename>", methods=["GET"])
    @_mbl_required
    def api_voting_file_get(filename):
        safe = _safe_filename(Path(filename).name)
        path = (VOTINGS_DIR / safe).resolve()
        if not str(path).startswith(str(VOTINGS_DIR.resolve())):
            return jsonify({"error": "Ungültiger Pfad"}), 400
        if not path.exists():
            return jsonify({"error": "Datei nicht gefunden"}), 404
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return jsonify({"data": data, "filename": safe})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── API: Datei speichern (neu oder bearbeiten) ────────────────────────────
    @app.route("/api/voting/files", methods=["POST"])
    @_mbl_required
    def api_voting_file_save():
        body     = request.get_json() or {}
        filename = _safe_filename(body.get("filename", "abstimmung"))
        data     = body.get("data")
        if not data:
            return jsonify({"error": "Keine Daten angegeben"}), 400

        VOTINGS_DIR.mkdir(parents=True, exist_ok=True)
        path = (VOTINGS_DIR / filename).resolve()
        if not str(path).startswith(str(VOTINGS_DIR.resolve())):
            return jsonify({"error": "Ungültiger Pfad"}), 400

        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            rel_path = str(path.relative_to(_BOT_ROOT))
            return jsonify({
                "ok":       True,
                "filename": filename,
                "path":     rel_path,
                "abs_path": str(path),
                "command":  f"/abstimmung erstellen json_pfad:{rel_path} server_id:<SERVER_ID>",
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── API: Datei löschen ────────────────────────────────────────────────────
    @app.route("/api/voting/files/<path:filename>", methods=["DELETE"])
    @_mbl_required
    def api_voting_file_delete(filename):
        safe = _safe_filename(Path(filename).name)
        path = (VOTINGS_DIR / safe).resolve()
        if not str(path).startswith(str(VOTINGS_DIR.resolve())):
            return jsonify({"error": "Ungültiger Pfad"}), 400
        if not path.exists():
            return jsonify({"error": "Datei nicht gefunden"}), 404
        try:
            path.unlink()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── API: Validate JSON ────────────────────────────────────────────────────
    @app.route("/api/voting/validate", methods=["POST"])
    @_mbl_required
    def api_voting_validate():
        data = request.get_json() or {}
        errors = []
        if "Kategorie" not in data:         errors.append("'Kategorie' fehlt")
        if "Beschreibung" not in data:      errors.append("'Beschreibung' fehlt")
        if "Zur_Auswahl" not in data:       errors.append("'Zur_Auswahl' fehlt")
        fragen = data.get("Fragen", [])
        if not isinstance(fragen, list) or not fragen:
            errors.append("'Fragen' fehlt oder ist leer")
        else:
            for i, frage in enumerate(fragen):
                if not isinstance(frage, dict):
                    errors.append(f"Frage {i+1}: kein Objekt")
                    continue
                if "Frage" not in frage:    errors.append(f"Frage {i+1}: 'Frage' fehlt")
                if "Typ" not in frage:      errors.append(f"Frage {i+1}: 'Typ' fehlt")
                if frage.get("Typ") in ("choice", "person") and "Optionen" not in frage:
                    errors.append(f"Frage {i+1}: 'Optionen' fehlt")
        return jsonify({"valid": len(errors) == 0, "errors": errors})


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════


# ── Public Results Template ───────────────────────────────────────────────────
_PUBLIC_RESULTS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{{ data.title }} – Ergebnisse</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono&display=swap">
<style>
:root{--bg:#060708;--surface:#0f1117;--card:#161b24;--card2:#1c2130;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--dim:#3d4660;--accent:#5b8cff;--green:#4ade80;--gold:#fbbf24;--red:#f87171;--r:12px;--r2:8px;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:48px 22px;-webkit-font-smoothing:antialiased;}
.page{max-width:760px;margin:0 auto;}
h1{font-size:1.8rem;font-weight:700;margin-bottom:8px;}
.sub{color:var(--sub);font-size:.9rem;margin-bottom:32px;}
.pub-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.25);border-radius:20px;padding:4px 12px;font-size:.75rem;font-weight:600;color:var(--green);margin-bottom:28px;}
.stat-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:32px;}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;flex:1;min-width:120px;}
.stat-num{font-size:1.6rem;font-weight:700;color:var(--accent);font-family:'DM Mono',monospace;}
.stat-lbl{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);margin-top:4px;}
.q-block{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:24px;margin-bottom:16px;}
.q-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:6px;}
.q-title{font-size:1rem;font-weight:500;margin-bottom:18px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:160px;font-size:.85rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-track{flex:1;background:var(--border2);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{height:100%;background:var(--accent);border-radius:4px;transition:width .5s ease;}
.bar-count{font-family:'DM Mono',monospace;font-size:.78rem;color:var(--sub);width:40px;text-align:right;flex-shrink:0;}
.answer-row{background:var(--card2);border:1px solid var(--border2);border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.88rem;color:var(--sub);}
.answer-row span{color:var(--text);}
</style>
</head>
<body>
<div class="page">
  <h1>📊 {{ data.title }}</h1>
  <div class="sub">Veröffentlichte Ergebnisse</div>
  <div class="pub-badge">✅ Öffentlich veröffentlicht am {{ data.published_at[:10] }}</div>

  <div class="stat-row">
    <div class="stat-box"><div class="stat-num" id="totalCount">–</div><div class="stat-lbl">Antworten</div></div>
    <div class="stat-box"><div class="stat-num">{{ data.questions | length }}</div><div class="stat-lbl">Fragen</div></div>
  </div>

  {% for frage in data.questions %}
  <div class="q-block" data-q-idx="{{ loop.index0 }}" data-q-type="{{ frage.Typ }}">
    <div class="q-label">Frage {{ loop.index }}</div>
    <div class="q-title">{{ frage.Frage }}</div>
    <div class="q-results" id="qr-{{ loop.index0 }}"></div>
  </div>
  {% endfor %}
</div>
<script>
const DATA = {{ data | tojson }};
const entries = DATA.entries || [];
document.getElementById('totalCount').textContent = entries.length;
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
DATA.questions.forEach((frage, qi) => {
  const container = document.getElementById('qr-' + qi);
  const vals = entries.map(e => e.data && e.data[frage.Frage]).filter(v => v !== null && v !== undefined);
  if (frage.Typ === 'text') {
    container.innerHTML = vals.map(v => `<div class="answer-row"><span>${esc(String(v))}</span></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
  } else {
    const counts = {}; vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { const key = item?.name || item || '?'; counts[key] = (counts[key]||0)+1; }); });
    const sorted = Object.entries(counts).sort((a,b)=>b[1]-a[1]); const max = sorted[0]?.[1] || 1;
    container.innerHTML = sorted.map(([k,c]) => `<div class="bar-row"><div class="bar-label" title="${esc(k)}">${esc(k)}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.round(c/max*100)}%"></div></div><div class="bar-count">${c}</div></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
  }
});
</script>
</body>
</html>"""

# ── Voters Template ───────────────────────────────────────────────────────────
_VOTERS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ voting.title }} – Teilnehmer</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap">
<style>
:root{--bg:#060708;--surface:#0f1117;--card:#161b24;--card2:#1c2130;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--dim:#3d4660;--accent:#5b8cff;--green:#4ade80;--red:#f87171;--gold:#fbbf24;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:40px 22px;-webkit-font-smoothing:antialiased;}
.page{max-width:800px;margin:0 auto;}
.topbar{display:flex;align-items:center;gap:12px;margin-bottom:32px;padding-bottom:20px;border-bottom:1px solid var(--border);}
h1{font-size:1.5rem;font-weight:700;}
.sub{color:var(--sub);font-size:.9rem;margin-top:4px;}
.stats{display:flex;gap:12px;margin-bottom:28px;flex-wrap:wrap;}
.stat{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:16px 20px;flex:1;min-width:140px;}
.stat-num{font-size:1.8rem;font-weight:700;color:var(--green);font-family:'DM Mono',monospace;line-height:1;}
.stat-lbl{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);margin-top:4px;}
.voter-list{display:flex;flex-direction:column;gap:8px;}
.voter-item{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 18px;display:flex;align-items:center;gap:14px;transition:border-color .15s;}
.voter-item:hover{border-color:var(--border2);}
.voter-avatar{width:40px;height:40px;border-radius:50%;object-fit:cover;flex-shrink:0;}
.voter-avatar-ph{width:40px;height:40px;border-radius:50%;background:linear-gradient(135deg,#3d6fff,#5b8cff);display:flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:700;color:#fff;flex-shrink:0;}
.voter-info{flex:1;min-width:0;}
.voter-name{font-weight:600;font-size:.92rem;}
.voter-username{font-size:.76rem;color:var(--sub);font-family:'DM Mono',monospace;}
.voter-time{font-size:.72rem;color:var(--dim);font-family:'DM Mono',monospace;flex-shrink:0;}
.badge-anon{display:inline-flex;align-items:center;gap:5px;background:rgba(74,222,128,.1);color:var(--green);border:1px solid rgba(74,222,128,.2);border-radius:8px;padding:4px 10px;font-size:.72rem;font-weight:600;margin-bottom:20px;}
.notice{background:var(--card);border:1px solid var(--border);border-left:3px solid var(--gold);border-radius:10px;padding:14px 18px;font-size:.85rem;color:var(--sub);margin-bottom:24px;line-height:1.6;}
.back-link{color:var(--sub);text-decoration:none;font-size:.85rem;display:flex;align-items:center;gap:6px;}
.back-link:hover{color:var(--text);}
.empty{text-align:center;padding:60px 20px;color:var(--dim);}
.empty-icon{font-size:2.5rem;margin-bottom:12px;opacity:.4;}
</style>
</head>
<body>
<div class="page">
  <div class="topbar">
    <a href="/vote/{{ voting_id }}/results" class="back-link">← Ergebnisse</a>
    <span style="color:var(--border);">|</span>
    <h1>{{ voting.title }} – Teilnehmer</h1>
  </div>

  <div class="notice">
    🔒 Diese Seite zeigt <strong>nur wer abgestimmt hat</strong> – nicht was abgestimmt wurde.
    Die Abstimmungsdaten selbst bleiben vollständig anonym.
  </div>

  <div class="stats">
    <div class="stat">
      <div class="stat-num">{{ total_responses }}</div>
      <div class="stat-lbl">Abgestimmt</div>
    </div>
    <div class="stat">
      <div class="stat-num">{{ voters | length }}</div>
      <div class="stat-lbl">Erfasst im Log</div>
    </div>
    <div class="stat">
      <div class="stat-num">{{ '✅' if not voting.is_active else '🟢' }}</div>
      <div class="stat-lbl">{{ 'Abgeschlossen' if not voting.is_active else 'Aktiv' }}</div>
    </div>
  </div>

  {% if voters %}
  <div class="voter-list">
    {% for v in voters %}
    <div class="voter-item">
      {% if v.avatar_url %}
        <img class="voter-avatar" src="{{ v.avatar_url }}" alt="" onerror="this.style.display='none'">
      {% else %}
        <div class="voter-avatar-ph">{{ (v.display_name or '?')[:2].upper() }}</div>
      {% endif %}
      <div class="voter-info">
        <div class="voter-name">{{ v.display_name or '?' }}</div>
        {% if v.username %}<div class="voter-username">@{{ v.username }}</div>{% endif %}
      </div>
      <div class="voter-time">{{ (v.submitted_at or '')[:16].replace('T', ' ') }}</div>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">
    <div class="empty-icon">🗳️</div>
    <div style="font-size:.9rem;">Noch niemand hat abgestimmt.</div>
  </div>
  {% endif %}
</div>
</body>
</html>"""


# ── Voting Creator Template ───────────────────────────────────────────────────
_VOTING_CREATOR_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Abstimmungs-Creator – Insel Bot</title>
<link rel="stylesheet" href="/static/css/main.css">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
/* ── Override Fonts ────────────────────────────── */
body { font-family: 'Space Grotesk', sans-serif; }

/* ── Layout ─────────────────────────────────────── */
.creator-wrap { max-width: 1200px; margin: 0 auto; padding: 24px 22px 80px; }
.creator-grid {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 22px;
  align-items: start;
}
@media(max-width:900px){ .creator-grid { grid-template-columns: 1fr; } }

/* ── Sidebar ─────────────────────────────────────── */
.files-panel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
  position: sticky;
  top: 80px;
}
.files-panel-hd {
  padding: 14px 18px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.files-panel-title {
  font-size: .65rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--text3);
}
.files-list {
  max-height: 400px;
  overflow-y: auto;
}
.file-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  cursor: pointer;
  transition: background var(--fast);
}
.file-item:hover { background: var(--bg-hover); }
.file-item.active { background: var(--green-glow); border-left: 2px solid var(--green2); }
.file-item-info { flex: 1; min-width: 0; }
.file-item-name { font-size: .83rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-item-meta { font-size: .68rem; color: var(--text3); margin-top: 2px; }
.file-del-btn {
  background: none;
  border: none;
  color: var(--text3);
  cursor: pointer;
  padding: 3px 5px;
  border-radius: 4px;
  font-size: .85rem;
  transition: color var(--fast);
  flex-shrink: 0;
}
.file-del-btn:hover { color: var(--red); }
.no-files { padding: 30px 16px; text-align: center; color: var(--text3); font-size: .82rem; }

/* ── Editor ─────────────────────────────────────── */
.editor-card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  overflow: hidden;
}
.editor-hd {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-surface);
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.editor-title { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); }

.editor-body { padding: 24px; }

/* ── Form Fields ─────────────────────────────────── */
.fe { margin-bottom: 18px; }
.fe label {
  display: block;
  font-size: .63rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .08em;
  color: var(--text3);
  margin-bottom: 6px;
}
.fe input[type=text], .fe input[type=number], .fe textarea, .fe select {
  width: 100%;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  color: var(--text);
  font-family: 'Space Grotesk', sans-serif;
  font-size: .87rem;
  padding: 9px 12px;
  border-radius: var(--r-sm);
  outline: none;
  transition: border-color var(--fast), box-shadow var(--fast);
}
.fe input:focus, .fe textarea:focus, .fe select:focus {
  border-color: var(--green2);
  box-shadow: 0 0 0 3px rgba(34,197,94,.12);
}
.fe select option { background: #1d2128; }
.fe textarea { resize: vertical; min-height: 80px; }
.fe-hint { font-size: .68rem; color: var(--text3); margin-top: 4px; }
.fe-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media(max-width:600px){ .fe-row { grid-template-columns: 1fr; } }

/* ── Toggle ─────────────────────────────────────── */
.toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--r-sm);
  margin-bottom: 8px;
}
.toggle-lbl { font-size: .84rem; font-weight: 500; }
.toggle-switch { position: relative; width: 42px; height: 22px; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0;
  cursor: pointer;
  background: var(--border2);
  border-radius: 22px;
  transition: background .2s;
}
.toggle-slider:before {
  content: '';
  position: absolute;
  width: 16px; height: 16px;
  border-radius: 50%;
  background: #fff;
  left: 3px; top: 3px;
  transition: transform .2s;
}
.toggle-switch input:checked + .toggle-slider { background: var(--green2); }
.toggle-switch input:checked + .toggle-slider:before { transform: translateX(20px); }

/* ── Questions ─────────────────────────────────── */
.section-title {
  font-family: 'Space Grotesk', sans-serif;
  font-size: .75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--text3);
  margin: 24px 0 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }

.question-card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 16px;
  margin-bottom: 10px;
  position: relative;
}
.question-card-hd {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.q-num {
  width: 24px; height: 24px;
  border-radius: 50%;
  background: var(--green-glow);
  color: var(--green);
  border: 1px solid rgba(74,222,128,.25);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: .7rem;
  font-weight: 700;
  flex-shrink: 0;
}
.q-type-badge {
  background: var(--bg-card);
  border: 1px solid var(--border2);
  color: var(--text3);
  padding: 2px 8px;
  border-radius: 20px;
  font-size: .64rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .06em;
}
.q-del {
  background: none; border: none;
  color: var(--text3); cursor: pointer;
  padding: 4px 7px; border-radius: 5px;
  font-size: .9rem; margin-left: auto;
  transition: color var(--fast);
}
.q-del:hover { color: var(--red); }
.q-move {
  background: none; border: none;
  color: var(--text3); cursor: pointer;
  padding: 4px 6px; border-radius: 5px;
  font-size: .85rem;
  transition: color var(--fast);
}
.q-move:hover { color: var(--text2); }

.options-editor { margin-top: 12px; }
.option-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.option-row input {
  flex: 1;
  background: var(--bg-card);
  border: 1px solid var(--border);
  color: var(--text);
  font-family: 'Space Grotesk', sans-serif;
  font-size: .84rem;
  padding: 7px 10px;
  border-radius: var(--r-sm);
  outline: none;
}
.option-row input:focus { border-color: var(--green2); }
.option-del {
  background: none; border: none;
  color: var(--text3); cursor: pointer;
  padding: 4px 7px; font-size: .9rem;
  border-radius: 4px;
  transition: color var(--fast);
}
.option-del:hover { color: var(--red); }

/* ── Buttons ─────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 9px 18px;
  border-radius: var(--r-sm);
  font-family: 'Space Grotesk', sans-serif;
  font-weight: 600;
  font-size: .87rem;
  cursor: pointer;
  border: none;
  transition: all var(--mid);
  text-decoration: none;
}
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--green2); color: #000; }
.btn-primary:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
.btn-outline { background: transparent; color: var(--text2); border: 1px solid var(--border2); }
.btn-outline:hover:not(:disabled) { border-color: var(--green2); color: var(--green2); }
.btn-ghost { background: var(--bg-surface); color: var(--text2); border: 1px solid var(--border); }
.btn-ghost:hover:not(:disabled) { border-color: var(--border3); color: var(--text); }
.btn-sm { padding: 6px 12px; font-size: .78rem; }
.btn-danger { background: var(--red-bg); color: var(--red); border: 1px solid var(--red-border); }
.btn-danger:hover:not(:disabled) { background: rgba(248,113,113,.18); }

.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; }

/* ── Add Question Menu ─────────────────────────── */
.add-q-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 12px 0; }
.add-q-btn {
  background: var(--bg-surface);
  border: 1px solid var(--border2);
  color: var(--text2);
  padding: 10px 14px;
  border-radius: var(--r-sm);
  font-family: 'Space Grotesk', sans-serif;
  font-size: .83rem;
  font-weight: 600;
  cursor: pointer;
  transition: all var(--fast);
  display: flex;
  align-items: center;
  gap: 8px;
  text-align: left;
}
.add-q-btn:hover { border-color: var(--green2); color: var(--green2); background: var(--green-g3); }

/* ── Toast ─────────────────────────────────────── */
.toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { background: var(--bg-card); border: 1px solid var(--border2); border-radius: var(--r); padding: 11px 16px; font-size: .83rem; color: var(--text); box-shadow: var(--shadow); min-width: 220px; display: flex; align-items: center; gap: 8px; animation: fadeDown .2s ease both; }
.toast.ok  { border-left: 3px solid var(--green2); }
.toast.err { border-left: 3px solid var(--red); }
.toast.info { border-left: 3px solid var(--blue); }

/* ── Command Box ─────────────────────────────────── */
.command-box {
  background: var(--bg-base);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 14px 16px;
  margin-top: 16px;
  display: none;
}
.command-box.show { display: block; }
.command-box-title { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--green); margin-bottom: 8px; }
.command-line {
  font-family: 'JetBrains Mono', monospace;
  font-size: .78rem;
  color: var(--text2);
  background: var(--bg-surface);
  border: 1px solid var(--border2);
  border-radius: var(--r-sm);
  padding: 10px 14px;
  word-break: break-all;
  cursor: pointer;
  transition: border-color var(--fast);
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.command-line:hover { border-color: var(--green2); }
.cmd-copy { background: none; border: none; color: var(--text3); cursor: pointer; font-size: .8rem; flex-shrink: 0; transition: color var(--fast); }
.cmd-copy:hover { color: var(--green); }
.command-note { font-size: .7rem; color: var(--text3); margin-top: 6px; }

/* ── Viewer IDs ─────────────────────────────────── */
.viewer-chip {
  display: inline-flex; align-items: center; gap: 4px;
  background: rgba(91,140,255,.1);
  border: 1px solid rgba(91,140,255,.25);
  color: #5b8cff;
  padding: 2px 8px; border-radius: 12px;
  font-size: .73rem; font-weight: 600;
  margin: 2px;
}
.viewer-chip-del { background: none; border: none; color: rgba(91,140,255,.6); cursor: pointer; padding: 0 2px; font-size: .85rem; transition: color var(--fast); }
.viewer-chip-del:hover { color: var(--red); }

/* ── Validation ─────────────────────────────────── */
.val-error { background: var(--red-bg); border: 1px solid var(--red-border); border-radius: var(--r-sm); padding: 10px 14px; font-size: .82rem; color: var(--red); margin-top: 8px; display: none; }
.val-error.show { display: block; }
.val-error ul { padding-left: 16px; margin-top: 4px; }
.val-error li { margin-bottom: 2px; }
</style>
</head>
<body class="detail-body">

<div class="toast-container" id="toastContainer"></div>

<!-- Topbar -->
<div class="detail-topbar">
  <a href="/dashboard" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    Dashboard
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">🗳️ Abstimmungs-Creator</span>
  <span style="margin-left:auto;font-size:.75rem;color:var(--text3);">Nur für MBL</span>
</div>

<div class="creator-wrap">
  <div class="creator-grid">

    <!-- ── SIDEBAR: Gespeicherte Dateien ── -->
    <div>
      <div class="files-panel">
        <div class="files-panel-hd">
          <span class="files-panel-title">💾 Gespeicherte Dateien</span>
          <button class="btn btn-sm btn-primary" onclick="newVoting()">+ Neu</button>
        </div>
        <div class="files-list" id="filesList">
          <div class="no-files">Lade...</div>
        </div>
      </div>
    </div>

    <!-- ── MAIN: Editor ── -->
    <div class="editor-card">
      <div class="editor-hd">
        <span class="editor-title">✏️ Abstimmung bearbeiten</span>
        <input type="text" id="filenameInput" placeholder="dateiname.json"
          style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;padding:5px 10px;border-radius:var(--r-sm);outline:none;width:180px;"
          oninput="onFilenameInput(this.value)">
        <div style="margin-left:auto;display:flex;gap:8px;">
          <button class="btn btn-outline btn-sm" onclick="validateCurrent()">🔍 Prüfen</button>
          <button class="btn btn-primary btn-sm" onclick="saveFile()">💾 Speichern</button>
        </div>
      </div>

      <div class="editor-body">

        <!-- Basisinfos -->
        <div class="fe-row">
          <div class="fe">
            <label>Kategorie / Titel <span style="color:var(--red)">*</span></label>
            <input type="text" id="fKategorie" placeholder="z.B. Server-Abstimmung Q1 2026">
          </div>
          <div class="fe">
            <label>Zur_Auswahl (User-IDs oder --All)</label>
            <input type="text" id="fZurAuswahl" value="--All" placeholder="--All oder 123,456,789">
            <div class="fe-hint">--All = alle; oder komma-getrennte Discord-IDs</div>
          </div>
        </div>

        <div class="fe">
          <label>Beschreibung <span style="color:var(--red)">*</span></label>
          <textarea id="fBeschreibung" placeholder="Kurze Beschreibung der Abstimmung..." rows="2"></textarea>
        </div>

        <!-- Voter-Viewer IDs (NEU) -->
        <div class="fe">
          <label>Voter-Log Betrachter (User-IDs, können sehen wer abgestimmt hat)</label>
          <div id="viewerChips" style="margin-bottom:6px;min-height:24px;"></div>
          <div style="display:flex;gap:8px;">
            <input type="text" id="viewerIdInput" placeholder="Discord User-ID eingeben..."
              style="flex:1;background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;"
              onkeydown="if(event.key==='Enter'){addViewer();event.preventDefault();}">
            <button class="btn btn-ghost btn-sm" onclick="addViewer()">+ Hinzufügen</button>
          </div>
          <div class="fe-hint">Diese User-IDs sehen unter /vote/ID/voters wer abgestimmt hat (neben MBL)</div>
        </div>

        <!-- RSA Key (optional) -->
        <div>
          <div class="toggle-row">
            <span class="toggle-lbl">🔐 Ende-zu-Ende Verschlüsselung</span>
            <label class="toggle-switch">
              <input type="checkbox" id="fEncEnabled" onchange="toggleEnc(this.checked)">
              <span class="toggle-slider"></span>
            </label>
          </div>
          <div id="encSection" style="display:none;margin-top:8px;">
            <div class="fe">
              <label>RSA Public Key (PEM)</label>
              <textarea id="fPublicKey" rows="4" placeholder="-----BEGIN PUBLIC KEY-----&#10;...&#10;-----END PUBLIC KEY-----"></textarea>
              <div class="fe-hint">Nur der Public Key wird hier gespeichert. Den Private Key für die Entschlüsselung sicher aufbewahren.</div>
            </div>
          </div>
        </div>

        <!-- Fragen -->
        <div class="section-title">❓ Fragen</div>
        <div id="questionsList"></div>

        <!-- Neue Frage hinzufügen -->
        <div class="add-q-grid">
          <button class="add-q-btn" onclick="addQuestion('text')">📝 Freitext</button>
          <button class="add-q-btn" onclick="addQuestion('choice')">☑️ Auswahl</button>
          <button class="add-q-btn" onclick="addQuestion('rating')">⭐ Bewertung</button>
          <button class="add-q-btn" onclick="addQuestion('person')">👤 Person</button>
        </div>

        <!-- Validation -->
        <div class="val-error" id="valError">
          <strong>⚠️ Fehler in der Konfiguration:</strong>
          <ul id="valErrorList"></ul>
        </div>

        <!-- Buttons -->
        <div class="btn-row">
          <button class="btn btn-primary" onclick="saveFile()">💾 Speichern</button>
          <button class="btn btn-outline" onclick="validateCurrent()">🔍 Validieren</button>
          <button class="btn btn-ghost" onclick="showJson()">{ } JSON anzeigen</button>
          <button class="btn btn-ghost" onclick="newVoting()">+ Neue Abstimmung</button>
        </div>

        <!-- Command Box -->
        <div class="command-box" id="commandBox">
          <div class="command-box-title">✅ Gespeichert – Discord Command</div>
          <div class="command-line" id="commandLine" onclick="copyCommand()" title="Klicken zum Kopieren">
            <span id="commandText"></span>
            <button class="cmd-copy">📋</button>
          </div>
          <div class="command-note">Ersetze &lt;SERVER_ID&gt; mit der Discord Server-ID und führe den Command als MBL aus.</div>
        </div>

      </div><!-- /.editor-body -->
    </div><!-- /.editor-card -->

  </div><!-- /.creator-grid -->
</div><!-- /.creator-wrap -->

<!-- JSON Modal -->
<div id="jsonModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.75);display:none;align-items:center;justify-content:center;padding:24px;">
  <div style="background:var(--bg-card);border:1px solid var(--border2);border-radius:var(--r-xl);padding:24px;max-width:700px;width:100%;max-height:80vh;overflow-y:auto;box-shadow:var(--shadow-xl);">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <span style="font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);">{ } JSON-Vorschau</span>
      <button onclick="closeJson()" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:1.1rem;line-height:1;">✕</button>
    </div>
    <textarea id="jsonPreview" readonly rows="20"
      style="width:100%;background:var(--bg-base);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.76rem;padding:12px;border-radius:var(--r-sm);resize:vertical;outline:none;"></textarea>
    <div style="margin-top:10px;display:flex;gap:8px;">
      <button class="btn btn-primary btn-sm" onclick="copyJson()">📋 Kopieren</button>
      <button class="btn btn-outline btn-sm" onclick="closeJson()">Schließen</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let _questions   = [];
let _viewerIds   = [];
let _currentFile = null;  // aktiv geladene Datei

// ── Helpers ────────────────────────────────────────────────────────────────
const q   = id => document.getElementById(id);
const esc = s  => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

function toast(msg, type='ok') {
  const icons = { ok:'✅', err:'❌', info:'ℹ️' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${icons[type]||''}</span><span>${esc(msg)}</span>`;
  q('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ── Files Sidebar ──────────────────────────────────────────────────────────
async function loadFiles() {
  const r = await fetch('/api/voting/files');
  const d = await r.json();
  const list = q('filesList');
  if (!d.files || !d.files.length) {
    list.innerHTML = '<div class="no-files">Noch keine Dateien gespeichert.<br>Klicke "+ Neu" um zu beginnen.</div>';
    return;
  }
  list.innerHTML = d.files.map(f => `
    <div class="file-item ${_currentFile === f.filename ? 'active' : ''}" id="fi-${esc(f.filename)}" onclick="loadFile('${esc(f.filename)}')">
      <div class="file-item-info">
        <div class="file-item-name" title="${esc(f.filename)}">${esc(f.title || f.filename)}</div>
        <div class="file-item-meta">${esc(f.filename)} · ${f.error ? '⚠️ Fehler' : (f.size/1024).toFixed(1)+'KB'}</div>
      </div>
      <button class="file-del-btn" onclick="deleteFile(event,'${esc(f.filename)}')" title="Löschen">🗑️</button>
    </div>
  `).join('');
}

async function loadFile(filename) {
  const r = await fetch(`/api/voting/files/${encodeURIComponent(filename)}`);
  const d = await r.json();
  if (!r.ok) { toast('Datei konnte nicht geladen werden: ' + (d.error||'?'), 'err'); return; }
  _currentFile = filename;
  populateForm(d.data);
  q('filenameInput').value = filename;
  hideCommandBox();
  loadFiles();
  toast(`${filename} geladen`, 'info');
}

async function deleteFile(e, filename) {
  e.stopPropagation();
  if (!confirm(`"${filename}" wirklich löschen?`)) return;
  const r = await fetch(`/api/voting/files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error||'?'), 'err'); return; }
  if (_currentFile === filename) { _currentFile = null; newVoting(); }
  toast(`${filename} gelöscht`, 'ok');
  loadFiles();
}

// ── Form ───────────────────────────────────────────────────────────────────
function populateForm(data) {
  q('fKategorie').value    = data.Kategorie    || '';
  q('fBeschreibung').value = data.Beschreibung || '';
  q('fZurAuswahl').value   = Array.isArray(data.Zur_Auswahl)
    ? data.Zur_Auswahl.join(',')
    : (data.Zur_Auswahl || '--All');

  const enc = !!data.RSA_Public_Key;
  q('fEncEnabled').checked = enc;
  q('encSection').style.display = enc ? '' : 'none';
  q('fPublicKey').value = data.RSA_Public_Key || '';

  // Viewer IDs
  _viewerIds = [];
  const raw = data.allowed_viewers || '';
  if (typeof raw === 'string' && raw.trim()) {
    _viewerIds = raw.split(',').map(x => x.trim()).filter(Boolean);
  } else if (Array.isArray(raw)) {
    _viewerIds = raw;
  }
  renderViewerChips();

  // Fragen
  _questions = (data.Fragen || []).map(f => ({
    text:     f.Frage        || '',
    type:     f.Typ          || 'text',
    required: f.Pflicht      !== false,
    multi:    !!f.Mehrfach,
    options:  Array.isArray(f.Optionen) ? [...f.Optionen] : (f.Optionen || ''),
    min:      f.Min          || 1,
    max:      f.Max          || 5,
  }));
  renderQuestions();
}

function buildData() {
  const fragen = _questions.map(q => {
    const obj = { Frage: q.text, Typ: q.type, Pflicht: q.required };
    if (q.type === 'choice') {
      obj.Mehrfach = q.multi;
      obj.Optionen = Array.isArray(q.options) ? q.options : [];
    }
    if (q.type === 'person') {
      obj.Mehrfach = q.multi;
      obj.Optionen = typeof q.options === 'string' ? q.options : '--All';
    }
    if (q.type === 'rating') { obj.Min = q.min; obj.Max = q.max; }
    return obj;
  });

  const zur = q('fZurAuswahl').value.trim();

  const data = {
    Kategorie:    q('fKategorie').value.trim(),
    Beschreibung: q('fBeschreibung').value.trim(),
    Zur_Auswahl:  zur,
    Fragen:       fragen,
  };

  if (q('fEncEnabled').checked && q('fPublicKey').value.trim()) {
    data.RSA_Public_Key = q('fPublicKey').value.trim();
  }

  if (_viewerIds.length > 0) {
    data.allowed_viewers = _viewerIds.join(',');
  }

  return data;
}

function newVoting() {
  _currentFile = null;
  _questions   = [];
  _viewerIds   = [];
  q('fKategorie').value    = '';
  q('fBeschreibung').value = '';
  q('fZurAuswahl').value   = '--All';
  q('fEncEnabled').checked = false;
  q('encSection').style.display = 'none';
  q('fPublicKey').value = '';
  q('filenameInput').value = '';
  renderViewerChips();
  renderQuestions();
  hideCommandBox();
  hideValidation();
  loadFiles();
}

// ── Save ───────────────────────────────────────────────────────────────────
async function saveFile() {
  const data     = buildData();
  let   filename = q('filenameInput').value.trim() || data.Kategorie || 'abstimmung';
  if (!filename.endsWith('.json')) filename += '.json';

  hideValidation();

  // Erst validieren
  const vr = await fetch('/api/voting/validate', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
  });
  const vd = await vr.json();
  if (!vd.valid) { showValidation(vd.errors); return; }

  const r = await fetch('/api/voting/files', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ filename, data }),
  });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error||'?'), 'err'); return; }

  _currentFile = d.filename;
  q('filenameInput').value = d.filename;
  toast(`✓ ${d.filename} gespeichert`, 'ok');
  showCommandBox(d.path);
  loadFiles();
}

function showCommandBox(path) {
  const cmd = `/abstimmung erstellen json_pfad:${path} server_id:<SERVER_ID>`;
  q('commandText').textContent = cmd;
  q('commandBox').classList.add('show');
}
function hideCommandBox() { q('commandBox').classList.remove('show'); }

function copyCommand() {
  const txt = q('commandText').textContent;
  navigator.clipboard.writeText(txt).then(() => toast('Command kopiert!', 'ok'));
}

// ── Validate ───────────────────────────────────────────────────────────────
async function validateCurrent() {
  const data = buildData();
  const r = await fetch('/api/voting/validate', {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data)
  });
  const d = await r.json();
  if (d.valid) {
    hideValidation();
    toast('✓ Konfiguration ist gültig!', 'ok');
  } else {
    showValidation(d.errors);
    toast(`${d.errors.length} Fehler gefunden`, 'err');
  }
}

function showValidation(errors) {
  q('valErrorList').innerHTML = errors.map(e => `<li>${esc(e)}</li>`).join('');
  q('valError').classList.add('show');
}
function hideValidation() { q('valError').classList.remove('show'); }

// ── Filename ───────────────────────────────────────────────────────────────
function onFilenameInput(val) {
  // just track
}

// ── Encryption ─────────────────────────────────────────────────────────────
function toggleEnc(on) {
  q('encSection').style.display = on ? '' : 'none';
}

// ── Viewers ────────────────────────────────────────────────────────────────
function addViewer() {
  const inp = q('viewerIdInput');
  const val = inp.value.trim();
  if (!val || !/^\d{15,20}$/.test(val)) { toast('Ungültige Discord User-ID (15-20 Ziffern)', 'err'); return; }
  if (_viewerIds.includes(val)) { toast('Bereits hinzugefügt', 'info'); return; }
  _viewerIds.push(val);
  inp.value = '';
  renderViewerChips();
}

function removeViewer(uid) {
  _viewerIds = _viewerIds.filter(x => x !== uid);
  renderViewerChips();
}

function renderViewerChips() {
  q('viewerChips').innerHTML = _viewerIds.map(uid =>
    `<span class="viewer-chip">${esc(uid)}<button class="viewer-chip-del" onclick="removeViewer('${uid}')">✕</button></span>`
  ).join('') || '<span style="font-size:.72rem;color:var(--text3);">Keine – nur MBL kann den Voter-Log sehen</span>';
}

// ── Questions ──────────────────────────────────────────────────────────────
function addQuestion(type) {
  const defaults = {
    text:   { text:'', type:'text', required:true, multi:false, options:[], min:1, max:5 },
    choice: { text:'', type:'choice', required:true, multi:false, options:['Option 1','Option 2'], min:1, max:5 },
    rating: { text:'', type:'rating', required:true, multi:false, options:[], min:1, max:5 },
    person: { text:'', type:'person', required:false, multi:false, options:'--All', min:1, max:5 },
  };
  _questions.push({...defaults[type]});
  renderQuestions();
}

function removeQuestion(idx) {
  _questions.splice(idx, 1);
  renderQuestions();
}

function moveQuestion(idx, dir) {
  const to = idx + dir;
  if (to < 0 || to >= _questions.length) return;
  [_questions[idx], _questions[to]] = [_questions[to], _questions[idx]];
  renderQuestions();
}

function renderQuestions() {
  const container = q('questionsList');
  if (!_questions.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text3);padding:20px;font-size:.83rem;">Noch keine Fragen. Klicke unten auf einen Fragetyp.</div>';
    return;
  }
  container.innerHTML = _questions.map((qst, i) => renderQuestion(qst, i)).join('');
}

const TYPE_LABELS = { text:'Freitext', choice:'Auswahl', rating:'Bewertung', person:'Person' };
const TYPE_ICONS  = { text:'📝', choice:'☑️', rating:'⭐', person:'👤' };

function renderQuestion(qst, i) {
  const typeOpts = ['text','choice','rating','person'].map(t =>
    `<option value="${t}" ${qst.type===t?'selected':''}>${TYPE_ICONS[t]} ${TYPE_LABELS[t]}</option>`
  ).join('');

  let extra = '';
  if (qst.type === 'choice') {
    const opts = Array.isArray(qst.options) ? qst.options : [];
    const optRows = opts.map((o, oi) => `
      <div class="option-row">
        <input type="text" value="${esc(o)}" placeholder="Option ${oi+1}"
          oninput="_questions[${i}].options[${oi}]=this.value">
        <button class="option-del" onclick="removeOption(${i},${oi})">✕</button>
      </div>`).join('');
    extra = `
      <div class="options-editor">
        ${optRows}
        <button class="btn btn-ghost btn-sm" style="margin-top:4px;" onclick="addOption(${i})">+ Option</button>
      </div>
      <div style="margin-top:10px;display:flex;align-items:center;gap:8px;">
        <label class="toggle-switch">
          <input type="checkbox" ${qst.multi?'checked':''} onchange="_questions[${i}].multi=this.checked">
          <span class="toggle-slider"></span>
        </label>
        <span style="font-size:.83rem;color:var(--sub);">Mehrfachauswahl erlauben</span>
      </div>`;
  } else if (qst.type === 'rating') {
    extra = `
      <div style="display:flex;align-items:center;gap:14px;margin-top:8px;">
        <div>
          <div style="font-size:.65rem;color:var(--text3);margin-bottom:4px;">MIN</div>
          <input type="number" value="${qst.min}" min="0" max="10" style="width:70px;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.85rem;padding:6px 8px;border-radius:var(--r-sm);outline:none;"
            oninput="_questions[${i}].min=+this.value">
        </div>
        <div>
          <div style="font-size:.65rem;color:var(--text3);margin-bottom:4px;">MAX</div>
          <input type="number" value="${qst.max}" min="1" max="100" style="width:70px;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.85rem;padding:6px 8px;border-radius:var(--r-sm);outline:none;"
            oninput="_questions[${i}].max=+this.value">
        </div>
      </div>`;
  } else if (qst.type === 'person') {
    const isAll = (typeof qst.options === 'string' && qst.options.includes('All')) || qst.options === '--All';
    extra = `
      <div style="margin-top:8px;">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
          <label class="toggle-switch">
            <input type="checkbox" ${isAll?'checked':''} onchange="togglePersonAll(${i},this.checked)">
            <span class="toggle-slider"></span>
          </label>
          <span style="font-size:.83rem;color:var(--sub);">Alle Server-Mitglieder</span>
        </div>
        <div id="personOptsSection_${i}" style="${isAll?'display:none':''}">
          <div style="font-size:.68rem;color:var(--text3);margin-bottom:6px;">User-IDs (komma-getrennt)</div>
          <input type="text" placeholder="123456789,987654321,..." style="width:100%;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.75rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;"
            value="${isAll ? '' : esc(Array.isArray(qst.options) ? qst.options.join(',') : qst.options)}"
            oninput="_questions[${i}].options=this.value.split(',').map(x=>x.trim()).filter(Boolean)">
        </div>
        <div style="margin-top:8px;display:flex;align-items:center;gap:8px;">
          <label class="toggle-switch">
            <input type="checkbox" ${qst.multi?'checked':''} onchange="_questions[${i}].multi=this.checked">
            <span class="toggle-slider"></span>
          </label>
          <span style="font-size:.83rem;color:var(--sub);">Mehrfachauswahl erlauben</span>
        </div>
      </div>`;
  }

  return `<div class="question-card">
    <div class="question-card-hd">
      <div class="q-num">${i+1}</div>
      <select class="q-type-badge" style="background:var(--bg-card);border:1px solid var(--border2);color:var(--text3);padding:3px 8px;border-radius:20px;font-size:.64rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;outline:none;cursor:pointer;"
        onchange="changeType(${i},this.value)">${typeOpts}</select>
      <div style="display:flex;align-items:center;gap:4px;margin-left:auto;">
        <button class="q-move" onclick="moveQuestion(${i},-1)" title="Nach oben" ${i===0?'disabled':''}>↑</button>
        <button class="q-move" onclick="moveQuestion(${i},1)" title="Nach unten" ${i===_questions.length-1?'disabled':''}>↓</button>
        <button class="q-del" onclick="removeQuestion(${i})">✕</button>
      </div>
    </div>
    <div class="fe" style="margin-bottom:8px;">
      <label>Frage ${i+1}</label>
      <input type="text" value="${esc(qst.text)}" placeholder="Deine Frage..." oninput="_questions[${i}].text=this.value">
    </div>
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
      <label class="toggle-switch">
        <input type="checkbox" ${qst.required?'checked':''} onchange="_questions[${i}].required=this.checked">
        <span class="toggle-slider"></span>
      </label>
      <span style="font-size:.8rem;color:var(--sub);">Pflichtfrage</span>
    </div>
    ${extra}
  </div>`;
}

function changeType(idx, type) {
  const defaults = { text:{options:[],multi:false,min:1,max:5}, choice:{options:['Option 1','Option 2'],multi:false,min:1,max:5}, rating:{options:[],multi:false,min:1,max:5}, person:{options:'--All',multi:false,min:1,max:5} };
  const old = _questions[idx];
  _questions[idx] = { text: old.text, type, required: old.required, ...defaults[type] };
  renderQuestions();
}

function addOption(qidx) {
  if (!Array.isArray(_questions[qidx].options)) _questions[qidx].options = [];
  _questions[qidx].options.push(`Option ${_questions[qidx].options.length + 1}`);
  renderQuestions();
}

function removeOption(qidx, oidx) {
  _questions[qidx].options.splice(oidx, 1);
  renderQuestions();
}

function togglePersonAll(qidx, on) {
  _questions[qidx].options = on ? '--All' : [];
  const sec = document.getElementById(`personOptsSection_${qidx}`);
  if (sec) sec.style.display = on ? 'none' : '';
}

// ── JSON Preview ───────────────────────────────────────────────────────────
function showJson() {
  const data = buildData();
  q('jsonPreview').value = JSON.stringify(data, null, 2);
  q('jsonModal').style.display = 'flex';
}
function closeJson() { q('jsonModal').style.display = 'none'; }
function copyJson() {
  navigator.clipboard.writeText(q('jsonPreview').value).then(() => toast('JSON kopiert!', 'ok'));
}

// ── Init ───────────────────────────────────────────────────────────────────
loadFiles();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# EXISTING TEMPLATES (unchanged from original)
# ══════════════════════════════════════════════════════════════════════════════

def _render_error(title, msg, code=400, show_results_link=None, show_login_link=None):
    extra = ""
    if show_results_link:
        extra += f'<a href="{show_results_link}" style="color:var(--accent);margin-top:16px;display:inline-block;">→ Ergebnisse ansehen</a>'
    if show_login_link:
        extra += f'<a href="{show_login_link}" style="color:var(--accent);margin-top:16px;display:inline-block;">→ Erneut anmelden</a>'
    html = f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<title>{title}</title>
<style>
  :root {{ --bg:#0a0b0d; --card:#1d2128; --text:#e8edf5; --sub:#8b95a8; --border:#252b35; --accent:#4ade80; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',sans-serif; display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:16px; padding:48px 40px; text-align:center; max-width:400px; }}
  h1 {{ font-size:1.4rem; margin-bottom:12px; }}
  p {{ color:var(--sub); font-size:.9rem; line-height:1.6; }}
</style></head><body>
<div class="card">
  <div style="font-size:2.5rem;margin-bottom:16px;">{'⚠️' if code == 403 else '❌'}</div>
  <h1>{title}</h1><p>{msg}</p>{extra}
</div></body></html>"""
    return Response(html, status=code, mimetype="text/html")


def _render_already_voted(voting: dict, voting_id: str, user_name: str) -> Response:
    html = f"""<!DOCTYPE html><html lang="de"><head><meta charset="UTF-8">
<title>Bereits abgestimmt</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;600;700&display=swap">
<style>
  :root{{--bg:#060708;--card:#161b24;--border:#1f2535;--text:#e2e8f5;--sub:#7a8499;--green:#4ade80;}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
    display:flex;align-items:center;justify-content:center;padding:24px;}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:16px;
    padding:48px 40px;text-align:center;max-width:420px;width:100%;}}
  .icon{{font-size:3rem;margin-bottom:20px;}}
  h1{{font-size:1.5rem;font-weight:700;margin-bottom:10px;}}
  p{{color:var(--sub);font-size:.9rem;line-height:1.6;margin-bottom:20px;}}
  .name{{color:var(--green);font-weight:600;}}
  a{{display:inline-block;color:var(--sub);font-size:.82rem;text-decoration:none;
    border:1px solid var(--border);border-radius:8px;padding:8px 18px;transition:border-color .2s;margin:4px;}}
  a:hover{{border-color:var(--green);color:var(--green);}}
</style></head><body>
<div class="card">
  <div class="icon">✅</div>
  <h1>Bereits abgestimmt</h1>
  <p>Du hast als <span class="name">{user_name}</span> bereits an dieser Abstimmung teilgenommen.<br><br>
  Jede Person kann nur einmal abstimmen. Deine Antwort wurde anonym gespeichert.</p>
  <a href="/vote/{voting_id}/results">→ Ergebnisse ansehen</a>
</div></body></html>"""
    return Response(html, status=200, mimetype="text/html")


_LOGIN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ voting.title }} – Anmelden</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&display=swap">
<style>
:root{--bg:#060708;--card:#161b24;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--accent:#5b8cff;--green:#4ade80;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;
  display:flex;align-items:center;justify-content:center;padding:24px;
  background-image:radial-gradient(ellipse 80% 60% at 10% -10%,rgba(91,140,255,0.07) 0%,transparent 60%);}
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;
  padding:52px 44px;text-align:center;max-width:440px;width:100%;
  box-shadow:0 24px 80px rgba(0,0,0,.7);}
.badge{display:inline-flex;align-items:center;gap:6px;
  background:rgba(91,140,255,.1);border:1px solid rgba(91,140,255,.25);
  border-radius:20px;padding:4px 14px;font-size:.72rem;font-weight:600;
  letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin-bottom:22px;}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
h1{font-size:1.6rem;font-weight:700;margin-bottom:8px;}
.sub{color:var(--sub);font-size:.9rem;line-height:1.6;margin-bottom:32px;}
.discord-btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;
  width:100%;padding:14px 20px;background:#5865F2;color:#fff;
  font-family:'DM Sans',sans-serif;font-size:.95rem;font-weight:600;
  border-radius:10px;border:none;cursor:pointer;text-decoration:none;
  transition:all .2s;box-shadow:0 4px 18px rgba(88,101,242,.32);}
.discord-btn:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(88,101,242,.48);}
.note{margin-top:20px;font-size:.75rem;color:var(--sub);line-height:1.6;
  background:rgba(255,255,255,.03);border:1px solid var(--border2);
  border-radius:8px;padding:12px 16px;}
</style>
</head>
<body>
<div class="card">
  <div class="badge"><span class="dot"></span>Abstimmung</div>
  <h1>{{ voting.title }}</h1>
  <p class="sub">{{ voting.description or 'Melde dich mit Discord an um an dieser Abstimmung teilzunehmen.' }}</p>
  <a href="{{ oauth_url }}" class="discord-btn">
    <svg width="20" height="15" viewBox="0 0 71 55" fill="#fff">
      <path d="M60.1 4.9A58.6 58.6 0 0 0 45.6 0a40 40 0 0 0-1.9 3.8 54.2 54.2 0 0 0-16.2 0A40 40 0 0 0 25.7 0 58.3 58.3 0 0 0 11 4.9C1.6 18.3-.9 31.3.3 44.1a58.9 58.9 0 0 0 18 9.1 44 44 0 0 0 3.8-6.2 38.4 38.4 0 0 1-6-2.9l1.5-1.1a42.2 42.2 0 0 0 36 0l1.5 1.1a38.4 38.4 0 0 1-6 2.9 44 44 0 0 0 3.8 6.2 58.7 58.7 0 0 0 18-9.1c1.5-15.3-2.6-28.2-10.8-39.1ZM23.7 36.1c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Z"/>
    </svg>
    Mit Discord anmelden
  </a>
  <div class="note">🔒 Deine Identität bleibt anonym. Es wird nur ein <strong>Hash</strong> deiner Discord-ID gespeichert.</div>
</div>
</body>
</html>"""


_VOTE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ voting.title }} – Abstimmung</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap">
<style>
:root {
  --bg: #060708; --surface: #0f1117; --card: #161b24; --card2: #1c2130;
  --border: #1f2535; --border2: #2a3245; --text: #e2e8f5; --sub: #7a8499;
  --dim: #3d4660; --accent: #5b8cff; --accent2: #3d6fff; --green: #4ade80;
  --red: #f87171; --gold: #fbbf24; --r: 12px; --r2: 8px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: 'DM Sans', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; line-height: 1.6; -webkit-font-smoothing: antialiased; }
.bg-layer { position: fixed; inset: 0; z-index: 0; pointer-events: none; background: radial-gradient(ellipse 80% 60% at 10% -10%, rgba(91,140,255,0.07) 0%, transparent 60%), radial-gradient(ellipse 60% 40% at 90% 100%, rgba(74,222,128,0.04) 0%, transparent 60%); }
.page { position: relative; z-index: 1; max-width: 720px; margin: 0 auto; padding: 48px 22px 80px; }
.user-bar { display: flex; align-items: center; justify-content: space-between; background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 10px 16px; margin-bottom: 32px; font-size: .83rem; }
.user-info { display: flex; align-items: center; gap: 8px; color: var(--sub); }
.user-name { color: var(--text); font-weight: 600; }
.user-badge { background: rgba(74,222,128,.1); color: var(--green); border: 1px solid rgba(74,222,128,.22); border-radius: 20px; padding: 2px 10px; font-size: .68rem; font-weight: 700; }
.logout-link { color: var(--sub); font-size: .78rem; text-decoration: none; border: 1px solid var(--border2); border-radius: 6px; padding: 4px 10px; transition: all .15s; }
.logout-link:hover { border-color: var(--red); color: var(--red); }
.vote-header { text-align: center; margin-bottom: 48px; }
.vote-badge { display: inline-flex; align-items: center; gap: 6px; background: rgba(91,140,255,.1); border: 1px solid rgba(91,140,255,.25); border-radius: 20px; padding: 4px 14px; font-size: .72rem; font-weight: 600; letter-spacing: .08em; text-transform: uppercase; color: var(--accent); margin-bottom: 20px; }
.vote-badge::before { content: ''; width:6px; height:6px; border-radius:50%; background:var(--green); display:block; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(1.3)} }
.vote-title { font-size: clamp(1.6rem, 4vw, 2.4rem); font-weight: 700; letter-spacing: -.5px; color: var(--text); margin-bottom: 10px; }
.vote-desc { color: var(--sub); font-size: .92rem; max-width: 480px; margin: 0 auto; }
.progress-bar { background: var(--border); border-radius: 4px; height: 3px; margin-bottom: 40px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent); border-radius: 4px; transition: width .4s ease; width: 0; }
.progress-text { text-align: right; font-size: .72rem; color: var(--sub); margin-top: 6px; font-family: 'DM Mono', monospace; }
.question-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 28px 28px; margin-bottom: 16px; transition: border-color .2s; }
.question-card.active { border-color: var(--accent); }
.question-card.answered { border-color: rgba(74,222,128,.3); }
.q-number { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--dim); margin-bottom: 6px; font-family: 'DM Mono', monospace; }
.q-text { font-size: 1rem; font-weight: 500; color: var(--text); margin-bottom: 20px; line-height: 1.5; }
.q-required { color: var(--red); margin-left: 3px; }
.opt-grid { display: flex; flex-direction: column; gap: 8px; }
.opt-item { display: flex; align-items: center; gap: 12px; background: var(--card2); border: 1px solid var(--border2); border-radius: var(--r2); padding: 12px 16px; cursor: pointer; transition: all .15s; user-select: none; }
.opt-item:hover { border-color: var(--accent); background: rgba(91,140,255,.07); }
.opt-item.selected { border-color: var(--accent); background: rgba(91,140,255,.12); }
.opt-item.selected .opt-indicator { background: var(--accent); border-color: var(--accent); }
.opt-item.selected .opt-indicator::after { display: block; }
.opt-indicator { width: 18px; height: 18px; border-radius: 50%; flex-shrink: 0; border: 2px solid var(--border2); background: transparent; position: relative; transition: all .15s; }
.opt-indicator::after { content: ''; display: none; width: 8px; height: 8px; border-radius: 50%; background: var(--bg); position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%); }
.opt-indicator.checkbox { border-radius: 4px; }
.opt-indicator.checkbox::after { content: '✓'; width: auto; height: auto; font-size: 10px; background: transparent; color: var(--bg); font-weight: 700; }
.opt-label { font-size: .9rem; color: var(--text); flex: 1; }
.person-search { width: 100%; background: var(--card2); border: 1px solid var(--border2); color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .88rem; padding: 10px 14px; border-radius: var(--r2); outline: none; margin-bottom: 10px; transition: border-color .15s; }
.person-search:focus { border-color: var(--accent); }
.person-list { max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.person-item { display: flex; align-items: center; gap: 10px; background: var(--card2); border: 1px solid var(--border2); border-radius: var(--r2); padding: 9px 14px; cursor: pointer; transition: all .15s; }
.person-item:hover { border-color: var(--accent); }
.person-item.selected { border-color: var(--accent); background: rgba(91,140,255,.1); }
.person-avatar { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; background: var(--dim); flex-shrink: 0; font-size: .65rem; display: flex; align-items: center; justify-content: center; color: var(--text); font-weight: 600; }
.person-name { font-size: .88rem; flex: 1; }
.rating-row { display: flex; gap: 8px; flex-wrap: wrap; }
.rating-btn { width: 44px; height: 44px; border-radius: var(--r2); background: var(--card2); border: 1px solid var(--border2); color: var(--sub); font-size: .9rem; font-weight: 600; cursor: pointer; transition: all .15s; display: flex; align-items: center; justify-content: center; }
.rating-btn:hover { border-color: var(--accent); color: var(--accent); }
.rating-btn.selected { background: var(--accent); border-color: var(--accent); color: var(--bg); }
.text-input { width: 100%; background: var(--card2); border: 1px solid var(--border2); color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .9rem; padding: 12px 14px; border-radius: var(--r2); outline: none; resize: vertical; min-height: 100px; transition: border-color .15s; }
.text-input:focus { border-color: var(--accent); }
.submit-section { margin-top: 36px; text-align: center; }
.submit-btn { background: var(--accent); color: #000; font-family: 'DM Sans', sans-serif; font-weight: 700; font-size: .95rem; padding: 14px 40px; border: none; border-radius: var(--r2); cursor: pointer; transition: all .2s; letter-spacing: .02em; }
.submit-btn:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); box-shadow: 0 6px 24px rgba(91,140,255,.3); }
.submit-btn:disabled { opacity: .4; cursor: not-allowed; }
.anon-note { margin-top: 16px; font-size: .75rem; color: var(--dim); display: flex; align-items: center; justify-content: center; gap: 6px; }
.success-overlay { display: none; position: fixed; inset: 0; z-index: 999; background: var(--bg); align-items: center; justify-content: center; flex-direction: column; text-align: center; gap: 20px; padding: 24px; }
.success-overlay.show { display: flex; }
.success-icon { font-size: 4rem; animation: pop .4s ease; }
@keyframes pop { 0%{transform:scale(.5);opacity:0} 80%{transform:scale(1.1)} 100%{transform:scale(1);opacity:1} }
.success-title { font-size: 1.6rem; font-weight: 700; }
.success-sub { color: var(--sub); font-size: .9rem; }
.enc-badge { display: inline-flex; align-items: center; gap: 5px; background: rgba(251,191,36,.08); border: 1px solid rgba(251,191,36,.2); border-radius: 6px; padding: 4px 10px; font-size: .70rem; font-weight: 600; color: var(--gold); margin-bottom: 24px; }
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border2); border-radius: 4px; }
</style>
</head>
<body>
<div class="bg-layer"></div>
<div class="success-overlay" id="successOverlay">
  <div class="success-icon">✅</div>
  <div class="success-title">Danke für deine Stimme!</div>
  <div class="success-sub">Deine Antworten wurden anonym gespeichert.<br>Du kannst dieses Fenster jetzt schließen.</div>
</div>
<div class="page">
  <div class="user-bar">
    <div class="user-info">
      <span>Angemeldet als</span>
      <span class="user-name">{{ user_name }}</span>
      <span class="user-badge">✓ Verifiziert</span>
    </div>
    <a href="/vote/{{ voting_id }}/logout" class="logout-link">Abmelden</a>
  </div>
  <div class="vote-header">
    <div class="vote-badge">Abstimmung läuft</div>
    <h1 class="vote-title">{{ voting.title }}</h1>
    {% if voting.description %}<p class="vote-desc">{{ voting.description }}</p>{% endif %}
    {% if voting.public_key %}<div style="margin-top:16px;"><span class="enc-badge">🔐 Ende-zu-Ende verschlüsselt</span></div>{% endif %}
  </div>
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width: 0%"></div></div>
  <div class="progress-text" id="progressText">0 / {{ voting.questions | length }} beantwortet</div>
  <form id="voteForm">
  {% for i, frage in enumerate(voting.questions) %}
  <div class="question-card" id="qcard-{{ i }}" data-index="{{ i }}">
    <div class="q-number">Frage {{ i + 1 }} von {{ voting.questions | length }}</div>
    <div class="q-text">{{ frage.Frage }}{% if frage.get('Pflicht', False) %}<span class="q-required">*</span>{% endif %}</div>
    {% if frage.Typ == 'choice' %}
    <div class="opt-grid" id="opts-{{ i }}">
      {% for opt in frage.get('Optionen', []) %}
      <div class="opt-item" data-q="{{ i }}" data-val="{{ opt }}" data-multi="{{ 'true' if frage.get('Mehrfach', False) else 'false' }}" onclick="toggleOpt(this)">
        <div class="opt-indicator {{ 'checkbox' if frage.get('Mehrfach', False) else '' }}"></div>
        <span class="opt-label">{{ opt }}</span>
      </div>
      {% endfor %}
    </div>
    {% elif frage.Typ == 'rating' %}
    <div class="rating-row" id="rating-{{ i }}">
      {% for v in range(frage.get('Min', 1), frage.get('Max', 5) + 1) %}
      <button type="button" class="rating-btn" data-q="{{ i }}" data-val="{{ v }}" onclick="selectRating(this)">{{ v }}</button>
      {% endfor %}
    </div>
    {% elif frage.Typ == 'text' %}
    <textarea class="text-input" id="text-{{ i }}" data-q="{{ i }}" placeholder="Deine Antwort..." rows="4" oninput="updateProgress()"></textarea>
    {% elif frage.Typ == 'person' %}
    <input type="text" class="person-search" id="psearch-{{ i }}" placeholder="Name suchen..." oninput="filterPersons({{ i }}, this.value)">
    <div class="person-list" id="plist-{{ i }}"><div style="color:var(--sub);font-size:.82rem;padding:8px;">Lädt Mitglieder...</div></div>
    {% endif %}
  </div>
  {% endfor %}
  </form>
  <div class="submit-section">
    <button class="submit-btn" id="submitBtn" onclick="submitVote()">Abstimmung abschicken →</button>
    <div class="anon-note">🔒 Deine Antwort ist vollständig anonym — keine Rückverfolgung möglich</div>
  </div>
</div>
<script>
const VOTING_ID  = {{ voting_id | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
const PUBLIC_KEY = {{ (voting.public_key or '') | tojson }};
const HAS_PERSON = QUESTIONS.some(q => q.Typ === 'person');
const answers = {};

function toggleOpt(el) {
  const q = el.dataset.q, val = el.dataset.val, multi = el.dataset.multi === 'true';
  if (!multi) { document.querySelectorAll(`.opt-item[data-q="${q}"]`).forEach(e => e.classList.remove('selected')); answers[q] = val; }
  else { el.classList.toggle('selected'); const sel = Array.from(document.querySelectorAll(`.opt-item[data-q="${q}"].selected`)).map(e => e.dataset.val); answers[q] = sel.length > 0 ? sel : undefined; }
  el.classList.toggle('selected', !multi || (Array.isArray(answers[q]) && answers[q].includes(val)));
  updateProgress();
}
function selectRating(btn) { const q = btn.dataset.q; document.querySelectorAll(`.rating-btn[data-q="${q}"]`).forEach(b => b.classList.remove('selected')); btn.classList.add('selected'); answers[q] = parseInt(btn.dataset.val); updateProgress(); }
function selectPerson(idx, userId, name) {
  const multi = QUESTIONS[idx]?.Mehrfach || false;
  const item = document.querySelector(`.person-item[data-uid="${userId}"][data-q="${idx}"]`);
  if (!multi) { document.querySelectorAll(`.person-item[data-q="${idx}"]`).forEach(e => e.classList.remove('selected')); answers[idx] = {id: userId, name: name}; }
  else { item?.classList.toggle('selected'); const sel = Array.from(document.querySelectorAll(`.person-item[data-q="${idx}"].selected`)).map(e => ({id: e.dataset.uid, name: e.dataset.name})); answers[idx] = sel.length > 0 ? sel : undefined; }
  if (item && !multi) item.classList.add('selected');
  updateProgress();
}
function filterPersons(idx, query) { const items = document.querySelectorAll(`.person-item[data-q="${idx}"]`); const q = query.toLowerCase(); items.forEach(item => { item.style.display = (!q || item.dataset.name.toLowerCase().includes(q)) ? '' : 'none'; }); }
function updateProgress() {
  let answered = 0;
  QUESTIONS.forEach((frage, i) => {
    if (frage.Typ === 'text') { const el = document.getElementById(`text-${i}`); if (el && el.value.trim()) { answers[i] = el.value.trim(); answered++; } else if (answers[i]) answered++; }
    else if (answers[i] !== undefined && answers[i] !== null) answered++;
    const card = document.getElementById(`qcard-${i}`);
    if (card) card.classList.toggle('answered', answers[i] !== undefined && answers[i] !== null);
  });
  const pct = QUESTIONS.length ? (answered / QUESTIONS.length * 100) : 0;
  const fill = document.getElementById('progressFill'), txt = document.getElementById('progressText');
  if (fill) fill.style.width = pct + '%';
  if (txt) txt.textContent = `${answered} / ${QUESTIONS.length} beantwortet`;
}
async function encryptData(data, pemPublicKey) {
  const encoder = new TextEncoder(), dataBytes = encoder.encode(data);
  const aesKey = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const aesCipherBuf = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aesKey, dataBytes);
  const aesCipher = new Uint8Array(aesCipherBuf), aesTag = aesCipher.slice(-16), ciphertext = aesCipher.slice(0, -16);
  const exportedAesKey = new Uint8Array(await crypto.subtle.exportKey('raw', aesKey));
  const pemBody = pemPublicKey.replace(/-----BEGIN PUBLIC KEY-----/, '').replace(/-----END PUBLIC KEY-----/, '').replace(/\s+/g, '');
  const derBuffer = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  const rsaKey = await crypto.subtle.importKey('spki', derBuffer.buffer, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  const encAesKey = new Uint8Array(await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, rsaKey, exportedAesKey));
  const b64 = buf => btoa(String.fromCharCode(...buf));
  return [b64(encAesKey), b64(aesTag), b64(iv), b64(ciphertext)].join('|');
}
async function submitVote() {
  const missing = [];
  QUESTIONS.forEach((frage, i) => { if (!frage.Pflicht) return; if (frage.Typ === 'text') { const el = document.getElementById(`text-${i}`); if (!el || !el.value.trim()) missing.push(i + 1); } else if (answers[i] === undefined || answers[i] === null) missing.push(i + 1); });
  if (missing.length) { alert(`Bitte beantworte Pflichtfragen: ${missing.join(', ')}`); return; }
  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = 'Wird abgeschickt...';
  QUESTIONS.forEach((frage, i) => { if (frage.Typ === 'text') { const el = document.getElementById(`text-${i}`); if (el && el.value.trim()) answers[i] = el.value.trim(); } });
  const payload = {}, answerObj = {};
  QUESTIONS.forEach((frage, i) => { answerObj[frage.Frage] = answers[i] ?? null; });
  if (PUBLIC_KEY) {
    try { payload.encrypted_data = await encryptData(JSON.stringify(answerObj), PUBLIC_KEY); }
    catch(e) { alert('Verschlüsselung fehlgeschlagen: ' + e.message); btn.disabled = false; btn.textContent = 'Abstimmung abschicken →'; return; }
  } else { payload.answers = answerObj; }
  try {
    const r = await fetch(`/vote/${VOTING_ID}/submit`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
    const d = await r.json();
    if (!r.ok) { if (r.status === 401) { window.location.href = `/vote/${VOTING_ID}/login`; return; } alert(d.error || 'Fehler beim Abschicken'); btn.disabled = false; btn.textContent = 'Abstimmung abschicken →'; return; }
    document.getElementById('successOverlay').classList.add('show');
  } catch(e) { alert('Netzwerkfehler: ' + e.message); btn.disabled = false; btn.textContent = 'Abstimmung abschicken →'; }
}
async function loadPersonQuestions() {
  const personQs = QUESTIONS.map((q, i) => ({q, i})).filter(({q}) => q.Typ === 'person');
  if (!personQs.length) return;
  let members = [];
  try { const r = await fetch(`/api/vote/${VOTING_ID}/members`); if (r.status === 401) return; const d = await r.json(); members = d.members || []; } catch(e) { console.error(e); }
  personQs.forEach(({q, i}) => {
    const list = document.getElementById(`plist-${i}`);
    if (!list) return;
    let displayMembers = members;
    if (q.Optionen && q.Optionen !== '--All' && Array.isArray(q.Optionen)) { const allowed = new Set(q.Optionen.map(String)); displayMembers = members.filter(m => allowed.has(m.id)); }
    if (!displayMembers.length) { list.innerHTML = '<div style="color:var(--sub);font-size:.82rem;padding:8px;">Keine Mitglieder gefunden</div>'; return; }
    list.innerHTML = displayMembers.map(m => { const initials = (m.display || '?').slice(0, 2).toUpperCase(); const avatarHtml = m.avatar ? `<img class="person-avatar" src="${m.avatar}" alt="">` : `<div class="person-avatar">${initials}</div>`; return `<div class="person-item" data-q="${i}" data-uid="${m.id}" data-name="${m.display}" onclick="selectPerson(${i}, '${m.id}', '${m.display.replace(/'/g,"\\'")}')"> ${avatarHtml} <span class="person-name">${m.display}</span></div>`; }).join('');
  });
}
if (HAS_PERSON) loadPersonQuestions();
updateProgress();
</script>
</body>
</html>"""


_RESULTS_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{{ voting.title }} – Ergebnisse</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono&display=swap">
<style>
:root{--bg:#060708;--surface:#0f1117;--card:#161b24;--card2:#1c2130;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--dim:#3d4660;--accent:#5b8cff;--green:#4ade80;--red:#f87171;--gold:#fbbf24;--r:12px;--r2:8px;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:48px 22px;-webkit-font-smoothing:antialiased;}
.page{max-width:760px;margin:0 auto;}
h1{font-size:1.8rem;font-weight:700;margin-bottom:8px;}
.sub{color:var(--sub);font-size:.9rem;margin-bottom:36px;}
.stat-row{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:36px;}
.stat-box{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:18px 20px;flex:1;min-width:120px;}
.stat-num{font-size:1.6rem;font-weight:700;color:var(--accent);font-family:'DM Mono',monospace;}
.stat-lbl{font-size:.72rem;text-transform:uppercase;letter-spacing:.07em;color:var(--dim);margin-top:4px;}
.q-block{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:24px;margin-bottom:16px;}
.q-label{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:6px;}
.q-title{font-size:1rem;font-weight:500;margin-bottom:18px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:160px;font-size:.85rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-track{flex:1;background:var(--border2);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{height:100%;background:var(--accent);border-radius:4px;transition:width .5s ease;}
.bar-count{font-family:'DM Mono',monospace;font-size:.78rem;color:var(--sub);width:40px;text-align:right;flex-shrink:0;}
.enc-notice{background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.2);border-radius:var(--r2);padding:12px 16px;font-size:.82rem;color:var(--gold);margin-bottom:24px;}
.dec-link{display:inline-flex;align-items:center;gap:6px;color:var(--accent);font-size:.85rem;text-decoration:none;margin-top:8px;}
.dec-link:hover{text-decoration:underline;}
.voters-link{display:inline-flex;align-items:center;gap:6px;color:var(--green);font-size:.85rem;text-decoration:none;background:rgba(74,222,128,.08);border:1px solid rgba(74,222,128,.2);border-radius:8px;padding:8px 16px;}
.voters-link:hover{background:rgba(74,222,128,.15);}
</style>
</head>
<body>
<div class="page">
  <h1>📊 {{ voting.title }}</h1>
  <div class="sub">{{ voting.description }}</div>

  {% if is_mbl and voting.is_active %}
  <div style="display:inline-flex;align-items:center;gap:6px;background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.25);border-radius:20px;padding:4px 12px;font-size:.75rem;font-weight:600;color:var(--green);margin-bottom:20px;">🟢 Laufende Abstimmung – MBL-Vorschau</div>
  {% endif %}

  {% if voting.public_key %}
  <div class="enc-notice">
    🔐 Diese Abstimmung ist verschlüsselt. Die Rohdaten können nur mit dem privaten Schlüssel entschlüsselt werden.
    <br><a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">→ Entschlüsselungs-Tool öffnen</a>
    {% if public_results_exist %}
    <br><a class="dec-link" href="/vote/{{ voting_id }}/public-results" style="color:var(--green);">→ Öffentliche Ergebnisseite anzeigen</a>
    {% endif %}
  </div>
  {% endif %}

  <div class="stat-row">
    <div class="stat-box"><div class="stat-num">{{ responses | length }}</div><div class="stat-lbl">Antworten</div></div>
    <div class="stat-box"><div class="stat-num">{{ voting.questions | length }}</div><div class="stat-lbl">Fragen</div></div>
    <div class="stat-box"><div class="stat-num">{{ '✅' if not voting.is_active else '🟢' }}</div><div class="stat-lbl">{{ 'Beendet' if not voting.is_active else 'Aktiv' }}</div></div>
  </div>

  {% if is_mbl %}
  <div style="margin-bottom:24px;">
    <a class="voters-link" href="/vote/{{ voting_id }}/voters">👁️ Voter-Log anzeigen (wer hat abgestimmt)</a>
  </div>
  {% endif %}

  {% if not voting.public_key %}
    {% for frage in voting.questions %}
    <div class="q-block" data-q-idx="{{ loop.index0 }}" data-q-type="{{ frage.Typ }}">
      <div class="q-label">Frage {{ loop.index }}</div>
      <div class="q-title">{{ frage.Frage }}</div>
      <div class="q-results" id="qr-{{ loop.index0 }}"><div style="color:var(--sub);font-size:.82rem;">Wird berechnet...</div></div>
    </div>
    {% endfor %}
  {% else %}
    <div class="q-block">
      <div class="q-title">Verschlüsselte Antworten ({{ responses|length }})</div>
      <div style="color:var(--sub);font-size:.85rem;">Nutze das <a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">Entschlüsselungs-Tool</a> um die Antworten mit deinem privaten Schlüssel zu entschlüsseln.</div>
    </div>
  {% endif %}
</div>
<script>
const RESPONSES = {{ responses | tojson }};
const QUESTIONS = {{ voting.questions | tojson }};
const IS_ENCRYPTED = {{ ('true' if voting.public_key else 'false') }};
if (!IS_ENCRYPTED && RESPONSES.length) {
  const parsed = RESPONSES.map(r => { try { return JSON.parse(r.answers); } catch { return {}; } });
  QUESTIONS.forEach((frage, qi) => {
    const container = document.getElementById(`qr-${qi}`);
    if (!container) return;
    const vals = parsed.map(p => p[frage.Frage]).filter(v => v !== null && v !== undefined);
    if (frage.Typ === 'text') {
      container.innerHTML = vals.filter(Boolean).map(v => `<div style="background:var(--card2);border:1px solid var(--border2);border-radius:var(--r2);padding:10px 14px;font-size:.88rem;margin-bottom:8px;">${esc(String(v))}</div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
    } else if (frage.Typ === 'rating') {
      const counts = {}; for (let v = frage.Min || 1; v <= (frage.Max || 5); v++) counts[v] = 0;
      vals.forEach(v => { if (counts[v] !== undefined) counts[v]++; });
      const max = Math.max(...Object.values(counts), 1);
      const avg = vals.length ? (vals.reduce((a, b) => a + Number(b), 0) / vals.length).toFixed(2) : '–';
      container.innerHTML = `<div style="color:var(--sub);font-size:.78rem;margin-bottom:12px;">Ø ${avg}</div>` + Object.entries(counts).map(([v, c]) => `<div class="bar-row"><div class="bar-label">★ ${v}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`).join('');
    } else if (frage.Typ === 'choice') {
      const counts = {}; vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { counts[item] = (counts[item] || 0) + 1; }); });
      const max = Math.max(...Object.values(counts), 1);
      container.innerHTML = Object.entries(counts).sort((a,b) => b[1]-a[1]).map(([label, c]) => `<div class="bar-row"><div class="bar-label" title="${esc(label)}">${esc(label)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
    } else if (frage.Typ === 'person') {
      const counts = {}; vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { const name = item?.name || item || '?'; counts[name] = (counts[name] || 0) + 1; }); });
      const max = Math.max(...Object.values(counts), 1);
      container.innerHTML = Object.entries(counts).sort((a,b) => b[1]-a[1]).map(([name, c]) => `<div class="bar-row"><div class="bar-label">${esc(name)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
    }
  });
}
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>"""


_RECONSTRUCT_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{{ voting.title }} – Entschlüsseln</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono&display=swap">
<style>
:root{--bg:#060708;--surface:#0f1117;--card:#161b24;--card2:#1c2130;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--dim:#3d4660;--accent:#5b8cff;--green:#4ade80;--gold:#fbbf24;--red:#f87171;--r:12px;--r2:8px;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:48px 22px;-webkit-font-smoothing:antialiased;}
.page{max-width:760px;margin:0 auto;}
h1{font-size:1.6rem;font-weight:700;margin-bottom:8px;}
.sub{color:var(--sub);font-size:.9rem;margin-bottom:32px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:28px;margin-bottom:20px;}
label{display:block;font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:8px;}
textarea.key-input{width:100%;background:var(--card2);border:1px solid var(--border2);color:var(--text);font-family:'DM Mono',monospace;font-size:.78rem;padding:14px;border-radius:var(--r2);resize:vertical;min-height:140px;outline:none;transition:border-color .15s;}
textarea.key-input:focus{border-color:var(--accent);}
.btn{background:var(--accent);color:#000;font-family:'DM Sans',sans-serif;font-weight:700;font-size:.9rem;padding:12px 28px;border:none;border-radius:var(--r2);cursor:pointer;margin-top:14px;transition:all .2s;}
.btn:hover:not(:disabled){filter:brightness(1.1);}
.btn:disabled{opacity:.4;cursor:not-allowed;}
.btn-green{background:var(--green);color:#000;}
.warn-box{background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.2);border-radius:var(--r2);padding:12px 16px;font-size:.82rem;color:var(--gold);margin-bottom:20px;}
.active-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.25);border-radius:20px;padding:4px 12px;font-size:.75rem;font-weight:600;color:var(--green);margin-bottom:20px;}
.pub-box{background:rgba(91,140,255,.07);border:1px solid rgba(91,140,255,.25);border-radius:var(--r2);padding:16px 20px;margin-top:16px;display:none;}
.pub-box.show{display:block;}
.pub-link{display:block;font-family:'DM Mono',monospace;font-size:.82rem;color:var(--accent);word-break:break-all;margin-top:8px;}
.already-pub{background:rgba(74,222,128,.07);border:1px solid rgba(74,222,128,.2);border-radius:var(--r2);padding:12px 16px;font-size:.82rem;color:var(--green);margin-bottom:20px;}
.result-container{display:none;}
.result-container.show{display:block;}
.q-block{background:var(--card2);border:1px solid var(--border2);border-radius:var(--r2);padding:18px 20px;margin-bottom:12px;}
.q-label{font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:4px;}
.q-title{font-size:.92rem;font-weight:500;margin-bottom:14px;}
.answer-row{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.88rem;color:var(--sub);}
.answer-row span{color:var(--text);}
.progress-text{font-size:.78rem;color:var(--sub);margin-top:10px;}
.dl-btn{background:var(--card2);border:1px solid var(--border2);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.85rem;font-weight:600;padding:9px 18px;border-radius:var(--r2);cursor:pointer;margin-right:8px;transition:all .15s;}
.dl-btn:hover{border-color:var(--accent);color:var(--accent);}
</style>
</head>
<body>
<div class="page">
  <h1>🔓 Antworten entschlüsseln</h1>
  <div class="sub">{{ voting.title }} — Entschlüsselung mit privatem RSA-Schlüssel</div>

  {% if voting.is_active %}
  <div class="active-badge">🟢 Abstimmung läuft noch – MBL-Vorschau</div>
  {% endif %}

  {% if already_published %}
  <div class="already-pub">✅ Ergebnisse bereits veröffentlicht · <a href="/vote/{{ voting_id }}/public-results" style="color:var(--green);">→ Öffentliche Seite öffnen</a></div>
  {% endif %}

  <div class="warn-box">⚠️ Der private Schlüssel verlässt <strong>niemals</strong> deinen Browser. Die Entschlüsselung findet vollständig lokal statt.</div>
  <div class="card">
    <label>Privater RSA-Schlüssel (PEM-Format)</label>
    <textarea class="key-input" id="privateKeyInput" placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;-----END RSA PRIVATE KEY-----"></textarea>
    <div><button class="btn" id="decryptBtn" onclick="startDecrypt()">🔓 Entschlüsseln ({{ responses | length }} Antworten)</button></div>
    <div class="progress-text" id="progressText"></div>
  </div>
  <div class="result-container" id="resultContainer">
    <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;">
      <button class="dl-btn" onclick="downloadJSON()">⬇️ JSON exportieren</button>
      <button class="dl-btn" onclick="downloadCSV()">⬇️ CSV exportieren</button>
      {% if is_mbl %}
      <button class="dl-btn btn-green" onclick="publishResults()" id="publishBtn">🌐 Ergebnisse veröffentlichen</button>
      {% endif %}
    </div>
    <div id="resultsGrid"></div>
    {% if is_mbl %}
    <div class="pub-box" id="pubBox">
      <strong style="font-size:.85rem;">✅ Veröffentlicht!</strong>
      <div style="font-size:.78rem;color:var(--sub);margin-top:4px;">Öffentlicher Link:</div>
      <a class="pub-link" id="pubLink" href="#" target="_blank"></a>
    </div>
    {% endif %}
  </div>
</div>
<script>
const RESPONSES = {{ responses | tojson }};
const QUESTIONS = {{ voting.questions | tojson }};
const VOTING_ID = {{ voting_id | tojson }};
const IS_MBL    = {{ is_mbl | tojson }};
let decryptedAll = [];
async function decryptEntry(encStr, privateKey) {
  const parts = encStr.split('|'); if (parts.length !== 4) throw new Error('Ungültiges Format');
  const [encKeyB64, tagB64, ivB64, cipherB64] = parts;
  const b64ToU8 = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const encAesKey = b64ToU8(encKeyB64), tag = b64ToU8(tagB64), iv = b64ToU8(ivB64), ciphertext = b64ToU8(cipherB64);
  const aesKeyBytes = await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, privateKey, encAesKey);
  const importedAes = await crypto.subtle.importKey('raw', aesKeyBytes, { name: 'AES-GCM' }, false, ['decrypt']);
  const combined = new Uint8Array(ciphertext.length + tag.length); combined.set(ciphertext); combined.set(tag, ciphertext.length);
  const plainBuf = await crypto.subtle.decrypt({ name: 'AES-GCM', iv }, importedAes, combined);
  return new TextDecoder().decode(plainBuf);
}
async function importPrivateKey(pem) {
  const pemBody = pem.replace(/-----BEGIN (?:RSA )?PRIVATE KEY-----/, '').replace(/-----END (?:RSA )?PRIVATE KEY-----/, '').replace(/\s+/g, '');
  const der = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  return await crypto.subtle.importKey('pkcs8', der.buffer, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt']);
}
async function startDecrypt() {
  const pem = document.getElementById('privateKeyInput').value.trim();
  if (!pem) { alert('Bitte privaten Schlüssel eingeben'); return; }
  const btn = document.getElementById('decryptBtn'); btn.disabled = true; btn.textContent = 'Entschlüsselt...';
  let privateKey; try { privateKey = await importPrivateKey(pem); } catch(e) { alert(e.message); btn.disabled = false; btn.textContent = '🔓 Entschlüsseln'; return; }
  const enc = RESPONSES.filter(r => r.is_encrypted); const prog = document.getElementById('progressText'); decryptedAll = []; let failed = 0;
  for (let i = 0; i < enc.length; i++) { prog.textContent = `${i+1} / ${enc.length} entschlüsselt...`; try { const plain = await decryptEntry(enc[i].answers, privateKey); decryptedAll.push({ data: JSON.parse(plain), submitted_at: enc[i].submitted_at }); } catch { failed++; } }
  prog.textContent = `✅ ${decryptedAll.length} entschlüsselt${failed > 0 ? ` (${failed} fehlgeschlagen)` : ''}`;
  renderResults(); btn.textContent = '🔓 Erneut entschlüsseln'; btn.disabled = false;
}
function renderResults() {
  const container = document.getElementById('resultsGrid'); const rc = document.getElementById('resultContainer'); rc.classList.add('show');
  const qBlocks = QUESTIONS.map((frage, qi) => { const vals = decryptedAll.map(d => d.data[frage.Frage]).filter(v => v !== null && v !== undefined); let inner = ''; if (frage.Typ === 'text') { inner = vals.map(v => `<div class="answer-row"><span>${esc(String(v))}</span></div>`).join(''); } else { const counts = {}; vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { const key = item?.name || item || '?'; counts[key] = (counts[key] || 0) + 1; }); }); inner = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([k, c]) => `<div class="answer-row"><span>${esc(k)}</span> — ${c}×</div>`).join(''); } return `<div class="q-block"><div class="q-label">Frage ${qi+1}</div><div class="q-title">${esc(frage.Frage)}</div>${inner || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>'}</div>`; });
  container.innerHTML = qBlocks.join('');
}
async function publishResults() {
  if (!IS_MBL) return;
  if (!decryptedAll.length) { alert('Zuerst entschlüsseln!'); return; }
  const btn = document.getElementById('publishBtn'); btn.disabled = true; btn.textContent = 'Veröffentliche...';
  try {
    const res = await fetch(`/api/vote/${VOTING_ID}/publish-results`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ results: decryptedAll })
    });
    const data = await res.json();
    if (data.ok) {
      const box = document.getElementById('pubBox'); const link = document.getElementById('pubLink');
      box.classList.add('show'); link.href = data.url; link.textContent = data.url;
      btn.textContent = '✅ Veröffentlicht';
    } else {
      alert('Fehler: ' + (data.error || 'Unbekannt')); btn.disabled = false; btn.textContent = '🌐 Veröffentlichen';
    }
  } catch(e) { alert('Netzwerkfehler: ' + e.message); btn.disabled = false; btn.textContent = '🌐 Veröffentlichen'; }
}
function downloadJSON() { const blob = new Blob([JSON.stringify(decryptedAll, null, 2)], {type:'application/json'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'abstimmung_ergebnisse.json'; a.click(); }
function downloadCSV() { const headers = ['Abgestimmt am', ...QUESTIONS.map(q => q.Frage)]; const rows = decryptedAll.map(d => [d.submitted_at, ...QUESTIONS.map(q => { const v = d.data[q.Frage]; if (Array.isArray(v)) return v.map(i => i?.name || i).join('; '); return v?.name || v || ''; })]); const csv = [headers, ...rows].map(r => r.map(c => `"${String(c||'').replace(/"/g,'""')}`).join(',')).join('\n'); const blob = new Blob(['\uFEFF' + csv], {type:'text/csv;charset=utf-8'}); const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = 'abstimmung_ergebnisse.csv'; a.click(); }
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>"""