"""
tickets/html_export.py
=======================
Stub – die HTML-Generierung wurde nach manager.py (_build_log_html) verschoben.
Diese Datei bleibt für Rückwärtskompatibilität, falls noch irgendwo importiert wird.
"""

from __future__ import annotations


def generate_html_export(ticket: dict, messages: list) -> str:
    """Delegiert an den manager. Nur noch für Kompatibilität vorhanden."""
    from .manager import _build_log_html
    import discord

    class _FakeCloser:
        display_name = ticket.get("closed_by") or "System"

    return _build_log_html(ticket, messages, _FakeCloser()).decode("utf-8")


def save_html_export(server_id: str, ticket_id: int, ticket: dict, messages: list):
    """No-op – kein Dateisystem mehr. HTML wird direkt in-memory beim DM gebaut."""
    pass