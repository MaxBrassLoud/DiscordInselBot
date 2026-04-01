"""
features/voice/cog.py
=====================
Voice Channel Creator – dynamische Voice-Kanäle on demand.

ABLAUF:
  • /voice_setup  – Admin richtet Erstell-Kanal + berechtigte Rollen ein.
  • User betritt den Erstell-Kanal:
      → Wird automatisch in einen neuen Hauptkanal verschoben (Standard: offen).
      → Im Text-Chat des Hauptkanals erscheint ein persistentes Panel.
  • Panel-Buttons (nur Besitzer oder berechtigte Rollen):
      🔒 / 🔓  Privat / Öffentlich umschalten
      👟       User kicken  (öffnet User-Select)
      👥       User-Limit setzen (nur offen)  [+ / - / Reset]
      🗑️       Kanal löschen
  • Warteraum (nur wenn privat):
      → Hauptkanal ist für ALLE sichtbar, aber nur ANGENOMMENE User können joinen.
      → Warteraum-Beitritt → Anfrage-Nachricht im Haupt-Kanal-Chat.
      → Besitzer / berechtigte Rollen: ✅ Annehmen | ❌ Ablehnen.
      → Annehmen: User bekommt dauerhaft connect=True auf dem Hauptkanal.
      → Ablehnen / Kick: connect-Berechtigung wird entzogen.
  • Cleanup-Task (alle 30s):
      → Haupt + Warteraum leer ≥ empty_timeout Sekunden → automatisch löschen.

SUPABASE SQL:
    CREATE TABLE IF NOT EXISTS voice_creator_config (
        id            BIGSERIAL PRIMARY KEY,
        server_id     TEXT NOT NULL UNIQUE,
        category_id   TEXT,
        channel_id    TEXT NOT NULL,
        channel_name  TEXT NOT NULL DEFAULT '➕  Kanal erstellen',
        empty_timeout    INTEGER NOT NULL DEFAULT 30,
        allowed_role_ids TEXT NOT NULL DEFAULT '',
        creator_role_ids TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS voice_channels (
        id              BIGSERIAL PRIMARY KEY,
        server_id       TEXT NOT NULL,
        owner_id        TEXT NOT NULL,
        main_channel_id TEXT NOT NULL,
        wait_channel_id TEXT,
        panel_message_id TEXT,
        is_open         BOOLEAN NOT NULL DEFAULT true,
        user_limit      INTEGER NOT NULL DEFAULT 0,
        created_at      TIMESTAMPTZ DEFAULT now(),
        last_empty_at   TIMESTAMPTZ
    );

    CREATE INDEX IF NOT EXISTS idx_voice_channels_server   ON voice_channels (server_id);
    CREATE INDEX IF NOT EXISTS idx_voice_channels_main     ON voice_channels (main_channel_id);
    CREATE INDEX IF NOT EXISTS idx_voice_channels_wait     ON voice_channels (wait_channel_id);
    CREATE INDEX IF NOT EXISTS idx_voice_channels_owner    ON voice_channels (server_id, owner_id);
"""

from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("voice")


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
# PERMISSION CHECK
# ══════════════════════════════════════════════════════════════════════════════

def _can_manage(member: discord.Member, vc_row: dict, cfg: dict) -> bool:
    """Besitzer ODER berechtigte Rolle ODER Server-Admin."""
    if member.guild_permissions.administrator:
        return True
    if str(member.id) == vc_row.get("owner_id"):
        return True
    allowed = set(_parse_role_ids(cfg.get("allowed_role_ids", "")))
    return bool(allowed & {str(r.id) for r in member.roles})


# ══════════════════════════════════════════════════════════════════════════════
# PRIVATE MODE PERMISSION HELPERS
#
# Im privaten Modus gilt:
#   • Hauptkanal: view_channel=True für alle (sichtbar)
#                 connect=False für default_role (nicht joinbar)
#   • Angenommene User: individuelle Overrides mit connect=True
#   • Gekickte / abgelehnte User: individuelle Overrides mit connect=False
# ══════════════════════════════════════════════════════════════════════════════

async def _grant_connect(channel: discord.VoiceChannel, member: discord.Member) -> None:
    """Gibt einem Member dauerhaft connect=True auf dem privaten Hauptkanal."""
    await channel.set_permissions(
        member,
        view_channel=True,
        connect=True,
    )


async def _revoke_connect(channel: discord.VoiceChannel, member: discord.Member) -> None:
    """
    Entzieht einem Member die Berechtigung dem privaten Hauptkanal beizutreten.
    Kanal bleibt sichtbar (view_channel=True, connect=False).
    """
    await channel.set_permissions(
        member,
        view_channel=True,
        connect=False,
    )


# ══════════════════════════════════════════════════════════════════════════════
# PANEL VIEW  (persistent im Voice-Text-Chat)
# ══════════════════════════════════════════════════════════════════════════════

def _build_panel_embed(vc_row: dict, guild: discord.Guild) -> discord.Embed:
    owner  = guild.get_member(int(vc_row["owner_id"]))
    is_open = vc_row.get("is_open", True)
    limit   = vc_row.get("user_limit", 0)

    color = discord.Color.green() if is_open else discord.Color.blurple()
    embed = discord.Embed(
        title="🎙️ Kanal-Steuerung",
        color=color,
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
    if not is_open and vc_row.get("wait_channel_id"):
        embed.add_field(
            name="⏳ Warteraum",
            value=f"<#{vc_row['wait_channel_id']}>",
            inline=False,
        )
    if not is_open:
        embed.add_field(
            name="ℹ️ Hinweis",
            value=(
                "Der Kanal ist für alle **sichtbar**, aber nur **angenommene** Mitglieder "
                "können beitreten. Alle anderen müssen den Warteraum nutzen."
            ),
            inline=False,
        )
    embed.set_footer(text="Nur Besitzer und berechtigte Rollen können die Buttons nutzen.")
    return embed


class VoicePanelView(discord.ui.View):
    """Persistentes Panel im Text-Chat des Hauptkanals."""

    def __init__(self):
        super().__init__(timeout=None)

    # ── Privat / Öffentlich togglen ───────────────────────────────────────────

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

        await interaction.response.defer(ephemeral=True)
        new_open = not vc_row.get("is_open", True)

        main_ch = interaction.guild.get_channel(int(vc_row["main_channel_id"]))
        if not main_ch:
            await interaction.followup.send("❌ Voice-Kanal nicht gefunden.", ephemeral=True)
            return

        if new_open:
            # ── Öffentlich ────────────────────────────────────────────────────
            # Alle dürfen rein → default_role connect=True.
            # Individuelle Overrides (von privatem Modus) belassen wir;
            # sie schaden nicht, da default_role jetzt eh erlaubt ist.
            await main_ch.set_permissions(
                interaction.guild.default_role,
                view_channel=True, connect=True,
            )

            # Warteraum löschen
            if vc_row.get("wait_channel_id"):
                wait_ch = interaction.guild.get_channel(int(vc_row["wait_channel_id"]))
                if wait_ch:
                    try:
                        await wait_ch.delete(reason="Kanal auf öffentlich gestellt")
                    except Exception:
                        pass
                _update_vc(vc_row["id"], {"is_open": True, "wait_channel_id": None})
            else:
                _update_vc(vc_row["id"], {"is_open": True})

        else:
            # ── Privat ────────────────────────────────────────────────────────
            # Hauptkanal: für alle SICHTBAR, aber nur angenommene User können joinen.

            # default_role: sehen ja, joinen nein
            await main_ch.set_permissions(
                interaction.guild.default_role,
                view_channel=True, connect=False,
            )

            # Besitzer: volle Rechte
            owner = interaction.guild.get_member(int(vc_row["owner_id"]))
            if owner:
                await main_ch.set_permissions(
                    owner,
                    view_channel=True, connect=True,
                    move_members=True, manage_channels=True,
                )

            # Bot: volle Rechte
            await main_ch.set_permissions(
                interaction.guild.me,
                view_channel=True, connect=True,
                move_members=True, manage_channels=True,
            )

            # Berechtigte Panel-Rollen (allowed_role_ids):
            # können joinen + Panel nutzen
            for role_id in _parse_role_ids(cfg.get("allowed_role_ids", "")):
                role = interaction.guild.get_role(int(role_id))
                if role:
                    await main_ch.set_permissions(
                        role,
                        view_channel=True, connect=True, move_members=True,
                    )

            # Warteraum erstellen
            wait_ch_id = vc_row.get("wait_channel_id")
            if not wait_ch_id:
                owner_display = owner.display_name if owner else "Kanal"
                wait_ow: dict = {
                    # Alle dürfen den Warteraum betreten (um anzuklopfen)
                    interaction.guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, connect=True, speak=False, send_messages=False,
                    ),
                    interaction.guild.me: discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                        move_members=True, manage_channels=True,
                    ),
                }
                # Besitzer darf Warteraum NICHT betreten
                if owner:
                    wait_ow[owner] = discord.PermissionOverwrite(
                        view_channel=True, connect=False,
                    )
                # Berechtigte Rollen sehen Warteraum, joinen unnötig (sie können direkt rein)
                for role_id in _parse_role_ids(cfg.get("allowed_role_ids", "")):
                    role = interaction.guild.get_role(int(role_id))
                    if role:
                        wait_ow[role] = discord.PermissionOverwrite(
                            view_channel=True, connect=False,
                        )

                category = main_ch.category
                wait_ch  = await interaction.guild.create_voice_channel(
                    name=f"⏳ Warteraum – {owner_display}",
                    category=category,
                    overwrites=wait_ow,
                    reason="Kanal auf privat gestellt",
                )
                wait_ch_id = str(wait_ch.id)

            _update_vc(vc_row["id"], {"is_open": False, "wait_channel_id": wait_ch_id, "user_limit": 0})

        # Panel aktualisieren
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        await self._refresh_panel(interaction, vc_row)
        await interaction.followup.send(
            f"{'🔓 Kanal ist jetzt öffentlich.' if new_open else '🔒 Kanal ist jetzt privat. Der Kanal ist für alle sichtbar, aber nur angenommene Mitglieder können beitreten.'}",
            ephemeral=True,
        )

    # ── User kicken ───────────────────────────────────────────────────────────

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
        members_in_vc = [m for m in (main_ch.members if main_ch else [])
                         if str(m.id) != vc_row["owner_id"]]

        if not members_in_vc:
            await interaction.response.send_message(
                "ℹ️ Keine anderen Mitglieder im Kanal.", ephemeral=True
            )
            return

        view = _KickSelectView(members_in_vc, main_ch, vc_row)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="👟 User aus dem Kanal entfernen",
                description=(
                    "Wähle einen oder mehrere Nutzer die aus dem Voice-Kanal entfernt werden sollen.\n"
                    "Im **privaten Modus** wird ihnen auch die Berechtigung entzogen, wieder beizutreten."
                ),
                color=discord.Color.red(),
            ),
            view=view,
            ephemeral=True,
        )

    # ── User-Limit ─────────────────────────────────────────────────────────────

    @discord.ui.button(
        label="👥 User-Limit",
        style=discord.ButtonStyle.secondary,
        custom_id="vpc_user_limit",
        row=0,
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
        view    = _LimitView(vc_row, current)
        await interaction.response.send_message(
            embed=discord.Embed(
                title="👥 User-Limit festlegen",
                description=f"Aktuelles Limit: **{current if current > 0 else 'Kein Limit'}**\n\n"
                            "Nutze die Buttons um das Limit anzupassen.",
                color=discord.Color.blurple(),
            ),
            view=view,
            ephemeral=True,
        )

    # ── Kanal löschen ─────────────────────────────────────────────────────────

    @discord.ui.button(
        label="🗑️ Kanal löschen",
        style=discord.ButtonStyle.danger,
        custom_id="vpc_delete_channel",
        row=0,
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
                description="Der Voice-Kanal und der Warteraum werden sofort gelöscht.",
                color=discord.Color.red(),
            ),
            view=_DeleteConfirmView(vc_row),
            ephemeral=True,
        )

    # ── Intern: Panel-Nachricht aktualisieren ─────────────────────────────────

    async def _refresh_panel(self, interaction: discord.Interaction, vc_row: dict):
        if not vc_row or not vc_row.get("panel_message_id"):
            return
        try:
            ch  = interaction.guild.get_channel(int(vc_row["main_channel_id"]))
            msg = await ch.fetch_message(int(vc_row["panel_message_id"]))
            await msg.edit(embed=_build_panel_embed(vc_row, interaction.guild), view=VoicePanelView())
        except Exception as e:
            logger.error(f"[vpc] Panel-Refresh fehlgeschlagen: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# KICK SELECT VIEW
# Beim Kick im privaten Modus wird connect=False gesetzt → User kann nicht mehr joinen
# ══════════════════════════════════════════════════════════════════════════════

class _KickSelectView(discord.ui.View):
    def __init__(self, members: list[discord.Member], channel: discord.VoiceChannel,
                 vc_row: dict):
        super().__init__(timeout=60)
        self.channel = channel
        self.vc_row  = vc_row

        options = [
            discord.SelectOption(
                label=m.display_name[:100],
                value=str(m.id),
                description=f"@{m.name}",
            )
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
        kicked = []
        is_private = not self.vc_row.get("is_open", True)

        for uid in interaction.data["values"]:
            member = interaction.guild.get_member(int(uid))
            if not member:
                continue
            # Aus dem Kanal entfernen
            if member.voice and member.voice.channel == self.channel:
                try:
                    await member.move_to(None, reason="Voice-Kick durch Kanalbesitzer")
                    kicked.append(member.display_name)
                except Exception as e:
                    logger.error(f"[kick] move_to: {e}")
            else:
                kicked.append(member.display_name)

            # Im privaten Modus: connect-Berechtigung entziehen
            if is_private:
                try:
                    await _revoke_connect(self.channel, member)
                except Exception as e:
                    logger.error(f"[kick] revoke_connect: {e}")

        self.stop()
        names = ", ".join(kicked) if kicked else "Niemand"
        extra = "\nIm privaten Modus können sie nicht mehr beitreten." if is_private and kicked else ""
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

    @discord.ui.button(label="➕ +1", style=discord.ButtonStyle.success)
    async def plus_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = min(99, self.current + 1)
        await self._apply(interaction)

    @discord.ui.button(label="➕ +5", style=discord.ButtonStyle.success)
    async def plus_five(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = min(99, self.current + 5)
        await self._apply(interaction)

    @discord.ui.button(label="➖ -1", style=discord.ButtonStyle.danger)
    async def minus_one(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = max(0, self.current - 1)
        await self._apply(interaction)

    @discord.ui.button(label="➖ -5", style=discord.ButtonStyle.danger)
    async def minus_five(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = max(0, self.current - 5)
        await self._apply(interaction)

    @discord.ui.button(label="🔄 Reset", style=discord.ButtonStyle.secondary)
    async def reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current = 0
        await self._apply(interaction)

    async def _apply(self, interaction: discord.Interaction):
        main_ch = interaction.guild.get_channel(int(self.vc_row["main_channel_id"]))
        if main_ch:
            try:
                await main_ch.edit(user_limit=self.current)
            except Exception as e:
                logger.error(f"[limit] {e}")
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
            embed=discord.Embed(title="Abgebrochen", color=discord.Color.green()),
            view=None,
        )


# ══════════════════════════════════════════════════════════════════════════════
# WARTERAUM-ANFRAGE VIEW
#
# Erscheint im Text-Chat des Hauptkanals wenn jemand den Warteraum betritt.
#
# ✅ Annehmen → _grant_connect() → dauerhaftes connect=True auf Hauptkanal
# ❌ Ablehnen → User wird aus Warteraum entfernt, KEINE connect-Berechtigung
# ══════════════════════════════════════════════════════════════════════════════

class WaitingRoomRequestView(discord.ui.View):
    def __init__(
        self,
        requester: discord.Member,
        main_ch: discord.VoiceChannel,
        wait_ch: discord.VoiceChannel,
        vc_row: dict,
        cfg: dict,
    ):
        super().__init__(timeout=120)
        self.requester = requester
        self.main_ch   = main_ch
        self.wait_ch   = wait_ch
        self.vc_row    = vc_row
        self.cfg       = cfg
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

        try:
            # Dauerhafte connect-Berechtigung vergeben
            await _grant_connect(self.main_ch, self.requester)
            # User in den Hauptkanal verschieben
            if self._still_waiting():
                await self.requester.move_to(self.main_ch)
        except Exception as e:
            logger.error(f"[accept] {e}")

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"✅ {self.requester.display_name} wurde angenommen",
                description="Der User hat dauerhaft Zugang zu diesem Kanal.",
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

        try:
            # Aus Warteraum entfernen – keine connect-Berechtigung wird gesetzt,
            # der User bleibt auf dem default_role-Niveau (sehen, nicht joinen).
            if self._still_waiting():
                await self.requester.move_to(None)
        except Exception as e:
            logger.error(f"[deny] {e}")

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"❌ {self.requester.display_name} wurde abgelehnt",
                description="Der User kann weiterhin den Warteraum nutzen um erneut anzufragen.",
                color=discord.Color.red(),
            ),
            view=self,
        )

    async def on_timeout(self):
        if self._done:
            return
        self._done = True
        try:
            if self._still_waiting():
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
    """
    Erstellt einen öffentlichen Hauptkanal + sendet das Panel im Text-Chat.
    Kein Warteraum initial – User kann per Panel auf privat stellen.
    Berechtigte Rollen können immer joinen.
    """
    existing = _get_vc_by_owner(str(guild.id), str(member.id))
    if existing:
        main_ch = guild.get_channel(int(existing["main_channel_id"]))
        if main_ch and member.voice:
            try:
                await member.move_to(main_ch)
            except Exception:
                pass
        return

    category_id = cfg.get("category_id")
    category    = guild.get_channel(int(category_id)) if category_id else None
    base_name   = member.display_name

    allowed_role_ids = _parse_role_ids(cfg.get("allowed_role_ids", ""))

    # Hauptkanal – Standard: öffentlich (alle dürfen sehen + joinen)
    main_ow = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, connect=True,
        ),
        member: discord.PermissionOverwrite(
            view_channel=True, connect=True,
            move_members=True, manage_channels=True,
        ),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True,
            move_members=True, manage_channels=True,
        ),
    }
    for role_id in allowed_role_ids:
        role = guild.get_role(int(role_id))
        if role:
            main_ow[role] = discord.PermissionOverwrite(
                view_channel=True, connect=True, move_members=True,
            )

    main_ch = await guild.create_voice_channel(
        name=f"🎙️ {base_name}",
        category=category,
        overwrites=main_ow,
        reason=f"Voice Creator – {member.display_name}",
    )

    if member.voice:
        try:
            await member.move_to(main_ch)
        except Exception as e:
            logger.warning(f"[voice] Move fehlgeschlagen: {e}")

    vc_data = _save_vc({
        "server_id":        str(guild.id),
        "owner_id":         str(member.id),
        "main_channel_id":  str(main_ch.id),
        "wait_channel_id":  None,
        "panel_message_id": None,
        "is_open":          True,
        "user_limit":       0,
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "last_empty_at":    None,
    })

    try:
        panel_embed = _build_panel_embed(vc_data, guild)
        panel_view  = VoicePanelView()
        panel_msg   = await main_ch.send(embed=panel_embed, view=panel_view)
        _update_vc(vc_data["id"], {"panel_message_id": str(panel_msg.id)})
        try:
            await panel_msg.pin()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[voice] Panel senden fehlgeschlagen: {e}")

    logger.info(f"[voice] Erstellt für {member.display_name}: main={main_ch.id}")


# ══════════════════════════════════════════════════════════════════════════════
# CLEANUP
# ══════════════════════════════════════════════════════════════════════════════

async def _delete_vc_channels(guild: discord.Guild, vc_row: dict):
    for ch_id in filter(None, [vc_row.get("main_channel_id"), vc_row.get("wait_channel_id")]):
        ch = guild.get_channel(int(ch_id))
        if ch:
            try:
                await ch.delete(reason="Voice Creator – Cleanup")
            except discord.NotFound:
                pass
            except Exception as e:
                logger.warning(f"[cleanup] {ch_id}: {e}")
    _delete_vc(vc_row["id"])
    logger.info(f"[voice] Cleanup: owner={vc_row['owner_id']}")


# ══════════════════════════════════════════════════════════════════════════════
# SETUP VIEW
# ══════════════════════════════════════════════════════════════════════════════

class VoiceSetupView(discord.ui.View):
    def __init__(self, guild_id: int, bot: discord.Client):
        super().__init__(timeout=300)
        self.guild_id        = str(guild_id)
        self.bot             = bot
        self.category_id:    str | None   = None
        self.channel_name:   str          = "➕  Kanal erstellen"
        self.empty_timeout:  int          = 30
        self.allowed_roles:  list[str]    = []
        self.creator_roles:  list[str]    = []
        self._rebuild()

    def _build_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🎙️ Voice Creator Setup",
            description=(
                "Richtet einen **Join-to-Create** Voice-Kanal ein.\n"
                "Tritt ein User diesem Kanal bei, erstellt der Bot automatisch\n"
                "einen eigenen Kanal mit Panel zur Steuerung.\n\n"
                "**Privater Modus:** Hauptkanal ist für alle **sichtbar**, "
                "aber nur **angenommene** Mitglieder können beitreten. "
                "Alle anderen müssen über den Warteraum anfragen."
            ),
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="📁 Kategorie",
            value=f"<#{self.category_id}>" if self.category_id else "*Server-Root*",
            inline=True,
        )
        e.add_field(name="🎙️ Kanal-Name", value=f"`{self.channel_name}`", inline=True)
        e.add_field(name="⏱️ Timeout",    value=f"{self.empty_timeout}s", inline=True)
        e.add_field(
            name="🎙️ Erstell-Berechtigung",
            value=(
                ", ".join(f"<@&{r}>" for r in self.creator_roles)
                if self.creator_roles else "*Jeder darf einen Kanal erstellen*"
            ),
            inline=False,
        )
        e.add_field(
            name="🔑 Panel-Rollen (immer joinen + Panel nutzen)",
            value=(
                ", ".join(f"<@&{r}>" for r in self.allowed_roles)
                if self.allowed_roles else "*keine*"
            ),
            inline=False,
        )
        return e

    def _rebuild(self):
        self.clear_items()

        cat_sel = discord.ui.ChannelSelect(
            placeholder="📁 Kategorie für Kanäle (optional)",
            min_values=0, max_values=1,
            channel_types=[discord.ChannelType.category],
            row=0,
        )
        cat_sel.callback = self._cb_category
        self.add_item(cat_sel)

        creator_sel = discord.ui.RoleSelect(
            placeholder="🎙️ Erstell-Rollen: Wer darf Kanäle erstellen? (leer = jeder)",
            min_values=0, max_values=10,
            row=1,
        )
        creator_sel.callback = self._cb_creator_roles
        self.add_item(creator_sel)

        role_sel = discord.ui.RoleSelect(
            placeholder="🔑 Panel-Rollen: können immer joinen + Panel nutzen",
            min_values=0, max_values=10,
            row=2,
        )
        role_sel.callback = self._cb_roles
        self.add_item(role_sel)

        btn_name = discord.ui.Button(
            label="✏️ Kanal-Namen ändern",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        btn_name.callback = self._cb_name
        self.add_item(btn_name)

        btn_timeout = discord.ui.Button(
            label=f"⏱️ Timeout: {self.empty_timeout}s",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        btn_timeout.callback = self._cb_timeout
        self.add_item(btn_timeout)

        save_btn = discord.ui.Button(
            label="🚀 Setup abschließen",
            style=discord.ButtonStyle.success,
            row=4,
        )
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

            cfg = _get_config(str(guild.id))
            if cfg and cfg.get("channel_id"):
                old_ch = guild.get_channel(int(cfg["channel_id"]))
                if old_ch:
                    try:
                        await old_ch.delete(reason="Voice Creator – Neu eingerichtet")
                    except Exception:
                        pass

            ow = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True, connect=True, speak=False,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True, connect=True,
                    move_members=True, manage_channels=True,
                ),
            }
            for role_id in self.allowed_roles:
                role = guild.get_role(int(role_id))
                if role:
                    ow[role] = discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                    )

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

        except Exception as e:
            logger.error(f"[VoiceSetupView._cb_save] {e}")
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class _NameModal(discord.ui.Modal, title="Erstell-Kanal umbenennen"):
    name = discord.ui.TextInput(
        label="Name des Join-to-Create Kanals",
        placeholder="z.B. ➕  Kanal erstellen",
        required=True, max_length=100,
    )

    def __init__(self, view: VoiceSetupView):
        super().__init__()
        self._view = view
        self.name.default = view.channel_name

    async def on_submit(self, interaction: discord.Interaction):
        self._view.channel_name = self.name.value
        self._view._rebuild()
        await interaction.response.edit_message(
            embed=self._view._build_embed(), view=self._view
        )


class _TimeoutModal(discord.ui.Modal, title="Leerlauf-Timeout festlegen"):
    timeout = discord.ui.TextInput(
        label="Sekunden bis Kanäle gelöscht werden (min. 10)",
        placeholder="z.B. 30",
        required=True, max_length=6,
    )

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
        await interaction.response.edit_message(
            embed=self._view._build_embed(), view=self._view
        )


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

    @app_commands.command(
        name="voice_setup",
        description="Richte den automatischen Voice Channel Creator ein",
    )
    async def voice_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        view = VoiceSetupView(guild_id=interaction.guild_id, bot=self.bot)
        await interaction.response.send_message(
            embed=view._build_embed(), view=view, ephemeral=True
        )

    @app_commands.command(
        name="voice_info",
        description="Aktive Voice-Creator-Kanäle und Konfiguration",
    )
    async def voice_info(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)

        cfg = _get_config(str(interaction.guild_id))
        if not cfg:
            await interaction.followup.send(
                "❌ Nicht eingerichtet. Nutze `/voice_setup`.", ephemeral=True
            )
            return

        vcs   = _get_all_vcs(str(interaction.guild_id))
        roles = _parse_role_ids(cfg.get("allowed_role_ids", ""))
        embed = discord.Embed(title="🎙️ Voice Creator – Übersicht", color=discord.Color.blurple())
        cat_val = f"<#{cfg['category_id']}>" if cfg.get("category_id") else "*keine*"
        embed.add_field(
            name="⚙️ Konfiguration",
            value=(
                f"Erstell-Kanal: <#{cfg['channel_id']}>\n"
                f"Kategorie: {cat_val}\n"
                f"Timeout: {cfg.get('empty_timeout', 30)}s\n"
                f"Berechtigte Rollen: {', '.join(f'<@&{r}>' for r in roles) or '*keine*'}"
            ),
            inline=False,
        )
        for vc in vcs:
            owner = interaction.guild.get_member(int(vc["owner_id"]))
            wait_val  = f"<#{vc['wait_channel_id']}>" if vc.get("wait_channel_id") else "*keiner*"
            mode_icon = "🔓" if vc["is_open"] else "🔒"
            embed.add_field(
                name=f"{mode_icon} <#{vc['main_channel_id']}>",
                value=(
                    f"Besitzer: {owner.mention if owner else vc['owner_id']}\n"
                    f"Warteraum: {wait_val}\n"
                    f"Limit: {vc.get('user_limit') or 'kein'}"
                ),
                inline=True,
            )
        if not vcs:
            embed.add_field(name="Aktive Kanäle", value="*Keine*", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        server_id     = str(member.guild.id)
        guild         = member.guild
        cfg           = _get_config(server_id)
        if not cfg:
            return

        creator_ch_id = cfg.get("channel_id")

        # ── 1. User betritt den Erstell-Kanal ─────────────────────────────────
        if after.channel and str(after.channel.id) == creator_ch_id:
            creator_role_ids = set(_parse_role_ids(cfg.get("creator_role_ids", "")))
            if creator_role_ids:
                user_role_ids = {str(r.id) for r in member.roles}
                if not (member.guild_permissions.administrator or creator_role_ids & user_role_ids):
                    try:
                        await member.move_to(None)
                    except Exception:
                        pass
                    try:
                        await member.send(
                            embed=discord.Embed(
                                title="❌ Keine Berechtigung",
                                description="Du hast nicht die nötige Rolle um einen eigenen Voice-Kanal zu erstellen.",
                                color=discord.Color.red(),
                            )
                        )
                    except discord.Forbidden:
                        pass
                    return
            await _create_voice_channels(guild=guild, member=member, cfg=cfg, bot=self.bot)
            return

        # ── 2. User betritt den Warteraum ─────────────────────────────────────
        if after.channel and str(after.channel.id) != creator_ch_id:
            vc_row = _get_vc_by_wait(str(after.channel.id))
            if vc_row:
                await self._handle_waitroom_join(guild, member, vc_row, cfg)

        # ── 3. User verlässt Kanal → Leerlauf prüfen ──────────────────────────
        if before.channel and str(before.channel.id) != creator_ch_id:
            vc_row = _get_vc_by_main(str(before.channel.id))
            if vc_row:
                await self._check_empty(guild, vc_row)
                return
            vc_row = _get_vc_by_wait(str(before.channel.id))
            if vc_row:
                await self._check_empty(guild, vc_row)

    async def _handle_waitroom_join(
        self, guild: discord.Guild, member: discord.Member, vc_row: dict, cfg: dict
    ):
        """
        User betritt Warteraum.
        • Besitzer / berechtigte Rollen → direkt in den Hauptkanal verschieben.
        • Alle anderen → Anfrage im Text-Chat des Hauptkanals posten.
        """
        main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
        wait_ch = guild.get_channel(int(vc_row["wait_channel_id"]))
        if not main_ch or not wait_ch:
            return

        # Besitzer oder berechtigte Rollen → direkt rein
        if _can_manage(member, vc_row, cfg):
            try:
                await _grant_connect(main_ch, member)
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom auto-join] {e}")
            return

        # Prüfen ob der User bereits eine explizite connect=True Berechtigung hat
        # (wurde schon einmal angenommen und nicht gekickt)
        overwrite = main_ch.overwrites_for(member)
        if overwrite.connect is True:
            # Bereits angenommen → direkt verschieben
            try:
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom re-join] {e}")
            return

        # Anfrage-Nachricht senden
        embed = discord.Embed(
            title="🔔 Beitrittsanfrage",
            description=(
                f"**{member.mention}** (`{member.display_name}`) möchte\n"
                f"dem Kanal **{main_ch.name}** beitreten."
            ),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Anfrage läuft nach 2 Minuten ab → wird automatisch abgelehnt.")

        view = WaitingRoomRequestView(
            requester=member, main_ch=main_ch, wait_ch=wait_ch,
            vc_row=vc_row, cfg=cfg,
        )
        try:
            await main_ch.send(embed=embed, view=view)
        except Exception as e:
            logger.error(f"[waitroom] Anfrage senden fehlgeschlagen: {e}")

    async def _check_empty(self, guild: discord.Guild, vc_row: dict):
        main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
        wait_id = vc_row.get("wait_channel_id")
        wait_ch = guild.get_channel(int(wait_id)) if wait_id else None

        main_empty = not main_ch or len(main_ch.members) == 0
        wait_empty = not wait_ch or len(wait_ch.members) == 0

        if main_empty and wait_empty:
            if not vc_row.get("last_empty_at"):
                _update_vc(vc_row["id"], {
                    "last_empty_at": datetime.now(timezone.utc).isoformat()
                })
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