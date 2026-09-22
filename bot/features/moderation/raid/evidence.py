from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import discord

from bot.utils.logger import get_logger

from .config import CONFIG
from .forbidden_store import ForbiddenImageStore
from .hashing import compute_image_hashes

logger = get_logger("raid.evidence")


async def send_user_embed(
    user: discord.abc.Messageable,
    title: str,
    description: str,
    color: discord.Color,
    fields: Optional[list[tuple[str, str, bool]]] = None,
) -> None:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="Raid-Protection System")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        logger.debug(f"[raid] DM to {user} not possible")
    except Exception as e:
        logger.warning(f"[raid] DM failed: {e}")


def build_report_embed(
    guild: discord.Guild,
    user: discord.Member,
    until: datetime,
    is_new: bool,
    messages: list[discord.Message],
    trigger_reason: str,
    trigger_count: int,
    sum_mentions: int,
    case_id: str,
) -> discord.Embed:
    joined_text = (
        discord.utils.format_dt(user.joined_at, "F") if user.joined_at else "Unbekannt"
    )
    attachment_count = sum(len(msg.attachments) for msg in messages)
    channel_count = len({msg.channel.id for msg in messages})

    trigger_text = {
        "mentions": f"**{trigger_count}** Erwaehnungen",
        "text": f"**{trigger_count}** gleiche Textnachrichten",
        "images": f"**{trigger_count}** aehnliche Bilder",
        "forbidden_image": f"**{trigger_count}** verbotenes Bild",
    }.get(trigger_reason, "unbekannt")

    embed = discord.Embed(
        title="Raid-Protection | Moderationsfall",
        description=(
            "Automatischer Schutz hat eine verdaechtige Aktivitaet erkannt, den User temporaer gestoppt "
            "und die Beweise unten im Log gesichert."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="User", value=f"{user.mention}\n`{user}`\n`{user.id}`", inline=True
    )
    embed.add_field(
        name="Timeout",
        value=f"Bis {discord.utils.format_dt(until, 'F')}\n{discord.utils.format_dt(until, 'R')}",
        inline=True,
    )
    embed.add_field(
        name="Risikoprofil",
        value=f"Neuer User (< {CONFIG['new_member_days']} Tage)" if is_new else "Bestehender User",
        inline=True,
    )
    embed.add_field(
        name="Ausloeser",
        value=(
            f"{trigger_text}\n"
            f"in **{len(messages)}** Nachrichten\n"
            f"**{attachment_count}** Anhaenge aus **{channel_count}** Kanal/Kanaelen"
        ),
        inline=False,
    )
    embed.add_field(
        name="Beweise",
        value=(
            "Alle verdaechtigen Nachrichten werden unter diesem Report einzeln protokolliert. "
            "Bilder, Videos und Dateien werden neu hochgeladen, sofern Discord sie noch bereitstellt."
        ),
        inline=False,
    )
    embed.add_field(
        name="Fall-Daten", value=f"Guild-ID: `{guild.id}`\nUser-ID: `{user.id}`", inline=False
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_footer(text=f"Fall-ID: {case_id} | Bis: {until.isoformat()}")
    return embed


def build_evidence_embeds(
    evidence_items: list[dict[str, Any]],
    failed_files: list[str],
) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    total = len(evidence_items)
    current = discord.Embed(
        title=f"Evidence | {total} verdaechtige Nachrichten",
        description=(
            "Gebuendelte Sicherung der geloeschten Raid-Nachrichten. "
            "Anhaenge sind, soweit moeglich, an diese Log-Nachricht angehaengt."
        ),
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )

    for item in evidence_items:
        msg: discord.Message = item["message"]
        attachment_text = "\n".join(item["attachments"]) if item["attachments"] else "Keine Anhaenge"
        flag = item.get("flag", "")
        value = (
            f"**Kanal:** {msg.channel.mention} | **Zeit:** {discord.utils.format_dt(msg.created_at, 'F')}\n"
            f"**Erwaehnungen:** {item['mention_count']} | **Nachricht-ID:** `{msg.id}`\n"
            f"**Text:** {item['content']}\n"
            f"**Anhaenge:** {attachment_text}"
        )
        if flag:
            value = f"{flag}\n{value}"
        if len(value) > 1024:
            value = value[:1021] + "..."

        if len(current.fields) >= 24:
            current.set_footer(text="Originalnachrichten werden nach Sicherung geloescht")
            embeds.append(current)
            current = discord.Embed(
                title=f"Evidence | Fortsetzung ({total} Nachrichten)",
                color=discord.Color.blurple(),
                timestamp=datetime.now(timezone.utc),
            )

        current.add_field(name=f"Nachricht {item['index']}/{total}", value=value, inline=False)

    if failed_files:
        failed_value = "\n".join(failed_files)
        if len(failed_value) > 1024:
            failed_value = failed_value[:1021] + "..."
        if len(current.fields) >= 24:
            current.set_footer(text="Originalnachrichten werden nach Sicherung geloescht")
            embeds.append(current)
            current = discord.Embed(
                title="Evidence | Upload-Hinweise",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
        current.add_field(name="Nicht neu hochgeladen", value=failed_value, inline=False)

    current.set_footer(text="Originalnachrichten werden nach Sicherung geloescht")
    embeds.append(current)
    return embeds


async def send_evidence_logs(
    log_channel: discord.TextChannel,
    messages: list[discord.Message],
    forbidden_store: ForbiddenImageStore,
) -> None:
    if not messages:
        embed = discord.Embed(
            title="Evidence | Keine Nachrichten gefunden",
            description="Im Watchlist-Eintrag waren keine Nachrichten mehr vorhanden.",
            color=discord.Color.dark_gray(),
            timestamp=datetime.now(timezone.utc),
        )
        await log_channel.send(embed=embed)
        return

    evidence_items: list[dict[str, Any]] = []
    files: list[discord.File] = []
    failed_files: list[str] = []

    for idx, msg in enumerate(messages, 1):
        mention_count = len(msg.mentions) + len(msg.role_mentions)
        content = msg.content.strip() if msg.content else "*Keine Textnachricht*"
        if len(content) > 650:
            content = content[:647] + "..."

        attachment_lines = []
        flag = ""
        if msg.attachments:
            for attachment in msg.attachments:
                size_mb = attachment.size / 1024 / 1024
                annotation = ""
                # Verbotene Bilder pruefen
                if attachment.content_type and attachment.content_type.startswith("image/"):
                    try:
                        data = await attachment.read()
                        sha1, phash = compute_image_hashes(data)
                        if forbidden_store.exact_match(sha1):
                            annotation = "  (X) **EXAKTES VERBOTENES BILD**"
                        else:
                            match = forbidden_store.find_similar(
                                phash, CONFIG["image_similarity_threshold"]
                            )
                            if match:
                                annotation = (
                                    f"  (X) **aehnlich zu verbotenem Bild** "
                                    f"(`{match['sha1'][:12]}...`)"
                                )
                    except Exception as e:
                        logger.debug(f"[raid] forbidden check failed: {e}")

                attachment_lines.append(
                    f"`{attachment.filename}` ({size_mb:.2f} MB) - {attachment.url}{annotation}"
                )
                if annotation:
                    flag = "**Enthaelt ein oder mehrere verbotene Bilder**"

                try:
                    files.append(await attachment.to_file(use_cached=True))
                except Exception as e:
                    try:
                        files.append(await attachment.to_file())
                    except Exception as retry_error:
                        logger.warning(
                            f"[raid] attachment backup failed ({attachment.filename}): "
                            f"{e}; retry: {retry_error}"
                        )
                        failed_files.append(f"{attachment.filename}: {attachment.url}")

        evidence_items.append(
            {
                "index": idx,
                "message": msg,
                "mention_count": mention_count,
                "content": content,
                "attachments": attachment_lines,
                "flag": flag,
            }
        )

    embeds = build_evidence_embeds(evidence_items, failed_files)
    max_messages = max((len(embeds) + 9) // 10, (len(files) + 9) // 10, 1)

    for batch in range(max_messages):
        embed_chunk = embeds[batch * 10:(batch + 1) * 10]
        file_chunk = files[batch * 10:(batch + 1) * 10]
        content = None
        if max_messages > 1:
            content = f"Evidence-Paket {batch + 1}/{max_messages}"

        try:
            if embed_chunk and file_chunk:
                await log_channel.send(
                    content=content,
                    embeds=embed_chunk,
                    files=file_chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif embed_chunk:
                await log_channel.send(
                    content=content,
                    embeds=embed_chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif file_chunk:
                await log_channel.send(
                    content=content or "Weitere gesicherte Anhaenge",
                    files=file_chunk,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as e:
            logger.warning(f"[raid] bundled evidence upload failed: {e}")
            if embed_chunk:
                try:
                    await log_channel.send(
                        content=content,
                        embeds=embed_chunk,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except Exception as embed_error:
                    logger.warning(f"[raid] bundled evidence embed fallback failed: {embed_error}")

        await asyncio.sleep(0.25)