import os
import discord
import random
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
from supabase import create_client
from keep_alive import keep_alive
from discord.ui import View, Select
from discord.ui import View, Select, Modal, TextInput, Button
from discord import app_commands, Interaction, Embed, TextStyle, PermissionOverwrite
import asyncio
import json
from levelcalc import calculate_level
import ast


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
                    ],
                    custom_id="channel_select_unique"
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
                self.prev_button = Button(label="◀️ Vorherige Seite", style=discord.ButtonStyle.secondary, custom_id="prev_page_unique")
                self.next_button = Button(label="▶️ Nächste Seite", style=discord.ButtonStyle.secondary, custom_id="next_page_unique")
                self.prev_button.callback = self.prev_page
                self.next_button.callback = self.next_page
                self.add_item(self.prev_button)
                self.add_item(self.next_button)
                self.update_buttons()

            def build_role_select(self):
                start = self.page * self.PAGE_SIZE
                end = start + self.PAGE_SIZE
                options = [discord.SelectOption(label=role.name, value=str(role.id)) for role in self.roles[start:end]]
                return Select(placeholder="Wähle die Event-Rolle …", options=options, min_values=1, max_values=1, custom_id="role_select_unique")

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
                        discord.SelectOption(label="📅 Event-Ankündiger", value="event"),
                        discord.SelectOption(label="🎮 Spieleabend", value="game_night")
                    ],
                    custom_id="feature_select_unique"
                )
                self.select.callback = self.select_callback
                self.add_item(self.select)

            async def select_callback(self, inter: discord.Interaction):
                choice = self.select.values[0]
                field = "welcome_enabled" if choice == "welcome" else "game_night_enabled" if choice == "game_night" else "event_enabled"
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
        enabled = supabase.table("server_settings").select("event_enabled").eq("serverid", str(interaction.guild.id)).execute()
        if not enabled.data or not enabled.data[0].get("event_enabled", False):
            embed = discord.Embed(
                title="❌ Funktion deaktiviert",
                description="Der Event-Ankündiger ist auf diesem Server nicht aktiviert. Bitte aktiviere die Funktion zuerst mit `/activate`.",
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
            embed = discord.Embed(
                title=f"✅ Event **{name}** gespeichert.",
                description=f"Start: `{zeitpunkt}`" + (f"\nEnde: `{endzeit}`" if endzeit else ""),
                color=discord.Color.green()
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            embed= discord.Embed(
                title="❌ Fehler",
                description=f"{e}",
                color=discord.Color.red()
            )
            await interaction.response.send_message(embed=embed)


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



    class VoteView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.votes_yes = {}
            self.votes_no = set()

        def build_embed(self, name: str, zeitpunkt: str, author: discord.Member, description: str = None) -> discord.Embed:
            dabei = ", ".join(f"<@{uid}>" for uid in self.votes_yes.keys()) if self.votes_yes else "Noch keiner"
            keine_zeit = ", ".join(self.votes_no) if self.votes_no else "Noch keiner"

            # Zeit in Discord Timestamp konvertieren
            try:
                dt = datetime.strptime(zeitpunkt, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                timestamp = int(dt.timestamp())
                zeit_fmt = f"<t:{timestamp}:F>"  # schön formatiert mit Tag, Datum und Uhrzeit
            except:
                zeit_fmt = zeitpunkt  # Fallback, falls Parsing fehlschlägt

            embed = discord.Embed(
                title="🎮 Spieleabend geplant!",
                description=(
                    f"**Spiel:** {name}\n"
                    + (f"**Beschreibung:** {description}\n\n" if description else "")
                    + f"**Start:** {zeit_fmt}\n\n"
                    + f"✅ Dabei: {dabei}\n"
                    + f"❌ Keine Zeit: {keine_zeit}"
                ),
                color=discord.Color.blurple()
            ).set_footer(text=f"Geplant von {author.display_name}")

            return embed

        @discord.ui.button(label="✅ Dabei!", style=discord.ButtonStyle.success)
        async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.votes_yes[str(interaction.user.id)] = {
                "name": interaction.user.display_name,
                "id": str(interaction.user.id)
            }
            self.votes_no.discard(interaction.user.display_name)

            # DB updaten
            data = supabase.table("game_nights").select("yes_votes").eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            yes_votes = data.data[0].get("yes_votes", "{}")
            if isinstance(yes_votes, str):
                try:
                    yes_votes = eval(yes_votes)
                except:
                    yes_votes = {}
            yes_votes[str(interaction.user.id)] = {
                "name": interaction.user.display_name,
                "id": str(interaction.user.id)
            }

            supabase.table("game_nights").update({
                "yes_votes": str(yes_votes)
            }).eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()

            await interaction.response.edit_message(
                embed=self.build_embed(self.event_name, self.event_time, self.event_author, self.event_description),
                view=self
            )

        @discord.ui.button(label="❌ Keine Zeit", style=discord.ButtonStyle.danger)
        async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            self.votes_no.add(interaction.user.display_name)
            self.votes_yes.pop(str(interaction.user.id), None)

            # DB updaten
            data = supabase.table("game_nights").select("yes_votes").eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()
            yes_votes = data.data[0].get("yes_votes", "{}")
            if isinstance(yes_votes, str):
                try:
                    yes_votes = eval(yes_votes)
                except:
                    yes_votes = {}
            yes_votes.pop(str(interaction.user.id), None)

            supabase.table("game_nights").update({
                "yes_votes": str(yes_votes)
            }).eq("serverid", str(interaction.guild.id)).eq("name", self.event_name).execute()

            await interaction.response.edit_message(
                embed=self.build_embed(self.event_name, self.event_time, self.event_author, self.event_description),
                view=self
            )

        # Context
        def set_context(self, name: str, zeitpunkt: str, author: discord.Member, description: str = None):
            self.event_name = name
            self.event_time = zeitpunkt
            self.event_author = author
            self.event_description = description

        def add_votes(self, serverid: str):
            data = supabase.table("game_nights").select("yes_votes").eq("serverid", serverid).eq("name", self.event_name).execute()
            yes_votes = data.data[0].get("yes_votes", "{}")
            try:
                return eval(yes_votes) if isinstance(yes_votes, str) else yes_votes
            except:
                return {}


    class VoteView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)  # persistente View

        def build_embed_from_row(self, row: dict, guild: discord.Guild) -> discord.Embed:
            # safe parse yes_votes
            yes_votes_raw = row.get("yes_votes", "{}")
            try:
                yes_votes = ast.literal_eval(yes_votes_raw) if isinstance(yes_votes_raw, str) else (yes_votes_raw or {})
                if not isinstance(yes_votes, dict):
                    yes_votes = {}
            except Exception:
                yes_votes = {}

            dabei = ", ".join(f"<@{uid}>" for uid in yes_votes.keys()) if yes_votes else "Noch keiner"
            description = row.get("description", "")

            # Zeit formatieren
            try:
                dt = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                zeit_fmt = f"<t:{int(dt.timestamp())}:F>"
            except Exception:
                zeit_fmt = row.get("time", "Unbekannt")

            author = None
            try:
                if row.get("senderid") and guild:
                    author = guild.get_member(int(row.get("senderid")))
            except Exception:
                author = None

            footer_text = f"Geplant von {author.display_name}" if author else "Geplant von Unbekannt"

            embed = discord.Embed(
                title="🎮 Spieleabend geplant!",
                description=(
                    f"**Spiel:** {row.get('name','Unbekannt')}\n"
                    + (f"**Beschreibung:** {description}\n\n" if description else "")
                    + f"**Start:** {zeit_fmt}\n\n"
                    + f"✅ Dabei: {dabei}"
                ),
                color=discord.Color.blurple()
            ).set_footer(text=footer_text)

            return embed

        @discord.ui.button(label="✅ Dabei!", style=discord.ButtonStyle.success, custom_id="vote_yes")
        async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            serverid = str(interaction.guild.id)
            msg_id = str(interaction.message.id)

            # Hole DB-Row anhand messageid
            resp = supabase.table("game_nights").select("*").eq("serverid", serverid).eq("messageid", msg_id).execute()
            if not resp.data:
                return await interaction.response.send_message("❌ Dieses Event existiert nicht mehr.", ephemeral=True)
            row = resp.data[0]

            # parse yes_votes sicher
            yes_votes_raw = row.get("yes_votes", "{}")
            try:
                yes_votes = ast.literal_eval(yes_votes_raw) if isinstance(yes_votes_raw, str) else (yes_votes_raw or {})
                if not isinstance(yes_votes, dict):
                    yes_votes = {}
            except Exception:
                yes_votes = {}

            # hinzufügen
            yes_votes[str(interaction.user.id)] = {
                "name": interaction.user.display_name,
                "id": str(interaction.user.id)
            }

            supabase.table("game_nights").update({"yes_votes": str(yes_votes)}).eq("serverid", serverid).eq("messageid", msg_id).execute()

            # neu bauen & editieren
            row["yes_votes"] = str(yes_votes)
            await interaction.response.edit_message(embed=self.build_embed_from_row(row, interaction.guild), view=self)

        @discord.ui.button(label="❌ Keine Zeit", style=discord.ButtonStyle.danger, custom_id="vote_no")
        async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            serverid = str(interaction.guild.id)
            msg_id = str(interaction.message.id)

            resp = supabase.table("game_nights").select("*").eq("serverid", serverid).eq("messageid", msg_id).execute()
            if not resp.data:
                return await interaction.response.send_message("❌ Dieses Event existiert nicht mehr.", ephemeral=True)
            row = resp.data[0]

            # parse yes_votes
            yes_votes_raw = row.get("yes_votes", "{}")
            try:
                yes_votes = ast.literal_eval(yes_votes_raw) if isinstance(yes_votes_raw, str) else (yes_votes_raw or {})
                if not isinstance(yes_votes, dict):
                    yes_votes = {}
            except Exception:
                yes_votes = {}

            # entfernen
            yes_votes.pop(str(interaction.user.id), None)

            supabase.table("game_nights").update({"yes_votes": str(yes_votes)}).eq("serverid", serverid).eq("messageid", msg_id).execute()

            row["yes_votes"] = str(yes_votes)
            await interaction.response.edit_message(embed=self.build_embed_from_row(row, interaction.guild), view=self)




    @bot.tree.command(name="spieleabend", description="Plane einen Spieleabend mit Abstimmung")
    @app_commands.describe(
        name="Name des Spiels",
        zeitpunkt="Startzeit (Format: YYYY-MM-DD HH:MM)",
        beschreibung="Optionale Beschreibung"
    )
    async def spieleabend(interaction: discord.Interaction, name: str, zeitpunkt: str, beschreibung: str = None):
        try:
            if not check_time_format(zeitpunkt):
                embed = discord.Embed(
                    title="❌ Falsches Zeitformat",
                    description="Die Startzeit muss im Format `YYYY-MM-DD HH:MM` sein.",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            server_id = str(interaction.guild.id)

            enabled = supabase.table("server_settings").select("game_night_enabled").eq("serverid", server_id).execute()
            if not enabled.data or not enabled.data[0].get("game_night_enabled", False):
                embed = discord.Embed(
                    title="❌ Funktion deaktiviert",
                    description="Der Spieleabend ist auf diesem Server nicht aktiviert. Bitte aktiviere die Funktion zuerst mit `/activate`.",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            # Zeit fixen
            if len(zeitpunkt) == 16:
                zeitpunkt = zeitpunkt + ":00"

            # Channel aus Settings
            settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", server_id).execute()
            channel_id = None
            if settings.data and settings.data[0].get("game_night_channel"):
                try:
                    channel_id = int(settings.data[0]["game_night_channel"])
                except Exception:
                    channel_id = None

            channel = interaction.channel if not channel_id else interaction.guild.get_channel(channel_id)
            if not channel:
                return await interaction.response.send_message("❌ Kein Spieleabend-Channel gefunden.", ephemeral=True)

            # initial votes (Ersteller ist automatisch dabei)
            initial_yes = {
                str(interaction.user.id): {
                    "name": interaction.user.display_name,
                    "id": str(interaction.user.id)
                }
            }

            # Build embed lokal (vor dem DB-Eintrag)
            temp_row = {
                "serverid": server_id,
                "name": name,
                "time": zeitpunkt,
                "description": beschreibung or "",
                "senderid": str(interaction.user.id),
                "yes_votes": str(initial_yes),
                "messageid": ""  # wird nach dem Senden gefüllt
            }

            view = VoteView()
            embed = view.build_embed_from_row(temp_row, interaction.guild)

            # Nachricht senden
            msg = await channel.send("Ein neuer Spieleabend wurde erstellt!", embed=embed, view=view)

            # DB-Eintrag mit messageid speichern
            insert_payload = {
                "serverid": server_id,
                "name": name,
                "time": zeitpunkt,
                "description": beschreibung or "",
                "yes_votes": str(initial_yes),
                "senderid": str(interaction.user.id),
                "messageid": str(msg.id)
            }
            try:
                supabase.table("game_nights").insert(insert_payload).execute()
            except Exception as e:
                # falls Insert fehlschlägt: loggen, aber die Nachricht bleibt im Channel
                print(f"[WARN] DB insert failed for game_night: {e}")

            # persistente View registrieren (damit Buttons nach Neustart weiter funktionieren)
            try:
                bot.add_view(VoteView(), message_id=msg.id)
            except Exception as e:
                print(f"[WARN] konnte VoteView nicht persistent registrieren: {e}")

            await interaction.response.send_message(
                f"✅ Spieleabend **{name}** am `{zeitpunkt}` erstellt.",
                ephemeral=True
            )

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)



    # --- Autocomplete für remove_spieleabend ---
    async def spieleabend_autocomplete(interaction: discord.Interaction, current: str):
        server_id = str(interaction.guild.id)
        events = supabase.table("game_nights").select("name").eq("serverid", server_id).execute()
        choices = []
        if events.data:
            for e in events.data:
                if current.lower() in e["name"].lower():
                    choices.append(app_commands.Choice(name=e["name"], value=e["name"]))
        return choices[:25]


    # --- Slash Command: /remove_spieleabend ---
    @bot.tree.command(name="remove_spieleabend", description="Entfernt einen Spieleabend")
    @app_commands.autocomplete(name=spieleabend_autocomplete)
    async def remove_spieleabend(interaction: discord.Interaction, name: str):
        try:
            result = supabase.table("game_nights").select("senderid", "messageid") \
                .eq("name", name).eq("serverid", str(interaction.guild.id)).execute()
            if not result.data:
                return await interaction.response.send_message("❌ Spieleabend nicht gefunden.", ephemeral=True)

            senderid = result.data[0].get("senderid")
            messageid = result.data[0].get("messageid")

            if not interaction.user.guild_permissions.administrator and str(interaction.user.id) != str(senderid):
                embed = discord.Embed(
                    title="❌ Keine Berechtigung",
                    description="Nur Administratoren oder der Ersteller dürfen den Spieleabend löschen.",
                    color=discord.Color.red()
                )
                return await interaction.response.send_message(embed=embed, ephemeral=True)

            if messageid:
                try:
                    settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", str(interaction.guild.id)).execute()
                    if settings.data and settings.data[0].get("game_night_channel"):
                        channel_id = int(settings.data[0]["game_night_channel"])
                        channel = interaction.guild.get_channel(channel_id)
                    else:
                        channel = interaction.channel

                    msg = await channel.fetch_message(int(messageid))
                    await msg.delete()
                except Exception as e:
                    print(f"[WARN] Nachricht konnte nicht gelöscht werden: {e}")

            supabase.table("game_nights").delete().eq("serverid", str(interaction.guild.id)).eq("name", name).execute()
            await interaction.response.send_message(f"🗑️ Spieleabend **{name}** wurde entfernt.", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Entfernen: {e}", ephemeral=True)


    @bot.tree.command(name="ticket_setup", description="Richte das Ticket-System ein")
    async def ticket_setup(interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            embed = discord.Embed(
                title="❌ Keine Berechtigung",
                description="Nur Administratoren dürfen Funktionen aktivieren.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

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
                description="Nur Administratoren oder Moderatoren dürfen Bewerbungen starten.",
                color=discord.Color.red()
            )
            return await interaction.response.send_message(embed=embed, ephemeral=True)

        class CategorySelectView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)
                self.add_item(discord.ui.Select(
                    placeholder="Wähle deine Kategorie …",
                    options=[
                        discord.SelectOption(label="Mitglied", value="Mitglied", description="Normales Mitglied"),
                        discord.SelectOption(label="Verbündeter", value="Verbündeter", description="Verbündeter Spieler")
                    ],
                    custom_id="category_select_unique"
                ))
                self.children[0].callback = self.select_callback

            async def select_callback(self, select_interaction: discord.Interaction):
                category = select_interaction.data['values'][0]

                # Modal für Minecraft Namen
                class MinecraftNameModal(discord.ui.Modal):
                    def __init__(self, category: str):
                        super().__init__(title="Bewerbung")
                        self.category = category
                        self.mc_name_input = discord.ui.TextInput(
                            label="Dein Minecraft Name",
                            placeholder="Gib hier deinen MC-Namen ein",
                            required=True,
                            max_length=32
                        )
                        self.add_item(self.mc_name_input)

                    async def on_submit(self, modal_interaction: discord.Interaction):
                        mc_name = self.mc_name_input.value

                        # Button für Regeln (globale View!)
                        rules_view = RulesButtonView(self.category, mc_name)

                        if not modal_interaction.response.is_done():
                            await modal_interaction.response.send_message(
                                "Klicke auf den Button, um den Regeln zuzustimmen und die Bewerbung abzuschicken.",
                                view=rules_view,
                                ephemeral=True
                            )
                        else:
                            await modal_interaction.followup.send(
                                "Klicke auf den Button, um den Regeln zuzustimmen und die Bewerbung abzuschicken.",
                                view=rules_view,
                                ephemeral=True
                            )

                await select_interaction.response.send_modal(MinecraftNameModal(category))

        embed = discord.Embed(
            title="📝 Bewerbung starten",
            description="Bitte wähle zuerst, ob du Mitglied oder Verbündeter werden möchtest.",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=CategorySelectView())




    class TicketSetupView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)
            self.select = discord.ui.Select(
                placeholder="Wähle die Ticket-Kategorie …",
                options=[
                    discord.SelectOption(label="Support Ticket", value="support", description="Für allgemeine Support-Anfragen"),
                    discord.SelectOption(label="Bauprojekt", value="bauprojekt", description="Für Bauprojekte oder Planungen")
                ],
                custom_id="ticket_category_select_unique"
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


    class RulesButtonView(discord.ui.View):
        def __init__(self, category: str, mc_name: str):
            super().__init__(timeout=None)
            self.category = category
            self.mc_name = mc_name

        @discord.ui.button(label="Ich stimme den Regeln zu", style=discord.ButtonStyle.success, custom_id="rules_accept_unique")
        async def rules_button(self, interaction: discord.Interaction, button: discord.ui.Button):
            guild = interaction.guild
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }
            channel_name = f"bewerbung-{interaction.user.name}".lower()
            ticket_channel = await guild.create_text_channel(
                name=channel_name,
                overwrites=overwrites,
                reason="Neue Bewerbung"
            )

            embed = discord.Embed(
                title="✅ Bewerbung erstellt",
                description=f"**Kategorie:** {self.category}\n**Minecraft Name:** {self.mc_name}\nModeratoren können nun die Bewerbung prüfen.",
                color=discord.Color.green()
            )
            await ticket_channel.send(f"{interaction.user.mention}, willkommen! Dein privater Bewerbungs-Channel wurde erstellt.", embed=embed)
            await interaction.response.send_message(f"✅ Dein Bewerbungs-Channel wurde erstellt: {ticket_channel.mention}", ephemeral=True)


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
    async def event_reminder_loop():
        await bot.wait_until_ready()  # Sicherstellen, dass der Bot bereit ist
        while not bot.is_closed():
            now = datetime.now()
            # Bis zur nächsten vollen Minute schlafen
            sleep_seconds = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(sleep_seconds)
            try:
                settings = True
                if settings:
                    save_xp_to_db()
            except Exception as e:
                print(f"[ERROR] Fehler beim Speichern der XP: {e}")


            try:
                now = datetime.now()
                # 2 Stunden früher
                now = now - timedelta(hours=2)

                # --- Spieleabende (game_nights) ---
                gn_resp = supabase.table("game_nights").select("*").execute()

                if gn_resp.data:
                    for row in gn_resp.data:
                        # Zeitdifferenz zum Start
                        target_time = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
                        delta = target_time - now

                        # Channel finden
                        guild = bot.get_guild(int(row["serverid"]))
                        if not guild:
                            continue
                        settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", str(guild.id)).execute()
                        if not settings.data or not settings.data[0].get("game_night_channel"):
                            continue
                        channel = guild.get_channel(int(settings.data[0]["game_night_channel"]))
                        if not channel:
                            continue

                        # yes_votes parsen -> Dict {user_id: {...}}
                        yes_votes_raw = row.get("yes_votes", "{}")
                        try:
                            yes_votes = eval(yes_votes_raw) if isinstance(yes_votes_raw, str) else (yes_votes_raw or {})
                            if not isinstance(yes_votes, dict):
                                yes_votes = {}
                        except Exception:
                            yes_votes = {}
                        mentions = " ".join(f"<@{uid}>" for uid in yes_votes.keys()) if yes_votes else ""

                        # 10-Minuten-Reminder: genau im Fenster (600s - 60s, 600s]
                        if 540 < delta.total_seconds() <= 600:
                            text = f"{mentions} ⏳ In **10 Minuten** startet der Spieleabend **{row['name']}**!"
                            embed = discord.Embed(
                                title="🔔 Spieleabend-Erinnerung (T–10)",
                                description=f"**Spiel:** {row['name']}\n**Start:** `{row['time']}`",
                                color=discord.Color.orange()
                            )
                            await channel.send(content=text if mentions else None, embed=embed)

                        # Start-Reminder: genau im Fenster (0s - 60s]
                        if -60 < delta.total_seconds() <= 0:
                            dabei = ", ".join(f"<@{uid}>" for uid in yes_votes.keys()) if yes_votes else "Noch keiner"
                            text = f"🎮 Der Spieleabend **{row['name']}** startet jetzt!\n\n✅ Dabei: {dabei}"
                            embed = discord.Embed(
                                title="🎮 Spieleabend startet jetzt!",
                                description=f"**Spiel:** {row['name']}\n**Start:** `{row['time']}`",
                                color=discord.Color.green()
                            )
                            await channel.send(content=text, embed=embed)

                # --- Allgemeine Events (events) ---
                ev_resp = supabase.table("events").select("*").execute()
                for row in ev_resp.data:
                    target_time = datetime.strptime(row["target_time"], "%Y-%m-%d %H:%M:%S")
                    end_time_dt = datetime.strptime(row["end_time"], "%Y-%m-%d %H:%M:%S") if row.get("end_time") else None
                    delta = target_time - now

                    milestones = {
                        7 * 24 * 3600: "📢 In **1 Woche** startet das Event!",
                        24 * 3600: "⏰ In **24 Stunden** startet das Event!",
                        3600: "⚡ In **1 Stunde** geht es los!",
                        600: "⏳ In **10 Minuten** geht es los!",
                        0: "🚀 Das Event startet jetzt!",
                    }

                    # Channel + Settings prüfen
                    guild = bot.get_guild(int(row["serverid"]))
                    if not guild:
                        continue
                    settings = supabase.table("server_settings").select("event_channel,event_enabled,event_role_id")\
                        .eq("serverid", str(guild.id)).execute()
                    if not settings.data:
                        continue
                    s = settings.data[0]
                    if not s.get("event_enabled"):
                        continue
                    channel_id = s.get("event_channel")
                    if not channel_id:
                        continue
                    channel = guild.get_channel(int(channel_id))
                    if not channel:
                        continue

                    for seconds, message in milestones.items():
                        # Fenster: (seconds-60, seconds]
                        if (seconds - 60) < delta.total_seconds() <= seconds:
                            role_id = s.get("event_role_id")
                            role_mention = f"<@&{role_id}>" if role_id else ""
                            embed = discord.Embed(
                                title="📢 Event-Erinnerung",
                                description=f"**Event:** {row['name']}\nStart: `{row['target_time']}`",
                                color=discord.Color.orange()
                            )
                            await channel.send(content=f"{role_mention} {message}".strip(), embed=embed)

                    # Auto-Cleanup
                    delete_event = False
                    if row["name"].startswith("Spieleabend:") and now >= target_time:
                        delete_event = True
                    elif not end_time_dt and now >= target_time:
                        delete_event = True
                    elif end_time_dt and now >= end_time_dt:
                        delete_event = True

                    if delete_event:
                        supabase.table("events").delete().eq("name", row["name"]).eq("serverid", row["serverid"]).execute()
                        print(f"🗑 Event gelöscht: {row['name']} (Server: {row['serverid']})")

            except Exception as e:
                print("❌ Fehler im Reminder-Loop:", e)



    # ---- Level System ----
    @bot.event
    async def on_message(message):
        if message.author.bot:
            return

        # JSON einlesen oder neu erstellen
        try:
            with open("ram.json", "r") as f:
                ram = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            ram = {}

        guild_id = str(message.guild.id)
        user_id = str(message.author.id)

        if guild_id not in ram:
            ram[guild_id] = {}
        if user_id not in ram[guild_id]:
            ram[guild_id][user_id] = {"xp": 0, "serverid": guild_id}

        # +1 XP für jede Nachricht
        ram[guild_id][user_id]["xp"] += 1

        with open("ram.json", "w") as f:
            json.dump(ram, f, indent=4)

        await bot.process_commands(message)  # Commands nicht blockieren


    def save_xp_to_db():
        try:
            with open("ram.json", "r") as f:
                ram = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return
        settings = supabase.table("server_settings").select("level_system_enabled", "serverid").eq("level_system_enabled", True).execute()
        Server_Ids = [s["serverid"] for s in settings.data] if settings.data else []
        if not settings.data:
            return

        for guild_id, users in ram.items():
            for user_id, data in users.items():
                
                xp = int(data.get("xp", 0))

                if guild_id not in Server_Ids:
                    print(f"[INFO] Level System deaktiviert auf Server {guild_id}, XP nicht gespeichert.")
                    return 

                # Prüfen, ob User schon existiert
                resp = supabase.table("levels").select("xp", "level").eq("userid", user_id).eq("serverid", guild_id).execute()
                if not resp.data:
                    # Neuer Eintrag
                    supabase.table("levels").insert({
                        "userid": user_id,
                        "serverid": guild_id,
                        "xp": xp,
                        "level": 0
                    }).execute()
                else:
                    # Update XP (DB-Wert in int konvertieren!)
                    try:
                        current_xp = int(resp.data[0].get("xp", 0))
                    except (ValueError, TypeError):
                        current_xp = 0

                    supabase.table("levels").update({
                        "xp": current_xp + xp,
                        "level": calculate_level(current_xp + xp)  # Level = XP // 100
                    }).eq("userid", user_id).eq("serverid", guild_id).execute()

        # RAM leeren nach Speichern
        with open("ram.json", "w") as f:
            json.dump({}, f)


    # ---- Voice XP Task ----
    async def voice_xp_loop():
        await bot.wait_until_ready()  # Sicherstellen, dass der Bot bereit ist
        while not bot.is_closed():
            now = datetime.now()
            # Bis zur nächsten vollen Minute schlafen
            sleep_seconds = 60 - now.second - now.microsecond / 1_000_000
            await asyncio.sleep(sleep_seconds)
            for guild in bot.guilds:
                for vc in guild.voice_channels:
                    for member in vc.members:
                        if member.bot:
                            continue
                        try:
                            with open("ram.json", "r") as f:
                                ram = json.load(f)
                        except (FileNotFoundError, json.JSONDecodeError):
                            ram = {}

                        guild_id = str(guild.id)
                        user_id = str(member.id)

                        if guild_id not in ram:
                            ram[guild_id] = {}
                        if user_id not in ram[guild_id]:
                            ram[guild_id][user_id] = {"xp": 0, "serverid": guild_id}

                        # +6 XP pro Minute im Voice
                        ram[guild_id][user_id]["xp"] += 6

                        with open("ram.json", "w") as f:
                            json.dump(ram, f, indent=4)

            await asyncio.sleep(60)  # jede Minute prüfen

    # pingpong test !ping
    @bot.command()
    async def ping(ctx):
        await ctx.send("Pong!")

except Exception as e:
    print(f"❌ Fehler beim Starten des Bots: {e}")


@bot.command()
async def welcome(ctx):
    if ctx.author.guild_permissions.administrator:
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
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"✅ {len(synced)} globale Slash-Commands synchronisiert")

        # Hintergrundtasks starten
        bot.loop.create_task(event_reminder_loop())
        bot.loop.create_task(voice_xp_loop())

        # andere globale persistente Views (z.B. TicketSetupView) wieder registrieren, falls nötig
        try:
            bot.add_view(TicketSetupView())
        except Exception:
            pass

        # --- Offene Spieleabende laden und VoteView für jede message registrieren ---
        try:
            events = supabase.table("game_nights").select("serverid", "messageid").execute()
            if events.data:
                for row in events.data:
                    try:
                        guild = bot.get_guild(int(row["serverid"]))
                        if not guild:
                            continue
                        msg_id = int(row["messageid"])

                        # try channel from settings first (faster)
                        settings = supabase.table("server_settings").select("game_night_channel").eq("serverid", str(guild.id)).execute()
                        found = False
                        if settings.data and settings.data[0].get("game_night_channel"):
                            ch = guild.get_channel(int(settings.data[0]["game_night_channel"]))
                            if ch:
                                try:
                                    await ch.fetch_message(msg_id)
                                    bot.add_view(VoteView(), message_id=msg_id)
                                    found = True
                                except Exception:
                                    found = False

                        # fallback: scan text channels (falls channel nicht in settings steht)
                        if not found:
                            for ch in guild.text_channels:
                                try:
                                    await ch.fetch_message(msg_id)
                                    bot.add_view(VoteView(), message_id=msg_id)
                                    found = True
                                    break
                                except Exception:
                                    continue

                        if found:
                            print(f"✅ Spieleabend-View wiederhergestellt: {msg_id} (Server {guild.id})")
                        else:
                            print(f"[WARN] Nachricht {msg_id} nicht gefunden in Server {guild.id}")

                    except Exception as e:
                        print(f"[WARN] Fehler beim Wiederherstellen einer Spieleabend-View: {e}")
        except Exception as e:
            print(f"[WARN] Fehler beim Laden der game_nights aus DB: {e}")

        print("✅ on_ready abgeschlossen")

    except Exception as e:
        print(f"❌ Fehler beim Synchronisieren: {e}")


bot.run(TOKEN)



