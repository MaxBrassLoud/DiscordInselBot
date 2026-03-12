"""
web_app/app.py  –  aiohttp app factory + keep_alive
"""
from __future__ import annotations
import asyncio
import logging

from aiohttp import web

from .middleware            import error_middleware
from .ssl_utils             import get_ssl_context
from .discord_api           import close_session
from .routes.auth           import handle_login, handle_callback, handle_logout
from .routes.api            import handle_api_members
from .routes.dashboard      import (
    handle_dashboard,
    handle_ticket_list, handle_ticket_detail,
    handle_application_list, handle_application_detail,
    handle_static,
)

log = logging.getLogger("web")


async def _on_shutdown(app: web.Application) -> None:
    """Sauber die gemeinsame aiohttp-Session schließen."""
    await close_session()


def build_app() -> web.Application:
    app = web.Application(middlewares=[error_middleware])

    # Shutdown-Handler registrieren
    app.on_shutdown.append(_on_shutdown)

    # Auth
    app.router.add_get("/login",          handle_login)
    app.router.add_get("/auth/callback",  handle_callback)
    app.router.add_get("/logout",         handle_logout)

    # Dashboard – unified
    app.router.add_get("/",               handle_dashboard)
    app.router.add_get("/dashboard",      handle_dashboard)
    app.router.add_get("/dashboard/tickets",                   handle_ticket_list)
    app.router.add_get("/dashboard/tickets/{ticket_id}",       handle_ticket_detail)
    app.router.add_get("/dashboard/applications",              handle_application_list)
    app.router.add_get("/dashboard/applications/{app_id}",     handle_application_detail)

    # API
    app.router.add_get("/api/members",    handle_api_members)

    # Static
    app.router.add_get("/static/{filename}", handle_static)

    return app


async def _run_server() -> None:
    ssl_ctx = get_ssl_context()
    app     = build_app()
    runner  = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 5000, ssl_context=ssl_ctx)
    await site.start()
    log.info("✅ Web-Dashboard läuft auf https://localhost:5000")


async def keep_alive() -> None:
    asyncio.get_event_loop().create_task(_run_server())
    await asyncio.sleep(0)