"""
bot/features/moderation/link_protection.py
===========================================
Link Protection – Blockiert unerlaubte Links in Nachrichten.

BEREITSTELLUNG:
  1. Führe das SQL-Schema in Supabase aus (siehe unten).
  2. Lade das Cog in server.py: "bot.features.moderation.link_protection"
  3. Nutze /linkprotection setup, um das System zu konfigurieren.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Optional, Set, List, Dict, Any
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("link_protection")

# ── Reguläre Ausdrücke ──────────────────────────────────────────────────────
URL_PATTERN = re.compile(
    r'(https?://[^\s]+|www\.[^\s]+)',
    re.IGNORECASE
)

YOUTUBE_CHANNEL_PATTERN = re.compile(
    r'(?:youtube\.com/(?:channel/|c/|@)|youtu\.be/)([a-zA-Z0-9_-]+)',
    re.IGNORECASE
)

TWITCH_CHANNEL_PATTERN = re.compile(
    r'(?:twitch\.tv/|clips\.twitch\.tv/)([a-zA-Z0-9_]+)',
    re.IGNORECASE
)


# ── Datenbank-Helfer ────────────────────────────────────────────────────────

def _get_link_config(server_id: str) -> dict:
    """Holt die Link-Protection-Konfiguration für einen Server."""
    try:
        sb = get_supabase()
        r = sb.table("link_protection_config") \
            .select("*") \
            .eq("server_id", server_id) \
            .execute()
        if r.data:
            return r.data[0]
    except Exception as e:
        logger.error(f"[link_protection] _get_link_config: {e}")
    return {}


def _set_link_config(server_id: str, data: dict):
    """Setzt die Link-Protection-Konfiguration für einen Server."""
    sb = get_supabase()
    existing = sb.table("link_protection_config") \
        .select("server_id") \
        .eq("server_id", server_id) \
        .execute()
    if existing.data:
        sb.table("link_protection_config").update(data) \
            .eq("server_id", server_id).execute()
    else:
        sb.table("link_protection_config").insert(data).execute()


def _get_allowed_links(server_id: str) -> List[dict]:
    """Holt alle erlaubten Links/Domains für einen Server."""
    try:
        sb = get_supabase()
        r = sb.table("link_protection_allowed") \
            .select("*") \
            .eq("server_id", server_id) \
            .execute()
        return r.data or []
    except Exception as e:
        logger.error(f"[link_protection] _get_allowed_links: {e}")
        return []


def _add_allowed_link(server_id: str, url: str, created_by: str, channel_id: Optional[str] = None, user_id: Optional[str] = None):
    """Fügt eine erlaubte Domain/URL hinzu."""
    try:
        sb = get_supabase()
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        if not domain:
            return

        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else domain
        if parsed.path and parsed.path != "/":
            base_url += parsed.path.rstrip("/")

        sb.table("link_protection_allowed").insert({
            "server_id": server_id,
            "url": base_url,
            "channel_id": channel_id,
            "user_id": user_id,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"[link_protection] _add_allowed_link: {e}")


def _delete_allowed_link(server_id: str, url: str):
    try:
        sb = get_supabase()
        sb.table("link_protection_allowed") \
            .delete() \
            .eq("server_id", server_id) \
            .eq("url", url) \
            .execute()
    except Exception as e:
        logger.error(f"[link_protection] _delete_allowed_link: {e}")


def _get_whitelisted_channels(server_id: str, platform: str) -> Set[str]:
    """Holt alle whitelisteten YouTube-/Twitch-Kanäle."""
    try:
        sb = get_supabase()
        r = sb.table("link_protection_platform_whitelist") \
            .select("channel_id") \
            .eq("server_id", server_id) \
            .eq("platform", platform) \
            .execute()
        return {row["channel_id"] for row in (r.data or [])}
    except Exception as e:
        logger.error(f"[link_protection] _get_whitelisted_channels: {e}")
        return set()


def _add_whitelisted_channel(server_id: str, platform: str, channel_id: str):
    try:
        sb = get_supabase()
        sb.table("link_protection_platform_whitelist").insert({
            "server_id": server_id,
            "platform": platform,
            "channel_id": channel_id,
        }).execute()
    except Exception as e:
        logger.error(f"[link_protection] _add_whitelisted_channel: {e}")


def _remove_whitelisted_channel(server_id: str, platform: str, channel_id: str):
    try:
        sb = get_supabase()
        sb.table("link_protection_platform_whitelist") \
            .delete() \
            .eq("server_id", server_id) \
            .eq("platform", platform) \
            .eq("channel_id", channel_id) \
            .execute()
    except Exception as e:
        logger.error(f"[link_protection] _remove_whitelisted_channel: {e}")


def _get_user_allowed_until(server_id: str, user_id: str) -> Optional[datetime]:
    """Holt den Zeitpunkt bis zu dem ein User Links senden darf."""
    try:
        sb = get_supabase()
        r = sb.table("link_protection_user_allow") \
            .select("allowed_until") \
            .eq("server_id", server_id) \
            .eq("user_id", user_id) \
            .execute()
        if r.data and r.data[0].get("allowed_until"):
            return datetime.fromisoformat(r.data[0]["allowed_until"])
    except Exception as e:
        logger.error(f"[link_protection] _get_user_allowed_until: {e}")
    return None


def _set_user_allowed_until(server_id: str, user_id: str, allowed_until: Optional[datetime]):
    try:
        sb = get_supabase()
        existing = sb.table("link_protection_user_allow") \
            .select("server_id") \
            .eq("server_id", server_id) \
            .eq("user_id", user_id) \
            .execute()
        data = {
            "server_id": server_id,
            "user_id": user_id,
            "allowed_until": allowed_until.isoformat() if allowed_until else None,
        }
        if existing.data:
            sb.table("link_protection_user_allow").update(data) \
                .eq("server_id", server_id).eq("user_id", user_id).execute()
        else:
            sb.table("link_protection_user_allow").insert(data).execute()
    except Exception as e:
        logger.error(f"[link_protection] _set_user_allowed_until: {e}")


def _log_action(server_id: str, action: str, user_id: str, target_url: str, moderator_id: Optional[str] = None):
    """Loggt eine Link-Protection-Aktion."""
    try:
        sb = get_supabase()
        sb.table("link_protection_logs").insert({
            "server_id": server_id,
            "action": action,
            "user_id": user_id,
            "target_url": target_url,
            "moderator_id": moderator_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        logger.error(f"[link_protection] _log_action: {e}")


# ── Helper-Funktionen ──────────────────────────────────────────────────────

def _is_allowed_url(url: str, allowed_links: List[dict]) -> bool:
    """Prüft, ob eine URL auf der Whitelist steht."""
    parsed = urlparse(url)
    domain = parsed.netloc or parsed.path
    for entry in allowed_links:
        entry_url = entry["url"]
        entry_parsed = urlparse(entry_url)
        entry_domain = entry_parsed.netloc or entry_parsed.path
        if parsed.netloc == entry_domain or parsed.path.startswith(entry_domain):
            return True
    return False


def _extract_youtube_channel(url: str) -> Optional[str]:
    """Extrahiert die YouTube-Kanal-ID oder den Handle aus einer URL."""
    match = YOUTUBE_CHANNEL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


def _extract_twitch_channel(url: str) -> Optional[str]:
    """Extrahiert den Twitch-Kanal aus einer URL."""
    match = TWITCH_CHANNEL_PATTERN.search(url)
    if match:
        return match.group(1)
    return None


# ── Views für den Link-Approval-Workflow ───────────────────────────────────

class LinkApprovalView(discord.ui.View):
    """View für den Link-Approval-Workflow im Moderation-Channel."""

    def __init__(self, server_id: str, user_id: str, url: str, original_message: discord.Message):
        super().__init__(timeout=300)
        self.server_id = server_id
        self.user_id = user_id
        self.url = url
        self.original_message = original_message

    @discord.ui.button(label="⏳ Für User temporär freischalten", style=discord.ButtonStyle.primary)
    async def temp_allow_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        modal = TempAllowModal(self.server_id, self.user_id, self.url, interaction.message, self)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🔓 User dauerhaft freischalten", style=discord.ButtonStyle.success)
    async def permanent_allow_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await self._handle_approval(interaction, allowed_until=None)

    @discord.ui.button(label="🌐 Link für jeden freischalten", style=discord.ButtonStyle.secondary)
    async def allow_for_all(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        _add_allowed_link(self.server_id, self.url, str(interaction.user.id))
        _log_action(self.server_id, "allow_global", self.user_id, self.url, str(interaction.user.id))
        await interaction.response.send_message(f"✅ Link `{self.url}` wurde für alle freigegeben.", ephemeral=True)
        await self._finish(interaction)

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        _log_action(self.server_id, "deny", self.user_id, self.url, str(interaction.user.id))
        await interaction.response.send_message(f"❌ Link `{self.url}` wurde abgelehnt.", ephemeral=True)
        await self._finish(interaction)

    async def _handle_approval(self, interaction: discord.Interaction, allowed_until: Optional[datetime]):
        _set_user_allowed_until(self.server_id, self.user_id, allowed_until)
        _log_action(self.server_id, "allow_user", self.user_id, self.url, str(interaction.user.id))
        await interaction.response.send_message(
            f"✅ User <@{self.user_id}> kann jetzt Links senden."
            + (f" bis <t:{int(allowed_until.timestamp())}:F>" if allowed_until else " (unbegrenzt)"),
            ephemeral=True
        )
        await self._finish(interaction)

    async def _finish(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed(
            title="Link-Freigabe abgeschlossen",
            description=f"Link: {self.url}\nUser: <@{self.user_id}>",
            color=discord.Color.green()
        )
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(embed=embed, view=self)
        self.stop()


class TempAllowModal(discord.ui.Modal, title="Temporäre Freischaltung"):
    duration = discord.ui.TextInput(
        label="Dauer (in Minuten)",
        placeholder="z.B. 30",
        required=True,
        max_length=6
    )

    def __init__(self, server_id: str, user_id: str, url: str, message: discord.Message, parent_view: LinkApprovalView):
        super().__init__()
        self.server_id = server_id
        self.user_id = user_id
        self.url = url
        self.message = message
        self.parent_view = parent_view

    async def on_submit(self, interaction: discord.Interaction):
        try:
            minutes = int(self.duration.value)
        except ValueError:
            await interaction.response.send_message("❌ Bitte eine gültige Zahl eingeben.", ephemeral=True)
            return
        if minutes < 1 or minutes > 1440:
            await interaction.response.send_message("❌ Bitte zwischen 1 und 1440 Minuten.", ephemeral=True)
            return

        allowed_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        await self.parent_view._handle_approval(interaction, allowed_until)


# ── Cog ────────────────────────────────────────────────────────────────────

class LinkProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._allowed_cache: Dict[str, List[dict]] = {}
        self._config_cache: Dict[str, dict] = {}

    linkprotection = app_commands.Group(
        name="linkprotection",
        description="Link-Protection System",
    )

    # ── Setup ───────────────────────────────────────────────────────────────

    @linkprotection.command(name="setup", description="[Admin] Konfiguriere die Link-Protection")
    async def linkprotection_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        config = _get_link_config(str(interaction.guild_id))
        view = LinkProtectionSetupView(interaction.guild_id, config)
        await interaction.response.send_message(embed=view.build_embed(), view=view, ephemeral=True)

    # ── Whitelist verwalten ────────────────────────────────────────────────

    @linkprotection.command(name="whitelist_add", description="[Admin] Füge eine URL/Domain zur Whitelist hinzu")
    @app_commands.describe(url="Die URL oder Domain (z.B. https://example.com)")
    async def whitelist_add(self, interaction: discord.Interaction, url: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        _add_allowed_link(str(interaction.guild_id), url, str(interaction.user.id))
        self._allowed_cache.pop(str(interaction.guild_id), None)
        await interaction.response.send_message(f"✅ `{url}` wurde zur Whitelist hinzugefügt.", ephemeral=True)

    @linkprotection.command(name="whitelist_remove", description="[Admin] Entferne eine URL/Domain von der Whitelist")
    @app_commands.describe(url="Die URL oder Domain")
    async def whitelist_remove(self, interaction: discord.Interaction, url: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        _delete_allowed_link(str(interaction.guild_id), domain)
        self._allowed_cache.pop(str(interaction.guild_id), None)
        await interaction.response.send_message(f"✅ `{domain}` wurde von der Whitelist entfernt.", ephemeral=True)

    @linkprotection.command(name="whitelist_list", description="Zeige alle whitelisteten URLs/Domains")
    async def whitelist_list(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        allowed = _get_allowed_links(str(interaction.guild_id))
        if not allowed:
            await interaction.response.send_message("📭 Keine Einträge auf der Whitelist.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🌐 Whitelist (URLs/Domains)",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        for entry in allowed:
            embed.add_field(
                name=entry["url"],
                value=f"Freigegeben von <@{entry['created_by']}>",
                inline=False
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── YouTube/Twitch Channel Whitelist ──────────────────────────────────

    @linkprotection.command(name="channel_add", description="[Admin] Füge einen YouTube/Twitch-Kanal zur Whitelist hinzu")
    @app_commands.describe(
        platform="youtube oder twitch",
        channel_id="Kanal-ID oder Handle (z.B. @name oder UCxxxxx)"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Twitch", value="twitch"),
    ])
    async def channel_add(self, interaction: discord.Interaction, platform: str, channel_id: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        _add_whitelisted_channel(str(interaction.guild_id), platform, channel_id.strip())
        await interaction.response.send_message(
            f"✅ Kanal `{channel_id}` ({platform}) wurde zur Whitelist hinzugefügt.",
            ephemeral=True
        )

    @linkprotection.command(name="channel_remove", description="[Admin] Entferne einen YouTube/Twitch-Kanal von der Whitelist")
    @app_commands.describe(
        platform="youtube oder twitch",
        channel_id="Kanal-ID oder Handle"
    )
    @app_commands.choices(platform=[
        app_commands.Choice(name="YouTube", value="youtube"),
        app_commands.Choice(name="Twitch", value="twitch"),
    ])
    async def channel_remove(self, interaction: discord.Interaction, platform: str, channel_id: str):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        _remove_whitelisted_channel(str(interaction.guild_id), platform, channel_id.strip())
        await interaction.response.send_message(
            f"✅ Kanal `{channel_id}` ({platform}) wurde von der Whitelist entfernt.",
            ephemeral=True
        )

    @linkprotection.command(name="channel_list", description="[Admin] Zeige alle whitelisteten YouTube/Twitch-Kanäle")
    async def channel_list(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        yt = _get_whitelisted_channels(str(interaction.guild_id), "youtube")
        tw = _get_whitelisted_channels(str(interaction.guild_id), "twitch")

        embed = discord.Embed(
            title="📺 Whitelist (YouTube/Twitch-Kanäle)",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        if yt:
            embed.add_field(name="YouTube", value="\n".join(f"`{c}`" for c in yt) or "Keine", inline=True)
        if tw:
            embed.add_field(name="Twitch", value="\n".join(f"`{c}`" for c in tw) or "Keine", inline=True)
        if not yt and not tw:
            embed.description = "Keine Kanäle whitelistet."

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── User-Freigabe verwalten ────────────────────────────────────────────

    @linkprotection.command(name="user_allow", description="[Admin] Schalte einen User dauerhaft für Links frei")
    @app_commands.describe(user="Der User")
    async def user_allow(self, interaction: discord.Interaction, user: discord.Member):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        _set_user_allowed_until(str(interaction.guild_id), str(user.id), None)
        await interaction.response.send_message(
            f"✅ {user.mention} kann jetzt dauerhaft Links senden.",
            ephemeral=True
        )

    @linkprotection.command(name="user_remove", description="[Admin] Entferne die Link-Freigabe eines Users")
    @app_commands.describe(user="Der User")
    async def user_remove(self, interaction: discord.Interaction, user: discord.Member):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        _set_user_allowed_until(str(interaction.guild_id), str(user.id), None)
        await interaction.response.send_message(
            f"✅ {user.mention} kann jetzt keine Links mehr senden.",
            ephemeral=True
        )

    @linkprotection.command(name="user_list", description="Zeige alle temporär freigeschalteten User")
    async def user_list(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        try:
            sb = get_supabase()
            r = sb.table("link_protection_user_allow") \
                .select("*") \
                .eq("server_id", str(interaction.guild_id)) \
                .execute()
            users = r.data or []
            now = datetime.now(timezone.utc)
            embed = discord.Embed(
                title="👤 Freigeschaltete User",
                color=discord.Color.blurple(),
                timestamp=now
            )

            for entry in users:
                uid = entry["user_id"]
                until = entry.get("allowed_until")
                if until:
                    dt = datetime.fromisoformat(until)
                    if dt > now:
                        status = f"bis <t:{int(dt.timestamp())}:R>"
                    else:
                        status = "⚠️ Abgelaufen"
                else:
                    status = "♾️ Dauerhaft"

                embed.add_field(
                    name=f"<@{uid}>",
                    value=status,
                    inline=True
                )

            if not users:
                embed.description = "Keine User freigeschaltet."

            await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)

    # ── On-Message-Listener ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        config = _get_link_config(str(message.guild.id))
        if not config.get("enabled", False):
            return

        allowed_channels = config.get("allowed_channel_ids", "")
        if allowed_channels:
            allowed_ids = [cid.strip() for cid in allowed_channels.split(",") if cid.strip()]
            if str(message.channel.id) in allowed_ids:
                return

        urls = URL_PATTERN.findall(message.content)
        if not urls:
            return

        allowed_links = _get_allowed_links(str(message.guild.id))
        whitelisted_yt = _get_whitelisted_channels(str(message.guild.id), "youtube")
        whitelisted_tw = _get_whitelisted_channels(str(message.guild.id), "twitch")

        user_allowed_until = _get_user_allowed_until(str(message.guild.id), str(message.author.id))
        user_is_allowed = user_allowed_until is None or user_allowed_until > datetime.now(timezone.utc)

        if message.author.guild_permissions.administrator:
            return

        blocked = False
        for url in urls:
            if _is_allowed_url(url, allowed_links):
                continue

            yt_channel = _extract_youtube_channel(url)
            if yt_channel and yt_channel in whitelisted_yt:
                continue

            tw_channel = _extract_twitch_channel(url)
            if tw_channel and tw_channel in whitelisted_tw:
                continue

            if user_is_allowed:
                continue

            blocked = True
            break

        if blocked:
            try:
                await message.delete()
            except discord.Forbidden:
                logger.warning(f"[link_protection] Kann Nachricht nicht löschen in {message.channel.name}")
                return
            except discord.HTTPException as e:
                logger.error(f"[link_protection] Fehler beim Löschen: {e}")
                return

            embed = discord.Embed(
                title="🔒 Link blockiert",
                description=(
                    "Deine Nachricht enthielt einen Link, der nicht erlaubt ist.\n\n"
                    "**Mögliche Gründe:**\n"
                    "• Die Domain ist nicht auf der Whitelist\n"
                    "• Der YouTube/Twitch-Kanal ist nicht freigegeben\n"
                    "• Du hast keine Freigabe zum Senden von Links\n\n"
                    "Du kannst einen Moderator um Freigabe bitten."
                ),
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc)
            )
            view = LinkBlockedView(str(message.guild.id), str(message.author.id), urls[0], message)
            await message.author.send(embed=embed, view=view)

            log_channel_id = config.get("moderation_log_channel_id")
            if log_channel_id:
                log_channel = message.guild.get_channel(int(log_channel_id))
                if log_channel:
                    log_embed = discord.Embed(
                        title="🔒 Link blockiert",
                        description=f"User: {message.author.mention}\nLink: {urls[0]}",
                        color=discord.Color.red(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    log_embed.set_footer(text=f"User-ID: {message.author.id}")
                    await log_channel.send(embed=log_embed, view=LinkApprovalView(str(message.guild.id), str(message.author.id), urls[0], message))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.content != after.content:
            await self.on_message(after)


# ── View: Link blockiert (DM) ─────────────────────────────────────────────

class LinkBlockedView(discord.ui.View):
    def __init__(self, server_id: str, user_id: str, url: str, original_message: discord.Message):
        super().__init__(timeout=300)
        self.server_id = server_id
        self.user_id = user_id
        self.url = url
        self.original_message = original_message

    @discord.ui.button(label="🔗 Link freigeben lassen", style=discord.ButtonStyle.primary)
    async def request_approval(self, interaction: discord.Interaction, button: discord.ui.Button):
        config = _get_link_config(self.server_id)
        log_channel_id = config.get("moderation_log_channel_id")
        if not log_channel_id:
            await interaction.response.send_message("❌ Kein Moderation-Channel konfiguriert.", ephemeral=True)
            return

        log_channel = interaction.guild.get_channel(int(log_channel_id))
        if not log_channel:
            await interaction.response.send_message("❌ Moderation-Channel nicht gefunden.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔗 Link-Freigabe angefragt",
            description=f"User: {interaction.user.mention}\nLink: {self.url}",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        await log_channel.send(embed=embed, view=LinkApprovalView(self.server_id, self.user_id, self.url, self.original_message))
        await interaction.response.send_message("✅ Freigabe wurde angefragt.", ephemeral=True)


# ── Setup View ─────────────────────────────────────────────────────────────

class LinkProtectionSetupView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict):
        super().__init__(timeout=300)
        self.guild_id = str(guild_id)
        self.config = current_config or {}
        self._rebuild()

    def build_embed(self) -> discord.Embed:
        enabled = self.config.get("enabled", False)
        log_channel = self.config.get("moderation_log_channel_id")
        allowed_channels = self.config.get("allowed_channel_ids", "")

        embed = discord.Embed(
            title="⚙️ Link-Protection Setup",
            color=discord.Color.green() if enabled else discord.Color.light_gray,  # FIX: .grey() statt .gray()
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Status", value="✅ Aktiviert" if enabled else "❌ Deaktiviert", inline=True)
        embed.add_field(
            name="📋 Log-Kanal",
            value=f"<#{log_channel}>" if log_channel else "*Nicht gesetzt*",
            inline=True
        )
        embed.add_field(
            name="📢 Erlaubte Kanäle",
            value="\n".join(f"<#{cid.strip()}>" for cid in allowed_channels.split(",") if cid.strip()) or "*Keine*",
            inline=False
        )
        embed.set_footer(text="Wähle die Kanäle und klicke Speichern.")
        return embed

    def _rebuild(self):
        self.clear_items()

        log_sel = discord.ui.ChannelSelect(
            placeholder="📋 Moderation-Log-Kanal (Pflicht)",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
            row=0
        )
        log_sel.callback = self._on_log_channel
        self.add_item(log_sel)

        allowed_sel = discord.ui.ChannelSelect(
            placeholder="📢 Kanäle ohne Prüfung (optional)",
            min_values=0, max_values=5,
            channel_types=[discord.ChannelType.text],
            row=1
        )
        allowed_sel.callback = self._on_allowed_channels
        self.add_item(allowed_sel)

        toggle_btn = discord.ui.Button(
            label="🔄 Aktivieren" if not self.config.get("enabled", False) else "🔄 Deaktivieren",
            style=discord.ButtonStyle.success if not self.config.get("enabled", False) else discord.ButtonStyle.danger,
            row=2
        )
        toggle_btn.callback = self._on_toggle
        self.add_item(toggle_btn)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.primary,
            row=2
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    async def _on_log_channel(self, interaction: discord.Interaction):
        self.config["moderation_log_channel_id"] = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_allowed_channels(self, interaction: discord.Interaction):
        self.config["allowed_channel_ids"] = ",".join(interaction.data["values"])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.config["enabled"] = not self.config.get("enabled", False)
        self._rebuild()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if not self.config.get("moderation_log_channel_id"):
            await interaction.response.send_message(
                "❌ Bitte einen Log-Kanal auswählen.",
                ephemeral=True
            )
            return

        _set_link_config(self.guild_id, {
            "enabled": self.config.get("enabled", False),
            "moderation_log_channel_id": self.config.get("moderation_log_channel_id"),
            "allowed_channel_ids": self.config.get("allowed_channel_ids", ""),
        })

        embed = self.build_embed()
        embed.title = "✅ Link-Protection gespeichert!"
        embed.color = discord.Color.green()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)


# ── SQL für die benötigten Tabellen ────────────────────────────────────────

SQL_SCHEMA = """
-- Tabelle für die Link-Protection-Konfiguration
CREATE TABLE IF NOT EXISTS link_protection_config (
    server_id TEXT PRIMARY KEY,
    enabled BOOLEAN DEFAULT FALSE,
    moderation_log_channel_id TEXT,
    allowed_channel_ids TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Tabelle für erlaubte URLs/Domains (globale Whitelist)
CREATE TABLE IF NOT EXISTS link_protection_allowed (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    url TEXT NOT NULL,
    channel_id TEXT,
    user_id TEXT,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (server_id, url)
);

-- Tabelle für YouTube/Twitch-Kanal-Whitelist
CREATE TABLE IF NOT EXISTS link_protection_platform_whitelist (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
    channel_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (server_id, platform, channel_id)
);

-- Tabelle für User-Freigaben
CREATE TABLE IF NOT EXISTS link_protection_user_allow (
    server_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    allowed_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (server_id, user_id)
);

-- Tabelle für Logs
CREATE TABLE IF NOT EXISTS link_protection_logs (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    action TEXT NOT NULL,
    user_id TEXT NOT NULL,
    target_url TEXT,
    moderator_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);
"""


async def setup(bot: commands.Bot):

    await bot.add_cog(LinkProtectionCog(bot))