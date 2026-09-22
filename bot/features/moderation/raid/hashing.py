from __future__ import annotations

import hashlib
import io
import re
from typing import Optional

import discord
import imagehash
from PIL import Image

from bot.utils.logger import get_logger

logger = get_logger("raid.hashing")


def normalize_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compute_image_hashes(data: bytes) -> tuple[str, str]:
    """Return (sha1_hex, phash_hex)."""
    sha1 = hashlib.sha1(data).hexdigest()
    img = Image.open(io.BytesIO(data))
    if img.mode != "RGB":
        img = img.convert("RGB")
    phash = imagehash.average_hash(img, hash_size=16)
    return sha1, str(phash)


async def hash_attachment(
    attachment: discord.Attachment,
) -> Optional[tuple[str, str]]:
    if not attachment.content_type or not attachment.content_type.startswith("image/"):
        return None
    try:
        data = await attachment.read()
        return compute_image_hashes(data)
    except Exception as e:
        logger.debug(f"[raid] hash failed for {attachment.filename}: {e}")
        return None


def is_similar(phash1: str, phash2: str, threshold: int = 5) -> bool:
    if not phash1 or not phash2:
        return False
    try:
        h1 = imagehash.hex_to_hash(phash1)
        h2 = imagehash.hex_to_hash(phash2)
        return (h1 - h2) <= threshold
    except Exception:
        return False