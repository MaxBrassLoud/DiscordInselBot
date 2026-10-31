"""
bot/features/stream_notifications/detector.py
==============================================
Live-Status-Prüfung für Twitch- und YouTube-Kanäle via yt-dlp.

Warum yt-dlp?
  • Benötigt keine Twitch-Client-ID oder YouTube-API-Key.
  • Zuverlässig bei beiden Plattformen.
  • Deutlich langsamer als offizielle APIs — deshalb begrenzen wir die
    Concurrency im Cog (Semaphore) und überwachen die Ergebnisse robust.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

import yt_dlp

from bot.utils.logger import get_logger

logger = get_logger("stream_notifications.detector")


@dataclass
class LiveStatus:
    is_live: bool = False
    title: Optional[str] = None
    thumbnail: Optional[str] = None
    viewer_count: Optional[int] = None
    url: Optional[str] = None
    error: Optional[str] = None


class StreamDetector:
    """Statischer Detector – keine Instanz nötig."""

    TIMEOUT_SECONDS = 30
    INVALID_TWITCH_HANDLES = {
        "videos", "directory", "settings", "subscriptions",
        "inventory", "wallet", "drops", "downloads",
    }

    @staticmethod
    def build_url(platform: str, account_id: str) -> Optional[str]:
        p = (platform or "").lower()
        if p == "twitch":
            if account_id.lower() in StreamDetector.INVALID_TWITCH_HANDLES:
                return None
            return f"https://twitch.tv/{account_id}"
        if p == "youtube":
            # YouTube-Kanal-IDs beginnen mit "UC" und sind ~24 Zeichen lang
            if account_id.startswith("UC") and len(account_id) >= 20:
                return f"https://youtube.com/channel/{account_id}"
            return f"https://youtube.com/@{account_id}"
        return None

    @staticmethod
    def _fetch_info_blocking(url: str) -> dict | None:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": False,
            "noplaylist": True,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:
            logger.debug(f"[detector] yt-dlp Fehler für {url}: {e}")
            return None

    @staticmethod
    async def check(platform: str, account_id: str) -> LiveStatus:
        url = StreamDetector.build_url(platform, account_id)
        if not url:
            return LiveStatus(error=f"Ungültiger Account/Plattform: {platform}/{account_id}")

        loop = asyncio.get_event_loop()
        try:
            info = await asyncio.wait_for(
                loop.run_in_executor(
                    None, StreamDetector._fetch_info_blocking, url
                ),
                timeout=StreamDetector.TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.debug(f"[detector] Timeout: {url}")
            return LiveStatus(error="Timeout bei Stream-Abfrage")
        except Exception as e:
            logger.debug(f"[detector] Fehler bei {url}: {e}")
            return LiveStatus(error=str(e))

        if not info:
            return LiveStatus(error="Keine Stream-Info erhalten")

        live_status = (info.get("live_status") or "").lower()
        is_live = bool(info.get("is_live")) or live_status in (
            "is_live", "is_upcoming"
        )
        return LiveStatus(
            is_live=is_live,
            title=info.get("title"),
            thumbnail=info.get("thumbnail"),
            viewer_count=info.get("concurrent_view_count"),
            url=url,
        )