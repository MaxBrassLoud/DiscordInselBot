"""
bot/features/stream_notifications/views.py
===========================================
Persistente Accept/Decline-Views für Einladungs-DMs.

Wichtig für Neustarts:
  Jede Einladung erhält einen EIGENEN custom_id-Suffix (account_id),
  weil discord.py exakte custom_ids matcht. Der Cog registriert beim
  on_ready alle pending-Einladungen einmal über bot.add_view(...).
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord

from bot.utils.logger import get_logger

logger = get_logger("stream_notifications.views")


class InvitationView(discord.ui.View):
    def __init__(self, account_id: int, manager, bot: discord.Client):
        super().__init__(timeout=None)
        self.account_id = account_id
        self.manager    = manager
        self.bot        = bot

        accept = discord.ui.Button(
            label="✅ Annehmen",
            style=discord.ButtonStyle.success,
            custom_id=f"stream_inv_accept_{account_id}",
        )
        accept.callback = self._on_accept
        self.add_item(accept)

        decline = discord.ui.Button(
            label="❌ Ablehnen",
            style=discord.ButtonStyle.danger,
            custom_id=f"stream_inv_decline_{account_id}",
        )
        decline.callback = self._on_decline
        self.add_item(decline)

    # ── Handler ───────────────────────────────────────────────────────────
    async def _on_accept(self, interaction: discord.Interaction):
        await self._handle(interaction, accept=True)

    async def _on_decline(self, interaction: discord.Interaction):
        await self._handle(interaction, accept=False)

    async def _handle(self, interaction: discord.Interaction, accept: bool):
        # Sofort bestätigen, damit langsame DB-Aufrufe kein 10062 erzeugen
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.HTTPException as e:
            logger.warning(f"[invitation] defer fehlgeschlagen: {e}")
            return

        account = self.manager.get_account(self.account_id)
        if not account:
            await interaction.followup.send(
                "❌ Diese Einladung existiert nicht mehr.", ephemeral=True
            )
            return

        if str(account.get("discord_user_id")) != str(interaction.user.id):
            await interaction.followup.send(
                "❌ Diese Einladung gehört dir nicht.", ephemeral=True
            )
            return

        if account.get("invitation_status") != "pending":
            await interaction.followup.send(
                "ℹ️ Diese Einladung wurde bereits bearbeitet.", ephemeral=True
            )
            return

        if self.manager.is_expired(account):
            self.manager.update_account(self.account_id, {
                "invitation_status": "expired",
                "invitation_token":  None,
            })
            await interaction.followup.send(
                "⏰ Diese Einladung ist abgelaufen. Bitte ein Teammitglied um eine neue.",
                ephemeral=True,
            )
            return

        if accept:
            await self._accept(interaction, account)
        else:
            await self._decline(interaction, account)

    async def _accept(self, interaction, account):
        from .service import activate_account
        try:
            ok = await activate_account(self.bot, account)
            self.manager.update_account(self.account_id, {
                "invitation_status": "accepted",
                "invitation_token":  None,
                "streamer_role_given": bool(account.get("alert_enabled")),
            })
            mode = (
                "Du erhältst die **Streamer-Rolle** und wirst bei Live-Gängen gepingt."
                if account.get("alert_enabled")
                else "Du erhältst die **Live-Rolle** (Roter Punkt), sobald du streamst."
            )
            await interaction.followup.send(
                f"✅ Willkommen! Du bist jetzt für "
                f"**{account['account_name'] or account['account_id']}** "
                f"({account['platform'].title()}) eingetragen.\n\n{mode}",
                ephemeral=True,
            )
            if not ok:
                logger.warning(
                    f"[invitation] Rollenvergabe für Account {self.account_id} "
                    "unvollständig."
                )
            await self._notify_team(account, accepted=True)
        except Exception as e:
            logger.error(f"[invitation] accept: {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    async def _decline(self, interaction, account):
        self.manager.update_account(self.account_id, {
            "invitation_status": "declined",
            "invitation_token":  None,
        })
        await interaction.followup.send(
            "❌ Du hast die Einladung abgelehnt. Das Team wurde informiert.",
            ephemeral=True,
        )
        await self._notify_team(account, accepted=False)

    async def _notify_team(self, account, accepted: bool):
        try:
            config = self.manager.get_config(account["guild_id"])
            log_id = config.get("log_channel_id")
            if not log_id:
                return
            guild = self.bot.get_guild(int(account["guild_id"]))
            if not guild:
                return
            channel = guild.get_channel(int(log_id))
            if not channel:
                return
            verb = "angenommen" if accepted else "abgelehnt"
            emoji = "✅" if accepted else "❌"
            await channel.send(
                f"{emoji} Stream-Benachrichtigung für "
                f"**{account['account_name'] or account['account_id']}** "
                f"wurde von <@{account['discord_user_id']}> {verb}.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as e:
            logger.debug(f"[invitation] notify_team: {e}")