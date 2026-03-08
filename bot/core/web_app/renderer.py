"""
web_app/renderer.py  –  Jinja2 template rendering
"""
from __future__ import annotations
from pathlib import Path
from aiohttp import web

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def render(name: str, **ctx) -> web.Response:
    path = _TEMPLATE_DIR / name
    try:
        from jinja2 import Environment, FileSystemLoader, select_autoescape
        env  = Environment(
            loader=FileSystemLoader(str(_TEMPLATE_DIR)),
            autoescape=select_autoescape(["html"]),
        )
        html = env.get_template(name).render(**ctx)
    except ImportError:
        html = path.read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")