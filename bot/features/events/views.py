import discord
from bot.core.supabase_client import get_supabase
from .helpers import build_event_embed, parse_event_time, TZ
from bot.utils.logger import get_logger
from datetime import datetime

logger = get_logger("events.views")


async def _update_event_message(bot: discord.Client, event: dict):
    try:
        channel = bot.get_channel(int(event["channel_id"]))
        if not channel:
            return
        message = await channel.fetch_message(int(event["message_id"]))
        await message.edit(embed=build_event_embed(event), view=EventFollowView())
    except Exception as e:
        logger.error(f"[_update_event_message] {e}")


async def _notify_thread(bot: discord.Client, event: dict, text: str, ping: bool = False):
    try:
        thread = bot.get_channel(int(event["thread_id"]))
        if not thread:
            return
        mention_str = ""
        if ping and event.get("followers"):
            mention_str = " ".join(f"<@{uid}>" for uid in event["followers"]) + "\n"
        await thread.send(f"{mention_str}{text}")
    except Exception as e:
        logger.error(f"[_notify_thread] {e}")


async def _archive_event(bot: discord.Client, event: dict):
    try:
        thread = bot.get_channel(int(event["thread_id"]))
        if thread and isinstance(thread, discord.Thread):
            await thread.send("🗄️ *Dieser Event-Thread wird archiviert.*")
            await thread.edit(archived=True, locked=True)
    except Exception as e:
        logger.error(f"[_archive_event] Thread: {e}")
    try:
        get_supabase().table("events").delete().eq("id", event["id"]).execute()
    except Exception as e:
        logger.error(f"[_archive_event] DB: {e}")


class EventFollowView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Event folgen",       style=discord.ButtonStyle.success,   custom_id="event_follow",   emoji="🔔")
    async def follow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, follow=True)

    @discord.ui.button(label="Nicht interessiert", style=discord.ButtonStyle.secondary, custom_id="event_unfollow", emoji="🔕")
    async def unfollow_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, follow=False)

    async def _handle(self, interaction: discord.Interaction, follow: bool):
        user_id    = str(interaction.user.id)
        message_id = str(interaction.message.id)
        try:
            supabase  = get_supabase()
            result    = supabase.table("events").select("*").eq("message_id", message_id).execute()
            if not result.data:
                await interaction.response.send_message("❌ Event nicht gefunden!", ephemeral=True)
                return
            event     = result.data[0]
            followers = list(event.get("followers") or [])
            if follow:
                if user_id not in followers:
                    followers.append(user_id)
                msg = "✅ Du folgst jetzt diesem Event!"
            else:
                was_in = user_id in followers
                followers = [f for f in followers if f != user_id]
                msg = "🔕 Du folgst dem Event nicht mehr." if was_in else "ℹ️ Du hast nicht gefolgt."
            supabase.table("events").update({"followers": followers}).eq("id", event["id"]).execute()
            await interaction.message.edit(embed=build_event_embed({**event, "followers": followers}), view=self)
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class EventCreateModal(discord.ui.Modal, title="Event erstellen"):
    e_title = discord.ui.TextInput(label="Event-Name", placeholder="z.B. Community Turnier", required=True, max_length=100)
    e_desc  = discord.ui.TextInput(label="Beschreibung", placeholder="Worum geht es?", required=False,
                                   style=discord.TextStyle.paragraph, max_length=800)
    e_start = discord.ui.TextInput(label="Startzeit  (-1 = noch unbekannt)", placeholder="25.12.2025 20:00  |  -1 = TBA",
                                   required=True, max_length=30)
    e_end   = discord.ui.TextInput(label="Endzeit  (-1 = kein festes Ende)", placeholder="25.12.2025 23:00  |  -1 = offen",
                                   required=True, max_length=30)

    def __init__(self, bot: discord.Client, settings_getter):
        super().__init__()
        self.bot = bot
        self.get_settings = settings_getter

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            config   = await self.get_settings(str(interaction.guild_id))
            if not config or not config.get("event_channel_id"):
                await interaction.followup.send("❌ Bitte führe zuerst `/setup_event` aus!", ephemeral=True)
                return
            channel = self.bot.get_channel(int(config["event_channel_id"]))
            if not channel:
                await interaction.followup.send("❌ Event-Kanal nicht gefunden!", ephemeral=True)
                return

            raw_start = self.e_start.value.strip()
            raw_end   = self.e_end.value.strip()
            start_tba = raw_start == "-1"
            end_open  = raw_end   == "-1"
            start_dt  = None
            end_dt    = None

            if not start_tba:
                start_dt = parse_event_time(raw_start)
                if start_dt is None or start_dt == "-1":
                    await interaction.followup.send("❌ Ungültiges Startzeit-Format!", ephemeral=True)
                    return
            if not end_open:
                end_dt = parse_event_time(raw_end)
                if end_dt is None or end_dt == "-1":
                    await interaction.followup.send("❌ Ungültiges Endzeit-Format!", ephemeral=True)
                    return
                if start_dt and end_dt <= start_dt:
                    await interaction.followup.send("❌ Endzeit muss nach Startzeit liegen!", ephemeral=True)
                    return

            initial_status = "tba" if start_tba else "upcoming"
            tmp_event = {"id": "?", "title": self.e_title.value, "description": self.e_desc.value or "",
                         "start_time": start_dt.isoformat() if start_dt else None,
                         "end_time": end_dt.isoformat() if end_dt else None,
                         "status": initial_status, "followers": [], "end_open": end_open}

            message = await channel.send(embed=build_event_embed(tmp_event), view=EventFollowView())
            thread  = await message.create_thread(name=f"💬 {self.e_title.value}", auto_archive_duration=10080)
            await thread.send(f"👋 Willkommen im Event-Thread für **{self.e_title.value}**!")

            data = {"guild_id": str(interaction.guild_id), "message_id": str(message.id), "thread_id": str(thread.id),
                    "channel_id": str(channel.id), "title": self.e_title.value, "description": self.e_desc.value or "",
                    "start_time": start_dt.isoformat() if start_dt else None, "end_time": end_dt.isoformat() if end_dt else None,
                    "end_open": end_open, "status": initial_status, "followers": [], "creator_id": str(interaction.user.id),
                    "reminded_1h": False, "reminded_start": False, "archived": False}
            result = supabase.table("events").insert(data).execute()
            if result.data:
                await message.edit(embed=build_event_embed({**tmp_event, "id": result.data[0]["id"]}))
            await interaction.followup.send(f"✅ Event **{self.e_title.value}** erstellt! {message.jump_url}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventEditSelectView(discord.ui.View):
    def __init__(self, events: list[dict], action: str, bot: discord.Client):
        super().__init__(timeout=60)
        self.action     = action
        self.bot        = bot
        self.events_map = {str(e["id"]): e for e in events}
        options = [discord.SelectOption(label=e["title"][:100], description=f"ID {e['id']} | {e.get('status','?')}", value=str(e["id"])) for e in events[:25]]
        select = discord.ui.Select(placeholder="Wähle ein Event…", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        supabase = get_supabase()
        event_id = interaction.data["values"][0]
        fresh    = supabase.table("events").select("*").eq("id", event_id).execute()
        event    = fresh.data[0] if fresh.data else self.events_map[event_id]
        action   = self.action

        if action in ("start_time", "end_time"):
            await interaction.response.send_modal(EventRescheduleModal(event, self.bot))
        elif action == "cancel":
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "cancelled"}).eq("id", event_id).execute()
            await _update_event_message(self.bot, {**event, "status": "cancelled"})
            await _notify_thread(self.bot, event, "❌ **Das Event wurde abgesagt.**", ping=True)
            await interaction.followup.send("✅ Event abgesagt.", ephemeral=True)
        elif action == "delay":
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "delayed"}).eq("id", event_id).execute()
            await _update_event_message(self.bot, {**event, "status": "delayed"})
            await _notify_thread(self.bot, event, "⏸️ **Das Event wurde verschoben.**", ping=True)
            await interaction.followup.send("✅ Event als 'delayed' markiert.", ephemeral=True)
        elif action == "resume":
            await interaction.response.send_modal(EventRescheduleModal(event, self.bot))
        elif action in ("title", "description", "news"):
            await interaction.response.send_modal(EventTextEditModal(event, action, self.bot))
        elif action == "end_now":
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "ended"}).eq("id", event_id).execute()
            await _update_event_message(self.bot, {**event, "status": "ended"})
            await _notify_thread(self.bot, event, "✅ **Das Event wurde beendet. Danke!**", ping=True)
            await interaction.followup.send("✅ Event beendet.", ephemeral=True)
        elif action == "set_date":
            await interaction.response.send_modal(EventSetDateModal(event, self.bot))


class EventRescheduleModal(discord.ui.Modal, title="Event neu terminieren"):
    new_start = discord.ui.TextInput(label="Neue Startzeit (-1=TBA)", placeholder="DD.MM.YYYY HH:MM  |  -1", required=True, max_length=30)
    new_end   = discord.ui.TextInput(label="Neue Endzeit (-1=offen)", placeholder="DD.MM.YYYY HH:MM  |  -1", required=True, max_length=30)

    def __init__(self, event: dict, bot: discord.Client):
        super().__init__()
        self.event = event
        self.bot   = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase  = get_supabase()
            raw_start = self.new_start.value.strip()
            raw_end   = self.new_end.value.strip()
            start_tba = raw_start == "-1"
            end_open  = raw_end   == "-1"
            start_dt  = None
            end_dt    = None
            if not start_tba:
                start_dt = parse_event_time(raw_start)
                if not start_dt or start_dt == "-1":
                    await interaction.followup.send("❌ Ungültiges Startzeit-Format!", ephemeral=True)
                    return
            if not end_open:
                end_dt = parse_event_time(raw_end)
                if not end_dt or end_dt == "-1":
                    await interaction.followup.send("❌ Ungültiges Endzeit-Format!", ephemeral=True)
                    return
            new_status = "tba" if start_tba else "upcoming"
            updates = {"start_time": start_dt.isoformat() if start_dt else None,
                       "end_time": end_dt.isoformat() if end_dt else None,
                       "end_open": end_open, "status": new_status,
                       "reminded_1h": False, "reminded_start": False}
            supabase.table("events").update(updates).eq("id", self.event["id"]).execute()
            await _update_event_message(self.bot, {**self.event, **updates})
            notif = "📅 **Zeitänderung!**\n" + (
                "Startzeit wird bekannt gegeben." if start_tba
                else f"▶️ <t:{int(start_dt.timestamp())}:F> → {'Ende offen' if end_open else f'<t:{int(end_dt.timestamp())}:F>'}"
            )
            await _notify_thread(self.bot, self.event, notif, ping=True)
            await interaction.followup.send("✅ Event neu terminiert!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventTextEditModal(discord.ui.Modal):
    def __init__(self, event: dict, field: str, bot: discord.Client):
        labels = {"title": "Titel ändern", "description": "Beschreibung ändern", "news": "News senden"}
        super().__init__(title=labels.get(field, "Bearbeiten"))
        self.event = event
        self.field = field
        self.bot   = bot
        self.text_input = discord.ui.TextInput(
            label=labels.get(field, "Text"), placeholder="Eingabe...", required=True,
            style=discord.TextStyle.paragraph if field != "title" else discord.TextStyle.short, max_length=800
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            value    = self.text_input.value
            if self.field == "news":
                await _notify_thread(self.bot, self.event, f"📢 **Event-News:**\n{value}", ping=True)
            elif self.field == "title":
                supabase.table("events").update({"title": value}).eq("id", self.event["id"]).execute()
                await _update_event_message(self.bot, {**self.event, "title": value})
            elif self.field == "description":
                supabase.table("events").update({"description": value}).eq("id", self.event["id"]).execute()
                await _update_event_message(self.bot, {**self.event, "description": value})
            await interaction.followup.send("✅ Erledigt!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventSetDateModal(discord.ui.Modal, title="Datum festlegen"):
    new_start = discord.ui.TextInput(label="Startzeit", placeholder="DD.MM.YYYY HH:MM", required=True, max_length=30)
    new_end   = discord.ui.TextInput(label="Endzeit (-1=offen)", placeholder="DD.MM.YYYY HH:MM  |  -1", required=True, max_length=30)

    def __init__(self, event: dict, bot: discord.Client):
        super().__init__()
        self.event = event
        self.bot   = bot

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase = get_supabase()
            end_open = self.new_end.value.strip() == "-1"
            start_dt = parse_event_time(self.new_start.value.strip())
            if not start_dt or start_dt == "-1":
                await interaction.followup.send("❌ Ungültiges Format!", ephemeral=True)
                return
            end_dt = None
            if not end_open:
                end_dt = parse_event_time(self.new_end.value.strip())
                if not end_dt or end_dt <= start_dt:
                    await interaction.followup.send("❌ Ungültige Endzeit!", ephemeral=True)
                    return
            updates = {"start_time": start_dt.isoformat(), "end_time": end_dt.isoformat() if end_dt else None,
                       "end_open": end_open, "status": "upcoming", "reminded_1h": False, "reminded_start": False}
            supabase.table("events").update(updates).eq("id", self.event["id"]).execute()
            await _update_event_message(self.bot, {**self.event, **updates})
            await _notify_thread(self.bot, self.event, f"📅 **Datum gesetzt!** ▶️ <t:{int(start_dt.timestamp())}:F>", ping=True)
            await interaction.followup.send("✅ Datum gesetzt!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)