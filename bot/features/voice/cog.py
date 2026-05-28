"""
features/voice/cog.py
=====================
Voice Channel Creator – dynamische Voice-Kanäle on demand.

WARTERAUM-LOGIK:
  - TEAM-Rollen (allowed_role_ids):
      • Haben connect=True auf dem Hauptkanal → können direkt beitreten
      • Können aber auch in den Warteraum gehen und eine Anfrage stellen
      • Beim Ablehnen: nur Kick aus dem Warteraum, KEIN Block, können
        weiterhin in den Warteraum und in den Hauptkanal
  - BEREITS ANGENOMMENE User (haben access_role):
      • Werden beim Betreten des Warteraums direkt in den Hauptkanal verschoben
  - GESPERRTE User (in rejected_user_ids mit Ablauf-Zeit):
      • Werden aus dem Warteraum gekickt solange die Sperre aktiv ist
      • Nach Ablauf der Sperre können sie wieder anfragen
  - GEBANNTE User (in banned_user_ids):
      • Werden aus dem Warteraum dauerhaft gekickt
      • Können nicht in den Hauptkanal beitreten
      • Nur Besitzer/Team kann bannen/entbannen
  - NORMALE User:
      • Müssen den Warteraum nutzen und eine Anfrage stellen
      • Beim Ablehnen: Kick + Sperre für festgelegten Zeitraum
        (Standard: 5 Minuten, konfigurierbar vom Kanalbesitzer via Button)

DISCONNECT-WHITELIST-ENTFERNUNG:
  Wenn ein User per Discord-Disconnect (Moderator/Besitzer trennt Verbindung)
  aus dem Hauptkanal entfernt wird:
    • Zugangs-Rolle wird entzogen (Whitelist-Entfernung)
    • User wird für die konfigurierte reject_duration_minutes gesperrt

SPERR-FORMAT in rejected_user_ids:
  Statt reiner User-IDs wird ein JSON-String gespeichert:
  [{"user_id": "...", "until": "2026-04-22T15:00:00+00:00"}, ...]

BAN-FORMAT in banned_user_ids:
  [{"user_id": "...", "banned_by": "...", "banned_at": "ISO-Timestamp"}, ...]

NEUE DB-SPALTEN (einmalig ausführen):
    ALTER TABLE voice_channels
        ADD COLUMN IF NOT EXISTS rejected_user_ids JSONB DEFAULT '[]';
    ALTER TABLE voice_channels
        ADD COLUMN IF NOT EXISTS reject_duration_minutes INTEGER DEFAULT 5;
    ALTER TABLE voice_channels
        ADD COLUMN IF NOT EXISTS banned_user_ids JSONB DEFAULT '[]';
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("voice")

_toggle_locks: dict[str, asyncio.Lock] = {}


def _get_toggle_lock(channel_id: str) -> asyncio.Lock:
    if channel_id not in _toggle_locks:
        _toggle_locks[channel_id] = asyncio.Lock()
    return _toggle_locks[channel_id]


# ══════════════════════════════════════════════════════════════════════════════
# DB HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_config(server_id: str) -> dict | None:
    r = get_supabase().table("voice_creator_config") \
        .select("*").eq("server_id", server_id).execute()
    return r.data[0] if r.data else None


def _save_config(server_id: str, data: dict):
    sb = get_supabase()
    if sb.table("voice_creator_config").select("id").eq("server_id", server_id).execute().data:
        sb.table("voice_creator_config").update(data).eq("server_id", server_id).execute()
    else:
        sb.table("voice_creator_config").insert({"server_id": server_id, **data}).execute()


def _get_vc_by_main(main_channel_id: str) -> dict | None:
    r = get_supabase().table("voice_channels") \
        .select("*").eq("main_channel_id", main_channel_id).execute()
    return r.data[0] if r.data else None


def _get_vc_by_wait(wait_channel_id: str) -> dict | None:
    r = get_supabase().table("voice_channels") \
        .select("*").eq("wait_channel_id", wait_channel_id).execute()
    return r.data[0] if r.data else None


def _get_vc_by_owner(server_id: str, owner_id: str) -> dict | None:
    r = get_supabase().table("voice_channels") \
        .select("*").eq("server_id", server_id).eq("owner_id", owner_id).execute()
    return r.data[0] if r.data else None


def _save_vc(data: dict) -> dict:
    r = get_supabase().table("voice_channels").insert(data).execute()
    return r.data[0] if r.data else {}


def _delete_vc(vc_id: int):
    get_supabase().table("voice_channels").delete().eq("id", vc_id).execute()


def _update_vc(vc_id: int, data: dict):
    get_supabase().table("voice_channels").update(data).eq("id", vc_id).execute()


def _get_all_vcs(server_id: str) -> list[dict]:
    r = get_supabase().table("voice_channels").select("*").eq("server_id", server_id).execute()
    return r.data or []


def _parse_role_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


# ══════════════════════════════════════════════════════════════════════════════
# REJECT / SPERR HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_reject_list(vc_row: dict) -> list[dict]:
    """
    Gibt die Liste der gesperrten User zurück.
    Format: [{"user_id": "...", "until": "ISO-Timestamp"}, ...]
    """
    raw = vc_row.get("rejected_user_ids")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _is_rejected(vc_row: dict, user_id: str) -> bool:
    """
    Prüft ob ein User aktuell gesperrt ist.
    Abgelaufene Sperren werden ignoriert.
    """
    now = datetime.now(timezone.utc)
    for entry in _get_reject_list(vc_row):
        if entry.get("user_id") == str(user_id):
            until_str = entry.get("until")
            if not until_str:
                return True
            try:
                until = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if now < until:
                    return True
            except Exception:
                pass
    return False


def _get_reject_remaining(vc_row: dict, user_id: str) -> timedelta | None:
    """Gibt die verbleibende Sperrzeit zurück, oder None wenn nicht gesperrt."""
    now = datetime.now(timezone.utc)
    for entry in _get_reject_list(vc_row):
        if entry.get("user_id") == str(user_id):
            until_str = entry.get("until")
            if not until_str:
                return None
            try:
                until = datetime.fromisoformat(until_str.replace("Z", "+00:00"))
                if until.tzinfo is None:
                    until = until.replace(tzinfo=timezone.utc)
                if now < until:
                    return until - now
            except Exception:
                pass
    return None


def _add_reject(vc_id: int, vc_row: dict, user_id: str, duration_minutes: int):
    """Fügt einen User zur Sperrliste hinzu (mit Ablaufzeit)."""
    entries = _get_reject_list(vc_row)
    # Alten Eintrag entfernen falls vorhanden
    entries = [e for e in entries if e.get("user_id") != str(user_id)]
    until = datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)
    entries.append({
        "user_id": str(user_id),
        "until": until.isoformat(),
    })
    _update_vc(vc_id, {"rejected_user_ids": entries})


def _remove_reject(vc_id: int, vc_row: dict, user_id: str):
    """Entfernt einen User aus der Sperrliste."""
    entries = _get_reject_list(vc_row)
    entries = [e for e in entries if e.get("user_id") != str(user_id)]
    _update_vc(vc_id, {"rejected_user_ids": entries})


def _get_reject_duration(vc_row: dict) -> int:
    """Gibt die konfigurierte Sperr-Dauer in Minuten zurück (Standard: 5)."""
    return int(vc_row.get("reject_duration_minutes") or 5)


# ══════════════════════════════════════════════════════════════════════════════
# BAN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _get_ban_list(vc_row: dict) -> list[dict]:
    """
    Gibt die Liste der gebannten User zurück.
    Format: [{"user_id": "...", "banned_by": "...", "banned_at": "ISO-Timestamp"}, ...]
    """
    raw = vc_row.get("banned_user_ids")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return []
    return []


def _is_banned(vc_row: dict, user_id: str) -> bool:
    """Prüft ob ein User aus dem Voice-Kanal gebannt ist."""
    for entry in _get_ban_list(vc_row):
        if entry.get("user_id") == str(user_id):
            return True
    return False


def _add_ban(vc_id: int, vc_row: dict, user_id: str, banned_by_id: str):
    """Bannt einen User dauerhaft aus dem Voice-Kanal."""
    entries = _get_ban_list(vc_row)
    # Alten Eintrag entfernen falls vorhanden
    entries = [e for e in entries if e.get("user_id") != str(user_id)]
    entries.append({
        "user_id":   str(user_id),
        "banned_by": str(banned_by_id),
        "banned_at": datetime.now(timezone.utc).isoformat(),
    })
    _update_vc(vc_id, {"banned_user_ids": entries})


def _remove_ban(vc_id: int, vc_row: dict, user_id: str):
    """Entbannt einen User aus dem Voice-Kanal."""
    entries = _get_ban_list(vc_row)
    entries = [e for e in entries if e.get("user_id") != str(user_id)]
    _update_vc(vc_id, {"banned_user_ids": entries})


# ══════════════════════════════════════════════════════════════════════════════
# PERMISSION CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def _can_manage(member: discord.Member, vc_row: dict, cfg: dict) -> bool:
    """Besitzer ODER berechtigte Panel-Rolle ODER Server-Admin."""
    if member.guild_permissions.administrator:
        return True
    if str(member.id) == vc_row.get("owner_id"):
        return True
    allowed = set(_parse_role_ids(cfg.get("allowed_role_ids", "")))
    return bool(allowed & {str(r.id) for r in member.roles})


def _is_team_member(member: discord.Member, cfg: dict) -> bool:
    """
    Prüft ob der Member eine Team-Rolle hat (allowed_role_ids).
    Team-Mitglieder haben connect=True auf dem Hauptkanal und können
    direkt beitreten. Sie können aber auch in den Warteraum gehen und
    eine optionale Anfrage stellen. Werden beim Ablehnen nicht gesperrt.
    """
    if member.guild_permissions.administrator:
        return True
    allowed = set(_parse_role_ids(cfg.get("allowed_role_ids", "")))
    return bool(allowed & {str(r.id) for r in member.roles})


def _has_access_role(member: discord.Member, vc_row: dict) -> bool:
    """Prüft ob der Member die Zugangs-Rolle hat (wurde bereits manuell angenommen)."""
    access_role_id = vc_row.get("access_role_id")
    if not access_role_id:
        return False
    return any(str(r.id) == access_role_id for r in member.roles)


# ══════════════════════════════════════════════════════════════════════════════
# ZUGANGS-ROLLEN HELFER
# ══════════════════════════════════════════════════════════════════════════════

async def _create_access_role(guild: discord.Guild, channel_name: str) -> discord.Role | None:
    try:
        role = await guild.create_role(
            name=f"🔒 VC-Zugang – {channel_name[:40]}",
            reason="Voice Creator – temporäre Zugangs-Rolle",
            mentionable=False,
            hoist=False,
        )
        bot_top = guild.me.top_role
        if bot_top.position > 1:
            try:
                await role.edit(position=max(1, bot_top.position - 1))
            except discord.HTTPException:
                pass
        return role
    except discord.Forbidden:
        logger.error("[voice] _create_access_role: Forbidden")
        return None
    except discord.HTTPException as e:
        logger.error(f"[voice] _create_access_role: {e}")
        return None


async def _delete_access_role(guild: discord.Guild, role_id: str | None) -> None:
    if not role_id:
        return
    try:
        role = guild.get_role(int(role_id))
        if role:
            await role.delete(reason="Voice Creator – Kanal gelöscht")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"[voice] _delete_access_role {role_id}: {e}")


async def _grant_access(guild: discord.Guild, member: discord.Member, access_role_id: str) -> None:
    role = guild.get_role(int(access_role_id))
    if not role:
        return
    try:
        await member.add_roles(role, reason="Voice Creator – Zugang gewährt")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error(f"[voice] _grant_access {member}: {e}")


async def _revoke_access(guild: discord.Guild, member: discord.Member, access_role_id: str) -> None:
    role = guild.get_role(int(access_role_id))
    if not role:
        return
    try:
        await member.remove_roles(role, reason="Voice Creator – Zugang entzogen")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error(f"[voice] _revoke_access {member}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PANEL EMBED
# ══════════════════════════════════════════════════════════════════════════════

def _build_panel_embed(vc_row: dict, guild: discord.Guild) -> discord.Embed:
    owner   = guild.get_member(int(vc_row["owner_id"]))
    is_open = vc_row.get("is_open", True)
    limit   = vc_row.get("user_limit", 0)
    reject_minutes = _get_reject_duration(vc_row)

    embed = discord.Embed(
        title="🎙️ Kanal-Steuerung",
        color=discord.Color.green() if is_open else discord.Color.blurple(),
    )
    embed.add_field(
        name="👑 Besitzer",
        value=owner.mention if owner else f"<@{vc_row['owner_id']}>",
        inline=True,
    )
    embed.add_field(
        name="🔒 Modus",
        value="🔓 Öffentlich" if is_open else "🔒 Privat",
        inline=True,
    )
    if is_open:
        embed.add_field(
            name="👥 Limit",
            value=f"{limit} Personen" if limit > 0 else "Kein Limit",
            inline=True,
        )
    if not is_open:
        ban_count = len(_get_ban_list(vc_row))
        embed.add_field(
            name="⏳ Warteraum",
            value=f"<#{vc_row['wait_channel_id']}>" if vc_row.get("wait_channel_id") else "*–*",
            inline=False,
        )
        embed.add_field(
            name="⛔ Sperr-Dauer",
            value=f"{reject_minutes} Minuten bei Ablehnung",
            inline=True,
        )
        embed.add_field(
            name="🚫 Gebannte User",
            value=str(ban_count) if ban_count > 0 else "Keine",
            inline=True,
        )
        embed.add_field(
            name="ℹ️ Hinweis",
            value=(
                "Nur angenommene Mitglieder können beitreten. "
                "Team-Mitglieder können direkt in den Hauptkanal oder über den Warteraum anfragen."
            ),
            inline=False,
        )
    embed.set_footer(text="Nur Besitzer und berechtigte Rollen können die Buttons nutzen.")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# SPERR-DAUER MODAL
# ══════════════════════════════════════════════════════════════════════════════

class _RejectDurationModal(discord.ui.Modal, title="Sperr-Dauer festlegen"):
    duration = discord.ui.TextInput(
        label="Sperr-Dauer in Minuten (nach Ablehnung)",
        placeholder="z.B. 5  (0 = keine Sperre)",
        required=True,
        max_length=6,
    )

    def __init__(self, vc_row: dict, guild: discord.Guild):
        super().__init__()
        self._vc_row = vc_row
        self._guild  = guild
        self.duration.default = str(_get_reject_duration(vc_row))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            minutes = max(0, int(self.duration.value))
        except ValueError:
            minutes = 5
        _update_vc(self._vc_row["id"], {"reject_duration_minutes": minutes})

        # Panel aktualisieren
        fresh = _get_vc_by_main(str(self._vc_row["main_channel_id"]))
        if fresh and fresh.get("panel_message_id"):
            try:
                ch  = self._guild.get_channel(int(fresh["main_channel_id"]))
                msg = await ch.fetch_message(int(fresh["panel_message_id"]))
                await msg.edit(embed=_build_panel_embed(fresh, self._guild), view=VoicePanelView())
            except Exception:
                pass

        if minutes == 0:
            desc = "Abgelehnte User werden nicht gesperrt."
        else:
            desc = f"Abgelehnte User werden für **{minutes} Minute{'n' if minutes != 1 else ''}** gesperrt."

        await interaction.response.send_message(
            embed=discord.Embed(title="✅ Sperr-Dauer gesetzt", description=desc, color=discord.Color.green()),
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# BAN / UNBAN VIEW
# ══════════════════════════════════════════════════════════════════════════════

class _BanActionSelectView(discord.ui.View):
    """Erste Auswahl: Bannen oder Entbannen?"""

    def __init__(self, vc_row: dict, main_ch: discord.VoiceChannel, guild: discord.Guild):
        super().__init__(timeout=60)
        self.vc_row  = vc_row
        self.main_ch = main_ch
        self.guild   = guild

    @discord.ui.button(label="🚫 User bannen", style=discord.ButtonStyle.danger)
    async def ban_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Alle User im Kanal + alle bekannten Member aus dem Guild als Kandidaten
        # Wir zeigen alle User die NICHT gebannt, NICHT der Besitzer und NICHT der Bot sind
        all_members_in_ch = [
            m for m in self.main_ch.members
            if m.id != interaction.guild.me.id
            and str(m.id) != self.vc_row.get("owner_id")
            and not _is_banned(self.vc_row, str(m.id))
        ]
        if not all_members_in_ch:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="ℹ️ Keine bannbaren User",
                    description="Es befinden sich keine bannbaren User im Kanal.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        view = _BanSelectView(
            members=all_members_in_ch,
            vc_row=self.vc_row,
            main_ch=self.main_ch,
            guild=self.guild,
        )
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🚫 User aus Kanal bannen",
                description=(
                    "Wähle einen oder mehrere User die du aus diesem Voice-Kanal bannen möchtest.\n"
                    "Gebannte User werden sofort aus dem Kanal entfernt und können den Warteraum nicht mehr nutzen."
                ),
                color=discord.Color.red(),
            ),
            view=view,
        )

    @discord.ui.button(label="✅ User entbannen", style=discord.ButtonStyle.success)
    async def unban_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        ban_list = _get_ban_list(self.vc_row)
        if not ban_list:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="ℹ️ Keine gebannten User",
                    description="Es sind keine User aus diesem Kanal gebannt.",
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return

        view = _UnbanSelectView(
            ban_list=ban_list,
            vc_row=self.vc_row,
            guild=self.guild,
        )
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ User entbannen",
                description="Wähle einen oder mehrere User die du aus diesem Voice-Kanal entbannen möchtest.",
                color=discord.Color.green(),
            ),
            view=view,
        )


class _BanSelectView(discord.ui.View):
    """Select-Menü zum Auswählen der zu bannenden User."""

    def __init__(
        self,
        members: list[discord.Member],
        vc_row: dict,
        main_ch: discord.VoiceChannel,
        guild: discord.Guild,
    ):
        super().__init__(timeout=60)
        self.vc_row  = vc_row
        self.main_ch = main_ch
        self.guild   = guild

        options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=f"@{m.name}",
            )
            for m in members[:25]
        ]
        sel = discord.ui.Select(
            placeholder="User zum Bannen auswählen…",
            min_values=1,
            max_values=min(len(options), 10),
            options=options,
        )
        sel.callback = self._selected
        self.add_item(sel)

    async def _selected(self, interaction: discord.Interaction):
        # Frische Daten aus DB holen
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if not fresh:
            await interaction.response.edit_message(
                embed=discord.Embed(title="❌ Kanal nicht mehr gefunden.", color=discord.Color.red()),
                view=None,
            )
            return

        access_role_id = fresh.get("access_role_id")
        banned_names   = []

        for uid in interaction.data["values"]:
            member = interaction.guild.get_member(int(uid))
            if not member:
                continue

            # In die Ban-Liste eintragen
            _add_ban(fresh["id"], fresh, uid, str(interaction.user.id))
            # Frisch laden für nächsten Eintrag
            fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))

            # Zugangs-Rolle entziehen (von Whitelist entfernen)
            if access_role_id:
                await _revoke_access(interaction.guild, member, access_role_id)

            # Aus dem Kanal kicken wenn noch drin
            if member.voice and member.voice.channel == self.main_ch:
                try:
                    await member.move_to(None, reason="Voice-Ban durch Kanalbesitzer")
                except Exception as e:
                    logger.error(f"[ban] move_to: {e}")

            banned_names.append(member.display_name)

            # DM an gebannten User
            try:
                await member.send(embed=discord.Embed(
                    title="🚫 Du wurdest aus einem Voice-Kanal gebannt",
                    description=(
                        f"Du wurdest dauerhaft aus dem Voice-Kanal **{self.main_ch.name}** gebannt.\n"
                        "Du kannst dem Kanal und dem Warteraum nicht mehr beitreten."
                    ),
                    color=discord.Color.dark_red(),
                ))
            except discord.Forbidden:
                pass

        self.stop()
        names = ", ".join(banned_names) if banned_names else "Niemand"
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🚫 {names} {'wurde' if len(banned_names) == 1 else 'wurden'} gebannt",
                description="Zugangs-Rolle entzogen und aus dem Kanal entfernt.",
                color=discord.Color.dark_red(),
            ),
            view=None,
        )

        # Panel aktualisieren
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if fresh and fresh.get("panel_message_id"):
            try:
                msg = await self.main_ch.fetch_message(int(fresh["panel_message_id"]))
                await msg.edit(embed=_build_panel_embed(fresh, interaction.guild), view=VoicePanelView())
            except Exception:
                pass


class _UnbanSelectView(discord.ui.View):
    """Select-Menü zum Auswählen der zu entbannenden User."""

    def __init__(
        self,
        ban_list: list[dict],
        vc_row: dict,
        guild: discord.Guild,
    ):
        super().__init__(timeout=60)
        self.vc_row = vc_row
        self.guild  = guild

        options = []
        for entry in ban_list[:25]:
            uid    = entry.get("user_id", "")
            member = guild.get_member(int(uid)) if uid else None
            label  = member.display_name[:100] if member else f"User {uid[:10]}"
            desc   = f"@{member.name}" if member else f"ID: {uid}"
            options.append(discord.SelectOption(label=label, value=uid, description=desc))

        if not options:
            return

        sel = discord.ui.Select(
            placeholder="User zum Entbannen auswählen…",
            min_values=1,
            max_values=min(len(options), 10),
            options=options,
        )
        sel.callback = self._selected
        self.add_item(sel)

    async def _selected(self, interaction: discord.Interaction):
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if not fresh:
            await interaction.response.edit_message(
                embed=discord.Embed(title="❌ Kanal nicht mehr gefunden.", color=discord.Color.red()),
                view=None,
            )
            return

        unbanned_names = []
        for uid in interaction.data["values"]:
            _remove_ban(fresh["id"], fresh, uid)
            fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))

            member = interaction.guild.get_member(int(uid))
            name   = member.display_name if member else f"User {uid}"
            unbanned_names.append(name)

            # Optionale DM an entbannten User
            if member:
                try:
                    main_ch = interaction.guild.get_channel(int(self.vc_row["main_channel_id"]))
                    ch_name = main_ch.name if main_ch else "Voice-Kanal"
                    await member.send(embed=discord.Embed(
                        title="✅ Bann aufgehoben",
                        description=(
                            f"Dein Bann aus dem Voice-Kanal **{ch_name}** wurde aufgehoben.\n"
                            "Du kannst den Warteraum wieder nutzen um Zugang anzufragen."
                        ),
                        color=discord.Color.green(),
                    ))
                except discord.Forbidden:
                    pass

        self.stop()
        names = ", ".join(unbanned_names) if unbanned_names else "Niemand"
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"✅ {names} {'wurde' if len(unbanned_names) == 1 else 'wurden'} entbannt",
                description="Die User können den Warteraum wieder nutzen.",
                color=discord.Color.green(),
            ),
            view=None,
        )

        # Panel aktualisieren
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if fresh and fresh.get("panel_message_id"):
            try:
                main_ch = interaction.guild.get_channel(int(fresh["main_channel_id"]))
                if main_ch:
                    msg = await main_ch.fetch_message(int(fresh["panel_message_id"]))
                    await msg.edit(embed=_build_panel_embed(fresh, interaction.guild), view=VoicePanelView())
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# PANEL VIEW
# ══════════════════════════════════════════════════════════════════════════════

class VoicePanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def _refresh_panel(self, interaction: discord.Interaction, vc_row: dict):
        if not vc_row or not vc_row.get("panel_message_id"):
            return
        try:
            ch  = interaction.guild.get_channel(int(vc_row["main_channel_id"]))
            msg = await ch.fetch_message(int(vc_row["panel_message_id"]))
            await msg.edit(embed=_build_panel_embed(vc_row, interaction.guild), view=VoicePanelView())
        except Exception as e:
            logger.error(f"[vpc] Panel-Refresh: {e}")

    @discord.ui.button(
        label="🔒 Privat / 🔓 Öffentlich",
        style=discord.ButtonStyle.primary,
        custom_id="vpc_toggle_private",
        row=0,
    )
    async def toggle_private(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        channel_key = str(interaction.channel_id)
        lock = _get_toggle_lock(channel_key)

        if lock.locked():
            await interaction.response.send_message(
                "⏳ Der Kanal wird gerade umgestellt – bitte einen Moment warten.",
                ephemeral=True,
            )
            return

        async with lock:
            vc_row = _get_vc_by_main(channel_key)
            if not vc_row:
                await interaction.response.send_message("❌ Kanal nicht mehr gefunden.", ephemeral=True)
                return

            current_is_open = vc_row.get("is_open", True)
            new_open = not current_is_open
            _update_vc(vc_row["id"], {"is_open": new_open})
            await interaction.response.defer(ephemeral=True)

            guild   = interaction.guild
            main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
            if not main_ch:
                _update_vc(vc_row["id"], {"is_open": current_is_open})
                await interaction.followup.send("❌ Voice-Kanal nicht gefunden.", ephemeral=True)
                return

            owner = guild.get_member(int(vc_row["owner_id"]))

            if new_open:
                # ── Auf öffentlich stellen ────────────────────────────────────
                try:
                    await main_ch.set_permissions(guild.default_role, view_channel=True, connect=True)
                except discord.Forbidden:
                    logger.warning("[voice] toggle→öffentlich: Forbidden")

                if vc_row.get("wait_channel_id"):
                    wait_ch = guild.get_channel(int(vc_row["wait_channel_id"]))
                    if wait_ch:
                        try:
                            await wait_ch.delete(reason="Kanal auf öffentlich gestellt")
                        except Exception:
                            pass

                await _delete_access_role(guild, vc_row.get("access_role_id"))
                _update_vc(vc_row["id"], {
                    "is_open": True,
                    "wait_channel_id": None,
                    "access_role_id": None,
                })
                msg = "🔓 Kanal ist jetzt öffentlich."

            else:
                # ── Auf privat stellen ────────────────────────────────────────
                access_role = await _create_access_role(guild, main_ch.name)
                if not access_role:
                    _update_vc(vc_row["id"], {"is_open": True})
                    await interaction.followup.send(
                        "❌ Konnte Zugangs-Rolle nicht erstellen. Bot braucht **Manage Roles**.",
                        ephemeral=True,
                    )
                    return

                new_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, connect=False,
                    ),
                    access_role: discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, connect=True, move_members=True, manage_channels=True,
                    ),
                }
                if owner:
                    new_overwrites[owner] = discord.PermissionOverwrite(
                        view_channel=True, connect=True, move_members=True, manage_channels=True,
                    )
                for rid in _parse_role_ids(cfg.get("allowed_role_ids", "")):
                    role = guild.get_role(int(rid))
                    if role:
                        new_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True, connect=True, move_members=True,
                        )

                try:
                    await main_ch.edit(overwrites=new_overwrites, sync_permissions=False)
                except discord.Forbidden:
                    await _delete_access_role(guild, str(access_role.id))
                    _update_vc(vc_row["id"], {"is_open": True})
                    await interaction.followup.send("❌ Konnte Kanal-Berechtigungen nicht setzen.", ephemeral=True)
                    return

                for m in main_ch.members:
                    if m.id != guild.me.id:
                        await _grant_access(guild, m, str(access_role.id))

                owner_display = owner.display_name if owner else "Kanal"
                wait_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, connect=True, speak=False, send_messages=False,
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, connect=True, move_members=True, manage_channels=True,
                    ),
                    access_role: discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                    ),
                }
                if owner:
                    wait_overwrites[owner] = discord.PermissionOverwrite(
                        view_channel=True, connect=False,
                    )
                for rid in _parse_role_ids(cfg.get("allowed_role_ids", "")):
                    role = guild.get_role(int(rid))
                    if role:
                        wait_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True, connect=True, speak=False,
                        )

                try:
                    wait_ch = await guild.create_voice_channel(
                        name=f"⏳ Warteraum – {owner_display}",
                        category=main_ch.category,
                        overwrites=wait_overwrites,
                        reason="Voice Creator – Kanal auf privat gestellt",
                    )
                except discord.Forbidden:
                    await _delete_access_role(guild, str(access_role.id))
                    _update_vc(vc_row["id"], {"is_open": True})
                    await interaction.followup.send("❌ Konnte Warteraum nicht erstellen.", ephemeral=True)
                    return

                _update_vc(vc_row["id"], {
                    "is_open": False,
                    "wait_channel_id": str(wait_ch.id),
                    "access_role_id": str(access_role.id),
                    "user_limit": 0,
                })
                msg = (
                    "🔒 Kanal ist jetzt privat.\n"
                    "• Team-Mitglieder können direkt beitreten oder über den Warteraum anfragen.\n"
                    "• Neue Mitglieder müssen den Warteraum nutzen."
                )

            vc_row = _get_vc_by_main(channel_key)
            await self._refresh_panel(interaction, vc_row)
            await interaction.followup.send(msg, ephemeral=True)

    @discord.ui.button(
        label="👟 User kicken",
        style=discord.ButtonStyle.danger,
        custom_id="vpc_kick_user",
        row=0,
    )
    async def kick_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        main_ch = interaction.guild.get_channel(int(vc_row["main_channel_id"]))
        members_in_vc = [
            m for m in (main_ch.members if main_ch else [])
            if str(m.id) != vc_row["owner_id"] and m.id != interaction.guild.me.id
        ]
        if not members_in_vc:
            await interaction.response.send_message("ℹ️ Keine anderen Mitglieder im Kanal.", ephemeral=True)
            return

        is_private = not vc_row.get("is_open", True)
        view = _KickSelectView(members_in_vc, main_ch, vc_row, is_private)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="👟 User aus dem Kanal entfernen",
                description=(
                    "Wähle einen oder mehrere Nutzer.\n"
                    + ("Im **privaten Modus** wird die Zugangs-Rolle entzogen und "
                       "der User für die konfigurierte Sperr-Dauer gesperrt."
                       if is_private else "")
                ),
                color=discord.Color.red(),
            ),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="🚫 Bannen / Entbannen",
        style=discord.ButtonStyle.danger,
        custom_id="vpc_ban_user",
        row=1,
    )
    async def ban_user(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Ban oder Unban eines Users aus diesem Voice-Kanal (nicht Server-Ban)."""
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        is_private = not vc_row.get("is_open", True)
        if not is_private:
            await interaction.response.send_message(
                "❌ Bannen ist nur bei **privaten** Kanälen verfügbar.",
                ephemeral=True,
            )
            return

        main_ch = interaction.guild.get_channel(int(vc_row["main_channel_id"]))
        if not main_ch:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return

        ban_count = len(_get_ban_list(vc_row))
        view = _BanActionSelectView(vc_row=vc_row, main_ch=main_ch, guild=interaction.guild)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🚫 Kanal-Ban Verwaltung",
                description=(
                    "Wähle eine Aktion:\n\n"
                    f"Aktuell gebannte User: **{ban_count}**\n\n"
                    "• **Bannen** – User aus dem Kanal entfernen und dauerhaft sperren\n"
                    "• **Entbannen** – Bann aufheben, User kann wieder anfragen"
                ),
                color=discord.Color.dark_red(),
            ),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="👥 User-Limit",
        style=discord.ButtonStyle.secondary,
        custom_id="vpc_user_limit",
        row=1,
    )
    async def user_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if not vc_row.get("is_open", True):
            await interaction.response.send_message(
                "❌ Limit ist nur bei öffentlichen Kanälen verfügbar.", ephemeral=True
            )
            return
        current = vc_row.get("user_limit", 0)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="👥 User-Limit festlegen",
                description=f"Aktuelles Limit: **{current if current > 0 else 'Kein Limit'}**",
                color=discord.Color.blurple(),
            ),
            view=_LimitView(vc_row, current),
            ephemeral=True,
        )

    @discord.ui.button(
        label="⛔ Sperr-Dauer",
        style=discord.ButtonStyle.secondary,
        custom_id="vpc_reject_duration",
        row=1,
    )
    async def reject_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        """Kanalbesitzer kann einstellen wie lange abgelehnte User gesperrt sind."""
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.send_modal(
            _RejectDurationModal(vc_row=vc_row, guild=interaction.guild)
        )

    @discord.ui.button(
        label="🗑️ Kanal löschen",
        style=discord.ButtonStyle.danger,
        custom_id="vpc_delete_channel",
        row=1,
    )
    async def delete_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🗑️ Kanal wirklich löschen?",
                description="Voice-Kanal, Warteraum und Zugangs-Rolle werden sofort gelöscht.",
                color=discord.Color.red(),
            ),
            view=_DeleteConfirmView(vc_row),
            ephemeral=True,
        )


# ══════════════════════════════════════════════════════════════════════════════
# KICK SELECT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class _KickSelectView(discord.ui.View):
    def __init__(self, members: list[discord.Member], channel: discord.VoiceChannel, vc_row: dict, is_private: bool):
        super().__init__(timeout=60)
        self.channel    = channel
        self.vc_row     = vc_row
        self.is_private = is_private

        options = [
            discord.SelectOption(label=m.display_name[:100], value=str(m.id), description=f"@{m.name}")
            for m in members[:25]
        ]
        sel = discord.ui.Select(
            placeholder="User auswählen…",
            min_values=1,
            max_values=min(len(options), 10),
            options=options,
        )
        sel.callback = self._selected
        self.add_item(sel)

    async def _selected(self, interaction: discord.Interaction):
        access_role_id = self.vc_row.get("access_role_id")
        kicked = []

        # Frische DB-Daten für Reject-Dauer
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        target_row = fresh if fresh else self.vc_row
        duration = _get_reject_duration(target_row) if self.is_private else 0

        for uid in interaction.data["values"]:
            member = interaction.guild.get_member(int(uid))
            if not member:
                continue
            if member.voice and member.voice.channel == self.channel:
                try:
                    await member.move_to(None, reason="Voice-Kick durch Kanalbesitzer")
                except Exception as e:
                    logger.error(f"[kick] move_to: {e}")
            kicked.append(member.display_name)
            if self.is_private and access_role_id:
                # Zugangs-Rolle entziehen (von Whitelist entfernen)
                await _revoke_access(interaction.guild, member, access_role_id)
                # Sperr-Zeit setzen (wie bei Ablehnen im Warteraum)
                if duration > 0 and fresh:
                    _add_reject(fresh["id"], fresh, uid, duration)
                    fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))

        self.stop()
        names = ", ".join(kicked) if kicked else "Niemand"
        extra_lines = []
        if self.is_private and kicked:
            extra_lines.append("Zugangs-Rolle entzogen (von Whitelist entfernt).")
            if duration > 0:
                extra_lines.append(
                    f"Für **{duration} Minute{'n' if duration != 1 else ''}** gesperrt."
                )
        extra = "\n".join(extra_lines)

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"👟 {names} {'wurde' if len(kicked) == 1 else 'wurden'} entfernt",
                description=extra,
                color=discord.Color.orange(),
            ),
            view=None,
        )


# ══════════════════════════════════════════════════════════════════════════════
# LIMIT VIEW
# ══════════════════════════════════════════════════════════════════════════════

class _LimitView(discord.ui.View):
    def __init__(self, vc_row: dict, current: int):
        super().__init__(timeout=60)
        self.vc_row  = vc_row
        self.current = current

    def _embed(self) -> discord.Embed:
        return discord.Embed(
            title="👥 User-Limit",
            description=f"Aktuelles Limit: **{self.current if self.current > 0 else 'Kein Limit'}**",
            color=discord.Color.blurple(),
        )

    @discord.ui.button(label="➕ +1",    style=discord.ButtonStyle.success)
    async def plus_one(self, i, b):   self.current = min(99, self.current + 1); await self._apply(i)

    @discord.ui.button(label="➕ +5",    style=discord.ButtonStyle.success)
    async def plus_five(self, i, b):  self.current = min(99, self.current + 5); await self._apply(i)

    @discord.ui.button(label="➖ -1",    style=discord.ButtonStyle.danger)
    async def minus_one(self, i, b):  self.current = max(0, self.current - 1);  await self._apply(i)

    @discord.ui.button(label="➖ -5",    style=discord.ButtonStyle.danger)
    async def minus_five(self, i, b): self.current = max(0, self.current - 5);  await self._apply(i)

    @discord.ui.button(label="🔄 Reset", style=discord.ButtonStyle.secondary)
    async def reset(self, i, b):      self.current = 0;                          await self._apply(i)

    async def _apply(self, interaction: discord.Interaction):
        main_ch = interaction.guild.get_channel(int(self.vc_row["main_channel_id"]))
        if main_ch:
            try:
                await main_ch.edit(user_limit=self.current)
            except discord.Forbidden:
                logger.warning("[voice] _LimitView: Forbidden")
        _update_vc(self.vc_row["id"], {"user_limit": self.current})
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if fresh and fresh.get("panel_message_id") and main_ch:
            try:
                msg = await main_ch.fetch_message(int(fresh["panel_message_id"]))
                await msg.edit(embed=_build_panel_embed(fresh, interaction.guild), view=VoicePanelView())
            except Exception:
                pass
        await interaction.response.edit_message(embed=self._embed(), view=self)


# ══════════════════════════════════════════════════════════════════════════════
# DELETE CONFIRM VIEW
# ══════════════════════════════════════════════════════════════════════════════

class _DeleteConfirmView(discord.ui.View):
    def __init__(self, vc_row: dict):
        super().__init__(timeout=30)
        self.vc_row = vc_row

    @discord.ui.button(label="✅ Ja, löschen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.defer(ephemeral=True)
        await _delete_vc_channels(interaction.guild, self.vc_row)
        await interaction.followup.send("✅ Kanal wurde gelöscht.", ephemeral=True)

    @discord.ui.button(label="❌ Abbrechen", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(title="Abgebrochen", color=discord.Color.green()), view=None,
        )


# ══════════════════════════════════════════════════════════════════════════════
# WARTERAUM-ANFRAGE VIEW
# ══════════════════════════════════════════════════════════════════════════════

class WaitingRoomRequestView(discord.ui.View):
    def __init__(
        self,
        requester: discord.Member,
        main_ch: discord.VoiceChannel,
        wait_ch: discord.VoiceChannel,
        vc_row: dict,
        cfg: dict,
        is_team: bool = False,
    ):
        super().__init__(timeout=120)
        self.requester = requester
        self.main_ch   = main_ch
        self.wait_ch   = wait_ch
        self.vc_row    = vc_row
        self.cfg       = cfg
        self.is_team   = is_team
        self._done     = False

    def _still_waiting(self) -> bool:
        return (
            self.requester.voice is not None
            and self.requester.voice.channel is not None
            and self.requester.voice.channel.id == self.wait_ch.id
        )

    @discord.ui.button(label="✅ Annehmen", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_manage(interaction.user, self.vc_row, self.cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if self._done:
            await interaction.response.send_message("ℹ️ Bereits bearbeitet.", ephemeral=True)
            return
        self._done = True
        self.stop()

        access_role_id = self.vc_row.get("access_role_id")
        if access_role_id and not self.is_team:
            await _grant_access(interaction.guild, self.requester, access_role_id)

        if self._still_waiting():
            try:
                await self.requester.move_to(self.main_ch)
            except Exception as e:
                logger.error(f"[accept] move_to: {e}")

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"✅ {self.requester.display_name} wurde angenommen",
                description=(
                    "Zugang gewährt und in den Kanal verschoben."
                    if not self.is_team
                    else "Team-Mitglied in den Kanal verschoben."
                ),
                color=discord.Color.green(),
            ),
            view=self,
        )

    @discord.ui.button(label="❌ Ablehnen", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _can_manage(interaction.user, self.vc_row, self.cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        if self._done:
            await interaction.response.send_message("ℹ️ Bereits bearbeitet.", ephemeral=True)
            return
        self._done = True
        self.stop()

        if self._still_waiting():
            try:
                await self.requester.move_to(None)
            except Exception as e:
                logger.error(f"[deny] move_to: {e}")

        if self.is_team:
            description = (
                f"**{self.requester.display_name}** wurde aus dem Warteraum entfernt.\n"
                "Als Team-Mitglied kann er/sie weiterhin direkt in den Kanal beitreten."
            )
            color = discord.Color.orange()
        else:
            fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
            target_row = fresh if fresh else self.vc_row
            duration = _get_reject_duration(target_row)

            if duration > 0:
                _add_reject(target_row["id"], target_row, str(self.requester.id), duration)
                duration_text = f"für **{duration} Minute{'n' if duration != 1 else ''}**"
                description = (
                    f"**{self.requester.display_name}** wurde abgelehnt und "
                    f"{duration_text} aus dem Warteraum gesperrt."
                )
                try:
                    await self.requester.send(embed=discord.Embed(
                        title="❌ Zugangsanfrage abgelehnt",
                        description=(
                            f"Deine Anfrage für den Voice-Kanal wurde abgelehnt.\n"
                            f"Du kannst in {duration} Minute{'n' if duration != 1 else ''} erneut anfragen."
                        ),
                        color=discord.Color.red(),
                    ))
                except discord.Forbidden:
                    pass
            else:
                description = f"**{self.requester.display_name}** wurde abgelehnt."

            color = discord.Color.red()

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"❌ {self.requester.display_name} abgelehnt",
                description=description,
                color=color,
            ),
            view=self,
        )

    async def on_timeout(self):
        if self._done:
            return
        self._done = True
        if not self.is_team and self._still_waiting():
            try:
                await self.requester.move_to(None)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# KANAL ERSTELLEN
# ══════════════════════════════════════════════════════════════════════════════

async def _create_voice_channels(
    guild: discord.Guild,
    member: discord.Member,
    cfg: dict,
    bot: discord.Client,
) -> None:
    existing = _get_vc_by_owner(str(guild.id), str(member.id))
    if existing:
        main_ch = guild.get_channel(int(existing["main_channel_id"]))
        if main_ch and member.voice:
            try:
                await member.move_to(main_ch)
            except Exception:
                pass
        return

    category_id      = cfg.get("category_id")
    category         = guild.get_channel(int(category_id)) if category_id else None
    allowed_role_ids = _parse_role_ids(cfg.get("allowed_role_ids", ""))

    main_ow = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
        member: discord.PermissionOverwrite(
            view_channel=True, connect=True, move_members=True, manage_channels=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, move_members=True, manage_channels=True,
        ),
    }
    for role_id in allowed_role_ids:
        role = guild.get_role(int(role_id))
        if role:
            main_ow[role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, move_members=True,
            )

    try:
        main_ch = await guild.create_voice_channel(
            name=f"🎙️ {member.display_name}",
            category=category,
            overwrites=main_ow,
            reason=f"Voice Creator – {member.display_name}",
        )
    except discord.Forbidden:
        logger.error(f"[voice] Kanal-Erstellung fehlgeschlagen für {member}")
        return

    if member.voice:
        try:
            await member.move_to(main_ch)
        except Exception as e:
            logger.warning(f"[voice] Move fehlgeschlagen: {e}")

    vc_data = _save_vc({
        "server_id":               str(guild.id),
        "owner_id":                str(member.id),
        "main_channel_id":         str(main_ch.id),
        "wait_channel_id":         None,
        "panel_message_id":        None,
        "access_role_id":          None,
        "rejected_user_ids":       [],
        "reject_duration_minutes": 5,
        "banned_user_ids":         [],
        "is_open":                 True,
        "user_limit":              0,
        "created_at":              datetime.now(timezone.utc).isoformat(),
        "last_empty_at":           None,
    })

    try:
        panel_msg = await main_ch.send(
            embed=_build_panel_embed(vc_data, guild),
            view=VoicePanelView(),
        )
        _update_vc(vc_data["id"], {"panel_message_id": str(panel_msg.id)})
        try:
            await panel_msg.pin()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[voice] Panel senden fehlgeschlagen: {e}")

    logger.info(f"[voice] Kanal erstellt für {member.display_name}: {main_ch.id}")


# ══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

async def _delete_vc_channels(guild: discord.Guild, vc_row: dict) -> None:
    await _delete_access_role(guild, vc_row.get("access_role_id"))

    for ch_id in filter(None, [vc_row.get("main_channel_id"), vc_row.get("wait_channel_id")]):
        ch = guild.get_channel(int(ch_id))
        if ch:
            try:
                await ch.delete(reason="Voice Creator – Cleanup")
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[cleanup] Kanal {ch_id}: {e}")

    _toggle_locks.pop(str(vc_row.get("main_channel_id", "")), None)
    _delete_vc(vc_row["id"])
    logger.info(f"[voice] Cleanup: owner={vc_row['owner_id']}")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════

class VoiceSetupView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id       = str(guild_id)
        self.bot            = bot
        self.category_id:   str | None = None
        self.channel_name:  str        = "➕  Kanal erstellen"
        self.empty_timeout: int        = 30
        self.allowed_roles: list[str]  = []
        self.creator_roles: list[str]  = []
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🎙️ Voice Creator Setup",
            description=(
                "**Benötigte Bot-Berechtigungen (kein Admin nötig):**\n"
                "✅ Manage Roles • Manage Channels • Move Members\n"
                "✅ View Channel • Connect • Send Messages\n\n"
                "⚠️ Die Bot-Rolle muss **über allen anderen Rollen** stehen."
            ),
            color=discord.Color.blurple(),
        )
        e.add_field(name="📁 Kategorie", value=f"<#{self.category_id}>" if self.category_id else "*Server-Root*", inline=True)
        e.add_field(name="🎙️ Kanal-Name", value=f"`{self.channel_name}`", inline=True)
        e.add_field(name="⏱️ Timeout", value=f"{self.empty_timeout}s", inline=True)
        e.add_field(
            name="🎙️ Erstell-Berechtigung",
            value=", ".join(f"<@&{r}>" for r in self.creator_roles) if self.creator_roles else "*Jeder*",
            inline=False,
        )
        e.add_field(
            name="🔑 Team-Rollen (direkter Zugang + Panel nutzen)",
            value=", ".join(f"<@&{r}>" for r in self.allowed_roles) if self.allowed_roles else "*keine*",
            inline=False,
        )
        return e

    def _rebuild(self):
        self.clear_items()

        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Kategorie für Kanäle (optional)",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.category], row=0,
        )
        cat_sel.callback = self._cb_category
        self.add_item(cat_sel)

        creator_sel = discord.ui.RoleSelect(
            placeholder="🎙️ Erstell-Rollen (leer = jeder)",
            min_values=0, max_values=10, row=1,
        )
        creator_sel.callback = self._cb_creator_roles
        self.add_item(creator_sel)

        role_sel = discord.ui.RoleSelect(
            placeholder="🔑 Team-Rollen: direkter Zugang + Panel",
            min_values=0, max_values=10, row=2,
        )
        role_sel.callback = self._cb_roles
        self.add_item(role_sel)

        btn_name = discord.ui.Button(label="✏️ Kanal-Namen ändern", style=discord.ButtonStyle.secondary, row=3)
        btn_name.callback = self._cb_name
        self.add_item(btn_name)

        btn_timeout = discord.ui.Button(label=f"⏱️ Timeout: {self.empty_timeout}s", style=discord.ButtonStyle.secondary, row=3)
        btn_timeout.callback = self._cb_timeout
        self.add_item(btn_timeout)

        save_btn = discord.ui.Button(label="🚀 Setup abschließen", style=discord.ButtonStyle.success, row=4)
        save_btn.callback = self._cb_save
        self.add_item(save_btn)

    async def _cb_category(self, interaction: discord.Interaction):
        vals = interaction.data.get("values", [])
        self.category_id = vals[0] if vals else None
        self._rebuild()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _cb_creator_roles(self, interaction: discord.Interaction):
        self.creator_roles = interaction.data.get("values", [])
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _cb_roles(self, interaction: discord.Interaction):
        self.allowed_roles = interaction.data.get("values", [])
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def _cb_name(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_NameModal(self))

    async def _cb_timeout(self, interaction: discord.Interaction):
        await interaction.response.send_modal(_TimeoutModal(self))

    async def _cb_save(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            guild    = interaction.guild
            category = guild.get_channel(int(self.category_id)) if self.category_id else None

            me_perms = guild.me.guild_permissions
            missing = []
            if not me_perms.manage_roles:    missing.append("Manage Roles")
            if not me_perms.manage_channels: missing.append("Manage Channels")
            if not me_perms.move_members:    missing.append("Move Members")
            if missing:
                await interaction.followup.send(f"❌ Dem Bot fehlen: **{', '.join(missing)}**", ephemeral=True)
                return

            cfg = _get_config(str(guild.id))
            if cfg and cfg.get("channel_id"):
                old_ch = guild.get_channel(int(cfg["channel_id"]))
                if old_ch:
                    try:
                        await old_ch.delete(reason="Voice Creator – Neu eingerichtet")
                    except Exception:
                        pass

            ow = {
                guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True, speak=False),
                guild.me: discord.PermissionOverwrite(view_channel=True, connect=True, move_members=True, manage_channels=True),
            }
            for role_id in self.allowed_roles:
                role = guild.get_role(int(role_id))
                if role:
                    ow[role] = discord.PermissionOverwrite(view_channel=True, connect=True)

            creator_ch = await guild.create_voice_channel(
                name=self.channel_name,
                category=category,
                overwrites=ow,
                reason="Voice Creator Setup",
            )

            _save_config(str(guild.id), {
                "category_id":      str(self.category_id) if self.category_id else None,
                "channel_id":       str(creator_ch.id),
                "channel_name":     self.channel_name,
                "empty_timeout":    self.empty_timeout,
                "allowed_role_ids": ",".join(self.allowed_roles),
                "creator_role_ids": ",".join(self.creator_roles),
            })

            embed = self._build_embed()
            embed.title = "✅ Voice Creator eingerichtet!"
            embed.color = discord.Color.green()
            embed.add_field(name="🎙️ Erstell-Kanal", value=creator_ch.mention, inline=False)
            for item in self.children:
                item.disabled = True
            await interaction.edit_original_response(embed=embed, view=self)

        except discord.Forbidden:
            await interaction.followup.send("❌ Fehlende Berechtigungen.", ephemeral=True)
        except Exception as e:
            logger.error(f"[VoiceSetupView._cb_save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class _NameModal(discord.ui.Modal, title="Erstell-Kanal umbenennen"):
    name = discord.ui.TextInput(label="Name des Join-to-Create Kanals", placeholder="➕  Kanal erstellen", required=True, max_length=100)

    def __init__(self, view: VoiceSetupView):
        super().__init__()
        self._view = view
        self.name.default = view.channel_name

    async def on_submit(self, interaction: discord.Interaction):
        self._view.channel_name = self.name.value
        self._view._rebuild()
        await interaction.response.edit_message(embed=self._view._build_embed(), view=self._view)


class _TimeoutModal(discord.ui.Modal, title="Leerlauf-Timeout festlegen"):
    timeout = discord.ui.TextInput(label="Sekunden bis Kanäle gelöscht werden (min. 10)", placeholder="30", required=True, max_length=6)

    def __init__(self, view: VoiceSetupView):
        super().__init__()
        self._view = view
        self.timeout.default = str(view.empty_timeout)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self._view.empty_timeout = max(10, int(self.timeout.value))
        except ValueError:
            self._view.empty_timeout = 30
        self._view._rebuild()
        await interaction.response.edit_message(embed=self._view._build_embed(), view=self._view)


# ══════════════════════════════════════════════════════════════════════════════
# COG
# ══════════════════════════════════════════════════════════════════════════════

class VoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cleanup_task.start()

    def cog_unload(self):
        self.cleanup_task.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(VoicePanelView())
        logger.info("✅ VoicePanelView registriert")

    voice = app_commands.Group(
        name="voice",
        description="Voicechannel System"
    )

    @voice.command(name="setup", description="Richte den automatischen Voice Channel Creator ein")
    async def voice_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = VoiceSetupView(guild_id=interaction.guild_id, bot=self.bot)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)


    async def _handle_waitroom_join(
        self, guild: discord.Guild, member: discord.Member, vc_row: dict, cfg: dict
    ):
        main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
        wait_ch = guild.get_channel(int(vc_row["wait_channel_id"]))
        if not main_ch or not wait_ch:
            return

        # ── Kanalbesitzer / can_manage → direkt in den Hauptkanal ────────────
        if _can_manage(member, vc_row, cfg):
            try:
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom manage auto-join] {e}")
            return

        # ── Bereits angenommene User (haben access_role) → direkt weiterleiten
        if _has_access_role(member, vc_row):
            try:
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom access_role re-join] {e}")
            return

        # ── Gebannte User → sofort kicken ─────────────────────────────────────
        if _is_banned(vc_row, str(member.id)):
            try:
                await member.move_to(None)
            except Exception as e:
                logger.error(f"[waitroom banned kick] {e}")
            try:
                main_ch_name = main_ch.name if main_ch else "Voice-Kanal"
                await member.send(embed=discord.Embed(
                    title="🚫 Zugang dauerhaft gesperrt",
                    description=(
                        f"Du bist aus dem Voice-Kanal **{main_ch_name}** gebannt.\n"
                        "Du kannst den Warteraum nicht nutzen."
                    ),
                    color=discord.Color.dark_red(),
                ))
            except discord.Forbidden:
                pass
            return

        # ── Aktuell gesperrte User → sofort kicken ───────────────────────────
        if _is_rejected(vc_row, str(member.id)):
            remaining = _get_reject_remaining(vc_row, str(member.id))
            try:
                await member.move_to(None)
            except Exception as e:
                logger.error(f"[waitroom rejected kick] {e}")
            try:
                if remaining:
                    minutes = int(remaining.total_seconds() // 60) + 1
                    time_text = f"Noch ca. **{minutes} Minute{'n' if minutes != 1 else ''}**."
                else:
                    time_text = ""
                await member.send(embed=discord.Embed(
                    title="🚫 Zugang gesperrt",
                    description=(
                        f"Du wurdest aus dem Warteraum von **{main_ch.name}** entfernt.\n"
                        f"Deine Anfrage wurde kürzlich abgelehnt. {time_text}"
                    ),
                    color=discord.Color.red(),
                ))
            except discord.Forbidden:
                pass
            return

        # ── Team-Mitglieder → Warteraum erlaubt, optionale Anfrage ───────────
        if _is_team_member(member, cfg):
            embed = discord.Embed(
                title="🔔 Team-Anfrage aus dem Warteraum",
                description=(
                    f"**{member.mention}** (`{member.display_name}`) "
                    f"wartet im Warteraum und möchte in **{main_ch.name}**.\n\n"
                    f"ℹ️ Als Team-Mitglied kann er/sie auch direkt beitreten."
                ),
                color=discord.Color.blue(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            try:
                await main_ch.send(
                    embed=embed,
                    view=WaitingRoomRequestView(
                        requester=member,
                        main_ch=main_ch,
                        wait_ch=wait_ch,
                        vc_row=vc_row,
                        cfg=cfg,
                        is_team=True,
                    ),
                )
            except Exception as e:
                logger.error(f"[waitroom team request] {e}")
            return

        # ── Normale User → automatische Anfrage ──────────────────────────────
        embed = discord.Embed(
            title="🔔 Beitrittsanfrage",
            description=(
                f"**{member.mention}** (`{member.display_name}`) "
                f"möchte dem Kanal **{main_ch.name}** beitreten."
            ),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        reject_minutes = _get_reject_duration(vc_row)
        footer = f"Ablehnen sperrt den User für {reject_minutes} Minuten · Timeout: 2 Minuten"
        embed.set_footer(text=footer)
        try:
            await main_ch.send(
                embed=embed,
                view=WaitingRoomRequestView(
                    requester=member,
                    main_ch=main_ch,
                    wait_ch=wait_ch,
                    vc_row=vc_row,
                    cfg=cfg,
                    is_team=False,
                ),
            )
        except Exception as e:
            logger.error(f"[waitroom normal request] {e}")

    async def _check_empty(self, guild: discord.Guild, vc_row: dict):
        main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
        wait_id = vc_row.get("wait_channel_id")
        wait_ch = guild.get_channel(int(wait_id)) if wait_id else None

        main_empty = not main_ch or len(main_ch.members) == 0
        wait_empty = not wait_ch or len(wait_ch.members) == 0

        if main_empty and wait_empty:
            if not vc_row.get("last_empty_at"):
                _update_vc(vc_row["id"], {"last_empty_at": datetime.now(timezone.utc).isoformat()})
        else:
            if vc_row.get("last_empty_at"):
                _update_vc(vc_row["id"], {"last_empty_at": None})

    @tasks.loop(seconds=30)
    async def cleanup_task(self):
        try:
            for guild in self.bot.guilds:
                cfg = _get_config(str(guild.id))
                if not cfg:
                    continue
                timeout = cfg.get("empty_timeout", 30)
                for vc_row in _get_all_vcs(str(guild.id)):
                    last_empty = vc_row.get("last_empty_at")
                    if not last_empty:
                        if not guild.get_channel(int(vc_row["main_channel_id"])):
                            await _delete_vc_channels(guild, vc_row)
                        continue
                    try:
                        empty_since = datetime.fromisoformat(last_empty.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    if (datetime.now(timezone.utc) - empty_since).total_seconds() >= timeout:
                        await _delete_vc_channels(guild, vc_row)
        except Exception as e:
            logger.error(f"[voice_cleanup_task] {e}")

    @cleanup_task.before_loop
    async def before_cleanup(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceCog(bot))
