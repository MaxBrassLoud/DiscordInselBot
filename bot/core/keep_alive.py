"""
bot/core/keep_alive.py
========================
FIXES:
  - [CRITICAL] keep_alive() war async und nutzte await asyncio.sleep()
    im falschen Kontext. Der Flask-Thread teilt keinen Event-Loop
    mit dem Discord-Bot. Jetzt: sync Thread-Start, kein await nötig.
"""

from __future__ import annotations
import logging
import os
import sys
import threading

log = logging.getLogger("web")


def _start_flask() -> None:
    """Import + start Flask app in the current thread (blocking)."""
    try:
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


def keep_alive() -> None:
    """
    Starte Flask in einem Daemon-Thread.
    SYNC – kein async/await nötig oder korrekt hier.
    """
    t = threading.Thread(
        target=_start_flask,
        daemon=True,
        name="flask-dashboard",
    )
    t.start()
    log.info("[web] Flask-Thread gestartet")


async def keep_alive_async() -> None:
    """
    Async-Wrapper für server.py (await keep_alive_async()).
    Startet den sync Thread und kehrt sofort zurück.
    """
    import asyncio
    keep_alive()
    await asyncio.sleep(0.1)


async def build_app():
    """Kompatibilitäts-Shim – gibt das Flask App-Objekt zurück."""
    from bot.core.web_app.flask_app.app import app
    return app


__all__ = ["keep_alive", "keep_alive_async", "build_app"]
