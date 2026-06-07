"""
bot/core/web_app/flask_app/voting_routes.py
=============================================
NEUE FEATURES:
  - Voter-Log: Wer hat abgestimmt (Discord-User-ID + Name, NICHT was)
  - Voting Creator: Seite für MBL zum Erstellen/Bearbeiten von Abstimmungs-JSONs
  - Voter-Liste Route: /vote/<voting_id>/voters (nur MBL + ausgewählte Viewer)
  - API zum Verwalten von JSON-Dateien im Dateisystem

  NEU v2:
  - Person-Fragen: Min/Max Auswahl-Anzahl (min_select / max_select)
  - Person-Fragen: Mitgliedschaftsfilter nach Serverzugehörigkeit (min/max Tage)
  - Person-Fragen: Ergebnis-Modus wählbar (Einzel vs. Gruppen-Auszählung)
  - Bugfix: Mehrfachauswahl bei Person- und Choice-Fragen korrekt abwählbar
  NEU v3:
  - Fragen: Überschrift + Beschreibung pro Frage
  - Rollen-Filter: Abstimmende müssen bestimmte Rollen haben/nicht haben
  - Person-Fragen: Rollen-Filter für Kandidaten (require_roles / exclude_roles)
  - Person-Suchleiste: Keine Vorauswahl, nur bei Suche + bereits Ausgewählte immer sichtbar

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
    """Fetch members including joined_at and role IDs for filtering."""
    members = _bot_get(f"/guilds/{guild_id}/members?limit=1000") or []
    result = []
    for m in members:
        u = m.get("user", {})
        if u.get("bot"):
            continue
        joined_at = m.get("joined_at")
        membership_days = None
        if joined_at:
            try:
                joined_dt = datetime.fromisoformat(joined_at.replace("Z", "+00:00"))
                membership_days = (datetime.now(timezone.utc) - joined_dt).days
            except Exception:
                membership_days = None
        result.append({
            "id":              u.get("id", ""),
            "display":         m.get("nick") or u.get("global_name") or u.get("username") or "?",
            "username":        u.get("username", ""),
            "avatar": (
                f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=32"
                if u.get("avatar") else None
            ),
            "joined_at":       joined_at,
            "membership_days": membership_days,
            "roles":           m.get("roles", []),  # list of role ID strings
        })
    return result


def _get_guild_roles(guild_id: str) -> list[dict]:
    """Fetch all roles of a guild (id + name + color)."""
    roles = _bot_get(f"/guilds/{guild_id}/roles") or []
    result = []
    for r in roles:
        if r.get("name") == "@everyone":
            continue
        result.append({
            "id":    r.get("id", ""),
            "name":  r.get("name", "?"),
            "color": r.get("color", 0),
        })
    return sorted(result, key=lambda x: x["name"].lower())


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
    name = re.sub(r'[^\w\-_. äöüÄÖÜß]', '', name).strip()
    if not name.endswith(".json"):
        name += ".json"
    return name or "abstimmung.json"


def register_voting_routes(app, login_required=None, _is_mbl_fn=None, _bot_get_fn=None):
    from bot.core.supabase_client import get_supabase

    def _is_mbl_user(user: dict) -> bool:
        return bool(MBL_ID and user.get("id") == MBL_ID)

    def _can_see_voters(user: dict, voting: dict) -> bool:
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

        # Auto-close if ends_at has passed
        ends_at_raw = voting.get("ends_at")
        if ends_at_raw:
            try:
                ends_dt = datetime.fromisoformat(ends_at_raw.replace("Z", "+00:00"))
                if datetime.now(timezone.utc) >= ends_dt and voting.get("is_active"):
                    sb.table("votings").update({"is_active": False}).eq("id", voting_id).execute()
                    voting["is_active"] = False
            except Exception:
                pass

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
            ends_at=voting.get("ends_at", ""),
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

        session.pop(f"voter_{voting_id}", None)
        return jsonify({"ok": True, "message": "Danke für deine Teilnahme!"})

    # ── GET /vote/<voting_id>/voters ──────────────────────────────────────────
    @app.route("/vote/<voting_id>/voters")
    def vote_voters(voting_id):
        sb   = get_supabase()
        r    = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "", 404)
        voting = r.data[0]

        flask_user = session.get("user", {})
        if not _can_see_voters(flask_user, voting):
            return _render_error("Kein Zugriff", "Du hast keinen Zugriff auf diese Seite.", 403)

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

        flask_user   = flask_session.get("user", {})
        voter_session = flask_session.get(f"voter_{voting_id}", {})
        mbl_uid      = flask_user.get("id") or voter_session.get("user_id", "")
        is_mbl       = bool(MBL_ID and mbl_uid == MBL_ID)

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

        flask_user    = flask_session.get("user", {})
        voter_session = flask_session.get(f"voter_{voting_id}", {})
        mbl_uid       = flask_user.get("id") or voter_session.get("user_id", "")
        is_mbl        = bool(MBL_ID and mbl_uid == MBL_ID)

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

        # Find the voter's own roles so the frontend can apply question-level role gates
        voter_id   = voter_session.get("user_id", "")
        voter_roles = []
        for m in members:
            if m.get("id") == voter_id:
                voter_roles = m.get("roles", [])
                break

        return jsonify({"members": members, "voterRoles": voter_roles})

    # ── API: Guild roles (for creator role-picker) ────────────────────────────
    @app.route("/api/voting/roles/<server_id>")
    def api_voting_roles(server_id):
        """Return all roles of a guild. Used by creator to set role filters."""
        user = session.get("user", {})
        if not _is_mbl_user(user):
            return jsonify({"error": "Kein Zugriff"}), 403
        roles = _get_guild_roles(server_id)
        return jsonify({"roles": roles})

    # ── API: Voter-Log (JSON) ─────────────────────────────────────────────────
    @app.route("/api/vote/<voting_id>/voters")
    def api_vote_voters(voting_id):
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

    # ── POST /api/vote/<id>/publish-results ───────────────────────────────────
    @app.route("/api/vote/<voting_id>/publish-results", methods=["POST"])
    def api_publish_results(voting_id):
        from flask import session as flask_session
        import json as _json, datetime as _dt
        flask_user    = flask_session.get("user", {})
        voter_session = flask_session.get(f"voter_{voting_id}", {})
        mbl_uid       = flask_user.get("id") or voter_session.get("user_id", "")
        is_mbl        = bool(MBL_ID and mbl_uid == MBL_ID)
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

    # ── GET /vote/<id>/public-results ─────────────────────────────────────────
    @app.route("/vote/<voting_id>/public-results")
    def vote_public_results(voting_id):
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

    # ── Public: Key Generator ─────────────────────────────────────────────────
    @app.route("/tools/keygen")
    def keygen_page():
        return render_template_string(_KEYGEN_TEMPLATE)

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
        user = session["user"]
        return render_template_string(_VOTING_CREATOR_TEMPLATE, user=user)

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
                # Validate person-specific fields
                if frage.get("Typ") == "person":
                    mn = frage.get("min_select", 1)
                    mx = frage.get("max_select", 1)
                    if mn > mx:
                        errors.append(f"Frage {i+1}: min_select ({mn}) > max_select ({mx})")
        return jsonify({"valid": len(errors) == 0, "errors": errors})

    # ══════════════════════════════════════════════════════════════════════════
    # VOTING MANAGER (nur MBL) – Abstimmungen starten/beenden/fortsetzen
    # ══════════════════════════════════════════════════════════════════════════

    @app.route("/dashboard/voting-manager")
    @_mbl_required
    def voting_manager():
        user = session["user"]
        return render_template_string(_VOTING_MANAGER_TEMPLATE, user=user, web_base_url=WEB_BASE_URL)

    # ── API: Alle Votings laden ───────────────────────────────────────────────
    @app.route("/api/voting/manage/list", methods=["GET"])
    @_mbl_required
    def api_voting_manage_list():
        sb   = get_supabase()
        rows = sb.table("votings").select(
            "id, title, description, is_active, created_at, ends_at, server_id, created_by"
        ).order("created_at", desc=True).execute().data or []
        result = []
        for v in rows:
            try:
                resp_count = len(
                    sb.table("voting_responses").select("id").eq("voting_id", v["id"]).execute().data or []
                )
            except Exception:
                resp_count = 0
            v["response_count"]      = resp_count
            v["vote_url"]            = f"{WEB_BASE_URL}/vote/{v['id']}"
            v["results_url"]         = f"{WEB_BASE_URL}/vote/{v['id']}/results"
            v["voters_url"]          = f"{WEB_BASE_URL}/vote/{v['id']}/voters"
            v["reconstruct_url"]     = f"{WEB_BASE_URL}/vote/{v['id']}/reconstruct"
            v["public_results_url"]  = f"{WEB_BASE_URL}/vote/{v['id']}/public-results"
            result.append(v)
        return jsonify({"votings": result})

    # ── API: Voting starten (aktivieren) ──────────────────────────────────────
    @app.route("/api/voting/manage/<voting_id>/start", methods=["POST"])
    @_mbl_required
    def api_voting_manage_start(voting_id):
        sb      = get_supabase()
        payload = request.get_json(silent=True) or {}
        ends_at = payload.get("ends_at")  # ISO string or null
        update  = {"is_active": True}
        if ends_at is not None:
            update["ends_at"] = ends_at if ends_at else None
        sb.table("votings").update(update).eq("id", voting_id).execute()
        return jsonify({"ok": True})

    # ── API: Voting beenden ───────────────────────────────────────────────────
    @app.route("/api/voting/manage/<voting_id>/stop", methods=["POST"])
    @_mbl_required
    def api_voting_manage_stop(voting_id):
        sb = get_supabase()
        sb.table("votings").update({"is_active": False, "ends_at": None}).eq("id", voting_id).execute()
        return jsonify({"ok": True})

    # ── API: Voting-Deadline setzen ───────────────────────────────────────────
    @app.route("/api/voting/manage/<voting_id>/set-deadline", methods=["POST"])
    @_mbl_required
    def api_voting_manage_deadline(voting_id):
        sb      = get_supabase()
        payload = request.get_json(silent=True) or {}
        ends_at = payload.get("ends_at")
        sb.table("votings").update({"ends_at": ends_at}).eq("id", voting_id).execute()
        return jsonify({"ok": True})

    # ── API: Voting löschen ───────────────────────────────────────────────────
    @app.route("/api/voting/manage/<voting_id>/delete", methods=["DELETE"])
    @_mbl_required
    def api_voting_manage_delete(voting_id):
        sb = get_supabase()
        sb.table("voting_responses").delete().eq("voting_id", voting_id).execute()
        sb.table("voting_voter_log").delete().eq("voting_id", voting_id).execute()
        sb.table("votings").delete().eq("id", voting_id).execute()
        return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# TEMPLATES
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# KEY GENERATOR TEMPLATE (öffentlich zugänglich)
# ══════════════════════════════════════════════════════════════════════════════

_KEYGEN_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>RSA Schlüssel-Generator</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap">
<style>
:root{--bg:#060708;--card:#161b24;--card2:#1c2130;--border:#1f2535;--border2:#2a3245;--text:#e2e8f5;--sub:#7a8499;--dim:#3d4660;--accent:#5b8cff;--green:#4ade80;--gold:#fbbf24;--red:#f87171;--r:14px;--r2:8px;}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;padding:48px 22px;-webkit-font-smoothing:antialiased;background-image:radial-gradient(ellipse 80% 60% at 10% -10%,rgba(91,140,255,0.07) 0%,transparent 60%);}
.page{max-width:720px;margin:0 auto;}
.hero{text-align:center;margin-bottom:48px;}
.hero-icon{font-size:3rem;margin-bottom:16px;}
h1{font-size:2rem;font-weight:700;margin-bottom:8px;letter-spacing:-.5px;}
.hero-sub{color:var(--sub);font-size:.95rem;max-width:480px;margin:0 auto 28px;line-height:1.6;}
.security-badge{display:inline-flex;align-items:center;gap:8px;background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.25);border-radius:20px;padding:6px 16px;font-size:.78rem;font-weight:600;color:var(--green);}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--r);padding:28px;margin-bottom:18px;}
.card-title{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin-bottom:16px;}
.key-size-row{display:flex;gap:8px;margin-bottom:20px;}
.key-size-btn{flex:1;padding:10px;border-radius:var(--r2);border:1px solid var(--border2);background:var(--card2);color:var(--sub);font-family:'DM Sans',sans-serif;font-size:.85rem;font-weight:600;cursor:pointer;transition:all .15s;}
.key-size-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(91,140,255,.1);}
.gen-btn{width:100%;padding:14px;background:var(--green);color:#000;font-family:'DM Sans',sans-serif;font-weight:700;font-size:.95rem;border:none;border-radius:var(--r2);cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:10px;}
.gen-btn:hover:not(:disabled){filter:brightness(1.1);transform:translateY(-1px);}
.gen-btn:disabled{opacity:.5;cursor:not-allowed;transform:none;}
.key-box{background:var(--card2);border:1px solid var(--border2);border-radius:var(--r2);padding:14px;font-family:'DM Mono',monospace;font-size:.72rem;color:var(--sub);word-break:break-all;line-height:1.6;min-height:80px;white-space:pre-wrap;}
.key-actions{display:flex;gap:8px;margin-top:10px;}
.key-btn{background:var(--card2);border:1px solid var(--border2);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.82rem;font-weight:600;padding:8px 14px;border-radius:var(--r2);cursor:pointer;transition:all .15s;display:flex;align-items:center;gap:6px;}
.key-btn:hover{border-color:var(--accent);color:var(--accent);}
.key-btn.private{border-color:rgba(248,113,113,.3);color:var(--red);}
.key-btn.private:hover{background:rgba(248,113,113,.08);}
.warning-box{background:rgba(248,113,113,.07);border:1px solid rgba(248,113,113,.25);border-radius:var(--r2);padding:14px 18px;font-size:.84rem;color:var(--red);line-height:1.6;margin-top:16px;}
.warning-box strong{font-weight:700;}
.info-box{background:rgba(91,140,255,.07);border:1px solid rgba(91,140,255,.2);border-radius:var(--r2);padding:14px 18px;font-size:.84rem;color:var(--sub);line-height:1.6;}
.info-box strong{color:var(--text);}
.step-row{display:flex;gap:14px;margin-bottom:14px;align-items:flex-start;}
.step-num{width:28px;height:28px;border-radius:50%;background:rgba(91,140,255,.15);border:1px solid rgba(91,140,255,.3);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:.75rem;font-weight:700;flex-shrink:0;margin-top:1px;}
.step-text{font-size:.88rem;color:var(--sub);line-height:1.6;}
.step-text strong{color:var(--text);}
.spinner{width:18px;height:18px;border:2px solid rgba(0,0,0,.3);border-top-color:#000;border-radius:50%;animation:spin .6s linear infinite;}
@keyframes spin{to{transform:rotate(360deg)}}
.toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:var(--card);border:1px solid var(--green);color:var(--green);padding:10px 20px;border-radius:10px;font-size:.85rem;font-weight:600;z-index:9999;animation:fadeUp .2s ease;}
@keyframes fadeUp{from{opacity:0;transform:translateX(-50%) translateY(10px)}}
.key-label{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin-bottom:6px;}
.key-label.pub{color:var(--accent);}
.key-label.priv{color:var(--red);}
</style>
</head>
<body>
<div class="page">
  <div class="hero">
    <div class="hero-icon">🔐</div>
    <h1>RSA Schlüssel-Generator</h1>
    <p class="hero-sub">Generiere ein RSA-Schlüsselpaar für verschlüsselte Abstimmungen. Deine privaten Schlüssel verlassen niemals diesen Browser.</p>
    <div class="security-badge">🛡️ 100% clientseitig – kein Server sieht deine Keys</div>
  </div>

  <div class="card">
    <div class="card-title">⚙️ Schlüsselgröße</div>
    <div class="key-size-row">
      <button class="key-size-btn" data-bits="2048" onclick="selectBits(2048)">2048 Bit<br><span style="font-size:.72rem;font-weight:400;opacity:.7;">Standard</span></button>
      <button class="key-size-btn active" data-bits="4096" onclick="selectBits(4096)">4096 Bit<br><span style="font-size:.72rem;font-weight:400;opacity:.7;">Empfohlen</span></button>
    </div>
    <button class="gen-btn" id="genBtn" onclick="generateKeys()">
      <span id="genBtnText">🔑 Schlüsselpaar generieren</span>
    </button>
  </div>

  <div class="card" id="keysCard" style="display:none;">
    <div class="card-title">🔑 Generiertes Schlüsselpaar</div>

    <div style="margin-bottom:20px;">
      <div class="key-label pub">🌐 Public Key (für den Creator / MBL)</div>
      <div class="key-box" id="pubKeyBox">–</div>
      <div class="key-actions">
        <button class="key-btn" onclick="copyKey('pub')">📋 Kopieren</button>
        <button class="key-btn" onclick="downloadKey('pub')">⬇️ Als .pem speichern</button>
      </div>
    </div>

    <div>
      <div class="key-label priv">🔒 Private Key (NUR FÜR DICH – sicher aufbewahren!)</div>
      <div class="key-box" id="privKeyBox">–</div>
      <div class="key-actions">
        <button class="key-btn private" onclick="copyKey('priv')">📋 Kopieren</button>
        <button class="key-btn private" onclick="downloadKey('priv')">⬇️ Als .pem speichern</button>
      </div>
    </div>

    <div class="warning-box" style="margin-top:20px;">
      ⚠️ <strong>Wichtig:</strong> Speichere den privaten Schlüssel sofort sicher ab (z.B. als .pem-Datei)!
      Er wird <strong>nicht auf dem Server gespeichert</strong> und kann nicht wiederhergestellt werden.
      Ohne den privaten Schlüssel können die Abstimmungsergebnisse <strong>nicht entschlüsselt</strong> werden.
    </div>
  </div>

  <div class="card">
    <div class="card-title">📖 Anleitung</div>
    <div class="step-row"><div class="step-num">1</div><div class="step-text"><strong>Public Key kopieren</strong> → Im Voting-Creator unter "🔐 Ende-zu-Ende Verschlüsselung" einfügen</div></div>
    <div class="step-row"><div class="step-num">2</div><div class="step-text"><strong>Private Key sicher speichern</strong> → Als .pem-Datei herunterladen und an einem sicheren Ort aufbewahren</div></div>
    <div class="step-row"><div class="step-num">3</div><div class="step-text"><strong>Abstimmung läuft</strong> → Alle Antworten werden mit deinem Public Key verschlüsselt, niemand kann sie ohne deinen Private Key lesen</div></div>
    <div class="step-row"><div class="step-num">4</div><div class="step-text"><strong>Auswertung</strong> → Unter <code style="background:var(--card2);padding:1px 5px;border-radius:4px;">/vote/ID/reconstruct</code> Private Key eingeben um alle Antworten lokal zu entschlüsseln</div></div>
    <div class="info-box" style="margin-top:6px;">
      <strong>Technisch:</strong> RSA-OAEP (SHA-256) + AES-GCM 256-bit hybrid encryption via Web Crypto API.
      Alle Operationen laufen vollständig im Browser – kein Netzwerkzugriff für die Schlüsselgenerierung.
    </div>
  </div>
</div>

<script>
let _selectedBits = 4096;
let _pubKey  = '';
let _privKey = '';

function selectBits(bits) {
  _selectedBits = bits;
  document.querySelectorAll('.key-size-btn').forEach(b => b.classList.toggle('active', +b.dataset.bits === bits));
}

async function generateKeys() {
  const btn     = document.getElementById('genBtn');
  const btnText = document.getElementById('genBtnText');
  btn.disabled  = true;
  btnText.innerHTML = '<div class="spinner"></div> Generiere (' + _selectedBits + ' Bit)...';

  try {
    const keyPair = await crypto.subtle.generateKey(
      { name: 'RSA-OAEP', modulusLength: _selectedBits, publicExponent: new Uint8Array([1,0,1]), hash: 'SHA-256' },
      true, ['encrypt', 'decrypt']
    );

    const pubDer  = await crypto.subtle.exportKey('spki',  keyPair.publicKey);
    const privDer = await crypto.subtle.exportKey('pkcs8', keyPair.privateKey);

    const b64 = buf => btoa(String.fromCharCode(...new Uint8Array(buf)));
    const wrapPem = (label, b64str) => {
      const lines = b64str.match(/.{1,64}/g).join('\n');
      return `-----BEGIN ${label}-----\n${lines}\n-----END ${label}-----`;
    };

    _pubKey  = wrapPem('PUBLIC KEY',      b64(pubDer));
    _privKey = wrapPem('PRIVATE KEY', b64(privDer));

    document.getElementById('pubKeyBox').textContent  = _pubKey;
    document.getElementById('privKeyBox').textContent = _privKey;
    document.getElementById('keysCard').style.display = '';
    document.getElementById('keysCard').scrollIntoView({ behavior:'smooth' });
  } catch(e) {
    alert('Fehler beim Generieren: ' + e.message);
  }
  btn.disabled = false;
  btnText.textContent = '🔄 Neues Schlüsselpaar generieren';
}

function copyKey(type) {
  const text = type === 'pub' ? _pubKey : _privKey;
  navigator.clipboard.writeText(text).then(() => showToast(type === 'pub' ? '✅ Public Key kopiert!' : '✅ Private Key kopiert!'));
}

function downloadKey(type) {
  const text = type === 'pub' ? _pubKey : _privKey;
  const name = type === 'pub' ? 'public_key.pem' : 'private_key.pem';
  const blob = new Blob([text], { type: 'text/plain' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = name;
  a.click();
  showToast(type === 'pub' ? '⬇️ Public Key gespeichert' : '⬇️ Private Key gespeichert');
}

function showToast(msg) {
  const t = document.createElement('div');
  t.className = 'toast';
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2800);
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# VOTING MANAGER TEMPLATE (nur MBL)
# ══════════════════════════════════════════════════════════════════════════════

_VOTING_MANAGER_TEMPLATE = r"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Voting Manager – Insel Bot</title>
<link rel="stylesheet" href="/static/css/main.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap">
<style>
body{font-family:'Space Grotesk',sans-serif;}
.mgr-wrap{max-width:1100px;margin:0 auto;padding:24px 22px 80px;}
.topbar-row{display:flex;align-items:center;gap:12px;margin-bottom:28px;flex-wrap:wrap;}
.section-hd{display:flex;align-items:center;justify-content:space-between;margin-bottom:18px;flex-wrap:wrap;gap:10px;}
.section-hd h2{font-size:1rem;font-weight:700;}
.vote-card{background:var(--bg-card);border:1px solid var(--border);border-radius:var(--r-lg);padding:20px;margin-bottom:14px;transition:border-color .15s;}
.vote-card:hover{border-color:var(--border2);}
.vote-card-hd{display:flex;align-items:flex-start;gap:14px;margin-bottom:14px;}
.status-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;margin-top:6px;}
.status-dot.active{background:#4ade80;box-shadow:0 0 8px rgba(74,222,128,.6);}
.status-dot.inactive{background:#3d4660;}
.vote-title{font-size:1rem;font-weight:700;margin-bottom:3px;}
.vote-meta{font-size:.75rem;color:var(--text3);line-height:1.6;}
.vote-meta code{background:var(--bg-surface);padding:1px 5px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:.7rem;}
.vote-stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
.vote-stat{background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:8px 14px;font-size:.78rem;color:var(--text2);}
.vote-stat span{font-size:1.05rem;font-weight:700;color:var(--green2);font-family:'JetBrains Mono',monospace;margin-right:5px;}
.vote-actions{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.btn{display:inline-flex;align-items:center;gap:6px;padding:8px 16px;border-radius:var(--r-sm);font-family:'Space Grotesk',sans-serif;font-weight:600;font-size:.82rem;cursor:pointer;border:none;transition:all .15s;text-decoration:none;}
.btn-green{background:var(--green2);color:#000;}
.btn-green:hover{filter:brightness(1.1);}
.btn-red{background:rgba(248,113,113,.15);color:#f87171;border:1px solid rgba(248,113,113,.3);}
.btn-red:hover{background:rgba(248,113,113,.25);}
.btn-ghost{background:var(--bg-surface);color:var(--text2);border:1px solid var(--border2);}
.btn-ghost:hover{border-color:var(--green2);color:var(--green2);}
.btn-link{background:transparent;color:var(--text3);border:none;padding:6px 8px;font-size:.78rem;}
.btn-link:hover{color:var(--text2);}
.btn-sm{padding:5px 10px;font-size:.74rem;}
.btn-danger{background:rgba(248,113,113,.1);color:#f87171;border:1px solid rgba(248,113,113,.2);}
.btn-danger:hover{background:rgba(248,113,113,.2);}
.deadline-row{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap;}
.deadline-input{background:var(--bg-surface);border:1px solid var(--border2);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.82rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;transition:border-color .15s;}
.deadline-input:focus{border-color:var(--green2);}
.deadline-badge{display:inline-flex;align-items:center;gap:5px;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.25);border-radius:8px;padding:3px 10px;font-size:.72rem;font-weight:600;color:#fbbf24;}
.deadline-badge.expired{background:rgba(248,113,113,.1);border-color:rgba(248,113,113,.3);color:#f87171;}
.links-row{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);}
.link-item{display:inline-flex;align-items:center;gap:5px;color:var(--text3);text-decoration:none;font-size:.75rem;padding:4px 8px;border-radius:6px;border:1px solid var(--border);transition:all .15s;}
.link-item:hover{border-color:var(--green2);color:var(--green2);}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9998;display:none;align-items:center;justify-content:center;padding:24px;}
.modal-overlay.show{display:flex;}
.modal{background:var(--bg-card);border:1px solid var(--border2);border-radius:var(--r-xl);padding:28px;max-width:480px;width:100%;box-shadow:0 24px 80px rgba(0,0,0,.6);}
.modal-title{font-size:1rem;font-weight:700;margin-bottom:16px;}
.modal-label{font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:6px;}
.duration-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px;}
.dur-btn{background:var(--bg-surface);border:1px solid var(--border2);color:var(--text2);padding:10px 6px;border-radius:var(--r-sm);font-family:'Space Grotesk',sans-serif;font-size:.8rem;font-weight:600;cursor:pointer;transition:all .15s;text-align:center;}
.dur-btn:hover,.dur-btn.active{border-color:var(--green2);color:var(--green2);background:rgba(34,197,94,.08);}
.toast-c{position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;}
.toast{background:var(--bg-card);border:1px solid var(--border2);border-radius:var(--r);padding:10px 16px;font-size:.82rem;color:var(--text);min-width:200px;display:flex;align-items:center;gap:8px;animation:fadeDown .2s ease;}
.toast.ok{border-left:3px solid var(--green2);}
.toast.err{border-left:3px solid #f87171;}
@keyframes fadeDown{from{opacity:0;transform:translateY(-8px)}}
.empty-state{text-align:center;padding:60px 20px;color:var(--text3);}
.filter-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;}
.filter-btn{background:var(--bg-surface);border:1px solid var(--border);color:var(--text3);padding:6px 14px;border-radius:20px;font-family:'Space Grotesk',sans-serif;font-size:.78rem;font-weight:600;cursor:pointer;transition:all .15s;}
.filter-btn.active{background:rgba(34,197,94,.1);border-color:var(--green2);color:var(--green2);}
.search-inp{background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.85rem;padding:8px 14px;border-radius:var(--r-sm);outline:none;flex:1;min-width:180px;}
.search-inp:focus{border-color:var(--green2);}
</style>
</head>
<body class="detail-body">
<div class="toast-c" id="toastC"></div>

<div class="detail-topbar">
  <a href="/dashboard" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    Dashboard
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">🗳️ Voting Manager</span>
  <div style="margin-left:auto;display:flex;gap:8px;">
    <a href="/dashboard/voting-creator" class="btn btn-ghost" style="font-size:.8rem;">✏️ Creator</a>
    <a href="/tools/keygen" target="_blank" class="btn btn-ghost" style="font-size:.8rem;">🔑 Key-Generator</a>
  </div>
</div>

<div class="mgr-wrap">
  <div class="section-hd">
    <h2>Alle Abstimmungen</h2>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <input class="search-inp" id="searchInp" placeholder="🔍 Suchen..." oninput="renderList()">
      <button class="btn btn-green" onclick="location.href='/dashboard/voting-creator'">+ Neue Abstimmung</button>
    </div>
  </div>

  <div class="filter-row">
    <button class="filter-btn active" data-filter="all"      onclick="setFilter('all')">Alle</button>
    <button class="filter-btn"        data-filter="active"   onclick="setFilter('active')">🟢 Aktiv</button>
    <button class="filter-btn"        data-filter="inactive" onclick="setFilter('inactive')">⏹ Beendet</button>
  </div>

  <div id="votingList"><div class="empty-state">Lade Abstimmungen...</div></div>
</div>

<!-- Deadline Modal -->
<div class="modal-overlay" id="deadlineModal">
  <div class="modal">
    <div class="modal-title" id="deadlineModalTitle">⏱️ Abstimmungs-Dauer festlegen</div>
    <div id="deadlineModalHint" style="font-size:.8rem;color:var(--text3);margin-bottom:12px;margin-top:-8px;"></div>
    <div class="modal-label">Schnellauswahl</div>
    <div class="duration-grid">
      <button class="dur-btn" onclick="setDuration(30,'min')">30 Min</button>
      <button class="dur-btn" onclick="setDuration(1,'h')">1 Std</button>
      <button class="dur-btn" onclick="setDuration(2,'h')">2 Std</button>
      <button class="dur-btn" onclick="setDuration(6,'h')">6 Std</button>
      <button class="dur-btn" onclick="setDuration(12,'h')">12 Std</button>
      <button class="dur-btn" onclick="setDuration(24,'h')">1 Tag</button>
      <button class="dur-btn" onclick="setDuration(48,'h')">2 Tage</button>
      <button class="dur-btn" onclick="setDuration(72,'h')">3 Tage</button>
      <button class="dur-btn" onclick="setDuration(168,'h')">7 Tage</button>
    </div>
    <div class="modal-label">Oder genaues Enddatum</div>
    <input type="datetime-local" id="customDeadline" class="deadline-input" style="width:100%;margin-bottom:16px;">
    <div style="display:flex;gap:8px;">
      <button class="btn btn-green" id="startBtn" style="flex:1;display:none;" onclick="startWithDeadline()">▶️ Starten</button>
      <button class="btn btn-green" id="applyBtn" style="flex:1;" onclick="applyDeadline()">✅ Übernehmen</button>
      <button class="btn btn-ghost" onclick="closeDeadlineModal()">Abbrechen</button>
      <button class="btn btn-danger btn-sm" onclick="clearOrSkipDeadline()">Kein Limit</button>
    </div>
  </div>
</div>

<script>
let _votings      = [];
let _filter       = 'all';
let _deadlineVid  = null;
let _deadlineMode = 'set'; // 'set' | 'start'

function toast(msg, type='ok') {
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${type==='ok'?'✅':'❌'}</span><span>${msg}</span>`;
  document.getElementById('toastC').appendChild(t);
  setTimeout(() => t.remove(), 3500);
}

function setFilter(f) {
  _filter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter === f));
  renderList();
}

async function loadVotings() {
  try {
    const r = await fetch('/api/voting/manage/list');
    const d = await r.json();
    _votings = d.votings || [];
    renderList();
  } catch(e) { toast('Fehler beim Laden', 'err'); }
}

function renderList() {
  const query = document.getElementById('searchInp').value.toLowerCase();
  let list = _votings;
  if (_filter === 'active')   list = list.filter(v => v.is_active);
  if (_filter === 'inactive') list = list.filter(v => !v.is_active);
  if (query) list = list.filter(v => v.title.toLowerCase().includes(query) || v.id.includes(query));

  const container = document.getElementById('votingList');
  if (!list.length) {
    container.innerHTML = '<div class="empty-state">Keine Abstimmungen gefunden.</div>';
    return;
  }
  container.innerHTML = list.map(v => renderCard(v)).join('');
  // Start countdowns
  list.forEach(v => { if (v.is_active && v.ends_at) startCountdown(v.id, v.ends_at); });
}

function formatDate(iso) {
  if (!iso) return '–';
  try { return new Date(iso).toLocaleString('de-DE', { day:'2-digit', month:'2-digit', year:'numeric', hour:'2-digit', minute:'2-digit' }); }
  catch { return iso; }
}

function renderCard(v) {
  const isActive  = v.is_active;
  const hasDeadline = v.ends_at;
  const deadlineExpired = hasDeadline && new Date(v.ends_at) < new Date();

  let deadlineBadge = '';
  if (hasDeadline) {
    if (deadlineExpired) {
      deadlineBadge = `<span class="deadline-badge expired">⏰ Abgelaufen: ${formatDate(v.ends_at)}</span>`;
    } else {
      deadlineBadge = `<span class="deadline-badge">⏱️ Endet: ${formatDate(v.ends_at)} (<span id="cd_${v.id}">...</span>)</span>`;
    }
  }

  const startStopBtn = isActive
    ? `<button class="btn btn-red" onclick="stopVoting('${v.id}')">⏹ Beenden</button>`
    : `<button class="btn btn-green" onclick="startVoting('${v.id}')">▶️ Starten / Fortsetzen</button>`;

  return `<div class="vote-card" id="vc_${v.id}">
    <div class="vote-card-hd">
      <div class="status-dot ${isActive?'active':'inactive'}"></div>
      <div style="flex:1;min-width:0;">
        <div class="vote-title">${esc(v.title)}</div>
        <div class="vote-meta">
          ID: <code>${v.id}</code> · Server: <code>${v.server_id||'–'}</code>
          · Erstellt: ${formatDate(v.created_at)}
        </div>
        ${deadlineBadge ? `<div style="margin-top:6px;">${deadlineBadge}</div>` : ''}
      </div>
    </div>
    <div class="vote-stats">
      <div class="vote-stat"><span>${v.response_count}</span>Antworten</div>
      <div class="vote-stat"><span>${isActive?'🟢':'⏹'}</span>${isActive?'Aktiv':'Beendet'}</div>
    </div>
    <div class="vote-actions">
      ${startStopBtn}
      <button class="btn btn-ghost" onclick="openDeadlineModal('${v.id}', '${v.ends_at||''}')">⏱️ Deadline</button>
      <button class="btn btn-danger btn-sm" onclick="deleteVoting('${v.id}', '${esc(v.title)}')">🗑️</button>
    </div>
    <div class="links-row">
      <a class="link-item" href="${v.vote_url}" target="_blank">🗳️ Abstimmung</a>
      <a class="link-item" href="${v.results_url}" target="_blank">📊 Ergebnisse</a>
      <a class="link-item" href="${v.voters_url}" target="_blank">👁️ Voter-Log</a>
      <a class="link-item" href="${v.reconstruct_url}" target="_blank">🔓 Entschlüsseln</a>
      <a class="link-item" href="${v.public_results_url}" target="_blank">🌐 Public Results</a>
      <button class="btn btn-link" onclick="copyLink('${v.vote_url}')">📋 Link kopieren</button>
    </div>
  </div>`;
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function copyLink(url) {
  navigator.clipboard.writeText(url).then(() => toast('Link kopiert!'));
}

async function startVoting(vid) {
  _deadlineVid  = vid;
  _deadlineMode = 'start';
  const v   = _votings.find(x => x.id === vid);
  const inp = document.getElementById('customDeadline');
  if (v && v.ends_at && new Date(v.ends_at) > new Date()) {
    inp.value = new Date(v.ends_at).toISOString().slice(0,16);
  } else {
    inp.value = new Date(Date.now() + 24*3600000).toISOString().slice(0,16);
  }
  document.querySelectorAll('.dur-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('deadlineModalTitle').textContent = '▶️ Abstimmung starten';
  document.getElementById('deadlineModalHint').textContent  = 'Optional: Enddatum festlegen. Lasse das Feld frei für unbegrenzte Laufzeit.';
  document.getElementById('startBtn').style.display  = '';
  document.getElementById('applyBtn').style.display  = 'none';
  document.getElementById('deadlineModal').classList.add('show');
}

async function _doStart(vid, endsAt) {
  const r = await fetch(`/api/voting/manage/${vid}/start`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ends_at: endsAt || null })
  });
  const d = await r.json();
  if (d.ok) { toast('Abstimmung gestartet ▶️'); closeDeadlineModal(); await loadVotings(); }
  else toast('Fehler: ' + (d.error||'?'), 'err');
}

async function stopVoting(vid) {
  if (!confirm('Abstimmung wirklich beenden?')) return;
  const r = await fetch(`/api/voting/manage/${vid}/stop`, { method:'POST' });
  const d = await r.json();
  if (d.ok) { toast('Abstimmung beendet'); await loadVotings(); }
  else toast('Fehler', 'err');
}

async function deleteVoting(vid, title) {
  if (!confirm(`"${title}" und alle Antworten wirklich löschen?\nDas kann nicht rückgängig gemacht werden!`)) return;
  const r = await fetch(`/api/voting/manage/${vid}/delete`, { method:'DELETE' });
  const d = await r.json();
  if (d.ok) { toast('Gelöscht'); await loadVotings(); }
  else toast('Fehler', 'err');
}

// ── Deadline Modal ─────────────────────────────────────────────────────────
// openDeadlineModal is defined further below

function closeDeadlineModal() {
  document.getElementById('deadlineModal').classList.remove('show');
  _deadlineVid = null;
}

function setDuration(amount, unit) {
  const ms = unit === 'min' ? amount*60000 : amount*3600000;
  const d  = new Date(Date.now() + ms);
  document.getElementById('customDeadline').value = d.toISOString().slice(0,16);
  document.querySelectorAll('.dur-btn').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
}

async function applyDeadline() {
  if (!_deadlineVid) return;
  const val = document.getElementById('customDeadline').value;
  if (!val) { toast('Bitte Datum wählen', 'err'); return; }
  const endsAt = new Date(val).toISOString();
  const r = await fetch(`/api/voting/manage/${_deadlineVid}/set-deadline`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ends_at: endsAt })
  });
  const d = await r.json();
  if (d.ok) { toast('Deadline gesetzt'); closeDeadlineModal(); await loadVotings(); }
  else toast('Fehler', 'err');
}

async function clearDeadline() {
  if (!_deadlineVid) return;
  const r = await fetch(`/api/voting/manage/${_deadlineVid}/set-deadline`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ ends_at: null })
  });
  const d = await r.json();
  if (d.ok) { toast('Deadline entfernt'); closeDeadlineModal(); await loadVotings(); }
  else toast('Fehler', 'err');
}

async function startWithDeadline() {
  if (!_deadlineVid) return;
  const val    = document.getElementById('customDeadline').value;
  const endsAt = val ? new Date(val).toISOString() : null;
  await _doStart(_deadlineVid, endsAt);
}

async function clearOrSkipDeadline() {
  if (_deadlineMode === 'start') {
    await _doStart(_deadlineVid, null);
  } else {
    await clearDeadline();
  }
}

function openDeadlineModal(vid, currentEndsAt) {
  _deadlineVid  = vid;
  _deadlineMode = 'set';
  const inp = document.getElementById('customDeadline');
  if (currentEndsAt) {
    try { inp.value = new Date(currentEndsAt).toISOString().slice(0,16); } catch {}
  } else {
    inp.value = new Date(Date.now() + 24*3600000).toISOString().slice(0,16);
  }
  document.querySelectorAll('.dur-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('deadlineModalTitle').textContent = '⏱️ Abstimmungs-Dauer festlegen';
  document.getElementById('deadlineModalHint').textContent  = '';
  document.getElementById('startBtn').style.display  = 'none';
  document.getElementById('applyBtn').style.display  = '';
  document.getElementById('deadlineModal').classList.add('show');
}

// ── Countdown ──────────────────────────────────────────────────────────────
const _countdownIntervals = {};
function startCountdown(vid, endsAt) {
  if (_countdownIntervals[vid]) clearInterval(_countdownIntervals[vid]);
  const el = document.getElementById(`cd_${vid}`);
  if (!el) return;
  const endTs = new Date(endsAt).getTime();
  function update() {
    const diff = endTs - Date.now();
    if (!document.getElementById(`cd_${vid}`)) { clearInterval(_countdownIntervals[vid]); return; }
    if (diff <= 0) { el.textContent = 'abgelaufen'; clearInterval(_countdownIntervals[vid]); loadVotings(); return; }
    const h = Math.floor(diff/3600000);
    const m = Math.floor((diff%3600000)/60000);
    const s = Math.floor((diff%60000)/1000);
    el.textContent = h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`;
  }
  update();
  _countdownIntervals[vid] = setInterval(update, 1000);
}

// Close modal on backdrop click
document.getElementById('deadlineModal').addEventListener('click', e => {
  if (e.target === document.getElementById('deadlineModal')) closeDeadlineModal();
});

loadVotings();
// Refresh every 30s
setInterval(loadVotings, 30000);
</script>
</body>
</html>"""


# ── Shared JS result-rendering helper (group vs individual counting) ──────────
# This snippet is embedded in both _RESULTS_TEMPLATE and _PUBLIC_RESULTS_TEMPLATE
_RESULT_JS_HELPER = r"""
function computePersonResults(vals, resultMode) {
  // vals: array of single items or arrays (each voter's answer)
  if (resultMode === 'group') {
    // Count each unique combination as one vote
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      const names = items.map(i => i?.name || String(i || '?')).sort().join(' + ');
      counts[names] = (counts[names] || 0) + 1;
    });
    return counts;
  } else {
    // Count each person individually
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      items.forEach(item => {
        const name = item?.name || String(item || '?');
        counts[name] = (counts[name] || 0) + 1;
      });
    });
    return counts;
  }
}
"""

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
.q-mode-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(91,140,255,.1);border:1px solid rgba(91,140,255,.2);border-radius:10px;padding:2px 8px;font-size:.65rem;font-weight:600;color:#5b8cff;margin-bottom:12px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:180px;font-size:.82rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
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
  <div class="q-block" data-q-idx="{{ loop.index0 }}" data-q-type="{{ frage.Typ }}" data-result-mode="{{ frage.get('result_mode', 'individual') }}">
    <div class="q-label">Frage {{ loop.index }}</div>
    {% if frage.get('heading') %}<div style="font-size:1.05rem;font-weight:700;margin-bottom:4px;letter-spacing:-.2px;">{{ frage.heading }}</div>{% endif %}
    <div class="q-title">{{ frage.Frage }}</div>
    {% if frage.get('subtext') %}<div style="font-size:.8rem;color:var(--sub);margin-bottom:10px;line-height:1.5;">{{ frage.subtext }}</div>{% endif %}
    {% if frage.Typ == 'person' %}
    <div class="q-mode-badge">{{ '👥 Gruppen-Auswertung' if frage.get('result_mode','individual') == 'group' else '👤 Einzel-Auswertung' }}</div>
    {% endif %}
    <div class="q-results" id="qr-{{ loop.index0 }}"></div>
  </div>
  {% endfor %}
</div>
<script>
const DATA = {{ data | tojson }};
const entries = DATA.entries || [];
document.getElementById('totalCount').textContent = entries.length;
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function computePersonResults(vals, resultMode) {
  if (resultMode === 'group') {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      const names = items.map(i => i?.name || String(i || '?')).sort().join(' + ');
      counts[names] = (counts[names] || 0) + 1;
    });
    return counts;
  } else {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      items.forEach(item => {
        const name = item?.name || String(item || '?');
        counts[name] = (counts[name] || 0) + 1;
      });
    });
    return counts;
  }
}
DATA.questions.forEach((frage, qi) => {
  const container = document.getElementById('qr-' + qi);
  const vals = entries.map(e => e.data && e.data[frage.Frage]).filter(v => v !== null && v !== undefined);
  if (frage.Typ === 'text') {
    container.innerHTML = vals.map(v => `<div class="answer-row"><span>${esc(String(v))}</span></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
  } else if (frage.Typ === 'person') {
    const resultMode = frage.result_mode || 'individual';
    const counts = computePersonResults(vals, resultMode);
    const sorted = Object.entries(counts).sort((a,b)=>b[1]-a[1]); const max = sorted[0]?.[1] || 1;
    container.innerHTML = sorted.map(([k,c]) => `<div class="bar-row"><div class="bar-label" title="${esc(k)}">${esc(k)}</div><div class="bar-track"><div class="bar-fill" style="width:${Math.round(c/max*100)}%"></div></div><div class="bar-count">${c}</div></div>`).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
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
    <div class="stat"><div class="stat-num">{{ total_responses }}</div><div class="stat-lbl">Abgestimmt</div></div>
    <div class="stat"><div class="stat-num">{{ voters | length }}</div><div class="stat-lbl">Erfasst im Log</div></div>
    <div class="stat"><div class="stat-num">{{ '✅' if not voting.is_active else '🟢' }}</div><div class="stat-lbl">{{ 'Abgeschlossen' if not voting.is_active else 'Aktiv' }}</div></div>
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
body { font-family: 'Space Grotesk', sans-serif; }
.creator-wrap { max-width: 1200px; margin: 0 auto; padding: 24px 22px 80px; }
.creator-grid { display: grid; grid-template-columns: 320px 1fr; gap: 22px; align-items: start; }
@media(max-width:900px){ .creator-grid { grid-template-columns: 1fr; } }
.files-panel { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--r-lg); overflow: hidden; position: sticky; top: 80px; }
.files-panel-hd { padding: 14px 18px; border-bottom: 1px solid var(--border); background: var(--bg-surface); display: flex; align-items: center; justify-content: space-between; }
.files-panel-title { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); }
.files-list { max-height: 400px; overflow-y: auto; }
.file-item { display: flex; align-items: center; gap: 10px; padding: 12px 16px; border-bottom: 1px solid var(--border); cursor: pointer; transition: background var(--fast); }
.file-item:hover { background: var(--bg-hover); }
.file-item.active { background: var(--green-glow); border-left: 2px solid var(--green2); }
.file-item-info { flex: 1; min-width: 0; }
.file-item-name { font-size: .83rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-item-meta { font-size: .68rem; color: var(--text3); margin-top: 2px; }
.file-del-btn { background: none; border: none; color: var(--text3); cursor: pointer; padding: 3px 5px; border-radius: 4px; font-size: .85rem; transition: color var(--fast); flex-shrink: 0; }
.file-del-btn:hover { color: var(--red); }
.no-files { padding: 30px 16px; text-align: center; color: var(--text3); font-size: .82rem; }
.editor-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--r-lg); overflow: hidden; }
.editor-hd { padding: 16px 20px; border-bottom: 1px solid var(--border); background: var(--bg-surface); display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.editor-title { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); }
.editor-body { padding: 24px; }
.fe { margin-bottom: 18px; }
.fe label { display: block; font-size: .63rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--text3); margin-bottom: 6px; }
.fe input[type=text], .fe input[type=number], .fe textarea, .fe select { width: 100%; background: var(--bg-surface); border: 1px solid var(--border); color: var(--text); font-family: 'Space Grotesk', sans-serif; font-size: .87rem; padding: 9px 12px; border-radius: var(--r-sm); outline: none; transition: border-color var(--fast), box-shadow var(--fast); }
.fe input:focus, .fe textarea:focus, .fe select:focus { border-color: var(--green2); box-shadow: 0 0 0 3px rgba(34,197,94,.12); }
.fe select option { background: #1d2128; }
.fe textarea { resize: vertical; min-height: 80px; }
.fe-hint { font-size: .68rem; color: var(--text3); margin-top: 4px; }
.fe-row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.fe-row-3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
@media(max-width:600px){ .fe-row, .fe-row-3 { grid-template-columns: 1fr; } }
.toggle-row { display: flex; align-items: center; justify-content: space-between; padding: 10px 14px; background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--r-sm); margin-bottom: 8px; }
.toggle-lbl { font-size: .84rem; font-weight: 500; }
.toggle-switch { position: relative; width: 42px; height: 22px; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-slider { position: absolute; inset: 0; cursor: pointer; background: var(--border2); border-radius: 22px; transition: background .2s; }
.toggle-slider:before { content: ''; position: absolute; width: 16px; height: 16px; border-radius: 50%; background: #fff; left: 3px; top: 3px; transition: transform .2s; }
.toggle-switch input:checked + .toggle-slider { background: var(--green2); }
.toggle-switch input:checked + .toggle-slider:before { transform: translateX(20px); }
.section-title { font-size: .75rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--text3); margin: 24px 0 12px; display: flex; align-items: center; gap: 8px; }
.section-title::after { content: ''; flex: 1; height: 1px; background: var(--border); }
.question-card { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--r); padding: 16px; margin-bottom: 10px; position: relative; }
.question-card-hd { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.q-num { width: 24px; height: 24px; border-radius: 50%; background: var(--green-glow); color: var(--green); border: 1px solid rgba(74,222,128,.25); display: flex; align-items: center; justify-content: center; font-size: .7rem; font-weight: 700; flex-shrink: 0; }
.q-type-badge { background: var(--bg-card); border: 1px solid var(--border2); color: var(--text3); padding: 2px 8px; border-radius: 20px; font-size: .64rem; font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
.q-del { background: none; border: none; color: var(--text3); cursor: pointer; padding: 4px 7px; border-radius: 5px; font-size: .9rem; margin-left: auto; transition: color var(--fast); }
.q-del:hover { color: var(--red); }
.q-move { background: none; border: none; color: var(--text3); cursor: pointer; padding: 4px 6px; border-radius: 5px; font-size: .85rem; transition: color var(--fast); }
.q-move:hover { color: var(--text2); }
.options-editor { margin-top: 12px; }
.option-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.option-row input { flex: 1; background: var(--bg-card); border: 1px solid var(--border); color: var(--text); font-family: 'Space Grotesk', sans-serif; font-size: .84rem; padding: 7px 10px; border-radius: var(--r-sm); outline: none; }
.option-row input:focus { border-color: var(--green2); }
.option-del { background: none; border: none; color: var(--text3); cursor: pointer; padding: 4px 7px; font-size: .9rem; border-radius: 4px; transition: color var(--fast); }
.option-del:hover { color: var(--red); }
.person-extra { margin-top: 12px; background: rgba(91,140,255,.04); border: 1px solid rgba(91,140,255,.15); border-radius: var(--r-sm); padding: 14px; }
.person-extra-title { font-size: .62rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: #5b8cff; margin-bottom: 10px; }
.result-mode-row { display: flex; gap: 8px; margin-top: 8px; }
.result-mode-btn { flex: 1; padding: 8px 10px; border-radius: var(--r-sm); border: 1px solid var(--border2); background: var(--bg-card); color: var(--text2); font-family: 'Space Grotesk', sans-serif; font-size: .78rem; font-weight: 600; cursor: pointer; transition: all var(--fast); text-align: center; }
.result-mode-btn.active { border-color: var(--green2); color: var(--green2); background: var(--green-g3); }
.membership-filter-section { margin-top: 10px; padding: 10px; background: var(--bg-card); border: 1px solid var(--border2); border-radius: var(--r-sm); }
.btn { display: inline-flex; align-items: center; gap: 6px; padding: 9px 18px; border-radius: var(--r-sm); font-family: 'Space Grotesk', sans-serif; font-weight: 600; font-size: .87rem; cursor: pointer; border: none; transition: all var(--mid); text-decoration: none; }
.btn:disabled { opacity: .4; cursor: not-allowed; }
.btn-primary { background: var(--green2); color: #000; }
.btn-primary:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
.btn-outline { background: transparent; color: var(--text2); border: 1px solid var(--border2); }
.btn-outline:hover:not(:disabled) { border-color: var(--green2); color: var(--green2); }
.btn-ghost { background: var(--bg-surface); color: var(--text2); border: 1px solid var(--border); }
.btn-ghost:hover:not(:disabled) { border-color: var(--border3); color: var(--text); }
.btn-sm { padding: 6px 12px; font-size: .78rem; }
.btn-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; }
.add-q-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; margin: 12px 0; }
.add-q-btn { background: var(--bg-surface); border: 1px solid var(--border2); color: var(--text2); padding: 10px 14px; border-radius: var(--r-sm); font-family: 'Space Grotesk', sans-serif; font-size: .83rem; font-weight: 600; cursor: pointer; transition: all var(--fast); display: flex; align-items: center; gap: 8px; text-align: left; }
.add-q-btn:hover { border-color: var(--green2); color: var(--green2); background: var(--green-g3); }
.toast-container { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
.toast { background: var(--bg-card); border: 1px solid var(--border2); border-radius: var(--r); padding: 11px 16px; font-size: .83rem; color: var(--text); box-shadow: var(--shadow); min-width: 220px; display: flex; align-items: center; gap: 8px; animation: fadeDown .2s ease both; }
.toast.ok  { border-left: 3px solid var(--green2); }
.toast.err { border-left: 3px solid var(--red); }
.toast.info { border-left: 3px solid var(--blue); }
.command-box { background: var(--bg-base); border: 1px solid var(--border); border-radius: var(--r); padding: 14px 16px; margin-top: 16px; display: none; }
.command-box.show { display: block; }
.command-box-title { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .08em; color: var(--green); margin-bottom: 8px; }
.command-line { font-family: 'JetBrains Mono', monospace; font-size: .78rem; color: var(--text2); background: var(--bg-surface); border: 1px solid var(--border2); border-radius: var(--r-sm); padding: 10px 14px; word-break: break-all; cursor: pointer; transition: border-color var(--fast); display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.command-line:hover { border-color: var(--green2); }
.cmd-copy { background: none; border: none; color: var(--text3); cursor: pointer; font-size: .8rem; flex-shrink: 0; transition: color var(--fast); }
.cmd-copy:hover { color: var(--green); }
.command-note { font-size: .7rem; color: var(--text3); margin-top: 6px; }
.viewer-chip { display: inline-flex; align-items: center; gap: 4px; background: rgba(91,140,255,.1); border: 1px solid rgba(91,140,255,.25); color: #5b8cff; padding: 2px 8px; border-radius: 12px; font-size: .73rem; font-weight: 600; margin: 2px; }
.viewer-chip-del { background: none; border: none; color: rgba(91,140,255,.6); cursor: pointer; padding: 0 2px; font-size: .85rem; transition: color var(--fast); }
.viewer-chip-del:hover { color: var(--red); }
.val-error { background: var(--red-bg); border: 1px solid var(--red-border); border-radius: var(--r-sm); padding: 10px 14px; font-size: .82rem; color: var(--red); margin-top: 8px; display: none; }
.val-error.show { display: block; }
.val-error ul { padding-left: 16px; margin-top: 4px; }
.val-error li { margin-bottom: 2px; }
.role-chip { display:inline-flex;align-items:center;gap:3px;padding:2px 7px;border-radius:10px;font-size:.68rem;font-weight:600;margin:2px; }
.role-chip-require { background:rgba(74,222,128,.12);border:1px solid rgba(74,222,128,.3);color:#4ade80; }
.role-chip-exclude { background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.3);color:#f87171; }
.role-chip-del { background:none;border:none;cursor:pointer;padding:0 2px;font-size:.8rem;opacity:.7;transition:opacity .15s; }
.role-chip-del:hover { opacity:1; }
.role-filter-section { background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:var(--r-sm);padding:10px 12px; }
</style>
</head>
<body class="detail-body">

<div class="toast-container" id="toastContainer"></div>

<div class="detail-topbar">
  <a href="/dashboard" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg>
    Dashboard
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">🗳️ Abstimmungs-Creator</span>
  <div style="margin-left:auto;display:flex;gap:8px;align-items:center;">
    <a href="/dashboard/voting-manager" style="background:var(--bg-surface);border:1px solid var(--border2);color:var(--text2);padding:5px 12px;border-radius:var(--r-sm);font-size:.78rem;font-weight:600;text-decoration:none;transition:all .15s;" onmouseover="this.style.borderColor='var(--green2)'" onmouseout="this.style.borderColor='var(--border2)'">📊 Manager</a>
    <a href="/tools/keygen" target="_blank" style="background:var(--bg-surface);border:1px solid var(--border2);color:var(--text2);padding:5px 12px;border-radius:var(--r-sm);font-size:.78rem;font-weight:600;text-decoration:none;transition:all .15s;" onmouseover="this.style.borderColor='var(--green2)'" onmouseout="this.style.borderColor='var(--border2)'">🔑 Keys</a>
    <span style="font-size:.75rem;color:var(--text3);">Nur für MBL</span>
  </div>
</div>

<div class="creator-wrap">
  <div class="creator-grid">

    <div>
      <div class="files-panel">
        <div class="files-panel-hd">
          <span class="files-panel-title">💾 Gespeicherte Dateien</span>
          <button class="btn btn-sm btn-primary" onclick="newVoting()">+ Neu</button>
        </div>
        <div class="files-list" id="filesList"><div class="no-files">Lade...</div></div>
      </div>
    </div>

    <div class="editor-card">
      <div class="editor-hd">
        <span class="editor-title">✏️ Abstimmung bearbeiten</span>
        <input type="text" id="filenameInput" placeholder="dateiname.json"
          style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;padding:5px 10px;border-radius:var(--r-sm);outline:none;width:160px;">
        <input type="text" id="guildServerIdInput" placeholder="Server-ID (für Rollen)"
          style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.78rem;padding:5px 10px;border-radius:var(--r-sm);outline:none;width:160px;"
          onchange="loadGuildRoles()">
        <div style="margin-left:auto;display:flex;gap:8px;">
          <button class="btn btn-ghost btn-sm" onclick="loadGuildRoles()" title="Rollen laden">🎭</button>
          <button class="btn btn-outline btn-sm" onclick="validateCurrent()">🔍 Prüfen</button>
          <button class="btn btn-primary btn-sm" onclick="saveFile()">💾 Speichern</button>
        </div>
      </div>

      <div class="editor-body">
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

        <div class="fe">
          <label>Voter-Log Betrachter (User-IDs)</label>
          <div id="viewerChips" style="margin-bottom:6px;min-height:24px;"></div>
          <div style="display:flex;gap:8px;">
            <input type="text" id="viewerIdInput" placeholder="Discord User-ID eingeben..."
              style="flex:1;background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-family:'Space Grotesk',sans-serif;font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;"
              onkeydown="if(event.key==='Enter'){addViewer();event.preventDefault();}">
            <button class="btn btn-ghost btn-sm" onclick="addViewer()">+ Hinzufügen</button>
          </div>
          <div class="fe-hint">Diese User-IDs sehen unter /vote/ID/voters wer abgestimmt hat</div>
        </div>

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
            </div>
          </div>
        </div>

        <div class="section-title">❓ Fragen</div>
        <div id="questionsList"></div>

        <div class="add-q-grid">
          <button class="add-q-btn" onclick="addQuestion('text')">📝 Freitext</button>
          <button class="add-q-btn" onclick="addQuestion('choice')">☑️ Auswahl</button>
          <button class="add-q-btn" onclick="addQuestion('rating')">⭐ Bewertung</button>
          <button class="add-q-btn" onclick="addQuestion('person')">👤 Person</button>
        </div>

        <div class="val-error" id="valError">
          <strong>⚠️ Fehler in der Konfiguration:</strong>
          <ul id="valErrorList"></ul>
        </div>

        <div class="btn-row">
          <button class="btn btn-primary" onclick="saveFile()">💾 Speichern</button>
          <button class="btn btn-outline" onclick="validateCurrent()">🔍 Validieren</button>
          <button class="btn btn-ghost" onclick="showJson()">{ } JSON anzeigen</button>
          <button class="btn btn-ghost" onclick="newVoting()">+ Neue Abstimmung</button>
        </div>

        <div class="command-box" id="commandBox">
          <div class="command-box-title">✅ Gespeichert – Discord Command</div>
          <div class="command-line" id="commandLine" onclick="copyCommand()" title="Klicken zum Kopieren">
            <span id="commandText"></span>
            <button class="cmd-copy">📋</button>
          </div>
          <div class="command-note">Ersetze &lt;SERVER_ID&gt; mit der Discord Server-ID.</div>
        </div>
      </div>
    </div>

  </div>
</div>

<div id="jsonModal" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.75);align-items:center;justify-content:center;padding:24px;">
  <div style="background:var(--bg-card);border:1px solid var(--border2);border-radius:var(--r-xl);padding:24px;max-width:700px;width:100%;max-height:80vh;overflow-y:auto;box-shadow:var(--shadow-xl);">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;">
      <span style="font-size:.65rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);">{ } JSON-Vorschau</span>
      <button onclick="closeJson()" style="background:none;border:none;color:var(--text3);cursor:pointer;font-size:1.1rem;">✕</button>
    </div>
    <textarea id="jsonPreview" readonly rows="20" style="width:100%;background:var(--bg-base);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.76rem;padding:12px;border-radius:var(--r-sm);resize:vertical;outline:none;"></textarea>
    <div style="margin-top:10px;display:flex;gap:8px;">
      <button class="btn btn-primary btn-sm" onclick="copyJson()">📋 Kopieren</button>
      <button class="btn btn-outline btn-sm" onclick="closeJson()">Schließen</button>
    </div>
  </div>
</div>

<script>
let _questions   = [];
let _viewerIds   = [];
let _currentFile = null;
let _guildRoles  = [];

const q   = id => document.getElementById(id);
const esc = s  => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, type='ok') {
  const icons = { ok:'✅', err:'❌', info:'ℹ️' };
  const t = document.createElement('div');
  t.className = `toast ${type}`;
  t.innerHTML = `<span>${icons[type]||''}</span><span>${esc(msg)}</span>`;
  q('toastContainer').appendChild(t);
  setTimeout(() => t.remove(), 4000);
}

// ── File list ──────────────────────────────────────────────────────────────
async function loadFiles() {
  const r = await fetch('/api/voting/files');
  const d = await r.json();
  const list = q('filesList');
  if (!d.files || !d.files.length) {
    list.innerHTML = '<div class="no-files">Noch keine Dateien.<br>Klicke "+ Neu" um zu beginnen.</div>';
    return;
  }
  list.innerHTML = d.files.map(f => `
    <div class="file-item ${_currentFile === f.filename ? 'active' : ''}" onclick="loadFile('${esc(f.filename)}')">
      <div class="file-item-info">
        <div class="file-item-name" title="${esc(f.filename)}">${esc(f.title || f.filename)}</div>
        <div class="file-item-meta">${esc(f.filename)} · ${f.error ? '⚠️' : (f.size/1024).toFixed(1)+'KB'}</div>
      </div>
      <button class="file-del-btn" onclick="event.stopPropagation();deleteFile('${esc(f.filename)}')">🗑️</button>
    </div>`).join('');
}

async function loadFile(filename) {
  const r = await fetch(`/api/voting/files/${encodeURIComponent(filename)}`);
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error||'?'), 'err'); return; }
  _currentFile = filename;
  populateForm(d.data);
  q('filenameInput').value = filename;
  hideCommandBox();
  loadFiles();
  toast(`${filename} geladen`, 'info');
}

async function deleteFile(filename) {
  if (!confirm(`"${filename}" wirklich löschen?`)) return;
  const r = await fetch(`/api/voting/files/${encodeURIComponent(filename)}`, { method: 'DELETE' });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error||'?'), 'err'); return; }
  if (_currentFile === filename) { _currentFile = null; newVoting(); }
  toast(`${filename} gelöscht`);
  loadFiles();
}

// ── Populate form from loaded JSON ─────────────────────────────────────────
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
  if (data.guild_id && q('guildServerIdInput')) {
    q('guildServerIdInput').value = data.guild_id;
    // Auto-load roles if we have a guild_id
    if (data.guild_id && _guildRoles.length === 0) loadGuildRoles();
  }
  _viewerIds = [];
  const raw = data.allowed_viewers || '';
  if (typeof raw === 'string' && raw.trim())
    _viewerIds = raw.split(',').map(x => x.trim()).filter(Boolean);
  else if (Array.isArray(raw))
    _viewerIds = raw;
  renderViewerChips();
  _questions = (data.Fragen || []).map(f => ({
    text:                f.Frage              || '',
    type:                f.Typ                || 'text',
    required:            f.Pflicht            !== false,
    multi:               !!f.Mehrfach,
    options:             Array.isArray(f.Optionen) ? [...f.Optionen] : (f.Optionen || ''),
    min:                 f.Min                || 1,
    max:                 f.Max                || 5,
    min_select:          f.min_select         || 1,
    max_select:          f.max_select         || 1,
    result_mode:         f.result_mode        || 'individual',
    membership_filter:   !!f.membership_filter,
    membership_min_days: f.membership_min_days || null,
    membership_max_days: f.membership_max_days || null,
    require_roles:       Array.isArray(f.require_roles)
        ? f.require_roles.map(r => typeof r === 'object' ? r : { id: String(r), name: String(r) })
        : [],
    exclude_roles:       Array.isArray(f.exclude_roles)
        ? f.exclude_roles.map(r => typeof r === 'object' ? r : { id: String(r), name: String(r) })
        : [],
    candidate_require_roles: Array.isArray(f.candidate_require_roles)
        ? f.candidate_require_roles.map(r => typeof r === 'object' ? r : { id: String(r), name: String(r) })
        : [],
    candidate_exclude_roles: Array.isArray(f.candidate_exclude_roles)
        ? f.candidate_exclude_roles.map(r => typeof r === 'object' ? r : { id: String(r), name: String(r) })
        : [],
    heading:             f.heading            || '',
    subtext:             f.subtext            || '',
  }));
  renderQuestions();
}

// ── Build JSON data from current form state ────────────────────────────────
function buildData() {
  // Sync text fields from DOM before building (in case user is mid-typing)
  _questions.forEach((qst, i) => {
    const hEl = document.getElementById(`q_heading_${i}`);
    const sEl = document.getElementById(`q_subtext_${i}`);
    const tEl = document.getElementById(`q_text_${i}`);
    if (hEl) qst.heading = hEl.value;
    if (sEl) qst.subtext = sEl.value;
    if (tEl) qst.text    = tEl.value;
    if (qst.type === 'choice') {
      const optEls = document.querySelectorAll(`.q_opt_input[data-qi="${i}"]`);
      if (optEls.length) qst.options = Array.from(optEls).map(el => el.value);
    }
  });

  const fragen = _questions.map(qst => {
    const obj = { Frage: qst.text, Typ: qst.type, Pflicht: qst.required };
    if (qst.heading) obj.heading = qst.heading;
    if (qst.subtext) obj.subtext = qst.subtext;
    // Voter role gates (all question types)
    if (qst.require_roles && qst.require_roles.length) obj.require_roles = qst.require_roles;
    if (qst.exclude_roles && qst.exclude_roles.length) obj.exclude_roles = qst.exclude_roles;
    if (qst.type === 'choice') {
      obj.Mehrfach = qst.multi;
      obj.Optionen = Array.isArray(qst.options) ? qst.options : [];
    }
    if (qst.type === 'person') {
      obj.Mehrfach    = qst.multi;
      obj.Optionen    = typeof qst.options === 'string' ? qst.options : '--All';
      obj.min_select  = qst.min_select  || 1;
      obj.max_select  = qst.max_select  || 1;
      obj.result_mode = qst.result_mode || 'individual';
      if (qst.membership_filter) {
        obj.membership_filter = true;
        if (qst.membership_min_days) obj.membership_min_days = parseInt(qst.membership_min_days);
        if (qst.membership_max_days) obj.membership_max_days = parseInt(qst.membership_max_days);
      }
      // For person questions: separate candidate filter from voter gate
      if (qst.candidate_require_roles && qst.candidate_require_roles.length) obj.candidate_require_roles = qst.candidate_require_roles;
      if (qst.candidate_exclude_roles && qst.candidate_exclude_roles.length) obj.candidate_exclude_roles = qst.candidate_exclude_roles;
    }
    if (qst.type === 'rating') { obj.Min = qst.min; obj.Max = qst.max; }
    return obj;
  });

  const guildId = q('guildServerIdInput')?.value.trim() || '';
  const data = {
    Kategorie:    q('fKategorie').value.trim(),
    Beschreibung: q('fBeschreibung').value.trim(),
    Zur_Auswahl:  q('fZurAuswahl').value.trim(),
    Fragen:       fragen,
  };
  if (guildId) data.guild_id = guildId;
  if (q('fEncEnabled').checked && q('fPublicKey').value.trim())
    data.RSA_Public_Key = q('fPublicKey').value.trim();
  if (_viewerIds.length > 0)
    data.allowed_viewers = _viewerIds.join(',');
  return data;
}

// ── Reset form ─────────────────────────────────────────────────────────────
function newVoting() {
  _currentFile = null; _questions = []; _viewerIds = [];
  q('fKategorie').value = ''; q('fBeschreibung').value = '';
  q('fZurAuswahl').value = '--All';
  q('fEncEnabled').checked = false;
  q('encSection').style.display = 'none';
  q('fPublicKey').value = '';
  q('filenameInput').value = '';
  renderViewerChips(); renderQuestions(); hideCommandBox(); hideValidation(); loadFiles();
}

// ── Save ───────────────────────────────────────────────────────────────────
async function saveFile() {
  const data = buildData();
  let filename = q('filenameInput').value.trim() || data.Kategorie || 'abstimmung';
  if (!filename.endsWith('.json')) filename += '.json';
  hideValidation();
  const vr = await fetch('/api/voting/validate', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
  });
  const vd = await vr.json();
  if (!vd.valid) { showValidation(vd.errors); return; }
  const r = await fetch('/api/voting/files', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({ filename, data })
  });
  const d = await r.json();
  if (!r.ok) { toast('Fehler: ' + (d.error||'?'), 'err'); return; }
  _currentFile = d.filename;
  q('filenameInput').value = d.filename;
  toast(`✓ ${d.filename} gespeichert`);
  showCommandBox(d.path);
  loadFiles();
}

function showCommandBox(path) {
  q('commandText').textContent = `/abstimmung erstellen json_pfad:${path} server_id:<SERVER_ID>`;
  q('commandBox').classList.add('show');
}
function hideCommandBox() { q('commandBox').classList.remove('show'); }
function copyCommand() {
  navigator.clipboard.writeText(q('commandText').textContent).then(() => toast('Command kopiert!'));
}

// ── Validate ───────────────────────────────────────────────────────────────
async function validateCurrent() {
  const data = buildData();
  const r = await fetch('/api/voting/validate', {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
  });
  const d = await r.json();
  if (d.valid) { hideValidation(); toast('✓ Konfiguration gültig!'); }
  else { showValidation(d.errors); toast(`${d.errors.length} Fehler`, 'err'); }
}
function showValidation(errors) {
  q('valErrorList').innerHTML = errors.map(e => `<li>${esc(e)}</li>`).join('');
  q('valError').classList.add('show');
}
function hideValidation() { q('valError').classList.remove('show'); }
function toggleEnc(on) { q('encSection').style.display = on ? '' : 'none'; }

// ── Viewer IDs ─────────────────────────────────────────────────────────────
function addViewer() {
  const inp = q('viewerIdInput'), val = inp.value.trim();
  if (!val || !/^\d{15,20}$/.test(val)) { toast('Ungültige Discord User-ID', 'err'); return; }
  if (_viewerIds.includes(val)) { toast('Bereits hinzugefügt', 'info'); return; }
  _viewerIds.push(val); inp.value = ''; renderViewerChips();
}
function removeViewer(uid) {
  _viewerIds = _viewerIds.filter(x => x !== uid); renderViewerChips();
}
function renderViewerChips() {
  q('viewerChips').innerHTML = _viewerIds.map(uid =>
    `<span class="viewer-chip">${esc(uid)}<button class="viewer-chip-del" onclick="removeViewer('${uid}')">✕</button></span>`
  ).join('') || '<span style="font-size:.72rem;color:var(--text3);">Keine – nur MBL sieht Voter-Log</span>';
}

// ── Guild roles ────────────────────────────────────────────────────────────
async function loadGuildRoles() {
  const sid = q('guildServerIdInput')?.value.trim();
  if (!sid) { toast('Bitte Server-ID eingeben', 'info'); return; }
  try {
    const r = await fetch(`/api/voting/roles/${sid}`);
    const d = await r.json();
    if (d.roles) {
      _guildRoles = d.roles;
      toast(`${_guildRoles.length} Rollen geladen`, 'ok');
      renderQuestions();
    } else { toast('Keine Rollen gefunden', 'info'); }
  } catch(e) { toast('Fehler beim Laden der Rollen', 'err'); }
}

// ── Add / Remove / Move questions ──────────────────────────────────────────
function addQuestion(type) {
  const BASE = { heading:'', subtext:'', require_roles:[], exclude_roles:[], candidate_require_roles:[], candidate_exclude_roles:[] };
  const defaults = {
    text:   { ...BASE, text:'', type:'text',   required:true,  multi:false, options:[],                      min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null },
    choice: { ...BASE, text:'', type:'choice', required:true,  multi:false, options:['Option 1','Option 2'], min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null },
    rating: { ...BASE, text:'', type:'rating', required:true,  multi:false, options:[],                      min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null },
    person: { ...BASE, text:'', type:'person', required:false, multi:false, options:'--All',                 min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null },
  };
  _questions.push({...defaults[type]});
  renderQuestions();
  // Scroll to new question
  setTimeout(() => {
    const cards = document.querySelectorAll('.question-card');
    if (cards.length) cards[cards.length-1].scrollIntoView({ behavior:'smooth', block:'start' });
  }, 80);
}

function removeQuestion(idx) {
  _questions.splice(idx, 1); renderQuestions();
}
function moveQuestion(idx, dir) {
  const to = idx + dir;
  if (to < 0 || to >= _questions.length) return;
  [_questions[idx], _questions[to]] = [_questions[to], _questions[idx]];
  renderQuestions();
}

// ── Change question type (preserves text, heading, subtext, roles) ─────────
function changeType(idx, type) {
  const BASE2 = { require_roles:[], exclude_roles:[], candidate_require_roles:[], candidate_exclude_roles:[] };
  const typeDefaults = {
    text:   {...BASE2, multi:false, options:[],                      min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null},
    choice: {...BASE2, multi:false, options:['Option 1','Option 2'], min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null},
    rating: {...BASE2, multi:false, options:[],                      min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null},
    person: {...BASE2, multi:false, options:'--All',                 min:1, max:5, min_select:1, max_select:1, result_mode:'individual', membership_filter:false, membership_min_days:null, membership_max_days:null},
  };
  const old = _questions[idx];
  // Sync current text values before switching type
  const hEl = document.getElementById(`q_heading_${idx}`);
  const sEl = document.getElementById(`q_subtext_${idx}`);
  const tEl = document.getElementById(`q_text_${idx}`);
  _questions[idx] = {
    ...typeDefaults[type],
    type,
    text:          tEl ? tEl.value : old.text,
    required:      old.required,
    heading:       hEl ? hEl.value : old.heading,
    subtext:       sEl ? sEl.value : old.subtext,
    require_roles: old.require_roles || [],
    exclude_roles: old.exclude_roles || [],
  };
  renderQuestions();
}

// ── Options (choice) ───────────────────────────────────────────────────────
function addOption(qidx) {
  // Sync current options from DOM first
  const optEls = document.querySelectorAll(`.q_opt_input[data-qi="${qidx}"]`);
  if (optEls.length) _questions[qidx].options = Array.from(optEls).map(el => el.value);
  if (!Array.isArray(_questions[qidx].options)) _questions[qidx].options = [];
  _questions[qidx].options.push(`Option ${_questions[qidx].options.length + 1}`);
  renderQuestions();
}
function removeOption(qidx, oidx) {
  // Sync before removing
  const optEls = document.querySelectorAll(`.q_opt_input[data-qi="${qidx}"]`);
  if (optEls.length) _questions[qidx].options = Array.from(optEls).map(el => el.value);
  _questions[qidx].options.splice(oidx, 1);
  renderQuestions();
}

// ── Person toggles ─────────────────────────────────────────────────────────
function togglePersonAll(qidx, on) {
  _questions[qidx].options = on ? '--All' : [];
  const sec = document.getElementById(`personOptsSection_${qidx}`);
  if (sec) sec.style.display = on ? 'none' : '';
}
function togglePersonMulti(qidx, on) {
  _questions[qidx].multi = on;
  const sec = document.getElementById(`minMaxSection_${qidx}`);
  if (sec) sec.style.display = on ? '' : 'none';
  if (!on) { _questions[qidx].min_select = 1; _questions[qidx].max_select = 1; }
}
function toggleMembershipFilter(qidx, on) {
  _questions[qidx].membership_filter = on;
  const sec = document.getElementById(`membershipFilterSection_${qidx}`);
  if (sec) sec.style.display = on ? '' : 'none';
}
function setResultMode(qidx, mode) {
  _questions[qidx].result_mode = mode;
  // Update buttons only (no full re-render)
  const parent = document.getElementById(`resultModeRow_${qidx}`);
  if (parent) {
    parent.querySelectorAll('.result-mode-btn').forEach(btn => {
      btn.classList.toggle('active', btn.dataset.mode === mode);
    });
  }
  // Update hint text
  const hint = document.getElementById(`resultModeHint_${qidx}`);
  if (hint) hint.textContent = mode === 'group'
    ? 'Bsp: "User A + User B" = 1 Stimme für diese Gruppe, nicht 1 Stimme pro Person.'
    : 'Bsp: Wählt jemand User A + B, bekommt jeder 1 Stimme einzeln.';
}

// ── Role filter helpers ────────────────────────────────────────────────────
function addRoleFilter(qidx, kind, selId) {
  const sel = document.getElementById(selId);
  if (!sel || !sel.value) return;
  const rid   = sel.value;
  const opt   = sel.options[sel.selectedIndex];
  const rname = opt ? opt.textContent.trim() : rid;
  const arr   = kind === 'require'
    ? (_questions[qidx].require_roles || [])
    : (_questions[qidx].exclude_roles || []);
  if (arr.find(r => r.id === rid)) return;
  arr.push({ id: rid, name: rname });
  if (kind === 'require') _questions[qidx].require_roles = arr;
  else                    _questions[qidx].exclude_roles = arr;
  sel.value = '';
  // Re-render only the chips div, not the full card
  const chipsDiv = document.getElementById(`roleChips_${kind}_${qidx}`);
  if (chipsDiv) chipsDiv.innerHTML = renderRoleChips(arr, kind, qidx);
}
function removeRoleFilter(qidx, kind, rid) {
  if (kind === 'require')
    _questions[qidx].require_roles = (_questions[qidx].require_roles||[]).filter(r => r.id !== rid);
  else
    _questions[qidx].exclude_roles = (_questions[qidx].exclude_roles||[]).filter(r => r.id !== rid);
  const arr = kind === 'require' ? _questions[qidx].require_roles : _questions[qidx].exclude_roles;
  const chipsDiv = document.getElementById(`roleChips_${kind}_${qidx}`);
  if (chipsDiv) chipsDiv.innerHTML = renderRoleChips(arr, kind, qidx);
}
function renderRoleChips(arr, kind, qidx) {
  // kind: 'require'/'exclude' → voter gate (removeRoleFilter)
  //       'crequire'/'cexclude' → candidate filter (removeCandidateRoleFilter)
  const isCandidate = kind === 'crequire' || kind === 'cexclude';
  const cssKind     = (kind === 'require' || kind === 'crequire') ? 'require' : 'exclude';
  return (arr||[]).map(r => {
    const removeFn = isCandidate
      ? `removeCandidateRoleFilter(${qidx},'${kind}','${r.id}')`
      : `removeRoleFilter(${qidx},'${kind}','${r.id}')`;
    return `<span class="role-chip role-chip-${cssKind}">${esc(r.name||r.id)}<button class="role-chip-del" onclick="${removeFn}">✕</button></span>`;
  }).join('');
}

// ── Render all questions ───────────────────────────────────────────────────
const TYPE_LABELS = { text:'Freitext', choice:'Auswahl', rating:'Bewertung', person:'Person' };
const TYPE_ICONS  = { text:'📝', choice:'☑️', rating:'⭐', person:'👤' };

function renderQuestions() {
  const container = q('questionsList');
  if (!_questions.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text3);padding:20px;font-size:.83rem;">Noch keine Fragen. Klicke unten auf einen Fragetyp.</div>';
    return;
  }
  container.innerHTML = _questions.map((qst, i) => renderQuestion(qst, i)).join('');
}

function renderQuestion(qst, i) {
  const total    = _questions.length;
  const typeOpts = ['text','choice','rating','person'].map(t =>
    `<option value="${t}" ${qst.type===t?'selected':''}>${TYPE_ICONS[t]} ${TYPE_LABELS[t]}</option>`
  ).join('');

  // ── Type-specific extra content ──────────────────────────────────────────
  let extra = '';

  if (qst.type === 'choice') {
    const opts = Array.isArray(qst.options) ? qst.options : [];
    extra = `
      <div class="options-editor">
        ${opts.map((o, oi) => `
          <div class="option-row">
            <input type="text" class="q_opt_input" data-qi="${i}" data-oi="${oi}"
              value="${esc(o)}" placeholder="Option ${oi+1}"
              onblur="_questions[${i}].options[${oi}]=this.value">
            <button class="option-del" onclick="removeOption(${i},${oi})">✕</button>
          </div>`).join('')}
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
          <input type="number" value="${qst.min}" min="0" max="100"
            style="width:70px;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:6px 8px;border-radius:var(--r-sm);outline:none;"
            onchange="_questions[${i}].min=+this.value">
        </div>
        <div>
          <div style="font-size:.65rem;color:var(--text3);margin-bottom:4px;">MAX</div>
          <input type="number" value="${qst.max}" min="1" max="100"
            style="width:70px;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:6px 8px;border-radius:var(--r-sm);outline:none;"
            onchange="_questions[${i}].max=+this.value">
        </div>
      </div>`;

  } else if (qst.type === 'person') {
    const isAll      = typeof qst.options === 'string' && qst.options.includes('All');
    const isGroup    = qst.result_mode === 'group';
    const memOn      = qst.membership_filter;
    const roleOpts   = (_guildRoles||[]).map(r =>
      `<option value="${r.id}">${esc(r.name)}</option>`).join('');

    extra = `
      <div class="person-extra">
        <div class="person-extra-title">👤 Person-Frage Einstellungen</div>

        <div style="margin-bottom:12px;">
          <div style="font-size:.68rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Kandidaten</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <label class="toggle-switch">
              <input type="checkbox" ${isAll?'checked':''} onchange="togglePersonAll(${i},this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span style="font-size:.83rem;color:var(--sub);">Alle Server-Mitglieder</span>
          </div>
          <div id="personOptsSection_${i}" style="${isAll?'display:none':''}">
            <div style="font-size:.68rem;color:var(--text3);margin-bottom:6px;">User-IDs (komma-getrennt)</div>
            <input type="text" placeholder="123456789,987654321,..."
              style="width:100%;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.75rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;"
              value="${isAll ? '' : esc(Array.isArray(qst.options) ? qst.options.join(',') : qst.options)}"
              onblur="_questions[${i}].options=this.value.split(',').map(x=>x.trim()).filter(Boolean)">
          </div>
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:.68rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Auswahlregeln</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
            <label class="toggle-switch">
              <input type="checkbox" ${qst.multi?'checked':''} onchange="togglePersonMulti(${i},this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span style="font-size:.83rem;color:var(--sub);">Mehrfachauswahl erlauben</span>
          </div>
          <div id="minMaxSection_${i}" style="${qst.multi?'':'display:none'}">
            <div class="fe-row" style="gap:10px;">
              <div class="fe" style="margin-bottom:0;">
                <label>Mindestauswahl</label>
                <input type="number" value="${qst.min_select||1}" min="1" max="100"
                  style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;width:100%;"
                  onchange="_questions[${i}].min_select=Math.max(1,+this.value)">
              </div>
              <div class="fe" style="margin-bottom:0;">
                <label>Maximalauswahl</label>
                <input type="number" value="${qst.max_select||1}" min="1" max="100"
                  style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;width:100%;"
                  onchange="_questions[${i}].max_select=Math.max(1,+this.value)">
              </div>
            </div>
          </div>
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:.68rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Kandidaten Rollen-Filter</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap;">
            <div style="flex:1;min-width:160px;">
              <div style="font-size:.65rem;color:#4ade80;font-weight:600;margin-bottom:4px;">✅ Kandidat muss Rolle haben</div>
              <div id="candChips_require_${i}" style="min-height:22px;margin-bottom:4px;">${renderRoleChips(qst.candidate_require_roles||[],'crequire',i)}</div>
              <div style="display:flex;gap:6px;">
                <select id="candReqSel_${i}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.76rem;padding:5px 8px;border-radius:var(--r-sm);outline:none;">
                  <option value="">-- Rolle --</option>${roleOpts}
                </select>
                <button class="btn btn-ghost btn-sm" onclick="addCandidateRoleFilter(${i},'require','candReqSel_${i}')">+</button>
              </div>
            </div>
            <div style="flex:1;min-width:160px;">
              <div style="font-size:.65rem;color:#f87171;font-weight:600;margin-bottom:4px;">🚫 Kandidat darf Rolle NICHT haben</div>
              <div id="candChips_exclude_${i}" style="min-height:22px;margin-bottom:4px;">${renderRoleChips(qst.candidate_exclude_roles||[],'cexclude',i)}</div>
              <div style="display:flex;gap:6px;">
                <select id="candExclSel_${i}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.76rem;padding:5px 8px;border-radius:var(--r-sm);outline:none;">
                  <option value="">-- Rolle --</option>${roleOpts}
                </select>
                <button class="btn btn-ghost btn-sm" onclick="addCandidateRoleFilter(${i},'exclude','candExclSel_${i}')">+</button>
              </div>
            </div>
          </div>
          <div style="font-size:.67rem;color:var(--text3);margin-top:6px;">Filtert welche Personen als Kandidaten angezeigt werden.</div>
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:.68rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Mitgliedschaftsfilter</div>
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
            <label class="toggle-switch">
              <input type="checkbox" ${memOn?'checked':''} onchange="toggleMembershipFilter(${i},this.checked)">
              <span class="toggle-slider"></span>
            </label>
            <span style="font-size:.83rem;color:var(--sub);">Nach Serverzugehörigkeit filtern</span>
          </div>
          <div id="membershipFilterSection_${i}" style="${memOn?'':'display:none'}">
            <div class="membership-filter-section">
              <div class="fe-row" style="gap:10px;">
                <div class="fe" style="margin-bottom:0;">
                  <label>Mindest-Tage</label>
                  <input type="number" value="${qst.membership_min_days||''}" min="0" placeholder="z.B. 30"
                    style="background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;width:100%;"
                    onchange="_questions[${i}].membership_min_days=this.value?+this.value:null">
                </div>
                <div class="fe" style="margin-bottom:0;">
                  <label>Maximal-Tage</label>
                  <input type="number" value="${qst.membership_max_days||''}" min="0" placeholder="z.B. 365"
                    style="background:var(--bg-surface);border:1px solid var(--border);color:var(--text);font-size:.85rem;padding:7px 10px;border-radius:var(--r-sm);outline:none;width:100%;"
                    onchange="_questions[${i}].membership_max_days=this.value?+this.value:null">
                </div>
              </div>
              <div style="font-size:.68rem;color:var(--text3);margin-top:6px;">Leer = kein Limit. Eingabe in Tagen.</div>
            </div>
          </div>
        </div>

        <div>
          <div style="font-size:.68rem;color:var(--text3);font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;">Ergebnis-Auswertung</div>
          <div class="result-mode-row" id="resultModeRow_${i}">
            <button class="result-mode-btn ${!isGroup?'active':''}" data-mode="individual" onclick="setResultMode(${i},'individual')">
              👤 Einzel<br><span style="font-size:.63rem;font-weight:400;opacity:.7;">Jede Person zählt separat</span>
            </button>
            <button class="result-mode-btn ${isGroup?'active':''}" data-mode="group" onclick="setResultMode(${i},'group')">
              👥 Gruppe<br><span style="font-size:.63rem;font-weight:400;opacity:.7;">Kombination als Einheit</span>
            </button>
          </div>
          <div id="resultModeHint_${i}" style="font-size:.68rem;color:var(--text3);margin-top:6px;">
            ${isGroup
              ? 'Bsp: "User A + User B" = 1 Stimme für diese Gruppe.'
              : 'Bsp: Wählt jemand A + B, bekommt jeder 1 Stimme einzeln.'}
          </div>
        </div>
      </div>`;
  }

  // ── Role filter for voters (all question types) ─────────────────────────
  const voterRoleOpts = (_guildRoles||[]).map(r =>
    `<option value="${r.id}">${esc(r.name)}</option>`).join('');

  const voterRoleSection = `
    <div class="role-filter-section" style="margin-bottom:12px;">
      <div style="font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:6px;">🎭 Wer darf abstimmen (Rollen-Gate)</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
        <div style="flex:1;min-width:160px;">
          <div style="font-size:.65rem;color:#4ade80;font-weight:600;margin-bottom:4px;">✅ Muss eine dieser Rollen haben</div>
          <div id="roleChips_require_v_${i}" style="min-height:22px;margin-bottom:4px;">${renderRoleChips(qst.require_roles||[],'require',i)}</div>
          <div style="display:flex;gap:6px;">
            <select id="reqRoleVSel_${i}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.76rem;padding:5px 8px;border-radius:var(--r-sm);outline:none;">
              <option value="">-- Rolle --</option>${voterRoleOpts}
            </select>
            <button class="btn btn-ghost btn-sm" onclick="addVoterRoleFilter(${i},'require','reqRoleVSel_${i}')">+</button>
          </div>
        </div>
        <div style="flex:1;min-width:160px;">
          <div style="font-size:.65rem;color:#f87171;font-weight:600;margin-bottom:4px;">🚫 Darf diese Rollen NICHT haben</div>
          <div id="roleChips_exclude_v_${i}" style="min-height:22px;margin-bottom:4px;">${renderRoleChips(qst.exclude_roles||[],'exclude',i)}</div>
          <div style="display:flex;gap:6px;">
            <select id="exclRoleVSel_${i}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);color:var(--text);font-size:.76rem;padding:5px 8px;border-radius:var(--r-sm);outline:none;">
              <option value="">-- Rolle --</option>${voterRoleOpts}
            </select>
            <button class="btn btn-ghost btn-sm" onclick="addVoterRoleFilter(${i},'exclude','exclRoleVSel_${i}')">+</button>
          </div>
        </div>
      </div>
      <div style="font-size:.67rem;color:var(--text3);">Leer = keine Einschränkung. Diese Frage wird für Nutzer ohne passende Rolle ausgeblendet.</div>
    </div>`;

  // Show voter role section for all question types
  const roleSection = voterRoleSection;

  return `<div class="question-card" id="qcard_editor_${i}">
    <div class="question-card-hd">
      <div class="q-num">${i+1}</div>
      <select style="background:var(--bg-card);border:1px solid var(--border2);color:var(--text3);padding:3px 8px;border-radius:20px;font-size:.64rem;font-weight:600;text-transform:uppercase;letter-spacing:.06em;outline:none;cursor:pointer;"
        onchange="changeType(${i},this.value)">${typeOpts}</select>
      <div style="display:flex;align-items:center;gap:4px;margin-left:auto;">
        <button class="q-move" onclick="moveQuestion(${i},-1)" title="Nach oben" ${i===0?'disabled':''}>↑</button>
        <button class="q-move" onclick="moveQuestion(${i},1)" title="Nach unten" ${i===total-1?'disabled':''}>↓</button>
        <button class="q-del" onclick="removeQuestion(${i})">✕</button>
      </div>
    </div>

    <div class="fe-row" style="margin-bottom:10px;">
      <div class="fe" style="margin-bottom:0;">
        <label>Überschrift (optional)</label>
        <input type="text" id="q_heading_${i}" value="${esc(qst.heading||'')}"
          placeholder="z.B. Wichtige Entscheidung"
          onblur="_questions[${i}].heading=this.value">
      </div>
      <div class="fe" style="margin-bottom:0;">
        <label>Beschreibung / Hinweis (optional)</label>
        <input type="text" id="q_subtext_${i}" value="${esc(qst.subtext||'')}"
          placeholder="Ergänzende Info zur Frage..."
          onblur="_questions[${i}].subtext=this.value">
      </div>
    </div>

    <div class="fe" style="margin-bottom:8px;">
      <label>Frage ${i+1} <span style="color:var(--red)">*</span></label>
      <input type="text" id="q_text_${i}" value="${esc(qst.text)}"
        placeholder="Deine Frage..."
        onblur="_questions[${i}].text=this.value">
    </div>

    <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px;">
      <label class="toggle-switch">
        <input type="checkbox" ${qst.required?'checked':''} onchange="_questions[${i}].required=this.checked">
        <span class="toggle-slider"></span>
      </label>
      <span style="font-size:.8rem;color:var(--sub);">Pflichtfrage</span>
    </div>

    ${roleSection}
    ${extra}
  </div>`;
}

// ── Voter role filter (for non-person question types) ──────────────────────
function addVoterRoleFilter(qidx, kind, selId) {
  const sel = document.getElementById(selId);
  if (!sel || !sel.value) return;
  const rid   = sel.value;
  const opt   = sel.options[sel.selectedIndex];
  const rname = opt ? opt.textContent.trim() : rid;
  const arr   = kind === 'require'
    ? (_questions[qidx].require_roles || [])
    : (_questions[qidx].exclude_roles || []);
  if (arr.find(r => r.id === rid)) return;
  arr.push({ id: rid, name: rname });
  if (kind === 'require') _questions[qidx].require_roles = arr;
  else                    _questions[qidx].exclude_roles = arr;
  sel.value = '';
  // Update the voter-chips div (note: _v_ suffix for non-person questions)
  const chipsDiv = document.getElementById(`roleChips_${kind}_v_${qidx}`);
  if (chipsDiv) chipsDiv.innerHTML = renderRoleChips(arr, kind, qidx);
  // Also try without _v_ suffix in case of person type
  const chipsDiv2 = document.getElementById(`roleChips_${kind}_${qidx}`);
  if (chipsDiv2) chipsDiv2.innerHTML = renderRoleChips(arr, kind, qidx);
}

// ── Candidate role filter (for person question type) ───────────────────────
function addCandidateRoleFilter(qidx, kind, selId) {
  const sel = document.getElementById(selId);
  if (!sel || !sel.value) return;
  const rid   = sel.value;
  const opt   = sel.options[sel.selectedIndex];
  const rname = opt ? opt.textContent.trim() : rid;
  const field = kind === 'require' ? 'candidate_require_roles' : 'candidate_exclude_roles';
  const chipKind = kind === 'require' ? 'crequire' : 'cexclude';
  if (!_questions[qidx][field]) _questions[qidx][field] = [];
  if (_questions[qidx][field].find(r => r.id === rid)) return;
  _questions[qidx][field].push({ id: rid, name: rname });
  sel.value = '';
  const divId = kind === 'require' ? `candChips_require_${qidx}` : `candChips_exclude_${qidx}`;
  const chipsDiv = document.getElementById(divId);
  if (chipsDiv) chipsDiv.innerHTML = renderRoleChips(_questions[qidx][field], chipKind, qidx);
}

function removeCandidateRoleFilter(qidx, kind, rid) {
  const field    = kind === 'crequire' ? 'candidate_require_roles' : 'candidate_exclude_roles';
  const divId    = kind === 'crequire' ? `candChips_require_${qidx}` : `candChips_exclude_${qidx}`;
  _questions[qidx][field] = (_questions[qidx][field]||[]).filter(r => r.id !== rid);
  const chipsDiv = document.getElementById(divId);
  if (chipsDiv) chipsDiv.innerHTML = renderRoleChips(_questions[qidx][field], kind, qidx);
}

// ── JSON preview ───────────────────────────────────────────────────────────
function showJson() {
  q('jsonPreview').value = JSON.stringify(buildData(), null, 2);
  q('jsonModal').style.display = 'flex';
}
function closeJson() { q('jsonModal').style.display = 'none'; }
function copyJson() {
  navigator.clipboard.writeText(q('jsonPreview').value).then(() => toast('JSON kopiert!'));
}

loadFiles();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# VOTE TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

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
.bg-layer { position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background: radial-gradient(ellipse 80% 60% at 10% -10%, rgba(91,140,255,0.07) 0%, transparent 60%),
              radial-gradient(ellipse 60% 40% at 90% 100%, rgba(74,222,128,0.04) 0%, transparent 60%); }
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
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(1.3)} }
.deadline-bar { display:inline-flex;align-items:center;gap:8px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:10px;padding:6px 14px;font-size:.78rem;font-weight:600;color:var(--gold);margin-bottom:18px; }
.deadline-bar.urgent { background:rgba(248,113,113,.08);border-color:rgba(248,113,113,.25);color:var(--red); }
.deadline-countdown { font-family:'DM Mono',monospace; }
.vote-title { font-size: clamp(1.6rem, 4vw, 2.4rem); font-weight: 700; letter-spacing: -.5px; margin-bottom: 10px; }
.vote-desc { color: var(--sub); font-size: .92rem; max-width: 480px; margin: 0 auto; }
.progress-bar { background: var(--border); border-radius: 4px; height: 3px; margin-bottom: 40px; overflow: hidden; }
.progress-fill { height: 100%; background: var(--accent); border-radius: 4px; transition: width .4s ease; }
.progress-text { text-align: right; font-size: .72rem; color: var(--sub); margin-top: 6px; font-family: 'DM Mono', monospace; }
.question-card { background: var(--card); border: 1px solid var(--border); border-radius: var(--r); padding: 28px; margin-bottom: 16px; transition: border-color .2s; }
.question-card.active { border-color: var(--accent); }
.question-card.answered { border-color: rgba(74,222,128,.3); }
.q-number { font-size: .65rem; font-weight: 700; text-transform: uppercase; letter-spacing: .1em; color: var(--dim); margin-bottom: 6px; font-family: 'DM Mono', monospace; }
.q-text { font-size: 1rem; font-weight: 500; margin-bottom: 6px; line-height: 1.5; }
.q-required { color: var(--red); margin-left: 3px; }
.q-hint { font-size: .75rem; color: var(--sub); margin-bottom: 16px; font-style: italic; }
.q-select-hint { font-size: .72rem; background: rgba(91,140,255,.1); border: 1px solid rgba(91,140,255,.2); border-radius: 6px; padding: 4px 10px; display: inline-block; margin-bottom: 14px; color: var(--accent); }
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
.opt-label { font-size: .9rem; flex: 1; }
.person-search { width: 100%; background: var(--card2); border: 1px solid var(--border2); color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .88rem; padding: 10px 14px; border-radius: var(--r2); outline: none; margin-bottom: 10px; transition: border-color .15s; }
.person-search:focus { border-color: var(--accent); }
.person-list { max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
.person-item { display: flex; align-items: center; gap: 10px; background: var(--card2); border: 1px solid var(--border2); border-radius: var(--r2); padding: 9px 14px; cursor: pointer; transition: all .15s; }
.person-item:hover { border-color: var(--accent); }
.person-item.selected { border-color: var(--accent); background: rgba(91,140,255,.1); }
.person-item.selected .person-check { display: flex; }
.person-avatar { width: 28px; height: 28px; border-radius: 50%; object-fit: cover; background: var(--dim); flex-shrink: 0; font-size: .65rem; display: flex; align-items: center; justify-content: center; color: var(--text); font-weight: 600; }
.person-name { font-size: .88rem; flex: 1; }
.person-check { display: none; width: 18px; height: 18px; border-radius: 4px; background: var(--accent); align-items: center; justify-content: center; font-size: .7rem; color: var(--bg); font-weight: 700; flex-shrink: 0; }
.person-membership { font-size: .66rem; color: var(--dim); font-family: 'DM Mono', monospace; }
.rating-row { display: flex; gap: 8px; flex-wrap: wrap; }
.rating-btn { width: 44px; height: 44px; border-radius: var(--r2); background: var(--card2); border: 1px solid var(--border2); color: var(--sub); font-size: .9rem; font-weight: 600; cursor: pointer; transition: all .15s; display: flex; align-items: center; justify-content: center; }
.rating-btn:hover { border-color: var(--accent); color: var(--accent); }
.rating-btn.selected { background: var(--accent); border-color: var(--accent); color: var(--bg); }
.text-input { width: 100%; background: var(--card2); border: 1px solid var(--border2); color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .9rem; padding: 12px 14px; border-radius: var(--r2); outline: none; resize: vertical; min-height: 100px; transition: border-color .15s; }
.text-input:focus { border-color: var(--accent); }
.selection-count { font-size: .72rem; font-family: 'DM Mono', monospace; margin-top: 8px; padding: 4px 10px; border-radius: 6px; display: inline-block; }
.selection-count.ok   { background: rgba(74,222,128,.1); color: var(--green); border: 1px solid rgba(74,222,128,.2); }
.selection-count.warn { background: rgba(248,113,113,.1); color: var(--red); border: 1px solid rgba(248,113,113,.2); }
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
    {% if ends_at %}
    <div id="timerBar" style="margin-top:20px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.2);border-radius:10px;padding:10px 18px;display:inline-flex;align-items:center;gap:10px;font-size:.83rem;">
      <span style="font-size:1rem;">⏱️</span>
      <span style="color:#fbbf24;font-weight:600;">Noch </span>
      <span id="voteCountdown" style="font-family:'DM Mono',monospace;color:#fbbf24;font-weight:700;font-size:.95rem;">...</span>
      <span style="color:var(--sub);">verbleibend</span>
    </div>
    {% endif %}
  </div>
  <div class="progress-bar"><div class="progress-fill" id="progressFill" style="width:0%"></div></div>
  <div class="progress-text" id="progressText">0 / {{ voting.questions | length }} beantwortet</div>

  <form id="voteForm">
  {% for i, frage in enumerate(voting.questions) %}
  <div class="question-card" id="qcard-{{ i }}" data-index="{{ i }}">
    <div class="q-number">Frage {{ i + 1 }} von {{ voting.questions | length }}</div>
    {% if frage.get('heading') %}<div class="q-heading">{{ frage.heading }}</div>{% endif %}
    <div class="q-text">{{ frage.Frage }}{% if frage.get('Pflicht', False) %}<span class="q-required">*</span>{% endif %}</div>
    {% if frage.get('subtext') %}<div class="q-subtext">{{ frage.subtext }}</div>{% endif %}

    {% if frage.Typ == 'person' %}
      {% set min_sel = frage.get('min_select', 1) %}
      {% set max_sel = frage.get('max_select', 1) %}
      {% set is_multi = frage.get('Mehrfach', False) or max_sel > 1 %}
      {% if is_multi %}
        <div class="q-select-hint">
          {% if min_sel == max_sel %}
            Wähle genau {{ min_sel }} Person{% if min_sel != 1 %}en{% endif %}
          {% elif min_sel > 1 %}
            Wähle {{ min_sel }}–{{ max_sel }} Personen
          {% else %}
            Wähle bis zu {{ max_sel }} Person{% if max_sel != 1 %}en{% endif %}
          {% endif %}
        </div>
      {% endif %}
    {% endif %}

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
    <div class="person-search-wrap">
      <span class="search-icon">🔍</span>
      <input type="text" class="person-search" id="psearch-{{ i }}" placeholder="Name eingeben zum Suchen..."
        oninput="filterPersons({{ i }}, this.value)">
    </div>
    <div class="person-list" id="plist-{{ i }}">
      <div class="person-list-empty">Tippe einen Namen ein um Personen zu suchen.</div>
    </div>
    <div id="pselected-{{ i }}" style="margin-top:8px;"></div>
    <div class="selection-count" id="selcount-{{ i }}" style="display:none;"></div>
    {% endif %}
  </div>
  {% endfor %}
  </form>

  <div class="submit-section">
    <button class="submit-btn" id="submitBtn" onclick="submitVote()">Abstimmung abschicken →</button>
    <div class="anon-note">🔒 Deine Antwort ist vollständig anonym</div>
  </div>
</div>

<script>
const VOTING_ID  = {{ voting_id | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
const PUBLIC_KEY = {{ (voting.public_key or '') | tojson }};
const HAS_PERSON = QUESTIONS.some(q => q.Typ === 'person');
const answers    = {};

// ── Choice toggleOpt – fixed deselect ─────────────────────────────────────
function toggleOpt(el) {
  const qi    = el.dataset.q;
  const val   = el.dataset.val;
  const multi = el.dataset.multi === 'true';

  if (!multi) {
    // Single: deselect all, select this (or deselect if already selected)
    const alreadySelected = el.classList.contains('selected');
    document.querySelectorAll(`.opt-item[data-q="${qi}"]`).forEach(e => e.classList.remove('selected'));
    if (!alreadySelected) {
      el.classList.add('selected');
      answers[qi] = val;
    } else {
      answers[qi] = undefined;
    }
  } else {
    // Multi: toggle this item
    el.classList.toggle('selected');
    const sel = Array.from(document.querySelectorAll(`.opt-item[data-q="${qi}"].selected`)).map(e => e.dataset.val);
    answers[qi] = sel.length > 0 ? sel : undefined;
  }
  updateProgress();
}

function selectRating(btn) {
  const qi = btn.dataset.q;
  // Toggle off if already selected
  if (btn.classList.contains('selected')) {
    btn.classList.remove('selected');
    answers[qi] = undefined;
  } else {
    document.querySelectorAll(`.rating-btn[data-q="${qi}"]`).forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
    answers[qi] = parseInt(btn.dataset.val);
  }
  updateProgress();
}

// ── Person selection with min/max enforcement ──────────────────────────────
function selectPerson(idx, userId, name) {
  const frage   = QUESTIONS[idx];
  const maxSel  = frage.max_select || 1;
  const multi   = frage.Mehrfach || maxSel > 1;

  if (!multi) {
    const curSel = !Array.isArray(answers[idx]) ? answers[idx] : null;
    const alreadySel = curSel && curSel.id === userId;
    answers[idx] = alreadySel ? undefined : { id: userId, name };
  } else {
    let sel = Array.isArray(answers[idx]) ? [...answers[idx]] : [];
    const existIdx = sel.findIndex(s => s.id === userId);
    if (existIdx >= 0) {
      sel.splice(existIdx, 1);
    } else {
      if (sel.length >= maxSel) sel.shift();
      sel.push({ id: userId, name });
    }
    answers[idx] = sel.length > 0 ? sel : undefined;
  }

  // Re-render search results with updated state
  const query = document.getElementById(`psearch-${idx}`)?.value || '';
  filterPersons(idx, query);
  updateSelectionCount(idx);
  updateProgress();
}

function updateSelectionCount(idx) {
  const frage  = QUESTIONS[idx];
  const minSel = frage.min_select || 1;
  const maxSel = frage.max_select || 1;
  const multi  = frage.Mehrfach || maxSel > 1;
  if (!multi) return;

  const countEl = document.getElementById(`selcount-${idx}`);
  if (!countEl) return;

  const sel = Array.from(document.querySelectorAll(`.person-item[data-q="${idx}"].selected`));
  const n   = sel.length;
  countEl.style.display = 'inline-block';

  if (n < minSel) {
    countEl.className = 'selection-count warn';
    countEl.textContent = `${n} / ${maxSel} ausgewählt (mind. ${minSel} erforderlich)`;
  } else {
    countEl.className = 'selection-count ok';
    countEl.textContent = `${n} / ${maxSel} ausgewählt ✓`;
  }
}

function filterPersons(idx, query) {
  const lq      = query.trim().toLowerCase();
  const list    = document.getElementById(`plist-${idx}`);
  const selBox  = document.getElementById(`pselected-${idx}`);
  if (!list) return;

  const frage    = QUESTIONS[idx];
  const maxSel   = frage.max_select || 1;
  const multi    = frage.Mehrfach || maxSel > 1;
  const allMembers = (_personMembers[idx] || []);

  // Currently selected IDs
  const selectedIds = new Set(
    Array.from(document.querySelectorAll(`.person-item-sel[data-q="${idx}"]`))
      .map(e => e.dataset.uid)
  );

  if (!lq) {
    // Empty search: show only selected (in selected box), hide main list content
    list.innerHTML = '<div class="person-list-empty">Tippe einen Namen ein um Personen zu suchen.</div>';
    renderSelectedPersons(idx);
    return;
  }

  // Filter by query
  const matched = allMembers.filter(m => m.display.toLowerCase().includes(lq) || (m.username||'').toLowerCase().includes(lq));

  if (!matched.length) {
    list.innerHTML = '<div class="person-list-empty">Keine Personen gefunden.</div>';
    return;
  }

  list.innerHTML = matched.map(m => {
    const initials   = (m.display || '?').slice(0, 2).toUpperCase();
    const avatarHtml = m.avatar
      ? `<img class="person-avatar" src="${m.avatar}" alt="" onerror="this.outerHTML='<div class=person-avatar>${initials}</div>'">`
      : `<div class="person-avatar">${initials}</div>`;
    const membershipHtml = m.membership_days != null ? `<span class="person-membership">${m.membership_days}d</span>` : '';
    const isSelected = selectedIds.has(m.id);
    const escapedName = (m.display || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
    return `<div class="person-item ${isSelected ? 'selected' : ''}" data-q="${idx}" data-uid="${m.id}" data-name="${m.display||''}"
        onclick="selectPerson(${idx}, '${m.id}', '${escapedName}')">
        ${avatarHtml}
        <span class="person-name">${m.display || '?'}</span>
        ${membershipHtml}
        ${multi ? `<div class="person-check">${isSelected ? '✓' : ''}</div>` : ''}
      </div>`;
  }).join('');
  renderSelectedPersons(idx);
}

// Stores all pre-filtered members per question index
const _personMembers = {};

function renderSelectedPersons(idx) {
  const frage    = QUESTIONS[idx];
  const maxSel   = frage.max_select || 1;
  const multi    = frage.Mehrfach || maxSel > 1;
  const selBox   = document.getElementById(`pselected-${idx}`);
  if (!selBox) return;

  const sel = Array.isArray(answers[idx]) ? answers[idx] : (answers[idx] ? [answers[idx]] : []);
  if (!sel.length) { selBox.innerHTML = ''; return; }

  const label = `<div class="person-list-section-label">✓ Ausgewählt (${sel.length})</div>`;
  const items = sel.map(s => {
    const name = s?.name || String(s || '?');
    const uid  = s?.id   || '';
    return `<div class="person-item selected person-item-sel" data-q="${idx}" data-uid="${uid}" data-name="${name}"
        onclick="selectPerson(${idx}, '${uid}', '${name.replace(/'/g, "\\'")}')">
        <div class="person-avatar">${name.slice(0,2).toUpperCase()}</div>
        <span class="person-name">${name}</span>
        ${multi ? '<div class="person-check">✓</div>' : ''}
      </div>`;
  }).join('');
  selBox.innerHTML = label + items;
}

function updateProgress() {
  let answered = 0;
  QUESTIONS.forEach((frage, i) => {
    if (frage.Typ === 'text') {
      const el = document.getElementById(`text-${i}`);
      if (el && el.value.trim()) { answers[i] = el.value.trim(); answered++; }
      else if (answers[i]) answered++;
    } else if (answers[i] !== undefined && answers[i] !== null) {
      answered++;
    }
    const card = document.getElementById(`qcard-${i}`);
    if (card) card.classList.toggle('answered', answers[i] !== undefined && answers[i] !== null);
  });
  const pct  = QUESTIONS.length ? (answered / QUESTIONS.length * 100) : 0;
  const fill = document.getElementById('progressFill');
  const txt  = document.getElementById('progressText');
  if (fill) fill.style.width = pct + '%';
  if (txt)  txt.textContent  = `${answered} / ${QUESTIONS.length} beantwortet`;
}

async function encryptData(data, pemPublicKey) {
  const encoder    = new TextEncoder();
  const dataBytes  = encoder.encode(data);
  const aesKey     = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt']);
  const iv         = crypto.getRandomValues(new Uint8Array(12));
  const aesCipherBuf = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, aesKey, dataBytes);
  const aesCipher  = new Uint8Array(aesCipherBuf);
  const aesTag     = aesCipher.slice(-16);
  const ciphertext = aesCipher.slice(0, -16);
  const exportedAesKey = new Uint8Array(await crypto.subtle.exportKey('raw', aesKey));
  const pemBody = pemPublicKey.replace(/-----BEGIN PUBLIC KEY-----/, '').replace(/-----END PUBLIC KEY-----/, '').replace(/\s+/g, '');
  const derBuffer = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  const rsaKey = await crypto.subtle.importKey('spki', derBuffer.buffer, { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt']);
  const encAesKey = new Uint8Array(await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, rsaKey, exportedAesKey));
  const b64 = buf => btoa(String.fromCharCode(...buf));
  return [b64(encAesKey), b64(aesTag), b64(iv), b64(ciphertext)].join('|');
}

async function submitVote() {
  // Sync text answers
  QUESTIONS.forEach((frage, i) => {
    if (frage.Typ === 'text') {
      const el = document.getElementById(`text-${i}`);
      if (el && el.value.trim()) answers[i] = el.value.trim();
    }
  });

  // Validate required + person min_select
  const missing = [];
  QUESTIONS.forEach((frage, i) => {
    if (frage.Typ === 'person') {
      const minSel = frage.min_select || 1;
      const maxSel = frage.max_select || 1;
      const multi  = frage.Mehrfach || maxSel > 1;
      if (frage.Pflicht || multi) {
        const sel = Array.isArray(answers[i]) ? answers[i] : (answers[i] ? [answers[i]] : []);
        if (multi && sel.length < minSel) {
          missing.push(`Frage ${i+1}: Bitte mindestens ${minSel} Person${minSel>1?'en':''} auswählen (aktuell: ${sel.length})`);
          return;
        }
        if (frage.Pflicht && sel.length === 0) {
          missing.push(i + 1);
          return;
        }
      }
    } else {
      if (!frage.Pflicht) return;
      if (answers[i] === undefined || answers[i] === null || answers[i] === '') missing.push(i + 1);
    }
  });

  if (missing.length) {
    const msgs = missing.map(m => typeof m === 'number' ? `Frage ${m}` : m);
    alert(`Bitte beantworte / korrigiere:\n${msgs.join('\n')}`);
    return;
  }

  const btn = document.getElementById('submitBtn');
  btn.disabled = true; btn.textContent = 'Wird abgeschickt...';

  const answerObj = {};
  QUESTIONS.forEach((frage, i) => { answerObj[frage.Frage] = answers[i] ?? null; });

  const payload = {};
  if (PUBLIC_KEY) {
    try { payload.encrypted_data = await encryptData(JSON.stringify(answerObj), PUBLIC_KEY); }
    catch(e) { alert('Verschlüsselung fehlgeschlagen: ' + e.message); btn.disabled = false; btn.textContent = 'Abstimmung abschicken →'; return; }
  } else {
    payload.answers = answerObj;
  }

  try {
    const r = await fetch(`/vote/${VOTING_ID}/submit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!r.ok) {
      if (r.status === 401) { window.location.href = `/vote/${VOTING_ID}/login`; return; }
      alert(d.error || 'Fehler beim Abschicken');
      btn.disabled = false; btn.textContent = 'Abstimmung abschicken →';
      return;
    }
    document.getElementById('successOverlay').classList.add('show');
  } catch(e) {
    alert('Netzwerkfehler: ' + e.message);
    btn.disabled = false; btn.textContent = 'Abstimmung abschicken →';
  }
}

// ── Load person questions with membership + role filtering ─────────────────
// Also applies voter-level role filter (require_roles / exclude_roles)
async function loadPersonQuestions() {
  const personQs = QUESTIONS.map((q, i) => ({q, i})).filter(({q}) => q.Typ === 'person');
  if (!personQs.length) return;

  let members = [], voterRoles = [];
  try {
    const r = await fetch(`/api/vote/${VOTING_ID}/members`);
    if (r.status === 401) return;
    const d = await r.json();
    members    = d.members    || [];
    voterRoles = d.voterRoles || []; // roles of the current voter (if provided)
  } catch(e) { console.error(e); return; }

  personQs.forEach(({q: frage, i}) => {
    const list = document.getElementById(`plist-${i}`);
    if (!list) return;

    let displayMembers = members;

    // Filter by allowed user IDs
    if (frage.Optionen && frage.Optionen !== '--All' && Array.isArray(frage.Optionen)) {
      const allowed = new Set(frage.Optionen.map(String));
      displayMembers = displayMembers.filter(m => allowed.has(m.id));
    }

    // Filter by membership duration
    if (frage.membership_filter) {
      const minDays = frage.membership_min_days != null ? frage.membership_min_days : null;
      const maxDays = frage.membership_max_days != null ? frage.membership_max_days : null;
      displayMembers = displayMembers.filter(m => {
        const days = m.membership_days;
        if (days === null || days === undefined) return true;
        if (minDays !== null && days < minDays) return false;
        if (maxDays !== null && days > maxDays) return false;
        return true;
      });
    }

    // Role filter for candidates (candidate_require_roles / candidate_exclude_roles on the question)
    if (frage.candidate_require_roles && frage.candidate_require_roles.length) {
      const req = new Set(frage.candidate_require_roles.map(r => r.id || r));
      displayMembers = displayMembers.filter(m => (m.roles||[]).some(rid => req.has(rid)));
    }
    if (frage.candidate_exclude_roles && frage.candidate_exclude_roles.length) {
      const excl = new Set(frage.candidate_exclude_roles.map(r => r.id || r));
      displayMembers = displayMembers.filter(m => !(m.roles||[]).some(rid => excl.has(rid)));
    }

    _personMembers[i] = displayMembers;

    // Show empty state (search-first)
    list.innerHTML = '<div class="person-list-empty">Tippe einen Namen ein um Personen zu suchen.</div>';
    renderSelectedPersons(i);
    updateSelectionCount(i);
  });
}

// ── Role-gating for question visibility ───────────────────────────────────
// Hides entire question cards for voters who don't pass role filters
async function applyVoterRoleFilters() {
  // We need the voter's own roles – fetched via /api/vote/<id>/members
  // which returns voterRoles for the current session user
  try {
    const r = await fetch(`/api/vote/${VOTING_ID}/members`);
    if (!r.ok) return;
    const d = await r.json();
    const voterRoles = new Set(d.voterRoles || []);
    QUESTIONS.forEach((frage, i) => {
      const card = document.getElementById(`qcard-${i}`);
      if (!card) return;
      const reqR  = (frage.require_roles || []).map(r2 => r2.id || r2);
      const exclR = (frage.exclude_roles || []).map(r2 => r2.id || r2);
      let visible = true;
      if (reqR.length  && !reqR.some(rid => voterRoles.has(rid)))   visible = false;
      if (exclR.length &&  exclR.some(rid => voterRoles.has(rid)))  visible = false;
      card.style.display = visible ? '' : 'none';
    });
  } catch(e) { /* silently ignore */ }
}

if (HAS_PERSON) loadPersonQuestions();
applyVoterRoleFilters();
updateProgress();

// ── Vote page deadline countdown ──────────────────────────────────────────
const ENDS_AT = {{ (voting.ends_at or '') | tojson }};
if (ENDS_AT) {
  const endTs = new Date(ENDS_AT).getTime();
  function updateVoteCountdown() {
    const cdEl = document.getElementById('voteCountdown');
    const barEl = document.getElementById('deadlineBar');
    if (!cdEl) return;
    const diff = endTs - Date.now();
    if (diff <= 0) {
      cdEl.textContent = 'Abgelaufen';
      if (barEl) { barEl.className = 'deadline-bar urgent'; }
      clearInterval(_cdInterval);
      return;
    }
    const d = Math.floor(diff/86400000);
    const h = Math.floor((diff%86400000)/3600000);
    const m = Math.floor((diff%3600000)/60000);
    const s = Math.floor((diff%60000)/1000);
    if (d > 0)      cdEl.textContent = `${d}T ${h}h ${m}m`;
    else if (h > 0) cdEl.textContent = `${h}h ${m}m ${s}s`;
    else            cdEl.textContent = `${m}m ${s}s`;
    // Turn red if less than 10 minutes
    if (barEl && diff < 600000) barEl.className = 'deadline-bar urgent';
  }
  updateVoteCountdown();
  const _cdInterval = setInterval(updateVoteCountdown, 1000);
}

// ── Countdown timer ────────────────────────────────────────────────────────
(function() {
  const endsAt = {{ ends_at | tojson }};
  if (!endsAt) return;
  const endTs = new Date(endsAt).getTime();
  const el = document.getElementById('voteCountdown');
  if (!el) return;
  function update() {
    const diff = endTs - Date.now();
    if (diff <= 0) {
      el.parentElement.innerHTML = '<span style="color:#f87171;font-weight:700;">⏰ Abstimmung abgelaufen – wird geschlossen...</span>';
      setTimeout(() => location.reload(), 3000);
      return;
    }
    const d = Math.floor(diff / 86400000);
    const h = Math.floor((diff % 86400000) / 3600000);
    const m = Math.floor((diff % 3600000) / 60000);
    const s = Math.floor((diff % 60000) / 1000);
    if (d > 0) el.textContent = `${d}d ${h}h ${m}m`;
    else if (h > 0) el.textContent = `${h}h ${m}m ${s}s`;
    else if (m > 0) el.textContent = `${m}m ${s}s`;
    else el.textContent = `${s}s`;
  }
  update();
  setInterval(update, 1000);
})();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

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
.q-title{font-size:1rem;font-weight:500;margin-bottom:10px;}
.q-mode-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(91,140,255,.1);border:1px solid rgba(91,140,255,.2);border-radius:10px;padding:2px 8px;font-size:.65rem;font-weight:600;color:#5b8cff;margin-bottom:14px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:180px;font-size:.82rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
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
    🔐 Diese Abstimmung ist verschlüsselt.
    <br><a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">→ Entschlüsselungs-Tool öffnen</a>
    {% if public_results_exist %}
    <br><a class="dec-link" href="/vote/{{ voting_id }}/public-results" style="color:var(--green);">→ Öffentliche Ergebnisseite</a>
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
    <a class="voters-link" href="/vote/{{ voting_id }}/voters">👁️ Voter-Log anzeigen</a>
  </div>
  {% endif %}

  {% if not voting.public_key %}
    {% for frage in voting.questions %}
    <div class="q-block">
      <div class="q-label">Frage {{ loop.index }}</div>
      {% if frage.get('heading') %}<div style="font-size:1.05rem;font-weight:700;margin-bottom:4px;letter-spacing:-.2px;">{{ frage.heading }}</div>{% endif %}
      <div class="q-title">{{ frage.Frage }}</div>
      {% if frage.get('subtext') %}<div style="font-size:.8rem;color:var(--sub);margin-bottom:10px;line-height:1.5;">{{ frage.subtext }}</div>{% endif %}
      {% if frage.Typ == 'person' %}
      <div class="q-mode-badge">{{ '👥 Gruppen-Auswertung' if frage.get('result_mode','individual') == 'group' else '👤 Einzel-Auswertung' }}</div>
      {% endif %}
      <div class="q-results" id="qr-{{ loop.index0 }}"><div style="color:var(--sub);font-size:.82rem;">Wird berechnet...</div></div>
    </div>
    {% endfor %}
  {% else %}
    <div class="q-block">
      <div class="q-title">Verschlüsselte Antworten ({{ responses|length }})</div>
      <div style="color:var(--sub);font-size:.85rem;">Nutze das <a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">Entschlüsselungs-Tool</a>.</div>
    </div>
  {% endif %}
</div>
<script>
const RESPONSES  = {{ responses | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
const IS_ENCRYPTED = {{ ('true' if voting.public_key else 'false') }};

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function computePersonResults(vals, resultMode) {
  if (resultMode === 'group') {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      const names = items.map(i => i?.name || String(i||'?')).sort().join(' + ');
      counts[names] = (counts[names] || 0) + 1;
    });
    return counts;
  } else {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      items.forEach(item => {
        const name = item?.name || String(item||'?');
        counts[name] = (counts[name] || 0) + 1;
      });
    });
    return counts;
  }
}

if (!IS_ENCRYPTED && RESPONSES.length) {
  const parsed = RESPONSES.map(r => { try { return JSON.parse(r.answers); } catch { return {}; } });
  QUESTIONS.forEach((frage, qi) => {
    const container = document.getElementById(`qr-${qi}`);
    if (!container) return;
    const vals = parsed.map(p => p[frage.Frage]).filter(v => v !== null && v !== undefined);

    if (frage.Typ === 'text') {
      container.innerHTML = vals.filter(Boolean).map(v =>
        `<div style="background:var(--card2);border:1px solid var(--border2);border-radius:var(--r2);padding:10px 14px;font-size:.88rem;margin-bottom:8px;">${esc(String(v))}</div>`
      ).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';

    } else if (frage.Typ === 'rating') {
      const counts = {}; for (let v = frage.Min||1; v <= (frage.Max||5); v++) counts[v] = 0;
      vals.forEach(v => { if (counts[v] !== undefined) counts[v]++; });
      const max = Math.max(...Object.values(counts), 1);
      const avg = vals.length ? (vals.reduce((a,b) => a + Number(b), 0) / vals.length).toFixed(2) : '–';
      container.innerHTML = `<div style="color:var(--sub);font-size:.78rem;margin-bottom:12px;">Ø ${avg}</div>`
        + Object.entries(counts).map(([v,c]) =>
            `<div class="bar-row"><div class="bar-label">★ ${v}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
          ).join('');

    } else if (frage.Typ === 'choice') {
      const counts = {};
      vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { counts[item] = (counts[item]||0)+1; }); });
      const max = Math.max(...Object.values(counts), 1);
      container.innerHTML = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([label,c]) =>
        `<div class="bar-row"><div class="bar-label" title="${esc(label)}">${esc(label)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
      ).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';

    } else if (frage.Typ === 'person') {
      const resultMode = frage.result_mode || 'individual';
      const counts = computePersonResults(vals, resultMode);
      const max    = Math.max(...Object.values(counts), 1);
      container.innerHTML = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([name,c]) =>
        `<div class="bar-row"><div class="bar-label" title="${esc(name)}">${esc(name)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
      ).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
    }
  });
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# RECONSTRUCT TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

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
.q-title{font-size:.92rem;font-weight:500;margin-bottom:6px;}
.q-mode-badge{display:inline-flex;align-items:center;gap:4px;background:rgba(91,140,255,.1);border:1px solid rgba(91,140,255,.2);border-radius:10px;padding:2px 8px;font-size:.63rem;font-weight:600;color:#5b8cff;margin-bottom:12px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:180px;font-size:.82rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-track{flex:1;background:var(--border);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{height:100%;background:var(--accent);border-radius:4px;}
.bar-count{font-family:'DM Mono',monospace;font-size:.78rem;color:var(--sub);width:40px;text-align:right;flex-shrink:0;}
.answer-row{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:10px 14px;margin-bottom:8px;font-size:.88rem;color:var(--sub);}
.answer-row span{color:var(--text);}
.progress-text{font-size:.78rem;color:var(--sub);margin-top:10px;}
.dl-btn{background:var(--card2);border:1px solid var(--border2);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.85rem;font-weight:600;padding:9px 18px;border-radius:var(--r2);cursor:pointer;margin-right:8px;margin-bottom:8px;transition:all .15s;}
.dl-btn:hover{border-color:var(--accent);color:var(--accent);}
</style>
</head>
<body>
<div class="page">
  <h1>🔓 Antworten entschlüsseln</h1>
  <div class="sub">{{ voting.title }} — Entschlüsselung mit privatem RSA-Schlüssel</div>
  {% if voting.is_active %}<div class="active-badge">🟢 Abstimmung läuft noch – MBL-Vorschau</div>{% endif %}
  {% if already_published %}<div class="already-pub">✅ Ergebnisse bereits veröffentlicht · <a href="/vote/{{ voting_id }}/public-results" style="color:var(--green);">→ Öffentliche Seite</a></div>{% endif %}
  <div class="warn-box">⚠️ Der private Schlüssel verlässt <strong>niemals</strong> deinen Browser.</div>
  <div class="card">
    <label>Privater RSA-Schlüssel (PEM)</label>
    <textarea class="key-input" id="privateKeyInput" placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;-----END RSA PRIVATE KEY-----"></textarea>
    <div><button class="btn" id="decryptBtn" onclick="startDecrypt()">🔓 Entschlüsseln ({{ responses | length }} Antworten)</button></div>
    <div class="progress-text" id="progressText"></div>
  </div>

  <div class="result-container" id="resultContainer">
    <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;">
      <button class="dl-btn" onclick="downloadJSON()">⬇️ JSON</button>
      <button class="dl-btn" onclick="downloadCSV()">⬇️ CSV</button>
      {% if is_mbl %}<button class="dl-btn btn-green" onclick="publishResults()" id="publishBtn">🌐 Ergebnisse veröffentlichen</button>{% endif %}
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
const RESPONSES  = {{ responses | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
const VOTING_ID  = {{ voting_id | tojson }};
const IS_MBL     = {{ is_mbl | tojson }};
let decryptedAll = [];

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

function computePersonResults(vals, resultMode) {
  if (resultMode === 'group') {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      const names = items.map(i => i?.name || String(i||'?')).sort().join(' + ');
      counts[names] = (counts[names]||0)+1;
    });
    return counts;
  } else {
    const counts = {};
    vals.forEach(v => {
      const items = Array.isArray(v) ? v : [v];
      items.forEach(item => {
        const name = item?.name || String(item||'?');
        counts[name] = (counts[name]||0)+1;
      });
    });
    return counts;
  }
}

async function decryptEntry(encStr, privateKey) {
  const parts = encStr.split('|'); if (parts.length !== 4) throw new Error('Ungültiges Format');
  const [encKeyB64, tagB64, ivB64, cipherB64] = parts;
  const b64ToU8 = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const encAesKey = b64ToU8(encKeyB64), tag = b64ToU8(tagB64), iv = b64ToU8(ivB64), ciphertext = b64ToU8(cipherB64);
  const aesKeyBytes = await crypto.subtle.decrypt({ name:'RSA-OAEP' }, privateKey, encAesKey);
  const importedAes = await crypto.subtle.importKey('raw', aesKeyBytes, { name:'AES-GCM' }, false, ['decrypt']);
  const combined = new Uint8Array(ciphertext.length + tag.length); combined.set(ciphertext); combined.set(tag, ciphertext.length);
  const plainBuf = await crypto.subtle.decrypt({ name:'AES-GCM', iv }, importedAes, combined);
  return new TextDecoder().decode(plainBuf);
}

async function importPrivateKey(pem) {
  const pemBody = pem.replace(/-----BEGIN (?:RSA )?PRIVATE KEY-----/,'').replace(/-----END (?:RSA )?PRIVATE KEY-----/,'').replace(/\s+/g,'');
  const der = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  return await crypto.subtle.importKey('pkcs8', der.buffer, { name:'RSA-OAEP', hash:'SHA-256' }, false, ['decrypt']);
}

async function startDecrypt() {
  const pem = document.getElementById('privateKeyInput').value.trim();
  if (!pem) { alert('Bitte privaten Schlüssel eingeben'); return; }
  const btn = document.getElementById('decryptBtn'); btn.disabled = true; btn.textContent = 'Entschlüsselt...';
  let privateKey; try { privateKey = await importPrivateKey(pem); } catch(e) { alert(e.message); btn.disabled = false; btn.textContent = '🔓 Entschlüsseln'; return; }
  const enc = RESPONSES.filter(r => r.is_encrypted); const prog = document.getElementById('progressText');
  decryptedAll = []; let failed = 0;
  for (let i = 0; i < enc.length; i++) {
    prog.textContent = `${i+1} / ${enc.length} entschlüsselt...`;
    try { const plain = await decryptEntry(enc[i].answers, privateKey); decryptedAll.push({ data: JSON.parse(plain), submitted_at: enc[i].submitted_at }); }
    catch { failed++; }
  }
  prog.textContent = `✅ ${decryptedAll.length} entschlüsselt${failed > 0 ? ` (${failed} fehlgeschlagen)` : ''}`;
  renderResults();
  btn.textContent = '🔓 Erneut entschlüsseln'; btn.disabled = false;
}

function renderResults() {
  const container = document.getElementById('resultsGrid');
  document.getElementById('resultContainer').classList.add('show');

  container.innerHTML = QUESTIONS.map((frage, qi) => {
    const vals = decryptedAll.map(d => d.data[frage.Frage]).filter(v => v !== null && v !== undefined);
    let inner = '';

    if (frage.Typ === 'text') {
      inner = vals.map(v => `<div class="answer-row"><span>${esc(String(v))}</span></div>`).join('');

    } else if (frage.Typ === 'rating') {
      const counts = {}; for (let v = frage.Min||1; v <= (frage.Max||5); v++) counts[v] = 0;
      vals.forEach(v => { if (counts[v] !== undefined) counts[v]++; });
      const max = Math.max(...Object.values(counts), 1);
      const avg = vals.length ? (vals.reduce((a,b) => a+Number(b), 0) / vals.length).toFixed(2) : '–';
      inner = `<div style="color:var(--sub);font-size:.78rem;margin-bottom:10px;">Ø ${avg}</div>`
        + Object.entries(counts).map(([v,c]) =>
            `<div class="bar-row"><div class="bar-label">★ ${v}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
          ).join('');

    } else if (frage.Typ === 'choice') {
      const counts = {};
      vals.forEach(v => { const items = Array.isArray(v) ? v : [v]; items.forEach(item => { counts[item] = (counts[item]||0)+1; }); });
      const max = Math.max(...Object.values(counts), 1);
      inner = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([k,c]) =>
        `<div class="bar-row"><div class="bar-label">${esc(k)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
      ).join('');

    } else if (frage.Typ === 'person') {
      const resultMode = frage.result_mode || 'individual';
      const counts     = computePersonResults(vals, resultMode);
      const max        = Math.max(...Object.values(counts), 1);
      inner = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([name,c]) =>
        `<div class="bar-row"><div class="bar-label" title="${esc(name)}">${esc(name)}</div><div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div><div class="bar-count">${c}</div></div>`
      ).join('');
    }

    const modeBadge = frage.Typ === 'person'
      ? `<div class="q-mode-badge">${(frage.result_mode||'individual')==='group' ? '👥 Gruppen' : '👤 Einzel'}-Auswertung</div>`
      : '';

    const headingHtml = frage.heading ? `<div style="font-size:1.05rem;font-weight:700;margin-bottom:4px;">${esc(frage.heading)}</div>` : '';
    const subtextHtml = frage.subtext ? `<div style="font-size:.8rem;color:var(--sub);margin-bottom:10px;">${esc(frage.subtext)}</div>` : '';
    return `<div class="q-block">
      <div class="q-label">Frage ${qi+1}</div>
      ${headingHtml}
      <div class="q-title">${esc(frage.Frage)}</div>
      ${subtextHtml}
      ${modeBadge}
      ${inner || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>'}
    </div>`;
  }).join('');
}

async function publishResults() {
  if (!IS_MBL || !decryptedAll.length) { alert('Zuerst entschlüsseln!'); return; }
  const btn = document.getElementById('publishBtn'); btn.disabled = true; btn.textContent = 'Veröffentliche...';
  try {
    const res = await fetch(`/api/vote/${VOTING_ID}/publish-results`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ results: decryptedAll }),
    });
    const data = await res.json();
    if (data.ok) {
      const box = document.getElementById('pubBox'), link = document.getElementById('pubLink');
      box.classList.add('show'); link.href = data.url; link.textContent = data.url;
      btn.textContent = '✅ Veröffentlicht';
    } else { alert('Fehler: ' + (data.error||'?')); btn.disabled = false; btn.textContent = '🌐 Veröffentlichen'; }
  } catch(e) { alert('Netzwerkfehler: ' + e.message); btn.disabled = false; btn.textContent = '🌐 Veröffentlichen'; }
}

function downloadJSON() { const blob = new Blob([JSON.stringify(decryptedAll, null, 2)], {type:'application/json'}); const a = document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='abstimmung_ergebnisse.json'; a.click(); }
function downloadCSV() {
  const headers = ['Abgestimmt am', ...QUESTIONS.map(q => q.Frage)];
  const rows = decryptedAll.map(d => [d.submitted_at, ...QUESTIONS.map(q => {
    const v = d.data[q.Frage]; if (Array.isArray(v)) return v.map(i => i?.name||i).join('; '); return v?.name||v||'';
  })]);
  const csv = [headers,...rows].map(r => r.map(c => `"${String(c||'').replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob(['\uFEFF'+csv], {type:'text/csv;charset=utf-8'}); const a = document.createElement('a'); a.href=URL.createObjectURL(blob); a.download='abstimmung_ergebnisse.csv'; a.click();
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# HELPER RENDERERS
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
  body{{font-family:'DM Sans',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:16px;padding:48px 40px;text-align:center;max-width:420px;width:100%;}}
  .icon{{font-size:3rem;margin-bottom:20px;}}
  h1{{font-size:1.5rem;font-weight:700;margin-bottom:10px;}}
  p{{color:var(--sub);font-size:.9rem;line-height:1.6;margin-bottom:20px;}}
  .name{{color:var(--green);font-weight:600;}}
  a{{display:inline-block;color:var(--sub);font-size:.82rem;text-decoration:none;border:1px solid var(--border);border-radius:8px;padding:8px 18px;transition:border-color .2s;margin:4px;}}
  a:hover{{border-color:var(--green);color:var(--green);}}
</style></head><body>
<div class="card">
  <div class="icon">✅</div>
  <h1>Bereits abgestimmt</h1>
  <p>Du hast als <span class="name">{user_name}</span> bereits teilgenommen.<br><br>
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
  padding:52px 44px;text-align:center;max-width:440px;width:100%;box-shadow:0 24px 80px rgba(0,0,0,.7);}
.badge{display:inline-flex;align-items:center;gap:6px;background:rgba(91,140,255,.1);border:1px solid rgba(91,140,255,.25);
  border-radius:20px;padding:4px 14px;font-size:.72rem;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--accent);margin-bottom:22px;}
.dot{width:6px;height:6px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
h1{font-size:1.6rem;font-weight:700;margin-bottom:8px;}
.sub{color:var(--sub);font-size:.9rem;line-height:1.6;margin-bottom:32px;}
.discord-btn{display:inline-flex;align-items:center;justify-content:center;gap:10px;width:100%;padding:14px 20px;
  background:#5865F2;color:#fff;font-family:'DM Sans',sans-serif;font-size:.95rem;font-weight:600;
  border-radius:10px;border:none;cursor:pointer;text-decoration:none;transition:all .2s;box-shadow:0 4px 18px rgba(88,101,242,.32);}
.discord-btn:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(88,101,242,.48);}
.note{margin-top:20px;font-size:.75rem;color:var(--sub);line-height:1.6;background:rgba(255,255,255,.03);
  border:1px solid var(--border2);border-radius:8px;padding:12px 16px;}
</style>
</head>
<body>
<div class="card">
  <div class="badge"><span class="dot"></span>Abstimmung</div>
  <h1>{{ voting.title }}</h1>
  <p class="sub">{{ voting.description or 'Melde dich mit Discord an um teilzunehmen.' }}</p>
  <a href="{{ oauth_url }}" class="discord-btn">
    <svg width="20" height="15" viewBox="0 0 71 55" fill="#fff">
      <path d="M60.1 4.9A58.6 58.6 0 0 0 45.6 0a40 40 0 0 0-1.9 3.8 54.2 54.2 0 0 0-16.2 0A40 40 0 0 0 25.7 0 58.3 58.3 0 0 0 11 4.9C1.6 18.3-.9 31.3.3 44.1a58.9 58.9 0 0 0 18 9.1 44 44 0 0 0 3.8-6.2 38.4 38.4 0 0 1-6-2.9l1.5-1.1a42.2 42.2 0 0 0 36 0l1.5 1.1a38.4 38.4 0 0 1-6 2.9 44 44 0 0 0 3.8 6.2 58.7 58.7 0 0 0 18-9.1c1.5-15.3-2.6-28.2-10.8-39.1ZM23.7 36.1c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Z"/>
    </svg>
    Mit Discord anmelden
  </a>
  <div class="note">🔒 Nur ein anonymer Hash deiner Discord-ID wird gespeichert.</div>
</div>
</body>
</html>"""
