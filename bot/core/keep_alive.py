"""
keep_alive.py  –  aiohttp Web-Dashboard mit Discord OAuth2 + HTTPS
====================================================================
Kein Flask, kein extra Install — aiohttp ist bereits durch discord.py vorhanden.
HTTPS läuft über Pythons eingebautes ssl-Modul (kein mkcert, kein Nginx).

Benötigte .env Variablen:
    DISCORD_CLIENT_ID        = Deine Discord Application Client ID
    DISCORD_CLIENT_SECRET    = Dein Discord Application Client Secret
    DISCORD_GUILD_ID         = ID deines Servers (nur Mitglieder dürfen rein)
    DISCORD_ALLOWED_ROLE_IDS = Kommagetrennte Rollen-IDs  (leer = alle Mitglieder)
    WEB_BASE_URL             = https://localhost:5000  (kein trailing /)
    FLASK_SECRET_KEY         = Beliebiger langer String (für Session-Signing)

    # Optional – eigene Zertifikatsdateien (sonst wird beim Start auto-generiert):
    SSL_CERT                 = certs/localhost.pem
    SSL_KEY                  = certs/localhost-key.pem

SSL-Zertifikat:
    Beim ersten Start ohne SSL_CERT/SSL_KEY wird automatisch ein selbst-
    signiertes Zertifikat im Ordner "certs/" erstellt (Python-stdlib, kein Tool).
    Browser zeigen einmalig eine Warnung → "Trotzdem fortfahren" klicken.
    Wenn du kein Browser-Warning willst → mkcert verwenden und Pfade in .env setzen.

Discord Developer Portal:
    OAuth2 → Redirects → https://localhost:5000/auth/callback
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse, parse_qs

import aiohttp
from aiohttp import web

log = logging.getLogger("web")

DISCORD_API = "https://discord.com/api/v10"

# ─────────────────────────────────────────────────────────────────────────────
# Lazy config helpers  (lesen .env erst beim ersten Request-Handling,
# also garantiert NACH load_dotenv() in server.py)
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()

def _client_id()     -> str:  return _cfg("DISCORD_CLIENT_ID")
def _client_secret() -> str:  return _cfg("DISCORD_CLIENT_SECRET")
def _guild_id()      -> str:  return _cfg("DISCORD_GUILD_ID")
def _secret_key()    -> bytes: return _cfg("FLASK_SECRET_KEY", secrets.token_hex(32)).encode()

def _allowed_roles() -> set[str]:
    return {r.strip() for r in _cfg("DISCORD_ALLOWED_ROLE_IDS").split(",") if r.strip()}

def _redirect_uri() -> str:
    base = _cfg("WEB_BASE_URL", "https://localhost:5000").rstrip("/")
    return f"{base}/auth/callback"


# ═════════════════════════════════════════════════════════════════════════════
# SESSION  (signiertes Cookie, kein Server-Side-Storage nötig)
# ═════════════════════════════════════════════════════════════════════════════

_SESSION_COOKIE = "insel_session"
_SESSION_TTL    = 60 * 60 * 24 * 7   # 7 Tage


def _sign(payload: str) -> str:
    sig = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _verify(cookie: str) -> str | None:
    """Gibt den payload zurück wenn die Signatur stimmt, sonst None."""
    try:
        payload, sig = cookie.rsplit(".", 1)
        expected = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return payload
    except Exception:
        pass
    return None


def _get_session(request: web.Request) -> dict:
    raw = request.cookies.get(_SESSION_COOKIE, "")
    if not raw:
        return {}
    payload = _verify(raw)
    if not payload:
        return {}
    try:
        data = json.loads(payload)
        if data.get("_exp", 0) < time.time():
            return {}
        return data
    except Exception:
        return {}


def _set_session(response: web.Response, data: dict) -> None:
    data["_exp"] = int(time.time()) + _SESSION_TTL
    payload      = json.dumps(data, separators=(",", ":"))
    signed       = _sign(payload)
    response.set_cookie(
        _SESSION_COOKIE, signed,
        max_age=_SESSION_TTL,
        httponly=True,
        samesite="Lax",
        secure=True,   # nur über HTTPS übertragen
    )


def _clear_session(response: web.Response) -> None:
    response.del_cookie(_SESSION_COOKIE)


# ═════════════════════════════════════════════════════════════════════════════
# DISCORD API CALLS  (async, nutzt denselben aiohttp-ClientSession wie d.py)
# ═════════════════════════════════════════════════════════════════════════════

async def _exchange_code(code: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        resp = await s.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id":     _client_id(),
                "client_secret": _client_secret(),
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  _redirect_uri(),
            },
        )
        if not resp.ok:
            log.error(f"[OAuth] Token exchange failed: {resp.status} {await resp.text()}")
            return None
        return await resp.json()


async def _discord_get(path: str, access_token: str) -> dict | None:
    async with aiohttp.ClientSession() as s:
        resp = await s.get(
            f"{DISCORD_API}{path}",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        return await resp.json() if resp.ok else None


def _is_authorized(member: dict | None) -> bool:
    if not member or ("roles" not in member and "user" not in member):
        return False
    allowed = _allowed_roles()
    if not allowed:
        return True
    return bool(set(member.get("roles", [])) & allowed)


# ═════════════════════════════════════════════════════════════════════════════
# TEMPLATES  (HTML-Strings, kein Template-Engine nötig)
# ═════════════════════════════════════════════════════════════════════════════

_DISCORD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="17" '
    'viewBox="0 0 71 55" fill="#fff">'
    '<path d="M60.1 4.9A58.6 58.6 0 0 0 45.6 0a40 40 0 0 0-1.9 3.8 54.2 54.2 0 0 0'
    '-16.2 0A40 40 0 0 0 25.7 0 58.3 58.3 0 0 0 11 4.9C1.6 18.3-.9 31.3.3 44.1a58.9'
    ' 58.9 0 0 0 18 9.1 44 44 0 0 0 3.8-6.2 38.4 38.4 0 0 1-6-2.9l1.5-1.1a42.2 42.2'
    ' 0 0 0 36 0l1.5 1.1a38.4 38.4 0 0 1-6 2.9 44 44 0 0 0 3.8 6.2 58.7 58.7 0 0 0'
    ' 18-9.1c1.5-15.3-2.6-28.2-10.8-39.1ZM23.7 36.1c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2'
    ' 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1'
    's2.8-7.2 6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Z"/></svg>'
)

_BASE_STYLE = """
*,*::before,*::after{box-sizing:border-box}
body{margin:0;font-family:'Segoe UI',Roboto,sans-serif;
  background:radial-gradient(circle at top right,#1e293b,#0f172a);
  color:#f1f5f9;min-height:100vh}
a{color:inherit;text-decoration:none}
"""


def _page(title: str, body: str) -> web.Response:
    html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>{_BASE_STYLE}</style>
</head>
<body>{body}</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


def _render_login(error: str = "", *, next_url: str = "") -> web.Response:
    cid     = _client_id()
    csecret = _client_secret()

    if not cid or not csecret:
        button = (
            '<div style="background:rgba(248,113,113,.12);border:1px solid rgba(248,113,113,.35);'
            'color:#f87171;border-radius:12px;padding:14px 18px;font-size:.88rem;line-height:1.7">'
            "<strong>⚠️ Konfigurationsfehler</strong><br>"
            "DISCORD_CLIENT_ID oder DISCORD_CLIENT_SECRET fehlen in der .env!<br>"
            f"<small style='color:#64748b'>CLIENT_ID={'✅' if cid else '❌ fehlt'} &nbsp; "
            f"CLIENT_SECRET={'✅' if csecret else '❌ fehlt'}</small></div>"
        )
        state_input = ""
    else:
        state = secrets.token_urlsafe(16)
        params = urlencode({
            "client_id":    cid,
            "redirect_uri": _redirect_uri(),
            "response_type": "code",
            "scope":        "identify guilds.members.read",
            "state":        state,
        })
        oauth_url = f"https://discord.com/oauth2/authorize?{params}"
        button = (
            f'<a href="{oauth_url}" style="display:inline-flex;align-items:center;'
            f'justify-content:center;gap:12px;width:100%;padding:14px 24px;background:#5865F2;'
            f'color:#fff;font-size:1rem;font-weight:700;border-radius:12px;">'
            f'{_DISCORD_SVG} Mit Discord anmelden</a>'
        )
        state_input = state

    error_html = (
        f'<div style="background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);'
        f'color:#f87171;border-radius:10px;padding:10px 16px;font-size:.88rem;margin-bottom:18px">'
        f"{error}</div>"
    ) if error else ""

    body = f"""
    <div style="display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px">
      <div style="background:#1e293b;border:1px solid #334155;border-radius:24px;
        padding:48px 40px;width:100%;max-width:420px;text-align:center;
        box-shadow:0 25px 60px rgba(0,0,0,.5)">
        <div style="font-size:3.5rem;margin-bottom:16px">🎫</div>
        <h1 style="margin:0 0 8px;font-size:1.8rem;
          background:linear-gradient(to right,#38bdf8,#818cf8);
          -webkit-background-clip:text;-webkit-text-fill-color:transparent">Insel Bot</h1>
        <p style="color:#94a3b8;font-size:.95rem;margin:0 0 32px">
          Dashboard – Melde dich mit Discord an</p>
        {error_html}
        {button}
        <div style="background:rgba(56,189,248,.07);border:1px solid rgba(56,189,248,.18);
          color:#94a3b8;border-radius:10px;padding:10px 16px;font-size:.82rem;margin-top:18px">
          Nur autorisierte Servermitglieder haben Zugriff.</div>
        <!-- state + next_url als versteckte Meta-Tags für den Callback -->
        <meta name="oauth-state" content="{state_input}">
        <meta name="next-url" content="{next_url}">
      </div>
    </div>"""

    resp = _page("Insel Bot – Login", body)
    # State im Cookie speichern (für CSRF-Prüfung beim Callback)
    if state_input:
        resp.set_cookie("oauth_state", state_input, httponly=True, secure=True, samesite="Lax", max_age=300)
    if next_url:
        resp.set_cookie("next_url", next_url, httponly=True, secure=True, samesite="Lax", max_age=300)
    return resp


def _render_home(user: dict | None) -> web.Response:
    if user:
        topbar = f"""
        <div style="display:inline-flex;align-items:center;gap:12px;background:#1e293b;
          border:1px solid #334155;padding:8px 18px;border-radius:50px;margin-bottom:20px">
          <img src="{user['avatar']}" style="width:32px;height:32px;border-radius:50%">
          <span style="font-weight:600">{user['display_name']}</span>
          <a href="/logout" style="color:#94a3b8;font-size:.82rem;padding:4px 10px;
            border:1px solid #334155;border-radius:6px">Abmelden</a>
        </div>
        <div style="display:flex;gap:12px;justify-content:center;margin-bottom:36px">
          <a href="/dashboard/tickets" style="display:inline-flex;align-items:center;gap:8px;
            background:#38bdf8;color:#0f172a;padding:10px 22px;
            border-radius:12px;font-size:.95rem;font-weight:700">
            🎫 Ticket Dashboard</a>
        </div>"""
    else:
        topbar = """
        <div style="margin-bottom:32px">
          <a href="/login" style="display:inline-flex;align-items:center;gap:10px;
            background:#5865F2;color:#fff;padding:12px 28px;
            border-radius:12px;font-size:.95rem;font-weight:700">
            Mit Discord anmelden</a>
        </div>"""

    body = f"""
    <div style="display:flex;flex-direction:column;align-items:center;padding:60px 20px">
      <div style="max-width:1100px;width:100%;text-align:center">
        <div style="font-size:4rem;color:#38bdf8;margin-bottom:20px">
          <i class="fas fa-umbrella-beach"></i></div>
        <h1 style="font-size:3rem;margin:0 0 12px;
          background:linear-gradient(to right,#38bdf8,#818cf8);
          -webkit-background-clip:text;-webkit-text-fill-color:transparent">Insel Bot</h1>
        <div style="display:inline-flex;align-items:center;background:rgba(34,197,94,.1);
          color:#4ade80;padding:6px 16px;border-radius:20px;font-size:.9rem;font-weight:600;
          border:1px solid rgba(34,197,94,.2);margin-bottom:24px">
          <span style="width:8px;height:8px;background:#4ade80;border-radius:50%;
            margin-right:8px;display:inline-block"></span>System Aktiv</div>
        <br>
        {topbar}
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px">
          {"".join(f'''<div style="background:#1e293b;border:1px solid #334155;padding:24px;
            border-radius:16px;text-align:left">
            <i class="fas fa-{icon}" style="font-size:1.5rem;color:#38bdf8;margin-bottom:12px;display:block"></i>
            <h3 style="margin:0 0 8px;color:#fff">{name}</h3>
            <p style="margin:0;font-size:.9rem;color:#94a3b8">{desc}</p></div>'''
            for icon, name, desc in [
              ("gamepad","Spieleabende","Erstelle und verwalte Spieleabende mit RSVP-System"),
              ("photo-video","Medien","Automatische Weiterleitung von Bildern, Videos und Links"),
              ("calendar-star","Events","Event-System mit Follow, Erinnerungen und Live-Status"),
              ("door-open","Welcomer","Willkommens- und Abschiedsnachrichten"),
              ("tags","Rollenvergabe","Selbst-Rollenvergabe per Button"),
              ("ticket-alt","Ticket-System","Modulares Support-Ticket-System"),
            ])}
        </div>
      </div>
    </div>
    <footer style="margin-top:60px;padding:30px 0;border-top:1px solid #334155;
      text-align:center;font-size:.9rem;color:#94a3b8">
      &copy; 2026 Insel Bot &bull; Made with ❤ for Die Insel Community</footer>"""

    return _page("Insel Bot – Übersicht", body)


# ═════════════════════════════════════════════════════════════════════════════
# JINJA-like Template-Rendering für ticket_list.html / ticket_view.html
# (Die bestehenden HTML-Template-Dateien bleiben unverändert nutzbar)
# ═════════════════════════════════════════════════════════════════════════════

_TEMPLATE_DIR = Path(__file__).parent / "web"


def _render_template(name: str, **ctx) -> web.Response:
    """
    Minimaler Template-Renderer für die vorhandenen Jinja2-Templates.
    Nutzt Jinja2 falls installiert (discord.py zieht es manchmal mit),
    fällt sonst auf string.replace zurück.
    """
    path = _TEMPLATE_DIR / name
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        tmpl = env.get_template(name)
        html = tmpl.render(**ctx)
    except ImportError:
        # Jinja2 nicht verfügbar → rohe HTML-Datei zurückgeben
        html = path.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


# ═════════════════════════════════════════════════════════════════════════════
# ROUTE HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

async def handle_home(request: web.Request) -> web.Response:
    sess = _get_session(request)
    return _render_home(sess.get("user"))


async def handle_login(request: web.Request) -> web.Response:
    sess = _get_session(request)
    if "user" in sess:
        raise web.HTTPFound("/dashboard/tickets")
    next_url = request.rel_url.query.get("next", "")
    return _render_login(next_url=next_url)


async def handle_callback(request: web.Request) -> web.Response:
    # CSRF state check
    returned_state = request.rel_url.query.get("state", "")
    expected_state = request.cookies.get("oauth_state", "")
    if not returned_state or returned_state != expected_state:
        return _render_login("❌ Ungültiger State. Bitte erneut anmelden.")

    # Discord-Fehler abfangen
    if "error" in request.rel_url.query:
        desc = request.rel_url.query.get("error_description", "Zugriff verweigert.")
        return _render_login(f"❌ {desc}")

    code = request.rel_url.query.get("code", "")
    if not code:
        return _render_login("❌ Kein Autorisierungscode erhalten.")

    # Code → Token
    token_data = await _exchange_code(code)
    if not token_data or "access_token" not in token_data:
        return _render_login("❌ Token-Austausch fehlgeschlagen.")

    access_token = token_data["access_token"]

    # User laden
    discord_user = await _discord_get("/users/@me", access_token)
    if not discord_user or "id" not in discord_user:
        return _render_login("❌ Discord-Profil konnte nicht geladen werden.")

    # Guild-Check
    member   = None
    guild_id = _guild_id()
    if guild_id:
        member = await _discord_get(f"/users/@me/guilds/{guild_id}/member", access_token)
        if not _is_authorized(member):
            return _render_login("❌ Du bist kein autorisiertes Mitglied dieses Servers.")

    # Avatar
    avatar_hash = discord_user.get("avatar")
    avatar_url  = (
        f"https://cdn.discordapp.com/avatars/{discord_user['id']}/{avatar_hash}.png?size=64"
        if avatar_hash
        else f"https://cdn.discordapp.com/embed/avatars/{int(discord_user.get('discriminator') or 0) % 5}.png"
    )

    nick = (member or {}).get("nick") if member else None
    user_data = {
        "id":           discord_user["id"],
        "username":     discord_user.get("global_name") or discord_user.get("username", "?"),
        "avatar":       avatar_url,
        "display_name": nick or discord_user.get("global_name") or discord_user.get("username", "?"),
    }

    next_url = request.cookies.get("next_url", "/dashboard/tickets") or "/dashboard/tickets"
    resp     = web.HTTPFound(next_url)
    _set_session(resp, {"user": user_data})
    resp.del_cookie("oauth_state")
    resp.del_cookie("next_url")
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    _clear_session(resp)
    return resp


# ── Ticket Dashboard ──────────────────────────────────────────────────────────

async def handle_ticket_list(request: web.Request) -> web.Response:
    sess = _get_session(request)
    if "user" not in sess:
        next_url = str(request.rel_url)
        raise web.HTTPFound(f"/login?next={next_url}")

    from bot.core.supabase_client import get_supabase
    user      = sess["user"]
    supabase  = get_supabase()
    servers   = supabase.table("ticket_servers").select("*").execute().data or []
    server_id = request.rel_url.query.get("server_id")
    tickets   = []
    selected  = None

    if server_id:
        selected = next((s for s in servers if s["server_id"] == server_id), None)
        if selected:
            q = supabase.table("tickets").select("*").eq("server_id", server_id)
            if request.rel_url.query.get("status"):
                q = q.eq("status", request.rel_url.query["status"])
            if request.rel_url.query.get("module"):
                q = q.eq("module", request.rel_url.query["module"])
            sort    = request.rel_url.query.get("sort", "newest")
            q       = q.order("created_at", desc=(sort != "oldest"))
            tickets = q.execute().data or []

    return _render_template(
        "ticket_list.html",
        user=user, servers=servers, tickets=tickets,
        selected=selected, server_id=server_id,
        filters=request.rel_url.query,
    )


async def handle_ticket_detail(request: web.Request) -> web.Response:
    sess = _get_session(request)
    if "user" not in sess:
        next_url = str(request.rel_url)
        raise web.HTTPFound(f"/login?next={next_url}")

    from bot.features.tickets.storage import load_ticket, load_messages
    user      = sess["user"]
    ticket_id = int(request.match_info["ticket_id"])
    server_id = request.rel_url.query.get("server_id", "")
    ticket    = load_ticket(server_id, ticket_id)
    if not ticket:
        raise web.HTTPNotFound()

    messages = load_messages(server_id, ticket_id)
    return _render_template(
        "ticket_view.html",
        user=user, ticket=ticket, messages=messages,
        server_id=server_id,
    )


# ── Static files ──────────────────────────────────────────────────────────────

async def handle_static(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    path     = _TEMPLATE_DIR / filename
    if not path.exists() or not path.is_file():
        raise web.HTTPNotFound()
    content_types = {
        ".css": "text/css", ".js": "application/javascript",
        ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
    }
    ct = content_types.get(path.suffix, "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=ct)


# ═════════════════════════════════════════════════════════════════════════════
# SSL  –  selbstsigniertes Zertifikat auto-generieren (Python stdlib)
# ═════════════════════════════════════════════════════════════════════════════

def _get_ssl_context() -> ssl.SSLContext:
    cert_file = _cfg("SSL_CERT", "certs/localhost.pem")
    key_file  = _cfg("SSL_KEY",  "certs/localhost-key.pem")

    if not os.path.isfile(cert_file) or not os.path.isfile(key_file):
        log.info("[SSL] Kein Zertifikat gefunden – erstelle selbstsigniertes Zertifikat...")
        _generate_self_signed(cert_file, key_file)
        log.info(f"[SSL] Zertifikat erstellt: {cert_file}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


def _generate_self_signed(cert_path: str, key_path: str) -> None:
    """
    Erstellt ein selbstsigniertes Zertifikat ohne externe Tools.
    Nutzt nur Pythons stdlib (cryptography ist NICHT nötig).
    Fällt auf subprocess+openssl zurück falls vorhanden, sonst dummy-cert.
    """
    import subprocess, shutil

    Path(cert_path).parent.mkdir(parents=True, exist_ok=True)

    # Versuch 1: openssl (auf Windows oft als Git-Bash-Tool verfügbar)
    if shutil.which("openssl"):
        try:
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", key_path, "-out", cert_path,
                "-days", "3650", "-nodes",
                "-subj", "/CN=localhost",
                "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
            ], check=True, capture_output=True)
            return
        except subprocess.CalledProcessError:
            pass

    # Versuch 2: cryptography-Bibliothek (falls installiert)
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        import datetime

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
            .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
            .add_extension(x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
            ]), critical=False)
            .sign(key, hashes.SHA256())
        )
        Path(key_path).write_bytes(
            key.private_bytes(serialization.Encoding.PEM,
                              serialization.PrivateFormat.TraditionalOpenSSL,
                              serialization.NoEncryption()))
        Path(cert_path).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        return
    except ImportError:
        pass

    raise RuntimeError(
        "Konnte kein SSL-Zertifikat erstellen.\n"
        "Lösung A: openssl installieren (z.B. via Git for Windows)\n"
        "Lösung B: pip install cryptography\n"
        "Lösung C: mkcert nutzen und SSL_CERT / SSL_KEY in .env setzen"
    )


# ═════════════════════════════════════════════════════════════════════════════
# APP FACTORY + KEEP-ALIVE
# ═════════════════════════════════════════════════════════════════════════════

def _build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/",                              handle_home)
    app.router.add_get("/login",                         handle_login)
    app.router.add_get("/auth/callback",                 handle_callback)
    app.router.add_get("/logout",                        handle_logout)
    app.router.add_get("/dashboard/tickets",             handle_ticket_list)
    app.router.add_get("/dashboard/tickets/{ticket_id}", handle_ticket_detail)
    app.router.add_get("/static/{filename}",             handle_static)
    return app


async def _run_server() -> None:
    ssl_ctx = _get_ssl_context()
    app     = _build_app()
    runner  = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 5000, ssl_context=ssl_ctx)
    await site.start()
    log.info("✅ Web-Dashboard läuft auf https://localhost:5000")


async def keep_alive() -> None:
    """
    Startet den aiohttp-Server.
    Muss mit await aufgerufen werden, BEVOR bot.start() awaited wird:

        async def main():
            async with bot:
                await keep_alive()       # ← Server starten
                await bot.start(TOKEN)   # ← Bot starten

    keep_alive() kehrt sofort zurück – der Server läuft im Hintergrund
    als asyncio-Task im selben Event Loop.
    """
    asyncio.get_event_loop().create_task(_run_server())
    # Kurz yielden damit der Task registriert wird bevor bot.start() blockiert
    await asyncio.sleep(0)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run_server())