"""
features/voice/cog.py
=====================
Voice Channel Creator – dynamische Voice-Kanäle on demand.
Läuft vollständig OHNE Administrator-Rechte.

WARUM KEIN set_permissions() FÜR MEMBER?
  Discord verbietet es, Channel-Overwrites für Member oder Rollen zu setzen,
  wenn der Bot kein Administrator ist. manage_roles auf Server-Ebene reicht
  dafür nicht – Discord verlangt einen expliziten manage_roles-Overwrite AUF
  DEM KANAL, und den darf ein Bot ohne Admin nur für sich selbst setzen.

LÖSUNG – ZUGANGS-ROLLEN-SYSTEM (kein Admin nötig):
  Beim Wechsel auf privat erstellt der Bot eine temporäre Discord-Rolle
  "🔒 VC-Zugang – <Kanalname>" direkt UNTER seiner eigenen Rolle.
  Der Kanal-Overwrite für diese Rolle (connect=True) wird beim Erstellen
  des privaten Kanals einmalig gesetzt – das ist erlaubt weil der Bot die
  Rolle selbst erstellt hat und sie unterhalb seiner Rolle liegt.

  • Annehmen  → Zugangs-Rolle an Member vergeben  (add_roles)
  • Kick      → Zugangs-Rolle entziehen            (remove_roles)
  • Ablehnen  → keine Rolle → User bleibt gesperrt

  Der Bot braucht dafür nur:
    ✅ Manage Roles       (Rollen erstellen / vergeben / entziehen)
    ✅ Manage Channels    (Kanäle erstellen / löschen)
    ✅ Move Members       (User zwischen Voice-Kanälen verschieben)
    ✅ View Channel       (Kanäle sehen)
    ✅ Connect            (Voice-Kanälen beitreten)
    ✅ Send Messages      (Panel senden)
    ✅ Read Message History

SUPABASE SQL (Ergänzung – einmalig ausführen):
  ALTER TABLE voice_channels ADD COLUMN IF NOT EXISTS access_role_id TEXT;
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger
from bot.utils.permissions import has_admin_rights

logger = get_logger("voice")

# ── Per-channel toggle locks (prevents race condition on simultaneous presses) ─
# Module-level dict survives across VoicePanelView instances (persistent views
# create a new object per interaction, so instance-level locks don't work).
_toggle_locks: dict[str, asyncio.Lock] = {}


def _get_toggle_lock(channel_id: str) -> asyncio.Lock:
    """Returns (and lazily creates) an asyncio.Lock for a given channel ID."""
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


def _has_access_role(member: discord.Member, vc_row: dict) -> bool:
    """Prüft ob der Member die Zugangs-Rolle für diesen privaten Kanal hat."""
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
            reason="Voice Creator – temporäre Zugangs-Rolle für privaten Kanal",
            mentionable=False,
            hoist=False,
        )
        bot_top = guild.me.top_role
        if bot_top.position > 1:
            try:
                await role.edit(position=max(1, bot_top.position - 1))
            except discord.HTTPException:
                pass
        logger.info(f"[voice] Zugangs-Rolle erstellt: {role.name} ({role.id})")
        return role
    except discord.Forbidden:
        logger.error(
            "[voice] _create_access_role: Forbidden – "
            "Bot braucht 'Manage Roles' auf Server-Ebene."
        )
        return None
    except discord.HTTPException as e:
        logger.error(f"[voice] _create_access_role HTTP-Fehler: {e}")
        return None


async def _delete_access_role(guild: discord.Guild, role_id: str | None) -> None:
    if not role_id:
        return
    try:
        role = guild.get_role(int(role_id))
        if role:
            await role.delete(reason="Voice Creator – Kanal gelöscht")
            logger.info(f"[voice] Zugangs-Rolle {role_id} gelöscht")
    except discord.Forbidden:
        logger.warning(f"[voice] _delete_access_role: Forbidden für {role_id}")
    except discord.HTTPException as e:
        logger.error(f"[voice] _delete_access_role HTTP-Fehler: {e}")


async def _grant_access(
    guild: discord.Guild,
    member: discord.Member,
    access_role_id: str,
) -> None:
    role = guild.get_role(int(access_role_id))
    if not role:
        logger.warning(f"[voice] _grant_access: Rolle {access_role_id} nicht gefunden")
        return
    try:
        await member.add_roles(role, reason="Voice Creator – Zugang gewährt")
    except discord.Forbidden:
        logger.error(
            f"[voice] _grant_access: Forbidden für {member} – "
            "Bot-Rolle muss über der Zugangs-Rolle liegen."
        )
    except discord.HTTPException as e:
        logger.error(f"[voice] _grant_access HTTP-Fehler: {e}")


async def _revoke_access(
    guild: discord.Guild,
    member: discord.Member,
    access_role_id: str,
) -> None:
    role = guild.get_role(int(access_role_id))
    if not role:
        return
    try:
        await member.remove_roles(role, reason="Voice Creator – Zugang entzogen")
    except discord.Forbidden:
        logger.error(f"[voice] _revoke_access: Forbidden für {member}")
    except discord.HTTPException as e:
        logger.error(f"[voice] _revoke_access HTTP-Fehler: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# PANEL EMBED
# ══════════════════════════════════════════════════════════════════════════════

def _build_panel_embed(vc_row: dict, guild: discord.Guild) -> discord.Embed:
    owner   = guild.get_member(int(vc_row["owner_id"]))
    is_open = vc_row.get("is_open", True)
    limit   = vc_row.get("user_limit", 0)

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
        embed.add_field(
            name="⏳ Warteraum",
            value=f"<#{vc_row['wait_channel_id']}>" if vc_row.get("wait_channel_id") else "*–*",
            inline=False,
        )
        embed.add_field(
            name="ℹ️ Hinweis",
            value=(
                "Der Kanal ist für alle **sichtbar**, aber nur **angenommene** Mitglieder "
                "können beitreten. Neue Mitglieder müssen den Warteraum nutzen."
            ),
            inline=False,
        )
    embed.set_footer(text="Nur Besitzer und berechtigte Rollen können die Buttons nutzen.")
    return embed


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

    # ── 🔒 / 🔓 Toggle ───────────────────────────────────────────────────────

    @discord.ui.button(
        label="🔒 Privat / 🔓 Öffentlich",
        style=discord.ButtonStyle.primary,
        custom_id="vpc_toggle_private",
        row=0,
    )
    async def toggle_private(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ── Basic checks BEFORE deferring (must respond within 3s) ───────────
        cfg    = _get_config(str(interaction.guild_id))
        vc_row = _get_vc_by_main(str(interaction.channel_id))
        if not cfg or not vc_row:
            await interaction.response.send_message("❌ Kanal nicht gefunden.", ephemeral=True)
            return
        if not _can_manage(interaction.user, vc_row, cfg):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return

        # ── Per-channel lock ──────────────────────────────────────────────────
        # Module-level dict ensures the same lock object is used across all
        # VoicePanelView instances (persistent views create new objects per click).
        channel_key = str(interaction.channel_id)
        lock = _get_toggle_lock(channel_key)

        if lock.locked():
            # Another toggle is already in progress – respond immediately
            # so Discord doesn't show "interaction failed".
            await interaction.response.send_message(
                "⏳ Der Kanal wird gerade umgestellt – bitte einen Moment warten.",
                ephemeral=True,
            )
            return

        async with lock:
            # ── Re-fetch inside lock: always use latest DB state ──────────────
            vc_row = _get_vc_by_main(channel_key)
            if not vc_row:
                await interaction.response.send_message(
                    "❌ Kanal nicht mehr gefunden.", ephemeral=True
                )
                return

            current_is_open = vc_row.get("is_open", True)
            new_open = not current_is_open

            # ── Optimistic DB lock: flip the flag immediately ─────────────────
            # Any concurrent request that slips past lock.locked() will re-fetch
            # and see the already-flipped value, so it won't create a second
            # waitroom / access role.
            _update_vc(vc_row["id"], {"is_open": new_open})

            # Safe to defer now – we own the operation
            await interaction.response.defer(ephemeral=True)

            guild   = interaction.guild
            main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
            if not main_ch:
                # Roll back
                _update_vc(vc_row["id"], {"is_open": current_is_open})
                await interaction.followup.send("❌ Voice-Kanal nicht gefunden.", ephemeral=True)
                return

            owner = guild.get_member(int(vc_row["owner_id"]))

            if new_open:
                # ── Öffentlich ────────────────────────────────────────────────
                try:
                    await main_ch.set_permissions(
                        guild.default_role,
                        view_channel=True, connect=True,
                    )
                except discord.Forbidden:
                    logger.warning("[voice] toggle→öffentlich: Forbidden bei default_role")

                # Warteraum löschen
                if vc_row.get("wait_channel_id"):
                    wait_ch = guild.get_channel(int(vc_row["wait_channel_id"]))
                    if wait_ch:
                        try:
                            await wait_ch.delete(reason="Kanal auf öffentlich gestellt")
                        except Exception:
                            pass

                # Zugangs-Rolle löschen
                await _delete_access_role(guild, vc_row.get("access_role_id"))
                _update_vc(vc_row["id"], {
                    "is_open": True,
                    "wait_channel_id": None,
                    "access_role_id": None,
                })
                msg = "🔓 Kanal ist jetzt öffentlich."

            else:
                # ── Privat ────────────────────────────────────────────────────

                # Zugangs-Rolle erstellen
                access_role = await _create_access_role(guild, main_ch.name)
                if not access_role:
                    # Roll back
                    _update_vc(vc_row["id"], {"is_open": True})
                    await interaction.followup.send(
                        "❌ Konnte Zugangs-Rolle nicht erstellen.\n"
                        "Stelle sicher dass der Bot **Manage Roles** auf Server-Ebene hat.",
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
                        view_channel=True, connect=True,
                        move_members=True, manage_channels=True,
                    ),
                }
                if owner:
                    new_overwrites[owner] = discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                        move_members=True, manage_channels=True,
                    )
                for rid in _parse_role_ids(cfg.get("allowed_role_ids", "")):
                    role = guild.get_role(int(rid))
                    if role:
                        new_overwrites[role] = discord.PermissionOverwrite(
                            view_channel=True, connect=True, move_members=True,
                        )

                try:
                    await main_ch.edit(
                        overwrites=new_overwrites,
                        sync_permissions=False,
                        reason="Voice Creator – Kanal auf privat gestellt",
                    )
                except discord.Forbidden:
                    await _delete_access_role(guild, str(access_role.id))
                    _update_vc(vc_row["id"], {"is_open": True})
                    await interaction.followup.send(
                        "❌ Konnte Kanal-Berechtigungen nicht setzen.\n"
                        "Stelle sicher dass der Bot **Manage Channels** hat.",
                        ephemeral=True,
                    )
                    return

                # Aktuelle Mitglieder bekommen sofort die Zugangs-Rolle
                for m in main_ch.members:
                    if m.id != guild.me.id:
                        await _grant_access(guild, m, str(access_role.id))

                # Warteraum erstellen
                owner_display = owner.display_name if owner else "Kanal"
                wait_overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True, connect=True, speak=False, send_messages=False,
                    ),
                    guild.me: discord.PermissionOverwrite(
                        view_channel=True, connect=True,
                        move_members=True, manage_channels=True,
                    ),
                    access_role: discord.PermissionOverwrite(
                        view_channel=True, connect=False,
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
                            view_channel=True, connect=False,
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
                    await interaction.followup.send(
                        "❌ Konnte Warteraum nicht erstellen.\n"
                        "Stelle sicher dass der Bot **Manage Channels** hat.",
                        ephemeral=True,
                    )
                    return

                _update_vc(vc_row["id"], {
                    "is_open": False,
                    "wait_channel_id": str(wait_ch.id),
                    "access_role_id": str(access_role.id),
                    "user_limit": 0,
                })
                msg = (
                    "🔒 Kanal ist jetzt privat. Aktuelle Mitglieder behalten Zugang. "
                    "Neue Mitglieder müssen über den Warteraum anfragen."
                )

            vc_row = _get_vc_by_main(channel_key)
            await self._refresh_panel(interaction, vc_row)
            await interaction.followup.send(msg, ephemeral=True)

    # ── 👟 Kick ───────────────────────────────────────────────────────────────

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
                    + ("Im **privaten Modus** wird der Zugang dauerhaft entzogen."
                       if is_private else "")
                ),
                color=discord.Color.red(),
            ),
            view=view,
            ephemeral=True,
        )

    # ── 👥 Limit ──────────────────────────────────────────────────────────────

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
        await interaction.response.send_message(
            embed=discord.Embed(
                title="👥 User-Limit festlegen",
                description=f"Aktuelles Limit: **{current if current > 0 else 'Kein Limit'}**",
                color=discord.Color.blurple(),
            ),
            view=_LimitView(vc_row, current),
            ephemeral=True,
        )

    # ── 🗑️ Löschen ───────────────────────────────────────────────────────────

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
    def __init__(
        self,
        members: list[discord.Member],
        channel: discord.VoiceChannel,
        vc_row: dict,
        is_private: bool,
    ):
        super().__init__(timeout=60)
        self.channel    = channel
        self.vc_row     = vc_row
        self.is_private = is_private

        options = [
            discord.SelectOption(
                label=m.display_name[:100], value=str(m.id), description=f"@{m.name}"
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
        access_role_id = self.vc_row.get("access_role_id")
        kicked = []
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
                await _revoke_access(interaction.guild, member, access_role_id)

        self.stop()
        names = ", ".join(kicked) if kicked else "Niemand"
        extra = "\nZugang dauerhaft entzogen." if self.is_private and kicked else ""
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

    @discord.ui.button(label="➕ +1",  style=discord.ButtonStyle.success)
    async def plus_one(self, i, b):   self.current = min(99, self.current + 1); await self._apply(i)

    @discord.ui.button(label="➕ +5",  style=discord.ButtonStyle.success)
    async def plus_five(self, i, b):  self.current = min(99, self.current + 5); await self._apply(i)

    @discord.ui.button(label="➖ -1",  style=discord.ButtonStyle.danger)
    async def minus_one(self, i, b):  self.current = max(0, self.current - 1);  await self._apply(i)

    @discord.ui.button(label="➖ -5",  style=discord.ButtonStyle.danger)
    async def minus_five(self, i, b): self.current = max(0, self.current - 5);  await self._apply(i)

    @discord.ui.button(label="🔄 Reset", style=discord.ButtonStyle.secondary)
    async def reset(self, i, b):      self.current = 0;                          await self._apply(i)

    async def _apply(self, interaction: discord.Interaction):
        main_ch = interaction.guild.get_channel(int(self.vc_row["main_channel_id"]))
        if main_ch:
            try:
                await main_ch.edit(user_limit=self.current)
            except discord.Forbidden:
                logger.warning("[voice] _LimitView: Forbidden beim user_limit edit")
        _update_vc(self.vc_row["id"], {"user_limit": self.current})
        fresh = _get_vc_by_main(str(self.vc_row["main_channel_id"]))
        if fresh and fresh.get("panel_message_id") and main_ch:
            try:
                msg = await main_ch.fetch_message(int(fresh["panel_message_id"]))
                await msg.edit(
                    embed=_build_panel_embed(fresh, interaction.guild), view=VoicePanelView()
                )
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

        access_role_id = self.vc_row.get("access_role_id")
        if access_role_id:
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
                description="Zugang dauerhaft gewährt – kann auch später wieder beitreten.",
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

        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"❌ {self.requester.display_name} wurde abgelehnt",
                description="Der User kann über den Warteraum erneut anfragen.",
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

    try:
        main_ch = await guild.create_voice_channel(
            name=f"🎙️ {member.display_name}",
            category=category,
            overwrites=main_ow,
            reason=f"Voice Creator – {member.display_name}",
        )
    except discord.Forbidden:
        logger.error(
            f"[voice] Kanal-Erstellung fehlgeschlagen für {member} – "
            "Bot braucht 'Manage Channels'."
        )
        return

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
        "access_role_id":   None,
        "is_open":          True,
        "user_limit":       0,
        "created_at":       datetime.now(timezone.utc).isoformat(),
        "last_empty_at":    None,
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
    """Löscht Haupt- und Warteraum-Kanal sowie die Zugangs-Rolle."""
    # Zugangs-Rolle zuerst löschen
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

    # Auch den Lock für diesen Kanal aufräumen
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
                "⚠️ Die Bot-Rolle muss **über allen anderen Rollen** stehen "
                "die Zugang erhalten sollen (Rollenhierarchie)."
            ),
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="📁 Kategorie",
            value=f"<#{self.category_id}>" if self.category_id else "*Server-Root*",
            inline=True,
        )
        e.add_field(name="🎙️ Kanal-Name", value=f"`{self.channel_name}`", inline=True)
        e.add_field(name="⏱️ Timeout",    value=f"{self.empty_timeout}s",  inline=True)
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
            placeholder="🔑 Panel-Rollen: immer joinen + Panel nutzen",
            min_values=0, max_values=10, row=2,
        )
        role_sel.callback = self._cb_roles
        self.add_item(role_sel)

        btn_name = discord.ui.Button(
            label="✏️ Kanal-Namen ändern", style=discord.ButtonStyle.secondary, row=3,
        )
        btn_name.callback = self._cb_name
        self.add_item(btn_name)

        btn_timeout = discord.ui.Button(
            label=f"⏱️ Timeout: {self.empty_timeout}s",
            style=discord.ButtonStyle.secondary, row=3,
        )
        btn_timeout.callback = self._cb_timeout
        self.add_item(btn_timeout)

        save_btn = discord.ui.Button(
            label="🚀 Setup abschließen", style=discord.ButtonStyle.success, row=4,
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

            me_perms = guild.me.guild_permissions
            missing = []
            if not me_perms.manage_roles:    missing.append("Manage Roles")
            if not me_perms.manage_channels: missing.append("Manage Channels")
            if not me_perms.move_members:    missing.append("Move Members")
            if missing:
                await interaction.followup.send(
                    f"❌ Dem Bot fehlen folgende Server-Berechtigungen: "
                    f"**{', '.join(missing)}**\n"
                    "Bitte in den Server-Einstellungen → Rollen ergänzen.",
                    ephemeral=True,
                )
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
            await interaction.followup.send(
                "❌ Fehlende Berechtigungen. Stelle sicher dass der Bot "
                "**Manage Channels** hat.", ephemeral=True,
            )
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
        await interaction.response.edit_message(embed=self._view._build_embed(), view=self._view)


class _TimeoutModal(discord.ui.Modal, title="Leerlauf-Timeout festlegen"):
    timeout = discord.ui.TextInput(
        label="Sekunden bis Kanäle gelöscht werden (min. 10)",
        placeholder="z.B. 30", required=True, max_length=6,
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

    @app_commands.command(
        name="voice_setup",
        description="Richte den automatischen Voice Channel Creator ein",
    )
    async def voice_setup(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=VoiceSetupView(guild_id=interaction.guild_id, bot=self.bot)._build_embed(),
            view=VoiceSetupView(guild_id=interaction.guild_id, bot=self.bot),
            ephemeral=True,
        )

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

        # ── 1. Erstell-Kanal betreten ──────────────────────────────────────────
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
                        await member.send(embed=discord.Embed(
                            title="❌ Keine Berechtigung",
                            description="Du hast nicht die nötige Rolle um einen eigenen Voice-Kanal zu erstellen.",
                            color=discord.Color.red(),
                        ))
                    except discord.Forbidden:
                        pass
                    return
            await _create_voice_channels(guild=guild, member=member, cfg=cfg, bot=self.bot)
            return

        # ── 2. Warteraum betreten ─────────────────────────────────────────────
        if after.channel and str(after.channel.id) != creator_ch_id:
            vc_row = _get_vc_by_wait(str(after.channel.id))
            if vc_row:
                await self._handle_waitroom_join(guild, member, vc_row, cfg)

        # ── 3. Kanal verlassen → Leerlauf prüfen ──────────────────────────────
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
        main_ch = guild.get_channel(int(vc_row["main_channel_id"]))
        wait_ch = guild.get_channel(int(vc_row["wait_channel_id"]))
        if not main_ch or not wait_ch:
            return

        if _can_manage(member, vc_row, cfg):
            try:
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom auto-join] {e}")
            return

        if _has_access_role(member, vc_row):
            try:
                await member.move_to(main_ch)
            except Exception as e:
                logger.error(f"[waitroom re-join] {e}")
            return

        embed = discord.Embed(
            title="🔔 Beitrittsanfrage",
            description=(
                f"**{member.mention}** (`{member.display_name}`) "
                f"möchte dem Kanal **{main_ch.name}** beitreten."
            ),
            color=discord.Color.orange(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text="Anfrage läuft nach 2 Minuten ab → automatisch abgelehnt.")
        try:
            await main_ch.send(
                embed=embed,
                view=WaitingRoomRequestView(
                    requester=member, main_ch=main_ch, wait_ch=wait_ch,
                    vc_row=vc_row, cfg=cfg,
                ),
            )
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