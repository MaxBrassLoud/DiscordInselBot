from flask import Flask
from threading import Thread
from flask import Blueprint, render_template, redirect, url_for, session, request, abort
from functools import wraps

app = Flask('')

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("auth.login", next=request.url))
        return f(*args, **kwargs)
    return decorated

@app.route('/')
def home():
    return """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Insel Bot – Übersicht</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <style>
    :root {
      --primary: #38bdf8; --primary-hover: #0ea5e9; --bg-dark: #0f172a;
      --bg-card: #1e293b; --border: #334155; --text-main: #f1f5f9; --text-muted: #94a3b8;
    }
    body { margin:0; font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
      background:radial-gradient(circle at top right,#1e293b,#0f172a); color:var(--text-main);
      display:flex; flex-direction:column; align-items:center; padding:60px 20px; min-height:100vh; }
    .container { max-width:1100px; width:100%; text-align:center; }
    h1 { font-size:3rem; margin:0 0 15px; background:linear-gradient(to right,#38bdf8,#818cf8);
      -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
    .status-badge { display:inline-flex; align-items:center; background:rgba(34,197,94,0.1);
      color:#4ade80; padding:6px 16px; border-radius:20px; font-size:.9rem; font-weight:600;
      border:1px solid rgba(34,197,94,0.2); margin-bottom:50px; }
    .status-dot { width:8px; height:8px; background:#4ade80; border-radius:50%; margin-right:8px;
      animation:pulse 2s infinite; }
    @keyframes pulse { 0%{transform:scale(1);opacity:1} 50%{transform:scale(1.5);opacity:.5} 100%{transform:scale(1);opacity:1} }
    .features { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:16px; margin-top:40px; }
    .feature { background:var(--bg-card); border:1px solid var(--border); padding:24px;
      border-radius:16px; text-align:left; transition:all .3s; }
    .feature:hover { transform:translateY(-4px); border-color:var(--primary); }
    .feature i { font-size:1.5rem; color:var(--primary); margin-bottom:12px; display:block; }
    .feature h3 { margin:0 0 8px; color:#fff; }
    .feature p { margin:0; font-size:.9rem; color:var(--text-muted); }
    footer { margin-top:60px; padding:30px 0; border-top:1px solid var(--border);
      width:100%; text-align:center; font-size:.9rem; color:var(--text-muted); }
  </style>
</head>
<body>
<div class="container">
  <div style="font-size:4rem;color:var(--primary);margin-bottom:20px"><i class="fas fa-umbrella-beach"></i></div>
  <h1>Insel Bot</h1>
  <div class="status-badge"><div class="status-dot"></div>System Aktiv</div>
  <div class="features">
    <div class="feature"><i class="fas fa-gamepad"></i><h3>Spieleabende</h3><p>Erstelle und verwalte Spieleabende mit RSVP-System</p></div>
    <div class="feature"><i class="fas fa-photo-video"></i><h3>Medien</h3><p>Automatische Weiterleitung von Bildern, Videos und Links</p></div>
    <div class="feature"><i class="fas fa-calendar-star"></i><h3>Events</h3><p>Event-System mit Follow, Erinnerungen und Live-Status</p></div>
    <div class="feature"><i class="fas fa-door-open"></i><h3>Welcomer</h3><p>Willkommens- und Abschiedsnachrichten</p></div>
    <div class="feature"><i class="fas fa-tags"></i><h3>Rollenvergabe</h3><p>Selbst-Rollenvergabe per Button</p></div>
    <div class="feature"><i class="fas fa-ticket-alt"></i><h3>Ticket-System</h3><p>Modulares Support-Ticket-System</p></div>
  </div>
</div>
<footer>&copy; 2026 Insel Bot &bull; Made with ❤️ for Die Insel Community</footer>
</body>
</html>"""

app.route("/tickets")
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


app.route("/tickets/<int:ticket_id>")
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
def run():
    app.run(host="0.0.0.0", port=5000)


def keep_alive():
    t = Thread(target=run)
    t.start()


if __name__ == "__main__":
    keep_alive()