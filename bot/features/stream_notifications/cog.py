"""
bot/features/stream_notifications/cog.py
==========================================
Stream-Benachrichtigungssystem v2.

Kernidee:
  • JEDER mit Zustimmung (via DM-Einladung) kann Streamer sein – keine
    Content-Rolle als Voraussetzung.
  • Team entscheidet pro Eintrag:
        alert=True  → Streamer-Rolle (permanent) + Channel-Alert + Live-Rolle
        alert=False → nur Live-Rolle (Roter Punkt), kein Alert, keine
                      Content-Rolle.
  • Alte link/unlink/…-Befehle entfallen vollständig.
  • Streamer können sich NICHT selbst entfernen (nur Admins via remove).

COMMANDS:
  /streamalerts setup   – Kanal + Streamer-/Live-Rolle + optionaler Log-Kanal
  /streamalerts add     – Streamer eintragen (mit optionalem Discord-User)
  /streamalerts edit    – Eintrag bearbeiten
  /streamalerts remove  – Eintrag entfernen (nur Team)
  /streamalerts list    – Alle Einträge (Team-Übersicht)
  /streamalerts status  – Eigene Einträge (User-Sicht)
  /streamalerts resend  – Einladung erneut senden
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

from .detector import StreamDetector
from .manager import StreamAccountManager
from .service import (
    activate_account,
    deactivate_account,
    handle_live_transition,
    handle_offline_transition,
)
from .views import InvitationView

logger = get_logger("stream_notifications")


# ══════════════════════════════════════════════════════════════════════════════
# URL-PARSING
# ══════════════════════════════════════════════════════════════════════════════

TWITCH_RE     = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?twitch\.tv/([A-Za-z0-9_]{3,25})/?(?:\?.*)?$"
)
YT_CHANNEL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/channel/([A-Za-z0-9_-]+)"
)
YT_HANDLE_RE  = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/@([A-Za-z0-9_.\-]+)"
)
YT_CUSTOM_RE  = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/c/([A-Za-z0-9_\-]+)"
)
YT_USER_RE    = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?youtube\.com/user/([A-Za-z0-9_\-]+)"
)

INVALID_TWITCH_HANDLES = {
    "videos", "directory", "settings", "subscriptions",
    "inventory", "wallet", "drops", "downloads",
}


def parse_stream_url(url: str) -> tuple[Optional[str], Optional[str]]:
    """Liefert (platform, account_id) oder (None, None)."""
    if not url:
        return None, None
    url = url.strip()

    m = TWITCH_RE.match(url)
    if m:
        login = m.group(1).lower()
        if login in INVALID_TWITCH_HANDLES:
            return None, None
        return "twitch", login

    for regex in (YT_CHANNEL_RE, YT_HANDLE_RE, YT_CUSTOM_RE, YT_USER_RE):
        m = regex.search(url)
        if m:
            return "youtube", m.group(1)

    return None, None


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class StreamAlertsCog(commands.Cog):
    CONCURRENT_CHECKS = 5   # parallele yt-dlp-Abfragen
    CHECK_INTERVAL    = 120  # Sekunden zwischen zwei Durchläufen

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._restored_invitations = False
        self._checker_task.start()

    def cog_unload(self):
        self._checker_task.cancel()

    # ═════════════════════════════════════════════════════════════════════════
    # ON READY – persistente Views wiederherstellen
    # ═════════════════════════════════════════════════════════════════════════

    @commands.Cog.listener()
    async def on_ready(self):
        if self._restored_invitations:
            return
        self._restored_invitations = True
        try:
            pending = StreamAccountManager.get_pending_invitations()
            registered = 0
            for account in pending:
                # Abgelaufene Einladungen direkt markieren
                if StreamAccountManager.is_expired(account):
                    StreamAccountManager.update_account(account["id"], {
                        "invitation_status": "expired",
                        "invitation_token":  None,
                    })
                    continue
                view = InvitationView(
                    account_id=account["id"],
                    manager=StreamAccountManager,
                    bot=self.bot,
                )
                self.bot.add_view(view)
                registered += 1
            logger.info(
                f"✅ {registered} Stream-Invitation-View(s) registriert "
                f"({len(pending) - registered} abgelaufen)"
            )
        except Exception as e:
            logger.error(f"[on_ready] View-Restore fehlgeschlagen: {e}")

    # ═════════════════════════════════════════════════════════════════════════
    # SLASH GROUP
    # ═════════════════════════════════════════════════════════════════════════

    streamalerts = app_commands.Group(
        name="streamalerts",
        description="Stream-Benachrichtigungen verwalten",
    )

    # ── /streamalerts setup ───────────────────────────────────────────────
    @streamalerts.command(
        name="setup",
        description="Konfiguriere das Stream-Alert-System",
    )
    @app_commands.describe(
        alert_channel="Kanal in dem Live-Alerts gesendet werden",
        streamer_role="Rolle für verifizierte Streamer (Content Creator)",
        live_role="Rolle die NUR während des Streams vergeben wird (Roter Punkt)",
        log_channel="Kanal für Annahme/Ablehnungs-Protokolle (optional)",
    )
    async def setup_cmd(
        self,
        interaction: discord.Interaction,
        alert_channel: discord.TextChannel,
        streamer_role: discord.Role,
        live_role: discord.Role,
        log_channel: Optional[discord.TextChannel] = None,
    ):
        if not has_admin_rights(interaction):
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
            return

        # Rollen-Reihenfolge prüfen: die Bot-Rolle muss über beiden stehen.
        bot_member = interaction.guild.me
        if bot_member:
            if streamer_role >= bot_member.top_role:
                await interaction.response.send_message(
                    f"❌ Die Streamer-Rolle {streamer_role.mention} steht **über** "
                    f"der Bot-Rolle. Bitte ziehe die Bot-Rolle höher.",
                    ephemeral=True,
                )
                return
            if live_role >= bot_member.top_role:
                await interaction.response.send_message(
                    f"❌ Die Live-Rolle {live_role.mention} steht **über** "
                    f"der Bot-Rolle. Bitte ziehe die Bot-Rolle höher.",
                    ephemeral=True,
                )
                return
            # Streamer- und Live-Rolle dürfen nicht identisch sein
            if streamer_role.id == live_role.id:
                await interaction.response.send_message(
                    "❌ Streamer-Rolle und Live-Rolle müssen unterschiedlich sein.",
                    ephemeral=True,
                )
                return

        ok = StreamAccountManager.upsert_config(str(interaction.guild_id), {
            "channel_id":       str(alert_channel.id),
            "streamer_role_id": str(streamer_role.id),
            "live_role_id":     str(live_role.id),
            "log_channel_id":   str(log_channel.id) if log_channel else None,
            "enabled":          True,
        })
        if not ok:
            await interaction.response.send_message(
                "❌ Fehler beim Speichern der Konfiguration.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="✅ Stream-Alert-System konfiguriert",
            color=discord.Color.green(),
        )
        embed.add_field(name="📢 Alert-Kanal",    value=alert_channel.mention, inline=True)
        embed.add_field(name="🎬 Streamer-Rolle", value=streamer_role.mention, inline=True)
        embed.add_field(name="🔴 Live-Rolle",     value=live_role.mention,     inline=True)
        if log_channel:
            embed.add_field(name="📋 Log-Kanal",  value=log_channel.mention,   inline=False)
        embed.add_field(
            name="ℹ️ Nächster Schritt",
            value="Füge Streamer mit `/streamalerts add` hinzu.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /streamalerts add ─────────────────────────────────────────────────
    @streamalerts.command(name="add", description="Füge einen Streamer hinzu")
    @app_commands.describe(
        url="Twitch- oder YouTube-Kanal-URL",
        user="Discord-Mitglied (optional – erhält Einladung per DM)",
        alert="True = Streamer-Rolle + Alerts | False = nur Live-Rolle",
    )
    async def add_cmd(
        self,
        interaction: discord.Interaction,
        url: str,
        user: Optional[discord.Member] = None,
        alert: bool = True,
    ):
        if not has_admin_rights(interaction):
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        # 1. Setup prüfen
        config = StreamAccountManager.get_config(str(interaction.guild_id))
        if not config.get("channel_id"):
            await interaction.followup.send(
                "❌ Das Stream-Alert-System ist noch nicht eingerichtet. "
                "Nutze zuerst `/streamalerts setup`.",
                ephemeral=True,
            )
            return

        # 2. URL parsen
        platform, account_id = parse_stream_url(url)
        if not platform:
            await interaction.followup.send(
                "❌ Konnte die URL nicht erkennen. Beispiele:\n"
                "• `https://twitch.tv/username`\n"
                "• `https://youtube.com/@handle`\n"
                "• `https://youtube.com/channel/UC…`\n"
                "• `https://youtube.com/user/username`",
                ephemeral=True,
            )
            return

        # 3. Duplikat-Check
        for existing in StreamAccountManager.list_accounts(str(interaction.guild_id)):
            if (existing["platform"] == platform
                    and existing["account_id"].lower() == account_id.lower()):
                await interaction.followup.send(
                    f"❌ Dieser Streamer ist bereits eingetragen "
                    f"(ID `{existing['id']}`). Nutze `/streamalerts edit`.",
                    ephemeral=True,
                )
                return

        # 4. Account anlegen
        account = StreamAccountManager.create_account(
            guild_id=str(interaction.guild_id),
            platform=platform,
            account_id=account_id,
            account_name=account_id,
            discord_user_id=str(user.id) if user else None,
            alert_enabled=bool(alert),
            added_by=str(interaction.user.id),
        )
        if not account:
            await interaction.followup.send(
                "❌ Fehler beim Speichern in der Datenbank.", ephemeral=True
            )
            return

        # 5. Bestätigungs-Embed aufbauen
        embed = discord.Embed(
            title="✅ Streamer hinzugefügt",
            color=discord.Color.green(),
        )
        embed.add_field(name="🆔 ID",        value=f"`{account['id']}`",  inline=True)
        embed.add_field(name="📺 Plattform", value=platform.title(),      inline=True)
        embed.add_field(name="🔗 Account",   value=f"`{account_id}`",     inline=True)
        embed.add_field(
            name="🎬 Modus",
            value="🔔 Streamer-Rolle + Alerts" if alert else "🔇 nur Live-Rolle",
            inline=True,
        )
        embed.add_field(
            name="👤 Verknüpfter User",
            value=user.mention if user else "*keiner*",
            inline=True,
        )

        # 6. Einladung senden oder direkt aktivieren
        if user:
            sent = await self._send_invitation(account, user)
            embed.add_field(
                name="📨 Einladung",
                value=("✅ Per DM versendet"
                       if sent else
                       "⚠️ Konnte nicht per DM gesendet werden "
                       "(DMs blockiert? Nutze `/streamalerts resend`)"),
                inline=False,
            )
        else:
            StreamAccountManager.update_account(account["id"], {
                "invitation_status": "accepted",
            })
            embed.add_field(
                name="📨 Einladung",
                value="ℹ️ Kein User verknüpft – nur Tracking aktiv.",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /streamalerts edit ────────────────────────────────────────────────
    @streamalerts.command(name="edit", description="Bearbeite einen Streamer")
    @app_commands.describe(
        account_id="ID des Streamers (siehe /streamalerts list)",
        url="Neue URL (optional)",
        user="Neuer Discord-User (optional)",
        alert="Neuer Alert-Modus (optional)",
    )
    async def edit_cmd(
        self,
        interaction: discord.Interaction,
        account_id: int,
        url: Optional[str] = None,
        user: Optional[discord.Member] = None,
        alert: Optional[bool] = None,
    ):
        if not has_admin_rights(interaction):
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        account = StreamAccountManager.get_account(account_id)
        if not account or str(account["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send(
                f"❌ Streamer mit ID `{account_id}` nicht gefunden.",
                ephemeral=True,
            )
            return

        updates: dict = {}
        user_changed = False

        if url is not None:
            platform, new_id = parse_stream_url(url)
            if not platform:
                await interaction.followup.send("❌ Ungültige URL.", ephemeral=True)
                return
            updates["platform"]     = platform
            updates["account_id"]   = new_id
            updates["account_name"] = new_id

        if user is not None and str(user.id) != str(account.get("discord_user_id")):
            user_changed = True
            # Alten User vollständig entrollen
            if account.get("discord_user_id"):
                await deactivate_account(self.bot, account)
            updates["discord_user_id"] = str(user.id)

        if alert is not None and alert != account.get("alert_enabled"):
            updates["alert_enabled"] = alert

        if not updates:
            await interaction.followup.send(
                "ℹ️ Keine Änderungen angegeben.", ephemeral=True
            )
            return

        StreamAccountManager.update_account(account_id, updates)
        updated = StreamAccountManager.get_account(account_id)

        # Einladung neu senden, wenn der User gewechselt hat
        invitation_msg = ""
        if user_changed and user is not None:
            token = StreamAccountManager.issue_new_invitation_token(account_id)
            if token:
                fresh = StreamAccountManager.get_account(account_id)
                sent = await self._send_invitation(fresh, user)
                invitation_msg = (
                    "Neue Einladung wurde per DM versendet."
                    if sent else
                    "⚠️ Einladung konnte nicht gesendet werden."
                )

        # Alert-Modus geändert und Account bereits akzeptiert?
        elif alert is not None and account.get("invitation_status") == "accepted":
            if alert:
                await activate_account(self.bot, updated)
            else:
                # Streamer-Rolle entziehen; Live-Rolle bleibt ggf. während
                # eines laufenden Streams bestehen.
                guild = self.bot.get_guild(int(account["guild_id"]))
                member = (
                    guild.get_member(int(updated["discord_user_id"]))
                    if guild and updated.get("discord_user_id") else None
                )
                config = StreamAccountManager.get_config(account["guild_id"])
                if member and config.get("streamer_role_id"):
                    role = guild.get_role(int(config["streamer_role_id"]))
                    if role and role in member.roles:
                        try:
                            await member.remove_roles(role, reason="Alert-Modus geändert")
                        except Exception as e:
                            logger.warning(f"[edit] Rolle entfernen: {e}")

        embed = discord.Embed(
            title="✅ Streamer bearbeitet",
            color=discord.Color.green(),
        )
        embed.add_field(name="🆔 ID",        value=f"`{account_id}`",           inline=True)
        embed.add_field(name="📺 Plattform", value=updated["platform"].title(), inline=True)
        embed.add_field(name="🔗 Account",   value=f"`{updated['account_id']}`", inline=True)
        embed.add_field(
            name="🎬 Alert",
            value="✅ AN" if updated["alert_enabled"] else "🔇 AUS",
            inline=True,
        )
        embed.add_field(
            name="👤 User",
            value=(f"<@{updated['discord_user_id']}>"
                   if updated.get("discord_user_id") else "*keiner*"),
            inline=True,
        )
        embed.add_field(
            name="📨 Status",
            value=updated["invitation_status"],
            inline=True,
        )
        if invitation_msg:
            embed.add_field(name="📬 Einladung", value=invitation_msg, inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /streamalerts remove ──────────────────────────────────────────────
    @streamalerts.command(name="remove", description="Entferne einen Streamer")
    @app_commands.describe(account_id="ID des Streamers (siehe /streamalerts list)")
    async def remove_cmd(self, interaction: discord.Interaction, account_id: int):
        # Nur Admins – verhindert, dass Streamer sich selbst austragen.
        if not has_admin_rights(interaction):
            await interaction.response.send_message(
                "❌ Keine Berechtigung. Streamer können sich nicht selbst austragen.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)

        account = StreamAccountManager.get_account(account_id)
        if not account or str(account["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send(
                f"❌ Streamer mit ID `{account_id}` nicht gefunden.",
                ephemeral=True,
            )
            return

        await deactivate_account(self.bot, account)
        StreamAccountManager.delete_account(account_id)

        embed = discord.Embed(
            title="🗑️ Streamer entfernt",
            description=(
                f"`{account['account_name'] or account['account_id']}` "
                f"(ID `{account_id}`) wurde entfernt. "
                f"Alle zugehörigen Rollen wurden entzogen."
            ),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /streamalerts list ────────────────────────────────────────────────
    @streamalerts.command(
        name="list",
        description="Liste aller eingetragenen Streamer",
    )
    async def list_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        accounts = StreamAccountManager.list_accounts(str(interaction.guild_id))
        if not accounts:
            await interaction.followup.send(
                "ℹ️ Noch keine Streamer eingetragen.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title=f"📺 Streamer auf {interaction.guild.name}",
            description=f"**{len(accounts)} Einträge**",
            color=discord.Color.blurple(),
        )

        STATUS_EMOJI = {
            "accepted": "✅",
            "pending":  "⏳",
            "declined": "❌",
            "expired":  "⏰",
            "none":     "➖",
        }

        for a in accounts[:25]:
            status = a.get("invitation_status") or "none"
            status_icon = STATUS_EMOJI.get(status, "➖")
            live_icon   = "🔴" if a.get("is_live") else "⚫"
            alert_str   = "🔔 Alert" if a.get("alert_enabled") else "🔇 Live-only"
            user_str    = (
                f"<@{a['discord_user_id']}>" if a.get("discord_user_id") else "*keiner*"
            )
            embed.add_field(
                name=f"{live_icon} `#{a['id']}` · {a['platform'].title()} · `{a['account_id']}`",
                value=f"{status_icon} `{status}` · {alert_str} · {user_str}",
                inline=False,
            )
        if len(accounts) > 25:
            embed.set_footer(text=f"… und {len(accounts) - 25} weitere")
        else:
            embed.set_footer(text="/streamalerts edit <id> zum Bearbeiten")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /streamalerts status ──────────────────────────────────────────────
    @streamalerts.command(
        name="status",
        description="Zeigt deine eigenen Stream-Einträge",
    )
    async def status_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        accounts = StreamAccountManager.get_accounts_for_discord_user(
            str(interaction.user.id)
        )
        if not accounts:
            await interaction.followup.send(
                "ℹ️ Du bist für keine Stream-Benachrichtigungen eingetragen. "
                "Wende dich ans Team, wenn du Streamer werden möchtest.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📺 Deine Stream-Benachrichtigungen",
            color=(discord.Color.green()
                   if any(a.get("is_live") for a in accounts)
                   else discord.Color.blurple()),
        )

        STATUS_EMOJI = {
            "accepted": "✅",
            "pending":  "⏳",
            "declined": "❌",
            "expired":  "⏰",
            "none":     "➖",
        }

        for a in accounts:
            status = a.get("invitation_status") or "none"
            status_icon = STATUS_EMOJI.get(status, "➖")
            live_str = "🔴 **LIVE**" if a.get("is_live") else "⚫ offline"
            mode = ("🔔 Streamer-Rolle + Alerts"
                    if a.get("alert_enabled") else "🔇 nur Live-Rolle")
            embed.add_field(
                name=f"{a['platform'].title()} · `{a['account_id']}`",
                value=f"{status_icon} `{status}` · {mode} · {live_str}",
                inline=False,
            )
        embed.set_footer(
            text="Du kannst dich nicht selbst entfernen – wende dich an das Team."
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /streamalerts resend ──────────────────────────────────────────────
    @streamalerts.command(
        name="resend",
        description="Sendet eine Einladung erneut",
    )
    @app_commands.describe(account_id="ID des Streamers (siehe /streamalerts list)")
    async def resend_cmd(self, interaction: discord.Interaction, account_id: int):
        if not has_admin_rights(interaction):
            await interaction.response.send_message(
                "❌ Keine Berechtigung.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)

        account = StreamAccountManager.get_account(account_id)
        if not account or str(account["guild_id"]) != str(interaction.guild_id):
            await interaction.followup.send(
                f"❌ Streamer mit ID `{account_id}` nicht gefunden.",
                ephemeral=True,
            )
            return
        if not account.get("discord_user_id"):
            await interaction.followup.send(
                "❌ Kein Discord-User mit diesem Eintrag verknüpft.",
                ephemeral=True,
            )
            return

        guild = self.bot.get_guild(int(account["guild_id"]))
        member = guild.get_member(int(account["discord_user_id"])) if guild else None
        if not member:
            await interaction.followup.send(
                "❌ Der verknüpfte User ist nicht (mehr) auf dem Server.",
                ephemeral=True,
            )
            return

        token = StreamAccountManager.issue_new_invitation_token(account_id)
        if not token:
            await interaction.followup.send(
                "❌ Konnte keinen neuen Token ausstellen.", ephemeral=True
            )
            return

        # View neu registrieren, da die Custom-IDs der alten View bestehen bleiben,
        # wir aber einen neuen Token erzeugen (View bleibt account_id-basiert,
        # also unverändert – die Registrierung erneut vornehmen ist idempotent).
        try:
            self.bot.add_view(InvitationView(
                account_id=account_id,
                manager=StreamAccountManager,
                bot=self.bot,
            ))
        except Exception as e:
            logger.debug(f"[resend] add_view: {e}")

        fresh = StreamAccountManager.get_account(account_id)
        sent = await self._send_invitation(fresh, member)
        if sent:
            await interaction.followup.send(
                f"✅ Neue Einladung an {member.mention} gesendet.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"⚠️ Konnte {member.mention} keine DM senden – DMs blockiert?",
                ephemeral=True,
            )

    # ═════════════════════════════════════════════════════════════════════════
    # INTERNE HELFER
    # ═════════════════════════════════════════════════════════════════════════

    async def _send_invitation(
        self,
        account: dict,
        user: discord.Member,
    ) -> bool:
        """Sendet die Einladungs-DM. Gibt True zurück bei Erfolg."""
        try:
            embed = discord.Embed(
                title="📺 Stream-Benachrichtigung – Einladung",
                description=(
                    f"Das Team von **{user.guild.name}** möchte dich als "
                    f"Streamer eintragen.\n\n"
                    f"**Plattform:** {account['platform'].title()}\n"
                    f"**Account:** `{account['account_id']}`\n"
                    f"**Modus:** "
                    + ("🔔 Streamer-Rolle + Alerts bei Live-Gängen"
                       if account.get("alert_enabled")
                       else "🔇 Nur Live-Rolle (Roter Punkt), keine Alerts")
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(
                text="Diese Einladung ist 7 Tage gültig. Bei Fragen: Team kontaktieren."
            )
            view = InvitationView(
                account_id=account["id"],
                manager=StreamAccountManager,
                bot=self.bot,
            )
            await user.send(embed=embed, view=view)
            return True
        except discord.Forbidden:
            logger.info(
                f"[invitation] {user.id} hat DMs blockiert – Einladung nicht zugestellt."
            )
            return False
        except Exception as e:
            logger.error(f"[invitation] send: {e}")
            return False

    # ═════════════════════════════════════════════════════════════════════════
    # CHECKER-LOOP
    # ═════════════════════════════════════════════════════════════════════════

    @tasks.loop(seconds=CHECK_INTERVAL)
    async def _checker_task(self):
        try:
            accounts = StreamAccountManager.get_active_accounts()
        except Exception as e:
            logger.error(f"[checker] Laden der Accounts fehlgeschlagen: {e}")
            return
        if not accounts:
            return

        sem = asyncio.Semaphore(self.CONCURRENT_CHECKS)

        async def _guarded(acc: dict):
            async with sem:
                await self._check_one_account(acc)

        await asyncio.gather(*[_guarded(a) for a in accounts], return_exceptions=True)

    @_checker_task.before_loop
    async def _before_checker(self):
        await self.bot.wait_until_ready()

    async def _check_one_account(self, account: dict):
        """Prüft einen einzelnen Account und reagiert auf Statuswechsel."""
        try:
            status = await StreamDetector.check(
                account["platform"], account["account_id"]
            )
        except Exception as e:
            logger.warning(
                f"[checker] Fehler bei {account['platform']}/"
                f"{account['account_id']}: {e}"
            )
            StreamAccountManager.update_account(account["id"], {
                "last_error": str(e),
            })
            return

        # Fehler → nur protokollieren, is_live-Status nicht verändern
        if status.error:
            StreamAccountManager.update_account(account["id"], {
                "last_error": status.error,
            })
            return

        was_live = bool(account.get("is_live"))
        now_live = bool(status.is_live)

        StreamAccountManager.update_account(account["id"], {
            "last_error":    None,
            "last_check_at": __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        })

        try:
            if now_live and not was_live:
                logger.info(
                    f"[checker] 🔴 LIVE: {account['platform']}/"
                    f"{account['account_id']}"
                )
                await handle_live_transition(self.bot, account, status)
            elif not now_live and was_live:
                logger.info(
                    f"[checker] ⚫ OFFLINE: {account['platform']}/"
                    f"{account['account_id']}"
                )
                await handle_offline_transition(self.bot, account)
        except Exception as e:
            logger.error(f"[checker] Transition: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(StreamAlertsCog(bot))