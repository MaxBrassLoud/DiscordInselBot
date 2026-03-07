import os
import sys
from flask import Blueprint, render_template, redirect, url_for, session, request, abort
from functools import wraps

tickets_bp = Blueprint("tickets", __name__, url_prefix="/dashboard/tickets")


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated


@tickets_bp.route("/")
@login_required
def ticket_list():
    from bot.core.supabase_client import get_supabase
    from bot.features.tickets.storage import get_all_tickets_for_server

    user      = session["user"]
    supabase  = get_supabase()
    servers   = (supabase.table("ticket_servers").select("*").execute().data or [])
    server_id = request.args.get("server_id")
    tickets   = []
    selected  = None

    if server_id:
        selected = next((s for s in servers if s["server_id"] == server_id), None)
        if selected:
            q = supabase.table("tickets").select("*").eq("server_id", server_id)
            status = request.args.get("status")
            module = request.args.get("module")
            if status:
                q = q.eq("status", status)
            if module:
                q = q.eq("module", module)
            sort = request.args.get("sort", "newest")
            q = q.order("created_at", desc=(sort != "oldest"))
            tickets = q.execute().data or []

    return render_template("ticket_list.html",
                           user=user, servers=servers, tickets=tickets,
                           selected=selected, server_id=server_id,
                           filters=request.args)


@tickets_bp.route("/<int:ticket_id>")
@login_required
def ticket_detail(ticket_id: int):
    from bot.features.tickets.storage import load_ticket, load_messages

    user      = session["user"]
    server_id = request.args.get("server_id", "")
    ticket    = load_ticket(server_id, ticket_id)
    if not ticket:
        abort(404)

    is_creator = str(ticket.get("creator_id")) == str(user["id"])
    if not is_creator:
        pass  # Add staff-role check here for production

    messages = load_messages(server_id, ticket_id)
    return render_template("ticket_view.html",
                           user=user, ticket=ticket, messages=messages,
                           server_id=server_id)