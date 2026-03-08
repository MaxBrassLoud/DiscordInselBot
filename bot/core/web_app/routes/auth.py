"""
web_app/routes/auth.py  –  Discord OAuth2 login / callback / logout
"""
from __future__ import annotations
import secrets
from urllib.parse import urlencode

from aiohttp import web

from ..session    import get_session, set_session, clear_session
from ..discord_api import (
    client_id, client_secret, redirect_uri, exchange_code,
    discord_get, is_authorized, guild_id, guild_icon_url,
    member_display_name, member_avatar_url,
)

_DISCORD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="17" viewBox="0 0 71 55" fill="#fff">'
    '<path d="M60.1 4.9A58.6 58.6 0 0 0 45.6 0a40 40 0 0 0-1.9 3.8 54.2 54.2 0 0 0-16.2 0A40 40 0 0 0 '
    '25.7 0 58.3 58.3 0 0 0 11 4.9C1.6 18.3-.9 31.3.3 44.1a58.9 58.9 0 0 0 18 9.1 44 44 0 0 0 3.8-6.2 '
    '38.4 38.4 0 0 1-6-2.9l1.5-1.1a42.2 42.2 0 0 0 36 0l1.5 1.1a38.4 38.4 0 0 1-6 2.9 44 44 0 0 0 3.8 '
    '6.2 58.7 58.7 0 0 0 18-9.1c1.5-15.3-2.6-28.2-10.8-39.1ZM23.7 36.1c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 '
    '6.4-7.2 6.5 3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Zm23.6 0c-3.5 0-6.4-3.2-6.4-7.1s2.8-7.2 6.4-7.2 6.5 '
    '3.2 6.4 7.2c0 3.9-2.8 7.1-6.4 7.1Z"/></svg>'
)


def _login_page(error: str = "", next_url: str = "") -> web.Response:
    cid     = client_id()
    csecret = client_secret()

    if not cid or not csecret:
        button = (
            '<div class="auth-error">'
            '<strong>⚠️ Konfigurationsfehler</strong><br>'
            f'CLIENT_ID: {"✅" if cid else "❌ fehlt"} &nbsp; CLIENT_SECRET: {"✅" if csecret else "❌ fehlt"}'
            '</div>'
        )
        state_val = ""
    else:
        state_val = secrets.token_urlsafe(16)
        params    = urlencode({
            "client_id":     cid,
            "redirect_uri":  redirect_uri(),
            "response_type": "code",
            "scope":         "identify guilds.members.read",
            "state":         state_val,
        })
        oauth_url = f"https://discord.com/oauth2/authorize?{params}"
        button = (
            f'<a href="{oauth_url}" class="discord-btn">'
            f'{_DISCORD_SVG} Mit Discord anmelden</a>'
        )

    error_html = f'<div class="auth-error">{error}</div>' if error else ""

    html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Insel Bot – Login</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/dashboard.css">
</head>
<body class="login-body">
  <div class="login-bg">
    <div class="login-grid"></div>
    <div class="login-glow"></div>
  </div>
  <div class="login-card">
    <div class="login-icon">🎫</div>
    <h1 class="login-title">Insel Bot</h1>
    <p class="login-sub">Dashboard · Nur für autorisierte Mitglieder</p>
    {error_html}
    {button}
    <div class="login-note">
      <span class="dot"></span>
      Nur autorisierte Servermitglieder erhalten Zugang
    </div>
  </div>
  <meta name="oauth-state" content="{state_val}">
</body>
</html>"""

    resp = web.Response(text=html, content_type="text/html")
    if state_val:
        resp.set_cookie("oauth_state", state_val, httponly=True, secure=True, samesite="Lax", max_age=300)
    if next_url:
        resp.set_cookie("next_url", next_url, httponly=True, secure=True, samesite="Lax", max_age=300)
    return resp


async def handle_login(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" in sess:
        raise web.HTTPFound("/dashboard")
    next_url = request.rel_url.query.get("next", "")
    return _login_page(next_url=next_url)


async def handle_callback(request: web.Request) -> web.Response:
    returned_state = request.rel_url.query.get("state", "")
    expected_state = request.cookies.get("oauth_state", "")
    if not returned_state or returned_state != expected_state:
        return _login_page("❌ Ungültiger State. Bitte erneut anmelden.")

    if "error" in request.rel_url.query:
        desc = request.rel_url.query.get("error_description", "Zugriff verweigert.")
        return _login_page(f"❌ {desc}")

    code = request.rel_url.query.get("code", "")
    if not code:
        return _login_page("❌ Kein Autorisierungscode erhalten.")

    token_data = await exchange_code(code)
    if not token_data or "access_token" not in token_data:
        return _login_page("❌ Token-Austausch fehlgeschlagen.")

    access_token  = token_data["access_token"]
    discord_user  = await discord_get("/users/@me", access_token)
    if not discord_user or "id" not in discord_user:
        return _login_page("❌ Discord-Profil konnte nicht geladen werden.")

    member   = None
    gid      = guild_id()
    if gid:
        member = await discord_get(f"/users/@me/guilds/{gid}/member", access_token)
        if not is_authorized(member):
            return _login_page("❌ Du bist kein autorisiertes Mitglied dieses Servers.")

    avatar_hash = discord_user.get("avatar")
    avatar_url  = (
        f"https://cdn.discordapp.com/avatars/{discord_user['id']}/{avatar_hash}.png?size=64"
        if avatar_hash
        else f"https://cdn.discordapp.com/embed/avatars/{int(discord_user.get('discriminator') or 0) % 5}.png"
    )
    nick         = (member or {}).get("nick") if member else None
    member_roles = (member or {}).get("roles", []) if member else []
    user_data    = {
        "id":           discord_user["id"],
        "username":     discord_user.get("global_name") or discord_user.get("username", "?"),
        "avatar":       avatar_url,
        "display_name": nick or discord_user.get("global_name") or discord_user.get("username", "?"),
        "guild_id":     gid,
        "roles":        member_roles,
    }

    next_url = request.cookies.get("next_url", "/dashboard") or "/dashboard"
    resp     = web.HTTPFound(next_url)
    set_session(resp, {"user": user_data})
    resp.del_cookie("oauth_state")
    resp.del_cookie("next_url")
    return resp


async def handle_logout(request: web.Request) -> web.Response:
    resp = web.HTTPFound("/login")
    clear_session(resp)
    return resp