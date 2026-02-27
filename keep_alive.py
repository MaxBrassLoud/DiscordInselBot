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
      max-width: 1000px;
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
      max-width: 600px;
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
      margin-bottom: 40px;
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

    .commands {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 25px;
      width: 100%;
      perspective: 1000px;
    }

    .command {
      background: var(--bg-card);
      border: 1px solid var(--border);
      padding: 30px;
      border-radius: 20px;
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
      top: 0;
      left: 0;
      width: 4px;
      height: 100%;
      background: var(--primary);
      opacity: 0;
      transition: opacity 0.3s;
    }

    .command:hover {
      transform: translateY(-8px) scale(1.02);
      box-shadow: 0 20px 40px rgba(0,0,0,0.4);
      border-color: var(--primary);
    }

    .command:hover::before {
      opacity: 1;
    }

    .command i {
      font-size: 1.8rem;
      color: var(--primary);
      margin-bottom: 15px;
      display: block;
    }

    .command h3 {
      margin: 0;
      color: #fff;
      font-size: 1.3rem;
      margin-bottom: 10px;
    }

    .command p {
      margin: 0;
      font-size: 0.95rem;
      color: var(--text-muted);
      line-height: 1.5;
      flex-grow: 1;
    }

    .command .tag {
      display: inline-block;
      margin-top: 15px;
      font-size: 0.75rem;
      font-weight: 700;
      text-transform: uppercase;
      color: var(--primary);
      background: rgba(56, 189, 248, 0.1);
      padding: 2px 8px;
      border-radius: 4px;
      align-self: flex-start;
    }

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
    <div class="header-icon"><i class="fas fa-island-tropical"></i></div>
    <h1>Insel Bot</h1>
    <div class="status-badge">
      <div class="status-dot"></div>
      System Aktiv
    </div>
    <p class="subtitle">
      Der ultimative Begleiter für deine Discord-Community. Organisiere Spieleabende, verwalte Server-Bilder und verbinde deine Mitglieder.
    </p>

    <div class="commands">
      <div class="command">
        <i class="fas fa-cogs"></i>
        <h3>/setup_spieleabend</h3>
        <p>Konfiguriere das System: Wähle den Ankündigungskanal, die Ping-Rolle, Lösch-Berechtigungen und den <b>Bilder-Kanal</b>.</p>
        <span class="tag">Admin</span>
      </div>

      <div class="command">
        <i class="fas fa-calendar-plus"></i>
        <h3>/spieleabend</h3>
        <p>Erstelle ein neues Event. Der Bot erstellt automatisch einen Thread für Diskussionen und fügt RSVP-Buttons hinzu.</p>
        <span class="tag">User</span>
      </div>

      <div class="command">
        <i class="fas fa-image"></i>
        <h3>Bilder-Archiv</h3>
        <p>Poste Bilder in einen beliebigen Kanal – der Bot erkennt sie und spiegelt sie automatisch in den konfigurierten <b>Bilder-Kanal</b>.</p>
        <span class="tag">Auto-Feature</span>
      </div>

      <div class="command">
        <i class="fas fa-bell"></i>
        <h3>Smart Reminders</h3>
        <p>Automatische Benachrichtigungen 1 Stunde (für Unentschlossene) und 10 Minuten vor Start sowie pünktlich zum Beginn.</p>
        <span class="tag">System</span>
      </div>

      <div class="command">
        <i class="fas fa-trash-alt"></i>
        <h3>Event-Management</h3>
        <p>Lösche geplante Events sauber aus dem Kanal und der Datenbank. Threads werden dabei automatisch mit aufgeräumt.</p>
        <span class="tag">Mod / Creator</span>
      </div>
      
      <div class="command">
        <i class="fas fa-database"></i>
        <h3>Supabase Sync</h3>
        <p>Alle Einstellungen und Events werden in Echtzeit mit Supabase synchronisiert, um Datenverlust zu verhindern.</p>
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
