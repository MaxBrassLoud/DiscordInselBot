"""Raid protection based on total mentions, text duplicates and image duplicates per short time window."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.utils.logger import get_logger

from .raid.case import ImageEvidence, RaidCase
from .raid.config import CONFIG, NOT_ALLOWED_ACTIONS, RAID_ACTIONS
from .raid.db import RaidDatabase
from .raid.detection import is_suspicious
from .raid.evidence import (
    build_report_embed,
    send_evidence_logs,
    send_user_embed,
)
from .raid.forbidden_store import ForbiddenImageStore
from .raid.hashing import compute_image_hashes
from .raid.permissions import format_duration, is_moderator, parse_duration
from .raid.resolve import resolve_case
from .raid.views import ForbiddenReviewView, RaidReportView

logger = get_logger("raid_protection")


RAID_ACTION_COLORS = {
    "approve": discord.Color.green(),
    "warn_release": discord.Color.gold(),
    "keep_timeout": discord.Color.red(),
    "ban": discord.Color.dark_red(),
}


async def _require_moderator(interaction: discord.Interaction) -> bool:
    """Antwortet mit Ephemeral-Fehler, wenn User kein Moderator ist. Gibt True/False zurueck."""
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message(
            "Nur auf Servern nutzbar.", ephemeral=True
        )
        return False
    cog: Optional[RaidProtectionCog] = interaction.client.get_cog("RaidProtectionCog")
    if cog is None:
        await interaction.response.send_message(
            "System nicht bereit.", ephemeral=True
        )
        return False
    if not await cog.is_moderator(interaction.user, interaction.guild):
        await interaction.response.send_message(
            "Keine Berechtigung. (Admin / Moderator-Rolle / MBL)", ephemeral=True
        )
        return False
    return True


class RaidProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watchlist: dict[int, dict[str, Any]] = {}
        self._case_images: dict[str, list[ImageEvidence]] = {}
        self._persistent_registered = False

        self.db = RaidDatabase()
        self.forbidden_store = ForbiddenImageStore(CONFIG["forbidden_images_path"])

    # ------------------------------------------------------------------
    # Permission helper
    # ------------------------------------------------------------------
    async def is_moderator(
        self, user: discord.abc.User, guild: discord.Guild
    ) -> bool:
        return await is_moderator(user, guild, self.db)

    @commands.Cog.listener()
    async def on_ready(self):
        if self._persistent_registered:
            return
        self.bot.add_view(RaidReportView(self))
        self._persistent_registered = True
        logger.info("RaidReportView registered as persistent view")
        logger.info(
            f"[raid] forbidden image store loaded ({self.forbidden_store.size()} Eintraege)"
        )

    # ------------------------------------------------------------------
    # Fall-Aufloesung & Report-Aktionen
    # ------------------------------------------------------------------
    async def resolve_case(
        self, interaction: discord.Interaction, fallback: Optional[RaidCase]
    ) -> Optional[RaidCase]:
        return await resolve_case(interaction, fallback)

    async def execute_report_action(
        self,
        interaction: discord.Interaction,
        action: str,
        case: RaidCase,
        report_message: Optional[discord.Message] = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        meta = RAID_ACTIONS[action]
        guild = interaction.guild or self.bot.get_guild(case.guild_id)
        if not guild:
            await interaction.followup.send("Server nicht gefunden.", ephemeral=True)
            return

        images: list[ImageEvidence] = []
        if case.case_id:
            images = self._case_images.pop(case.case_id, [])

        member = guild.get_member(case.user_id)
        user: discord.User | discord.Member | None = member or self.bot.get_user(
            case.user_id
        )
        if user is None:
            try:
                user = await self.bot.fetch_user(case.user_id)
            except Exception:
                user = None

        if action in {"approve", "warn_release"} and member is None:
            await interaction.followup.send(
                "Der User ist nicht mehr auf dem Server, der Timeout kann nicht aufgehoben werden.",
                ephemeral=True,
            )
            return

        try:
            if action in {"approve", "warn_release"} and member:
                await member.timeout(
                    None, reason=f"Raid-Fall durch {interaction.user} bewertet"
                )
            elif action == "ban":
                if member:
                    await member.ban(
                        reason=f"Raid-Fall durch {interaction.user} bestaetigt"
                    )
                elif user:
                    await guild.ban(
                        user, reason=f"Raid-Fall durch {interaction.user} bestaetigt"
                    )
        except discord.Forbidden:
            await interaction.followup.send(
                "Dem Bot fehlen die passenden Rechte fuer diese Aktion.", ephemeral=True
            )
            return
        except Exception as e:
            await interaction.followup.send(
                f"Fehler bei der Aktion: {e}", ephemeral=True
            )
            return

        if user:
            fields = []
            if action == "keep_timeout" and case.until:
                fields.append(
                    ("Dauer", f"Bis {discord.utils.format_dt(case.until, 'F')}", False)
                )
            await send_user_embed(
                user=user,
                title=meta["dm_title"],
                description=meta["dm_description"],
                color=RAID_ACTION_COLORS[action],
                fields=fields,
            )

        await self.db.log_moderation_action(
            guild_id=str(guild.id),
            action=f"raid_{action}",
            target_id=str(case.user_id),
            target_name=str(user) if user else str(case.user_id),
            moderator_id=str(interaction.user.id),
            moderator_name=str(interaction.user),
            reason=meta["done"],
            until=case.until if action == "keep_timeout" else None,
        )

        message_to_edit = report_message or interaction.message
        if message_to_edit and message_to_edit.embeds:
            embed = message_to_edit.embeds[0]
            embed.color = RAID_ACTION_COLORS[action]
            if meta["done"] not in (embed.description or ""):
                embed.description = (
                    f"{embed.description or ''}\n\n**{meta['done']}**\n"
                    f"Bewertet von {interaction.user.mention}."
                )
            view = RaidReportView(self, case, disabled=True)
            try:
                await message_to_edit.edit(embed=embed, view=view)
            except Exception as e:
                logger.warning(f"[raid] report message edit failed: {e}")

        await interaction.followup.send(meta["feedback"], ephemeral=True)

        if action in NOT_ALLOWED_ACTIONS and images:
            await self._start_forbidden_review(interaction, case, images)

    # ------------------------------------------------------------------
    # Forbidden Image Review
    # ------------------------------------------------------------------
    async def _start_forbidden_review(
        self,
        interaction: discord.Interaction,
        case: RaidCase,
        images: list[ImageEvidence],
    ) -> None:
        seen: set[str] = set()
        unique: list[ImageEvidence] = []
        for img in images:
            if img.sha1 in seen:
                continue
            seen.add(img.sha1)
            unique.append(img)

        if not unique:
            return

        case_id = case.case_id or f"{case.guild_id}-{case.user_id}"
        view = ForbiddenReviewView(self, case_id, unique)
        try:
            await interaction.followup.send(
                content=(
                    "Der Fall wurde als **nicht zulaessig** bewertet. "
                    "Bitte entscheide fuer jedes Bild, ob es zur Liste der verbotenen Bilder "
                    "hinzugefuegt werden soll."
                ),
                embed=view.build_embed(),
                view=view,
                ephemeral=True,
            )
        except Exception as e:
            logger.warning(f"[raid] forbidden review send failed: {e}")

    # ------------------------------------------------------------------
    # on_message
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.author, discord.Member):
            return
        if message.author.is_timed_out():
            return

        # 1. Allow-Liste
        allowed = await self.db.get_allowed_users(message.guild)
        if message.author.id in allowed:
            return

        # 2. Moderator-Rollen / Admins werden ignoriert
        if await self.is_moderator(message.author, message.guild):
            return

        # 3. Exakter SHA1-Match gegen verbotene Bilder
        for attachment in message.attachments:
            if not attachment.content_type or not attachment.content_type.startswith(
                "image/"
            ):
                continue
            try:
                data = await attachment.read()
                sha1, phash = compute_image_hashes(data)
            except Exception:
                continue
            if self.forbidden_store.exact_match(sha1):
                await self._handle_forbidden_image(message, attachment, sha1, phash)
                return

        # 4. Normale Raid-Erkennung
        triggered, details = await is_suspicious(message, self._watchlist)
        if triggered:
            await self._handle_suspicious_user(message, details)

    # ------------------------------------------------------------------
    # Handler: exaktes verbotenes Bild
    # ------------------------------------------------------------------
    async def _handle_forbidden_image(
        self,
        message: discord.Message,
        attachment: discord.Attachment,
        sha1: str,
        phash: str,
    ) -> None:
        user = message.author
        guild = message.guild
        log_channel = await self.db.get_log_channel(guild)

        until = datetime.now(timezone.utc) + timedelta(hours=CONFIG["timeout_hours"])

        try:
            await user.timeout(until, reason="Verbotenes Bild erkannt (SHA1-Match)")
        except Exception as e:
            logger.warning(f"[raid] timeout failed for {user}: {e}")

        await self.db.log_moderation_action(
            guild_id=str(guild.id),
            action="raid_timeout",
            target_id=str(user.id),
            target_name=str(user),
            moderator_id=str(self.bot.user.id) if self.bot.user else None,
            moderator_name=str(self.bot.user) if self.bot.user else "System",
            reason=f"Automatischer Timeout: verbotenes Bild (SHA1 `{sha1[:12]}...`)",
            until=until,
        )

        await send_user_embed(
            user=user,
            title="Verbotenes Bild erkannt - Timeout verhaengt",
            description=(
                "Du hast ein Bild gesendet, das auf diesem Server verboten ist. "
                f"Du wurdest fuer {CONFIG['timeout_hours']} Stunden in den Timeout versetzt."
            ),
            color=discord.Color.dark_red(),
            fields=[
                ("Dauer", f"Bis {discord.utils.format_dt(until, 'F')}", False),
                ("Datei", attachment.filename, False),
            ],
        )

        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"[raid] forbidden image message delete failed: {e}")

        if not log_channel:
            logger.warning(
                f"[raid] no moderation log channel for guild {guild.id}; skipping report"
            )
            return

        case_id = f"{guild.id}-{user.id}-{int(datetime.now(timezone.utc).timestamp())}"

        self._case_images[case_id] = [
            ImageEvidence(
                url=attachment.url,
                filename=attachment.filename,
                sha1=sha1,
                phash=phash,
            )
        ]

        is_new = bool(
            user.joined_at
            and (datetime.now(timezone.utc) - user.joined_at).days
            <= CONFIG["new_member_days"]
        )

        embed = build_report_embed(
            guild=guild,
            user=user,
            until=until,
            is_new=is_new,
            messages=[message],
            trigger_reason="forbidden_image",
            trigger_count=1,
            sum_mentions=0,
            case_id=case_id,
        )
        await log_channel.send(
            embed=embed,
            view=RaidReportView(self, RaidCase(guild.id, user.id, until, case_id)),
        )

        await send_evidence_logs(log_channel, [message], self.forbidden_store)

    # ------------------------------------------------------------------
    # Handler: normaler Raid-Verdacht
    # ------------------------------------------------------------------
    async def _handle_suspicious_user(
        self, message: discord.Message, details: dict
    ):
        user = message.author
        guild = message.guild
        log_channel = await self.db.get_log_channel(guild)
        if not log_channel:
            logger.warning(
                f"[raid] no moderation log channel configured for guild {guild.id}"
            )
            return

        entry = self._watchlist.pop(user.id, None)
        if not entry:
            return

        trigger_reason = details["reason"]
        trigger_count = details["count"]
        sum_mentions = details["sum_mentions"]
        messages_to_delete: list[discord.Message] = [
            e["msg"] for e in details["entries"]
        ]

        image_evidence: list[ImageEvidence] = []
        for e in details["entries"]:
            for (url, fn, sha1, phash) in e.get("images", []):
                match = self.forbidden_store.find_similar(
                    phash, CONFIG["image_similarity_threshold"]
                )
                image_evidence.append(
                    ImageEvidence(
                        url=url,
                        filename=fn,
                        sha1=sha1,
                        phash=phash,
                        forbidden_match=match,
                    )
                )

        until = datetime.now(timezone.utc) + timedelta(hours=CONFIG["timeout_hours"])

        try:
            await user.timeout(
                until, reason="Raid-Verdacht - zu viele Erwaehnungen/Duplikate"
            )
        except Exception as e:
            logger.warning(f"[raid] timeout failed for {user}: {e}")

        reason_text = {
            "mentions": f"{trigger_count} Erwaehnungen",
            "text": f"{trigger_count} gleiche Textnachrichten",
            "images": f"{trigger_count} aehnliche Bilder",
        }.get(trigger_reason, "unbekannt")

        await self.db.log_moderation_action(
            guild_id=str(guild.id),
            action="raid_timeout",
            target_id=str(user.id),
            target_name=str(user),
            moderator_id=str(self.bot.user.id) if self.bot.user else None,
            moderator_name=str(self.bot.user) if self.bot.user else "System",
            reason=f"Automatischer Timeout bei Raid-Verdacht ({reason_text}).",
            until=until,
        )

        await send_user_embed(
            user=user,
            title="Raid-Verdacht - Timeout verhaengt",
            description=(
                f"Deine Nachrichten wurden wegen eines Raid-Verdachts geloescht und du wurdest fuer "
                f"{CONFIG['timeout_hours']} Stunden in den Timeout versetzt.\n\n"
                "Ein Moderator prueft den Fall."
            ),
            color=discord.Color.orange(),
            fields=[
                ("Zeitraum", f"Bis {discord.utils.format_dt(until, 'F')}", False),
                ("Grund", reason_text, False),
                ("Nachrichtenanzahl", str(len(messages_to_delete)), False),
            ],
        )

        case_id = f"{guild.id}-{user.id}-{int(datetime.now(timezone.utc).timestamp())}"
        if image_evidence:
            self._case_images[case_id] = image_evidence

        is_new = bool(
            user.joined_at
            and (datetime.now(timezone.utc) - user.joined_at).days
            <= CONFIG["new_member_days"]
        )

        embed = build_report_embed(
            guild=guild,
            user=user,
            until=until,
            is_new=is_new,
            messages=messages_to_delete,
            trigger_reason=trigger_reason,
            trigger_count=trigger_count,
            sum_mentions=sum_mentions,
            case_id=case_id,
        )
        await log_channel.send(
            embed=embed,
            view=RaidReportView(self, RaidCase(guild.id, user.id, until, case_id)),
        )

        await send_evidence_logs(log_channel, messages_to_delete, self.forbidden_store)

        if messages_to_delete:
            await self._delete_messages_safely(messages_to_delete)

    async def _delete_messages_safely(self, messages: list[discord.Message]) -> None:
        by_channel: dict[int, list[discord.Message]] = {}
        for msg in messages:
            by_channel.setdefault(msg.channel.id, []).append(msg)

        for channel_messages in by_channel.values():
            channel = channel_messages[0].channel
            try:
                if isinstance(channel, discord.TextChannel) and len(channel_messages) > 1:
                    recent = [
                        msg
                        for msg in channel_messages
                        if (datetime.now(timezone.utc) - msg.created_at).days < 14
                    ]
                    if len(recent) > 1:
                        await channel.delete_messages(recent)
                        continue
            except Exception as e:
                logger.warning(f"[raid] bulk delete failed: {e}")

            for msg in channel_messages:
                try:
                    await msg.delete()
                except discord.NotFound:
                    pass
                except Exception as e:
                    logger.warning(f"[raid] message delete failed: {e}")
                await asyncio.sleep(0.2)

    # ==================================================================
    # Slash-Commands
    # ==================================================================
    raid = app_commands.Group(name="raid", description="Raid-Protection System")
    moderation = app_commands.Group(
        name="moderation",
        description="Moderator-Rollen verwalten",
        parent=raid,
    )
    allow = app_commands.Group(
        name="allow",
        description="User temporaer vom Raid-Schutz ausnehmen",
        parent=raid,
    )

    # ---------- /raid moderation ----------
    @moderation.command(name="add", description="Fuegt eine Moderator-Rolle hinzu")
    async def moderation_add(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        if not await _require_moderator(interaction):
            return
        success = await self.db.add_moderator_role(interaction.guild, role)
        msg = (
            f"{role.mention} ist jetzt Moderator-Rolle."
            if success
            else f"{role.mention} ist bereits eine Moderator-Rolle."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @moderation.command(name="remove", description="Entfernt eine Moderator-Rolle")
    async def moderation_remove(
        self, interaction: discord.Interaction, role: discord.Role
    ):
        if not await _require_moderator(interaction):
            return
        success = await self.db.remove_moderator_role(interaction.guild, role)
        msg = (
            f"{role.mention} ist keine Moderator-Rolle mehr."
            if success
            else f"{role.mention} war keine Moderator-Rolle."
        )
        await interaction.response.send_message(msg, ephemeral=True)

    @moderation.command(name="list", description="Listet alle Moderator-Rollen auf")
    async def moderation_list(self, interaction: discord.Interaction):
        if not await _require_moderator(interaction):
            return
        role_ids = await self.db.get_moderator_role_ids(interaction.guild)
        if not role_ids:
            await interaction.response.send_message(
                "Es sind keine Moderator-Rollen konfiguriert.", ephemeral=True
            )
            return
        roles = [interaction.guild.get_role(int(rid)) for rid in role_ids]
        mention_list = ", ".join(r.mention for r in roles if r) or "keine gefunden"
        await interaction.response.send_message(
            f"Moderator-Rollen: {mention_list}", ephemeral=True
        )

    # ---------- /raid allow ----------
    @allow.command(
        name="add",
        description="Nimmt einen User temporaer vom Raid-Schutz aus",
    )
    async def allow_add(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        dauer: str,
    ):
        if not await _require_moderator(interaction):
            return
        td = parse_duration(dauer)
        if td is None or td.total_seconds() <= 0:
            await interaction.response.send_message(
                "Ungueltige Dauer. Beispiele: `10m`, `1h`, `2d`, `1w`.",
                ephemeral=True,
            )
            return
        if td > timedelta(days=90):
            await interaction.response.send_message(
                "Maximale Dauer ist 90 Tage.", ephemeral=True
            )
            return

        until = datetime.now(timezone.utc) + td
        ok = await self.db.add_allowed_user(
            interaction.guild, user.id, until, interaction.user.id
        )
        if not ok:
            await interaction.response.send_message(
                "Fehler beim Speichern in der Datenbank.", ephemeral=True
            )
            return

        await self.db.log_moderation_action(
            guild_id=str(interaction.guild.id),
            action="raid_allow_add",
            target_id=str(user.id),
            target_name=str(user),
            moderator_id=str(interaction.user.id),
            moderator_name=str(interaction.user),
            reason=f"Allow-Liste: {format_duration(td)}",
            until=until,
        )

        await interaction.response.send_message(
            f"{user.mention} ist fuer **{format_duration(td)}** "
            f"vom Raid-Schutz ausgenommen (bis {discord.utils.format_dt(until, 'F')}).",
            ephemeral=True,
        )

    @allow.command(name="remove", description="Entfernt einen User von der Allow-Liste")
    async def allow_remove(
        self, interaction: discord.Interaction, user: discord.Member
    ):
        if not await _require_moderator(interaction):
            return
        ok = await self.db.remove_allowed_user(interaction.guild, user.id)
        if ok:
            await self.db.log_moderation_action(
                guild_id=str(interaction.guild.id),
                action="raid_allow_remove",
                target_id=str(user.id),
                target_name=str(user),
                moderator_id=str(interaction.user.id),
                moderator_name=str(interaction.user),
                reason="Von Allow-Liste entfernt",
            )
            await interaction.response.send_message(
                f"{user.mention} wurde von der Allow-Liste entfernt.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"{user.mention} stand nicht auf der Allow-Liste.", ephemeral=True
            )

    @allow.command(name="list", description="Zeigt alle User auf der Allow-Liste")
    async def allow_list(self, interaction: discord.Interaction):
        if not await _require_moderator(interaction):
            return
        users = await self.db.get_allowed_users(interaction.guild)
        if not users:
            await interaction.response.send_message(
                "Die Allow-Liste ist leer.", ephemeral=True
            )
            return

        lines = []
        for uid, until in sorted(users.items(), key=lambda x: x[1]):
            lines.append(
                f"<@{uid}> (`{uid}`) - bis {discord.utils.format_dt(until, 'F')}"
            )
        text = "\n".join(lines)
        if len(text) > 1900:
            text = text[:1900] + "\n..."

        embed = discord.Embed(
            title=f"Raid-Allow-Liste ({len(users)})",
            description=text,
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Extras ----------
    @raid.command(
        name="forbidden_count", description="Anzahl verbotener Bilder anzeigen"
    )
    async def raid_forbidden_count(self, interaction: discord.Interaction):
        if not await _require_moderator(interaction):
            return
        count = self.forbidden_store.size()
        await interaction.response.send_message(
            f"Es sind **{count}** verbotene Bilder gespeichert.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidProtectionCog(bot))