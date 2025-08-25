import os
import sqlite3
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from datetime import datetime

from keep_alive import keep_alive

# --- Setup ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

keep_alive()  # Starte den Webserver, um den Bot am Leben zu halten

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DB_FILE = "events.db"


# --- DB Setup ---
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS events (
                 name TEXT PRIMARY KEY,
                 target_time TEXT
                 )""")
    conn.commit()
    conn.close()

init_db()


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

    return "Noch " + ", ".join(parts)


# --- Hilfsfunktion für Autocomplete ---
async def event_autocomplete(interaction: discord.Interaction, current: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name FROM events")
    rows = c.fetchall()
    conn.close()

    # Filter nach aktueller Eingabe
    return [
        app_commands.Choice(name=row[0], value=row[0])
        for row in rows if current.lower() in row[0].lower()
    ][:25]  # max. 25 Vorschläge erlaubt


# --- Slash Commands ---
@bot.tree.command(name="add_event", description="Neues Event hinzufügen (Format: YYYY-MM-DD HH:MM:SS)")
@app_commands.describe(name="Name des Events", zeitpunkt="Zeitpunkt im Format YYYY-MM-DD HH:MM:SS")
async def add_event(interaction: discord.Interaction, name: str, zeitpunkt: str):
    # --- Adminprüfung ---
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ Du brauchst **Administratorrechte**, um Events hinzuzufügen.",
            ephemeral=True
        )
    # --- Alternative: auf bestimmte Rolle prüfen ---
    # role_name = "EventManager"
    # if not any(role.name == role_name for role in interaction.user.roles):
    #     return await interaction.response.send_message(
    #         f"❌ Du brauchst die Rolle **{role_name}**, um das zu dürfen.",
    #         ephemeral=True
    #     )

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    try:
        c.execute("INSERT OR REPLACE INTO events (name, target_time) VALUES (?, ?)", (name, zeitpunkt))
        conn.commit()
        await interaction.response.send_message(f"✅ Event **{name}** für `{zeitpunkt}` gespeichert.")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ Fehler: {e}")
    finally:
        conn.close()


@bot.tree.command(name="time_until", description="Zeigt die Zeit bis zu einem gespeicherten Event")
@app_commands.autocomplete(name=event_autocomplete)
async def time_until(interaction: discord.Interaction, name: str):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT target_time FROM events WHERE name = ?", (name,))
    row = c.fetchone()
    conn.close()

    if row:
        result = format_time_until(row[0])
        await interaction.response.send_message(f"📅 Event **{name}** → {result}")
    else:
        await interaction.response.send_message(f"⚠️ Kein Event mit dem Namen **{name}** gefunden.")


@bot.tree.command(name="list_events", description="Zeigt alle gespeicherten Events")
async def list_events(interaction: discord.Interaction):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT name, target_time FROM events")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await interaction.response.send_message("📂 Keine Events gespeichert.")
        return

    msg = "**📅 Gespeicherte Events:**\n"
    for name, zeit in rows:
        msg += f"• **{name}** → `{zeit}`\n"

    await interaction.response.send_message(msg)




@bot.tree.command(name="remove_event", description="Löscht ein gespeichertes Event")
@app_commands.autocomplete(name=event_autocomplete)
async def remove_event(interaction: discord.Interaction, name: str):
    # --- Adminprüfung ---
    if not interaction.user.guild_permissions.administrator:
        return await interaction.response.send_message(
            "❌ Du brauchst **Administratorrechte**, um Events zu löschen.",
            ephemeral=True
        )

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM events WHERE name = ?", (name,))
    conn.commit()
    conn.close()

    await interaction.response.send_message(f"🗑️ Event **{name}** gelöscht.")

@bot.event
async def on_member_join(member):
    # Channel-ID des allgemeinen Channels, z.B. 123456789012345678
    # channel_id = "🌍-willkommen"
    # channel = bot.get_channel(channel_id)
    
    # if channel:
    #     embed = discord.Embed(
    #         title=f"***Willkommen {member.name} zu Die Insel!***",
    #         description=f"Bitte lies dir einmal die Regeln durch und setzte einen hacken. Schreibe bitte denen Minecraft-ingame Namen Namen in #minecraft-name damit wir dich immer zu ordnen können :)\n ",
    #         color=discord.Color.green()
    #     )
    #     embed.set_thumbnail(url=member.avatar.url if member.avatar else None)
    #     embed.add_field(name="Regeln", value="Bitte lese die Regeln im #regeln Channel durch.", inline=False)
    #     embed.set_footer(text="Viel Spaß auf unserem Server!")
        
    #     await channel.send(embed=embed)
    # else:
    #     print("Channel nicht gefunden!")
    pass


# Bot Start
@bot.event
async def on_ready():
    await bot.tree.sync()  # Slash Commands mit Discord synchronisieren
    print(f"{bot.user} ist online ✅")


bot.run(TOKEN)
