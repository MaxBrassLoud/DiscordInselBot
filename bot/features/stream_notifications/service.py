"""
bot/features/stream_notifications/service.py
=============================================
Gemeinsame Geschäftslogik:
  • activate_account()          – Streamer-Rolle bei Annahme vergeben
  • deactivate_account()        – Rollen bei Entfernung entziehen
  • handle_live_transition()    – Live-Rolle + Alert senden
  • handle_offline_transition() – Live-Rolle wieder entfernen
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import discord

from bot.utils.logger import get_logger

logger = get_logger("stream_notifications.service")


def _guild_and_member(bot: discord.Client, guild_id, user_id):
    """Sicher (guild, member) holen – beide dürfen None sein."""
    try:
        guild = bot.get_guild(int(guild_id))
    except (TypeError, ValueError):
        return None, None
    if not guild or not user_id:
        return guild, None
    try:
        return guild, guild.get_member(int(user_id))
    except (TypeError, ValueError):
        return guild, None


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════════

async def activate_account(bot: discord.Client, account: dict) -> bool:
    """Vergibt bei Annahme die Streamer-Rolle (nur wenn alert_enabled)."""
    if not account.get("alert_enabled"):
        return True
    if not account.get("discord_user_id"):
        return True

    from .manager import StreamAccountManager
    guild, member = _guild_and_member(
        bot, account["guild_id"], account["discord_user_id"]
    )
    if not guild:
        logger.warning(f"[service] Guild {account['guild_id']} nicht gefunden.")
        return False
    if not member:
        logger.info(
            f"[service] User {account['discord_user_id']} nicht (mehr) auf Guild {guild.id}."
        )
        return False

    config = StreamAccountManager.get_config(account["guild_id"])
    role_id = config.get("streamer_role_id")
    if not role_id:
        logger.warning(
            f"[service] streamer_role_id fehlt in Config von Guild {guild.id}."
        )
        return False

    role = guild.get_role(int(role_id))
    if not role:
        logger.warning(f"[service] Rolle {role_id} nicht gefunden auf Guild {guild.id}.")
        return False

    if role in member.roles:
        return True
    try:
        await member.add_roles(role, reason="Stream-Benachrichtigung angenommen")
        logger.info(f"[service] {member} → Streamer-Rolle {role.name}")
        return True
    except discord.Forbidden:
        logger.warning(
            f"[service] Keine Berechtigung, {member} die Rolle {role.name} zu geben."
        )
        return False
    except Exception as e:
        logger.error(f"[service] add_roles: {e}")
        return False


async def deactivate_account(bot: discord.Client, account: dict) -> bool:
    """Entzieht Streamer- und Live-Rolle."""
    if not account.get("discord_user_id"):
        return True

    from .manager import StreamAccountManager
    guild, member = _guild_and_member(
        bot, account["guild_id"], account["discord_user_id"]
    )
    if not guild or not member:
        return True

    config = StreamAccountManager.get_config(account["guild_id"])
    roles_to_remove: list[discord.Role] = []
    for key in ("streamer_role_id", "live_role_id"):
        rid = config.get(key)
        if not rid:
            continue
        role = guild.get_role(int(rid))
        if role and role in member.roles:
            roles_to_remove.append(role)

    for role in roles_to_remove:
        try:
            await member.remove_roles(role, reason="Stream-Benachrichtigung entfernt")
            logger.info(f"[service] {member} ← Rolle {role.name} entfernt")
        except discord.Forbidden:
            logger.warning(
                f"[service] Keine Berechtigung, {member} Rolle {role.name} zu entfernen."
            )
        except Exception as e:
            logger.error(f"[service] remove_roles {role.name}: {e}")

    return True


# ══════════════════════════════════════════════════════════════════════════════
# LIVE-TRANSITIONS
# ══════════════════════════════════════════════════════════════════════════════

async def handle_live_transition(bot: discord.Client, account: dict, live_status) -> None:
    """Streamer geht online → Live-Rolle + ggf. Channel-Alert."""
    from .manager import StreamAccountManager

    guild, member = _guild_and_member(
        bot, account["guild_id"], account.get("discord_user_id")
    )
    if not guild:
        return

    config = StreamAccountManager.get_config(account["guild_id"])

    # 1. Live-Rolle (Roter Punkt) vergeben
    live_role_id = config.get("live_role_id")
    if member and live_role_id:
        live_role = guild.get_role(int(live_role_id))
        if live_role and live_role not in member.roles:
            try:
                await member.add_roles(live_role, reason="Streamer ist live")
                logger.info(f"[service] {member} → Live-Rolle {live_role.name}")
            except Exception as e:
                logger.warning(f"[service] add live role: {e}")

    # 2. Channel-Alert senden (nur bei alert_enabled)
    if account.get("alert_enabled"):
        channel_id = config.get("channel_id")
        if channel_id:
            channel = guild.get_channel(int(channel_id))
            if channel:
                await _send_live_alert(channel, account, live_status, config, member)

    # 3. Zustand speichern
    StreamAccountManager.update_account(account["id"], {
        "is_live": True,
        "live_since": datetime.now(timezone.utc).isoformat(),
    })


async def handle_offline_transition(bot: discord.Client, account: dict) -> None:
    """Streamer geht offline → Live-Rolle entfernen."""
    from .manager import StreamAccountManager

    guild, member = _guild_and_member(
        bot, account["guild_id"], account.get("discord_user_id")
    )
    if guild and member:
        config = StreamAccountManager.get_config(account["guild_id"])
        live_role_id = config.get("live_role_id")
        if live_role_id:
            live_role = guild.get_role(int(live_role_id))
            if live_role and live_role in member.roles:
                try:
                    await member.remove_roles(live_role, reason="Stream beendet")
                    logger.info(f"[service] {member} ← Live-Rolle entfernt")
                except Exception as e:
                    logger.warning(f"[service] remove live role: {e}")

    StreamAccountManager.update_account(account["id"], {
        "is_live": False,
        "live_since": None,
    })


# ══════════════════════════════════════════════════════════════════════════════
# ALERT-EMBED
# ══════════════════════════════════════════════════════════════════════════════

async def _send_live_alert(
    channel: discord.TextChannel,
    account: dict,
    live_status,
    config: dict,
    member: Optional[discord.Member],
):
    platform = "Twitch" if account["platform"] == "twitch" else "YouTube"
    title = live_status.title or f"{account['account_name'] or account['account_id']} ist live!"

    embed = discord.Embed(
        title=f"🔴 {title}",
        url=live_status.url,
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="📺 Plattform", value=platform, inline=True)
    embed.add_field(
        name="🎬 Account",
        value=f"`{account['account_id']}`",
        inline=True,
    )
    if live_status.viewer_count:
        embed.add_field(
            name="👥 Zuschauer",
            value=str(live_status.viewer_count),
            inline=True,
        )
    if live_status.thumbnail:
        embed.set_image(url=live_status.thumbnail)
    if member:
        embed.set_footer(
            text=f"Gestartet · {member.display_name}",
            icon_url=member.display_avatar.url if member.display_avatar else None,
        )
    else:
        embed.set_footer(text="Gestartet")

    # Nur Streamer-Rolle pingen, keine @everyone/@here
    content_parts = []
    role_id = config.get("streamer_role_id")
    if role_id:
        content_parts.append(f"<@&{role_id}>")
    content = " ".join(content_parts) if content_parts else None

    try:
        await channel.send(
            content=content,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, here=False, roles=True, users=False
            ),
        )
    except Exception as e:
        logger.error(f"[service] Live-Alert senden fehlgeschlagen: {e}")