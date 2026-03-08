"""
web_app/session.py  –  HMAC-signed cookie sessions
"""
from __future__ import annotations
import hashlib, hmac, json, os, secrets, time
from aiohttp import web

_SESSION_COOKIE = "insel_session"
_SESSION_TTL    = 60 * 60 * 24 * 7


def _secret_key() -> bytes:
    return os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32)).encode()

def _sign(payload: str) -> str:
    sig = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"

def _verify(cookie: str) -> str | None:
    try:
        payload, sig = cookie.rsplit(".", 1)
        expected = hmac.new(_secret_key(), payload.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(sig, expected):
            return payload
    except Exception:
        pass
    return None

def get_session(request: web.Request) -> dict:
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

def set_session(response: web.Response, data: dict) -> None:
    data["_exp"] = int(time.time()) + _SESSION_TTL
    payload      = json.dumps(data, separators=(",", ":"))
    signed       = _sign(payload)
    response.set_cookie(
        _SESSION_COOKIE, signed,
        max_age=_SESSION_TTL, httponly=True, samesite="Lax", secure=True,
    )

def clear_session(response: web.Response) -> None:
    response.del_cookie(_SESSION_COOKIE)