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
  <style>
    body {
      margin: 0;
      font-family: Arial, sans-serif;
      background: linear-gradient(135deg, #0f172a, #1e293b);
      color: #f1f5f9;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 40px 20px;
      min-height: 100vh;
    }
    h1 {
      font-size: 2.5rem;
      margin-bottom: 10px;
      color: #38bdf8;
      text-align: center;
    }
    p {
      font-size: 1.1rem;
      color: #cbd5e1;
      margin-bottom: 30px;
      text-align: center;
      max-width: 700px;
    }
    .commands {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 15px;
      width: 100%;
      max-width: 900px;
    }
    .command {
      background: #1e293b;
      border: 1px solid #334155;
      padding: 16px;
      border-radius: 14px;
      transition: transform 0.2s, box-shadow 0.2s;
    }
    .command:hover {
      transform: translateY(-4px);
      box-shadow: 0 10px 25px rgba(0,0,0,0.25);
    }
    .command h3 {
      margin: 0;
      color: #38bdf8;
      font-size: 1.1rem;
    }
    .command p {
      margin: 6px 0 0 0;
      font-size: 0.95rem;
      color: #94a3b8;
    }
    footer {
      margin-top: auto;
      font-size: 0.9rem;
      color: #64748b;
      padding-top: 40px;
    }
  </style>
</head>
<body>

  <h1>🎮 Insel Bot</h1>
  <p>
    Danke, dass du den Insel Bot nutzt!  
    Der Bot bietet ein vollständiges Spieleabend-System.
  </p>

  <div class="commands">

    <div class="command">
      <h3>/setup_spieleabend</h3>
      <p>Richtet den Spieleabend-Bot ein (Ping-Rolle, Kanal, Lösch-Rollen).</p>
    </div>

    <div class="command">
      <h3>/spieleabend</h3>
      <p>Erstellt einen neuen Spieleabend mit Thread und Teilnahme-Buttons.</p>
    </div>

    <div class="command">
      <h3>/spieleabend_loeschen</h3>
      <p>Löscht einen Spieleabend inklusive Nachricht, Thread und Datenbank-Eintrag.</p>
    </div>

  </div>

  <footer>© 2026 Insel Bot – Entwickelt für Discord</footer>

</body>
</html>
"""

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

if __name__ == "__main__":
    keep_alive()
