import os
import json
from datetime import datetime
from pathlib import Path
from bot.utils.logger import get_logger

logger = get_logger("tickets.storage")

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent.parent.parent / "Tickets"


def _ticket_dir(server_id: str, ticket_id: int) -> Path:
    path = BASE_DIR / str(server_id) / str(ticket_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_ticket(server_id: str, ticket_id: int, ticket_data: dict):
    path = _ticket_dir(server_id, ticket_id) / "ticket.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ticket_data, f, ensure_ascii=False, indent=2)
    logger.info(f"Ticket {ticket_id} gespeichert: {path}")


def load_ticket(server_id: str, ticket_id: int) -> dict | None:
    path = _ticket_dir(server_id, ticket_id) / "ticket.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def update_ticket(server_id: str, ticket_id: int, updates: dict):
    data = load_ticket(server_id, ticket_id) or {}
    data.update(updates)
    save_ticket(server_id, ticket_id, data)


def save_messages(server_id: str, ticket_id: int, messages: list):
    path = _ticket_dir(server_id, ticket_id) / "messages.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)


def load_messages(server_id: str, ticket_id: int) -> list:
    path = _ticket_dir(server_id, ticket_id) / "messages.json"
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def append_message(server_id: str, ticket_id: int, user: str, user_id: str, content: str, attachments: list = None):
    messages = load_messages(server_id, ticket_id)
    messages.append({
        "timestamp":   datetime.utcnow().isoformat(),
        "user":        user,
        "user_id":     user_id,
        "message":     content,
        "attachments": attachments or [],
    })
    save_messages(server_id, ticket_id, messages)


def get_all_tickets_for_server(server_id: str) -> list[dict]:
    server_dir = BASE_DIR / str(server_id)
    if not server_dir.exists():
        return []
    tickets = []
    for ticket_dir in server_dir.iterdir():
        if ticket_dir.is_dir():
            ticket_file = ticket_dir / "ticket.json"
            if ticket_file.exists():
                with open(ticket_file, "r", encoding="utf-8") as f:
                    tickets.append(json.load(f))
    return tickets