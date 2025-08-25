import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime
from supabase import create_client
from keep_alive import keep_alive

# --- Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
SUPABASE_URL = os.getenv("SupabaseURL")
SUPABASE_APIKEY = os.getenv("SupabaseAPIKEY")

keep_alive()

supabase = create_client(SUPABASE_URL, SUPABASE_APIKEY) 

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# --- Zeitberechnung ---
def format_time_until(target_time: str) -> str:
    now = datetime.now()
    try:
        target = datetime.strptime(target_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "⚠️ Falsches Format! Benutze 'YYYY-MM-DD HH:MM:SS'"

    delta = target - now
    if delta.total_seconds() < 0:
        return "⏰ Dieser Zeitpunkt liegt bereits in der Vergangenheit!"

    days = delta.days
    seconds = delta.seconds
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    seconds_left = seconds % 60

    months = days // 30
    days = days % 30
    weeks = days // 7
    days = days % 7

    parts = []
    if months: parts.append(f"{months} Monat{'e' if months != 1 else ''}")
    if weeks: parts.append(f"{weeks} Woche{'n' if weeks != 1 else ''}")
    if days: parts.append(f"{days} Tag{'e' if days != 1 else ''}")
    if hours: parts.append(f"{hours} Stunde{'n' if hours != 1 else ''}")
    if minutes: parts.append(f"{minutes} Minute{'n' if minutes != 1 else ''}")
    if seconds_left: parts.append(f"{seconds_left} Sekunde{'n' if seconds_left != 1 else ''}")

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


# --- Slash Commands ---
@bot.tree.command(name="add_event", description="Neues Event hinzufügen (Format: YYYY-MM-DD HH:MM:SS)")
@app_commands.describe(name="Name des Events", zeitpunkt="Startzeit im Format YYYY-MM-DD HH:MM:SS", endzeit="Endzeit im Format YYYY-MM-DD HH:MM:SS")
async def add_event(interaction: discord.Interaction, name: str, zeitpunkt: str, endzeit: str = None):
    if not interaction.user.guild_permissions.administrator:
        embed = discord.Embed(
            title="❌ Fehlende Berechtigung",
            description="Du brauchst **Administratorrechte**, um Events hinzuzufügen.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    server_id = str(interaction.guild.id)
    try:
        supabase.table("events").upsert({
            "name": name,
            "target_time": zeitpunkt,
            "end_time": endzeit,
            "serverid": server_id
        }).execute()

        embed = discord.Embed(
            title=f"✅ Event **{name}** gespeichert.",
            description=f"Start: `{zeitpunkt}`" + (f" | Ende: `{endzeit}`" if endzeit else ""),
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        embed = discord.Embed(
            title="❌ Fehler beim Hinzufügen des Events",
            description=str(e),
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
        embed = discord.Embed(
            title=f"⏱ Zeit bis Event {name}",
            color=discord.Color.green()
        )
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

        embed = discord.Embed(
            title="📂 Gespeicherte Events",
            description="Hier sind alle gespeicherten Events:",
            color=discord.Color.green()
        )
        for name, time in rows:
            embed.add_field(name=name, value=f"Startet am: {time}", inline=False)

        await interaction.response.send_message(embed=embed)

    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


@bot.tree.command(name="remove_event", description="Löscht ein gespeichertes Event")
@app_commands.autocomplete(name=event_autocomplete)
async def remove_event(interaction: discord.Interaction, name: str):
    if not interaction.user.guild_permissions.administrator:
        embed = discord.Embed(
            title="❌ Fehlende Berechtigung",
            description="Du brauchst **Administratorrechte**, um Events zu löschen.",
            color=discord.Color.red()
        )
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    server_id = str(interaction.guild.id)
    try:
        response = supabase.table("events").select("*").eq("name", name).eq("serverid", server_id).execute()
        if not response.data:
            return await interaction.response.send_message(
                f"❌ Kein Event mit dem Namen **{name}** gefunden.", ephemeral=True
            )

        supabase.table("events").delete().eq("name", name).eq("serverid", server_id).execute()
        embed = discord.Embed(
            title="✅ Event gelöscht",
            description=f"Das Event **{name}** wurde erfolgreich gelöscht.",
            color=discord.Color.yellow()
        )
        await interaction.response.send_message(embed=embed)

    except Exception as e:
        await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# --- Willkommensnachricht ---
# @bot.event
# async def on_member_join(member):
#     channel = discord.utils.get(member.guild.text_channels, name="willkommen")
#     if channel:
#         embed = discord.Embed(
#             title=f"***Willkommen {member.name} zu Die Insel!***",
#             description="Willkommen auf unserem Discord-Server, wir freuen uns dass du den Weg zu uns gefunden hast!",
#             color=discord.Color.green()
#         )
#         embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
#         embed.add_field(name="Regeln", value="Bitte lese die Regeln im #regeln Channel durch und setze einen Haken darunter", inline=False)
#         embed.set_footer(text="Wir wünschen dir viel Spaß auf unserem Server!")
#         await channel.send(embed=embed)


# --- Ping Command ---
# @bot.command()
async def ping(ctx):
    await ctx.send("Pong! 🏓")


# --- Bot Start ---
@bot.event
async def on_ready():
    print(f"{bot.user} ist online ✅")
    for guild in bot.guilds:
        print(f"Server-Name: {guild.name} | Server-ID: {guild.id}")


bot.run(TOKEN)
