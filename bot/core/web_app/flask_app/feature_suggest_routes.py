"""
bot/core/web_app/flask_app/feature_suggest_routes.py
====================================================
Web-Routen für das Feature-Suggest-System.

Fix: DM-Versand läuft jetzt über thread-safe dispatch-Methoden des Cogs.
     Cog-Suche über isinstance-Fallback, unabhängig vom registrierten Namen.
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import jsonify, request, render_template, session, redirect

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

log = get_logger("feature_suggest_web")

MBL_ID = os.getenv("MBL", "")

STATUSES   = ["proposed", "accepted", "in_progress", "done", "rejected"]
PRIORITIES = ["important", "normal", "can_wait", "bug"]


def _is_mbl(user: dict) -> bool:
    return bool(MBL_ID and user.get("id") == MBL_ID)


def _get_feature_cog(bot):
    """Findet den FeatureSuggest-Cog – unabhängig vom registrierten Namen."""
    if bot is None:
        return None
    # 1. Direkt über den Namen
    cog = bot.get_cog("FeatureSuggest")
    if cog is not None:
        return cog
    # 2. Fallback: über Cog-Klasse suchen
    try:
        from bot.features.feature_suggest.cog import FeatureSuggest
        for c in bot.cogs.values():
            if isinstance(c, FeatureSuggest):
                return c
    except Exception as e:
        log.warning(f"[feature_suggest] Cog-Fallback-Suche: {e}")
    return None


def _notify_user(bot, user_id: str, suggestion: dict,
                 old_status: str, new_status: str, comment: str = "") -> bool:
    cog = _get_feature_cog(bot)
    if cog is None:
        log.warning("[feature_suggest] FeatureSuggest-Cog nicht gefunden – DM übersprungen.")
        return False
    return cog.dispatch_notify_user(
        user_id, suggestion["id"], suggestion["title"],
        old_status, new_status, comment,
    )


def _notify_mbl(bot, suggestion_id: int, title: str, creator: str) -> bool:
    cog = _get_feature_cog(bot)
    if cog is None:
        log.warning("[feature_suggest] FeatureSuggest-Cog nicht gefunden – MBL-DM übersprungen.")
        return False
    return cog.dispatch_notify_mbl(suggestion_id, title, creator)


def register_feature_suggest_routes(app, login_required, get_bot=None):
    """
    get_bot: optionaler Callback → returns discord.ext.commands.Bot oder None
    """

    # ── GET /feature-suggest (Token-Formular) ────────────────────────────────
    @app.route("/feature-suggest")
    def feature_suggest_form():
        token = request.args.get("token", "").strip()
        if not token:
            return render_template(
                "error.html", code=400, title="Ungültiger Link",
                icon="🔗", msg="Es fehlt ein gültiger Token.",
            ), 400

        sb = get_supabase()
        row = (sb.table("feature_suggestion_tokens")
                 .select("*").eq("token", token).execute())
        if not row.data:
            return render_template(
                "error.html", code=404, title="Link ungültig",
                icon="🔗", msg="Dieser Link existiert nicht.",
            ), 404

        tk = row.data[0]
        if tk.get("used"):
            return render_template(
                "error.html", code=410, title="Bereits verwendet",
                icon="♻️", msg="Dieser Link wurde schon benutzt. Nutze /suggest für einen neuen.",
            ), 410

        try:
            exp = datetime.fromisoformat(tk["expires_at"].replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                return render_template(
                    "error.html", code=410, title="Link abgelaufen",
                    icon="⏰", msg="Dieser Link ist leider abgelaufen. Nutze /suggest erneut.",
                ), 410
        except Exception:
            pass

        return render_template("feature_suggest_form.html", token=token)

    # ── POST /api/feature-suggest/submit ─────────────────────────────────────
    @app.route("/api/feature-suggest/submit", methods=["POST"])
    def feature_suggest_submit():
        body  = request.get_json() or {}
        token = (body.get("token") or "").strip()
        title = (body.get("title") or "").strip()
        desc  = (body.get("description") or "").strip()

        if not token or not title or not desc:
            return jsonify({"error": "Titel, Beschreibung und Token sind nötig."}), 400
        if len(title) > 200:
            return jsonify({"error": "Titel max. 200 Zeichen."}), 400
        if len(desc) > 5000:
            return jsonify({"error": "Beschreibung max. 5000 Zeichen."}), 400

        sb = get_supabase()
        row = (sb.table("feature_suggestion_tokens")
                 .select("*").eq("token", token).execute())
        if not row.data:
            return jsonify({"error": "Token ungültig."}), 404
        tk = row.data[0]
        if tk.get("used"):
            return jsonify({"error": "Token bereits verwendet."}), 410

        user_id = tk["user_id"]

        # Discord-Namen holen (über Bot)
        creator_name = user_id
        bot = get_bot() if get_bot else None
        if bot is not None:
            import asyncio
            loop = None
            cog = _get_feature_cog(bot)
            if cog is not None and cog._loop and not cog._loop.is_closed():
                loop = cog._loop
            if loop is not None:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        bot.fetch_user(int(user_id)), loop,
                    )
                    u = fut.result(timeout=4)
                    creator_name = (
                        getattr(u, "display_name", None)
                        or getattr(u, "name", user_id)
                    )
                except Exception as e:
                    log.warning(f"[feature_suggest] fetch_user: {e}")

        ins = sb.table("feature_suggestions").insert({
            "title":        title,
            "description":  desc,
            "status":       "proposed",
            "priority":     "normal",
            "creator_id":   user_id,
            "creator_name": creator_name,
        }).execute()

        if not ins.data:
            return jsonify({"error": "Speichern fehlgeschlagen."}), 500
        suggestion = ins.data[0]

        # Verlauf-Eintrag
        sb.table("feature_suggestion_comments").insert({
            "suggestion_id": suggestion["id"],
            "author_id":     user_id,
            "author_name":   creator_name,
            "author_is_mbl": False,
            "content":       "Vorschlag eingereicht.",
            "from_status":   None,
            "to_status":     "proposed",
        }).execute()

        # Token als benutzt markieren
        sb.table("feature_suggestion_tokens").update({
            "used": True, "suggestion_id": suggestion["id"],
        }).eq("token", token).execute()

        # MBL benachrichtigen
        if bot is not None:
            ok = _notify_mbl(bot, suggestion["id"], title, creator_name)
            if not ok:
                log.warning("[feature_suggest] MBL-DM konnte nicht gesendet werden.")

        return jsonify({"ok": True, "id": suggestion["id"]})

    # ── GET /api/feature-suggest/mine ────────────────────────────────────────
    @app.route("/api/feature-suggest/mine")
    def feature_suggest_mine():
        user = session.get("user")
        if not user:
            return jsonify({"error": "Unauthorized"}), 401
        uid = user["id"]
        sb  = get_supabase()

        rows = (sb.table("feature_suggestions")
                  .select("*").eq("creator_id", uid)
                  .order("created_at", desc=True).execute().data or [])

        result = []
        for s in rows:
            comments = (sb.table("feature_suggestion_comments")
                          .select("*").eq("suggestion_id", s["id"])
                          .order("created_at").execute().data or [])
            result.append({**s, "comments": comments})
        return jsonify({"suggestions": result})

    # ── GET /feature-suggest/view (Token-basiert, kein Login) ────────────────
    @app.route("/feature-suggest/view")
    def feature_suggest_view():
        token = request.args.get("token", "").strip()
        if not token:
            return render_template(
                "error.html", code=400, title="Ungültiger Link",
                icon="🔗", msg="Es fehlt ein gültiger Token.",
            ), 400

        sb = get_supabase()
        row = (sb.table("feature_suggestion_tokens")
                 .select("*").eq("token", token).execute())
        if not row.data:
            return render_template(
                "error.html", code=404, title="Link ungültig",
                icon="🔗", msg="Dieser Link existiert nicht.",
            ), 404

        tk = row.data[0]

        try:
            exp = datetime.fromisoformat(tk["expires_at"].replace("Z", "+00:00"))
            if exp < datetime.now(timezone.utc):
                return render_template(
                    "error.html", code=410, title="Link abgelaufen",
                    icon="⏰", msg="Dieser Link ist abgelaufen. Nutze /suggest erneut.",
                ), 410
        except Exception:
            pass

        return render_template(
            "feature_suggest_view.html",
            token=token,
            user_id=tk["user_id"],
        )

    # ── GET /api/feature-suggest/mine-by-token ───────────────────────────────
    @app.route("/api/feature-suggest/mine-by-token")
    def feature_suggest_mine_by_token():
        token = request.args.get("token", "").strip()
        if not token:
            return jsonify({"error": "Kein Token."}), 400

        sb = get_supabase()
        tk_row = (sb.table("feature_suggestion_tokens")
                    .select("*").eq("token", token).execute())
        if not tk_row.data:
            return jsonify({"error": "Token ungültig."}), 404

        uid = tk_row.data[0]["user_id"]
        rows = (sb.table("feature_suggestions")
                  .select("*").eq("creator_id", uid)
                  .order("created_at", desc=True).execute().data or [])

        result = []
        for s in rows:
            s["comments"] = (sb.table("feature_suggestion_comments")
                               .select("*").eq("suggestion_id", s["id"])
                               .order("created_at").execute().data or [])
            result.append(s)

        return jsonify({"suggestions": result})

    # ── GET /dashboard/my-features (eingeloggt) ──────────────────────────────
    @app.route("/dashboard/my-features")
    @login_required
    def dashboard_my_features():
        return render_template("feature_my_view.html", user=session["user"])

    # ── GET /api/feature-suggest/mine (bereits vorhanden) ────────────────────
    # bleibt unverändert

    # ══════════════════════════════════════════════════════════════════════════
    # MBL-Bereich
    # ══════════════════════════════════════════════════════════════════════════

    def mbl_required(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if "user" not in session:
                return jsonify({"error": "Unauthorized"}), 401
            if not _is_mbl(session["user"]):
                return jsonify({"error": "Nur MBL"}), 403
            return f(*args, **kwargs)
        return wrapper

    @app.route("/dashboard/features")
    @login_required
    def dashboard_features():
        if not _is_mbl(session["user"]):
            return render_template(
                "error.html", code=403, title="Kein Zugriff",
                icon="🚫", msg="Nur MBL hat Zugriff auf das Feature-Board.",
            ), 403
        return render_template("feature_board.html", user=session["user"])

    @app.route("/api/features")
    @mbl_required
    def api_features_list():
        sb = get_supabase()
        rows = (sb.table("feature_suggestions")
                  .select("*").order("created_at", desc=True).execute().data or [])
        for s in rows:
            s["comments"] = (sb.table("feature_suggestion_comments")
                               .select("*").eq("suggestion_id", s["id"])
                               .order("created_at").execute().data or [])
        return jsonify({
            "suggestions": rows,
            "statuses":    STATUSES,
            "priorities":  PRIORITIES,
        })

    @app.route("/api/features", methods=["POST"])
    @mbl_required
    def api_features_create():
        body = request.get_json() or {}
        title = (body.get("title") or "").strip()
        desc  = (body.get("description") or "").strip()
        prio  = body.get("priority", "normal")
        if not title or not desc:
            return jsonify({"error": "Titel und Beschreibung nötig."}), 400
        if prio not in PRIORITIES:
            prio = "normal"

        sb  = get_supabase()
        uid = session["user"]["id"]
        ins = sb.table("feature_suggestions").insert({
            "title":          title,
            "description":    desc,
            "status":         "accepted",
            "priority":       prio,
            "creator_id":     uid,
            "creator_name":   session["user"].get("display_name")
                              or session["user"].get("username", uid),
            "is_mbl_created": True,
        }).execute()
        if not ins.data:
            return jsonify({"error": "Speichern fehlgeschlagen."}), 500
        s = ins.data[0]

        sb.table("feature_suggestion_comments").insert({
            "suggestion_id": s["id"],
            "author_id":     uid,
            "author_name":   session["user"].get("username", uid),
            "author_is_mbl": True,
            "content":       "Von MBL direkt hinzugefügt.",
            "from_status":   None,
            "to_status":     "accepted",
        }).execute()

        return jsonify({"ok": True, "suggestion": s})

    @app.route("/api/features/<int:sid>", methods=["PATCH"])
    @mbl_required
    def api_features_update(sid: int):
        body = request.get_json() or {}
        sb   = get_supabase()

        cur = sb.table("feature_suggestions").select("*").eq("id", sid).execute()
        if not cur.data:
            return jsonify({"error": "Nicht gefunden."}), 404
        current = cur.data[0]

        new_status = body.get("status")
        new_prio   = body.get("priority")
        comment    = (body.get("comment") or "").strip()

        updates = {"updated_at": datetime.now(timezone.utc).isoformat()}
        if new_status and new_status in STATUSES:
            updates["status"] = new_status
        if new_prio and new_prio in PRIORITIES:
            updates["priority"] = new_prio

        sb.table("feature_suggestions").update(updates).eq("id", sid).execute()

        status_changed = bool(new_status and new_status != current["status"])

        if status_changed or comment:
            sb.table("feature_suggestion_comments").insert({
                "suggestion_id": sid,
                "author_id":     session["user"]["id"],
                "author_name":   session["user"].get("username", "MBL"),
                "author_is_mbl": True,
                "content":       comment or f"Status geändert zu {new_status}",
                "from_status":   current["status"] if status_changed else None,
                "to_status":     new_status if status_changed else None,
            }).execute()

        # User benachrichtigen (nicht sich selbst)
        if status_changed and current["creator_id"] != session["user"]["id"]:
            bot = get_bot() if get_bot else None
            ok = _notify_user(
                bot, current["creator_id"], current,
                current["status"], new_status, comment,
            )
            if not ok:
                log.warning("[feature_suggest] DM an User konnte nicht gesendet werden.")

        return jsonify({"ok": True})

    @app.route("/api/features/<int:sid>/comment", methods=["POST"])
    @mbl_required
    def api_features_comment(sid: int):
        body    = request.get_json() or {}
        content = (body.get("content") or "").strip()
        if not content:
            return jsonify({"error": "Kommentar leer."}), 400

        sb = get_supabase()
        sb.table("feature_suggestion_comments").insert({
            "suggestion_id": sid,
            "author_id":     session["user"]["id"],
            "author_name":   session["user"].get("username", "MBL"),
            "author_is_mbl": True,
            "content":       content,
        }).execute()

        s_row = sb.table("feature_suggestions").select("*").eq("id", sid).execute()
        if s_row.data:
            s = s_row.data[0]
            if s["creator_id"] != session["user"]["id"]:
                bot = get_bot() if get_bot else None
                ok = _notify_user(
                    bot, s["creator_id"], s,
                    s["status"], s["status"], f"💬 {content}",
                )
                if not ok:
                    log.warning("[feature_suggest] DM (Kommentar) konnte nicht gesendet werden.")
        return jsonify({"ok": True})

    def _load_suggestion_with_comments(sb, sid: int) -> dict | None:
        row = sb.table("feature_suggestions").select("*").eq("id", sid).execute()
        if not row.data:
            return None
        s = row.data[0]
        s["comments"] = (sb.table("feature_suggestion_comments")
                         .select("*").eq("suggestion_id", sid)
                         .order("created_at").execute().data or [])
        return s