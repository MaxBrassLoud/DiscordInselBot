import logging
import logging.handlers
import sys
import os

_initialized = False

def _setup_root_logger():
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # oder INFO – wie du magst

    # Ordner anlegen
    log_dir = "Log"
    os.makedirs(log_dir, exist_ok=True)

    # Formatter
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Konsolen-Handler
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Datei-Handler (nur EINMAL!)
    log_file = os.path.join(log_dir, "app.log")
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Optional: verhindern, dass Flask/Bibliotheken doppelt loggen
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

def get_logger(name: str) -> logging.Logger:
    """Liefert einen Logger, der an die zentrale Konfiguration angebunden ist."""
    _setup_root_logger()
    return logging.getLogger(name)