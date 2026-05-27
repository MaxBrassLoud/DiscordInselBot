from __future__ import annotations

import os

from flask import render_template


def _env(name: str, fallback: str) -> str:
    return os.getenv(name, fallback).strip() or fallback


def _legal_details() -> dict[str, str]:
    return {
        "owner_name": _env("LEGAL_OWNER_NAME", "MaxBrassLoud / Die Insel"),
        #"address": _env("LEGAL_ADDRESS", "Bitte vollstaendige Betreiberadresse eintragen"),
        "email": _env("LEGAL_EMAIL", "info@samacraft.de"),
        #"phone": _env("LEGAL_PHONE", "Nicht angegeben"),
        "responsible": _env("LEGAL_RESPONSIBLE", "MaxBrassLoud"),
        #"supervisory_authority": _env(
        #    "LEGAL_SUPERVISORY_AUTHORITY",
        #    "Zustaendige Datenschutzaufsichtsbehoerde am Wohn- oder Sitzort des Verantwortlichen",
        #),
        "last_updated": _env("LEGAL_LAST_UPDATED", "21.05.2026"),
    }


def register_legal_routes(app):
    @app.route("/datenschutz")
    def privacy_policy():
        details = _legal_details()
        sections = [
            {
                "title": "1. Verantwortlicher",
                "body": [
                    f"Verantwortlich fuer die Verarbeitung personenbezogener Daten ist {details['owner_name']}.",
                    f"Kontakt: {details['email']}",
                ],
            },
            {
                "title": "2. Welche Daten verarbeitet werden",
                "body": [
                    "Beim Besuch dieser Website koennen technische Zugriffsdaten wie IP-Adresse, Zeitpunkt, angefragte URL, Browser-Informationen und Server-Logs verarbeitet werden.",
                    "Bei der Anmeldung ueber Discord OAuth werden Discord-ID, Benutzername, Anzeigename, Avatar-URL und zugehoerige Rolleninformationen verarbeitet, soweit dies fuer Login, Dashboard-Zugriff, Abstimmungen, Tickets oder Bewerbungen erforderlich ist.",
                    "Bei Bot-Funktionen koennen je nach Modul Inhalte wie Ticket-Nachrichten, Bewerbungsdaten, Minecraft-Name, Moderationsprotokolle, Abstimmungsstatus und Konfigurationsdaten verarbeitet werden.",
                ],
            },
            {
                "title": "3. Zwecke und Rechtsgrundlagen",
                "body": [
                    "Die Verarbeitung erfolgt zur Bereitstellung des Discord-Bots, zur Zugriffskontrolle, zur Bearbeitung von Tickets und Bewerbungen, zur Durchfuehrung von Abstimmungen und zur technischen Sicherheit des Dienstes.",
                    "Rechtsgrundlagen sind insbesondere Art. 6 Abs. 1 lit. b DSGVO fuer nutzungsbezogene Funktionen, Art. 6 Abs. 1 lit. f DSGVO fuer Sicherheit, Missbrauchsschutz und Protokollierung sowie Art. 6 Abs. 1 lit. a DSGVO, soweit eine Einwilligung erforderlich ist.",
                ],
            },
            {
                "title": "4. Empfaenger und Dienste",
                "body": [
                    "Daten koennen an Discord uebermittelt werden, wenn Discord OAuth, Bot-API-Aufrufe, Avatare oder Servermitgliedschaften genutzt werden.",
                    "Die Anwendung nutzt Supabase zur Speicherung von Konfigurations- und Funktionsdaten. Je nach Hosting koennen technische Logs beim Hosting-Anbieter entstehen.",
                    "Eine Weitergabe an Dritte erfolgt nur, soweit sie fuer den Betrieb erforderlich ist oder eine rechtliche Pflicht besteht.",
                ],
            },
            {
                "title": "5. Cookies und Sessions",
                "body": [
                    "Die Website verwendet technisch notwendige Session-Cookies, um Login-Zustand, OAuth-Schutzwerte und Abstimmungs-Sitzungen zu verwalten.",
                    "Diese Cookies dienen nicht dem Tracking zu Werbezwecken.",
                ],
            },
            {
                "title": "6. Speicherdauer",
                "body": [
                    "Personenbezogene Daten werden nur so lange gespeichert, wie sie fuer die genannten Zwecke erforderlich sind.",
                    "Tickets, Bewerbungen, Abstimmungsdaten und Logs koennen geloescht oder anonymisiert werden, sobald sie fuer Serververwaltung, Nachvollziehbarkeit oder Missbrauchsschutz nicht mehr benoetigt werden.",
                ],
            },
            {
                "title": "7. Rechte betroffener Personen",
                "body": [
                    "Betroffene Personen haben nach Massgabe der DSGVO Rechte auf Auskunft, Berichtigung, Loeschung, Einschraenkung der Verarbeitung, Datenuebertragbarkeit und Widerspruch.",
                    "Soweit die Verarbeitung auf Einwilligung beruht, kann diese Einwilligung jederzeit mit Wirkung fuer die Zukunft widerrufen werden.",
                    f"Anfragen koennen an {details['email']} gerichtet werden. Ausserdem besteht ein Beschwerderecht bei einer Datenschutzaufsichtsbehoerde.",
                    #f"Zustaendige Aufsichtsbehoerde: {details['supervisory_authority']}",
                ],
            },
        ]
        return render_template("legal.html", page="datenschutz", details=details, sections=sections)

    @app.route("/impressum")
    def imprint():
        details = _legal_details()
        sections = [
            {
                "title": "Angaben gemaess § 5 DDG",
                "body": [
                    details["owner_name"],
                    #details["address"],
                ],
            },
            {
                "title": "Kontakt",
                "body": [
                    f"E-Mail: {details['email']}",
                    #f"Telefon: {details['phone']}",
                ],
            },
            {
                "title": "Verantwortlich fuer den Inhalt",
                "body": [
                    details["responsible"],
                    #details["address"],
                ],
            },
            {
                "title": "Haftung fuer Inhalte",
                "body": [
                    "Die Inhalte dieser Website wurden mit Sorgfalt erstellt. Fuer die Richtigkeit, Vollstaendigkeit und Aktualitaet der Inhalte wird keine Gewaehr uebernommen.",
                    "Als Diensteanbieter sind wir fuer eigene Inhalte nach den allgemeinen Gesetzen verantwortlich.",
                ],
            },
            {
                "title": "Haftung fuer Links",
                "body": [
                    "Diese Website kann Links zu externen Angeboten enthalten. Fuer deren Inhalte sind ausschliesslich die jeweiligen Anbieter verantwortlich.",
                ],
            },
        ]
        return render_template("legal.html", page="impressum", details=details, sections=sections)
