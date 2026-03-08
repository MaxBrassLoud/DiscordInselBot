"""
web_app/routes/dashboard.py  –  Unified Dashboard (Tickets + Bewerbungen)
"""
from __future__ import annotations
import logging
from aiohttp import web

from ..session    import get_session
from ..renderer   import render
from ..discord_api import (
    user_has_server_access, user_can_see_ticket,
    cached_guild_info, guild_icon_url,
    get_member, member_display_name, member_avatar_url,
)

log = logging.getLogger("web.dashboard")


# ── helpers ───────────────────────────────────────────────────────────────────

async def _enrich_servers(raw_servers: list, user: dict) -> list:
    servers = []
    for srv in raw_servers:
        sid = srv.get("server_id", "")
        if not user_has_server_access(user, sid):
            continue
        guild = await cached_guild_info(sid)
        srv   = dict(srv)
        srv["guild_name"] = guild.get("name", sid) if guild else sid
        srv["guild_icon"] = guild_icon_url(guild) if guild else None
        servers.append(srv)
    return servers


async def _resolve_member(guild_id: str, user_id: str, cache: dict) -> dict | None:
    if not user_id:
        return None
    if user_id not in cache:
        cache[user_id] = await get_member(guild_id, user_id)
    return cache[user_id]


# ── Main dashboard (redirect) ─────────────────────────────────────────────────

async def handle_dashboard(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        raise web.HTTPFound(f"/login?next=/dashboard")
    raise web.HTTPFound("/dashboard/tickets")


# ── Tickets ───────────────────────────────────────────────────────────────────

async def handle_ticket_list(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        raise web.HTTPFound(f"/login?next={request.rel_url}")

    from bot.core.supabase_client import get_supabase
    user     = sess["user"]
    supabase = get_supabase()

    raw_servers = supabase.table("ticket_servers").select("*").execute().data or []
    servers     = await _enrich_servers(raw_servers, user)

    server_id = request.rel_url.query.get("server_id")
    tickets   = []
    selected  = None

    if server_id:
        selected = next((s for s in servers if s["server_id"] == server_id), None)
        if selected is None:
            raise web.HTTPForbidden(reason="Kein Zugriff auf diesen Server.")

        # Load all staff role IDs for this server
        all_staff_roles: list[str] = []
        mods = supabase.table("ticket_modules").select("id").eq("server_id", server_id).execute().data or []
        for mod_row in mods:
            roles = supabase.table("ticket_module_roles").select("role_id").eq("module_id", mod_row["id"]).execute().data or []
            all_staff_roles.extend(r["role_id"] for r in roles)

        q = supabase.table("tickets").select("*").eq("server_id", server_id)
        if request.rel_url.query.get("status"):
            q = q.eq("status", request.rel_url.query["status"])
        if request.rel_url.query.get("module"):
            q = q.eq("module", request.rel_url.query["module"])
        if request.rel_url.query.get("creator_id"):
            q = q.eq("creator_id", request.rel_url.query["creator_id"])
        sort = request.rel_url.query.get("sort", "newest")
        q    = q.order("created_at", desc=(sort != "oldest"))
        raw_tickets = q.execute().data or []

        member_cache: dict[str, dict] = {}
        for t in raw_tickets:
            if not user_can_see_ticket(user, t, server_id, all_staff_roles):
                continue
            m = await _resolve_member(server_id, t.get("creator_id", ""), member_cache)
            t["creator_display"] = member_display_name(m) if m else (t.get("creator_id") or "Unbekannt")
            t["creator_avatar"]  = member_avatar_url(m) if m else None
            tickets.append(t)

    selected_creator = None
    if request.rel_url.query.get("creator_id") and server_id:
        m = await get_member(server_id, request.rel_url.query["creator_id"])
        if m:
            selected_creator = {
                "id":      request.rel_url.query["creator_id"],
                "display": member_display_name(m),
                "avatar":  member_avatar_url(m),
            }

    return render("dashboard.html",
        user=user, servers=servers, tickets=tickets, applications=[],
        selected=selected, server_id=server_id,
        active_tab="tickets",
        filters=request.rel_url.query,
        selected_creator=selected_creator,
    )


async def handle_ticket_detail(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        raise web.HTTPFound(f"/login?next={request.rel_url}")

    from bot.features.tickets.storage import load_ticket, load_messages
    from bot.core.supabase_client import get_supabase
    user      = sess["user"]
    ticket_id = int(request.match_info["ticket_id"])
    server_id = request.rel_url.query.get("server_id", "")

    if not user_has_server_access(user, server_id):
        raise web.HTTPForbidden(reason="Kein Zugriff auf diesen Server.")

    ticket = load_ticket(server_id, ticket_id)
    if not ticket:
        raise web.HTTPNotFound()

    supabase = get_supabase()
    mod_name = ticket.get("module", "")
    staff_role_ids: list[str] = []
    if mod_name:
        mod_rows = supabase.table("ticket_modules").select("id")\
            .eq("server_id", server_id).eq("name", mod_name).execute().data or []
        for mod_row in mod_rows:
            roles = supabase.table("ticket_module_roles").select("role_id")\
                .eq("module_id", mod_row["id"]).execute().data or []
            staff_role_ids.extend(r["role_id"] for r in roles)

    if not user_can_see_ticket(user, ticket, server_id, staff_role_ids):
        raise web.HTTPForbidden(reason="Du hast keinen Zugriff auf dieses Ticket.")

    creator_id = ticket.get("creator_id", "")
    if creator_id:
        m = await get_member(server_id, creator_id)
        ticket["creator_display"] = member_display_name(m) if m else ticket.get("creator_name", creator_id)
        ticket["creator_avatar"]  = member_avatar_url(m) if m else None
    else:
        ticket["creator_display"] = ticket.get("creator_name", "Unbekannt")
        ticket["creator_avatar"]  = None

    claimed_id = ticket.get("claimed_by")
    if claimed_id:
        cm = await get_member(server_id, claimed_id)
        ticket["claimed_by_display"] = member_display_name(cm) if cm else claimed_id
    else:
        ticket["claimed_by_display"] = None

    messages = load_messages(server_id, ticket_id)
    return render("ticket_view.html",
        user=user, ticket=ticket, messages=messages, server_id=server_id)


# ── Applications ──────────────────────────────────────────────────────────────

async def handle_application_list(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        raise web.HTTPFound(f"/login?next={request.rel_url}")

    from bot.core.supabase_client import get_supabase
    user     = sess["user"]
    supabase = get_supabase()

    raw_servers = supabase.table("application_servers").select("*").execute().data or []
    servers     = await _enrich_servers(raw_servers, user)

    server_id    = request.rel_url.query.get("server_id")
    applications = []
    selected     = None

    if server_id:
        selected = next((s for s in servers if s["server_id"] == server_id), None)
        if selected is None:
            raise web.HTTPForbidden(reason="Kein Zugriff auf diesen Server.")

        q = supabase.table("applications").select("*").eq("server_id", server_id)
        if request.rel_url.query.get("status"):
            q = q.eq("status", request.rel_url.query["status"])
        if request.rel_url.query.get("creator_id"):
            q = q.eq("creator_id", request.rel_url.query["creator_id"])
        sort = request.rel_url.query.get("sort", "newest")
        q    = q.order("created_at", desc=(sort != "oldest"))
        raw_apps = q.execute().data or []

        member_cache: dict[str, dict] = {}
        for a in raw_apps:
            m = await _resolve_member(server_id, a.get("creator_id", ""), member_cache)
            a["creator_display"] = member_display_name(m) if m else a.get("creator_id", "")
            a["creator_avatar"]  = member_avatar_url(m) if m else None
            applications.append(a)

    selected_creator = None
    if request.rel_url.query.get("creator_id") and server_id:
        m = await get_member(server_id, request.rel_url.query["creator_id"])
        if m:
            selected_creator = {
                "id":      request.rel_url.query["creator_id"],
                "display": member_display_name(m),
                "avatar":  member_avatar_url(m),
            }

    return render("dashboard.html",
        user=user, servers=servers, tickets=[], applications=applications,
        selected=selected, server_id=server_id,
        active_tab="applications",
        filters=request.rel_url.query,
        selected_creator=selected_creator,
    )


async def handle_application_detail(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        raise web.HTTPFound(f"/login?next={request.rel_url}")

    from bot.features.applications.manager import load_application, load_app_messages
    from bot.core.supabase_client import get_supabase
    user      = sess["user"]
    app_id    = int(request.match_info["app_id"])
    server_id = request.rel_url.query.get("server_id", "")

    if not user_has_server_access(user, server_id):
        raise web.HTTPForbidden(reason="Kein Zugriff auf diesen Server.")

    app = load_application(server_id, app_id)
    if not app:
        raise web.HTTPNotFound()

    supabase = get_supabase()
    cfg_r    = supabase.table("application_servers").select("staff_role_ids").eq("server_id", server_id).execute()
    staff_role_ids: list[str] = []
    if cfg_r.data:
        staff_role_ids = [r.strip() for r in (cfg_r.data[0].get("staff_role_ids") or "").split(",") if r.strip()]

    if not user_can_see_ticket(user, {"creator_id": app.get("creator_id")}, server_id, staff_role_ids):
        raise web.HTTPForbidden(reason="Du hast keinen Zugriff auf diese Bewerbung.")

    creator_id = app.get("creator_id", "")
    if creator_id:
        m = await get_member(server_id, creator_id)
        app["creator_display"] = member_display_name(m) if m else app.get("creator_name", creator_id)
        app["creator_avatar"]  = member_avatar_url(m) if m else None
    else:
        app["creator_display"] = app.get("creator_name", "Unbekannt")
        app["creator_avatar"]  = None

    messages = load_app_messages(server_id, app_id)
    return render("application_view.html",
        user=user, app=app, messages=messages, server_id=server_id)


# ── Static files ──────────────────────────────────────────────────────────────

async def handle_static(request: web.Request) -> web.Response:
    from pathlib import Path
    filename = request.match_info["filename"]
    static_dir = Path(__file__).parent.parent / "static"
    path = static_dir / filename
    if not path.exists() or not path.is_file():
        raise web.HTTPNotFound()
    content_types = {
        ".css": "text/css", ".js": "application/javascript",
        ".png": "image/png", ".jpg": "image/jpeg", ".ico": "image/x-icon",
    }
    ct = content_types.get(path.suffix, "application/octet-stream")
    return web.Response(body=path.read_bytes(), content_type=ct)