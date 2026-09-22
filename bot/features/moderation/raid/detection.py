from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import discord

from .config import CONFIG
from .hashing import hash_attachment, is_similar, normalize_text


async def is_suspicious(
    message: discord.Message,
    watchlist: dict[int, dict[str, Any]],
) -> tuple[bool, dict]:
    """
    Prueft Mentions, Text-Duplikate und Bild-Duplikate.
    Gibt (True/False, Details) zurueck.
    """
    user_id = message.author.id
    now = datetime.now(timezone.utc)
    time_window = CONFIG.get("duplicate_time_window", CONFIG["message_time_window"])
    cutoff = now - timedelta(seconds=time_window)

    entry = watchlist.get(user_id)
    if entry is None:
        entry = {"guild_id": message.guild.id, "entries": [], "first_seen": now}
        watchlist[user_id] = entry

    # Alte Eintraege verwerfen
    entry["entries"] = [
        e for e in entry["entries"] if e["msg"].created_at >= cutoff
    ]

    # Text-Hash
    text_hash = None
    if message.content:
        normalized = normalize_text(message.content)
        if normalized:
            text_hash = hash(normalized)

    # Bild-Hashes fuer alle Attachments
    images: list[tuple[str, str, str, str]] = []  # (url, filename, sha1, phash)
    for att in message.attachments:
        result = await hash_attachment(att)
        if result:
            sha1, phash = result
            images.append((att.url, att.filename, sha1, phash))

    entry["entries"].append(
        {"msg": message, "text_hash": text_hash, "images": images}
    )

    # ---- Zaehlungen ----
    sum_mentions = sum(
        len(e["msg"].mentions) + len(e["msg"].role_mentions) for e in entry["entries"]
    )

    text_counts: dict[int, int] = {}
    for e in entry["entries"]:
        if e["text_hash"] is not None:
            text_counts[e["text_hash"]] = text_counts.get(e["text_hash"], 0) + 1
    max_text_count = max(text_counts.values(), default=0)

    all_phashes: list[str] = []
    for e in entry["entries"]:
        for (_url, _fn, _sha1, phash) in e["images"]:
            all_phashes.append(phash)

    threshold = CONFIG["image_similarity_threshold"]
    max_similar_images = 0
    for h in all_phashes:
        cnt = sum(1 for h2 in all_phashes if is_similar(h, h2, threshold))
        if cnt > max_similar_images:
            max_similar_images = cnt

    # ---- Schwellwertpruefung ----
    triggered = False
    trigger_reason = None
    trigger_count = 0
    if sum_mentions >= CONFIG["total_mention_threshold"]:
        triggered, trigger_reason, trigger_count = True, "mentions", sum_mentions
    elif max_similar_images >= CONFIG["image_duplicate_threshold"]:
        triggered, trigger_reason, trigger_count = True, "images", max_similar_images
    elif max_text_count >= CONFIG["text_duplicate_threshold"]:
        triggered, trigger_reason, trigger_count = True, "text", max_text_count

    if not triggered:
        return False, {}

    details = {
        "reason": trigger_reason,
        "count": trigger_count,
        "sum_mentions": sum_mentions,
        "max_text_count": max_text_count,
        "max_similar_images": max_similar_images,
        "entries": entry["entries"].copy(),
    }
    return True, details