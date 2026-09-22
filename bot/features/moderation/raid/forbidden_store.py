from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from bot.utils.logger import get_logger

from .hashing import is_similar

logger = get_logger("raid.forbidden")


class ForbiddenImageStore:
    """
    Speichert verbotene Bilder als JSON-Datei im Format:
        {
            "<sha1_hex>": "<phash_hex>",
            ...
        }
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = asyncio.Lock()
        self._cache: Optional[dict[str, str]] = None
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        d = os.path.dirname(self.path)
        if d and not os.path.exists(d):
            os.makedirs(d, exist_ok=True)

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not os.path.exists(self.path):
            self._cache = {}
            return self._cache
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            self._cache = {str(k): str(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"[forbidden] load failed: {e}")
            self._cache = {}
        return self._cache

    def _save(self, data: dict[str, str]) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            self._cache = data
        except Exception as e:
            logger.warning(f"[forbidden] save failed: {e}")

    # --- Public API ---

    def exact_match(self, sha1: str) -> bool:
        return sha1 in self._load()

    def find_similar(self, phash: str, threshold: int = 5) -> Optional[dict]:
        data = self._load()
        for sha1, stored_phash in data.items():
            if is_similar(phash, stored_phash, threshold):
                return {"sha1": sha1, "phash": stored_phash}
        return None

    async def add(self, sha1: str, phash: str) -> bool:
        async with self._lock:
            data = self._load()
            if sha1 in data:
                return False
            new_data = dict(data)
            new_data[sha1] = phash
            self._save(new_data)
            return True

    def size(self) -> int:
        return len(self._load())

    def all_entries(self) -> dict[str, str]:
        return dict(self._load())