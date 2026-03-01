from flask import Flask
from threading import Thread

app = Flask('')

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
      --primary: #38bdf8;
      --primary-hover: #0ea5e9;
      --bg-dark: #0f172a;
      --bg-card: #1e293b;
      --border: #334155;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
    }

    body {
      margin: 0;
      font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      background: radial-gradient(circle at top right, #1e293b, #0f172a);
      color: var(--text-main);
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 60px 20px;
      min-height: 100vh;
      line-height: 1.6;
    }

    .container {
      max-width: 1100px;
      width: 100%;
      text-align: center;
    }

    .header-icon {
      font-size: 4rem;
      color: var(--primary);
      margin-bottom: 20px;
      filter: drop-shadow(0 0 15px rgba(56, 189, 248, 0.4));
    }

    h1 {
      font-size: 3rem;
      margin: 0 0 15px 0;
      background: linear-gradient(to right, #38bdf8, #818cf8);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -1px;
    }

    .subtitle {
      font-size: 1.25rem;
      color: var(--text-muted);
      margin-bottom: 50px;
      max-width: 650px;
      margin-left: auto;
      margin-right: auto;
    }

    .status-badge {
      display: inline-flex;
      align-items: center;
      background: rgba(34, 197, 94, 0.1);
      color: #4ade80;
      padding: 6px 16px;
      border-radius: 20px;
      font-size: 0.9rem;
      font-weight: 600;
      border: 1px solid rgba(34, 197, 94, 0.2);
      margin-bottom: 50px;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      background: #4ade80;
      border-radius: 50%;
      margin-right: 8px;
      box-shadow: 0 0 10px #4ade80;
      animation: pulse 2s infinite;
    }

    @keyframes pulse {
      0% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.5); opacity: 0.5; }
      100% { transform: scale(1); opacity: 1; }
    }

    .section-label {
      text-align: left;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 2px;
      color: var(--text-muted);
      margin: 45px 0 18px 4px;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .section-label::after {
      content: '';
      flex: 1;
      height: 1px;
      background: var(--border);
    }

    .commands {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
      gap: 20px;
      width: 100%;
    }

    .command {
      background: var(--bg-card);
      border: 1px solid var(--border);
      padding: 28px;
      border-radius: 18px;
      text-align: left;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    .command::before {
      content: '';
      position: absolute;
      top: 0; left: 0;
      width: 4px; height: 100%;
      background: var(--accent, var(--primary));
      opacity: 0;
      transition: opacity 0.3s;
    }

    .command:hover {
      transform: translateY(-6px) scale(1.02);
      box-shadow: 0 20px 40px rgba(0,0,0,0.4);
      border-color: var(--accent, var(--primary));
    }

    .command:hover::before { opacity: 1; }

    .command i {
      font-size: 1.7rem;
      color: var(--accent, var(--primary));
      margin-bottom: 14px;
      display: block;
    }

    .command h3 {
      margin: 0 0 8px 0;
      color: #fff;
      font-size: 1.2rem;
    }

    .command p {
      margin: 0;
      font-size: 0.93rem;
      color: var(--text-muted);
      line-height: 1.55;
      flex-grow: 1;
    }

    .command .tag {
      display: inline-block;
      margin-top: 14px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--accent, var(--primary));
      background: rgba(56, 189, 248, 0.1);
      padding: 2px 8px;
      border-radius: 4px;
      align-self: flex-start;
    }

    /* Farbvarianten pro Sektion */
    .sec-spieleabend .command { --accent: #38bdf8; }
    .sec-spieleabend .command .tag { background: rgba(56,189,248,0.1); }

    .sec-media .command { --accent: #a78bfa; }
    .sec-media .command .tag { background: rgba(167,139,250,0.1); color: #a78bfa; }

    .sec-events .command { --accent: #34d399; }
    .sec-events .command .tag { background: rgba(52,211,153,0.1); color: #34d399; }

    .sec-welcome .command { --accent: #fb923c; }
    .sec-welcome .command .tag { background: rgba(251,146,60,0.1); color: #fb923c; }

    .sec-system .command { --accent: #94a3b8; }
    .sec-system .command .tag { background: rgba(148,163,184,0.1); color: #94a3b8; }

    footer {
      margin-top: 80px;
      padding: 40px 0;
      border-top: 1px solid var(--border);
      width: 100%;
      text-align: center;
      font-size: 0.9rem;
      color: var(--text-muted);
    }

    .heart { color: #f43f5e; }

    @media (max-width: 640px) {
      h1 { font-size: 2.2rem; }
      .commands { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
<div class="container">

  <div class="header-icon"><i class="fas fa-umbrella-beach"></i></div>
  <h1>Insel Bot</h1>
  <div class="status-badge">
    <div class="status-dot"></div>
    System Aktiv
  </div>
  <p class="subtitle">
    Der ultimative Begleiter für die Insel Community – Spieleabende, Events, Medien-Archiv, Willkommensnachrichten und vieles mehr.
  </p>

  <!-- ── SPIELEABENDE ─────────────────────────────────────── -->
  <div class="section-label"><i class="fas fa-gamepad"></i> Spieleabende</div>
  <div class="commands sec-spieleabend">
    <div class="command">
      <i class="fas fa-cogs"></i>
      <h3>/setup_spieleabend</h3>
      <p>Konfiguriere Ankündigungskanal, Ping-Rolle und Lösch-Berechtigungen für das Spieleabend-System.</p>
      <span class="tag">Admin</span>
    </div>
    <div class="command">
      <i class="fas fa-calendar-plus"></i>
      <h3>/spieleabend</h3>
      <p>Erstelle einen neuen Spieleabend per Modal. Der Bot erstellt automatisch einen Diskussions-Thread mit RSVP-Buttons (Dabei / Vielleicht / Keine Zeit).</p>
      <span class="tag">User</span>
    </div>
    <div class="command">
      <i class="fas fa-trash-alt"></i>
      <h3>/spieleabend_loeschen</h3>
      <p>Löscht einen Spieleabend samt Thread sauber aus dem Kanal und der Datenbank. Nur für Ersteller und berechtigte Rollen.</p>
      <span class="tag">Mod / Creator</span>
    </div>
    <div class="command">
      <i class="fas fa-bell"></i>
      <h3>Smart Reminders</h3>
      <p>Automatische Erinnerungen 1 Stunde vor Start (für Unentschlossene), 10 Minuten vor Start und pünktlich zum Beginn.</p>
      <span class="tag">System</span>
    </div>
  </div>

  <!-- ── MEDIEN-WEITERLEITUNG ────────────────────────────── -->
  <div class="section-label"><i class="fas fa-photo-video"></i> Medien-Weiterleitung</div>
  <div class="commands sec-media">
    <div class="command">
      <i class="fas fa-sliders-h"></i>
      <h3>/setup_media</h3>
      <p>Wähle den Ziel-Kanal und aktiviere oder deaktiviere einzelne Features per Toggle-Button.</p>
      <span class="tag">Admin</span>
    </div>
    <div class="command">
      <i class="fas fa-image"></i>
      <h3>Bilder (Blau)</h3>
      <p>Bilder aus beliebigen Kanälen werden automatisch erkannt und mit blauer Outline in den Medien-Kanal weitergeleitet.</p>
      <span class="tag">Auto</span>
    </div>
    <div class="command">
      <i class="fas fa-film"></i>
      <h3>Videos (Gelb)</h3>
      <p>Video-Anhänge werden mit gelber Outline weitergeleitet – inklusive Link zur Original-Nachricht.</p>
      <span class="tag">Auto</span>
    </div>
    <div class="command">
      <i class="fab fa-youtube"></i>
      <h3>YouTube-Links (Rot)</h3>
      <p>YouTube-Links werden erkannt, Titel und Thumbnail via yt-dlp abgerufen und als Embed mit roter Outline gepostet.</p>
      <span class="tag">Auto</span>
    </div>
    <div class="command">
      <i class="fab fa-twitch"></i>
      <h3>Twitch Clips (Lila)</h3>
      <p>Twitch-Clip-Links werden erkannt und als Embed mit Discord-nativer Preview und lila Outline weitergeleitet.</p>
      <span class="tag">Auto</span>
    </div>
  </div>

  <!-- ── EVENT SYSTEM ────────────────────────────────────── -->
  <div class="section-label"><i class="fas fa-calendar-star"></i> Event System</div>
  <div class="commands sec-events">
    <div class="command">
      <i class="fas fa-cog"></i>
      <h3>/setup_event</h3>
      <p>Lege den Event-Kanal fest und bestimme welche Rollen Events erstellen dürfen. Admins und MBL haben immer Zugriff.</p>
      <span class="tag">Admin</span>
    </div>
    <div class="command">
      <i class="fas fa-plus-circle"></i>
      <h3>/event_erstellen</h3>
      <p>Erstelle ein Event mit Name, Beschreibung, Start- und Endzeit. Der Bot postet ein Embed mit Live-Countdown und erstellt automatisch einen Thread.</p>
      <span class="tag">Berechtigt</span>
    </div>
    <div class="command">
      <i class="fas fa-bell"></i>
      <h3>Follower & Pings</h3>
      <p>User können Events folgen oder entfolgen. Follower werden bei Start, Ende und wichtigen Updates im Thread gepingt.</p>
      <span class="tag">Auto</span>
    </div>
    <div class="command">
      <i class="fas fa-edit"></i>
      <h3>/event_edit</h3>
      <p>Bearbeite Events per Dropdown: Zeiten ändern, Delay, Resume, Absagen, Titel, Beschreibung oder News an alle Follower senden.</p>
      <span class="tag">Berechtigt</span>
    </div>
    <div class="command">
      <i class="fas fa-users"></i>
      <h3>/event_list</h3>
      <p>Zeigt eine vollständige Follower-Liste aller aktiven Events des Servers – nur für Berechtigte sichtbar.</p>
      <span class="tag">Berechtigt</span>
    </div>
    <div class="command">
      <i class="fas fa-palette"></i>
      <h3>Live Status-Farben</h3>
      <p>Embeds wechseln automatisch die Farbe: Blau (geplant) → Grün (live) → Orange (verzögert) → Rot (abgesagt) → Grau (beendet).</p>
      <span class="tag">System</span>
    </div>
  </div>

  <!-- ── WELCOMER ────────────────────────────────────────── -->
  <div class="section-label"><i class="fas fa-door-open"></i> Welcomer</div>
  <div class="commands sec-welcome">
    <div class="command">
      <i class="fas fa-cog"></i>
      <h3>/setup_welcomer</h3>
      <p>Richte Willkommens- und Abschiedskanal ein und aktiviere oder deaktiviere beide Features unabhängig voneinander.</p>
      <span class="tag">Admin</span>
    </div>
    <div class="command">
      <i class="fas fa-hand-wave"></i>
      <h3>Willkommensnachrichten</h3>
      <p>Beim Serverbeitritt wird zufällig eine von 17 einzigartigen Willkommensnachrichten im konfigurierten Kanal gesendet.</p>
      <span class="tag">Auto</span>
    </div>
    <div class="command">
      <i class="fas fa-sign-out-alt"></i>
      <h3>Abschiedsnachrichten</h3>
      <p>Wenn ein Mitglied den Server verlässt, wird eine stille Nachricht in einen Mod-only-Kanal gesendet.</p>
      <span class="tag">Mod-only</span>
    </div>
  </div>

  <!-- ── SYSTEM ──────────────────────────────────────────── -->
  <div class="section-label"><i class="fas fa-server"></i> System</div>
  <div class="commands sec-system">
    <div class="command">
      <i class="fas fa-database"></i>
      <h3>Supabase + RAM-Cache</h3>
      <p>Alle Einstellungen werden in Supabase gespeichert. Ein 5-Minuten-RAM-Cache reduziert Datenbankabfragen drastisch.</p>
      <span class="tag">Technik</span>
    </div>
    <div class="command">
      <i class="fas fa-shield-alt"></i>
      <h3>Deduplizierung</h3>
      <p>Mehrere Bot-Instanzen werden über einen zufälligen Instanz-Delay und Channel-History-Check synchronisiert.</p>
      <span class="tag">Technik</span>
    </div>
  </div>

</div>

<footer>
  &copy; 2026 Insel Bot &bull; Made with <span class="heart">❤️</span> for Die Insel Community
</footer>

</body>
</html>"""

def run():
    app.run(host="0.0.0.0", port=5000)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == "__main__":
    keep_alive()