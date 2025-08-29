import os
import discord
import random
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime
from supabase import create_client
from keep_alive import keep_alive
from discord.ui import View, Select
from discord.ui import View, Select, Modal, TextInput, Button
from discord import app_commands, Interaction, Embed, TextStyle, PermissionOverwrite
import asyncio

# --- Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SupabaseURL")
SUPABASE_APIKEY = os.getenv("SupabaseAPIKEY")

keep_alive()

supabase = create_client(SUPABASE_URL, SUPABASE_APIKEY) 

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True
bot = commands.Bot(command_prefix="!", intents=intents)

try:
# --- Hilfsfunktion: Zeitformatierung ---
    def format_time_until(target_time: str) -> str:
        now = datetime.now()
        try:
            target = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return "⚠️ Falsches Format! Benutze 'YYYY-MM-DD HH:MM'"

        delta = target - now
        if delta.total_seconds() < 0:
            return "⏰ Dieser Zeitpunkt liegt bereits in der Vergangenheit!"

        days = delta.days
        seconds = delta.seconds
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60

        weeks = days // 7
        days = days % 7

        parts = []
        if weeks: parts.append(f"{weeks} Woche{'n' if weeks != 1 else ''}")
        if days: parts.append(f"{days} Tag{'e' if days != 1 else ''}")
        if hours: parts.append(f"{hours} Stunde{'n' if hours != 1 else ''}")
        if minutes: parts.append(f"{minutes} Minute{'n' if minutes != 1 else ''}")

        return ", ".join(parts)

    def check_time_format(time_str: str) -> bool:
        try:
            datetime.strptime(time_str, "%Y-%m-%d %H:%M")
            return True
        except ValueError:
            return False

    # --- Autocomplete für Event-Namen ---
    async def event_autocomplete(interaction: discord.Interaction, current: str):
        try:
            response = supabase.table("events").select("name").eq("serverid", str(interaction.guild.id)).execute()
            rows = [item['name'] for item in response.data]
            return [
                app_commands.Choice(name=row, value=row)
                for row in rows if current.lower() in row.lower()
            ][:25]
        except Exception as e:
            print("Autocomplete Error:", e)
            return []


    # --- Setup Command ---
    
    @bot.tree.command(name="setup", description="Setup für Willkommens-, Eventchannel und Event-Rolle")
    async def setup(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        class SetupView(View):
            def __init__(self, guild: discord.Guild):
                super().__init__(timeout=120)
                self.guild = guild

                # --- Channel Selector ---
                self.channel_select = Select(
                    placeholder="Wähle, was du einrichten möchtest …",
                    options=[
                        discord.SelectOption(label="📥 Welcome-Channel", value="welcome", description="Der Channel für Willkommensnachrichten"),
                        discord.SelectOption(label="📢 Event-Channel", value="event", description="Der Channel für Event-Ankündigungen"),
                        discord.SelectOption(label="🎮 Spieleabend-Channel", value="game_night", description="Der Channel für Spieleabend-Umfragen")
                    ]
                )
                self.channel_select.callback = self.channel_select_callback
                self.add_item(self.channel_select)

                # --- Event-Rolle Multi-Page Selector ---
                self.roles = [role for role in guild.roles if not role.is_default()]
                self.page = 0
                self.PAGE_SIZE = 24
                self.role_select = self.build_role_select()
                self.add_item(self.role_select)

                # Buttons für Navigation
                self.prev_button = Button(label="◀️ Vorherige Seite", style=discord.ButtonStyle.secondary)
                self.next_button = Button(label="▶️ Nächste Seite", style=discord.ButtonStyle.secondary)
                self.prev_button.callback = self.prev_page
                self.next_button.callback = self.next_page
                self.add_item(self.prev_button)
                self.add_item(self.next_button)
                self.update_buttons()

            def build_role_select(self):
                start = self.page * self.PAGE_SIZE
                end = start + self.PAGE_SIZE
                options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in self.roles[start:end]]
                return Select(placeholder="Wähle die Event-Rolle …", options=options, min_values=1, max_values=1)

            def update_buttons(self):
                self.prev_button.disabled = self.page == 0
                self.next_button.disabled = (self.page + 1) * self.PAGE_SIZE >= len(self.roles)

            async def channel_select_callback(self, inter: discord.Interaction):
                choice = self.channel_select.values[0]
                channel_id = inter.channel.id
                data = {"serverid": str(inter.guild.id)}
                if choice == "welcome":
                    data["welcome_channel"] = str(channel_id)
                    msg = f"✅ Welcome-Channel gesetzt auf {inter.channel.mention}"
                elif choice == "game_night":
                    data["game_night_channel"] = str(channel_id)
                    msg = f"✅ Spieleabend-Channel gesetzt auf {inter.channel.mention}"
                else:
                    data["event_channel"] = str(channel_id)
                    msg = f"✅ Event-Channel gesetzt auf {inter.channel.mention}"

                supabase.table("server_settings").upsert(data, on_conflict="serverid").execute()
                await inter.response.edit_message(embed=discord.Embed(title="⚙️ Setup abgeschlossen", description=msg, color=discord.Color.green()), view=None)

            async def role_select_callback(self, inter: discord.Interaction):
                role_id = self.role_select.values[0]
                supabase.table("server_settings").upsert({"serverid": str(inter.guild.id), "event_role_id": role_id}, on_conflict="serverid").execute()
                await inter.response.edit_message(embed=discord.Embed(title="✅ Event-Rolle gesetzt", description=f"Rolle <@&{role_id}> wird nun bei Event-Remindern erwähnt.", color=discord.Color.green()), view=None)

            async def prev_page(self, inter: discord.Interaction):
                self.page -= 1
                self.role_select = self.build_role_select()
                self.clear_items()
                self.add_item(self.channel_select)
                self.add_item(self.role_select)
                self.add_item(self.prev_button)
                self.add_item(self.next_button)
                self.update_buttons()
                self.role_select.callback = self.role_select_callback
                await inter.response.edit_message(view=self)

            async def next_page(self, inter: discord.Interaction):
                self.page += 1
                self.role_select = self.build_role_select()
                self.clear_items()
                self.add_item(self.channel_select)
                self.add_item(self.role_select)
                self.add_item(self.prev_button)
                self.add_item(self.next_button)
                self.update_buttons()
                self.role_select.callback = self.role_select_callback
                await inter.response.edit_message(view=self)

        embed = discord.Embed(
            title="⚙️ Setup starten",
            description="Bitte wähle unten, welchen Channel oder welche Rolle du einrichten willst.\n\n"
                        "📥 **Welcome** = Begrüßung neuer Mitglieder\n"
                        "📢 **Event** = Erinnerungen & Ankündigungen\n"
                        "🎮 **Spieleabend** = Umfragen für Spieleabende\n"
                        "🔔 **Event-Rolle** = Rolle, die bei Event-Remindern erwähnt wird",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=SetupView(interaction.guild), ephemeral=True)



    # --- Funktionen aktivieren/deaktivieren ---
    def feature_view(mode: str, guild_id: str):
        class FeatureView(View):
            def __init__(self):
                super().__init__(timeout=60)
                self.select = discord.ui.Select(
                    placeholder=f"Wähle die Funktion zum {mode} …",
                    options=[
                        discord.SelectOption(label="👋 Willkommensnachricht", value="welcome"),
                        discord.SelectOption(label="📅 Event-Ankündiger", value="event")
                    ]
                )
                self.select.callback = self.select_callback
                self.add_item(self.select)

            async def select_callback(self, inter: discord.Interaction):
                choice = self.select.values[0]
                field = "welcome_enabled" if choice == "welcome" else "event_enabled"
                supabase.table("server_settings").upsert(
                    {"serverid": guild_id, field: (mode == "aktivieren")},
                    on_conflict="serverid"
                ).execute()

                msg = f"{'✅' if mode == 'aktivieren' else '⛔'} `{choice}` {mode}."
                await inter.response.edit_message(embed=discord.Embed(
                    title=f"⚙️ Funktion {mode.capitalize()}",
                    description=msg,
                    color=(discord.Color.green() if mode == "aktivieren" else discord.Color.red())
                ), view=None)

            async def on_timeout(self):
                for child in self.children:
                    child.disabled = True

        return FeatureView()


    @bot.tree.command(name="activate", description="Aktiviere Funktionen des Bots")
    async def activate(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title="⚙️ Funktionen aktivieren",
            description="Wähle die Funktion, die du einschalten möchtest:",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, view=feature_view("aktivieren", str(interaction.guild.id)), ephemeral=True)


    @bot.tree.command(name="disable", description="Deaktiviere Funktionen des Bots")
    async def disable(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        embed = discord.Embed(
            title="⚙️ Funktionen deaktivieren",
            description="Wähle die Funktion, die du ausschalten möchtest:",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, view=feature_view("deaktivieren", str(interaction.guild.id)), ephemeral=True)



    # --- Events Commands ---
    @bot.tree.command(name="add_event", description="Neues Event hinzufügen")
    @app_commands.describe(
        name="Name des Events",
        zeitpunkt="Startzeit des Events (Format: YYYY-MM-DD HH:MM)",
        endzeit="Endzeit des Events (optional, Format: YYYY-MM-DD HH:MM)"
    )
    async def add_event(interaction: discord.Interaction, name: str, zeitpunkt: str, endzeit: str = None):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        if not check_time_format(zeitpunkt):
            embed = discord.Embed(
                title="❌ Falsches Zeitformat",
                description="Die Startzeit muss im Format `YYYY-MM-DD HH:MM` sein.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)
        if endzeit and not check_time_format(zeitpunkt):
            embed = discord.Embed(
                title="❌ Falsches Zeitformat",
                description="Die Endzeit muss im Format `YYYY-MM-DD HH:MM` sein.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        server_id = str(interaction.guild.id)
        try:
            if len(zeitpunkt) == 16:
                zeitpunkt = zeitpunkt + ":00"
            if endzeit and len(endzeit) == 16:
                endzeit = endzeit + ":00"

            supabase.table("events").upsert({
                "name": name,
                "target_time": zeitpunkt,
                "end_time": endzeit,
                "serverid": server_id
            }).execute()

            await interaction.response.send_message(
                f"✅ Event **{name}** gespeichert.\nStart: `{zeitpunkt}`" + (f"\nEnde: `{endzeit}`" if endzeit else "")
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


    @bot.tree.command(name="time_until", description="Zeigt die Zeit bis zu einem gespeicherten Event")
    @app_commands.autocomplete(name=event_autocomplete)
    async def time_until(interaction: discord.Interaction, name: str):
        try:
            response = supabase.table("events").select("target_time, end_time")\
                .eq("name", name).eq("serverid", str(interaction.guild.id)).execute()
            row = response.data[0] if response.data else None

            if not row:
                return await interaction.response.send_message(
                    f"⚠️ Kein Event mit dem Namen **{name}** gefunden.", ephemeral=True
                )

            now = datetime.now()
            start_time_dt = datetime.strptime(row['target_time'], "%Y-%m-%d %H:%M:%S")
            end_time_dt = datetime.strptime(row['end_time'], "%Y-%m-%d %H:%M:%S") if row.get('end_time') else None

            embed = discord.Embed(title=f"⏱ Zeit bis Event {name}", color=discord.Color.green())

            if now < start_time_dt:
                # Event startet noch
                embed.add_field(name="Startet in:", value=format_time_until(row['target_time']), inline=False)
                if end_time_dt:
                    embed.add_field(name="Endet in:", value=format_time_until(row['end_time']), inline=False)
            elif end_time_dt and now < end_time_dt:
                # Event läuft gerade
                embed.add_field(name="Status:", value="Jetzt – Event läuft", inline=False)
                embed.add_field(name="Endet in:", value=format_time_until(row['end_time']), inline=False)
            else:
                # Event ist vorbei
                embed.add_field(name="Status:", value="✅ Event ist vorbei", inline=False)

            await interaction.response.send_message(embed=embed)

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)



    @bot.tree.command(name="list_events", description="Zeigt alle gespeicherten Events")
    async def list_events(interaction: discord.Interaction):
        try:
            server_id = str(interaction.guild.id)
            response = supabase.table("events").select("name, target_time").eq("serverid", server_id).execute()
            rows = [(item['name'], item['target_time']) for item in response.data]

            if not rows:
                return await interaction.response.send_message("📂 Keine Events gespeichert.")

            embed = discord.Embed(title="📂 Gespeicherte Events", color=discord.Color.green())
            for name, time in rows:
                embed.add_field(name=f"Event {name} startet: {time}",value="", inline=False)

            await interaction.response.send_message(embed=embed)

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


    @bot.tree.command(name="remove_event", description="Löscht ein gespeichertes Event")
    @app_commands.autocomplete(name=event_autocomplete)
    async def remove_event(interaction: discord.Interaction, name: str):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        server_id = str(interaction.guild.id)
        try:
            response = supabase.table("events").select("*").eq("name", name).eq("serverid", server_id).execute()
            if not response.data:
                return await interaction.response.send_message(f"❌ Kein Event mit dem Namen **{name}** gefunden.", ephemeral=True)

            supabase.table("events").delete().eq("name", name).eq("serverid", server_id).execute()
            await interaction.response.send_message(f"✅ Event **{name}** gelöscht.")

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


    # --- Spieleabend VoteView ---
    class VoteView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.votes_yes = set()
            self.votes_no = set()

        def build_embed(self, name: str, zeitpunkt: str, author: discord.Member) -> discord.Embed:
            return discord.Embed(
                title="🎮 Spieleabend geplant!",
                description=(
                    f"**Spiel:** {name}\n"
                    f"**Start:** `{zeitpunkt}`\n\n"
                    f"✅ Dabei: {', '.join(self.votes_yes) if self.votes_yes else 'Noch keiner'}\n"
                    f"❌ Keine Zeit: {', '.join(self.votes_no) if self.votes_no else 'Noch keiner'}"
                ),
                color=discord.Color.blurple()
            ).set_footer(text=f"Geplant von {author.display_name}")

        @discord.ui.button(label="✅ Dabei!", style=discord.ButtonStyle.success)
        async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.votes_yes.add(interaction.user.display_name)
            self.votes_no.discard(interaction.user.display_name)
            list_  = supabase.table("game_nights").select("yes_votes").eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            list_ = list(list_.data[0]["yes_votes"])
            list_.append(str(interaction.user.id))
            supabase.table("game_nights").update({
                "yes_votes": str(list_)
            }).eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            await interaction.response.edit_message(
                embed=self.build_embed(self.event_name, self.event_time, self.event_author),
                view=self
            )

        @discord.ui.button(label="❌ Keine Zeit", style=discord.ButtonStyle.danger)
        async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.votes_no.add(interaction.user.display_name)
            self.votes_yes.discard(interaction.user.display_name)
            list_ = supabase.table("game_nights").select("yes_votes").eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            list_ = list(list_.data[0]["yes_votes"])
            list_.discard(str(interaction.user.id))
            supabase.table("game_nights").update({
                "yes_votes": str(list_)
            }).eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            await interaction.response.edit_message(
                embed=self.build_embed(self.event_name, self.event_time, self.event_author),
                view=self
            )

        # kleine Helfer, um Context reinzuschieben
        def set_context(self, name: str, zeitpunkt: str, author: discord.Member):
            self.event_name = name
            self.event_time = zeitpunkt
            self.event_author = author


    # --- Slash Command: /spieleabend ---
    @bot.tree.command(name="spieleabend", description="Plane einen Spieleabend mit Abstimmung")
    @app_commands.describe(
        name="Name des Spiels",
        zeitpunkt="Startzeit (Format: YYYY-MM-DD HH:MM)"
    )
    async def spieleabend(interaction: discord.Interaction, name: str, zeitpunkt: str):
        try:
            if not check_time_format(zeitpunkt):
                embed = discord.Embed(
                    title="❌ Falsches Zeitformat",
                    description="Die Startzeit muss im Format `YYYY-MM-DD HH:MM` sein.",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)
            server_id = str(interaction.guild.id)

            # Zeitformat fixen
            if len(zeitpunkt) == 16:
                zeitpunkt = zeitpunkt + ":00"

            # Speichere Event in Supabase (damit /list_events etc. es auch kennt)

            supabase.table("game_nights").insert({
                "serverid": server_id,
                "name": name,
                "time": zeitpunkt,
                "yes_votes": str([""])
            }).execute()

            # Spieleabend-Channel aus DB holen
            settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", server_id).execute()
            channel_id = None
            if settings.data and settings.data[0].get("game_night_channel"):
                channel_id = int(settings.data[0]["game_night_channel"])

            # Fallback: aktueller Channel
            channel = interaction.channel if not channel_id else interaction.guild.get_channel(channel_id)
            if not channel:
                return await interaction.response.send_message("❌ Kein Spieleabend-Channel gefunden.", ephemeral=True)

            # VoteView vorbereiten
            view = VoteView()
            view.set_context(name, zeitpunkt, interaction.user)

            embed = view.build_embed(name, zeitpunkt, interaction.user)

            # Nachricht senden
            message = await channel.send(
                "Ein neuer Spieleabend wurde erstellt!",
                embed=embed,
                view=view
            )

            await interaction.response.send_message(
                f"✅ Spieleabend **{name}** am `{zeitpunkt}` erstellt.",
                ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


    @bot.tree.command(name="ticket_setup", description="Richte das Ticket-System ein")
    async def ticket_setup(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        class TicketSetupView(View):
            def __init__(self):
                super().__init__(timeout=None)

                # Select Menu mit Optionen direkt initialisieren
                self.select = discord.ui.Select(
                    placeholder="Wähle die Ticket-Kategorie …",
                    options=[
                        discord.SelectOption(label="Support Ticket", value="support", description="Für allgemeine Support-Anfragen"),
                        discord.SelectOption(label="Bauprojekt", value="bauprojekt", description="Für Bauprojekte oder Planungen")
                    ]
                )
                self.select.callback = self.select_callback
                self.add_item(self.select)

            async def select_callback(self, interaction: discord.Interaction):
                category = self.select.values[0]
                guild = interaction.guild

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }

                channel_name = f"{category}-{interaction.user.name}".lower()
                ticket_channel = await guild.create_text_channel(
                    name=channel_name,
                    overwrites=overwrites,
                    reason=f"Neues Ticket ({category})"
                )
                await ticket_channel.send(f"Hallo {interaction.user.mention}, dies ist dein {category}-Ticket. Wie können wir dir helfen?")

        embed = discord.Embed(
            title="🎟 Ticket-System",
            description="Wähle die Kategorie deines Tickets aus dem Dropdown-Menü unten.",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=TicketSetupView())

    @bot.tree.command(name="bewerbung", description="Starte eine Bewerbung")
    async def bewerbung(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator and not interaction.user.guild_permissions.manage_guild:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        class CategorySelectView(View):
            def __init__(self):
                super().__init__(timeout=60)
                self.add_item(Select(
                    placeholder="Wähle deine Kategorie …",
                    options=[
                        discord.SelectOption(label="Mitglied", value="Mitglied", description="Normales Mitglied"),
                        discord.SelectOption(label="Verbündeter", value="Verbündeter", description="Verbündeter Spieler")
                    ]
                ))
                self.children[0].callback = self.select_callback

            async def select_callback(self, select_interaction: discord.Interaction):
                category = select_interaction.data['values'][0]

                # Modal für Minecraft Namen
                class MinecraftNameModal(Modal):
                    def __init__(self, category: str):
                        super().__init__(title="Bewerbung")
                        self.category = category
                        self.mc_name_input = TextInput(
                            label="Dein Minecraft Name",
                            placeholder="Gib hier deinen MC-Namen ein",
                            required=True,
                            max_length=32
                        )
                        self.add_item(self.mc_name_input)

                    async def on_submit(self, modal_interaction: discord.Interaction):
                        mc_name = self.mc_name_input.value

                        # Button für Regeln und Channel-Erstellung
                        class RulesButtonView(View):
                            def __init__(self):
                                super().__init__(timeout=None)

                            @discord.ui.button(label="Ich stimme den Regeln zu", style=discord.ButtonStyle.success, custom_id="rules_accept_unique")
                            async def rules_button(self, btn_inter: discord.Interaction, button: Button):
                                guild = btn_inter.guild
                                overwrites = {
                                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                                    btn_inter.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                                }
                                channel_name = f"bewerbung-{btn_inter.user.name}".lower()
                                ticket_channel = await guild.create_text_channel(
                                    name=channel_name,
                                    overwrites=overwrites,
                                    reason="Neue Bewerbung"
                                )

                                embed = discord.Embed(
                                    title="✅ Bewerbung erstellt",
                                    description=f"**Kategorie:** {category}\n**Minecraft Name:** {mc_name}\nModeratoren können nun die Bewerbung prüfen.",
                                    color=discord.Color.green()
                                )
                                await ticket_channel.send(f"{btn_inter.user.mention}, willkommen! Dein privater Bewerbungs-Channel wurde erstellt.", embed=embed)
                                await btn_inter.response.send_message(f"✅ Dein Bewerbungs-Channel wurde erstellt: {ticket_channel.mention}", ephemeral=True)
                        # Sende Nachricht mit Button
                        if not modal_interaction.response.is_done():
                            await modal_interaction.response.send_message(
                                "Klicke auf den Button, um den Regeln zuzustimmen und die Bewerbung abzuschicken.",
                                view=RulesButtonView(),
                                ephemeral=True
                            )
                        else:
                            await modal_interaction.followup.send(
                                "Klicke auf den Button, um den Regeln zuzustimmen und die Bewerbung abzuschicken.",
                                view=RulesButtonView(),
                                ephemeral=True
                            )

                await select_interaction.response.send_modal(MinecraftNameModal(category))

        embed = discord.Embed(
            title="📝 Bewerbung starten",
            description="Bitte wähle zuerst, ob du Mitglied oder Verbündeter werden möchtest.",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=CategorySelectView())


    # --- Willkommensnachricht ---
    @bot.event
    async def on_member_join(member):
        messages = [
            f"**{member.mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>**",
            f"**wow, wie toll! {member.mention} ist jetzt hier! <:welcome:1362364322772160513>**",
            f"**{member.mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>**",
            f"**Juhu, {member.mention} hat zur Insel gefunden!**",
            f"**Kuckt mal wer hier ist: {member.mention} ! <:pepehappy:1362364194967781598>**",
            f"**Herzlich Willkommen {member.mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>**",
            f"**{member.mention} ist dem Insel-Discord beigetreten! 🫡**",
            f"**Endlich! {member.mention} ist hier! 😇**",
            f"**Huhu {member.mention} . Willkommen 🙂**",
            f"**Ein wildes  {member.mention} ist auf die Insel geschlittert 😄**",
            f"**Wilkommen {member.mention} bei der Insel! <:pepehappy:1362364194967781598>**",
            f"**{member.mention}, was geht yallah <:welcome:1362364322772160513>**",
            f"**Oh halloo! {member.mention} 🙂 **",
            f"**Heyyyy was geeeht {member.mention} 😀 **",
            f"**{member.mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!**",
            f"**Seht Seht {member.mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>**",
            f"**Boar das schmeckt, {member.mention} ist nun hier!🙃**"
            ]
        settings = supabase.table("server_settings").select("welcome_channel, welcome_enabled").eq("serverid", str(member.guild.id)).execute()
        if not settings.data: 
            return
        s = settings.data[0]
        if not s.get("welcome_enabled"): 
            return

        channel_id = s.get("welcome_channel")
        if channel_id:
            channel = member.guild.get_channel(int(channel_id))
            if channel:
                embed = discord.Embed(
                    title="👋 Willkommen!",
                    description=random.choice(messages),
                    color=discord.Color.green()
                )
                await channel.send(embed=embed)



    # --- Reminder Task ---
    # --- Reminder Task ---
    async def event_reminder_loop():
        await bot.wait_until_ready()  # Sicherstellen, dass der Bot bereit ist
        while not bot.is_closed():
            now = datetime.now()
            # --- Warte bis zur nächsten vollen Minute ---
            sleep_seconds = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(sleep_seconds)

            try:
                now = datetime.now()
                response = supabase.table("events").select("*").execute()
                response2 = supabase.table("game_nights").select("*").execute()
                if False:
                    for row in response2.data:
                        print(row)
                        time = datetime.strptime(row['time'], "%Y-%m-%d %H:%M:%S")
                        if now > time:
                            supabase.table("game_nights").delete().eq("name", row['name']).eq("serverid", row['serverid']).execute()
                            print(f"🗑 Spieleabend gelöscht: {row['name']} (Server: {row['serverid']})")
                        delta = time - now
                        milestones = {
                            7*24*3600: "📢 In **1 Woche** startet das Event!",
                            24*3600: "⏰ In **24 Stunden** startet das Event!",
                            3600: "⚡ In **1 Stunde** geht es los!",
                            600: "⏳ In **10 Minuten** geht es los!",
                            0: "🚀 Das Event startet jetzt!"
                        }
                        for seconds, message in milestones.items():
                            if seconds - 60 < delta.total_seconds() <= seconds:
                                guild = bot.get_guild(int(row['serverid']))
                                if guild:
                                    settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", str(guild.id)).execute()
                                    if not settings.data: 
                                        continue
                                    s = settings.data[0]
                                    channel_id = s.get("game_night_channel")
                                    if channel_id:
                                        channel = guild.get_channel(int(channel_id))
                                        if channel:
                                            personstoping = list(response2.data[0]['yes_votes'])
                                            print(personstoping)
                                            await channel.send(
                                                f"{message}",
                                                embed=discord.Embed(
                                                    title="📢 Spieleabend-Erinnerung",
                                                    description=f"**Spiel:** {row['name']}\nStart: `{row['time']}` {discord.member(persons for persons in  personstoping)}",#erwähnen,
                                                    color=discord.Color.orange()
                                                )
                                            )
                for row in response.data:
                    target_time = datetime.strptime(row['target_time'], "%Y-%m-%d %H:%M:%S")
                    end_time_dt = datetime.strptime(row['end_time'], "%Y-%m-%d %H:%M:%S") if row.get('end_time') else None
                    delta = target_time - now

                    # --- Erinnerungen ---
                    milestones = {
                        7*24*3600: "📢 In **1 Woche** startet das Event!",
                        24*3600: "⏰ In **24 Stunden** startet das Event!",
                        3600: "⚡ In **1 Stunde** geht es los!",
                        600: "⏳ In **10 Minuten** geht es los!",
                        0: "🚀 Das Event startet jetzt!"
                    }

                    for seconds, message in milestones.items():
                        # Nachricht nur senden, wenn die Zeit bis zum Event zwischen seconds-60 und seconds liegt
                        if seconds - 60 < delta.total_seconds() <= seconds:
                            guild = bot.get_guild(int(row['serverid']))
                            if guild:
                                settings = supabase.table("server_settings").select("event_channel, event_enabled","event_role_id")\
                                    .eq("serverid", str(guild.id)).execute()
                                if not settings.data: 
                                    continue
                                s = settings.data[0]
                                if not s.get("event_enabled"): 
                                    continue

                                channel_id = s.get("event_channel")
                                if channel_id:
                                    channel = guild.get_channel(int(channel_id))
                                    if channel:
                                        role_id = s.get("event_role_id")
                                        role_mention = f"<@&{role_id}>" if role_id else ""

                                        await channel.send(
                                            f"{role_mention} {message}",
                                            embed=discord.Embed(
                                                title="📢 Event-Erinnerung",
                                                description=f"**Event:** {row['name']}\nStart: `{row['target_time']}`",
                                                color=discord.Color.orange()
                                            )
                                        )
                    # --- Automatisches Löschen abgeschlossener Events ---
                    delete_event = False
                    # Spieleabende: immer löschen, wenn vorbei
                    if row['name'].startswith("Spieleabend:") and now >= target_time:
                        delete_event = True
                    # Events ohne Endzeit: löschen, wenn Startzeit vorbei
                    elif not end_time_dt and now >= target_time:
                        delete_event = True
                    # Events mit Endzeit: löschen, wenn Endzeit vorbei
                    elif end_time_dt and now >= end_time_dt:
                        delete_event = True

                    if delete_event:
                        supabase.table("events").delete().eq("name", row['name']).eq("serverid", row['serverid']).execute()
                        print(f"🗑 Event gelöscht: {row['name']} (Server: {row['serverid']})")
            except Exception as e:
                print("❌ Fehler im Reminder-Loop:", e)


    # --- Bot Start ---
    @bot.event
    async def on_ready():
        try:
            synced = await bot.tree.sync()
            print(f"✅ {len(synced)} globale Slash-Commands synchronisiert")
            bot.loop.create_task(event_reminder_loop())
        except Exception as e:
            print(f"❌ Fehler beim Synchronisieren: {e}")
            


    # pingpong test !ping
    @bot.command()
    async def ping(ctx):
        await ctx.send("Pong!")

except Exception as e:
    print(f"❌ Fehler beim Starten des Bots: {e}")


@bot.command()
async def welcome(ctx):
    if False:
        member = ctx.author
        messages = [
            f"**{member.mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>**",
            f"**wow, wie toll! {member.mention} ist jetzt hier! <:welcome:1362364322772160513>**",
            f"**{member.mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>**",
            f"**Juhu, {member.mention} hat zur Insel gefunden!**",
            f"**Kuckt mal wer hier ist: {member.mention} ! <:pepehappy:1362364194967781598>**",
            f"**Herzlich Willkommen {member.mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>**",
            f"**{member.mention} ist dem Insel-Discord beigetreten! 🫡**",
            f"**Endlich! {member.mention} ist hier! 😇**",
            f"**Huhu {member.mention} . Willkommen 🙂**",
            f"**Ein wildes  {member.mention} ist auf die Insel geschlittert 😄**",
            f"**Wilkommen {member.mention} bei der Insel! <:pepehappy:1362364194967781598>**",
            f"**{member.mention}, was geht yallah <:welcome:1362364322772160513>**",
            f"**Oh halloo! {member.mention} 🙂 **",
            f"**Heyyyy was geeeht {member.mention} 😀 **",
            f"**{member.mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!**",
            f"**Seht Seht {member.mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>**",
            f"**Boar das schmeckt, {member.mention} ist nun hier!🙃**"
            ]
        for message in messages:
                embed = discord.Embed(
                    title="👋 Willkommen!",
                    description=message,
                
                    color=discord.Color.green()
                )
                await ctx.send(embed=embed)

bot.run(TOKEN)
