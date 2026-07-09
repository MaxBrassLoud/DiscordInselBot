"""Raid protection based on total mentions per short time window."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

<<<<<<< Updated upstream
# ===== DEBUG-MODUS =====
DEBUG = False  # Auf True setzen für ausführliche Konsolenausgaben
=======
logger = get_logger("raid_protection")
>>>>>>> Stashed changes


def _is_mbl(user_id: int) -> bool:
    mbl_ids = {uid.strip() for uid in os.getenv("MBL", "").split(",") if uid.strip()}
    return str(user_id) in mbl_ids

CONFIG = {
    "total_mention_threshold": 6,
    "message_time_window": 60,
    "new_member_days": 2,
    "timeout_hours": 24,
    "log_channel_override": None,
}

RAID_ACTIONS = {
    "approve": {
        "label": "Nachrichten waren zulaessig",
        "confirm_label": "Ja, Timeout aufheben",
        "style": discord.ButtonStyle.success,
        "color": discord.Color.green(),
        "done": "Fall abgeschlossen: Timeout aufgehoben.",
        "feedback": "Timeout aufgehoben.",
        "dm_title": "Entschuldigung - Timeout aufgehoben",
        "dm_description": "Deine Nachrichten wurden von einem Moderator als zulaessig eingestuft. Der Timeout wurde aufgehoben.",
        "required_perm": "moderate_members",
    },
    "warn_release": {
        "label": "Unzulaessig - Timeout aufheben",
        "confirm_label": "Ja, verwarnen",
        "style": discord.ButtonStyle.primary,
        "color": discord.Color.gold(),
        "done": "Fall abgeschlossen: Verwarnung, Timeout aufgehoben.",
        "feedback": "Verwarnung gesetzt und Timeout aufgehoben.",
        "dm_title": "Verwarnung - Timeout aufgehoben",
        "dm_description": "Deine Nachrichten wurden als unzulaessig eingestuft. Du erhaeltst eine Verwarnung, der Timeout wurde aufgehoben.",
        "required_perm": "moderate_members",
    },
    "keep_timeout": {
        "label": "Unzulaessig - Timeout behalten",
        "confirm_label": "Ja, Timeout behalten",
        "style": discord.ButtonStyle.secondary,
        "color": discord.Color.red(),
        "done": "Fall abgeschlossen: Timeout bleibt bestehen.",
        "feedback": "Timeout bleibt bestehen.",
        "dm_title": "Timeout bleibt bestehen",
        "dm_description": "Deine Nachrichten wurden als unzulaessig eingestuft. Der Timeout bleibt bestehen.",
        "required_perm": "moderate_members",
    },
    "ban": {
        "label": "Unzulaessig - User bannen",
        "confirm_label": "Ja, User bannen",
        "style": discord.ButtonStyle.danger,
        "color": discord.Color.dark_red(),
        "done": "Fall abgeschlossen: User gebannt.",
        "feedback": "User gebannt.",
        "dm_title": "Du wurdest gebannt",
        "dm_description": "Deine Nachrichten wurden als schwerwiegend unzulaessig eingestuft. Du wurdest vom Server verbannt.",
        "required_perm": "ban_members",
    },
}


def _run_db(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)


@dataclass
class RaidCase:
    guild_id: int
    user_id: int
    until: Optional[datetime] = None


class RaidProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watchlist: dict[int, dict[str, Any]] = {}
        self._ignored_cache: dict[str, dict[str, Any]] = {}
        self._persistent_registered = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._persistent_registered:
            return
        self.bot.add_view(RaidReportView(self))
        self._persistent_registered = True
        logger.info("RaidReportView registered as persistent view")

    async def _get_ignored_role_ids(self, guild: discord.Guild) -> set[str]:
        server_id = str(guild.id)
        now = datetime.now().timestamp()
        cache = self._ignored_cache.get(server_id)
        if cache and (now - cache["timestamp"]) < 60:
            return cache["roles"]

        def query() -> set[str]:
            response = (
                get_supabase()
                .table("raid_ignored_roles")
                .select("role_id")
                .eq("server_id", server_id)
                .execute()
            )
            return {row["role_id"] for row in response.data} if response.data else set()

        try:
            role_ids = await _run_db(query)
            self._ignored_cache[server_id] = {"roles": role_ids, "timestamp": now}
            return role_ids
        except Exception as e:
            logger.warning(f"[raid] ignored roles fetch failed: {e}")
            return set()

    async def _add_ignored_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)

        def query() -> bool:
            sb = get_supabase()
            check = (
                sb.table("raid_ignored_roles")
                .select("role_id")
                .eq("server_id", server_id)
                .eq("role_id", role_id)
                .execute()
            )
            if check.data:
                return False
            sb.table("raid_ignored_roles").insert({"server_id": server_id, "role_id": role_id}).execute()
            return True

        try:
            success = await _run_db(query)
            self._ignored_cache.pop(server_id, None)
            return success
        except Exception as e:
            logger.warning(f"[raid] ignored role insert failed: {e}")
            return False

    async def _remove_ignored_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)

        def query() -> None:
            (
                get_supabase()
                .table("raid_ignored_roles")
                .delete()
                .eq("server_id", server_id)
                .eq("role_id", role_id)
                .execute()
            )

        try:
            await _run_db(query)
            self._ignored_cache.pop(server_id, None)
            return True
        except Exception as e:
            logger.warning(f"[raid] ignored role delete failed: {e}")
            return False

    async def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if CONFIG["log_channel_override"]:
            channel = guild.get_channel(CONFIG["log_channel_override"])
            if isinstance(channel, discord.TextChannel):
                return channel

        def query() -> Optional[str]:
            response = (
                get_supabase()
                .table("settings")
                .select("moderation_log_channel_id")
                .eq("guild_id", str(guild.id))
                .execute()
            )
            if response.data:
                return response.data[0].get("moderation_log_channel_id")
            return None

        try:
            channel_id = await _run_db(query)
            if channel_id:
                channel = guild.get_channel(int(channel_id))
                if isinstance(channel, discord.TextChannel):
                    return channel
        except Exception as e:
            logger.warning(f"[raid] log channel fetch failed: {e}")
        return None

    async def _log_moderation_action(
        self,
        guild_id: str,
        action: str,
        target_id: str,
        target_name: str,
        moderator_id: Optional[str],
        moderator_name: Optional[str],
        reason: str,
        until: Optional[datetime] = None,
    ) -> None:
        def query() -> None:
            get_supabase().table("moderation_logs").insert(
                {
                    "server_id": guild_id,
                    "action": action,
                    "target_id": target_id,
                    "target_name": target_name,
                    "moderator_id": moderator_id,
                    "moderator_name": moderator_name,
                    "reason": reason,
                    "until": until.isoformat() if until else None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()

        try:
            await _run_db(query)
        except Exception as e:
            logger.warning(f"[raid] moderation log insert failed: {e}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        if not isinstance(message.author, discord.Member):
            return
        if message.author.is_timed_out():
            return

        if await self._is_suspicious(message):
            await self._handle_suspicious_user(message)

    async def _is_suspicious(self, message: discord.Message) -> bool:
        ignored_roles = await self._get_ignored_role_ids(message.guild)
        if any(str(role.id) in ignored_roles for role in message.author.roles):
            return False

        user_id = message.author.id
        now = datetime.now(timezone.utc)
        mention_count = len(message.mentions) + len(message.role_mentions)
        entry = self._watchlist.get(user_id)
        if entry is None:
            self._watchlist[user_id] = {
                "guild_id": message.guild.id,
                "messages": [message],
                "sum_mentions": mention_count,
                "first_seen": now,
            }
            return False

        cutoff = now - timedelta(seconds=CONFIG["message_time_window"])
        entry["messages"] = [m for m in entry["messages"] if m.created_at >= cutoff]
        entry["messages"].append(message)
        entry["sum_mentions"] = sum(len(m.mentions) + len(m.role_mentions) for m in entry["messages"])
        entry["first_seen"] = min(entry.get("first_seen", now), now)
        return entry["sum_mentions"] >= CONFIG["total_mention_threshold"]

    async def _send_user_embed(
        self,
        user: discord.abc.Messageable,
        title: str,
        description: str,
        color: discord.Color,
        fields: list[tuple[str, str, bool]] | None = None,
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

    async def _handle_suspicious_user(self, message: discord.Message):
        user = message.author
        guild = message.guild
        log_channel = await self._get_log_channel(guild)
        if not log_channel:
            logger.warning(f"[raid] no moderation log channel configured for guild {guild.id}")
            return

        entry = self._watchlist.pop(user.id, None)
        if not entry:
            return

        messages_to_delete: list[discord.Message] = entry.get("messages", [])
        sum_mentions = int(entry.get("sum_mentions", 0))
        until = datetime.now(timezone.utc) + timedelta(hours=CONFIG["timeout_hours"])

        try:
            await user.timeout(until, reason="Raid-Verdacht - zu viele Erwaehnungen")
        except Exception as e:
            logger.warning(f"[raid] timeout failed for {user}: {e}")

        await self._log_moderation_action(
            guild_id=str(guild.id),
            action="raid_timeout",
            target_id=str(user.id),
            target_name=str(user),
            moderator_id=str(self.bot.user.id) if self.bot.user else None,
            moderator_name=str(self.bot.user) if self.bot.user else "System",
            reason=f"Automatischer Timeout bei Raid-Verdacht ({sum_mentions} Erwaehnungen).",
            until=until,
        )

        await self._send_user_embed(
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
                ("Grund", f"{sum_mentions} Erwaehnungen in {len(messages_to_delete)} Nachrichten", False),
            ],
        )

        is_new = bool(user.joined_at and (datetime.now(timezone.utc) - user.joined_at).days <= CONFIG["new_member_days"])
        embed = self._build_report_embed(guild, user, until, is_new, messages_to_delete, sum_mentions)
        await log_channel.send(embed=embed, view=RaidReportView(self, RaidCase(guild.id, user.id, until)))
        await self._send_evidence_logs(log_channel, messages_to_delete)

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

    def _build_report_embed(
        self,
        guild: discord.Guild,
        user: discord.Member,
        until: datetime,
        is_new: bool,
        messages: list[discord.Message],
        sum_mentions: int,
    ) -> discord.Embed:
        joined_text = discord.utils.format_dt(user.joined_at, "F") if user.joined_at else "Unbekannt"
        case_id = f"{guild.id}-{user.id}-{int(datetime.now(timezone.utc).timestamp())}"
        attachment_count = sum(len(msg.attachments) for msg in messages)
        channel_count = len({msg.channel.id for msg in messages})
        embed = discord.Embed(
            title="Raid-Protection | Moderationsfall",
            description=(
                "Automatischer Schutz hat eine Mention-Spitze erkannt, den User temporaer gestoppt "
                "und die Beweise unten im Log gesichert."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="User", value=f"{user.mention}\n`{user}`\n`{user.id}`", inline=True)
        embed.add_field(name="Timeout", value=f"Bis {discord.utils.format_dt(until, 'F')}\n{discord.utils.format_dt(until, 'R')}", inline=True)
        embed.add_field(name="Risikoprofil", value="Neuer User (< 2 Tage)" if is_new else "Bestehender User", inline=True)
        embed.add_field(
            name="Ausloeser",
            value=(
                f"**{sum_mentions}** Erwaehnungen in **{len(messages)}** Nachrichten\n"
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
        embed.add_field(name="Fall-Daten", value=f"Guild-ID: `{guild.id}`\nUser-ID: `{user.id}`", inline=False)
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.set_footer(text=f"Fall-ID: {case_id} | Bis: {until.isoformat()}")
        return embed

    async def _send_evidence_logs(
            self,
            log_channel: discord.TextChannel,
            messages: list[discord.Message],
    ) -> None:
        """Sendet alle Evidence-Nachrichten gebündelt in einem einzigen Embed."""
        if not messages:
            embed = discord.Embed(
                title="Evidence | Keine Nachrichten gefunden",
                description="Im Watchlist-Eintrag waren keine Nachrichten mehr vorhanden.",
                color=discord.Color.dark_gray(),
                timestamp=datetime.now(timezone.utc),
            )
            await log_channel.send(embed=embed)
            return

        total = len(messages)
        embed = discord.Embed(
            title=f"Evidence – Alle verdächtigen Nachrichten ({total} Stück)",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )

        # Zusammenfassungen pro Nachricht erzeugen
        message_summaries = []
        for idx, msg in enumerate(messages, 1):
            content = msg.content.strip() if msg.content else "*Keine Textnachricht*"
            if len(content) > 500:
                content = content[:497] + "..."

            mention_count = len(msg.mentions) + len(msg.role_mentions)
            channel_mention = msg.channel.mention
            timestamp_str = discord.utils.format_dt(msg.created_at, "R")

            # Anhänge als Links (nach Löschung der Originalnachricht nicht mehr gültig)
            attachments_info = ""
            if msg.attachments:
                attach_links = [f"[{a.filename}]({a.url})" for a in msg.attachments[:5]]
                attachments_info = f"\nAnhänge: {', '.join(attach_links)}"
                if len(msg.attachments) > 5:
                    attachments_info += f" (+{len(msg.attachments) - 5} weitere)"

            summary = (
                f"**{idx}.** {channel_mention} • {timestamp_str} • {mention_count} Erwähnungen\n"
                f"{content}{attachments_info}"
            )
            message_summaries.append(summary)

        # Embed‑Felder füllen – maximal 25 Felder
        MAX_FIELDS = 25
        if total <= MAX_FIELDS:
            for summary in message_summaries:
                embed.add_field(name="\u200b", value=summary, inline=False)
        else:
            # Mehrere Zusammenfassungen in einem Feld bündeln (max. 1024 Zeichen pro Feld)
            fields = []
            current_field = ""
            for summary in message_summaries:
                if len(current_field) + len(summary) + 2 > 1024:
                    fields.append(current_field)
                    current_field = summary
                else:
                    if current_field:
                        current_field += "\n---\n"
                    current_field += summary
            if current_field:
                fields.append(current_field)

            for i, field_content in enumerate(fields[:MAX_FIELDS]):
                embed.add_field(name=f"Nachrichtenblock {i + 1}", value=field_content, inline=False)

            if len(fields) > MAX_FIELDS:
                embed.add_field(
                    name="Weitere Nachrichten",
                    value=f"{len(fields) - MAX_FIELDS} weitere Blöcke nicht angezeigt.",
                    inline=False,
                )

        # Fußzeile mit Gesamtstatistik
        total_mentions = sum(len(m.mentions) + len(m.role_mentions) for m in messages)
        embed.set_footer(text=f"Gesamte Erwähnungen: {total_mentions} | Nachrichten: {total}")

        await log_channel.send(embed=embed)

    async def resolve_case(self, interaction: discord.Interaction, fallback: Optional[RaidCase]) -> Optional[RaidCase]:
        if fallback:
            return fallback
        if not interaction.message or not interaction.message.embeds:
            return None

        embed = interaction.message.embeds[0]
        guild_id = interaction.guild_id
        user_id = None
        until = None

        footer = embed.footer.text or ""
        match = re.search(r"Fall-ID:\s*(\d+)-(\d+)-\d+", footer)
        if match:
            guild_id = int(match.group(1))
            user_id = int(match.group(2))

        if user_id is None:
            combined = f"{embed.description or ''}\n" + "\n".join(field.value for field in embed.fields)
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
            return RaidCase(int(guild_id), int(user_id), until)
        return None

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

        member = guild.get_member(case.user_id)
        user: discord.User | discord.Member | None = member or self.bot.get_user(case.user_id)
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
                await member.timeout(None, reason=f"Raid-Fall durch {interaction.user} bewertet")
            elif action == "ban":
                if member:
                    await member.ban(reason=f"Raid-Fall durch {interaction.user} bestaetigt")
                elif user:
                    await guild.ban(user, reason=f"Raid-Fall durch {interaction.user} bestaetigt")
        except discord.Forbidden:
            await interaction.followup.send("Dem Bot fehlen die passenden Rechte fuer diese Aktion.", ephemeral=True)
            return
        except Exception as e:
            await interaction.followup.send(f"Fehler bei der Aktion: {e}", ephemeral=True)
            return

        if user:
            fields = []
            if action == "keep_timeout" and case.until:
                fields.append(("Dauer", f"Bis {discord.utils.format_dt(case.until, 'F')}", False))
            await self._send_user_embed(
                user=user,
                title=meta["dm_title"],
                description=meta["dm_description"],
                color=meta["color"],
                fields=fields,
            )

        await self._log_moderation_action(
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
            embed.color = meta["color"]
            if meta["done"] not in (embed.description or ""):
                embed.description = f"{embed.description or ''}\n\n**{meta['done']}**\nBewertet von {interaction.user.mention}."
            view = RaidReportView(self, case, disabled=True)
            try:
                await message_to_edit.edit(embed=embed, view=view)
            except Exception as e:
                logger.warning(f"[raid] report message edit failed: {e}")

        await interaction.followup.send(meta["feedback"], ephemeral=True)

    @app_commands.command(name="raid_ignore", description="Ignoriere eine Rolle bei der Raid-Erkennung")
    @app_commands.default_permissions(administrator=True)
    async def raid_ignore(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
            return
        success = await self._add_ignored_role(interaction.guild, role)
        msg = f"{role.mention} wird ignoriert." if success else f"{role.mention} wird bereits ignoriert oder es gab einen Fehler."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="raid_unignore", description="Hebe die Ignorierung auf")
    @app_commands.default_permissions(administrator=True)
    async def raid_unignore(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
            return
        success = await self._remove_ignored_role(interaction.guild, role)
        msg = f"{role.mention} wird nun wieder ueberprueft." if success else f"{role.mention} war nicht in der Ignorierliste."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="raid_list_ignored", description="Zeige alle ignorierten Rollen")
    @app_commands.default_permissions(administrator=True)
    async def raid_list_ignored(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
            return
        ignored_ids = await self._get_ignored_role_ids(interaction.guild)
        if not ignored_ids:
            await interaction.response.send_message("Es werden keine Rollen ignoriert.", ephemeral=True)
            return
        roles = [interaction.guild.get_role(int(role_id)) for role_id in ignored_ids]
        mention_list = ", ".join(role.mention for role in roles if role) or "keine gefunden"
        await interaction.response.send_message(f"Ignorierte Rollen: {mention_list}", ephemeral=True)


class RaidConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: RaidProtectionCog,
        action: str,
        case: RaidCase,
        report_message: Optional[discord.Message],
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.action = action
        self.case = case
        self.report_message = report_message

        confirm = discord.ui.Button(
            label=RAID_ACTIONS[action]["confirm_label"],
            style=RAID_ACTIONS[action]["style"],
            custom_id=f"raid_confirm:{action}",
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(
            label="Abbrechen",
            style=discord.ButtonStyle.secondary,
            custom_id=f"raid_cancel:{action}",
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        await self.cog.execute_report_action(interaction, self.action, self.case, self.report_message)

    async def _cancel(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Abgebrochen.", view=self)


class RaidReportView(discord.ui.View):
    def __init__(self, cog: RaidProtectionCog, case: Optional[RaidCase] = None, disabled: bool = False):
        super().__init__(timeout=None)
        self.cog = cog
        self.case = case
        for action, meta in RAID_ACTIONS.items():
            button = discord.ui.Button(
                label=meta["label"],
                style=meta["style"],
                custom_id=f"raid_report:{action}",
                disabled=disabled,
            )
            button.callback = self._make_callback(action)
            self.add_item(button)

    def _make_callback(self, action: str):
        async def callback(interaction: discord.Interaction):
            if not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
                return

            required_perm = RAID_ACTIONS[action]["required_perm"]
            permissions = interaction.user.guild_permissions
            if not _is_mbl(interaction.user.id) and not permissions.administrator and not getattr(permissions, required_perm):
                await interaction.response.send_message("Keine Berechtigung.", ephemeral=True)
                return

            case = await self.cog.resolve_case(interaction, self.case)
            if not case:
                await interaction.response.send_message("Fall konnte nicht mehr gelesen werden.", ephemeral=True)
                return

            user_label = f"<@{case.user_id}>"
            meta = RAID_ACTIONS[action]
            await interaction.response.send_message(
                f"Bestaetigen: **{meta['label']}** fuer {user_label}?",
                view=RaidConfirmView(self.cog, action, case, interaction.message),
                ephemeral=True,
            )

        return callback


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidProtectionCog(bot))
