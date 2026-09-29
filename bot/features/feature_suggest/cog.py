"""
bot/features/feature_suggest/cog.py
====================================
Feature Suggest System – Discord Cog

Commands:
  /suggest  – Feature vorschlagen (DM mit Web-Link)

Fix: Loop wird in cog_load() gespeichert (läuft garantiert im Bot-Loop).
     Thread-safe dispatch_notify_* Methoden für Aufrufe aus dem Flask-Thread.
"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("feature_suggest")

MBL_ID      = os.getenv("MBL", "")
WEB_BASE    = os.getenv("WEB_BASE_URL", "http://localhost:5000")
TOKEN_TTL_H = 72


STATUS_LABELS = {
    "proposed":    "💡 Vorgeschlagen",
    "accepted":    "✅ Angenommen",
    "in_progress": "🔧 In Bearbeitung",
    "done":        "🎉 Erledigt",
    "rejected":    "❌ Abgelehnt",
}

PRIORITY_LABELS = {
    "important": "🔴 Wichtig",
    "normal":    "🟡 Normal",
    "can_wait":  "🟢 Kann warten",
    "bug":       "🐛 Bug",
}


class FeatureSuggest(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._loop: asyncio.AbstractEventLoop | None = None

    async def cog_load(self):
        """Wird vom Bot innerhalb des Loops aufgerufen."""
        self._loop = asyncio.get_running_loop()
        logger.info("[FeatureSuggest] Cog geladen, Loop gespeichert.")

    # Fallback falls cog_load nicht unterstützt wird
    @commands.Cog.listener()
    async def on_ready(self):
        if self._loop is None or self._loop.is_closed():
            self._loop = asyncio.get_running_loop()
            logger.info("[FeatureSuggest] Loop in on_ready gesetzt.")

    # ── Thread-safe API für Flask ────────────────────────────────────────────
    def dispatch_notify_user(
        self, user_id: str, suggestion_id: int, title: str,
        old_status: str, new_status: str, comment: str = "",
    ) -> bool:
        """Kann aus jedem Thread (z.B. Flask) aufgerufen werden."""
        if self._loop is None or self._loop.is_closed():
            logger.warning("[FeatureSuggest] Kein Loop verfügbar – DM übersprungen.")
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self.notify_user(
                    user_id, suggestion_id, title,
                    old_status, new_status, comment,
                ),
                self._loop,
            )
            return True
        except Exception as e:
            logger.error(f"[FeatureSuggest] dispatch_notify_user: {e}")
            return False

    def dispatch_notify_mbl(
        self, suggestion_id: int, title: str, creator: str,
    ) -> bool:
        if self._loop is None or self._loop.is_closed():
            logger.warning("[FeatureSuggest] Kein Loop verfügbar – MBL-DM übersprungen.")
            return False
        try:
            asyncio.run_coroutine_threadsafe(
                self.notify_mbl(suggestion_id, title, creator),
                self._loop,
            )
            return True
        except Exception as e:
            logger.error(f"[FeatureSuggest] dispatch_notify_mbl: {e}")
            return False

    # ── /suggest ─────────────────────────────────────────────────────────────
    @app_commands.command(
        name="suggest",
        description="Schlage ein neues Bot-Feature vor (Link kommt per DM).",
    )
    async def suggest(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        user    = interaction.user
        token   = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(hours=TOKEN_TTL_H)

        try:
            sb = get_supabase()
            sb.table("feature_suggestion_tokens").insert({
                "token":      token,
                "user_id":    str(user.id),
                "expires_at": expires.isoformat(),
            }).execute()
        except Exception as e:
            logger.error(f"[suggest] Token-Insert fehlgeschlagen: {e}")
            await interaction.followup.send(
                "❌ Fehler beim Erstellen des Links. Bitte später erneut versuchen.",
                ephemeral=True,
            )
            return

        link = f"{WEB_BASE}/feature-suggest?token={token}"

        embed = discord.Embed(
            title="💡 Feature-Vorschlag",
            description=(
                "Du kannst jetzt ein neues Feature vorschlagen!\n\n"
                f"**Dein persönlicher Link:**\n{link}\n\n"
                f"⏱ Gültig für {TOKEN_TTL_H} Stunden."
            ),
            color=0x4ade80,
        )
        try:
            await user.send(embed=embed)
            await interaction.followup.send(
                "✅ Link wurde dir per DM geschickt!", ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ Ich konnte dir keine DM schicken. Hier ist der Link:\n{link}",
                ephemeral=True,
            )

    # ── Notify MBL ───────────────────────────────────────────────────────────
    async def notify_mbl(self, suggestion_id: int, title: str, creator: str):
        if not MBL_ID:
            logger.warning("[FeatureSuggest] MBL_ID nicht gesetzt – MBL-DM übersprungen.")
            return
        try:
            mbl_user = await self.bot.fetch_user(int(MBL_ID))
            embed = discord.Embed(
                title="🆕 Neuer Feature-Vorschlag",
                description=(
                    f"**#{suggestion_id} – {title}**\n"
                    f"Vorgeschlagen von: {creator}"
                ),
                color=0x60a5fa,
            )
            embed.add_field(
                name="Bearbeiten",
                value=f"{WEB_BASE}/dashboard/features",
                inline=False,
            )
            await mbl_user.send(embed=embed)
            logger.info(f"[FeatureSuggest] MBL-DM gesendet (#{suggestion_id})")
        except discord.Forbidden:
            logger.warning("[FeatureSuggest] MBL hat DMs deaktiviert.")
        except discord.NotFound:
            logger.warning(f"[FeatureSuggest] MBL-User {MBL_ID} nicht gefunden.")
        except Exception as e:
            logger.error(f"[FeatureSuggest] notify_mbl fehlgeschlagen: {e}")

    # ── Notify User (Status-Update) ──────────────────────────────────────────
    async def notify_user(
        self, user_id: str, suggestion_id: int, title: str,
        old_status: str, new_status: str, comment: str = "",
    ):
        try:
            u = await self.bot.fetch_user(int(user_id))
            embed = discord.Embed(
                title="🔔 Update zu deinem Feature-Vorschlag",
                description=f"**#{suggestion_id} – {title}**",
                color=0x4ade80,
            )
            embed.add_field(
                name="Status",
                value=f"{STATUS_LABELS.get(old_status, old_status)} → "
                      f"{STATUS_LABELS.get(new_status, new_status)}",
                inline=False,
            )
            if comment:
                embed.add_field(name="Kommentar", value=comment[:1024], inline=False)
            embed.add_field(
                name="Übersicht",
                value=f"{WEB_BASE}/dashboard/features",
                inline=False,
            )
            await u.send(embed=embed)
            logger.info(f"[FeatureSuggest] DM an {user_id} gesendet (#{suggestion_id})")
        except discord.Forbidden:
            logger.warning(f"[FeatureSuggest] User {user_id} hat DMs deaktiviert.")
        except discord.NotFound:
            logger.warning(f"[FeatureSuggest] User {user_id} nicht gefunden.")
        except Exception as e:
            logger.error(f"[FeatureSuggest] notify_user fehlgeschlagen: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(FeatureSuggest(bot))