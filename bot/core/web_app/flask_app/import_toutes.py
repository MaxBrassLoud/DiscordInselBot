"""
bot/core/web_app/flask_app/import_routes.py
============================================
TicketTool-Import mit Web-Upload.

In app.py einbinden:
    from .import_routes import register_import_routes
    register_import_routes(app, login_required, _is_mbl, MBL_ID)

ROUTEN (alle nur MBL):
    GET  /dashboard/import               - Dashboard
    GET  /api/import/servers             - Alle konfigurierten Server aus DB (mit Name)
    GET  /api/import/modules/<server_id> - Ticket-Module eines Servers
    GET  /api/import/status              - Ordner-Uebersicht + DB-Statistiken
    POST /api/import/upload              - Dateien hochladen, Ordner erstellen, importieren, loeschen
    POST /api/import/run                 - Ordner-Import starten
    GET  /api/import/progress/<job_id>   - Job-Status pollen
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, request, render_template_string, session

# ---- Pfade ------------------------------------------------------------------
_FLASK_DIR  = Path(__file__).resolve().parent
_ROOT       = _FLASK_DIR.parent.parent.parent.parent   # projekt-root
IMPORTS_DIR = _ROOT / "imports"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB

# ---- Job-Verwaltung ---------------------------------------------------------
_import_jobs: dict[str, dict] = {}
_JOB_TTL = 3600


def _prune_jobs():
    now = time.time()
    for jid in list(_import_jobs.keys()):
        if now - _import_jobs[jid].get("started_at", now) > _JOB_TTL:
            del _import_jobs[jid]


# ---- Logging-Helfer fuer Background-Threads ---------------------------------

class _LogCapture:
    """Leitet stdout in eine Liste um (fuer Job-Log)."""
    def __init__(self, log_list: list, job: dict):
        self._list = log_list
        self._job  = job
        self._orig = sys.stdout

    def __enter__(self):
        sys.stdout = self
        return self

    def __exit__(self, *_):
        sys.stdout = self._orig

    def write(self, s):
        s = s.rstrip()
        if s:
            self._list.append(s)
            self._job["log"] = self._list.copy()
        self._orig.write(s + "\n") if s else None

    def flush(self):
        self._orig.flush()


def _ensure_imports():
    """Stellt sicher, dass bot.tools.import_tickettool geladen ist."""
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
    from bot.core.supabase_client import init_supabase
    init_supabase(os.getenv("SUPABASE_URL", ""), os.getenv("SUPABASE_KEY", ""))
    import bot.tools.import_tickettool as _mod
    _mod.IMPORTS_DIR = IMPORTS_DIR
    from bot.tools.import_tickettool import TicketToolImporter
    return TicketToolImporter


# ---- Background-Jobs --------------------------------------------------------

def _run_folder_import(job_id: str, server_id: str | None, all_mode: bool):
    """Importiert einen ganzen Ordner (keine Datei-Bereinigung)."""
    job = _import_jobs[job_id]
    job["status"] = "running"
    log_lines: list[str] = []

    with _LogCapture(log_lines, job):
        try:
            TicketToolImporter = _ensure_imports()
            importer = TicketToolImporter(dry_run=False, verbose=True)
            if all_mode:
                importer.import_all()
            else:
                importer.import_server(server_id)
            job["stats"]  = importer.stats
            job["status"] = "done"
        except Exception as e:
            job["status"] = "error"
            job["error"]  = str(e)
            print(f"Fehler: {e}")

    job["finished_at"] = time.time()


def _run_upload_import(
    job_id: str,
    file_paths: list[Path],
    server_id: str,
    import_type: str,
    module_name: str,
):
    """
    Importiert hochgeladene Dateien und loescht sie danach.
    Die Dateien liegen bereits in der richtigen Ordnerstruktur:
      imports/<server_id>/tickets/<module_name>/  oder
      imports/<server_id>/applications/
    Nach dem Import werden die Dateien geloescht.
    Der Ordner bleibt bestehen (fuer zukuenftige Uploads).
    """
    job = _import_jobs[job_id]
    job["status"] = "running"
    log_lines: list[str] = []

    with _LogCapture(log_lines, job):
        try:
            TicketToolImporter = _ensure_imports()
            importer = TicketToolImporter(dry_run=False, verbose=True)

            print(f"Upload-Import: {len(file_paths)} Datei(en) | Server {server_id} | {import_type}")

            for file_path in file_paths:
                try:
                    if import_type == "ticket":
                        importer.import_ticket_file(file_path, server_id, module_name)
                    else:
                        importer.import_application_file(file_path, server_id)
                except Exception as e:
                    print(f"Fehler bei {file_path.name}: {e}")
                    importer.stats["files_error"] += 1
                finally:
                    # Datei nach Verarbeitung immer loeschen
                    try:
                        file_path.unlink(missing_ok=True)
                        print(f"Geloescht: {file_path.name}")
                    except Exception as del_e:
                        print(f"Loeschen fehlgeschlagen ({file_path.name}): {del_e}")

            job["stats"]  = importer.stats
            job["status"] = "done"

        except Exception as e:
            job["status"] = "error"
            job["error"]  = str(e)
            print(f"Fehler: {e}")
            # Cleanup im Fehlerfall
            for fp in file_paths:
                try:
                    fp.unlink(missing_ok=True)
                except Exception:
                    pass

    job["finished_at"] = time.time()


# ---- Route-Registrierung ----------------------------------------------------

def register_import_routes(app, login_required, _is_mbl, MBL_ID):

    from functools import wraps

    def _mbl_only(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if "user" not in session:
                return jsonify({"error": "Unauthorized"}), 401
            if MBL_ID and not _is_mbl(session["user"]):
                return jsonify({"error": "Nur MBL"}), 403
            return f(*args, **kwargs)
        return decorated

    # -- GET /api/import/servers ----------------------------------------------
    @app.route("/api/import/servers")
    @_mbl_only
    def api_import_servers():
        """
        Gibt alle konfigurierten Server zurueck – mit Name aus Discord (via Bot-Token).
        Kombiniert ticket_servers + application_servers.
        """
        try:
            from bot.core.supabase_client import get_supabase
            sb  = get_supabase()
            ids: set[str] = set()

            for row in (sb.table("ticket_servers").select("server_id").execute().data or []):
                if row.get("server_id"):
                    ids.add(row["server_id"])
            for row in (sb.table("application_servers").select("server_id").execute().data or []):
                if row.get("server_id"):
                    ids.add(row["server_id"])

            # Discord-Name via Bot-Token holen (cached in app.py via _cached_guild)
            servers = []
            for sid in sorted(ids):
                try:
                    # _cached_guild ist in app.py definiert – wir importieren es direkt
                    from bot.core.web_app.flask_app.app import _cached_guild, _guild_icon_url
                    guild = _cached_guild(sid)
                    name  = guild.get("name", sid) if guild else sid
                    icon  = _guild_icon_url(guild) if guild else None
                except Exception:
                    name = sid
                    icon = None
                servers.append({"id": sid, "name": name, "icon": icon})

            return jsonify({"servers": servers})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # -- GET /api/import/modules/<server_id> ----------------------------------
    @app.route("/api/import/modules/<server_id>")
    @_mbl_only
    def api_import_modules(server_id):
        """Gibt Ticket-Module und ob Applications aktiviert sind."""
        try:
            from bot.core.supabase_client import get_supabase
            sb = get_supabase()

            # Ticket-Module
            rows = sb.table("ticket_modules").select("id,name,button_emoji") \
                     .eq("server_id", server_id).execute().data or []
            modules = [
                {
                    "id":    r["id"],
                    "name":  r["name"],
                    "emoji": r.get("button_emoji") or "🎫",
                }
                for r in rows
            ]

            # Application-System vorhanden?
            app_rows = sb.table("application_servers").select("server_id") \
                         .eq("server_id", server_id).execute().data or []
            has_applications = bool(app_rows)

            return jsonify({
                "modules":          modules,
                "has_applications": has_applications,
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # -- GET /api/import/status -----------------------------------------------
    @app.route("/api/import/status")
    @_mbl_only
    def api_import_status():
        servers = []
        if IMPORTS_DIR.exists():
            for server_dir in sorted(IMPORTS_DIR.iterdir()):
                if not server_dir.is_dir() or not server_dir.name.isdigit():
                    continue
                sid = server_dir.name

                ticket_count   = 0
                ticket_modules = []
                tickets_dir    = server_dir / "tickets"
                if tickets_dir.exists():
                    for mod_dir in tickets_dir.iterdir():
                        if mod_dir.is_dir():
                            # Temp-Ordner ignorieren
                            if mod_dir.name.startswith("_"):
                                continue
                            cnt = len(list(mod_dir.glob("*.html")))
                            ticket_count += cnt
                            if cnt:
                                ticket_modules.append(f"{mod_dir.name} ({cnt})")

                app_count = 0
                apps_dir  = server_dir / "applications"
                if apps_dir.exists():
                    app_count = len(list(apps_dir.glob("*.html")))

                try:
                    from bot.core.supabase_client import get_supabase
                    sb = get_supabase()
                    r_t = sb.table("tickets").select("ticket_id", count="exact") \
                            .eq("server_id", sid).eq("imported", True).execute()
                    r_a = sb.table("applications").select("app_id", count="exact") \
                            .eq("server_id", sid).eq("imported", True).execute()
                    db_t = r_t.count or 0
                    db_a = r_a.count or 0
                except Exception:
                    db_t = db_a = 0

                servers.append({
                    "server_id":           sid,
                    "files_tickets":       ticket_count,
                    "files_apps":          app_count,
                    "ticket_modules":      ticket_modules,
                    "db_imported_tickets": db_t,
                    "db_imported_apps":    db_a,
                })

        _prune_jobs()
        active = [
            {"job_id": jid, "status": j["status"], "server_id": j.get("server_id")}
            for jid, j in _import_jobs.items()
            if j["status"] in ("queued", "running")
        ]
        return jsonify({
            "imports_dir": str(IMPORTS_DIR),
            "servers":     servers,
            "active_jobs": active,
        })

    # -- POST /api/import/upload ----------------------------------------------
    @app.route("/api/import/upload", methods=["POST"])
    @_mbl_only
    def api_import_upload():
        """
        multipart/form-data:
            files[]     - .html Transcript-Dateien
            server_id   - Discord Server-ID
            import_type - "ticket" | "application"
            module_name - Modul-Name (nur fuer Tickets)

        Ablauf:
          1. Ordnerstruktur erstellen falls nicht vorhanden
          2. Dateien in den Ordner speichern
          3. Background-Import starten
          4. Nach Import: Dateien automatisch loeschen
        """
        server_id   = request.form.get("server_id",   "").strip()
        import_type = request.form.get("import_type", "ticket").strip()
        module_name = request.form.get("module_name", "").strip()
        files       = request.files.getlist("files[]")

        # Validierung
        if not server_id or not server_id.isdigit():
            return jsonify({"error": "Ungueltige oder fehlende server_id"}), 400
        if import_type not in ("ticket", "application"):
            return jsonify({"error": "import_type muss 'ticket' oder 'application' sein"}), 400
        if import_type == "ticket" and not module_name:
            return jsonify({"error": "module_name fehlt (Pflicht fuer Tickets)"}), 400
        if not files or all(not f.filename for f in files):
            return jsonify({"error": "Keine Dateien hochgeladen"}), 400

        # Nur .html
        valid_files = [f for f in files if f.filename and f.filename.lower().endswith(".html")]
        if not valid_files:
            return jsonify({"error": "Nur .html Transcript-Dateien werden akzeptiert"}), 400

        # Zielordner bestimmen und erstellen
        if import_type == "ticket":
            # Sanitize module_name fuer Dateisystem
            safe_module = "".join(
                c for c in module_name if c.isalnum() or c in " -_äöüÄÖÜß"
            ).strip() or "Unbekannt"
            target_dir = IMPORTS_DIR / server_id / "tickets" / safe_module
        else:
            target_dir = IMPORTS_DIR / server_id / "applications"

        target_dir.mkdir(parents=True, exist_ok=True)

        # Dateien speichern
        saved_paths: list[Path] = []
        total_size = 0

        for f in valid_files:
            safe_name = Path(f.filename).name
            # Pfad-Traversal verhindern
            safe_name = safe_name.replace("..", "").replace("/", "").replace("\\", "")
            if not safe_name.lower().endswith(".html"):
                safe_name += ".html"
            if not safe_name:
                continue

            content = f.read()
            total_size += len(content)
            if total_size > MAX_UPLOAD_BYTES:
                # Bereits gespeicherte Dateien wieder loeschen
                for p in saved_paths:
                    p.unlink(missing_ok=True)
                return jsonify({
                    "error": f"Gesamtgroesse ueberschreitet {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
                }), 413

            dest = target_dir / safe_name
            dest.write_bytes(content)
            saved_paths.append(dest)

        if not saved_paths:
            return jsonify({"error": "Keine gueltigen Dateien gespeichert"}), 400

        # Job starten
        job_id = str(uuid.uuid4())[:8]
        _import_jobs[job_id] = {
            "job_id":      job_id,
            "server_id":   server_id,
            "import_type": import_type,
            "module_name": module_name,
            "status":      "queued",
            "log":         [],
            "stats":       {},
            "started_at":  time.time(),
            "mode":        "upload",
            "file_count":  len(saved_paths),
        }

        t = threading.Thread(
            target=_run_upload_import,
            args=(job_id, saved_paths, server_id, import_type, module_name),
            daemon=True,
        )
        t.start()

        return jsonify({
            "job_id":     job_id,
            "status":     "queued",
            "file_count": len(saved_paths),
            "target_dir": str(target_dir.relative_to(_ROOT)),
        })

    # -- POST /api/import/run -------------------------------------------------
    @app.route("/api/import/run", methods=["POST"])
    @_mbl_only
    def api_import_run():
        data      = request.get_json() or {}
        server_id = data.get("server_id")
        all_mode  = bool(data.get("all", False))

        if not server_id and not all_mode:
            return jsonify({"error": "server_id oder all=true erforderlich"}), 400

        for j in _import_jobs.values():
            if j.get("server_id") == server_id and j["status"] == "running":
                return jsonify({"error": "Import laeuft bereits", "job_id": j["job_id"]}), 409

        job_id = str(uuid.uuid4())[:8]
        _import_jobs[job_id] = {
            "job_id":     job_id,
            "server_id":  server_id,
            "status":     "queued",
            "log":        [],
            "stats":      {},
            "started_at": time.time(),
            "mode":       "folder",
        }

        t = threading.Thread(
            target=_run_folder_import,
            args=(job_id, server_id, all_mode),
            daemon=True,
        )
        t.start()
        return jsonify({"job_id": job_id, "status": "queued"})

    # -- GET /api/import/progress/<job_id> ------------------------------------
    @app.route("/api/import/progress/<job_id>")
    @_mbl_only
    def api_import_progress(job_id):
        job = _import_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Job nicht gefunden"}), 404
        return jsonify({
            "job_id":  job_id,
            "status":  job["status"],
            "log":     job["log"][-150:],
            "stats":   job.get("stats", {}),
            "error":   job.get("error"),
            "mode":    job.get("mode", "folder"),
        })

    # -- GET /dashboard/import ------------------------------------------------
    @app.route("/dashboard/import")
    @login_required
    def import_dashboard():
        if MBL_ID and not _is_mbl(session["user"]):
            from flask import render_template
            return render_template(
                "error.html", code=403, title="Kein Zugriff",
                icon="🚫",
                msg="Nur MBL hat Zugriff auf das Import-Dashboard.",
            ), 403
        return render_template_string(_DASHBOARD_HTML, user=session["user"])


# ---- Dashboard HTML ---------------------------------------------------------
_DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>TicketTool Import – Insel Bot</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="stylesheet" href="/static/css/main.css">
  <style>
    /* ── Layout ───────────────────────────── */
    .page  { max-width: 1060px; margin: 0 auto; padding: 24px 22px; }
    .cols  { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 20px; }
    @media(max-width:820px){ .cols { grid-template-columns: 1fr; } }

    /* ── Panel ────────────────────────────── */
    .panel {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: var(--r-lg);
      overflow: hidden;
    }
    .panel-hd {
      display: flex; align-items: center; gap: 8px;
      padding: 11px 16px;
      border-bottom: 1px solid var(--border);
      background: var(--bg-surface);
    }
    .panel-hd-title {
      font-size: 0.60rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.08em; color: var(--text3);
    }
    .panel-body { padding: 16px 18px; }

    /* ── Drop-Zone ────────────────────────── */
    .drop-zone {
      position: relative;
      border: 2px dashed var(--border2);
      border-radius: var(--r);
      padding: 28px 20px;
      text-align: center;
      cursor: pointer;
      transition: all var(--mid);
    }
    .drop-zone:hover, .drop-zone.over {
      border-color: var(--green2);
      background: var(--green-g3);
    }
    .drop-zone input {
      position: absolute; inset: 0;
      opacity: 0; cursor: pointer;
      width: 100%; height: 100%;
    }
    .drop-icon  { font-size: 1.8rem; margin-bottom: 6px; }
    .drop-title { font-family: 'Rajdhani',sans-serif; font-weight: 700; color: var(--text2); font-size: 0.92rem; }
    .drop-sub   { font-size: 0.72rem; color: var(--text3); margin-top: 2px; }

    /* ── Datei-Liste ──────────────────────── */
    .file-list { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; max-height: 160px; overflow-y: auto; }
    .fi {
      display: flex; align-items: center; gap: 8px;
      padding: 5px 9px;
      background: var(--bg-surface); border: 1px solid var(--border);
      border-radius: var(--r-sm); font-size: 0.77rem;
    }
    .fi-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text2); }
    .fi-size { color: var(--text3); font-family: 'JetBrains Mono',monospace; font-size: 0.68rem; flex-shrink: 0; }
    .fi-del  {
      background: none; border: none; color: var(--text3);
      cursor: pointer; padding: 2px 5px; border-radius: 3px;
      transition: color var(--fast); line-height: 1; flex-shrink: 0; font-size: 0.9rem;
    }
    .fi-del:hover { color: var(--red); }

    /* ── Form-Felder ──────────────────────── */
    .f { margin-bottom: 12px; }
    .f label {
      display: block; font-size: 0.63rem; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--text3); margin-bottom: 4px;
    }
    .f select {
      width: 100%;
      background: var(--bg-surface); border: 1px solid var(--border);
      color: var(--text); font-family: 'Outfit',sans-serif;
      font-size: 0.85rem; padding: 8px 10px;
      border-radius: var(--r-sm); outline: none;
      transition: border-color var(--fast), box-shadow var(--fast);
      appearance: none;
      background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%238b95a8' stroke-width='2'%3E%3Cpolyline points='6 9 12 15 18 9'/%3E%3C/svg%3E");
      background-repeat: no-repeat;
      background-position: right 10px center;
      padding-right: 30px;
    }
    .f select:focus {
      border-color: var(--green2);
      box-shadow: 0 0 0 3px rgba(34,197,94,0.12);
    }
    .f select:disabled { opacity: 0.45; cursor: not-allowed; }
    .f select option { background: #1d2128; }

    /* ── Typ-Toggle ───────────────────────── */
    .type-toggle { display: flex; gap: 6px; }
    .type-btn {
      flex: 1; display: flex; align-items: center; justify-content: center; gap: 6px;
      padding: 8px 10px;
      background: var(--bg-surface); border: 1px solid var(--border);
      border-radius: var(--r-sm); cursor: pointer;
      font-size: 0.83rem; font-weight: 600; color: var(--text2);
      transition: all var(--fast);
    }
    .type-btn:hover { border-color: var(--border3); color: var(--text); }
    .type-btn.on    { border-color: var(--green); background: var(--green-glow); color: var(--green); }

    /* ── Upload-Button ────────────────────── */
    .upload-btn {
      width: 100%; padding: 11px;
      background: var(--green2); color: #000;
      font-family: 'Rajdhani',sans-serif; font-weight: 700; font-size: 0.95rem;
      border: none; border-radius: var(--r-sm);
      cursor: pointer; transition: all var(--mid); margin-top: 4px;
    }
    .upload-btn:hover:not(:disabled) { filter: brightness(1.1); transform: translateY(-1px); }
    .upload-btn:disabled { opacity: 0.4; cursor: not-allowed; }

    /* ── Upload-Fortschritt ───────────────── */
    .up-prog { display: none; margin-top: 8px; }
    .up-prog-bar { height: 3px; background: var(--border); border-radius: 2px; overflow: hidden; margin-bottom: 4px; }
    .up-prog-fill { height: 100%; background: var(--green2); border-radius: 2px; transition: width .2s ease; width: 0; }
    .up-prog-txt  { font-size: 0.70rem; color: var(--text3); text-align: center; }

    /* ── Job-Panel ────────────────────────── */
    .progress-strip { height: 3px; background: var(--border); }
    .progress-fill  {
      height: 100%; background: var(--green2); border-radius: 0;
      transition: width .3s ease; width: 0;
    }
    .progress-fill.anim {
      width: 40%;
      animation: indeterminate 1.4s ease infinite;
    }
    @keyframes indeterminate {
      0%   { transform: translateX(-150%); }
      100% { transform: translateX(400%); }
    }

    .log-box {
      background: var(--bg-base); border: 1px solid var(--border);
      border-radius: var(--r-sm); padding: 11px 13px;
      font-family: 'JetBrains Mono',monospace; font-size: 0.73rem;
      color: var(--text2); max-height: 260px; overflow-y: auto;
      white-space: pre-wrap; word-break: break-word;
    }
    .ll-ok   { color: #4ade80; }
    .ll-err  { color: #f87171; }
    .ll-skip { color: #555f6e; }
    .ll-warn { color: #fb923c; }

    .res-grid { display: grid; grid-template-columns: repeat(4,1fr); gap: 8px; padding: 14px 16px; }
    .res-box  { background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--r-sm); padding: 8px 10px; text-align: center; }
    .res-num  { font-family: 'Rajdhani',sans-serif; font-size: 1.35rem; font-weight: 700; color: var(--green); line-height: 1; }
    .res-num.bad { color: var(--red); }
    .res-lbl  { font-size: 0.58rem; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: var(--text3); margin-top: 3px; }

    /* ── Folder status ────────────────────── */
    .srv-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(300px,1fr)); gap: 12px; margin-top: 16px; }
    .srv-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--r-lg); padding: 15px 16px; }
    .srv-card h3 { font-family: 'Rajdhani',sans-serif; font-size: 0.92rem; font-weight: 700; color: var(--text); margin-bottom: 9px; }
    .sr { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 0.80rem; }
    .sr:last-of-type { border-bottom: none; }
    .sl { color: var(--text3); }
    .sv { font-family: 'JetBrains Mono',monospace; font-size: 0.76rem; }
    .mod-hint { font-size: 0.68rem; color: var(--text3); margin-top: 2px; font-style: italic; }
    .b-ok  { background: rgba(74,222,128,.10); color: var(--green); border: 1px solid rgba(74,222,128,.22); padding: 1px 6px; border-radius: 10px; font-size: 0.63rem; font-weight: 700; }
    .b-new { background: var(--orange-bg); color: var(--orange); border: 1px solid rgba(251,146,60,.28); padding: 1px 6px; border-radius: 10px; font-size: 0.63rem; font-weight: 700; }
    .fold-btn {
      display: flex; align-items: center; justify-content: center; gap: 5px;
      width: 100%; padding: 8px; margin-top: 10px;
      background: var(--bg-surface); border: 1px solid var(--border2);
      color: var(--text2); font-family: 'Rajdhani',sans-serif; font-weight: 700; font-size: 0.85rem;
      border-radius: var(--r-sm); cursor: pointer; transition: all var(--mid);
    }
    .fold-btn:hover:not(:disabled) { border-color: var(--green2); color: var(--green2); background: var(--green-g3); }
    .fold-btn:disabled { opacity: 0.38; cursor: not-allowed; }

    /* ── Misc ─────────────────────────────── */
    .divider { border: none; border-top: 1px solid var(--border); margin: 24px 0; }
    .section-title { font-family: 'Rajdhani',sans-serif; font-size: 1.1rem; font-weight: 700; color: var(--text); margin-bottom: 4px; }
    .section-sub   { font-size: 0.80rem; color: var(--text3); margin-bottom: 14px; }
    .info-box {
      background: var(--bg-surface); border: 1px solid var(--border);
      border-left: 3px solid var(--blue); border-radius: var(--r-sm);
      padding: 10px 14px; font-size: 0.80rem; color: var(--text2); line-height: 1.6;
    }
    .info-box code { background: var(--bg-raised); padding: 1px 5px; border-radius: 3px; font-size: 0.76rem; color: var(--green); }
    .topbar-refresh {
      background: var(--bg-surface); border: 1px solid var(--border);
      color: var(--text2); padding: 5px 13px; border-radius: var(--r-sm);
      cursor: pointer; font-size: 0.78rem; transition: all var(--fast); margin-left: auto;
    }
    .topbar-refresh:hover { border-color: var(--green2); color: var(--green); }
    #noJobPanel {
      display: flex; align-items: center; justify-content: center;
      min-height: 180px; color: var(--text3); font-size: 0.83rem; text-align: center;
    }
  </style>
</head>
<body class="detail-body">

<div class="detail-topbar">
  <a href="/dashboard" class="back-link">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
      <path d="M15 18l-6-6 6-6"/>
    </svg>
    Dashboard
  </a>
  <span class="topbar-sep">/</span>
  <span class="topbar-title">TicketTool Import</span>
  <button class="topbar-refresh" onclick="refreshAll()">↺ Aktualisieren</button>
</div>

<div class="page">

  <!-- ══ UPLOAD ════════════════════════════════════════════════════════════ -->
  <div class="cols">

    <!-- Linke Seite: Formular -->
    <div class="panel">
      <div class="panel-hd">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="16 16 12 12 8 16"/>
          <line x1="12" y1="12" x2="12" y2="21"/>
          <path d="M20.39 18.39A5 5 0 0018 9h-1.26A8 8 0 103 16.3"/>
        </svg>
        <span class="panel-hd-title">Dateien hochladen &amp; importieren</span>
      </div>
      <div class="panel-body">

        <!-- Drop-Zone -->
        <div class="drop-zone" id="dropZone">
          <input type="file" id="fileInput" multiple accept=".html"
                 onchange="onFiles(this.files)">
          <div class="drop-icon">📂</div>
          <div class="drop-title">HTML-Dateien ablegen oder klicken</div>
          <div class="drop-sub">Nur .html · max. 50 MB gesamt</div>
        </div>

        <!-- Upload-Fortschrittsanzeige -->
        <div class="up-prog" id="upProg">
          <div class="up-prog-bar"><div class="up-prog-fill" id="upFill"></div></div>
          <div class="up-prog-txt" id="upTxt">Hochladen...</div>
        </div>

        <!-- Datei-Liste -->
        <div class="file-list" id="fileList"></div>

        <!-- Server-Dropdown -->
        <div class="f" style="margin-top:14px;">
          <label>Server</label>
          <select id="selServer" onchange="onServerChange(this.value)">
            <option value="">Lade Server...</option>
          </select>
        </div>

        <!-- Typ-Toggle -->
        <div class="f">
          <label>Typ</label>
          <div class="type-toggle">
            <div class="type-btn on" id="btnTicket" onclick="setType('ticket')">🎫 Ticket</div>
            <div class="type-btn"   id="btnApp"    onclick="setType('application')">📋 Bewerbung</div>
          </div>
        </div>

        <!-- Modul-Dropdown (nur Tickets) -->
        <div class="f" id="modField">
          <label>Ticket-Modul</label>
          <select id="selModule" onchange="checkBtn()">
            <option value="">Erst Server auswählen</option>
          </select>
        </div>

        <!-- Import-Button -->
        <button class="upload-btn" id="uploadBtn" onclick="doUpload()" disabled>
          📥 Importieren
        </button>

      </div>
    </div>

    <!-- Rechte Seite: Job-Log -->
    <div class="panel" id="jobPanel" style="display:none;">
      <div class="panel-hd">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        </svg>
        <span class="panel-hd-title">Import-Fortschritt</span>
        <span id="jobBadge" style="margin-left:auto;font-size:0.74rem;color:var(--text2);"></span>
      </div>
      <div class="progress-strip">
        <div class="progress-fill anim" id="progFill"></div>
      </div>
      <div style="padding:12px 16px 0;">
        <div class="log-box" id="jobLog">Warte auf Output...</div>
      </div>
      <div class="res-grid" id="resGrid" style="display:none;"></div>
    </div>

    <div id="noJobPanel" class="panel">
      <div>
        <div style="font-size:1.6rem;margin-bottom:8px;opacity:.35;">⚡</div>
        Lade Dateien hoch um den Import zu starten.
      </div>
    </div>

  </div>

  <hr class="divider">

  <!-- ══ ORDNER-STATUS ═════════════════════════════════════════════════════ -->
  <div class="section-title">Ordner-Import (imports/)</div>
  <div class="section-sub" id="importsDirLbl"></div>

  <div class="info-box" style="margin-bottom:16px;">
    Dateien die per Upload importiert wurden werden danach <strong>automatisch gelöscht</strong>.
    Alternativ kannst du Transcripts manuell in
    <code>imports/&lt;SERVER_ID&gt;/tickets/&lt;MODUL&gt;/</code> oder
    <code>imports/&lt;SERVER_ID&gt;/applications/</code> ablegen –
    diese werden beim Ordner-Import <strong>nicht</strong> gelöscht.
  </div>

  <div class="srv-grid" id="srvGrid">
    <div style="color:var(--text3);font-size:0.83rem;">Lade...</div>
  </div>
  <div id="noFolders" style="display:none;text-align:center;padding:30px;color:var(--text3);font-size:0.83rem;">
    Keine Import-Ordner vorhanden.
  </div>

</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
let _files   = [];
let _type    = 'ticket';
let _jobId   = null;
let _poll    = null;

// ── Hilfsfunktionen ───────────────────────────────────────────────────────
const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const fmt = b => b < 1024 ? b+'B' : b < 1048576 ? (b/1024).toFixed(1)+'KB' : (b/1048576).toFixed(1)+'MB';

function q(id) { return document.getElementById(id); }

// ── Drag & Drop ───────────────────────────────────────────────────────────
const dz = q('dropZone');
dz.addEventListener('dragover',  e => { e.preventDefault(); dz.classList.add('over'); });
dz.addEventListener('dragleave', ()  => dz.classList.remove('over'));
dz.addEventListener('drop',      e  => {
  e.preventDefault(); dz.classList.remove('over');
  onFiles(e.dataTransfer.files);
});

function onFiles(fl) {
  const html = Array.from(fl).filter(f => f.name.toLowerCase().endsWith('.html'));
  if (!html.length) { alert('Nur .html Dateien werden akzeptiert.'); return; }
  const existing = new Set(_files.map(f => f.name));
  html.forEach(f => { if (!existing.has(f.name)) { _files.push(f); existing.add(f.name); } });
  renderFiles(); checkBtn();
}

function removeFile(i) { _files.splice(i, 1); renderFiles(); checkBtn(); }

function renderFiles() {
  q('fileList').innerHTML = _files.map((f, i) =>
    `<div class="fi">
       <span style="color:var(--green);flex-shrink:0;font-size:.85rem;">📄</span>
       <span class="fi-name" title="${esc(f.name)}">${esc(f.name)}</span>
       <span class="fi-size">${fmt(f.size)}</span>
       <button class="fi-del" onclick="removeFile(${i})">✕</button>
     </div>`
  ).join('');
}

// ── Typ-Toggle ────────────────────────────────────────────────────────────
function setType(t) {
  _type = t;
  q('btnTicket').classList.toggle('on', t === 'ticket');
  q('btnApp').classList.toggle('on', t === 'application');
  q('modField').style.display = t === 'ticket' ? '' : 'none';
  checkBtn();
}

// ── Server & Module laden ─────────────────────────────────────────────────
async function loadServers() {
  const sel = q('selServer');
  sel.innerHTML = '<option value="">Lade Server...</option>';
  sel.disabled  = true;

  try {
    const r    = await fetch('/api/import/servers');
    const data = await r.json();

    if (!r.ok || !data.servers) {
      sel.innerHTML = '<option value="">Fehler beim Laden</option>';
      return;
    }

    if (data.servers.length === 0) {
      sel.innerHTML = '<option value="">Keine Server konfiguriert</option>';
      return;
    }

    sel.innerHTML = '<option value="">-- Server auswählen --</option>' +
      data.servers.map(srv =>
        `<option value="${esc(srv.id)}">${esc(srv.name)} (${esc(srv.id)})</option>`
      ).join('');
    sel.disabled = false;

  } catch(e) {
    sel.innerHTML = '<option value="">Fehler: ' + esc(e.message) + '</option>';
  }
}

async function onServerChange(sid) {
  const modSel = q('selModule');
  modSel.innerHTML = '<option value="">Lade Module...</option>';
  modSel.disabled  = true;
  checkBtn();

  if (!sid) {
    modSel.innerHTML = '<option value="">Erst Server auswählen</option>';
    return;
  }

  try {
    const r    = await fetch('/api/import/modules/' + sid);
    const data = await r.json();

    if (!r.ok) {
      modSel.innerHTML = '<option value="">Fehler beim Laden</option>';
      return;
    }

    const mods = data.modules || [];
    if (mods.length === 0) {
      modSel.innerHTML = '<option value="">Keine Module gefunden</option>';
    } else {
      modSel.innerHTML = '<option value="">-- Modul auswählen --</option>' +
        mods.map(m => {
          // Custom-Emojis (<:name:id>) im Label weglassen
          const label = m.emoji && !m.emoji.startsWith('<') ? m.emoji + ' ' + m.name : m.name;
          return `<option value="${esc(m.name)}">${esc(label)}</option>`;
        }).join('');
    }
    modSel.disabled = false;

    // Wenn Applications vorhanden: App-Button aktivieren, sonst deaktivieren
    q('btnApp').style.opacity = data.has_applications ? '' : '0.4';
    q('btnApp').style.pointerEvents = data.has_applications ? '' : 'none';

  } catch(e) {
    modSel.innerHTML = '<option value="">Fehler: ' + esc(e.message) + '</option>';
  }
  checkBtn();
}

// ── Button-State ──────────────────────────────────────────────────────────
function checkBtn() {
  const sid  = q('selServer').value;
  const mod  = q('selModule').value;
  const ok   = _files.length > 0 && !!sid && (_type === 'application' || !!mod);
  q('uploadBtn').disabled = !ok;
}

// ── Upload & Import ───────────────────────────────────────────────────────
async function doUpload() {
  const sid  = q('selServer').value;
  const mod  = q('selModule').value;
  if (!_files.length || !sid) return;
  if (_type === 'ticket' && !mod) { alert('Bitte ein Modul auswählen.'); return; }

  q('uploadBtn').disabled = true;
  showJob('Dateien werden hochgeladen...');

  // Fortschritts-UI
  const wp  = q('upProg');
  const bar = q('upFill');
  const txt = q('upTxt');
  wp.style.display = '';
  bar.style.width  = '0%';

  const fd = new FormData();
  fd.append('server_id',   sid);
  fd.append('import_type', _type);
  fd.append('module_name', mod);
  _files.forEach(f => fd.append('files[]', f));

  let jobId;
  try {
    jobId = await new Promise((ok, fail) => {
      const xhr = new XMLHttpRequest();

      xhr.upload.addEventListener('progress', e => {
        if (e.lengthComputable) {
          const p = Math.round(e.loaded / e.total * 100);
          bar.style.width = p + '%';
          txt.textContent = `${fmt(e.loaded)} / ${fmt(e.total)} (${p}%)`;
        }
      });

      xhr.addEventListener('load', () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          const d = JSON.parse(xhr.responseText);
          ok(d.job_id);
        } else {
          try   { fail(new Error(JSON.parse(xhr.responseText).error || 'Upload fehlgeschlagen')); }
          catch  { fail(new Error('Upload fehlgeschlagen: ' + xhr.status)); }
        }
      });
      xhr.addEventListener('error', () => fail(new Error('Netzwerkfehler')));
      xhr.open('POST', '/api/import/upload');
      xhr.send(fd);
    });
  } catch(e) {
    wp.style.display = 'none';
    q('uploadBtn').disabled = false;
    showJob('Fehler: ' + e.message, true);
    q('jobBadge').textContent = 'Fehler ❌';
    q('progFill').className = 'progress-fill';
    q('progFill').style.cssText = 'width:0;background:var(--red)';
    return;
  }

  wp.style.display = 'none';
  _files = []; renderFiles(); checkBtn();
  _jobId = jobId;
  updateJob('running', [], {});
  if (_poll) clearInterval(_poll);
  _poll = setInterval(pollJob, 800);
}

// ── Job-Panel ─────────────────────────────────────────────────────────────
function showJob(msg, isError) {
  q('noJobPanel').style.display = 'none';
  q('jobPanel').style.display   = '';
  q('jobLog').textContent       = msg;
  q('resGrid').style.display    = 'none';
  q('progFill').className       = 'progress-fill anim';
  q('progFill').style.cssText   = '';
  q('jobBadge').textContent     = isError ? 'Fehler ❌' : 'läuft...';
}

function updateJob(status, logLines, data) {
  // Log rendern mit Farben
  if (logLines.length > 0) {
    q('jobLog').innerHTML = logLines.map(l => {
      let c = '';
      if (/^✅|^✓/.test(l))                   c = 'll-ok';
      else if (/^❌|^Fehler/.test(l))          c = 'll-err';
      else if (/^⏭️|bereits importiert/.test(l)) c = 'll-skip';
      else if (/^⚠️/.test(l))                 c = 'll-warn';
      return c ? `<span class="${c}">${esc(l)}</span>` : esc(l);
    }).join('\n');
    q('jobLog').scrollTop = q('jobLog').scrollHeight;
  }

  const badges = { queued:'Warteschlange', running:'läuft...', done:'Fertig ✅', error:'Fehler ❌' };
  q('jobBadge').textContent = badges[status] || status;

  if (status === 'done' || status === 'error') {
    const pf = q('progFill');
    pf.className = 'progress-fill';
    pf.style.width      = status === 'done' ? '100%' : '30%';
    pf.style.background = status === 'done' ? 'var(--green2)' : 'var(--red)';

    if (data.stats && status === 'done') {
      const s = data.stats;
      q('resGrid').style.display = 'grid';
      q('resGrid').innerHTML = `
        <div class="res-box"><div class="res-num">${s.tickets_created||0}</div><div class="res-lbl">Tickets</div></div>
        <div class="res-box"><div class="res-num">${s.apps_created||0}</div><div class="res-lbl">Bewerbungen</div></div>
        <div class="res-box"><div class="res-num">${s.messages_created||0}</div><div class="res-lbl">Nachrichten</div></div>
        <div class="res-box">
          <div class="res-num ${(s.files_error||0)>0?'bad':''}">${(s.files_skipped||0)+(s.files_error||0)}</div>
          <div class="res-lbl">Skip/Err</div>
        </div>`;
    } else if (status === 'error' && data.error) {
      q('resGrid').style.display = 'grid';
      q('resGrid').innerHTML = `
        <div class="res-box" style="grid-column:1/-1;border-color:var(--red-border);background:var(--red-bg);">
          <div style="color:var(--red);font-size:.83rem;">${esc(data.error)}</div>
        </div>`;
    }

    checkBtn();
    loadStatus();
  }
}

async function pollJob() {
  if (!_jobId) return;
  try {
    const r = await fetch('/api/import/progress/' + _jobId);
    const d = await r.json();
    updateJob(d.status, d.log||[], d);
    if (d.status === 'done' || d.status === 'error') {
      clearInterval(_poll); _poll = null;
    }
  } catch(_) {}
}

// ── Ordner-Status ─────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const r = await fetch('/api/import/status');
    if (!r.ok) return;
    const d = await r.json();

    q('importsDirLbl').textContent = d.imports_dir ? '📁 ' + d.imports_dir : '';

    const grid = q('srvGrid');
    const none = q('noFolders');

    if (!d.servers || d.servers.length === 0) {
      grid.style.display = 'none'; none.style.display = '';
      return;
    }
    grid.style.display = ''; none.style.display = 'none';

    const running = sid => d.active_jobs.some(j => j.server_id === sid);

    grid.innerHTML = d.servers.map(srv => {
      const pT = Math.max(0, srv.files_tickets - srv.db_imported_tickets);
      const pA = Math.max(0, srv.files_apps    - srv.db_imported_apps);
      const r2 = running(srv.server_id);
      const mh = srv.ticket_modules.length
        ? '<div class="mod-hint">Module: ' + srv.ticket_modules.map(m => esc(m)).join(' · ') + '</div>'
        : '';

      return `<div class="srv-card">
        <h3>Server <code style="font-size:.76rem;color:var(--green);">${esc(srv.server_id)}</code></h3>
        <div class="sr"><span class="sl">Ticket-Dateien</span>
          <span class="sv">${srv.files_tickets} ${pT>0?`<span class="b-new">+${pT} neu</span>`:'<span class="b-ok">✓</span>'}</span></div>
        ${mh ? `<div style="padding:2px 0 4px;">${mh}</div>` : ''}
        <div class="sr"><span class="sl">Bewerbungs-Dateien</span>
          <span class="sv">${srv.files_apps} ${pA>0?`<span class="b-new">+${pA} neu</span>`:'<span class="b-ok">✓</span>'}</span></div>
        <div class="sr"><span class="sl">DB Tickets importiert</span><span class="sv">${srv.db_imported_tickets}</span></div>
        <div class="sr"><span class="sl">DB Bewerbungen importiert</span><span class="sv">${srv.db_imported_apps}</span></div>
        <button class="fold-btn" id="fb-${esc(srv.server_id)}"
          onclick="folderImport('${esc(srv.server_id)}')" ${r2?'disabled':''}>
          ${r2 ? '⏳ läuft...' : '📁 Ordner-Import starten'}
        </button>
      </div>`;
    }).join('');
  } catch(e) { console.error(e); }
}

async function folderImport(sid) {
  if (!confirm('Ordner-Import für Server ' + sid + ' starten?')) return;
  const btn = q('fb-' + sid);
  if (btn) { btn.disabled = true; btn.textContent = 'Startet...'; }

  const r = await fetch('/api/import/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ server_id: sid }),
  });
  const d = await r.json();

  if (!r.ok) { alert('Fehler: ' + (d.error||'?')); if(btn){btn.disabled=false;btn.textContent='📁 Ordner-Import starten';} return; }

  _jobId = d.job_id;
  showJob('Ordner-Import gestartet...');
  if (_poll) clearInterval(_poll);
  _poll = setInterval(pollJob, 800);
}

function refreshAll() { loadServers(); loadStatus(); }

// ── Init ──────────────────────────────────────────────────────────────────
loadServers();
loadStatus();
setInterval(loadStatus, 30000);
</script>
</body>
</html>
"""