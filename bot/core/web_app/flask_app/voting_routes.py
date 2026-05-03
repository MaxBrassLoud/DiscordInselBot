"""
bot/core/web_app/flask_app/voting_routes.py
=============================================
Web-Routen für das Abstimmungssystem.

ÄNDERUNGEN (Login-Pflicht):
  - Abstimmungs-Seite erfordert Discord OAuth2-Login
  - voter_hash wird aus der echten Discord-User-ID berechnet → keine Doppelabstimmung möglich
  - Die User-ID selbst wird NICHT gespeichert – nur der SHA256-Hash
  - Abgestimmte User können sich ausloggen, die Abstimmung bleibt anonym

In app.py einbinden:
    from .voting_routes import register_voting_routes
    register_voting_routes(app)

ROUTEN:
  GET  /vote/<voting_id>              – Abstimmungsformular (Login erforderlich)
  GET  /vote/<voting_id>/login        – Discord OAuth2 Login für Abstimmung
  GET  /vote/<voting_id>/callback     – OAuth2 Callback
  GET  /vote/<voting_id>/logout       – Logout nach Abstimmung
  POST /vote/<voting_id>/submit       – Antwort absenden
  GET  /vote/<voting_id>/results      – Ergebnisse (nur MBL oder nach Ablauf)
  GET  /vote/<voting_id>/reconstruct  – Entschlüsselung mit privatem Schlüssel
  GET  /api/vote/<voting_id>/members  – Server-Mitglieder für Person-Fragen
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
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
        r = req_lib.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
            timeout=8,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def _get_guild_members(guild_id: str) -> list[dict]:
    """Lädt alle Server-Mitglieder (max 1000)."""
    members = _bot_get(f"/guilds/{guild_id}/members?limit=1000") or []
    result = []
    for m in members:
        u = m.get("user", {})
        if u.get("bot"):
            continue
        result.append({
            "id":          u.get("id", ""),
            "display":     m.get("nick") or u.get("global_name") or u.get("username") or "?",
            "username":    u.get("username", ""),
            "avatar":      (
                f"https://cdn.discordapp.com/avatars/{u['id']}/{u['avatar']}.png?size=32"
                if u.get("avatar") else None
            ),
        })
    return result


def _discord_oauth_url(voting_id: str, random_part: str) -> str:
    """Erzeugt Discord OAuth2 URL – state enthält voting_id und Zufallswert."""
    state = f"{voting_id}:{random_part}"
    params = urlencode({
        "client_id":     CLIENT_ID,
        "redirect_uri":  f"{WEB_BASE_URL}/vote/callback",   # feste URL
        "response_type": "code",
        "scope":         "identify",
        "state":         state,
    })
    return f"https://discord.com/oauth2/authorize?{params}"


def _exchange_code(voting_id: str, code: str) -> dict | None:
    """Tauscht OAuth-Code gegen Token – verwendet feste callback-URL."""
    try:
        r = req_lib.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  f"{WEB_BASE_URL}/vote/callback",   # feste URL
            },
            timeout=8,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def _get_discord_user(access_token: str) -> dict | None:
    """Holt Discord-Nutzerdaten über den Bearer Token."""
    try:
        r = req_lib.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8,
        )
        return r.json() if r.ok else None
    except Exception:
        return None


def register_voting_routes(app, login_required=None, _is_mbl_fn=None, _bot_get_fn=None):
    from bot.core.supabase_client import get_supabase

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

        # Login prüfen
        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return redirect(f"/vote/{voting_id}/login")

        user_id   = voter_session["user_id"]
        user_name = voter_session.get("display_name", "?")

        # Bereits abgestimmt?
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
            return _render_error(
                "Abstimmung geschlossen",
                "Diese Abstimmung ist nicht mehr aktiv.",
                403,
            )

        if not CLIENT_ID or not CLIENT_SECRET:
            return _render_error(
                "Konfigurationsfehler",
                "Discord OAuth2 ist nicht konfiguriert.",
                500,
            )

        random_part = secrets.token_urlsafe(16)
        oauth_url = _discord_oauth_url(voting_id, random_part)

        # Speichere den gesamten erwarteten state (voting_id:random) unter einem generischen Key
        # So können wir später beim Callback die voting_id aus dem state extrahieren.
        session["vote_oauth_state"] = f"{voting_id}:{random_part}"

        return render_template_string(
            _LOGIN_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            oauth_url=oauth_url,
        )

    # ── GET /vote/<voting_id>/callback ────────────────────────────────────────
    @app.route("/vote/callback")
    def vote_callback():
        sb = get_supabase()

        # State prüfen (CSRF-Schutz)
        stored_state = session.pop("vote_oauth_state", "")
        given_state = request.args.get("state", "")

        if not stored_state or stored_state != given_state:
            return _render_error("Ungültiger State", "Bitte versuche es erneut.", 400,
                                 show_login_link="/vote")  # Link zur Übersicht (optional)

        if "error" in request.args:
            return _render_error(
                "Zugriff verweigert",
                "Discord-Login wurde abgelehnt.",
                403,
                show_login_link="/vote",
            )

        code = request.args.get("code", "")
        if not code:
            return _render_error("Kein Code", "OAuth-Code fehlt.", 400,
                                 show_login_link="/vote")

        # Extrahiere voting_id aus dem state (Format: "voting_id:random")
        voting_id = given_state.split(":", 1)[0]

        # Abstimmung existiert?
        r = sb.table("votings").select("is_active").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)

        # Token tauschen (benötigt die voting_id für die redirect_uri – aber wir haben eine feste URL,
        # die muss exakt mit der in Discord eingetragenen übereinstimmen)
        tok = _exchange_code(voting_id, code)  # diese Funktion muss angepasst werden (siehe unten)
        if not tok or "access_token" not in tok:
            return _render_error("Token-Fehler", "Discord-Login fehlgeschlagen.", 500,
                                 show_login_link=f"/vote/{voting_id}/login")

        # User-Daten laden
        discord_user = _get_discord_user(tok["access_token"])
        if not discord_user or "id" not in discord_user:
            return _render_error("Profil-Fehler", "Nutzerdaten konnten nicht geladen werden.", 500,
                                 show_login_link=f"/vote/{voting_id}/login")

        user_id = discord_user["id"]
        display_name = discord_user.get("global_name") or discord_user.get("username") or "?"

        # Session setzen
        session[f"voter_{voting_id}"] = {
            "user_id": user_id,
            "display_name": display_name,
        }

        # Prüfen, ob bereits abgestimmt
        voter_hash_val = _voter_hash(user_id, voting_id)
        existing = sb.table("voting_responses") \
            .select("id") \
            .eq("voting_id", voting_id) \
            .eq("voter_hash", voter_hash_val) \
            .execute()
        if existing.data:
            voting_data = sb.table("votings").select("*").eq("id", voting_id).execute().data[0]
            return _render_already_voted(voting_data, voting_id, display_name)

        return redirect(f"/vote/{voting_id}")

    # ── GET /vote/<voting_id>/logout ──────────────────────────────────────────
    @app.route("/vote/<voting_id>/logout")
    def vote_logout(voting_id):
        session.pop(f"voter_{voting_id}", None)
        return redirect(f"/vote/{voting_id}/login")

    # ── POST /vote/<voting_id>/submit ─────────────────────────────────────────
    @app.route("/vote/<voting_id>/submit", methods=["POST"])
    def vote_submit(voting_id):
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Abstimmung nicht gefunden"}), 404
        voting = r.data[0]

        if not voting.get("is_active", True):
            return jsonify({"error": "Abstimmung ist geschlossen"}), 403

        # Login prüfen
        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return jsonify({"error": "Nicht eingeloggt"}), 401

        user_id = voter_session["user_id"]
        voter_hash_val = _voter_hash(user_id, voting_id)

        # Doppelte Abstimmung prüfen
        existing = sb.table("voting_responses")\
            .select("id")\
            .eq("voting_id", voting_id)\
            .eq("voter_hash", voter_hash_val)\
            .execute()
        if existing.data:
            return jsonify({"error": "Du hast bereits abgestimmt"}), 409

        body = request.get_json() or {}
        answers       = body.get("answers", {})
        encrypted_data = body.get("encrypted_data", None)
        is_encrypted  = bool(encrypted_data)

        stored_data = encrypted_data if is_encrypted else json.dumps(answers, ensure_ascii=False)

        try:
            sb.table("voting_responses").insert({
                "voting_id":     voting_id,
                "voter_hash":    voter_hash_val,
                "answers":       stored_data,
                "is_encrypted":  is_encrypted,
                "submitted_at":  datetime.now(timezone.utc).isoformat(),
            }).execute()
        except Exception as e:
            log.error(f"[vote_submit] {e}")
            return jsonify({"error": "Fehler beim Speichern"}), 500

        # Session nach Abstimmung löschen (User ist anonym, kein Grund mehr zum Behalten)
        session.pop(f"voter_{voting_id}", None)

        return jsonify({"ok": True, "message": "Danke für deine Teilnahme!"})

    # ── GET /vote/<voting_id>/results ─────────────────────────────────────────
    @app.route("/vote/<voting_id>/results")
    def vote_results(voting_id):
        from flask import session as flask_session
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)
        voting = r.data[0]

        user = flask_session.get("user", {})
        is_mbl = bool(MBL_ID and user.get("id") == MBL_ID)

        if voting.get("is_active") and not is_mbl:
            return _render_error(
                "Zugriff verweigert",
                "Die Ergebnisse sind erst nach Ende der Abstimmung verfügbar.",
                403,
            )

        responses = sb.table("voting_responses")\
            .select("answers, is_encrypted, submitted_at")\
            .eq("voting_id", voting_id)\
            .execute().data or []

        return render_template_string(
            _RESULTS_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            responses=responses,
            is_mbl=is_mbl,
        )

    # ── GET /vote/<voting_id>/reconstruct ─────────────────────────────────────
    @app.route("/vote/<voting_id>/reconstruct")
    def vote_reconstruct(voting_id):
        sb = get_supabase()
        r  = sb.table("votings").select("*").eq("id", voting_id).execute()
        if not r.data:
            return _render_error("Abstimmung nicht gefunden", "Diese Abstimmung existiert nicht.", 404)
        voting = r.data[0]

        if not voting.get("public_key"):
            return _render_error(
                "Keine Verschlüsselung",
                "Diese Abstimmung verwendet keine Verschlüsselung.",
                400,
            )

        responses = []
        if not voting.get("is_active"):
            responses = sb.table("voting_responses")\
                .select("answers, is_encrypted, submitted_at")\
                .eq("voting_id", voting_id)\
                .execute().data or []

        return render_template_string(
            _RECONSTRUCT_TEMPLATE,
            voting=voting,
            voting_id=voting_id,
            responses=responses,
        )

    # ── GET /api/vote/<voting_id>/members ──────────────────────────────────────
    @app.route("/api/vote/<voting_id>/members")
    def api_vote_members(voting_id):
        # Login prüfen
        voter_session = session.get(f"voter_{voting_id}")
        if not voter_session or not voter_session.get("user_id"):
            return jsonify({"error": "Nicht eingeloggt"}), 401

        sb = get_supabase()
        r  = sb.table("votings").select("server_id, allowed_users").eq("id", voting_id).execute()
        if not r.data:
            return jsonify({"error": "Nicht gefunden"}), 404
        voting = r.data[0]
        server_id = voting.get("server_id", "")
        members = _get_guild_members(server_id)
        return jsonify({"members": members})


# ══════════════════════════════════════════════════════════════════════════════
# ERROR / ALREADY-VOTED HELPERS
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
    border:1px solid var(--border);border-radius:8px;padding:8px 18px;transition:border-color .2s;}}
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


# ══════════════════════════════════════════════════════════════════════════════
# LOGIN TEMPLATE
# ══════════════════════════════════════════════════════════════════════════════

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
.shield{font-size:1.1rem;margin-bottom:6px;}
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

  <div class="note">
    <div class="shield">🔒</div>
    Deine Identität bleibt anonym. Es wird nur ein <strong>Hash</strong> deiner Discord-ID gespeichert –
    kein Name, kein Avatar, keine User-ID. Der Hash verhindert Doppelabstimmungen, ist aber nicht
    zurückverfolgbar.
  </div>
</div>
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
  --bg: #060708;
  --surface: #0f1117;
  --card: #161b24;
  --card2: #1c2130;
  --border: #1f2535;
  --border2: #2a3245;
  --text: #e2e8f5;
  --sub: #7a8499;
  --dim: #3d4660;
  --accent: #5b8cff;
  --accent2: #3d6fff;
  --green: #4ade80;
  --red: #f87171;
  --gold: #fbbf24;
  --r: 12px;
  --r2: 8px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'DM Sans', sans-serif;
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  line-height: 1.6;
  -webkit-font-smoothing: antialiased;
}

.bg-layer {
  position: fixed; inset: 0; z-index: 0; pointer-events: none;
  background:
    radial-gradient(ellipse 80% 60% at 10% -10%, rgba(91,140,255,0.07) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 90% 100%, rgba(74,222,128,0.04) 0%, transparent 60%);
}

.page {
  position: relative; z-index: 1;
  max-width: 720px; margin: 0 auto; padding: 48px 22px 80px;
}

/* ── User bar ── */
.user-bar {
  display: flex; align-items: center; justify-content: space-between;
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--r); padding: 10px 16px; margin-bottom: 32px;
  font-size: .83rem;
}
.user-info { display: flex; align-items: center; gap: 8px; color: var(--sub); }
.user-name { color: var(--text); font-weight: 600; }
.user-badge {
  background: rgba(74,222,128,.1); color: var(--green);
  border: 1px solid rgba(74,222,128,.22); border-radius: 20px;
  padding: 2px 10px; font-size: .68rem; font-weight: 700;
}
.logout-link {
  color: var(--sub); font-size: .78rem; text-decoration: none;
  border: 1px solid var(--border2); border-radius: 6px; padding: 4px 10px;
  transition: all .15s;
}
.logout-link:hover { border-color: var(--red); color: var(--red); }

/* ── Header ── */
.vote-header {
  text-align: center; margin-bottom: 48px;
}
.vote-badge {
  display: inline-flex; align-items: center; gap: 6px;
  background: rgba(91,140,255,.1); border: 1px solid rgba(91,140,255,.25);
  border-radius: 20px; padding: 4px 14px;
  font-size: .72rem; font-weight: 600; letter-spacing: .08em;
  text-transform: uppercase; color: var(--accent);
  margin-bottom: 20px;
}
.vote-badge::before { content: ''; width:6px; height:6px; border-radius:50%; background:var(--green); display:block; animation: pulse 2s infinite; }
@keyframes pulse { 0%, 100%{opacity:1;transform:scale(1)} 50%{opacity:.5;transform:scale(1.3)} }

.vote-title {
  font-size: clamp(1.6rem, 4vw, 2.4rem);
  font-weight: 700; letter-spacing: -.5px;
  color: var(--text);
  margin-bottom: 10px;
}
.vote-desc {
  color: var(--sub); font-size: .92rem; max-width: 480px; margin: 0 auto;
}

/* ── Progress ── */
.progress-bar {
  background: var(--border); border-radius: 4px; height: 3px;
  margin-bottom: 40px; overflow: hidden;
}
.progress-fill {
  height: 100%; background: var(--accent);
  border-radius: 4px; transition: width .4s ease; width: 0;
}
.progress-text {
  text-align: right; font-size: .72rem; color: var(--sub);
  margin-top: 6px; font-family: 'DM Mono', monospace;
}

/* ── Questions ── */
.question-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--r);
  padding: 28px 28px;
  margin-bottom: 16px;
  transition: border-color .2s;
}
.question-card.active { border-color: var(--accent); }
.question-card.answered { border-color: rgba(74,222,128,.3); }

.q-number {
  font-size: .65rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .1em; color: var(--dim); margin-bottom: 6px;
  font-family: 'DM Mono', monospace;
}
.q-text {
  font-size: 1rem; font-weight: 500; color: var(--text);
  margin-bottom: 20px; line-height: 1.5;
}
.q-required { color: var(--red); margin-left: 3px; }

/* ── Input types ── */
.opt-grid { display: flex; flex-direction: column; gap: 8px; }
.opt-item {
  display: flex; align-items: center; gap: 12px;
  background: var(--card2); border: 1px solid var(--border2);
  border-radius: var(--r2); padding: 12px 16px; cursor: pointer;
  transition: all .15s; user-select: none;
}
.opt-item:hover { border-color: var(--accent); background: rgba(91,140,255,.07); }
.opt-item.selected { border-color: var(--accent); background: rgba(91,140,255,.12); }
.opt-item.selected .opt-indicator { background: var(--accent); border-color: var(--accent); }
.opt-item.selected .opt-indicator::after { display: block; }

.opt-indicator {
  width: 18px; height: 18px; border-radius: 50%; flex-shrink: 0;
  border: 2px solid var(--border2);
  background: transparent;
  position: relative; transition: all .15s;
}
.opt-indicator::after {
  content: ''; display: none;
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--bg); position: absolute;
  top: 50%; left: 50%; transform: translate(-50%,-50%);
}
.opt-indicator.checkbox { border-radius: 4px; }
.opt-indicator.checkbox::after {
  content: '✓'; width: auto; height: auto; font-size: 10px;
  background: transparent; color: var(--bg); font-weight: 700;
}

.opt-label { font-size: .9rem; color: var(--text); flex: 1; }

/* ── Person picker ── */
.person-search {
  width: 100%; background: var(--card2); border: 1px solid var(--border2);
  color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .88rem;
  padding: 10px 14px; border-radius: var(--r2); outline: none;
  margin-bottom: 10px; transition: border-color .15s;
}
.person-search:focus { border-color: var(--accent); }
.person-list { max-height: 240px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }

.person-item {
  display: flex; align-items: center; gap: 10px;
  background: var(--card2); border: 1px solid var(--border2);
  border-radius: var(--r2); padding: 9px 14px; cursor: pointer;
  transition: all .15s;
}
.person-item:hover { border-color: var(--accent); }
.person-item.selected { border-color: var(--accent); background: rgba(91,140,255,.1); }
.person-avatar {
  width: 28px; height: 28px; border-radius: 50%; object-fit: cover;
  background: var(--dim); flex-shrink: 0; font-size: .65rem;
  display: flex; align-items: center; justify-content: center;
  color: var(--text); font-weight: 600;
}
.person-name { font-size: .88rem; flex: 1; }

/* ── Rating ── */
.rating-row { display: flex; gap: 8px; flex-wrap: wrap; }
.rating-btn {
  width: 44px; height: 44px; border-radius: var(--r2);
  background: var(--card2); border: 1px solid var(--border2);
  color: var(--sub); font-size: .9rem; font-weight: 600;
  cursor: pointer; transition: all .15s;
  display: flex; align-items: center; justify-content: center;
}
.rating-btn:hover { border-color: var(--accent); color: var(--accent); }
.rating-btn.selected { background: var(--accent); border-color: var(--accent); color: var(--bg); }

/* ── Text input ── */
.text-input {
  width: 100%; background: var(--card2); border: 1px solid var(--border2);
  color: var(--text); font-family: 'DM Sans', sans-serif; font-size: .9rem;
  padding: 12px 14px; border-radius: var(--r2); outline: none;
  resize: vertical; min-height: 100px; transition: border-color .15s;
}
.text-input:focus { border-color: var(--accent); }

/* ── Submit ── */
.submit-section { margin-top: 36px; text-align: center; }
.submit-btn {
  background: var(--accent); color: #000;
  font-family: 'DM Sans', sans-serif; font-weight: 700; font-size: .95rem;
  padding: 14px 40px; border: none; border-radius: var(--r2);
  cursor: pointer; transition: all .2s; letter-spacing: .02em;
}
.submit-btn:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); box-shadow: 0 6px 24px rgba(91,140,255,.3); }
.submit-btn:disabled { opacity: .4; cursor: not-allowed; }

.anon-note {
  margin-top: 16px; font-size: .75rem; color: var(--dim);
  display: flex; align-items: center; justify-content: center; gap: 6px;
}

/* ── Success ── */
.success-overlay {
  display: none; position: fixed; inset: 0; z-index: 999;
  background: var(--bg); align-items: center; justify-content: center;
  flex-direction: column; text-align: center; gap: 20px; padding: 24px;
}
.success-overlay.show { display: flex; }
.success-icon { font-size: 4rem; animation: pop .4s ease; }
@keyframes pop { 0%{transform:scale(.5);opacity:0} 80%{transform:scale(1.1)} 100%{transform:scale(1);opacity:1} }
.success-title { font-size: 1.6rem; font-weight: 700; }
.success-sub { color: var(--sub); font-size: .9rem; }

/* ── Encryption badge ── */
.enc-badge {
  display: inline-flex; align-items: center; gap: 5px;
  background: rgba(251,191,36,.08); border: 1px solid rgba(251,191,36,.2);
  border-radius: 6px; padding: 4px 10px;
  font-size: .70rem; font-weight: 600; color: var(--gold);
  margin-bottom: 24px;
}

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

  <!-- User-Bar -->
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
    {% if voting.description %}
    <p class="vote-desc">{{ voting.description }}</p>
    {% endif %}
    {% if voting.public_key %}
    <div style="margin-top:16px;">
      <span class="enc-badge">🔐 Ende-zu-Ende verschlüsselt</span>
    </div>
    {% endif %}
  </div>

  <div class="progress-bar">
    <div class="progress-fill" id="progressFill" style="width: 0%"></div>
  </div>
  <div class="progress-text" id="progressText">0 / {{ voting.questions | length }} beantwortet</div>

  <form id="voteForm">
  {% for i, frage in enumerate(voting.questions) %}
  <div class="question-card" id="qcard-{{ i }}" data-index="{{ i }}">
    <div class="q-number">Frage {{ i + 1 }} von {{ voting.questions | length }}</div>
    <div class="q-text">
      {{ frage.Frage }}
      {% if frage.get('Pflicht', False) %}<span class="q-required">*</span>{% endif %}
    </div>

    {% if frage.Typ == 'choice' %}
    <div class="opt-grid" id="opts-{{ i }}">
      {% for opt in frage.get('Optionen', []) %}
      <div class="opt-item"
           data-q="{{ i }}" data-val="{{ opt }}"
           data-multi="{{ 'true' if frage.get('Mehrfach', False) else 'false' }}"
           onclick="toggleOpt(this)">
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
    <textarea class="text-input" id="text-{{ i }}" data-q="{{ i }}"
              placeholder="Deine Antwort..." rows="4"
              oninput="updateProgress()"></textarea>

    {% elif frage.Typ == 'person' %}
    <input type="text" class="person-search" id="psearch-{{ i }}"
           placeholder="Name suchen..." oninput="filterPersons({{ i }}, this.value)">
    <div class="person-list" id="plist-{{ i }}">
      <div style="color:var(--sub);font-size:.82rem;padding:8px;">Lädt Mitglieder...</div>
    </div>
    {% endif %}
  </div>
  {% endfor %}
  </form>

  <div class="submit-section">
    <button class="submit-btn" id="submitBtn" onclick="submitVote()">
      Abstimmung abschicken →
    </button>
    <div class="anon-note">
      🔒 Deine Antwort ist vollständig anonym — keine Rückverfolgung möglich
    </div>
  </div>
</div>

<script>
const VOTING_ID  = {{ voting_id | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
const PUBLIC_KEY = {{ (voting.public_key or '') | tojson }};
const HAS_PERSON = QUESTIONS.some(q => q.Typ === 'person');

// ── Answers State ────────────────────────────────────────────────────────────
const answers = {};

function toggleOpt(el) {
  const q     = el.dataset.q;
  const val   = el.dataset.val;
  const multi = el.dataset.multi === 'true';

  if (!multi) {
    document.querySelectorAll(`.opt-item[data-q="${q}"]`).forEach(e => e.classList.remove('selected'));
    answers[q] = val;
  } else {
    el.classList.toggle('selected');
    const selected = Array.from(document.querySelectorAll(`.opt-item[data-q="${q}"].selected`)).map(e => e.dataset.val);
    answers[q] = selected.length > 0 ? selected : undefined;
  }
  el.classList.toggle('selected', !multi || (Array.isArray(answers[q]) && answers[q].includes(val)));
  updateProgress();
}

function selectRating(btn) {
  const q = btn.dataset.q;
  document.querySelectorAll(`.rating-btn[data-q="${q}"]`).forEach(b => b.classList.remove('selected'));
  btn.classList.add('selected');
  answers[q] = parseInt(btn.dataset.val);
  updateProgress();
}

function selectPerson(idx, userId, name) {
  const multi = QUESTIONS[idx]?.Mehrfach || false;
  const item  = document.querySelector(`.person-item[data-uid="${userId}"][data-q="${idx}"]`);

  if (!multi) {
    document.querySelectorAll(`.person-item[data-q="${idx}"]`).forEach(e => e.classList.remove('selected'));
    answers[idx] = {id: userId, name: name};
  } else {
    item?.classList.toggle('selected');
    const sel = Array.from(document.querySelectorAll(`.person-item[data-q="${idx}"].selected`))
                     .map(e => ({id: e.dataset.uid, name: e.dataset.name}));
    answers[idx] = sel.length > 0 ? sel : undefined;
  }
  if (item && !multi) item.classList.add('selected');
  updateProgress();
}

function filterPersons(idx, query) {
  const items = document.querySelectorAll(`.person-item[data-q="${idx}"]`);
  const q     = query.toLowerCase();
  items.forEach(item => {
    const name = item.dataset.name.toLowerCase();
    item.style.display = (!q || name.includes(q)) ? '' : 'none';
  });
}

// ── Progress ─────────────────────────────────────────────────────────────────
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
    if (card) {
      card.classList.toggle('answered', answers[i] !== undefined && answers[i] !== null);
    }
  });
  const pct = QUESTIONS.length ? (answered / QUESTIONS.length * 100) : 0;
  const fill = document.getElementById('progressFill');
  const txt  = document.getElementById('progressText');
  if (fill) fill.style.width = pct + '%';
  if (txt)  txt.textContent = `${answered} / ${QUESTIONS.length} beantwortet`;
}

// ── Encryption ────────────────────────────────────────────────────────────────
async function encryptData(data, pemPublicKey) {
  const encoder   = new TextEncoder();
  const dataBytes = encoder.encode(data);

  const aesKey = await crypto.subtle.generateKey(
    { name: 'AES-GCM', length: 256 }, true, ['encrypt']
  );
  const iv = crypto.getRandomValues(new Uint8Array(12));

  const aesCipherBuf = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv }, aesKey, dataBytes
  );
  const aesCipher = new Uint8Array(aesCipherBuf);
  const aesTag    = aesCipher.slice(-16);
  const ciphertext= aesCipher.slice(0, -16);
  const exportedAesKey = new Uint8Array(await crypto.subtle.exportKey('raw', aesKey));

  const pemBody = pemPublicKey
    .replace(/-----BEGIN PUBLIC KEY-----/, '')
    .replace(/-----END PUBLIC KEY-----/, '')
    .replace(/\s+/g, '');
  const derBuffer = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));

  const rsaKey = await crypto.subtle.importKey(
    'spki', derBuffer.buffer,
    { name: 'RSA-OAEP', hash: 'SHA-256' },
    false, ['encrypt']
  );

  const encAesKey = new Uint8Array(await crypto.subtle.encrypt(
    { name: 'RSA-OAEP' }, rsaKey, exportedAesKey
  ));

  const b64 = buf => btoa(String.fromCharCode(...buf));
  return [b64(encAesKey), b64(aesTag), b64(iv), b64(ciphertext)].join('|');
}

// ── Submit ────────────────────────────────────────────────────────────────────
async function submitVote() {
  const missing = [];
  QUESTIONS.forEach((frage, i) => {
    if (!frage.Pflicht) return;
    if (frage.Typ === 'text') {
      const el = document.getElementById(`text-${i}`);
      if (!el || !el.value.trim()) missing.push(i + 1);
    } else if (answers[i] === undefined || answers[i] === null) {
      missing.push(i + 1);
    }
  });
  if (missing.length) {
    alert(`Bitte beantworte Pflichtfragen: ${missing.join(', ')}`);
    return;
  }

  const btn = document.getElementById('submitBtn');
  btn.disabled = true;
  btn.textContent = 'Wird abgeschickt...';

  QUESTIONS.forEach((frage, i) => {
    if (frage.Typ === 'text') {
      const el = document.getElementById(`text-${i}`);
      if (el && el.value.trim()) answers[i] = el.value.trim();
    }
  });

  const payload  = {};
  const answerObj = {};
  QUESTIONS.forEach((frage, i) => {
    answerObj[frage.Frage] = answers[i] ?? null;
  });

  if (PUBLIC_KEY) {
    try {
      const encrypted = await encryptData(JSON.stringify(answerObj), PUBLIC_KEY);
      payload.encrypted_data = encrypted;
    } catch(e) {
      alert('Verschlüsselung fehlgeschlagen: ' + e.message);
      btn.disabled = false;
      btn.textContent = 'Abstimmung abschicken →';
      return;
    }
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
      if (r.status === 401) {
        // Session abgelaufen → zurück zum Login
        window.location.href = `/vote/${VOTING_ID}/login`;
        return;
      }
      alert(d.error || 'Fehler beim Abschicken');
      btn.disabled = false;
      btn.textContent = 'Abstimmung abschicken →';
      return;
    }
    document.getElementById('successOverlay').classList.add('show');
  } catch(e) {
    alert('Netzwerkfehler: ' + e.message);
    btn.disabled = false;
    btn.textContent = 'Abstimmung abschicken →';
  }
}

// ── Load persons ─────────────────────────────────────────────────────────────
async function loadPersonQuestions() {
  const personQs = QUESTIONS.map((q, i) => ({q, i})).filter(({q}) => q.Typ === 'person');
  if (!personQs.length) return;

  let members = [];
  try {
    const r = await fetch(`/api/vote/${VOTING_ID}/members`);
    if (r.status === 401) {
      // Nicht eingeloggt → Login-Seite
      return;
    }
    const d = await r.json();
    members = d.members || [];
  } catch(e) {
    console.error('Members laden fehlgeschlagen:', e);
  }

  personQs.forEach(({q, i}) => {
    const list = document.getElementById(`plist-${i}`);
    if (!list) return;

    let displayMembers = members;
    if (q.Optionen && q.Optionen !== '--All' && Array.isArray(q.Optionen)) {
      const allowed = new Set(q.Optionen.map(String));
      displayMembers = members.filter(m => allowed.has(m.id));
    }

    if (!displayMembers.length) {
      list.innerHTML = '<div style="color:var(--sub);font-size:.82rem;padding:8px;">Keine Mitglieder gefunden</div>';
      return;
    }

    list.innerHTML = displayMembers.map(m => {
      const initials = (m.display || '?').slice(0, 2).toUpperCase();
      const avatarHtml = m.avatar
        ? `<img class="person-avatar" src="${m.avatar}" alt="">`
        : `<div class="person-avatar">${initials}</div>`;
      return `<div class="person-item" data-q="${i}" data-uid="${m.id}" data-name="${m.display}"
               onclick="selectPerson(${i}, '${m.id}', '${m.display.replace(/'/g,"\\'")}')">
        ${avatarHtml}
        <span class="person-name">${m.display}</span>
      </div>`;
    }).join('');
  });
}

if (HAS_PERSON) loadPersonQuestions();
updateProgress();
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# RESULTS TEMPLATE (unverändert aus Original)
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
.q-title{font-size:1rem;font-weight:500;margin-bottom:18px;}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;}
.bar-label{width:160px;font-size:.85rem;color:var(--sub);flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-track{flex:1;background:var(--border2);border-radius:4px;height:8px;overflow:hidden;}
.bar-fill{height:100%;background:var(--accent);border-radius:4px;transition:width .5s ease;}
.bar-count{font-family:'DM Mono',monospace;font-size:.78rem;color:var(--sub);width:40px;text-align:right;flex-shrink:0;}
.enc-notice{background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.2);border-radius:var(--r2);padding:12px 16px;font-size:.82rem;color:var(--gold);margin-bottom:24px;}
.dec-link{display:inline-flex;align-items:center;gap:6px;color:var(--accent);font-size:.85rem;text-decoration:none;margin-top:8px;}
.dec-link:hover{text-decoration:underline;}
</style>
</head>
<body>
<div class="page">
  <h1>📊 {{ voting.title }}</h1>
  <div class="sub">{{ voting.description }}</div>

  {% if voting.public_key %}
  <div class="enc-notice">
    🔐 Diese Abstimmung ist verschlüsselt. Die Rohdaten können nur mit dem privaten Schlüssel entschlüsselt werden.
    <br><a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">→ Entschlüsselungs-Tool öffnen</a>
  </div>
  {% endif %}

  <div class="stat-row">
    <div class="stat-box">
      <div class="stat-num">{{ responses | length }}</div>
      <div class="stat-lbl">Antworten</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">{{ voting.questions | length }}</div>
      <div class="stat-lbl">Fragen</div>
    </div>
    <div class="stat-box">
      <div class="stat-num">{{ '✅' if not voting.is_active else '🟢' }}</div>
      <div class="stat-lbl">{{ 'Beendet' if not voting.is_active else 'Aktiv' }}</div>
    </div>
  </div>

  {% if not voting.public_key %}
    {% for frage in voting.questions %}
    <div class="q-block" data-q-idx="{{ loop.index0 }}" data-q-type="{{ frage.Typ }}">
      <div class="q-label">Frage {{ loop.index }}</div>
      <div class="q-title">{{ frage.Frage }}</div>
      <div class="q-results" id="qr-{{ loop.index0 }}">
        <div style="color:var(--sub);font-size:.82rem;">Wird berechnet...</div>
      </div>
    </div>
    {% endfor %}
  {% else %}
    <div class="q-block">
      <div class="q-title">Verschlüsselte Antworten ({{ responses|length }})</div>
      <div style="color:var(--sub);font-size:.85rem;">
        Nutze das <a class="dec-link" href="/vote/{{ voting_id }}/reconstruct">Entschlüsselungs-Tool</a>
        um die Antworten mit deinem privaten Schlüssel zu entschlüsseln.
      </div>
    </div>
  {% endif %}
</div>

<script>
const RESPONSES = {{ responses | tojson }};
const QUESTIONS = {{ voting.questions | tojson }};
const IS_ENCRYPTED = {{ ('true' if voting.public_key else 'false') }};

if (!IS_ENCRYPTED && RESPONSES.length) {
  const parsed = RESPONSES.map(r => {
    try { return JSON.parse(r.answers); } catch { return {}; }
  });

  QUESTIONS.forEach((frage, qi) => {
    const container = document.getElementById(`qr-${qi}`);
    if (!container) return;

    const vals = parsed.map(p => p[frage.Frage]).filter(v => v !== null && v !== undefined);

    if (frage.Typ === 'text') {
      const items = vals.filter(Boolean).map(v =>
        `<div style="background:var(--card2);border:1px solid var(--border2);border-radius:var(--r2);padding:10px 14px;font-size:.88rem;margin-bottom:8px;">${esc(String(v))}</div>`
      ).join('');
      container.innerHTML = items || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';

    } else if (frage.Typ === 'rating') {
      const counts = {};
      for (let v = frage.Min || 1; v <= (frage.Max || 5); v++) counts[v] = 0;
      vals.forEach(v => { if (counts[v] !== undefined) counts[v]++; });
      const max = Math.max(...Object.values(counts), 1);
      const avg = vals.length ? (vals.reduce((a, b) => a + Number(b), 0) / vals.length).toFixed(2) : '–';
      container.innerHTML = `<div style="color:var(--sub);font-size:.78rem;margin-bottom:12px;">Ø ${avg}</div>` +
        Object.entries(counts).map(([v, c]) =>
          `<div class="bar-row">
            <div class="bar-label">★ ${v}</div>
            <div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div>
            <div class="bar-count">${c}</div>
          </div>`
        ).join('');

    } else if (frage.Typ === 'choice') {
      const counts = {};
      vals.forEach(v => {
        const items = Array.isArray(v) ? v : [v];
        items.forEach(item => { counts[item] = (counts[item] || 0) + 1; });
      });
      const max = Math.max(...Object.values(counts), 1);
      const sorted = Object.entries(counts).sort((a,b) => b[1]-a[1]);
      container.innerHTML = sorted.map(([label, c]) =>
        `<div class="bar-row">
          <div class="bar-label" title="${esc(label)}">${esc(label)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div>
          <div class="bar-count">${c}</div>
        </div>`
      ).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';

    } else if (frage.Typ === 'person') {
      const counts = {};
      vals.forEach(v => {
        const items = Array.isArray(v) ? v : [v];
        items.forEach(item => {
          const name = item?.name || item || '?';
          counts[name] = (counts[name] || 0) + 1;
        });
      });
      const max = Math.max(...Object.values(counts), 1);
      const sorted = Object.entries(counts).sort((a,b) => b[1]-a[1]);
      container.innerHTML = sorted.map(([name, c]) =>
        `<div class="bar-row">
          <div class="bar-label">${esc(name)}</div>
          <div class="bar-track"><div class="bar-fill" style="width:${c/max*100}%"></div></div>
          <div class="bar-count">${c}</div>
        </div>`
      ).join('') || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>';
    }
  });
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
# RECONSTRUCT TEMPLATE (unverändert aus Original)
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
.warn-box{background:rgba(251,191,36,.07);border:1px solid rgba(251,191,36,.2);border-radius:var(--r2);padding:12px 16px;font-size:.82rem;color:var(--gold);margin-bottom:20px;}
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

  <div class="warn-box">
    ⚠️ Der private Schlüssel verlässt <strong>niemals</strong> deinen Browser.
    Die Entschlüsselung findet vollständig lokal statt.
    Keine Daten werden hochgeladen.
  </div>

  <div class="card">
    <label>Privater RSA-Schlüssel (PEM-Format)</label>
    <textarea class="key-input" id="privateKeyInput"
              placeholder="-----BEGIN RSA PRIVATE KEY-----&#10;...&#10;-----END RSA PRIVATE KEY-----&#10;oder&#10;-----BEGIN PRIVATE KEY-----&#10;...&#10;-----END PRIVATE KEY-----"></textarea>
    <div>
      <button class="btn" id="decryptBtn" onclick="startDecrypt()">
        🔓 Entschlüsseln ({{ responses | length }} Antworten)
      </button>
    </div>
    <div class="progress-text" id="progressText"></div>
  </div>

  <div class="result-container" id="resultContainer">
    <div style="display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap;">
      <button class="dl-btn" onclick="downloadJSON()">⬇️ JSON exportieren</button>
      <button class="dl-btn" onclick="downloadCSV()">⬇️ CSV exportieren</button>
    </div>
    <div id="resultsGrid"></div>
  </div>
</div>

<script>
const RESPONSES  = {{ responses | tojson }};
const QUESTIONS  = {{ voting.questions | tojson }};
let decryptedAll = [];

async function decryptEntry(encStr, privateKey) {
  const parts = encStr.split('|');
  if (parts.length !== 4) throw new Error('Ungültiges Format');
  const [encKeyB64, tagB64, ivB64, cipherB64] = parts;

  const b64ToU8 = b64 => Uint8Array.from(atob(b64), c => c.charCodeAt(0));
  const encAesKey = b64ToU8(encKeyB64);
  const tag       = b64ToU8(tagB64);
  const iv        = b64ToU8(ivB64);
  const ciphertext= b64ToU8(cipherB64);

  const aesKeyBytes = await crypto.subtle.decrypt(
    { name: 'RSA-OAEP' }, privateKey, encAesKey
  );

  const importedAes = await crypto.subtle.importKey(
    'raw', aesKeyBytes, { name: 'AES-GCM' }, false, ['decrypt']
  );
  const combined = new Uint8Array(ciphertext.length + tag.length);
  combined.set(ciphertext); combined.set(tag, ciphertext.length);

  const plainBuf = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv }, importedAes, combined
  );
  return new TextDecoder().decode(plainBuf);
}

async function importPrivateKey(pem) {
  const pemBody = pem
    .replace(/-----BEGIN (?:RSA )?PRIVATE KEY-----/, '')
    .replace(/-----END (?:RSA )?PRIVATE KEY-----/, '')
    .replace(/\s+/g, '');
  const der = Uint8Array.from(atob(pemBody), c => c.charCodeAt(0));
  try {
    return await crypto.subtle.importKey(
      'pkcs8', der.buffer,
      { name: 'RSA-OAEP', hash: 'SHA-256' },
      false, ['decrypt']
    );
  } catch {
    throw new Error('Ungültiger privater Schlüssel – bitte PKCS#8 Format verwenden');
  }
}

async function startDecrypt() {
  const pem = document.getElementById('privateKeyInput').value.trim();
  if (!pem) { alert('Bitte privaten Schlüssel eingeben'); return; }

  const btn = document.getElementById('decryptBtn');
  btn.disabled = true; btn.textContent = 'Entschlüsselt...';

  let privateKey;
  try {
    privateKey = await importPrivateKey(pem);
  } catch(e) {
    alert(e.message);
    btn.disabled = false; btn.textContent = '🔓 Entschlüsseln';
    return;
  }

  const enc  = RESPONSES.filter(r => r.is_encrypted);
  const prog = document.getElementById('progressText');
  decryptedAll = [];
  let failed = 0;

  for (let i = 0; i < enc.length; i++) {
    prog.textContent = `${i+1} / ${enc.length} entschlüsselt...`;
    try {
      const plain = await decryptEntry(enc[i].answers, privateKey);
      decryptedAll.push({ data: JSON.parse(plain), submitted_at: enc[i].submitted_at });
    } catch {
      failed++;
    }
  }

  prog.textContent = `✅ ${decryptedAll.length} entschlüsselt${failed > 0 ? ` (${failed} fehlgeschlagen)` : ''}`;
  renderResults();
  btn.textContent = '🔓 Erneut entschlüsseln';
  btn.disabled = false;
}

function renderResults() {
  const container = document.getElementById('resultsGrid');
  const rc        = document.getElementById('resultContainer');
  rc.classList.add('show');

  const qBlocks = QUESTIONS.map((frage, qi) => {
    const vals = decryptedAll.map(d => d.data[frage.Frage]).filter(v => v !== null && v !== undefined);

    let inner = '';
    if (frage.Typ === 'text') {
      inner = vals.map(v => `<div class="answer-row"><span>${esc(String(v))}</span></div>`).join('');
    } else {
      const counts = {};
      vals.forEach(v => {
        const items = Array.isArray(v) ? v : [v];
        items.forEach(item => {
          const key = item?.name || item || '?';
          counts[key] = (counts[key] || 0) + 1;
        });
      });
      const max = Math.max(...Object.values(counts), 1);
      inner = Object.entries(counts).sort((a,b)=>b[1]-a[1]).map(([k, c]) =>
        `<div class="answer-row"><span>${esc(k)}</span> — ${c}×</div>`
      ).join('');
    }

    return `<div class="q-block">
      <div class="q-label">Frage ${qi+1}</div>
      <div class="q-title">${esc(frage.Frage)}</div>
      ${inner || '<div style="color:var(--sub);font-size:.82rem;">Keine Antworten</div>'}
    </div>`;
  });

  container.innerHTML = qBlocks.join('');
}

function downloadJSON() {
  const blob = new Blob([JSON.stringify(decryptedAll, null, 2)], {type:'application/json'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `abstimmung_ergebnisse.json`; a.click();
}

function downloadCSV() {
  const headers = ['Abgestimmt am', ...QUESTIONS.map(q => q.Frage)];
  const rows = decryptedAll.map(d => [
    d.submitted_at,
    ...QUESTIONS.map(q => {
      const v = d.data[q.Frage];
      if (Array.isArray(v)) return v.map(i => i?.name || i).join('; ');
      return v?.name || v || '';
    })
  ]);
  const csv = [headers, ...rows].map(r => r.map(c => `"${String(c||'').replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob(['\uFEFF' + csv], {type:'text/csv;charset=utf-8'});
  const a = document.createElement('a'); a.href = URL.createObjectURL(blob);
  a.download = `abstimmung_ergebnisse.csv`; a.click();
}

function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
</script>
</body>
</html>"""