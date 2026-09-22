from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

import discord

from .config import _is_mbl


_DURATION_RE = re.compile(
    r"^\s*(\d+)\s*"
    r"(s|sec|secs|second|seconds|"
    r"m|min|mins|minute|minutes|"
    r"h|hr|hrs|hour|hours|"
    r"d|day|days|"
    r"w|week|weeks)\s*$",
    re.IGNORECASE,
)


def parse_duration(value: str) -> Optional[timedelta]:
    """Parst '10m', '1h', '2d', '1w' etc. -> timedelta. None bei Fehler."""
    if not value:
        return None
    match = _DURATION_RE.match(value.strip())
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("s"):
        return timedelta(seconds=amount)
    if unit.startswith("m"):
        return timedelta(minutes=amount)
    if unit.startswith("h"):
        return timedelta(hours=amount)
    if unit.startswith("d"):
        return timedelta(days=amount)
    if unit.startswith("w"):
        return timedelta(weeks=amount)
    return None


def format_duration(td: timedelta) -> str:
    total = int(td.total_seconds())
    if total % 604800 == 0:
        return f"{total // 604800}w"
    if total % 86400 == 0:
        return f"{total // 86400}d"
    if total % 3600 == 0:
        return f"{total // 3600}h"
    if total % 60 == 0:
        return f"{total // 60}m"
    return f"{total}s"


async def is_moderator(
    user: discord.abc.User,
    guild: discord.Guild,
    db,
) -> bool:
    """
    Berechtigung fuer alle Raid-System-Aktionen:
      - MBL-User
      - Server-Administratoren
      - Traeger einer Moderator-Rolle (ehem. 'ignored role')
    """
    if _is_mbl(user.id):
        return True
    if isinstance(user, discord.Member):
        if user.guild_permissions.administrator:
            return True
        role_ids = await db.get_moderator_role_ids(guild)
        if any(str(r.id) in role_ids for r in user.roles):
            return True
    return False