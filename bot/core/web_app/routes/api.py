"""
web_app/routes/api.py  –  REST API endpoints
"""
from __future__ import annotations
from aiohttp import web
from ..session    import get_session
from ..discord_api import user_has_server_access, get_guild_members


async def handle_api_members(request: web.Request) -> web.Response:
    sess = get_session(request)
    if "user" not in sess:
        return web.json_response({"error": "Unauthorized"}, status=401)

    query     = request.rel_url.query.get("q", "").strip()
    server_id = request.rel_url.query.get("server_id", "").strip()

    if not server_id or not query or len(query) < 2:
        return web.json_response([])

    if not user_has_server_access(sess["user"], server_id):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        members = await get_guild_members(server_id, query=query, limit=15)
        results = []
        for m in members:
            user = m.get("user", {})
            uid  = user.get("id", "")
            display  = m.get("nick") or user.get("global_name") or user.get("username") or uid
            username = user.get("username") or ""
            avatar   = user.get("avatar")
            if avatar and uid:
                avatar_url = f"https://cdn.discordapp.com/avatars/{uid}/{avatar}.png?size=32"
            else:
                disc = int(user.get("discriminator") or 0) % 5
                avatar_url = f"https://cdn.discordapp.com/embed/avatars/{disc}.png"
            results.append({"id": uid, "display": display, "username": username, "avatar": avatar_url})
        return web.json_response(results)
    except Exception as e:
        return web.json_response([])