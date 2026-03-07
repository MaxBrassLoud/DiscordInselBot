from pathlib import Path
from datetime import datetime
from bot.utils.logger import get_logger

logger = get_logger("tickets.export")


def generate_html_export(ticket: dict, messages: list) -> str:
    module    = ticket.get("module", "?")
    ticket_id = ticket.get("ticket_id", "?")
    creator   = ticket.get("creator_name", "?")
    desc      = ticket.get("description", "")
    created   = ticket.get("created_at", "")
    claimed   = ticket.get("claimed_by") or "Niemand"

    rows = ""
    for msg in messages:
        ts   = msg.get("timestamp", "")
        user = msg.get("user", "?")
        text = msg.get("message", "").replace("<", "&lt;").replace(">", "&gt;")
        atts = msg.get("attachments", [])
        att_html = ""
        for att in atts:
            att_html += f'<a class="att" href="{att}" target="_blank">📎 Anhang</a>'
        rows += f"""
        <div class="msg">
            <div class="meta"><span class="user">{user}</span><span class="ts">{ts}</span></div>
            <div class="body">{text}{att_html}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Ticket #{ticket_id} – Export</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:'Segoe UI',Roboto,sans-serif; background:#0f172a; color:#e2e8f0; padding:32px 16px; }}
  .container {{ max-width:860px; margin:0 auto; }}
  .header {{ background:#1e293b; border:1px solid #334155; border-radius:16px; padding:28px 32px; margin-bottom:24px; }}
  .header h1 {{ margin:0 0 6px; font-size:1.6rem; color:#38bdf8; }}
  .header .sub {{ color:#94a3b8; font-size:.9rem; margin-bottom:16px; }}
  .info-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
  .info-item {{ background:#0f172a; border-radius:10px; padding:12px 16px; }}
  .info-item .label {{ font-size:.7rem; text-transform:uppercase; letter-spacing:1px; color:#64748b; margin-bottom:4px; }}
  .info-item .value {{ font-size:.95rem; color:#f1f5f9; font-weight:600; }}
  .desc {{ background:#1e293b; border:1px solid #334155; border-left:4px solid #38bdf8; border-radius:12px;
           padding:20px 24px; margin-bottom:24px; white-space:pre-wrap; line-height:1.65; }}
  .messages-title {{ font-size:1rem; font-weight:700; color:#94a3b8; text-transform:uppercase;
                     letter-spacing:1.5px; margin-bottom:14px; }}
  .msg {{ background:#1e293b; border:1px solid #334155; border-radius:12px; padding:14px 18px; margin-bottom:10px; }}
  .meta {{ display:flex; justify-content:space-between; margin-bottom:6px; }}
  .user {{ font-weight:700; color:#38bdf8; font-size:.9rem; }}
  .ts {{ font-size:.78rem; color:#64748b; }}
  .body {{ white-space:pre-wrap; line-height:1.6; font-size:.9rem; }}
  .att {{ display:inline-block; margin-top:6px; color:#818cf8; font-size:.82rem; text-decoration:none; margin-right:8px; }}
  footer {{ text-align:center; margin-top:40px; font-size:.8rem; color:#475569; }}
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <h1>🎫 Ticket #{ticket_id}</h1>
    <div class="sub">Exportiert am {datetime.utcnow().strftime("%d.%m.%Y %H:%M")} UTC</div>
    <div class="info-grid">
      <div class="info-item"><div class="label">Modul</div><div class="value">{module}</div></div>
      <div class="info-item"><div class="label">Erstellt von</div><div class="value">{creator}</div></div>
      <div class="info-item"><div class="label">Erstellt am</div><div class="value">{created}</div></div>
      <div class="info-item"><div class="label">Übernommen von</div><div class="value">{claimed}</div></div>
    </div>
  </div>
  <div class="desc"><strong>📝 Beschreibung:</strong>\n{desc}</div>
  <div class="messages-title">💬 Chatverlauf ({len(messages)} Nachrichten)</div>
  {rows if rows else '<div class="msg"><div class="body"><em>Keine Nachrichten gespeichert.</em></div></div>'}
</div>
<footer>Insel Bot – Ticket Export</footer>
</body>
</html>"""


def save_html_export(server_id: str, ticket_id: int, ticket: dict, messages: list) -> Path:
    from features.tickets.storage import _ticket_dir
    html    = generate_html_export(ticket, messages)
    outpath = _ticket_dir(server_id, ticket_id) / "export.html"
    with open(outpath, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"HTML-Export gespeichert: {outpath}")
    return outpath