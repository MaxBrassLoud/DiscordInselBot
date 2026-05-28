import logging
import logging.handlers
import sys
import os

def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)

        # Ordner "Log" erstellen (falls nicht vorhanden)
        log_dir = "Log"
        os.makedirs(log_dir, exist_ok=True)

        # Konsolen-Handler (wie gehabt)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(console_handler)

        # Datei-Handler mit täglicher Rotation
        log_file = os.path.join(log_dir, "app.log")
        file_handler = logging.handlers.TimedRotatingFileHandler(
            log_file,
            when="midnight",      # tägliche Rotation um Mitternacht
            interval=1,
            backupCount=30,       # ältere Logs werden automatisch gelöscht (optional)
            encoding="utf-8"
        )
        # Gleiches Format wie in der Konsole
        file_handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(name)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(file_handler)

    return logger