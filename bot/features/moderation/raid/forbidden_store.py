from __future__ import annotations

import asyncio
import json
import os
from typing import Optional

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

from .hashing import is_similar

logger = get_logger("raid.forbidden")


class ForbiddenImageStore:
    """
    Speichert verbotene Bilder in der Datenbank (Tabelle raid_forbidden_images).

    - Liest beim ersten Zugriff alle Einträge in einen In-Memory-Cache.
    - Neue Einträge gehen sofort in die DB und in den Cache.
    - Eine vorhandene JSON-Datei wird beim ersten Zugriff nahtlos in die
      DB migriert und anschließend gelöscht.
    """

    def __init__(self, legacy_path: str):
        self.legacy_path = legacy_path
        self._lock = asyncio.Lock()
        self._cache: Optional[dict[str, str]] = None
        self._migration_done = False

    # ── Migration ────────────────────────────────────────────────────────────

    def _migrate_legacy_file(self) -> None:
        if self._migration_done:
            return
        self._migration_done = True

        if not os.path.exists(self.legacy_path):
            return

        try:
            with open(self.legacy_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            if not isinstance(data, dict):
                data = {}
        except Exception as exc:
            logger.warning(f"[forbidden] Legacy-Datei laden: {exc}")
            return

        if not data:
            try:
                os.unlink(self.legacy_path)
            except Exception:
                pass
            return

        try:
            sb = get_supabase()
            rows = [
                {"sha1": sha1, "phash": phash}
                for sha1, phash in data.items()
            ]
            sb.table("raid_forbidden_images").upsert(
                rows, on_conflict="sha1"
            ).execute()
            os.unlink(self.legacy_path)
            logger.info(
                f"[forbidden] {len(data)} Einträge aus Legacy-Datei "
                f"in DB migriert und Datei entfernt."
            )
        except Exception as exc:
            logger.error(f"[forbidden] Migration fehlgeschlagen: {exc}")

    # ── Cache ────────────────────────────────────────────────────────────────

    def _load(self) -> dict[str, str]:
        self._migrate_legacy_file()
        if self._cache is not None:
            return self._cache
        try:
            sb = get_supabase()
            r = (
                sb.table("raid_forbidden_images")
                .select("sha1, phash")
                .execute()
            )
            self._cache = {
                row["sha1"]: row["phash"]
                for row in (r.data or [])
            }
        except Exception as exc:
            logger.warning(f"[forbidden] Laden fehlgeschlagen: {exc}")
            self._cache = {}
        return self._cache

    # ── Öffentliche API ──────────────────────────────────────────────────────

    def exact_match(self, sha1: str) -> bool:
        return sha1 in self._load()

    def find_similar(
        self, phash: str, threshold: int = 5
    ) -> Optional[dict]:
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

            try:
                sb = get_supabase()
                sb.table("raid_forbidden_images").insert(
                    {"sha1": sha1, "phash": phash}
                ).execute()
            except Exception as exc:
                text = str(exc)
                if (
                    "23505" in text
                    or "duplicate" in text.lower()
                ):
                    return False
                logger.error(f"[forbidden] add fehlgeschlagen: {exc}")
                raise

            if self._cache is not None:
                self._cache[sha1] = phash
            else:
                self._cache = {sha1: phash}
            return True

    def size(self) -> int:
        return len(self._load())

    def all_entries(self) -> dict[str, str]:
        return dict(self._load())