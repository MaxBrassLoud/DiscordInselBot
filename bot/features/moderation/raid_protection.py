""" bot/features/moderation/raid_protection.py
Raid-Erkennung basierend auf Gesamt-Erwähnungen pro Zeitfenster.
Mit Debug-Modus, Ignorier-Rollen und Embed-Benachrichtigungen.
Reihenfolge: erst Timeout, dann Löschen – vermeidet doppelte Auslösungen.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional, Set

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.supabase_client import get_supabase

# ===== DEBUG-MODUS =====
DEBUG = True  # Auf True setzen für ausführliche Konsolenausgaben

def debug_print(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

# ===== KONFIGURATION (Schwellwerte) =====
CONFIG = {
    "total_mention_threshold": 6,     # Summe aller Erwähnungen im Zeitfenster
    "message_time_window": 60,        # Zeitfenster in Sekunden
    "new_member_days": 2,
    "timeout_hours": 24,
    "log_channel_override": None,     # Für Tests: Channel-ID eintragen
}
# ========================================

class RaidProtectionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._watchlist: Dict[int, Dict[str, Any]] = {}
        self._ignored_cache: Dict[str, Dict[str, Any]] = {}
        debug_print("✅ RaidProtectionCog geladen (Debug-Modus aktiv)")

    # ========== Datenbank – Ignorierte Rollen ==========

    async def _get_ignored_role_ids(self, guild: discord.Guild) -> Set[str]:
        server_id = str(guild.id)
        now = datetime.now().timestamp()
        cache = self._ignored_cache.get(server_id)
        if cache and (now - cache["timestamp"]) < 60:
            return cache["roles"]
        try:
            supabase = get_supabase()
            response = supabase.table("raid_ignored_roles") \
                .select("role_id") \
                .eq("server_id", server_id) \
                .execute()
            role_ids = {row["role_id"] for row in response.data} if response.data else set()
            self._ignored_cache[server_id] = {"roles": role_ids, "timestamp": now}
            return role_ids
        except Exception as e:
            debug_print(f"❌ Fehler beim Abrufen der ignorierten Rollen: {e}")
            return set()

    async def _add_ignored_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)
        try:
            supabase = get_supabase()
            check = supabase.table("raid_ignored_roles") \
                .select("role_id") \
                .eq("server_id", server_id) \
                .eq("role_id", role_id) \
                .execute()
            if check.data:
                return False
            supabase.table("raid_ignored_roles").insert({
                "server_id": server_id,
                "role_id": role_id
            }).execute()
            self._ignored_cache.pop(server_id, None)
            return True
        except Exception as e:
            debug_print(f"❌ Fehler beim Hinzufügen: {e}")
            return False

    async def _remove_ignored_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)
        try:
            supabase = get_supabase()
            supabase.table("raid_ignored_roles") \
                .delete() \
                .eq("server_id", server_id) \
                .eq("role_id", role_id) \
                .execute()
            self._ignored_cache.pop(server_id, None)
            return True
        except Exception as e:
            debug_print(f"❌ Fehler beim Entfernen: {e}")
            return False

    # ========== Log-Channel (dein Schema) ==========

    async def _get_log_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        if CONFIG["log_channel_override"]:
            channel = guild.get_channel(CONFIG["log_channel_override"])
            if channel:
                debug_print(f"📢 Verwende Override-Channel: {channel.name}")
                return channel
        try:
            supabase = get_supabase()
            response = supabase.table("settings") \
                .select("moderation_log_channel_id") \
                .eq("guild_id", str(guild.id)) \
                .execute()
            if response.data and response.data[0].get("moderation_log_channel_id"):
                channel_id = int(response.data[0]["moderation_log_channel_id"])
                channel = guild.get_channel(channel_id)
                if channel:
                    debug_print(f"📢 Log-Channel aus DB: {channel.name}")
                    return channel
                else:
                    debug_print(f"❌ Channel {channel_id} nicht gefunden.")
        except Exception as e:
            debug_print(f"❌ Fehler beim Abrufen des Log-Channels: {e}")
        return None

    # ========== Log in moderation_logs (dein Schema) ==========

    async def _log_moderation_action(self, guild_id: str, action: str, target: discord.Member,
                                     moderator: discord.Member, reason: str, until: Optional[datetime] = None):
        try:
            supabase = get_supabase()
            data = {
                "server_id": guild_id,
                "action": action,
                "target_id": str(target.id),
                "target_name": target.name,
                "moderator_id": str(moderator.id),
                "moderator_name": moderator.name,
                "reason": reason,
                "until": until.isoformat() if until else None,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            supabase.table("moderation_logs").insert(data).execute()
            debug_print(f"📝 Log in moderation_logs geschrieben: {action} für {target}")
        except Exception as e:
            debug_print(f"❌ Fehler beim Loggen: {e}")

    # ========== Erkennungslogik (Summe der Erwähnungen) ==========

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        debug_print(f"📩 on_message von {message.author} in {message.guild}")
        if message.author.bot:
            debug_print("   ➡️ Bot-Nachricht ignoriert")
            return
        if not message.guild:
            debug_print("   ➡️ Keine Guild – ignoriert")
            return
        if message.author.is_timed_out():
            debug_print(f"   ➡️ {message.author} ist bereits im Timeout – ignoriert")
            return

        if await self._is_suspicious(message):
            debug_print(f"   🚨 Verdacht erkannt für {message.author} (Summe erreicht)")
            await self._handle_suspicious_user(message)
        else:
            debug_print(f"   ➡️ Kein Verdacht – aktuelle Summe: {self._watchlist.get(message.author.id, {}).get('sum_mentions', 0)}")

    async def _is_suspicious(self, message: discord.Message) -> bool:
        # 1. Ignorierte Rollen prüfen
        ignored_roles = await self._get_ignored_role_ids(message.guild)
        if any(str(role.id) in ignored_roles for role in message.author.roles):
            debug_print(f"   ⏩ {message.author} hat ignorierte Rolle – überspringe")
            return False

        user_id = message.author.id
        now = datetime.now(timezone.utc)
        mention_count = len(message.mentions) + len(message.role_mentions)
        debug_print(f"   🧮 Erwähnungen in dieser Nachricht: {mention_count}")

        entry = self._watchlist.get(user_id)
        if entry is None:
            self._watchlist[user_id] = {
                "messages": [message],
                "sum_mentions": mention_count,
                "first_seen": now
            }
            debug_print(f"   📝 Neuer Eintrag – Summe: {mention_count}")
            return False

        # Alte Nachrichten entfernen
        cutoff = now - timedelta(seconds=CONFIG["message_time_window"])
        entry["messages"] = [m for m in entry["messages"] if m.created_at >= cutoff]
        entry["messages"].append(message)
        entry["sum_mentions"] = sum(len(m.mentions) + len(m.role_mentions) for m in entry["messages"])
        entry["first_seen"] = min(entry.get("first_seen", now), now)

        debug_print(f"   📊 Aktuelle Gesamt-Erwähnungen: {entry['sum_mentions']} (Threshold: {CONFIG['total_mention_threshold']})")

        if entry["sum_mentions"] >= CONFIG["total_mention_threshold"]:
            return True
        return False

    # ========== Timeout & Report ==========

    async def _send_user_embed(self, user: discord.Member, title: str, description: str, color: discord.Color,
                               fields: List[tuple] = None):
        """Sendet eine Embed-Nachricht an den User. Akzeptiert Felder als 2er- oder 3er-Tupel."""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc)
        )
        if fields:
            for item in fields:
                if len(item) == 3:
                    name, value, inline = item
                    embed.add_field(name=name, value=value, inline=inline)
                else:  # 2-Tupel
                    name, value = item
                    embed.add_field(name=name, value=value, inline=False)
        embed.set_footer(text="Raid-Protection System")
        try:
            await user.send(embed=embed)
            debug_print(f"   💬 Embed an {user} gesendet: {title}")
        except discord.Forbidden:
            debug_print(f"   ⚠️ DM an {user} nicht möglich (deaktiviert)")
        except Exception as e:
            debug_print(f"   ❌ Fehler beim Senden der DM: {e}")

    async def _handle_suspicious_user(self, message: discord.Message):
        user = message.author
        guild = message.guild
        debug_print(f"⚙️ Verarbeite Verdacht für {user} in {guild.name}")

        log_channel = await self._get_log_channel(guild)
        if not log_channel:
            debug_print("❌ Kein Log-Channel – breche ab")
            return

        # 1. Eintrag aus der Watchlist holen (bevor wir ihn löschen)
        entry = self._watchlist.pop(user.id, None)
        if not entry:
            debug_print("⚠️ Kein Watchlist-Eintrag für User – breche ab")
            return

        messages_to_delete = entry.get("messages", [])
        sum_mentions = entry.get("sum_mentions", 0)

        # 2. ZUERST Timeout setzen (damit keine weiteren Nachrichten kommen)
        until = datetime.now(timezone.utc) + timedelta(hours=CONFIG["timeout_hours"])
        try:
            await user.timeout(until, reason="Raid-Verdacht – zu viele Erwähnungen")
            debug_print(f"   ✅ Timeout gesetzt bis {until}")
        except Exception as e:
            debug_print(f"   ❌ Timeout-Fehler: {e}")
            # Trotzdem weitermachen mit Löschen und Logging?

        # 3. Log in die Datenbank (auch wenn Timeout fehlschlug – wir loggen den Versuch)
        await self._log_moderation_action(
            guild_id=str(guild.id),
            action="raid_timeout",
            target=user,
            moderator=self.bot.user,
            reason=f"Automatischer Timeout bei Raid-Verdacht (Gesamt-Erwähnungen: {sum_mentions}).",
            until=until
        )

        # 4. User-Benachrichtigung als Embed (auch wenn Timeout fehlschlug – wir informieren)
        await self._send_user_embed(
            user=user,
            title="🚨 Raid-Verdacht – Timeout verhängt",
            description=(
                f"Deine Nachrichten wurden wegen eines **Raid-Verdachts** gelöscht und du wurdest für "
                f"**{CONFIG['timeout_hours']} Stunden** in den Timeout versetzt.\n\n"
                f"Ein Moderator prüft den Fall. Du kannst dich im Log-Channel informieren."
            ),
            color=discord.Color.orange(),
            fields=[
                ("Zeitraum", f"Bis {discord.utils.format_dt(until, 'F')}", False),
                ("Grund", f"{sum_mentions} Erwähnungen in {len(messages_to_delete)} Nachrichten", False),
            ]
        )

        # 5. Jetzt die Nachrichten löschen (nach dem Timeout)
        if messages_to_delete:
            debug_print(f"🗑️ Lösche {len(messages_to_delete)} Nachrichten von {user}")
            for msg in messages_to_delete:
                try:
                    await msg.delete()
                    debug_print(f"   ✅ Gelöscht: {msg.content[:50]}...")
                except discord.NotFound:
                    # Nachricht existiert nicht mehr – ignorieren
                    debug_print(f"   ⚠️ Nachricht bereits gelöscht (404) – überspringe")
                except Exception as e:
                    debug_print(f"   ❌ Löschfehler: {e}")
                await asyncio.sleep(0.5)  # Rate-Limits vermeiden

        # 6. Report-Embed an den Log-Channel senden
        is_new = (datetime.now(timezone.utc) - user.joined_at).days <= CONFIG["new_member_days"]
        embed, view = await self._build_report_embed(guild, user, until, is_new, messages_to_delete, sum_mentions)
        await log_channel.send(embed=embed, view=view)
        debug_print(f"📨 Report gesendet an {log_channel.name}")

    async def _build_report_embed(self, guild, user, until, is_new, messages, sum_mentions):
        embed = discord.Embed(
            title="🛡️ Raid-Verdacht – Moderationsfall",
            description=(
                f"**User:** {user.mention} (`{user.id}`)\n"
                f"**Beigetreten:** {discord.utils.format_dt(user.joined_at, 'F')}\n"
                f"**Timeout bis:** {discord.utils.format_dt(until, 'F')}\n"
                f"**Status:** {'🆕 Neuer User (< 2 Tage)' if is_new else '👤 Bestehender User'}\n"
                f"**Erwähnungen insgesamt:** {sum_mentions}\n"
                f"**Nachrichten:** Die verdächtigen Nachrichten wurden automatisch gelöscht."
            ),
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )

        if messages:
            txt = ""
            for idx, msg in enumerate(messages[-5:], 1):
                content = msg.content[:300] + ("..." if len(msg.content) > 300 else "")
                erwähnungen = f"({len(msg.mentions)} User, {len(msg.role_mentions)} Rollen)"
                txt += f"**{idx}.** {discord.utils.format_dt(msg.created_at, 'R')}: {content} {erwähnungen}\n"
            embed.add_field(name="📝 Verdächtige Nachrichten (gelöscht)", value=txt[:1024], inline=False)
        else:
            embed.add_field(name="📝 Verdächtige Nachrichten", value="*Keine Nachrichten gefunden.*", inline=False)

        embed.set_footer(text=f"Fall-ID: {user.id}-{int(datetime.now().timestamp())}")
        view = RaidReportView(user, until, messages, self)
        return embed, view

    # ========== Slash-Commands ==========

    @app_commands.command(name="raid_ignore", description="Ignoriere eine Rolle bei der Raid-Erkennung")
    @app_commands.default_permissions(administrator=True)
    async def raid_ignore(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
            return
        success = await self._add_ignored_role(interaction.guild, role)
        if success:
            await interaction.response.send_message(f"✅ {role.mention} wird ignoriert.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {role.mention} wird bereits ignoriert oder Fehler.", ephemeral=True)

    @app_commands.command(name="raid_unignore", description="Hebe die Ignorierung auf")
    @app_commands.default_permissions(administrator=True)
    async def raid_unignore(self, interaction: discord.Interaction, role: discord.Role):
        if not interaction.guild:
            await interaction.response.send_message("Nur auf Servern nutzbar.", ephemeral=True)
            return
        success = await self._remove_ignored_role(interaction.guild, role)
        if success:
            await interaction.response.send_message(f"✅ {role.mention} wird nun wieder überprüft.", ephemeral=True)
        else:
            await interaction.response.send_message(f"⚠️ {role.mention} war nicht in der Ignorierliste.", ephemeral=True)

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
        roles = [interaction.guild.get_role(int(rid)) for rid in ignored_ids if interaction.guild.get_role(int(rid))]
        mention_list = ", ".join(r.mention for r in roles) if roles else "*(keine gefunden)*"
        await interaction.response.send_message(f"**Ignorierte Rollen:** {mention_list}", ephemeral=True)


class RaidReportView(discord.ui.View):
    """Interaktive Buttons mit Embed-Benachrichtigungen."""
    def __init__(self, user: discord.Member, until: datetime, messages: List[discord.Message], cog: RaidProtectionCog):
        super().__init__(timeout=3600)
        self.user = user
        self.until = until
        self.messages = messages
        self.cog = cog

    async def _send_user_embed(self, title: str, description: str, color: discord.Color, fields: List[tuple] = None):
        await self.cog._send_user_embed(self.user, title, description, color, fields)

    @discord.ui.button(label="✅ Nachrichten waren zulässig", style=discord.ButtonStyle.success)
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        debug_print(f"🟢 Approve von {interaction.user}")
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            await self.user.timeout(None, reason="Raid-Verdacht entkräftet")
            debug_print(f"   ✅ Timeout für {self.user} aufgehoben.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)
            return
        await self._send_user_embed(
            title="🙏 Entschuldigung – Timeout aufgehoben",
            description="Deine Nachrichten wurden von einem Moderator als **zulässig** eingestuft. Der Timeout wurde aufgehoben.",
            color=discord.Color.green()
        )
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.description += "\n\n✅ **Fall abgeschlossen:** Timeout aufgehoben."
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("✅ Timeout aufgehoben.", ephemeral=True)

    @discord.ui.button(label="⚠️ Unzulässig – Timeout aufheben", style=discord.ButtonStyle.primary)
    async def warn_release_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        debug_print(f"🟡 Warn-Release von {interaction.user}")
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            await self.user.timeout(None, reason="Verwarnung")
            debug_print(f"   ⚠️ Timeout für {self.user} aufgehoben (Verwarnung).")
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)
            return
        await self._send_user_embed(
            title="⚠️ Verwarnung – Timeout aufgehoben",
            description="Deine Nachrichten wurden als **unzulässig** eingestuft. Du erhältst eine Verwarnung, der Timeout wurde aufgehoben.",
            color=discord.Color.gold()
        )
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.gold()
        embed.description += "\n\n⚠️ **Fall abgeschlossen:** Timeout aufgehoben (Verwarnung)."
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("⚠️ Verwarnung, Timeout aufgehoben.", ephemeral=True)

    @discord.ui.button(label="⏳ Unzulässig – Timeout behalten", style=discord.ButtonStyle.secondary)
    async def keep_timeout_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        debug_print(f"⏳ Keep-Timeout von {interaction.user}")
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.moderate_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await self._send_user_embed(
            title="⏳ Timeout bleibt bestehen",
            description="Deine Nachrichten wurden als **unzulässig** eingestuft. Der Timeout bleibt bestehen.",
            color=discord.Color.red(),
            fields=[("Dauer", f"Bis {discord.utils.format_dt(self.until, 'F')}", False)]
        )
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.description += "\n\n⏳ **Fall abgeschlossen:** Timeout bleibt."
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("⏳ Timeout bleibt.", ephemeral=True)

    @discord.ui.button(label="🚫 Unzulässig – User bannen", style=discord.ButtonStyle.danger)
    async def ban_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        debug_print(f"🔴 Ban von {interaction.user}")
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.ban_members:
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        try:
            await self.user.ban(reason="Raid-Verdacht bestätigt")
            debug_print(f"   🚫 {self.user} wurde gebannt.")
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Bannen: {e}", ephemeral=True)
            return
        await self._send_user_embed(
            title="🚫 Du wurdest gebannt",
            description="Deine Nachrichten wurden als schwerwiegend **unzulässig** eingestuft. Du wurdest vom Server verbannt.",
            color=discord.Color.dark_red()
        )
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.dark_red()
        embed.description += "\n\n🚫 **Fall abgeschlossen:** User gebannt."
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("🚫 User gebannt.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(RaidProtectionCog(bot))