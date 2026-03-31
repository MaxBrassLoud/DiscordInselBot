"""
keep_alive.py  –  Flask-basiertes Web-Dashboard (ersetzt aiohttp)
==================================================================
Flask läuft in einem Daemon-Thread parallel zum asyncio Event-Loop
von discord.py. Kein SSL-Zertifikat-Overhead, kein aiohttp mehr.

Verzeichnisstruktur nach dieser Migration:
  bot/core/web_app/flask_app/
  ├── app.py           – Flask App + alle Routes
  ├── templates/       – Jinja2 HTML-Templates
  │   ├── index.html       – Homepage mit Feature-Übersicht
  │   ├── login.html       – Discord OAuth2 Login
  │   ├── dashboard.html   – Ticket- und Bewerbungsliste
  │   ├── ticket_view.html – Ticket-Detailansicht
  │   ├── application_view.html – Bewerbungs-Detailansicht
  │   └── error.html       – 403/404 Fehlerseite
  └── static/
      └── css/main.css – Design System (Grün-Akzente, Dark Mode, Mobile)

Benötigte .env Variablen:
    DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_GUILD_ID
    DISCORD_ALLOWED_ROLE_IDS, DISCORD_TOKEN
    FLASK_SECRET_KEY
    WEB_BASE_URL        (z.B. https://meinbot.de)
    WEB_PORT            (optional, Standard: 5000)
    SUPABASE_URL, SUPABASE_KEY
    MBL                 (Discord-User-ID für Superadmin)
"""

from __future__ import annotations
import asyncio
import logging
import os
import sys
import threading

log = logging.getLogger("web")


def _start_flask() -> None:
    """Import + start Flask app in the current thread (blocking)."""
    try:
        # Ensure project root is in path
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../"))
        if root not in sys.path:
            sys.path.insert(0, root)

        from bot.core.web_app.flask_app.app import app

        port = int(os.getenv("WEB_PORT", 5000))
        log.info(f"✅ Web-Dashboard läuft auf http://0.0.0.0:{port}")
        app.run(
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
            threaded=True,
        )
    except Exception as e:
        log.error(f"[web] Flask-Start fehlgeschlagen: {e}")


async def keep_alive() -> None:
    """
    Starte Flask in einem Daemon-Thread.
    Gibt sofort zurück – blockiert den asyncio-Loop nicht.
    """
    t = threading.Thread(
        target=_start_flask,
        daemon=True,
        name="flask-dashboard",
    )
    t.start()
    # Kurz yielden damit der Thread starten kann
    await asyncio.sleep(0.1)


async def build_app():
    """Kompatibilitäts-Shim – gibt das Flask App-Objekt zurück."""
    from bot.core.web_app.flask_app.app import app
    return app


__all__ = ["keep_alive", "build_app"]