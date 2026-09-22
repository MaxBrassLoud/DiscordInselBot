from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import discord

from .case import RaidCase


async def resolve_case(
    interaction: discord.Interaction, fallback: Optional[RaidCase]
) -> Optional[RaidCase]:
    if fallback and fallback.case_id:
        return fallback
    if not interaction.message or not interaction.message.embeds:
        return fallback

    embed = interaction.message.embeds[0]
    guild_id = interaction.guild_id
    user_id = None
    until = None
    case_id = None

    footer = embed.footer.text or ""

    match = re.search(r"Fall-ID:\s*(\S+)", footer)
    if match:
        case_id = match.group(1)
        parts = case_id.split("-")
        if len(parts) >= 2:
            try:
                guild_id = int(parts[0])
                user_id = int(parts[1])
            except ValueError:
                pass

    if user_id is None:
        combined = f"{embed.description or ''}\n" + "\n".join(
            field.value for field in embed.fields
        )
        match = re.search(r"User-ID:\s*`?(\d+)`?", combined)
        if match:
            user_id = int(match.group(1))

    until_match = re.search(r"Bis:\s*([^\s|]+)", footer)
    if until_match:
        try:
            until = datetime.fromisoformat(until_match.group(1).replace("Z", "+00:00"))
        except ValueError:
            until = None

    if guild_id and user_id:
        return RaidCase(int(guild_id), int(user_id), until, case_id)
    return fallback