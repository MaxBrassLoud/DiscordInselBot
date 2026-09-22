from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class RaidCase:
    guild_id: int
    user_id: int
    until: Optional[datetime] = None
    case_id: Optional[str] = None


@dataclass
class ImageEvidence:
    url: str
    filename: str
    sha1: str
    phash: str
    forbidden_match: Optional[dict] = None  # {"sha1": ..., "phash": ...}