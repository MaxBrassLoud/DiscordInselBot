"""
features/minecraft_names/cog.py
================================
Minecraft-Name Commands:

  /minecraft_name  – Jedes Mitglied kann seinen Minecraft-Namen
                     im MC-Log-Channel veröffentlichen bzw. aktualisieren.
                     Setzt auch den Discord-Nickname.

  /name <User> <MC-Name>  – Admins & MBL können für beliebige User
                             den Minecraft-Namen setzen / aktualisieren.

Supabase-Tabelle (einmalig ausführen):
    CREATE TABLE IF NOT EXISTS minecraft_names (
        user_id     TEXT    NOT NULL,
        server_id   TEXT    NOT NULL,
        mc_name     TEXT    NOT NULL,
        message_id  TEXT,           -- ID der Embed-Nachricht im MC-Log-Channel
        updated_at  TIMESTAMPTZ DEFAULT now(),
        PRIMARY KEY (user_id, server_id)
    );
"""

from __future__ import annotations

import os

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("minecraft_names")

# User-ID(s) mit Sonder-Zugriff (kommagetrennt im Env)
_MBL_IDS: set[str] = {
    uid.strip()
    for uid in os.getenv("MBL", os.getenv("MBL", "")).split(",")
    if uid.strip()
}


# ──────────────────────────────────────────────────────────────────────────────
# Helper: darf der User /name nutzen?
# ──────────────────────────────────────────────────────────────────────────────

def _can_manage_names(interaction: discord.Interaction) -> bool:
    """True für Server-Admins und User in _MBL_IDS."""
    if str(interaction.user.id) in _MBL_IDS:
        return True
    if isinstance(interaction.user, discord.Member):
        return interaction.user.guild_permissions.administrator
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Supabase helpers
# ──────────────────────────────────────────────────────────────────────────────

def _load_entry(server_id: str, user_id: str) -> dict | None:
    r = get_supabase().table("minecraft_names") \
        .select("*") \
        .eq("server_id", server_id) \
        .eq("user_id", user_id) \
        .execute()
    return r.data[0] if r.data else None


def _save_entry(server_id: str, user_id: str, mc_name: str, message_id: str | None):
    from datetime import datetime, timezone
    row = {
        "user_id":    user_id,
        "server_id":  server_id,
        "mc_name":    mc_name,
        "message_id": message_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    existing = get_supabase().table("minecraft_names") \
        .select("user_id") \
        .eq("server_id", server_id) \
        .eq("user_id", user_id) \
        .execute()
    if existing.data:
        get_supabase().table("minecraft_names").update(row) \
            .eq("server_id", server_id).eq("user_id", user_id).execute()
    else:
        get_supabase().table("minecraft_names").insert(row).execute()


# ──────────────────────────────────────────────────────────────────────────────
# Embed builder (wiederverwendbar)
# ──────────────────────────────────────────────────────────────────────────────

def _build_mc_name_embed(
    member: discord.Member,
    mc_name: str,
    *,
    updated: bool = False,
) -> discord.Embed:
    embed = discord.Embed(
        title="✏️ Minecraft-Name aktualisiert" if updated else "⛏️ Minecraft-Name registriert",
        color=discord.Color.from_rgb(89, 197, 98),
    )
    embed.add_field(name="🎮 Minecraft-Name", value=f"```{mc_name}```", inline=False)
    embed.add_field(
        name="👤 Discord-Nutzer",
        value=member.mention,
        inline=True,
    )
    embed.set_thumbnail(url=f"https://mc-heads.net/avatar/{mc_name}/128")
    embed.set_footer(
        text=f"Discord: {member.display_name}",
        icon_url=member.display_avatar.url if member.display_avatar else discord.Embed.Empty,
    )
    embed.timestamp = discord.utils.utcnow()
    return embed


# ──────────────────────────────────────────────────────────────────────────────
# Kern-Logik: MC-Name setzen / aktualisieren
# ──────────────────────────────────────────────────────────────────────────────

async def _set_minecraft_name(
    interaction: discord.Interaction,
    target: discord.Member,
    mc_name: str,
    *,
    set_nickname: bool = True,
) -> None:
    """
    Shared logic used by both /minecraft_name and /name.

    1. Setzt den Discord-Nickname (optional, schlägt still fehl bei fehlenden Rechten).
    2. Postet/aktualisiert die Embed-Karte im MC-Log-Channel.
    3. Speichert den Eintrag in Supabase.
    """
    await interaction.response.defer(ephemeral=True)

    server_id = str(interaction.guild_id)
    user_id   = str(target.id)
    mc_name   = mc_name.strip()

    # ── 1. Nickname setzen ───────────────────────────────────────────────────
    if set_nickname:
        try:
            await target.edit(nick=mc_name, reason=f"Minecraft-Name: {mc_name}")
        except discord.Forbidden:
            logger.warning(f"[minecraft_names] Konnte Nickname für {target} nicht setzen.")

    # ── 2. MC-Log-Channel ermitteln ──────────────────────────────────────────
    supabase = get_supabase()
    cfg_r = supabase.table("application_servers") \
        .select("mc_log_channel_id") \
        .eq("server_id", server_id) \
        .execute()
    mc_log_channel_id: str | None = (
        cfg_r.data[0].get("mc_log_channel_id") if cfg_r.data else None
    )

    # ── 3. Vorhandenen Eintrag laden ─────────────────────────────────────────
    existing  = _load_entry(server_id, user_id)
    updated   = existing is not None
    embed     = _build_mc_name_embed(target, mc_name, updated=updated)
    new_msg_id: str | None = existing["message_id"] if existing else None

    if mc_log_channel_id:
        mc_log_ch = interaction.guild.get_channel(int(mc_log_channel_id))
        if mc_log_ch:
            # Versuch: bestehende Nachricht bearbeiten
            if new_msg_id:
                try:
                    old_msg = await mc_log_ch.fetch_message(int(new_msg_id))
                    await old_msg.edit(embed=embed)
                    logger.info(f"[minecraft_names] Nachricht {new_msg_id} bearbeitet für {target}")
                except (discord.NotFound, discord.HTTPException):
                    # Nachricht weg → neu posten
                    new_msg_id = None

            if not new_msg_id:
                try:
                    sent      = await mc_log_ch.send(embed=embed)
                    new_msg_id = str(sent.id)
                    logger.info(f"[minecraft_names] Neue Nachricht {new_msg_id} für {target}")
                except Exception as e:
                    logger.error(f"[minecraft_names] Senden fehlgeschlagen: {e}")
        else:
            logger.warning(f"[minecraft_names] MC-Log-Channel {mc_log_channel_id} nicht gefunden.")
    else:
        logger.info("[minecraft_names] Kein MC-Log-Channel konfiguriert.")

    # ── 4. Supabase speichern ─────────────────────────────────────────────────
    _save_entry(server_id, user_id, mc_name, new_msg_id)

    # ── 5. Antwort ────────────────────────────────────────────────────────────
    action = "aktualisiert" if updated else "gespeichert"
    ch_hint = f" in <#{mc_log_channel_id}>" if mc_log_channel_id else ""
    await interaction.followup.send(
        f"✅ Minecraft-Name **{mc_name}** für {target.mention} {action}{ch_hint}.",
        ephemeral=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Cog
# ──────────────────────────────────────────────────────────────────────────────

class MinecraftNamesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── /minecraft_name ──────────────────────────────────────────────────────

    @app_commands.command(
        name="minecraft_name",
        description="Trage deinen Minecraft-Namen ein oder aktualisiere ihn.",
    )
    @app_commands.describe(minecraft_name="Dein Minecraft-Benutzername (case-sensitive)")
    async def minecraft_name(
        self,
        interaction: discord.Interaction,
        minecraft_name: str,
    ):
        """
        Jedes Servermitglied kann diesen Command nutzen.
        Setzt den Nickname und postet/aktualisiert die Karte im MC-Log-Channel.
        """
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur auf Servern verwendbar.", ephemeral=True)
            return

        member = interaction.guild.get_member(interaction.user.id) or interaction.user
        await _set_minecraft_name(interaction, member, minecraft_name)

    # ── /name ────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="name",
        description="[Admin] Setze den Minecraft-Namen eines anderen Mitglieds.",
    )
    @app_commands.describe(
        user="Das Discord-Mitglied",
        minecraft_name="Der Minecraft-Benutzername",
    )
    async def name(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        minecraft_name: str,
    ):
        """
        Nur für Server-Admins und Nutzer in der MBL_USER_IDS-Env-Variable.
        """
        if not interaction.guild:
            await interaction.response.send_message("❌ Nur auf Servern verwendbar.", ephemeral=True)
            return

        if not _can_manage_names(interaction):
            await interaction.response.send_message(
                "❌ Du benötigst Administrator-Rechte für diesen Befehl.", ephemeral=True
            )
            return

        await _set_minecraft_name(interaction, user, minecraft_name)


async def setup(bot: commands.Bot):
    await bot.add_cog(MinecraftNamesCog(bot))