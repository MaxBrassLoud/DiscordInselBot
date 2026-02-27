import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import random
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
import asyncio
from dotenv import load_dotenv
from keep_alive import keep_alive


keep_alive()

# Zufälliger Delay pro Instanz – verhindert dass mehrere Instanzen gleichzeitig senden
INSTANCE_DELAY = random.uniform(0.2, 1.5)


# Supabase Setup
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Bot Setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="?", intents=intents)

def has_rights(ctx: discord.Interaction):
    if ctx.user.guild_permissions.administrator:
        return True
    elif str(ctx.user.id) == str(os.getenv("MBL")):


        return True


    else:
        print(ctx.user.id)
        print(os.getenv("MBL"))
        return False

# Setup View
class SetupView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.ping_role = None
        self.channel = None
        self.delete_roles = []

        # Role Select für Ping-Rolle
        self.ping_role_select = discord.ui.RoleSelect(
            placeholder="Wähle die Rolle die gepingt werden soll",
            min_values=1,
            max_values=1,
            custom_id=f"ping_role_{guild_id}"
        )
        self.ping_role_select.callback = self.ping_role_callback
        self.add_item(self.ping_role_select)

        # Channel Select
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Wähle den Kanal für Spieleabende",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
            custom_id=f"channel_{guild_id}"
        )
        self.channel_select.callback = self.channel_callback
        self.add_item(self.channel_select)

        # Role Select für Lösch-Rollen
        self.delete_role_select = discord.ui.RoleSelect(
            placeholder="Wähle Rollen die Spieleabende löschen dürfen",
            min_values=1,
            max_values=10,
            custom_id=f"delete_roles_{guild_id}"
        )
        self.delete_role_select.callback = self.delete_roles_callback
        self.add_item(self.delete_role_select)

        # Speichern Button
        self.save_button = discord.ui.Button(
            label="Speichern",
            style=discord.ButtonStyle.success,
            emoji="💾",
            custom_id=f"save_{guild_id}",
            disabled=True
        )
        self.save_button.callback = self.save_callback
        self.add_item(self.save_button)

    async def ping_role_callback(self, interaction: discord.Interaction):
        self.ping_role = interaction.data['values'][0]
        await self.update_status(interaction)

    async def channel_callback(self, interaction: discord.Interaction):
        self.channel = interaction.data['values'][0]
        await self.update_status(interaction)

    async def delete_roles_callback(self, interaction: discord.Interaction):
        self.delete_roles = interaction.data['values']
        await self.update_status(interaction)

    async def update_status(self, interaction: discord.Interaction):
        # Aktiviere Speichern-Button wenn alles ausgefüllt
        if self.ping_role and self.channel and self.delete_roles:
            self.save_button.disabled = False

        # Update Embed
        embed = interaction.message.embeds[0]

        ping_role_text = f"<@&{self.ping_role}>" if self.ping_role else "*Nicht ausgewählt*"
        channel_text = f"<#{self.channel}>" if self.channel else "*Nicht ausgewählt*"
        delete_roles_text = " ".join(
            [f"<@&{rid}>" for rid in self.delete_roles]) if self.delete_roles else "*Nicht ausgewählt*"

        embed.clear_fields()
        embed.add_field(name="🔔 Ping Rolle", value=ping_role_text, inline=False)
        embed.add_field(name="📢 Kanal", value=channel_text, inline=False)
        embed.add_field(name="🗑️ Lösch-Rollen", value=delete_roles_text, inline=False)

        await interaction.response.edit_message(embed=embed, view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            # Speichere Einstellungen in Supabase
            data = {
                "guild_id": str(self.guild_id),
                "ping_role_id": str(self.ping_role),
                "channel_id": str(self.channel),
                "delete_role_ids": ",".join([str(rid) for rid in self.delete_roles])
            }

            # Prüfe ob Einstellungen existieren
            existing = supabase.table("settings").select("*").eq("guild_id", str(self.guild_id)).execute()

            if existing.data:
                supabase.table("settings").update(data).eq("guild_id", str(self.guild_id)).execute()
            else:
                supabase.table("settings").insert(data).execute()

            # Update Embed
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = "✅ Setup erfolgreich gespeichert!"

            # Deaktiviere alle Buttons
            for item in self.children:
                item.disabled = True

            await interaction.response.edit_message(embed=embed, view=self)

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler beim Speichern: {str(e)}", ephemeral=True)


# Modals
class SpielabendModal(discord.ui.Modal, title="Spieleabend erstellen"):
    def __init__(self):
        super().__init__()

    titel = discord.ui.TextInput(
        label="Titel (Spiel/Aktivität)",
        placeholder="z.B. Valorant, Minecraft, etc.",
        required=True,
        max_length=100
    )

    uhrzeit = discord.ui.TextInput(
        label="Uhrzeit",
        placeholder="z.B. 20:00 oder 03.01.2026 20:00",
        required=True,
        max_length=50
    )

    beschreibung = discord.ui.TextInput(
        label="Beschreibung (Optional)",
        placeholder="Weitere Details zum Spieleabend...",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            # Hole Einstellungen
            settings = supabase.table("settings").select("*").eq("guild_id", str(interaction.guild_id)).execute()

            if not settings.data:
                await interaction.followup.send("❌ Bitte führe zuerst `/setup_spieleabend` aus!", ephemeral=True)
                return

            config = settings.data[0]
            channel = bot.get_channel(int(config['channel_id']))

            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return

            # Parse Zeitpunkt
            zeitpunkt = self.parse_time(self.uhrzeit.value)

            # Erstelle Embed
            embed = discord.Embed(
                title=f"🎮 {self.titel.value}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="⏰ Uhrzeit", value=self.uhrzeit.value, inline=False)

            if self.beschreibung.value:
                embed.add_field(name="📝 Beschreibung", value=self.beschreibung.value, inline=False)

            embed.add_field(name="✅ Dabei", value="*Niemand*", inline=False)
            embed.add_field(name="❓ Vielleicht", value="*Niemand*", inline=False)
            embed.add_field(name="❌ Keine Zeit", value="*Niemand*", inline=False)

            # Ping Rolle
            role = interaction.guild.get_role(int(config['ping_role_id']))
            ping_text = role.mention if role else "@everyone"

            # Sende Nachricht
            view = SpielabendView()
            message = await channel.send(content=ping_text, embed=embed, view=view)

            # Erstelle Thread
            thread = await message.create_thread(
                name=f"💬 {self.titel.value}",
                auto_archive_duration=1440
            )

            await thread.send(f"Hier könnt ihr über den Spieleabend **{self.titel.value}** diskutieren! 🎮")

            # Speichere in Datenbank
            game_night_data = {
                "guild_id": str(interaction.guild_id),
                "message_id": str(message.id),
                "thread_id": str(thread.id),
                "titel": self.titel.value,
                "uhrzeit": self.uhrzeit.value,
                "zeitpunkt": zeitpunkt.isoformat() if zeitpunkt else None,
                "beschreibung": self.beschreibung.value or None,
                "dabei": [],
                "vielleicht": [],
                "keine_zeit": [],
                "creator_id": str(interaction.user.id)
            }

            result = supabase.table("game_nights").insert(game_night_data).execute()

            if result.data:
                game_night_id = result.data[0]['id']
                embed.set_footer(text=f"Spieleabend ID: {game_night_id}")
                await message.edit(embed=embed)

            await interaction.followup.send(f"✅ Spieleabend erstellt! {message.jump_url}", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {str(e)}", ephemeral=True)

    def parse_time(self, time_str: str):
        """Parse verschiedene Zeitformate"""
        try:
            # UTC+1 für Deutschland (Winterzeit) / UTC+2 (Sommerzeit)
            # Verwende einfach UTC und user gibt lokale Zeit ein
            tz = timezone(timedelta(hours=1))  # MEZ (Winterzeit)

            # Versuche HH:MM Format (heute)
            if ":" in time_str and len(time_str.split()) == 1:
                time_parts = time_str.split(":")
                now = datetime.now(tz)
                target = now.replace(hour=int(time_parts[0]), minute=int(time_parts[1]), second=0, microsecond=0)
                if target < now:
                    target += timedelta(days=1)
                return target

            # Versuche DD.MM.YYYY HH:MM Format
            for fmt in ["%d.%m.%Y %H:%M", "%d.%m. %H:%M"]:
                try:
                    parsed = datetime.strptime(time_str, fmt)
                    if fmt == "%d.%m. %H:%M":
                        parsed = parsed.replace(year=datetime.now(tz).year)
                    # Füge Zeitzone hinzu
                    parsed = parsed.replace(tzinfo=tz)
                    return parsed
                except:
                    continue

            return None
        except:
            return None


# Views
class SpielabendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dabei", style=discord.ButtonStyle.success, custom_id="dabei", emoji="✅")
    async def dabei_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "dabei")

    @discord.ui.button(label="Vielleicht", style=discord.ButtonStyle.primary, custom_id="vielleicht", emoji="❓")
    async def vielleicht_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "vielleicht")

    @discord.ui.button(label="Keine Zeit", style=discord.ButtonStyle.danger, custom_id="keine_zeit", emoji="❌")
    async def keine_zeit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "keine_zeit")

    async def handle_response(self, interaction: discord.Interaction, status: str):
        user_id = str(interaction.user.id)
        message_id = str(interaction.message.id)

        try:
            # Hole Spieleabend aus DB
            result = supabase.table("game_nights").select("*").eq("message_id", message_id).execute()

            if not result.data:
                await interaction.response.send_message("❌ Spieleabend nicht gefunden!", ephemeral=True)
                return

            game_night = result.data[0]

            # Entferne User aus allen Listen
            dabei = [uid for uid in game_night.get('dabei', []) if uid != user_id]
            vielleicht = [uid for uid in game_night.get('vielleicht', []) if uid != user_id]
            keine_zeit = [uid for uid in game_night.get('keine_zeit', []) if uid != user_id]

            # Füge zu gewählter Liste hinzu
            if status == "dabei":
                dabei.append(user_id)
            elif status == "vielleicht":
                vielleicht.append(user_id)
            elif status == "keine_zeit":
                keine_zeit.append(user_id)

            # Update Datenbank
            supabase.table("game_nights").update({
                "dabei": dabei,
                "vielleicht": vielleicht,
                "keine_zeit": keine_zeit
            }).eq("message_id", message_id).execute()

            # Update Embed
            embed = interaction.message.embeds[0]

            dabei_text = " ".join([f"<@{uid}>" for uid in dabei]) or "*Niemand*"
            vielleicht_text = " ".join([f"<@{uid}>" for uid in vielleicht]) or "*Niemand*"
            keine_zeit_text = " ".join([interaction.guild.get_member(int(uid)).display_name for uid in keine_zeit if
                                        interaction.guild.get_member(int(uid))]) or "*Niemand*"

            # Finde richtige Field Indizes
            for i, field in enumerate(embed.fields):
                if field.name == "✅ Dabei":
                    embed.set_field_at(i, name="✅ Dabei", value=dabei_text, inline=False)
                elif field.name == "❓ Vielleicht":
                    embed.set_field_at(i, name="❓ Vielleicht", value=vielleicht_text, inline=False)
                elif field.name == "❌ Keine Zeit":
                    embed.set_field_at(i, name="❌ Keine Zeit", value=keine_zeit_text, inline=False)

            await interaction.message.edit(embed=embed)
            await interaction.response.send_message("✅ Status aktualisiert!", ephemeral=True)

        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {str(e)}", ephemeral=True)


# Commands
@bot.tree.command(name="setup_spieleabend", description="Konfiguriere den Spieleabend Bot")
async def setup_spieleabend(interaction: discord.Interaction):
    if has_rights(interaction):
        embed = discord.Embed(
            title="⚙️ Spieleabend Bot Setup",
            description="Wähle die Einstellungen für den Spieleabend Bot aus:",
            color=discord.Color.blue()
        )
        embed.add_field(name="🔔 Ping Rolle", value="*Nicht ausgewählt*", inline=False)
        embed.add_field(name="📢 Kanal", value="*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🗑️ Lösch-Rollen", value="*Nicht ausgewählt*", inline=False)
        embed.set_footer(text="Wähle alle Optionen aus und klicke dann auf Speichern")

        view = SetupView(interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    else:
        embed = discord.Embed(
            title="Keine Berechtigung",
            description="Du hast nicht die Berechtigung diesen Command auszuführen"
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="spieleabend", description="Erstelle einen neuen Spieleabend")
async def spieleabend(interaction: discord.Interaction):
    await interaction.response.send_modal(SpielabendModal())


@bot.tree.command(name="spieleabend_loeschen", description="Lösche einen Spieleabend")
@app_commands.describe(spieleabend_id="Die ID des Spieleabends")
async def spieleabend_loeschen(interaction: discord.Interaction, spieleabend_id: int):
    await interaction.response.defer(ephemeral=True)

    try:
        settings = supabase.table("settings").select("*").eq(
            "guild_id", str(interaction.guild_id)
        ).execute()

        if not settings.data:
            await interaction.followup.send("❌ Keine Einstellungen gefunden!")
            return

        config = settings.data[0]
        delete_role_ids = config["delete_role_ids"].split(",")

        user_role_ids = [str(r.id) for r in interaction.user.roles]
        has_permission = (
            any(rid in delete_role_ids for rid in user_role_ids)
            or interaction.user.guild_permissions.administrator
        )

        result = supabase.table("game_nights").select("*").eq(
            "id", spieleabend_id
        ).execute()

        if not result.data:
            await interaction.followup.send("❌ Spieleabend nicht gefunden!")
            return

        game_night = result.data[0]

        if str(interaction.user.id) == game_night["creator_id"]:
            has_permission = True

        if not has_permission:
            await interaction.followup.send("❌ Keine Berechtigung!")
            return

        try:
            channel = bot.get_channel(int(config["channel_id"]))
            message = await channel.fetch_message(int(game_night["message_id"]))
            await message.delete()

            thread = await bot.fetch_channel(int(game_night["thread_id"]))
            print(thread)
            await thread.delete()
        except:
            pass

        supabase.table("game_nights").delete().eq("id", spieleabend_id).execute()

        await interaction.followup.send("✅ Spieleabend gelöscht!")

    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}")



# Background Tasks
@tasks.loop(minutes=1)
async def check_reminders():
    """Prüfe auf bevorstehende Spieleabende"""
    try:
        # UTC+1 für Deutschland (Winterzeit)
        tz = timezone(timedelta(hours=1))
        now = datetime.now(tz)

        # Hole alle Spieleabende
        result = supabase.table("game_nights").select("*").execute()

        for game_night in result.data:
            if not game_night.get('zeitpunkt'):
                continue

            # Parse Zeitpunkt mit Zeitzone
            zeitpunkt_str = game_night['zeitpunkt']
            zeitpunkt = datetime.fromisoformat(zeitpunkt_str)

            # Stelle sicher, dass zeitpunkt timezone-aware ist
            if zeitpunkt.tzinfo is None:
                zeitpunkt = zeitpunkt.replace(tzinfo=tz)

            time_diff = (zeitpunkt - now).total_seconds() / 60

            thread = bot.get_channel(int(game_night['thread_id']))
            if not thread:
                continue

            # 1 Stunde vorher - Ping Vielleicht
            if 59 <= time_diff <= 61 and game_night.get('vielleicht'):
                if not game_night.get('reminded_1h'):
                    mentions = " ".join([f"<@{uid}>" for uid in game_night['vielleicht']])
                    await thread.send(f"⏰ **1 Stunde bis zum Start!**\n{mentions} - Habt ihr doch noch Zeit?")
                    supabase.table("game_nights").update({"reminded_1h": True}).eq("id", game_night['id']).execute()

            # 10 Minuten vorher - Ping Dabei
            if 9 <= time_diff <= 11 and game_night.get('dabei'):
                if not game_night.get('reminded_10m'):
                    mentions = " ".join([f"<@{uid}>" for uid in game_night['dabei']])
                    await thread.send(f"⏰ **10 Minuten bis zum Start!**\n{mentions}")
                    supabase.table("game_nights").update({"reminded_10m": True}).eq("id", game_night['id']).execute()

            # Bei Beginn - Ping Dabei
            if -1 <= time_diff <= 1 and game_night.get('dabei'):
                if not game_night.get('reminded_start'):
                    mentions = " ".join([f"<@{uid}>" for uid in game_night['dabei']])
                    await thread.send(f"🎮 **Es geht los!**\n{mentions}")
                    supabase.table("game_nights").update({"reminded_start": True}).eq("id", game_night['id']).execute()

    except Exception as e:
        print(f"Fehler bei check_reminders: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    if interaction.response.is_done():
        await interaction.followup.send(
            f"❌ Fehler: {error}", ephemeral=True
        )
    else:
        await interaction.response.send_message(
            f"❌ Fehler: {error}", ephemeral=True
        )

    print(error)

@bot.command()
async def Ping(ctx):
    await ctx.send("Pong!")
    return



@bot.event
async def on_message(message: discord.Message):
    # Bot-Nachrichten ignorieren
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Nur in Gilden
    if not message.guild:
        await bot.process_commands(message)
        return

    # Prüfe ob Anhänge mit Bildern vorhanden sind
    image_attachments = [
        a for a in message.attachments
        if a.content_type and a.content_type.startswith("image/")
    ]

    if image_attachments:
        try:
            settings = supabase.table("settings").select("*").eq("guild_id", str(message.guild.id)).execute()

            if settings.data:
                config = settings.data[0]
                image_channel_id = config.get("image_channel_id")

                if image_channel_id:
                    image_channel = bot.get_channel(int(image_channel_id))

                    if image_channel and message.channel.id != image_channel.id:

                        # ── Deduplizierung: Discord-Kanal als Lock ────────────────────
                        # Jede Instanz wartet eine deterministisch unterschiedliche Zeit
                        # basierend auf ihrer Bot-User-ID (alle Instanzen haben dieselbe
                        # Bot-ID → gleiche Wartezeit). Daher: zufälligen lokalen Offset
                        # beim Start generieren und als Modul-Variable speichern.
                        await asyncio.sleep(INSTANCE_DELAY)

                        # Prüfe die letzten Nachrichten im Bilder-Kanal ob das Bild
                        # bereits von einer anderen Instanz gepostet wurde
                        already_posted = False
                        async for recent_msg in image_channel.history(limit=20):
                            if recent_msg.author.id == bot.user.id and recent_msg.embeds:
                                for emb in recent_msg.embeds:
                                    for field in emb.fields:
                                        if field.name == "🔗 Nachricht" and str(message.id) in field.value:
                                            already_posted = True
                                            break

                        if already_posted:
                            await bot.process_commands(message)
                            return
                        # ─────────────────────────────────────────────────────────────

                        for attachment in image_attachments:
                            embed = discord.Embed(
                                color=discord.Color.blurple(),
                                timestamp=message.created_at
                            )
                            embed.set_image(url=attachment.url)
                            embed.set_author(
                                name=message.author.display_name,
                                icon_url=message.author.display_avatar.url
                            )
                            embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
                            # message.id ist im jump_url enthalten – wird als Lock-Key genutzt
                            embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
                            if message.content:
                                embed.add_field(name="💬 Text", value=message.content[:500], inline=False)

                            await image_channel.send(embed=embed)

        except Exception as e:
            print(f"Fehler beim Bild-Weiterleiten: {e}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"Bot ist online als {bot.user}")

    # Registriere persistente Views
    bot.add_view(SpielabendView())


    # Sync Commands
    await bot.tree.sync()
    print("Commands synchronisiert")

    # Starte Background Tasks
    if not check_reminders.is_running():
        check_reminders.start()

    print("Bot ist bereit!")


# Starte Bot
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)