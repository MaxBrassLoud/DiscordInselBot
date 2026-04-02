"""
bot/features/reminders/cog.py
==============================
Erinnert Ticket- und Bewerbungs-Ersteller per DM wenn Staff eine Nachricht
geschrieben hat und der Ersteller 24 Stunden lang nicht geantwortet hat.

Logik:
  • Ein Task läuft alle 10 Minuten.
  • Für jedes offene Ticket / jede offene Bewerbung:
      1. Lade alle Nachrichten aus der DB.
      2. Finde die letzte Staff-Nachricht (user_id ≠ creator_id und user_id nicht in added_users).
      3. Prüfe ob der Ersteller DANACH noch geschrieben hat.
      4. Wenn nicht und die letzte Staff-Nachricht > 24h her ist → DM senden.
      5. Merke in `ticket_reminders`-Tabelle dass die DM gesendet wurde,
         damit sie nicht doppelt gesendet wird.
      6. Wenn der Ersteller später antwortet → Eintrag zurücksetzen.

Supabase SQL (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS ticket_reminders (
        id              BIGSERIAL PRIMARY KEY,
        entity_type     TEXT NOT NULL,          -- 'ticket' oder 'application'
        entity_id       INTEGER NOT NULL,
        server_id       TEXT NOT NULL,
        creator_id      TEXT NOT NULL,
        last_staff_msg  TIMESTAMPTZ,            -- wann hat Staff zuletzt geschrieben
        reminded_at     TIMESTAMPTZ,            -- wann wurde die DM gesendet (NULL = noch nicht)
        UNIQUE (entity_type, entity_id, server_id)
    );
    CREATE INDEX IF NOT EXISTS idx_ticket_reminders
        ON ticket_reminders (entity_type, server_id, entity_id);
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("reminders")

REMINDER_AFTER_HOURS = 24
CHECK_INTERVAL_MINUTES = 30
WEB_BASE_URL = os.getenv("WEB_BASE_URL", "http://localhost:5000")


# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: str | None) -> datetime | None:
    """Parse ISO timestamp string to aware datetime. Returns None on failure."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_staff_message(msg: dict, creator_id: str, added_users: list[str]) -> bool:
    """
    Eine Nachricht gilt als Staff-Nachricht wenn:
      - user_id ist gesetzt (kein Bot/System)
      - user_id ist nicht der Ersteller
      - user_id ist nicht in den hinzugefügten Usern
    """
    uid = msg.get("user_id", "")
    if not uid:
        return False
    if uid == creator_id:
        return False
    if uid in added_users:
        return False
    return True


def _analyze_messages(
    messages: list[dict],
    creator_id: str,
    added_users: list[str],
) -> tuple[datetime | None, bool]:
    """
    Analysiert die Nachrichtenliste und gibt zurück:
      (last_staff_msg_dt, creator_responded_after)

    last_staff_msg_dt:       Zeitstempel der letzten Staff-Nachricht (oder None)
    creator_responded_after: True wenn der Ersteller NACH der letzten Staff-Nachricht
                             noch geschrieben hat
    """
    last_staff_dt: datetime | None = None
    creator_after_staff = False

    for msg in messages:
        if msg.get("is_deleted"):
            continue
        ts = _parse_dt(msg.get("timestamp"))
        if ts is None:
            continue
        uid = msg.get("user_id", "")
        if _is_staff_message(msg, creator_id, added_users):
            if last_staff_dt is None or ts > last_staff_dt:
                last_staff_dt = ts
                creator_after_staff = False  # reset: staff wrote after last creator msg
        elif uid == creator_id:
            if last_staff_dt is not None and ts > last_staff_dt:
                creator_after_staff = True

    return last_staff_dt, creator_after_staff


def _get_reminder_row(entity_type: str, entity_id: int, server_id: str) -> dict | None:
    try:
        r = get_supabase().table("ticket_reminders") \
            .select("*") \
            .eq("entity_type", entity_type) \
            .eq("entity_id", entity_id) \
            .eq("server_id", server_id) \
            .execute()
        return r.data[0] if r.data else None
    except Exception as e:
        logger.error(f"[reminders] _get_reminder_row: {e}")
        return None


def _upsert_reminder(
    entity_type: str,
    entity_id: int,
    server_id: str,
    creator_id: str,
    last_staff_msg: datetime | None,
    reminded_at: datetime | None,
):
    try:
        sb = get_supabase()
        existing = _get_reminder_row(entity_type, entity_id, server_id)
        data = {
            "entity_type":    entity_type,
            "entity_id":      entity_id,
            "server_id":      server_id,
            "creator_id":     creator_id,
            "last_staff_msg": last_staff_msg.isoformat() if last_staff_msg else None,
            "reminded_at":    reminded_at.isoformat() if reminded_at else None,
        }
        if existing:
            sb.table("ticket_reminders").update(data) \
                .eq("entity_type", entity_type) \
                .eq("entity_id", entity_id) \
                .eq("server_id", server_id) \
                .execute()
        else:
            sb.table("ticket_reminders").insert(data).execute()
    except Exception as e:
        logger.error(f"[reminders] _upsert_reminder: {e}")


def _delete_reminder(entity_type: str, entity_id: int, server_id: str):
    try:
        get_supabase().table("ticket_reminders") \
            .delete() \
            .eq("entity_type", entity_type) \
            .eq("entity_id", entity_id) \
            .eq("server_id", server_id) \
            .execute()
    except Exception as e:
        logger.error(f"[reminders] _delete_reminder: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# DM BUILDER
# ══════════════════════════════════════════════════════════════════════════════

def _build_ticket_dm(
    guild_name: str,
    ticket_id: int,
    server_id: str,
    channel_id: str | None,
) -> discord.Embed:
    channel_hint = f"<#{channel_id}>" if channel_id else "deinem Ticket-Kanal"
    dashboard_url = f"{WEB_BASE_URL}/dashboard/tickets/{ticket_id}?server_id={server_id}"

    embed = discord.Embed(
        title="💬 Neue Nachricht in deinem Ticket",
        description=(
            f"Das Server-Team von **{guild_name}** hat in deinem Ticket "
            f"eine neue Nachricht hinterlassen und wartet auf deine Antwort. 😊\n\n"
            f"Du kannst direkt über den Kanal {channel_hint} antworten."
        ),
        color=discord.Color.blurple(),
        timestamp=_now(),
    )
    embed.add_field(
        name="🎫 Ticket",
        value=f"#{ticket_id}",
        inline=True,
    )
    embed.add_field(
        name="🌐 Dashboard",
        value=f"[Ticket öffnen]({dashboard_url})",
        inline=True,
    )
    embed.set_footer(text="Du erhältst diese Nachricht nur einmal pro Ticket-Antwort.")
    return embed


def _build_application_dm(
    guild_name: str,
    app_id: int,
    server_id: str,
    channel_id: str | None,
) -> discord.Embed:
    channel_hint = f"<#{channel_id}>" if channel_id else "deinem Bewerbungskanal"
    dashboard_url = f"{WEB_BASE_URL}/dashboard/applications/{app_id}?server_id={server_id}"

    embed = discord.Embed(
        title="💬 Neue Nachricht in deiner Bewerbung",
        description=(
            f"Das Server-Team von **{guild_name}** hat in deiner Bewerbung "
            f"eine neue Nachricht hinterlassen und wartet auf deine Antwort. 😊\n\n"
            f"Du kannst direkt über den Kanal {channel_hint} antworten."
        ),
        color=discord.Color.green(),
        timestamp=_now(),
    )
    embed.add_field(
        name="📋 Bewerbung",
        value=f"#{app_id}",
        inline=True,
    )
    embed.add_field(
        name="🌐 Dashboard",
        value=f"[Bewerbung öffnen]({dashboard_url})",
        inline=True,
    )
    embed.set_footer(text="Du erhältst diese Nachricht nur einmal pro Antwort des Teams.")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class ReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.reminder_task.start()

    def cog_unload(self):
        self.reminder_task.cancel()

    # ── Task ──────────────────────────────────────────────────────────────────

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def reminder_task(self):
        try:
            await self._check_tickets()
        except Exception as e:
            logger.error(f"[reminders] _check_tickets: {e}")
        try:
            await self._check_applications()
        except Exception as e:
            logger.error(f"[reminders] _check_applications: {e}")

    @reminder_task.before_loop
    async def before_reminder_task(self):
        await self.bot.wait_until_ready()

    # ── Tickets ───────────────────────────────────────────────────────────────

    async def _check_tickets(self):
        sb = get_supabase()
        tickets = sb.table("tickets") \
            .select("ticket_id, server_id, creator_id, channel_id, added_users, status") \
            .eq("status", "open") \
            .execute().data or []

        for ticket in tickets:
            try:
                await self._process_ticket(ticket)
            except Exception as e:
                logger.error(
                    f"[reminders] ticket #{ticket.get('ticket_id')}: {e}"
                )

    async def _process_ticket(self, ticket: dict):
        from bot.features.tickets.storage import load_messages

        ticket_id  = ticket["ticket_id"]
        server_id  = ticket["server_id"]
        creator_id = ticket.get("creator_id", "")
        channel_id = ticket.get("channel_id")
        added_users = ticket.get("added_users") or []

        if not creator_id:
            return

        messages = load_messages(server_id, ticket_id)
        last_staff_dt, creator_responded = _analyze_messages(
            messages, creator_id, added_users
        )

        if last_staff_dt is None:
            # No staff message at all → clean up any stale reminder row
            _delete_reminder("ticket", ticket_id, server_id)
            return

        row = _get_reminder_row("ticket", ticket_id, server_id)

        if creator_responded:
            # Creator replied → reset so we can remind again next time staff writes
            if row and row.get("reminded_at"):
                _upsert_reminder(
                    "ticket", ticket_id, server_id, creator_id,
                    last_staff_msg=last_staff_dt,
                    reminded_at=None,
                )
            elif not row:
                # Nothing to do yet
                pass
            else:
                # Update last_staff_msg in case it changed
                _upsert_reminder(
                    "ticket", ticket_id, server_id, creator_id,
                    last_staff_msg=last_staff_dt,
                    reminded_at=None,
                )
            return

        # Creator has NOT replied since last staff message
        already_reminded_for_this_staff_msg = (
            row is not None
            and row.get("reminded_at") is not None
            and _parse_dt(row.get("last_staff_msg")) == last_staff_dt
        )
        if already_reminded_for_this_staff_msg:
            return

        # Check if 24 hours have passed
        if (_now() - last_staff_dt) < timedelta(hours=REMINDER_AFTER_HOURS):
            # Not yet – but store the staff message timestamp so we can track it
            _upsert_reminder(
                "ticket", ticket_id, server_id, creator_id,
                last_staff_msg=last_staff_dt,
                reminded_at=None,
            )
            return

        # Time to send the DM
        sent = await self._send_ticket_dm(creator_id, server_id, ticket_id, channel_id)
        if sent:
            _upsert_reminder(
                "ticket", ticket_id, server_id, creator_id,
                last_staff_msg=last_staff_dt,
                reminded_at=_now(),
            )

    async def _send_ticket_dm(
        self,
        creator_id: str,
        server_id: str,
        ticket_id: int,
        channel_id: str | None,
    ) -> bool:
        guild = self.bot.get_guild(int(server_id))
        if not guild:
            return False
        member = guild.get_member(int(creator_id))
        if not member:
            return False

        embed = _build_ticket_dm(guild.name, ticket_id, server_id, channel_id)
        try:
            await member.send(embed=embed)
            logger.info(
                f"[reminders] Ticket #{ticket_id} – DM gesendet an {member} ({creator_id})"
            )
            return True
        except discord.Forbidden:
            logger.debug(
                f"[reminders] Ticket #{ticket_id} – DM fehlgeschlagen (Forbidden): {creator_id}"
            )
            return False
        except Exception as e:
            logger.error(f"[reminders] Ticket #{ticket_id} – DM Fehler: {e}")
            return False

    # ── Applications ──────────────────────────────────────────────────────────

    async def _check_applications(self):
        sb = get_supabase()
        apps = sb.table("applications") \
            .select("app_id, server_id, creator_id, channel_id, status") \
            .eq("status", "open") \
            .execute().data or []

        for app in apps:
            try:
                await self._process_application(app)
            except Exception as e:
                logger.error(
                    f"[reminders] application #{app.get('app_id')}: {e}"
                )

    async def _process_application(self, app: dict):
        from bot.features.applications.manager import load_app_messages

        app_id     = app["app_id"]
        server_id  = app["server_id"]
        creator_id = app.get("creator_id", "")
        channel_id = app.get("channel_id")

        if not creator_id:
            return

        # Applications haben keine added_users – nur Ersteller vs. alle anderen
        messages = load_app_messages(server_id, app_id)
        last_staff_dt, creator_responded = _analyze_messages(
            messages, creator_id, []  # No added_users for applications
        )

        if last_staff_dt is None:
            _delete_reminder("application", app_id, server_id)
            return

        row = _get_reminder_row("application", app_id, server_id)

        if creator_responded:
            _upsert_reminder(
                "application", app_id, server_id, creator_id,
                last_staff_msg=last_staff_dt,
                reminded_at=None,
            )
            return

        already_reminded_for_this_staff_msg = (
            row is not None
            and row.get("reminded_at") is not None
            and _parse_dt(row.get("last_staff_msg")) == last_staff_dt
        )
        if already_reminded_for_this_staff_msg:
            return

        if (_now() - last_staff_dt) < timedelta(hours=REMINDER_AFTER_HOURS):
            _upsert_reminder(
                "application", app_id, server_id, creator_id,
                last_staff_msg=last_staff_dt,
                reminded_at=None,
            )
            return

        sent = await self._send_application_dm(creator_id, server_id, app_id, channel_id)
        if sent:
            _upsert_reminder(
                "application", app_id, server_id, creator_id,
                last_staff_msg=last_staff_dt,
                reminded_at=_now(),
            )

    async def _send_application_dm(
        self,
        creator_id: str,
        server_id: str,
        app_id: int,
        channel_id: str | None,
    ) -> bool:
        guild = self.bot.get_guild(int(server_id))
        if not guild:
            return False
        member = guild.get_member(int(creator_id))
        if not member:
            return False

        embed = _build_application_dm(guild.name, app_id, server_id, channel_id)
        try:
            await member.send(embed=embed)
            logger.info(
                f"[reminders] Bewerbung #{app_id} – DM gesendet an {member} ({creator_id})"
            )
            return True
        except discord.Forbidden:
            logger.debug(
                f"[reminders] Bewerbung #{app_id} – DM fehlgeschlagen (Forbidden): {creator_id}"
            )
            return False
        except Exception as e:
            logger.error(f"[reminders] Bewerbung #{app_id} – DM Fehler: {e}")
            return False


async def setup(bot: commands.Bot):
    await bot.add_cog(ReminderCog(bot))