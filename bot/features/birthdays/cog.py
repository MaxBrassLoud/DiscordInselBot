"""
bot/features/birthdays/cog.py
==============================
Geburtstags-System:
  - /geburtstag eintragen  – Geburtstag speichern (dd.mm oder dd.mm.yyyy)
  - /geburtstag anzeigen   – Eigenen oder fremden Geburtstag anzeigen
  - /geburtstag löschen    – Eigenen Eintrag entfernen
  - /geburtstag liste      – Alle eingetragenen Geburtstage des Servers
  - Täglicher Check um 08:00 → Glückwunschnachricht im konfigurierten Kanal

Setup:
  - Env-Variable BIRTHDAY_CHANNEL_ID=<channel_id> setzen,
    oder /geburtstag setup @channel als Admin nutzen.

Supabase (einmalig):
    CREATE TABLE IF NOT EXISTS birthdays (
        user_id    TEXT NOT NULL,
        server_id  TEXT NOT NULL,
        birthday   DATE NOT NULL,
        PRIMARY KEY (user_id, server_id)
    );
    -- Kanal-Konfiguration wird in der settings-Tabelle gespeichert
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timezone, timedelta, date

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("birthdays")

BIRTHDAY_MESSAGES = [
    "Alles Gute zum Geburtstag, {mention}! 🥳🎉 Wir wünschen dir einen wunderschönen Tag!",
    "Happy Birthday, {mention}! 🎂 Möge dein Tag so besonders sein wie du!",
    "Ein Hoch auf {mention}! 🎈 Herzlichen Glückwunsch zum Geburtstag!",
    "🎉 {mention} hat heute Geburtstag! Wir wünschen dir alles Gute und ganz viel Spaß! 🎂",
    "Der heutige Tag gehört dir, {mention}! 🥳 Herzlichen Glückwunsch zum Geburtstag!",
    "🎊 Happy Birthday {mention}! Genieß deinen Tag in vollen Zügen! 🎁",
    "Heute ist ein besonderer Tag – {mention} hat Geburtstag! 🎂 Alles Liebe und Gute!",
    "Prost auf {mention}! 🥂 Happy Birthday – mögen deine Wünsche in Erfüllung gehen!",
    "🎈 {mention} wird heute ein Jahr älter und weiser! Herzlichen Glückwunsch! 🎉",
    "Auf ein weiteres fantastisches Jahr, {mention}! 🎂 Alles Gute zum Geburtstag!",
]

# Fallback wenn keine Env-Variable und kein DB-Eintrag
_FALLBACK_CHANNEL_ENV = os.getenv("BIRTHDAY_CHANNEL_ID", "")


def _today() -> date:
    return datetime.now(timezone(timedelta(hours=1))).date()


def _parse_birthday(raw: str) -> date | None:
    """
    Akzeptiert: dd.mm  oder  dd.mm.yyyy
    Gibt ein date-Objekt zurück (Jahr wird ggf. auf 2000 gesetzt).
    """
    raw = raw.strip()
    parts = raw.split(".")
    if len(parts) == 2:
        try:
            day, month = int(parts[0]), int(parts[1])
            return date(2000, month, day)
        except (ValueError, OverflowError):
            return None
    elif len(parts) == 3:
        try:
            day, month, year = int(parts[0]), int(parts[1]), int(parts[2])
            return date(year, month, day)
        except (ValueError, OverflowError):
            return None
    return None


def _format_date(d: date) -> str:
    if d.year == 2000:
        return f"{d.day:02d}.{d.month:02d}."
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def _get_birthday_channel_id(server_id: str) -> str | None:
    """Liest den Kanal aus der settings-Tabelle oder der Env-Variable."""
    try:
        sb = get_supabase()
        r  = sb.table("settings").select("birthday_channel_id")\
               .eq("guild_id", server_id).execute()
        if r.data and r.data[0].get("birthday_channel_id"):
            return str(r.data[0]["birthday_channel_id"])
    except Exception:
        pass
    return _FALLBACK_CHANNEL_ENV or None


def _set_birthday_channel_id(server_id: str, channel_id: str):
    sb = get_supabase()
    existing = sb.table("settings").select("id").eq("guild_id", server_id).execute()
    if existing.data:
        sb.table("settings").update({"birthday_channel_id": channel_id})\
          .eq("guild_id", server_id).execute()
    else:
        sb.table("settings").insert({
            "guild_id": server_id,
            "birthday_channel_id": channel_id,
        }).execute()


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class BirthdaysCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._wished_today: set[str] = set()  # "server_id:user_id" für heute
        self.birthday_check.start()

    def cog_unload(self):
        self.birthday_check.cancel()

    # ── Täglicher Check ───────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def birthday_check(self):
        now = datetime.now(timezone(timedelta(hours=1)))
        # Nur um 08:00 Uhr (±30s)
        if now.hour != 8 or now.minute != 0:
            return

        today = _today()
        try:
            sb = get_supabase()
            r  = sb.table("birthdays")\
                   .select("user_id, server_id, birthday")\
                   .execute()
            rows = r.data or []
        except Exception as e:
            logger.error(f"[birthdays] DB-Fehler beim täglichen Check: {e}")
            return

        for row in rows:
            server_id = row["server_id"]
            user_id   = row["user_id"]
            key       = f"{server_id}:{user_id}"

            try:
                bday = date.fromisoformat(row["birthday"])
            except Exception:
                continue

            if bday.month != today.month or bday.day != today.day:
                continue
            if key in self._wished_today:
                continue

            self._wished_today.add(key)
            await self._send_birthday_wish(server_id, user_id, bday, today)

        # Cache um Mitternacht leeren
        if now.hour == 0 and now.minute == 0:
            self._wished_today.clear()

    @birthday_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()

    async def _send_birthday_wish(
        self, server_id: str, user_id: str, bday: date, today: date
    ):
        channel_id = _get_birthday_channel_id(server_id)
        if not channel_id:
            logger.warning(f"[birthdays] Kein Geburtstags-Kanal für Server {server_id}")
            return

        guild   = self.bot.get_guild(int(server_id))
        channel = self.bot.get_channel(int(channel_id))
        if not guild or not channel:
            return

        member = guild.get_member(int(user_id))
        if not member:
            return

        age_str = ""
        if bday.year != 2000:
            age = today.year - bday.year
            age_str = f" ({age} Jahre 🎂)"

        msg = random.choice(BIRTHDAY_MESSAGES).format(mention=member.mention)
        embed = discord.Embed(
            description=msg + age_str,
            color=discord.Color.from_rgb(255, 182, 30),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"🎂 {_format_date(bday)}")

        try:
            await channel.send(content=member.mention, embed=embed)
            logger.info(f"[birthdays] Geburtstagswunsch gesendet: {member} auf {guild}")
        except discord.Forbidden:
            logger.warning(f"[birthdays] Kein Zugriff auf Kanal {channel_id}")
        except Exception as e:
            logger.error(f"[birthdays] Senden fehlgeschlagen: {e}")

    # ══════════════════════════════════════════════════════════════════════════
    # SLASH COMMANDS
    # ══════════════════════════════════════════════════════════════════════════

    geburtstag = app_commands.Group(
        name="geburtstag",
        description="Geburtstags-System",
    )

    # ── /geburtstag eintragen ─────────────────────────────────────────────────

    @geburtstag.command(name="eintragen", description="Trage deinen Geburtstag ein")
    @app_commands.describe(datum="Dein Geburtstag: dd.mm oder dd.mm.yyyy (z.B. 15.03 oder 15.03.2001)")
    async def cmd_eintragen(self, interaction: discord.Interaction, datum: str):
        bday = _parse_birthday(datum)
        if not bday:
            await interaction.response.send_message(
                "❌ Ungültiges Datum. Bitte nutze das Format **dd.mm** oder **dd.mm.yyyy**.\n"
                "Beispiele: `15.03` oder `15.03.2001`",
                ephemeral=True,
            )
            return

        server_id = str(interaction.guild_id)
        user_id   = str(interaction.user.id)

        try:
            sb = get_supabase()
            existing = sb.table("birthdays")\
                         .select("user_id")\
                         .eq("user_id", user_id)\
                         .eq("server_id", server_id)\
                         .execute()
            if existing.data:
                sb.table("birthdays")\
                  .update({"birthday": bday.isoformat()})\
                  .eq("user_id", user_id)\
                  .eq("server_id", server_id)\
                  .execute()
                action = "aktualisiert"
            else:
                sb.table("birthdays").insert({
                    "user_id":   user_id,
                    "server_id": server_id,
                    "birthday":  bday.isoformat(),
                }).execute()
                action = "eingetragen"
        except Exception as e:
            logger.error(f"[birthdays] Eintragen fehlgeschlagen: {e}")
            await interaction.response.send_message("❌ Fehler beim Speichern.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎂 Geburtstag eingetragen!",
            description=f"Dein Geburtstag wurde erfolgreich **{action}**.",
            color=discord.Color.green(),
        )
        embed.add_field(name="📅 Datum", value=_format_date(bday), inline=True)
        if bday.year != 2000:
            embed.add_field(name="📆 Jahr", value=str(bday.year), inline=True)
        embed.set_footer(text="Du bekommst am Morgen deines Geburtstags eine Nachricht! 🎉")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /geburtstag anzeigen ──────────────────────────────────────────────────

    @geburtstag.command(name="anzeigen", description="Zeige deinen oder einen anderen Geburtstag an")
    @app_commands.describe(mitglied="Das Mitglied dessen Geburtstag angezeigt werden soll (leer = du selbst)")
    async def cmd_anzeigen(
        self,
        interaction: discord.Interaction,
        mitglied: discord.Member | None = None,
    ):
        target = mitglied or interaction.user
        server_id = str(interaction.guild_id)

        try:
            sb = get_supabase()
            r  = sb.table("birthdays")\
                   .select("birthday")\
                   .eq("user_id", str(target.id))\
                   .eq("server_id", server_id)\
                   .execute()
        except Exception as e:
            await interaction.response.send_message("❌ Fehler beim Laden.", ephemeral=True)
            return

        if not r.data:
            if target == interaction.user:
                msg = "Du hast noch keinen Geburtstag eingetragen. Nutze `/geburtstag eintragen`!"
            else:
                msg = f"{target.mention} hat noch keinen Geburtstag eingetragen."
            await interaction.response.send_message(msg, ephemeral=True)
            return

        bday  = date.fromisoformat(r.data[0]["birthday"])
        today = _today()

        # Nächsten Geburtstag berechnen
        this_year = bday.replace(year=today.year)
        if this_year < today:
            next_bday = bday.replace(year=today.year + 1)
        else:
            next_bday = this_year
        days_until = (next_bday - today).days

        embed = discord.Embed(
            title=f"🎂 Geburtstag von {target.display_name}",
            color=discord.Color.from_rgb(255, 182, 30),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="📅 Datum", value=_format_date(bday), inline=True)
        if bday.year != 2000:
            age_this_year = today.year - bday.year
            if this_year < today:
                age_this_year += 1
            embed.add_field(name="🎈 Nächstes Alter", value=str(age_this_year), inline=True)

        if days_until == 0:
            embed.add_field(name="🎉 Heute!", value="Happy Birthday! 🥳", inline=False)
        elif days_until == 1:
            embed.add_field(name="⏳ In", value="Morgen! 🎉", inline=True)
        else:
            embed.add_field(name="⏳ In", value=f"{days_until} Tagen", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=False)

    # ── /geburtstag löschen ───────────────────────────────────────────────────

    @geburtstag.command(name="löschen", description="Lösche deinen eingetragenen Geburtstag")
    async def cmd_loeschen(self, interaction: discord.Interaction):
        server_id = str(interaction.guild_id)
        user_id   = str(interaction.user.id)

        try:
            sb = get_supabase()
            r  = sb.table("birthdays")\
                   .select("user_id")\
                   .eq("user_id", user_id)\
                   .eq("server_id", server_id)\
                   .execute()
            if not r.data:
                await interaction.response.send_message(
                    "ℹ️ Du hast keinen Geburtstag eingetragen.", ephemeral=True
                )
                return
            sb.table("birthdays")\
              .delete()\
              .eq("user_id", user_id)\
              .eq("server_id", server_id)\
              .execute()
        except Exception as e:
            await interaction.response.send_message("❌ Fehler beim Löschen.", ephemeral=True)
            return

        await interaction.response.send_message(
            "✅ Dein Geburtstag wurde erfolgreich gelöscht.", ephemeral=True
        )

    # ── /geburtstag liste ─────────────────────────────────────────────────────

    @geburtstag.command(name="liste", description="Zeige alle eingetragenen Geburtstage auf diesem Server")
    async def cmd_liste(self, interaction: discord.Interaction):
        await interaction.response.defer()
        server_id = str(interaction.guild_id)

        try:
            sb = get_supabase()
            r  = sb.table("birthdays")\
                   .select("user_id, birthday")\
                   .eq("server_id", server_id)\
                   .execute()
            rows = r.data or []
        except Exception as e:
            await interaction.followup.send("❌ Fehler beim Laden.", ephemeral=True)
            return

        if not rows:
            await interaction.followup.send(
                "📋 Noch niemand hat einen Geburtstag eingetragen.", ephemeral=True
            )
            return

        today = _today()

        # Sortieren: nach Datum im Jahr (Monat, Tag)
        def sort_key(row):
            try:
                d = date.fromisoformat(row["birthday"])
                this_year = d.replace(year=today.year)
                if this_year < today:
                    this_year = d.replace(year=today.year + 1)
                return (this_year - today).days
            except Exception:
                return 9999

        rows_sorted = sorted(rows, key=sort_key)

        lines = []
        for row in rows_sorted:
            member = interaction.guild.get_member(int(row["user_id"]))
            if not member:
                continue
            try:
                bday = date.fromisoformat(row["birthday"])
            except Exception:
                continue

            this_year = bday.replace(year=today.year)
            if this_year < today:
                next_bday = bday.replace(year=today.year + 1)
            else:
                next_bday = this_year
            days = (next_bday - today).days

            if days == 0:
                when = "**🎉 heute!**"
            elif days == 1:
                when = "morgen"
            else:
                when = f"in {days} Tagen"

            lines.append(
                f"**{member.display_name}** – {_format_date(bday)} *(in {when})*"
                if days > 1 else
                f"**{member.display_name}** – {_format_date(bday)} *({when})*"
            )

        # In Seiten aufteilen wenn zu lang
        chunk_size = 20
        chunks     = [lines[i:i+chunk_size] for i in range(0, len(lines), chunk_size)]

        for idx, chunk in enumerate(chunks):
            embed = discord.Embed(
                title=f"🎂 Geburtstage auf diesem Server" + (f" (Seite {idx+1}/{len(chunks)})" if len(chunks) > 1 else ""),
                description="\n".join(chunk),
                color=discord.Color.from_rgb(255, 182, 30),
            )
            embed.set_footer(text=f"{len(rows_sorted)} Einträge gesamt")
            await interaction.followup.send(embed=embed)

    # ── /geburtstag setup (Admin) ─────────────────────────────────────────────

    @geburtstag.command(name="setup", description="[Admin] Lege den Kanal für Geburtstagsnachrichten fest")
    @app_commands.describe(kanal="Der Kanal in dem Geburtstagswünsche gepostet werden")
    async def cmd_setup(self, interaction: discord.Interaction, kanal: discord.TextChannel):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        try:
            _set_birthday_channel_id(str(interaction.guild_id), str(kanal.id))
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ Geburtstagsnachrichten werden ab jetzt in {kanal.mention} gepostet! 🎂",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdaysCog(bot))