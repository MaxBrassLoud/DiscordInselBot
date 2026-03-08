"""
keep_alive.py  –  Entry point (thin wrapper around web_app package)
====================================================================
All logic has been split into:

  bot/core/web_app/
  ├── app.py          – aiohttp app factory + keep_alive()
  ├── session.py      – HMAC-signed cookie sessions
  ├── discord_api.py  – OAuth2 + Bot API helpers + guild cache
  ├── ssl_utils.py    – SSL context / self-signed cert generation
  ├── renderer.py     – Jinja2 template rendering
  ├── middleware.py   – Error handling middleware
  ├── routes/
  │   ├── auth.py     – /login  /auth/callback  /logout
  │   ├── api.py      – /api/members
  │   └── dashboard.py – /dashboard/** (tickets + applications unified)
  ├── templates/      – HTML templates
  └── static/         – CSS / JS assets

Required .env variables:
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_GUILD_ID
    DISCORD_ALLOWED_ROLE_IDS, DISCORD_TOKEN
    WEB_BASE_URL, FLASK_SECRET_KEY
    SSL_CERT (optional), SSL_KEY (optional)
"""

from bot.core.web_app.app import keep_alive, build_app  # noqa: F401

__all__ = ["keep_alive", "build_app"]