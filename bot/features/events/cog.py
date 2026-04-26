import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime

from bot.core.settings import get_settings, upsert_settings
from bot.core.supabase_client import get_supabase
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .helpers import TZ, build_event_embed
from .views import EventFollowView, EventCreateModal, EventEditSelectView, _archive_event, _notify_thread, _update_event_message

logger = get_logger("events")


async def has_event_rights(interaction: discord.Interaction, bot: discord.Client) -> bool:
    if str(interaction.user.id) == str(os.getenv("MBL")):
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    config = await get_settings(str(interaction.guild_id))
    if not config:
        return False
    role_ids = [r.strip() for r in config.get("event_role_ids", "").split(",") if r.strip()]
    return any(str(r.id) in role_ids for r in interaction.user.roles)


class SetupEventView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        cfg = current_config or {}
        self.event_channel_id: str | None = str(cfg["event_channel_id"]) if cfg.get("event_channel_id") else None
        self.event_role_ids:   list[str]  = [r for r in cfg.get("event_role_ids", "").split(",") if r] if cfg.get("event_role_ids") else []

        ch_sel = discord.ui.ChannelSelect(placeholder="📢 Kanal für Event-Ankündigungen", min_values=1, max_values=1, channel_types=[discord.ChannelType.text])
        ch_sel.callback = self.channel_callback
        self.add_item(ch_sel)

        role_sel = discord.ui.RoleSelect(placeholder="🔐 Rollen mit Event-Berechtigung", min_values=1, max_values=10)
        role_sel.callback = self.role_callback
        self.add_item(role_sel)

        self.save_btn = discord.ui.Button(label="💾 Speichern", style=discord.ButtonStyle.success,
                                          disabled=not (self.event_channel_id and self.event_role_ids))
        self.save_btn.callback = self.save_callback
        self.add_item(self.save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="⚙️ Event-System Setup", color=discord.Color.blurple())
        embed.add_field(name="📢 Event-Kanal",
                        value=f"<#{self.event_channel_id}>" if self.event_channel_id else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🔐 Berechtigte Rollen",
                        value=" ".join(f"<@&{r}>" for r in self.event_role_ids) or "*Nicht ausgewählt*", inline=False)
        return embed

    async def channel_callback(self, interaction: discord.Interaction):
        self.event_channel_id = interaction.data["values"][0]
        self.save_btn.disabled = not (self.event_channel_id and self.event_role_ids)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def role_callback(self, interaction: discord.Interaction):
        self.event_role_ids = interaction.data["values"]
        self.save_btn.disabled = not (self.event_channel_id and self.event_role_ids)
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            await upsert_settings(str(self.guild_id), {
                "event_channel_id": self.event_channel_id,
                "event_role_ids":   ",".join(self.event_role_ids),
            })
            embed = self._build_embed()
            embed.color = discord.Color.green()
            embed.title = "✅ Event-Setup gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class EventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.check_events.start()

    def cog_unload(self):
        self.check_events.cancel()

    @commands.Cog.listener()
    async def on_ready(self):
        self.bot.add_view(EventFollowView())
        logger.info("✅ EventFollowView registriert")

    event = app_commands.Group(
        name="event",
        description="Event System",
    )

    @event.command(name="setup", description="Konfiguriere das Event-System")
    async def setup_event(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Nur Admins.", ephemeral=True)
            return
        current = await get_settings(str(interaction.guild_id))
        view    = SetupEventView(interaction.guild_id, current)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @event.command(name="erstellen", description="Erstelle ein neues Event")
    async def event_erstellen(self, interaction: discord.Interaction):
        if not await has_event_rights(interaction, self.bot):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.send_modal(EventCreateModal(self.bot, get_settings))

    @event.command(name="list", description="Zeigt die Follower-Liste aller aktiven Events")
    async def event_list(self, interaction: discord.Interaction):
        if not await has_event_rights(interaction, self.bot):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            result   = supabase.table("events").select("*").eq("guild_id", str(interaction.guild_id)).eq("archived", False).execute()
            if not result.data:
                await interaction.followup.send("❌ Keine aktiven Events.", ephemeral=True)
                return
            embed = discord.Embed(title="📋 Event Follower-Liste", color=discord.Color.blurple())
            for ev in result.data:
                followers = ev.get("followers") or []
                mentions  = ", ".join(f"<@{uid}>" for uid in followers) if followers else "*Niemand*"
                embed.add_field(name=f"{ev['title']} (ID {ev['id']}) — {len(followers)} Follower",
                                value=mentions[:1024], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    @event.command(name="edit", description="Bearbeite ein bestehendes Event")
    @app_commands.describe(aktion="Was soll geändert werden?")
    @app_commands.choices(aktion=[
        app_commands.Choice(name="⏰ Startzeit ändern",         value="start_time"),
        app_commands.Choice(name="🏁 Endzeit ändern",           value="end_time"),
        app_commands.Choice(name="❌ Absagen",                  value="cancel"),
        app_commands.Choice(name="⏸️ Delay (unbestimmt)",      value="delay"),
        app_commands.Choice(name="▶️ Resume",                  value="resume"),
        app_commands.Choice(name="📅 Datum festlegen (TBA)",    value="set_date"),
        app_commands.Choice(name="🏁 Manuell beenden",          value="end_now"),
        app_commands.Choice(name="✏️ Titel ändern",            value="title"),
        app_commands.Choice(name="📝 Beschreibung ändern",      value="description"),
        app_commands.Choice(name="📢 News senden",              value="news"),
    ])
    async def event_edit(self, interaction: discord.Interaction, aktion: str):
        if not await has_event_rights(interaction, self.bot):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            result   = supabase.table("events").select("*").eq("guild_id", str(interaction.guild_id)).eq("archived", False).neq("status", "cancelled").execute()
            if not result.data:
                await interaction.followup.send("❌ Keine bearbeitbaren Events.", ephemeral=True)
                return
            view = EventEditSelectView(result.data, aktion, self.bot)
            await interaction.followup.send(f"Welches Event? *(Aktion: {aktion})*", view=view, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)

    @tasks.loop(minutes=1)
    async def check_events(self):
        try:
            supabase = get_supabase()
            now      = datetime.now(TZ)
            result   = supabase.table("events").select("*").eq("archived", False).execute()
            for ev in result.data:
                try:
                    status   = ev.get("status", "upcoming")
                    end_open = ev.get("end_open", False)
                    start_dt = datetime.fromisoformat(ev["start_time"]).replace(tzinfo=TZ) if ev.get("start_time") else None
                    end_dt   = datetime.fromisoformat(ev["end_time"]).replace(tzinfo=TZ)   if ev.get("end_time")   else None

                    if status in ("cancelled", "delayed", "ended", "tba", "open_end"):
                        if status == "ended" and end_dt and (now - end_dt).total_seconds() >= 86400:
                            await _archive_event(self.bot, ev)
                        continue

                    if status == "upcoming" and start_dt and now >= start_dt:
                        new_status = "open_end" if (end_open or not end_dt) else "live"
                        updates = {"status": new_status, "reminded_start": True}
                        supabase.table("events").update(updates).eq("id", ev["id"]).execute()
                        await _notify_thread(self.bot, ev, "🟢 **Das Event hat begonnen!**", ping=True)
                        await _update_event_message(self.bot, {**ev, **updates})
                        continue

                    if status == "live" and end_dt and now >= end_dt:
                        updates = {"status": "ended"}
                        supabase.table("events").update(updates).eq("id", ev["id"]).execute()
                        await _notify_thread(self.bot, ev, "✅ **Das Event ist beendet. Danke!**", ping=True)
                        await _update_event_message(self.bot, {**ev, **updates})
                        continue

                    if status == "upcoming" and start_dt:
                        diff_min = (start_dt - now).total_seconds() / 60
                        if 59 <= diff_min <= 61 and not ev.get("reminded_1h"):
                            supabase.table("events").update({"reminded_1h": True}).eq("id", ev["id"]).execute()
                            await _notify_thread(self.bot, ev, "⏰ **Noch 1 Stunde bis zum Event!**", ping=True)
                except Exception as e:
                    logger.error(f"[check_events] Event {ev.get('id')}: {e}")
        except Exception as e:
            logger.error(f"[check_events] Fehler: {e}")

    @check_events.before_loop
    async def before_events(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(EventsCog(bot))