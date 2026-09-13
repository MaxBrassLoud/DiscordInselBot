"""Zeitzonen-Helfer fuer serverweite, benutzerseitige Uhrzeiten.

Zeitpunkte werden in der Datenbank immer als UTC gespeichert. Nur bei einer
Eingabe wird die konfigurierte Server-Zeitzone verwendet. ``zoneinfo`` bringt
die Regeln fuer Sommer- und Winterzeit bereits mit Python mit.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Europe/Berlin"


def get_timezone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE)


def timezone_name(settings: dict | None) -> str:
    return (settings or {}).get("timezone") or DEFAULT_TIMEZONE


def local_now(settings: dict | None = None) -> datetime:
    return datetime.now(get_timezone(timezone_name(settings)))


def parse_local_time(value: str, tz: ZoneInfo) -> datetime | str | None:
    """Parst die im Server lokalisierte Eingabe und gibt einen UTC-Zeitpunkt zurück."""
    value = value.strip()
    if value == "-1":
        return "-1"
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m. %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now(tz).year)
            return parsed.replace(tzinfo=tz).astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        hour, minute = (int(part) for part in value.split(":"))
        now = datetime.now(tz)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def parse_stored_time(value: str | None, legacy_tz: ZoneInfo | None = None) -> datetime | None:
    """Liest neue UTC- sowie alte ISO-Zeitstempel sicher als UTC ein."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=legacy_tz or get_timezone(DEFAULT_TIMEZONE))
    return parsed.astimezone(timezone.utc)
