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
bot = commands.Bot(command_prefix="!", intents=intents)


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


@bot.tree.command(name="setup", description="Setup für Willkommens- und Eventchannel")
async def setup(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Nur Admins dürfen Setup ausführen.", ephemeral=True)

    class SetupView(View):
        def __init__(self):
            super().__init__(timeout=60)
            self.select = discord.ui.Select(
                placeholder="Wähle, was du einrichten möchtest …",
                options=[
                    discord.SelectOption(label="📥 Welcome-Channel", value="welcome", description="Der Channel für Willkommensnachrichten"),
                    discord.SelectOption(label="📢 Event-Channel", value="event", description="Der Channel für Event-Announcements")
                ]
            )
            self.select.callback = self.select_callback
            self.add_item(self.select)

        async def select_callback(self, inter: discord.Interaction):
            choice = self.select.values[0]
            channel_id = inter.channel.id

            if choice == "welcome":
                supabase.table("server_settings").upsert({
                    "serverid": str(inter.guild.id),
                    "welcome_channel": str(channel_id)
                }, on_conflict="serverid").execute()
                msg = f"✅ Welcome-Channel gesetzt auf {inter.channel.mention}"
            else:
                supabase.table("server_settings").upsert({
                    "serverid": str(inter.guild.id),
                    "event_channel": str(channel_id)
                }, on_conflict="serverid").execute()
                msg = f"✅ Event-Channel gesetzt auf {inter.channel.mention}"

            await inter.response.edit_message(embed=discord.Embed(
                title="⚙️ Setup abgeschlossen",
                description=msg,
                color=discord.Color.green()
            ), view=None)

        async def on_timeout(self):
            for child in self.children:
                child.disabled = True

    embed = discord.Embed(
        title="⚙️ Setup starten",
        description="Bitte wähle unten, welchen Channel du einrichten willst.\n\n"
                    "📥 **Welcome** = Begrüßung neuer Mitglieder\n"
                    "📢 **Event** = Erinnerungen & Ankündigungen",
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=SetupView(), ephemeral=True)


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
        return await interaction.response.send_message("❌ Nur Admins dürfen das.", ephemeral=True)

    embed = discord.Embed(
        title="⚙️ Funktionen aktivieren",
        description="Wähle die Funktion, die du einschalten möchtest:",
        color=discord.Color.green()
    )
    await interaction.response.send_message(embed=embed, view=feature_view("aktivieren", str(interaction.guild.id)), ephemeral=True)


@bot.tree.command(name="disable", description="Deaktiviere Funktionen des Bots")
async def disable(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Nur Admins dürfen das.", ephemeral=True)

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
        return await interaction.response.send_message("❌ Du brauchst Adminrechte.", ephemeral=True)

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
            return await interaction.response.send_message(f"⚠️ Kein Event mit dem Namen **{name}** gefunden.", ephemeral=True)

        start_time = format_time_until(row['target_time'])
        embed = discord.Embed(title=f"⏱ Zeit bis Event {name}", color=discord.Color.green())
        embed.add_field(name="Startet in:", value=start_time, inline=False)

        if row.get('end_time'):
            end_time = format_time_until(row['end_time'])
            embed.add_field(name="Endet in:", value=end_time, inline=False)

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
            embed.add_field(name=name, value=f"Startet am: {time}", inline=False)

        await interaction.response.send_message(embed=embed)

    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


@bot.tree.command(name="remove_event", description="Löscht ein gespeichertes Event")
@app_commands.autocomplete(name=event_autocomplete)
async def remove_event(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message("❌ Du brauchst Adminrechte.", ephemeral=True)

    server_id = str(interaction.guild.id)
    try:
        response = supabase.table("events").select("*").eq("name", name).eq("serverid", server_id).execute()
        if not response.data:
            return await interaction.response.send_message(f"❌ Kein Event mit dem Namen **{name}** gefunden.", ephemeral=True)

        supabase.table("events").delete().eq("name", name).eq("serverid", server_id).execute()
        await interaction.response.send_message(f"✅ Event **{name}** gelöscht.")

    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# --- Willkommensnachricht ---
@bot.event
async def on_member_join(member):
    messages = [
        f"***{member.mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>***",
        f"***wow, wie toll! {member.mention} ist jetzt hier! <:welcome:1362364322772160513>***",
        f"***{member.mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>***",
        f"***Juhu, {member.mention} hat zur Insel gefunden!***",
        f"***Kuckt mal wer hier ist: {member.mention} ! <:pepehappy:1362364194967781598>***",
        f"***Herzlich Willkommen {member.mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>***",
        f"***{member.mention} ist dem Insel-Discord beigetreten! 🫡***",
        f"***Endlich! {member.mention} ist hier! 😇***",
        f"***Huhu {member.mention} . Willkommen 🙂***",
        f"***Ein wildes  {member.mention} ist auf die Insel geschlittert 😄***",
        f"***Wilkommen {member.mention} bei der Insel! <:pepehappy:1362364194967781598>***",
        f"***{member.mention}, was geht yallah <:welcome:1362364322772160513>***",
        f"***Oh halloo! {member.mention} 🙂 ***",
        f"***Heyyyy was geeeht {member.mention} 😀",
        f"***{member.mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!***",
        f"***Seht Seht {member.mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>***",
        f"***Boar das schmeckt, {member.mention} ist nun hier!🙃***"
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
@tasks.loop(minutes=1)
async def check_event_reminders():
    now = datetime.now()
    try:
        response = supabase.table("events").select("name, target_time, serverid").execute()
        for row in response.data:
            target_time = datetime.strptime(row['target_time'], "%Y-%m-%d %H:%M:%S")
            delta = target_time - now

            milestones = {
                7*24*3600: "📢 In **1 Woche** startet das Event!",
                24*3600: "⏰ In **24 Stunden** startet das Event!",
                3600: "⚡ In **1 Stunde** geht es los!"
            }

            for seconds, message in milestones.items():
                if 0 <= delta.total_seconds() - seconds < 300:
                    guild = bot.get_guild(int(row['serverid']))
                    if guild:
                        settings = supabase.table("server_settings").select("event_channel, event_enabled").eq("serverid", str(guild.id)).execute()
                        if not settings.data: continue
                        s = settings.data[0]
                        if not s.get("event_enabled"): continue

                        channel_id = s.get("event_channel")
                        if channel_id:
                            channel = guild.get_channel(int(channel_id))
                            if channel:
                                await channel.send(f"@everyone {message}\n**Event:** {row['name']}\nStart: `{row['target_time']}`")
    except Exception as e:
        print("Reminder Error:", e)


# --- Bot Start ---
@bot.event
async def on_ready():
    
    print(f"{bot.user} ist online ✅")
    if not check_event_reminders.is_running():
        check_event_reminders.start()
    for guild in bot.guilds:
        guild_ = discord.Object(id=guild.id)
        await bot.tree.sync(guild=guild_) 
        print(f"Server-Name: {guild.name} | Server-ID: {guild.id}")

# pingpong test !ping
@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")


@bot.command()
async def welcome(ctx):
    member = ctx.author
    messages = [
        f"***{member.mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>***",
        f"***wow, wie toll! {member.mention} ist jetzt hier! <:welcome:1362364322772160513>***",
        f"***{member.mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>***",
        f"***Juhu, {member.mention} hat zur Insel gefunden!***",
        f"***Kuckt mal wer hier ist: {member.mention} ! <:pepehappy:1362364194967781598>***",
        f"***Herzlich Willkommen {member.mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>***",
        f"***{member.mention} ist dem Insel-Discord beigetreten! 🫡***",
        f"***Endlich! {member.mention} ist hier! 😇***",
        f"***Huhu {member.mention} . Willkommen 🙂***",
        f"***Ein wildes  {member.mention} ist auf die Insel geschlittert 😄***",
        f"***Wilkommen {member.mention} bei der Insel! <:pepehappy:1362364194967781598>***",
        f"***{member.mention}, was geht yallah <:welcome:1362364322772160513>***",
        f"***Oh halloo! {member.mention} 🙂 ***",
        f"***Heyyyy was geeeht {member.mention} 😀 ***",
        f"***{member.mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!***",
        f"***Seht Seht {member.mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>***",
        f"***Boar das schmeckt, {member.mention} ist nun hier!🙃***"
        ]
    for message in messages:
            embed = discord.Embed(
                title="👋 Willkommen!",
                description=message,
            
                color=discord.Color.green()
            )
            await ctx.send(embed=embed)

bot.run(TOKEN)
