"""
web_app/middleware.py  –  Error handling middleware
"""
from __future__ import annotations
from aiohttp import web


def _error_page(title: str, icon: str, heading: str, msg: str, back_url: str = "/dashboard") -> web.Response:
    html = f"""<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title}</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/dashboard.css">
</head>
<body style="display:flex;align-items:center;justify-content:center;min-height:100vh">
  <div class="error-card">
    <div class="error-icon">{icon}</div>
    <h1 class="error-title">{heading}</h1>
    <p class="error-msg">{msg}</p>
    <div style="display:flex;gap:12px;justify-content:center">
      <a href="{back_url}" class="btn btn-primary">← Dashboard</a>
      <a href="/logout" class="btn btn-ghost">Abmelden</a>
    </div>
  </div>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPForbidden as e:
        return _error_page("403 – Kein Zugriff", "🚫", "Kein Zugriff",
                           e.reason or "Du hast keine Berechtigung für diese Seite.")
    except web.HTTPNotFound:
        return _error_page("404 – Nicht gefunden", "🔍", "Nicht gefunden",
                           "Diese Seite oder dieser Eintrag existiert nicht.",
                           back_url="/dashboard")