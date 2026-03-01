import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import re
import random
import asyncio
import aiohttp
import yt_dlp
from datetime import datetime, timedelta, timezone
from supabase import create_client, Client
from dotenv import load_dotenv
from keep_alive import keep_alive


keep_alive()

# ── Instanz-Delay für Deduplizierung ─────────────────────────────────────────
INSTANCE_DELAY = random.uniform(0.2, 1.5)

# ── Supabase Setup ────────────────────────────────────────────────────────────
# Absoluter Pfad zur .env – funktioniert unabhängig vom Working Directory
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, ".env"))

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="?", intents=intents)

# ── Regex Patterns ────────────────────────────────────────────────────────────
YOUTUBE_PATTERN = re.compile(
    r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-&=?%]+)'
)
TWITCH_CLIP_PATTERN = re.compile(
    r'(https?://(?:www\.)?twitch\.tv/[\w]+/clip/[\w\-]+|https?://clips\.twitch\.tv/[\w\-]+)'
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v", ".wmv"}

# ═════════════════════════════════════════════════════════════════════════════
# RAM-CACHE SYSTEM
# ═════════════════════════════════════════════════════════════════════════════
CACHE_TTL = 5 * 60  # 5 Minuten in Sekunden

class SettingsCache:
    def __init__(self):
        self._cache: dict[str, dict] = {}

    def get(self, guild_id: str) -> dict | None:
        entry = self._cache.get(guild_id)
        if not entry:
            return None
        if (datetime.now().timestamp() - entry["ts"]) > CACHE_TTL:
            del self._cache[guild_id]
            return None
        return entry["data"]

    def set(self, guild_id: str, data: dict):
        self._cache[guild_id] = {"data": data, "ts": datetime.now().timestamp()}

    def invalidate(self, guild_id: str):
        self._cache.pop(guild_id, None)

settings_cache = SettingsCache()


async def get_settings(guild_id: str) -> dict | None:
    cached = settings_cache.get(guild_id)
    if cached is not None:
        return cached
    result = supabase.table("settings").select("*").eq("guild_id", guild_id).execute()
    if result.data:
        settings_cache.set(guild_id, result.data[0])
        return result.data[0]
    return None


# ═════════════════════════════════════════════════════════════════════════════
# HILFSFUNKTIONEN
# ═════════════════════════════════════════════════════════════════════════════

def has_rights(ctx: discord.Interaction) -> bool:
    if ctx.user.guild_permissions.administrator:
        return True
    elif str(ctx.user.id) == str(os.getenv("MBL")):
        return True
    else:
        return False


async def get_youtube_info(url: str) -> dict | None:
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        return info
    except Exception as e:
        print(f"[YouTube] Fehler beim Abrufen der Infos: {e}")
        return None


async def check_already_posted(image_channel: discord.TextChannel, message_id: int) -> bool:
    target_id = str(message_id)
    async for recent_msg in image_channel.history(limit=50):
        if recent_msg.author.id == bot.user.id and recent_msg.embeds:
            for emb in recent_msg.embeds:
                for field in emb.fields:
                    if field.name == "🔗 Nachricht":
                        parts = field.value.split("/")
                        if parts and parts[-1].rstrip(")") == target_id:
                            return True
    return False


# ── Media-Forward Funktionen ──────────────────────────────────────────────────

async def forward_image(image_channel: discord.TextChannel, attachment: discord.Attachment, message: discord.Message):
    embed = discord.Embed(color=0x5865F2, timestamp=message.created_at)
    embed.set_image(url=attachment.url)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    if message.content:
        embed.add_field(name="💬 Text", value=message.content[:500], inline=False)
    embed.set_footer(text="📷 Bild")
    await image_channel.send(embed=embed)


async def forward_video(image_channel: discord.TextChannel, attachment: discord.Attachment, message: discord.Message):
    embed = discord.Embed(color=0xFEE75C, timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    embed.add_field(name="🎬 Video", value=f"[{attachment.filename}]({attachment.url})", inline=False)
    if message.content:
        embed.add_field(name="💬 Text", value=message.content[:500], inline=False)
    embed.set_footer(text="🎥 Video")
    await image_channel.send(embed=embed)


async def forward_youtube(image_channel: discord.TextChannel, url: str, message: discord.Message):
    info = await get_youtube_info(url)
    embed = discord.Embed(
        color=0xED4245,
        timestamp=message.created_at,
        title=info.get("title", "YouTube Video") if info else "YouTube Video",
        url=url
    )
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    if info:
        thumbnail = info.get("thumbnail")
        if thumbnail:
            embed.set_image(url=thumbnail)
        channel_name = info.get("uploader") or info.get("channel")
        if channel_name:
            embed.add_field(name="📺 Kanal", value=channel_name, inline=True)
        duration = info.get("duration")
        if duration:
            minutes, seconds = divmod(int(duration), 60)
            hours, minutes = divmod(minutes, 60)
            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
            embed.add_field(name="⏱️ Länge", value=duration_str, inline=True)
    embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    clean_content = message.content.replace(url, "").strip() if message.content else ""
    if clean_content:
        embed.add_field(name="💬 Text", value=clean_content[:500], inline=False)
    embed.set_footer(text="🎬 YouTube")
    await image_channel.send(embed=embed)


async def forward_twitch_clip(image_channel: discord.TextChannel, url: str, message: discord.Message):
    embed = discord.Embed(color=0x9B59B6, timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal", value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    embed.add_field(name="🎮 Twitch Clip", value=url, inline=False)
    clean_content = message.content.replace(url, "").strip() if message.content else ""
    if clean_content:
        embed.add_field(name="💬 Text", value=clean_content[:500], inline=False)
    embed.set_footer(text="💜 Twitch Clip")
    await image_channel.send(content=url, embed=embed)


# ═════════════════════════════════════════════════════════════════════════════
# SETUP MEDIA VIEW
# ═════════════════════════════════════════════════════════════════════════════

class SetupMediaView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.image_channel_id: str | None = None

        cfg = current_config or {}
        self.feature_images  = bool(cfg.get("forward_images",  True))
        self.feature_videos  = bool(cfg.get("forward_videos",  True))
        self.feature_youtube = bool(cfg.get("forward_youtube", True))
        self.feature_twitch  = bool(cfg.get("forward_twitch",  True))

        if cfg.get("image_channel_id"):
            self.image_channel_id = str(cfg["image_channel_id"])

        self.channel_select = discord.ui.ChannelSelect(
            placeholder="📺 Ziel-Kanal für weitergeleitete Medien",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.channel_select.callback = self.channel_callback
        self.add_item(self.channel_select)

        self._rebuild_buttons()

    def _rebuild_buttons(self):
        to_remove = [item for item in self.children if isinstance(item, discord.ui.Button)]
        for item in to_remove:
            self.remove_item(item)

        def make_toggle(label: str, emoji: str, feature: str, state: bool):
            btn = discord.ui.Button(
                label=f"{label}: {'AN ✅' if state else 'AUS ❌'}",
                emoji=emoji,
                style=discord.ButtonStyle.success if state else discord.ButtonStyle.secondary,
            )
            async def callback(interaction: discord.Interaction, f=feature):
                setattr(self, f, not getattr(self, f))
                self._rebuild_buttons()
                await interaction.response.edit_message(embed=self._build_embed(), view=self)
            btn.callback = callback
            return btn

        self.add_item(make_toggle("Bilder",   "📷", "feature_images",  self.feature_images))
        self.add_item(make_toggle("Videos",   "🎥", "feature_videos",  self.feature_videos))
        self.add_item(make_toggle("YouTube",  "▶️", "feature_youtube", self.feature_youtube))
        self.add_item(make_toggle("Twitch",   "💜", "feature_twitch",  self.feature_twitch))

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.success,
            disabled=self.image_channel_id is None
        )
        save_btn.callback = self.save_callback
        self.add_item(save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Media-Weiterleitung Setup",
            description="Wähle den Ziel-Kanal und aktiviere/deaktiviere die gewünschten Features.",
            color=discord.Color.blurple()
        )
        channel_text = f"<#{self.image_channel_id}>" if self.image_channel_id else "*Nicht ausgewählt*"
        embed.add_field(name="📺 Ziel-Kanal", value=channel_text, inline=False)
        embed.add_field(
            name="🔧 Features",
            value=(
                f"📷 Bilder: {'✅ AN' if self.feature_images else '❌ AUS'}\n"
                f"🎥 Videos: {'✅ AN' if self.feature_videos else '❌ AUS'}\n"
                f"▶️ YouTube: {'✅ AN' if self.feature_youtube else '❌ AUS'}\n"
                f"💜 Twitch Clips: {'✅ AN' if self.feature_twitch else '❌ AUS'}"
            ),
            inline=False
        )
        embed.set_footer(text="Farben: Bilder=Blau | Videos=Gelb | YouTube=Rot | Twitch=Lila")
        return embed

    async def channel_callback(self, interaction: discord.Interaction):
        self.image_channel_id = interaction.data['values'][0]
        self._rebuild_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            data = {
                "guild_id": str(self.guild_id),
                "image_channel_id": self.image_channel_id,
                "forward_images":   self.feature_images,
                "forward_videos":   self.feature_videos,
                "forward_youtube":  self.feature_youtube,
                "forward_twitch":   self.feature_twitch,
            }
            existing = supabase.table("settings").select("id").eq("guild_id", str(self.guild_id)).execute()
            if existing.data:
                supabase.table("settings").update(data).eq("guild_id", str(self.guild_id)).execute()
            else:
                supabase.table("settings").insert(data).execute()
            settings_cache.invalidate(str(self.guild_id))
            embed = self._build_embed()
            embed.color = discord.Color.green()
            embed.title = "✅ Media-Setup gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler beim Speichern: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler beim Speichern: {e}", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
# SPIELEABEND SETUP VIEW
# ═════════════════════════════════════════════════════════════════════════════

class SetupView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.ping_role = None
        self.channel = None
        self.delete_roles = []

        self.ping_role_select = discord.ui.RoleSelect(
            placeholder="Wähle die Rolle die gepingt werden soll",
            min_values=1, max_values=1,
        )
        self.ping_role_select.callback = self.ping_role_callback
        self.add_item(self.ping_role_select)

        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Wähle den Kanal für Spieleabende",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.channel_select.callback = self.channel_callback
        self.add_item(self.channel_select)

        self.delete_role_select = discord.ui.RoleSelect(
            placeholder="Wähle Rollen die Spieleabende löschen dürfen",
            min_values=1, max_values=10,
        )
        self.delete_role_select.callback = self.delete_roles_callback
        self.add_item(self.delete_role_select)

        self.save_button = discord.ui.Button(
            label="Speichern", style=discord.ButtonStyle.success,
            emoji="💾", disabled=True
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
        if self.ping_role and self.channel and self.delete_roles:
            self.save_button.disabled = False
        embed = interaction.message.embeds[0]
        embed.clear_fields()
        embed.add_field(name="🔔 Ping Rolle",   value=f"<@&{self.ping_role}>" if self.ping_role else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="📢 Kanal",        value=f"<#{self.channel}>" if self.channel else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🗑️ Lösch-Rollen", value=" ".join([f"<@&{rid}>" for rid in self.delete_roles]) if self.delete_roles else "*Nicht ausgewählt*", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            data = {
                "guild_id":       str(self.guild_id),
                "ping_role_id":   str(self.ping_role),
                "channel_id":     str(self.channel),
                "delete_role_ids": ",".join([str(rid) for rid in self.delete_roles])
            }
            existing = supabase.table("settings").select("*").eq("guild_id", str(self.guild_id)).execute()
            if existing.data:
                supabase.table("settings").update(data).eq("guild_id", str(self.guild_id)).execute()
            else:
                supabase.table("settings").insert(data).execute()
            settings_cache.invalidate(str(self.guild_id))
            embed = interaction.message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = "✅ Setup erfolgreich gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler beim Speichern: {str(e)}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler beim Speichern: {str(e)}", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
# SPIELEABEND MODAL & VIEW
# ═════════════════════════════════════════════════════════════════════════════

class SpielabendModal(discord.ui.Modal, title="Spieleabend erstellen"):
    titel = discord.ui.TextInput(
        label="Titel (Spiel/Aktivität)",
        placeholder="z.B. Valorant, Minecraft, etc.",
        required=True, max_length=100
    )
    uhrzeit = discord.ui.TextInput(
        label="Uhrzeit",
        placeholder="z.B. 20:00 oder 03.01.2026 20:00",
        required=True, max_length=50
    )
    beschreibung = discord.ui.TextInput(
        label="Beschreibung (Optional)",
        placeholder="Weitere Details zum Spieleabend...",
        required=False, style=discord.TextStyle.paragraph, max_length=500
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            config = await get_settings(str(interaction.guild_id))
            if not config:
                await interaction.followup.send("❌ Bitte führe zuerst `/setup_spieleabend` aus!", ephemeral=True)
                return
            channel = bot.get_channel(int(config['channel_id']))
            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return
            zeitpunkt = self.parse_time(self.uhrzeit.value)
            embed = discord.Embed(
                title=f"🎮 {self.titel.value}",
                color=discord.Color.blue(),
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="⏰ Uhrzeit", value=self.uhrzeit.value, inline=False)
            if self.beschreibung.value:
                embed.add_field(name="📝 Beschreibung", value=self.beschreibung.value, inline=False)
            embed.add_field(name="✅ Dabei",    value="*Niemand*", inline=False)
            embed.add_field(name="❓ Vielleicht", value="*Niemand*", inline=False)
            embed.add_field(name="❌ Keine Zeit", value="*Niemand*", inline=False)
            role = interaction.guild.get_role(int(config['ping_role_id']))
            ping_text = role.mention if role else "@everyone"
            view = SpielabendView()
            message = await channel.send(content=ping_text, embed=embed, view=view)
            thread = await message.create_thread(name=f"💬 {self.titel.value}", auto_archive_duration=1440)
            await thread.send(f"Hier könnt ihr über den Spieleabend **{self.titel.value}** diskutieren! 🎮")
            game_night_data = {
                "guild_id":    str(interaction.guild_id),
                "message_id":  str(message.id),
                "thread_id":   str(thread.id),
                "titel":       self.titel.value,
                "uhrzeit":     self.uhrzeit.value,
                "zeitpunkt":   zeitpunkt.isoformat() if zeitpunkt else None,
                "beschreibung": self.beschreibung.value or None,
                "dabei":       [],
                "vielleicht":  [],
                "keine_zeit":  [],
                "creator_id":  str(interaction.user.id)
            }
            result = supabase.table("game_nights").insert(game_night_data).execute()
            if result.data:
                embed.set_footer(text=f"Spieleabend ID: {result.data[0]['id']}")
                await message.edit(embed=embed)
            await interaction.followup.send(f"✅ Spieleabend erstellt! {message.jump_url}", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {str(e)}", ephemeral=True)

    def parse_time(self, time_str: str):
        try:
            tz = timezone(timedelta(hours=1))
            if ":" in time_str and len(time_str.split()) == 1:
                parts = time_str.split(":")
                now = datetime.now(tz)
                target = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
                if target < now:
                    target += timedelta(days=1)
                return target
            for fmt in ["%d.%m.%Y %H:%M", "%d.%m. %H:%M"]:
                try:
                    parsed = datetime.strptime(time_str, fmt)
                    if fmt == "%d.%m. %H:%M":
                        parsed = parsed.replace(year=datetime.now(tz).year)
                    return parsed.replace(tzinfo=tz)
                except:
                    continue
            return None
        except:
            return None


class SpielabendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dabei",     style=discord.ButtonStyle.success, custom_id="dabei",     emoji="✅")
    async def dabei_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "dabei")

    @discord.ui.button(label="Vielleicht", style=discord.ButtonStyle.primary, custom_id="vielleicht", emoji="❓")
    async def vielleicht_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "vielleicht")

    @discord.ui.button(label="Keine Zeit", style=discord.ButtonStyle.danger,  custom_id="keine_zeit", emoji="❌")
    async def keine_zeit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_response(interaction, "keine_zeit")

    async def handle_response(self, interaction: discord.Interaction, status: str):
        user_id    = str(interaction.user.id)
        message_id = str(interaction.message.id)
        try:
            result = supabase.table("game_nights").select("*").eq("message_id", message_id).execute()
            if not result.data:
                await interaction.response.send_message("❌ Spieleabend nicht gefunden!", ephemeral=True)
                return
            gn = result.data[0]
            dabei     = [uid for uid in gn.get('dabei',     []) if uid != user_id]
            vielleicht = [uid for uid in gn.get('vielleicht', []) if uid != user_id]
            keine_zeit = [uid for uid in gn.get('keine_zeit', []) if uid != user_id]
            if status == "dabei":      dabei.append(user_id)
            elif status == "vielleicht": vielleicht.append(user_id)
            elif status == "keine_zeit": keine_zeit.append(user_id)
            supabase.table("game_nights").update({
                "dabei": dabei, "vielleicht": vielleicht, "keine_zeit": keine_zeit
            }).eq("message_id", message_id).execute()
            embed = interaction.message.embeds[0]
            dabei_text     = " ".join([f"<@{uid}>" for uid in dabei])     or "*Niemand*"
            vielleicht_text = " ".join([f"<@{uid}>" for uid in vielleicht]) or "*Niemand*"
            keine_zeit_text = " ".join([
                interaction.guild.get_member(int(uid)).display_name
                for uid in keine_zeit if interaction.guild.get_member(int(uid))
            ]) or "*Niemand*"
            for i, field in enumerate(embed.fields):
                if field.name == "✅ Dabei":
                    embed.set_field_at(i, name="✅ Dabei",     value=dabei_text,      inline=False)
                elif field.name == "❓ Vielleicht":
                    embed.set_field_at(i, name="❓ Vielleicht", value=vielleicht_text, inline=False)
                elif field.name == "❌ Keine Zeit":
                    embed.set_field_at(i, name="❌ Keine Zeit", value=keine_zeit_text, inline=False)
            await interaction.message.edit(embed=embed)
            await interaction.response.send_message("✅ Status aktualisiert!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {str(e)}", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
# SLASH COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

@bot.tree.command(name="setup_spieleabend", description="Konfiguriere den Spieleabend Bot")
async def setup_spieleabend(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message(
            embed=discord.Embed(title="Keine Berechtigung", description="Du hast nicht die nötige Berechtigung."),
            ephemeral=True
        )
        return
    embed = discord.Embed(
        title="⚙️ Spieleabend Bot Setup",
        description="Wähle die Einstellungen für den Spieleabend Bot aus:",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔔 Ping Rolle",   value="*Nicht ausgewählt*", inline=False)
    embed.add_field(name="📢 Kanal",        value="*Nicht ausgewählt*", inline=False)
    embed.add_field(name="🗑️ Lösch-Rollen", value="*Nicht ausgewählt*", inline=False)
    embed.set_footer(text="Wähle alle Optionen aus und klicke dann auf Speichern")
    await interaction.response.send_message(embed=embed, view=SetupView(interaction.guild_id), ephemeral=True)


@bot.tree.command(name="setup_media", description="Konfiguriere die Medien-Weiterleitung (Bilder, Videos, YouTube, Twitch)")
async def setup_media(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message(
            embed=discord.Embed(title="Keine Berechtigung", description="Du hast nicht die nötige Berechtigung."),
            ephemeral=True
        )
        return
    current_config = await get_settings(str(interaction.guild_id))
    view = SetupMediaView(interaction.guild_id, current_config)
    await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="spieleabend", description="Erstelle einen neuen Spieleabend")
async def spieleabend(interaction: discord.Interaction):
    await interaction.response.send_modal(SpielabendModal())


@bot.tree.command(name="spieleabend_loeschen", description="Lösche einen Spieleabend")
@app_commands.describe(spieleabend_id="Die ID des Spieleabends")
async def spieleabend_loeschen(interaction: discord.Interaction, spieleabend_id: int):
    await interaction.response.defer(ephemeral=True)
    try:
        config = await get_settings(str(interaction.guild_id))
        if not config:
            await interaction.followup.send("❌ Keine Einstellungen gefunden!")
            return
        delete_role_ids = config.get("delete_role_ids", "").split(",")
        user_role_ids   = [str(r.id) for r in interaction.user.roles]
        has_permission  = (
            any(rid in delete_role_ids for rid in user_role_ids)
            or interaction.user.guild_permissions.administrator
        )
        result = supabase.table("game_nights").select("*").eq("id", spieleabend_id).execute()
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
            await thread.delete()
        except:
            pass
        supabase.table("game_nights").delete().eq("id", spieleabend_id).execute()
        await interaction.followup.send("✅ Spieleabend gelöscht!")
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}")


# ── Welcomer Commands ────────────────────────────────────────────────────────

@bot.tree.command(name="setup_welcomer", description="Konfiguriere Willkommens- und Abschiedsnachrichten")
async def setup_welcomer(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    current = await get_settings(str(interaction.guild_id))
    view = SetupWelcomerView(interaction.guild_id, current)
    await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
# MEMBER JOIN / LEAVE EVENTS
# ═════════════════════════════════════════════════════════════════════════════

@bot.event
async def on_member_join(member: discord.Member):
    try:
        config = await get_settings(str(member.guild.id))
        if not config:
            return
        if config.get("welcome_enabled", True) and config.get("welcome_channel_id"):
            channel = bot.get_channel(int(config["welcome_channel_id"]))
            if channel:
                msg = random.choice(WELCOME_MESSAGES).format(mention=member.mention)
                await channel.send(msg)
    except Exception as e:
        print(f"[Welcomer] on_member_join Fehler: {e}")


@bot.event
async def on_member_remove(member: discord.Member):
    try:
        config = await get_settings(str(member.guild.id))
        if not config:
            return
        if config.get("goodbye_enabled", True) and config.get("goodbye_channel_id"):
            channel = bot.get_channel(int(config["goodbye_channel_id"]))
            if channel:
                await channel.send(f"**{member.display_name}** hat den Server verlassen.")
    except Exception as e:
        print(f"[Welcomer] on_member_remove Fehler: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# PREFIX COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

@bot.command()
async def Ping(ctx):
    await ctx.send("Pong!")


# ═════════════════════════════════════════════════════════════════════════════
# BACKGROUND TASKS
# ═════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=1)
async def check_reminders():
    try:
        tz  = timezone(timedelta(hours=1))
        now = datetime.now(tz)
        result = supabase.table("game_nights").select("*").execute()
        for gn in result.data:
            if not gn.get('zeitpunkt'):
                continue
            zeitpunkt_str = gn['zeitpunkt']
            zeitpunkt = datetime.fromisoformat(zeitpunkt_str)
            if zeitpunkt.tzinfo is None:
                zeitpunkt = zeitpunkt.replace(tzinfo=tz)
            time_diff = (zeitpunkt - now).total_seconds() / 60
            thread = bot.get_channel(int(gn['thread_id']))
            if not thread:
                continue
            if 59 <= time_diff <= 61 and gn.get('vielleicht') and not gn.get('reminded_1h'):
                mentions = " ".join([f"<@{uid}>" for uid in gn['vielleicht']])
                await thread.send(f"⏰ **1 Stunde bis zum Start!**\n{mentions} - Habt ihr doch noch Zeit?")
                supabase.table("game_nights").update({"reminded_1h": True}).eq("id", gn['id']).execute()
            if 9 <= time_diff <= 11 and gn.get('dabei') and not gn.get('reminded_10m'):
                mentions = " ".join([f"<@{uid}>" for uid in gn['dabei']])
                await thread.send(f"⏰ **10 Minuten bis zum Start!**\n{mentions}")
                supabase.table("game_nights").update({"reminded_10m": True}).eq("id", gn['id']).execute()
            if -1 <= time_diff <= 1 and gn.get('dabei') and not gn.get('reminded_start'):
                mentions = " ".join([f"<@{uid}>" for uid in gn['dabei']])
                await thread.send(f"🎮 **Es geht los!**\n{mentions}")
                supabase.table("game_nights").update({"reminded_start": True}).eq("id", gn['id']).execute()
    except Exception as e:
        print(f"[check_reminders] Fehler: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# ROLLEN-VERGABE SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

MAX_ROLE_MODULES = 5


class RoleAssignView(discord.ui.View):
    def __init__(self, role_id: int, module_db_id: int):
        super().__init__(timeout=None)
        self.role_id     = role_id
        self.module_db_id = module_db_id

        accept_btn = discord.ui.Button(
            label="✅ Rolle annehmen",
            style=discord.ButtonStyle.success,
            custom_id=f"role_accept_{module_db_id}",
        )
        accept_btn.callback = self.accept_callback
        self.add_item(accept_btn)

        decline_btn = discord.ui.Button(
            label="❌ Rolle ablehnen",
            style=discord.ButtonStyle.danger,
            custom_id=f"role_decline_{module_db_id}",
        )
        decline_btn.callback = self.decline_callback
        self.add_item(decline_btn)

    async def accept_callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role:
            await interaction.response.send_message("❌ Rolle nicht gefunden.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("ℹ️ Du hast diese Rolle bereits.", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Rollenvergabe Bot")
            await interaction.response.send_message(f"✅ Du hast die Rolle **{role.name}** erhalten!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Ich habe keine Berechtigung diese Rolle zu vergeben.", ephemeral=True)

    async def decline_callback(self, interaction: discord.Interaction):
        role = interaction.guild.get_role(self.role_id)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message("ℹ️ Du hast diese Rolle nicht.", ephemeral=True)
            return
        try:
            await interaction.user.remove_roles(role, reason="Rollenvergabe Bot")
            await interaction.response.send_message(f"🔕 Die Rolle **{role.name}** wurde entfernt.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ Ich habe keine Berechtigung diese Rolle zu entfernen.", ephemeral=True)


async def register_role_views():
    try:
        result = supabase.table("role_modules").select("*").execute()
        for mod in result.data:
            view = RoleAssignView(int(mod["role_id"]), mod["id"])
            bot.add_view(view)
        print(f"✅ {len(result.data)} Rollenvergabe-Views registriert")
    except Exception as e:
        print(f"[RoleSystem] register_role_views Fehler: {e}")


class RolePickerView(discord.ui.View):
    def __init__(self, display_name: str, role_desc: str, setup_view: "SetupRoleView"):
        super().__init__(timeout=120)
        self.display_name = display_name
        self.role_desc    = role_desc
        self.setup_view   = setup_view

        role_sel = discord.ui.RoleSelect(
            placeholder="Wähle die Discord-Rolle...",
            min_values=1,
            max_values=1,
        )
        role_sel.callback = self.role_selected
        self.add_item(role_sel)

    async def role_selected(self, interaction: discord.Interaction):
        role_id   = interaction.data["values"][0]
        role_name = interaction.guild.get_role(int(role_id)).name
        for mod in self.setup_view.modules:
            if mod["role_id"] == role_id:
                await interaction.response.send_message(
                    f"❌ Die Rolle **{role_name}** ist bereits einem Modul zugewiesen.",
                    ephemeral=True
                )
                return
        self.setup_view.modules.append({
            "display_name": self.display_name,
            "role_desc":    self.role_desc,
            "role_id":      role_id,
            "role_name":    role_name,
        })
        self.setup_view._rebuild()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✅ Modul hinzugefügt",
                description=(
                    f"**Anzeigename:** {self.display_name}\n"
                    f"**Discord-Rolle:** {role_name}\n\n"
                    "Du kannst das Setup-Fenster wieder öffnen oder weitere Module hinzufügen."
                ),
                color=discord.Color.green()
            ),
            view=None
        )
        try:
            await self.setup_view._original_interaction.edit_original_response(
                embed=self.setup_view._build_embed(),
                view=self.setup_view
            )
        except Exception:
            pass


class AddRoleModuleModal(discord.ui.Modal, title="Rollenmodul hinzufügen"):
    role_name = discord.ui.TextInput(
        label="Rollenname", placeholder="z.B. Gamer, Musik-Fan, ...",
        required=True, max_length=100
    )
    role_desc = discord.ui.TextInput(
        label="Beschreibung", placeholder="Was bekommt man mit dieser Rolle?",
        required=True, style=discord.TextStyle.paragraph, max_length=300
    )

    def __init__(self, setup_view: "SetupRoleView"):
        super().__init__()
        self.setup_view = setup_view

    async def on_submit(self, interaction: discord.Interaction):
        for mod in self.setup_view.modules:
            if mod["display_name"].lower() == self.role_name.value.lower():
                await interaction.response.send_message(
                    "❌ Ein Modul mit diesem Namen existiert bereits.", ephemeral=True
                )
                return
        view = RolePickerView(
            display_name=self.role_name.value,
            role_desc=self.role_desc.value,
            setup_view=self.setup_view,
        )
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🎭 Rolle auswählen",
                description=(
                    f"Modul **{self.role_name.value}** wird angelegt.\n\n"
                    "Wähle nun die **Discord-Rolle** die vergeben werden soll.\n"
                    "*(Der Anzeigename im Bot kann vom Rollennamen abweichen)*"
                ),
                color=discord.Color.blurple()
            ),
            view=view,
            ephemeral=True
        )


class SetupRoleView(discord.ui.View):
    def __init__(self, guild_id: int):
        super().__init__(timeout=300)
        self.guild_id              = guild_id
        self.modules: list         = []
        self.target_channel_id: str | None = None
        self._original_interaction = None
        self._rebuild()

    def _rebuild(self):
        for item in [i for i in self.children if isinstance(i, discord.ui.Button)]:
            self.remove_item(item)
        for item in [i for i in self.children if isinstance(i, discord.ui.ChannelSelect)]:
            self.remove_item(item)

        if len(self.modules) < MAX_ROLE_MODULES:
            add_btn = discord.ui.Button(
                label=f"➕ Modul hinzufügen ({len(self.modules)}/{MAX_ROLE_MODULES})",
                style=discord.ButtonStyle.primary,
            )
            async def add_callback(interaction: discord.Interaction):
                await interaction.response.send_modal(AddRoleModuleModal(self))
            add_btn.callback = add_callback
            self.add_item(add_btn)

        if self.modules:
            remove_btn = discord.ui.Button(
                label="🗑️ Letztes Modul entfernen",
                style=discord.ButtonStyle.secondary,
            )
            async def remove_callback(interaction: discord.Interaction):
                if self.modules:
                    self.modules.pop()
                self._rebuild()
                await interaction.response.edit_message(
                    embed=self._build_embed(), view=self
                )
            remove_btn.callback = remove_callback
            self.add_item(remove_btn)

        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Kanal für die Rollenvergabe-Nachricht",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        async def ch_callback(interaction: discord.Interaction):
            self.target_channel_id = interaction.data["values"][0]
            self._rebuild()
            await interaction.response.edit_message(
                embed=self._build_embed(), view=self
            )
        ch_sel.callback = ch_callback
        self.add_item(ch_sel)

        send_btn = discord.ui.Button(
            label="🚀 Nachrichten senden & speichern",
            style=discord.ButtonStyle.success,
            disabled=not (self.modules and self.target_channel_id),
        )
        send_btn.callback = self.send_callback
        self.add_item(send_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Rollenvergabe Setup",
            description=(
                "Füge bis zu **5 Rollenmodule** hinzu. Jedes Modul bekommt eine eigene "
                "Nachricht mit **Annehmen / Ablehnen** Buttons im gewählten Kanal."
            ),
            color=discord.Color.blurple()
        )
        if self.modules:
            for i, mod in enumerate(self.modules, 1):
                embed.add_field(
                    name=f"Modul {i}: {mod['display_name']} → @{mod['role_name']}",
                    value=mod["role_desc"][:200],
                    inline=False
                )
        else:
            embed.add_field(name="Module", value="*Noch keine Module hinzugefügt*", inline=False)
        embed.add_field(
            name="📢 Kanal",
            value=f"<#{self.target_channel_id}>" if self.target_channel_id else "*Nicht ausgewählt*",
            inline=False
        )
        embed.set_footer(text="Die Rollen werden automatisch auf dem Server erstellt wenn sie nicht existieren.")
        return embed

    async def send_callback(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        try:
            channel = bot.get_channel(int(self.target_channel_id))
            if not channel:
                await interaction.followup.send("❌ Kanal nicht gefunden!", ephemeral=True)
                return
            main_embed = discord.Embed(
                title="🎭 Rollenvergabe",
                description=(
                    "Wähle deine Rollen aus! Drücke bei der jeweiligen Rolle auf "
                    "**✅ Rolle annehmen** oder **❌ Rolle ablehnen**."
                ),
                color=discord.Color.blurple()
            )
            await channel.send(embed=main_embed)
            for mod in self.modules:
                role = interaction.guild.get_role(int(mod["role_id"]))
                if not role:
                    await interaction.followup.send(
                        f"⚠️ Rolle für Modul '{mod['display_name']}' nicht gefunden – übersprungen.",
                        ephemeral=True
                    )
                    continue
                db_data = {
                    "guild_id":    str(self.guild_id),
                    "role_id":     str(role.id),
                    "role_name":   mod["role_name"],
                    "display_name": mod["display_name"],
                    "role_desc":   mod["role_desc"],
                    "channel_id":  str(self.target_channel_id),
                }
                result = supabase.table("role_modules").insert(db_data).execute()
                db_id = result.data[0]["id"] if result.data else 0
                role_embed = discord.Embed(
                    title=f"🏷️ {mod['display_name']}",
                    description=mod["role_desc"],
                    color=discord.Color.blurple()
                )
                view = RoleAssignView(role.id, db_id)
                bot.add_view(view)
                sent_msg = await channel.send(embed=role_embed, view=view)
                supabase.table("role_modules").update({"message_id": str(sent_msg.id)}).eq("id", db_id).execute()
            for item in self.children:
                item.disabled = True
            await interaction.followup.send(
                f"✅ {len(self.modules)} Rollenvergabe-Module wurden in <#{self.target_channel_id}> gesendet!",
                ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Ich habe keine Berechtigung Rollen zu erstellen. Bitte prüfe meine Rolle im Server.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


@bot.tree.command(name="setup_rollen", description="Richte die Rollenvergabe ein")
async def setup_rollen(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    view = SetupRoleView(interaction.guild_id)
    view._original_interaction = interaction
    await interaction.response.send_message(
        embed=view._build_embed(), view=view, ephemeral=True
    )


class EditRoleModuleModal(discord.ui.Modal, title="Rollenmodul bearbeiten"):
    new_display = discord.ui.TextInput(
        label="Neuer Anzeigename", required=True, max_length=100
    )
    new_desc = discord.ui.TextInput(
        label="Neue Beschreibung", required=True,
        style=discord.TextStyle.paragraph, max_length=300
    )

    def __init__(self, module: dict):
        super().__init__()
        self.module = module
        self.new_display.default = module.get("display_name") or module.get("role_name", "")
        self.new_desc.default    = module.get("role_desc", "")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            mod_id = self.module["id"]
            supabase.table("role_modules").update({
                "display_name": self.new_display.value,
                "role_desc":    self.new_desc.value,
            }).eq("id", mod_id).execute()
            channel = bot.get_channel(int(self.module["channel_id"]))
            if channel:
                old_name = self.module.get("display_name") or self.module.get("role_name", "")
                async for msg in channel.history(limit=50):
                    if msg.author.id == bot.user.id and msg.embeds:
                        emb = msg.embeds[0]
                        if emb.title and old_name in emb.title:
                            new_embed = discord.Embed(
                                title=f"🏷️ {self.new_display.value}",
                                description=self.new_desc.value,
                                color=discord.Color.blurple()
                            )
                            role = interaction.guild.get_role(int(self.module["role_id"]))
                            view = RoleAssignView(role.id if role else 0, mod_id)
                            await msg.edit(embed=new_embed, view=view)
                            break
            await interaction.followup.send("✅ Modul aktualisiert!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EditRolePickerView(discord.ui.View):
    def __init__(self, module: dict, guild: discord.Guild):
        super().__init__(timeout=120)
        self.module = module
        self.guild  = guild

        role_sel = discord.ui.RoleSelect(
            placeholder="Neue Discord-Rolle wählen...",
            min_values=1, max_values=1,
        )
        role_sel.callback = self.role_selected
        self.add_item(role_sel)

    async def role_selected(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            new_role_id   = interaction.data["values"][0]
            new_role      = interaction.guild.get_role(int(new_role_id))
            new_role_name = new_role.name if new_role else "Unbekannt"
            mod_id        = self.module["id"]
            supabase.table("role_modules").update({
                "role_id":   str(new_role_id),
                "role_name": new_role_name,
            }).eq("id", mod_id).execute()
            channel = bot.get_channel(int(self.module["channel_id"]))
            if channel and new_role:
                display = self.module.get("display_name") or self.module.get("role_name", "")
                async for msg in channel.history(limit=50):
                    if msg.author.id == bot.user.id and msg.embeds:
                        if msg.embeds[0].title and display in msg.embeds[0].title:
                            view = RoleAssignView(new_role.id, mod_id)
                            bot.add_view(view)
                            await msg.edit(view=view)
                            break
            await interaction.followup.send(
                f"✅ Rolle geändert zu **{new_role_name}**!", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class RoleEditActionView(discord.ui.View):
    def __init__(self, module: dict, guild: discord.Guild):
        super().__init__(timeout=120)
        self.module = module
        self.guild  = guild

        edit_btn = discord.ui.Button(
            label="✏️ Name & Beschreibung", style=discord.ButtonStyle.primary
        )
        edit_btn.callback = self.edit_text
        self.add_item(edit_btn)

        role_btn = discord.ui.Button(
            label="🎭 Rolle ändern", style=discord.ButtonStyle.secondary
        )
        role_btn.callback = self.edit_role
        self.add_item(role_btn)

        del_btn = discord.ui.Button(
            label="🗑️ Modul löschen", style=discord.ButtonStyle.danger
        )
        del_btn.callback = self.delete_module
        self.add_item(del_btn)

    async def edit_text(self, interaction: discord.Interaction):
        await interaction.response.send_modal(EditRoleModuleModal(self.module))

    async def edit_role(self, interaction: discord.Interaction):
        view = EditRolePickerView(self.module, self.guild)
        await interaction.response.send_message(
            "Wähle die neue Discord-Rolle:", view=view, ephemeral=True
        )

    async def delete_module(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            channel = bot.get_channel(int(self.module["channel_id"]))
            if channel:
                display = self.module.get("display_name") or self.module.get("role_name", "")
                async for msg in channel.history(limit=50):
                    if msg.author.id == bot.user.id and msg.embeds:
                        if msg.embeds[0].title and display in msg.embeds[0].title:
                            await msg.delete()
                            break
            supabase.table("role_modules").delete().eq("id", self.module["id"]).execute()
            await interaction.followup.send("✅ Modul gelöscht!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class RoleModuleSelectView(discord.ui.View):
    def __init__(self, modules: list[dict], guild: discord.Guild):
        super().__init__(timeout=60)
        self.guild = guild
        options = [
            discord.SelectOption(
                label=(m.get("display_name") or m.get("role_name", "?"))[:100],
                description=f"ID {m['id']} | Rolle: @{m.get('role_name','?')}",
                value=str(m["id"])
            )
            for m in modules[:25]
        ]
        self.modules_map = {str(m["id"]): m for m in modules}
        sel = discord.ui.Select(placeholder="Wähle ein Rollenmodul...", options=options)
        sel.callback = self.selected
        self.add_item(sel)

    async def selected(self, interaction: discord.Interaction):
        mod_id = interaction.data["values"][0]
        fresh = supabase.table("role_modules").select("*").eq("id", mod_id).execute()
        module = fresh.data[0] if fresh.data else self.modules_map[mod_id][0]
        display = module.get("display_name") or module.get("role_name", "?")
        role    = interaction.guild.get_role(int(module["role_id"]))
        embed = discord.Embed(
            title=f"✏️ Modul bearbeiten: {display}",
            description=(
                f"**Discord-Rolle:** {role.mention if role else '❓ Nicht gefunden'}\n"
                f"**Beschreibung:** {module.get('role_desc','')[:200]}\n"
                f"**Modul-ID:** `{module['id']}`"
            ),
            color=discord.Color.blurple()
        )
        action_view = RoleEditActionView(module, self.guild)
        await interaction.response.edit_message(embed=embed, view=action_view)


@bot.tree.command(name="rollen_bearbeiten", description="Bearbeite oder lösche ein Rollenvergabe-Modul")
async def rollen_bearbeiten(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = supabase.table("role_modules").select("*").eq("guild_id", str(interaction.guild_id)).execute()
        if not result.data:
            await interaction.followup.send(
                "❌ Keine Rollenvergabe-Module gefunden. Nutze `/setup_rollen` zuerst.",
                ephemeral=True
            )
            return
        embed = discord.Embed(
            title="✏️ Rollenvergabe bearbeiten",
            description="Wähle ein Modul das du bearbeiten möchtest:",
            color=discord.Color.blurple()
        )
        for m in result.data:
            display = m.get("display_name") or m.get("role_name", "?")
            role    = interaction.guild.get_role(int(m["role_id"]))
            embed.add_field(
                name=f"ID `{m['id']}` — {display}",
                value=f"Rolle: {role.mention if role else '❓'} | {m.get('role_desc','')[:80]}",
                inline=False
            )
        view = RoleModuleSelectView(result.data, interaction.guild)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EditRoleDisplayNameModal(discord.ui.Modal, title="Anzeigename ändern"):
    new_name = discord.ui.TextInput(
        label="Neuer Anzeigename", placeholder="z.B. Musik-Fan", required=True, max_length=100
    )
    def __init__(self, module: dict):
        super().__init__()
        self.module = module

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase.table("role_modules").update({"display_name": self.new_name.value}).eq("id", self.module["id"]).execute()
            await _update_role_message(interaction.guild, self.module, display_name=self.new_name.value)
            await interaction.followup.send(f"✅ Anzeigename auf **{self.new_name.value}** geändert.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EditRoleDescModal(discord.ui.Modal, title="Beschreibung ändern"):
    new_desc = discord.ui.TextInput(
        label="Neue Beschreibung", placeholder="...", required=True,
        style=discord.TextStyle.paragraph, max_length=300
    )
    def __init__(self, module: dict):
        super().__init__()
        self.module = module

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            supabase.table("role_modules").update({"role_desc": self.new_desc.value}).eq("id", self.module["id"]).execute()
            await _update_role_message(interaction.guild, self.module, role_desc=self.new_desc.value)
            await interaction.followup.send("✅ Beschreibung aktualisiert.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


async def _update_role_message(guild: discord.Guild, module: dict, **overrides):
    try:
        if not module.get("message_id") or not module.get("channel_id"):
            return
        channel = bot.get_channel(int(module["channel_id"]))
        if not channel:
            return
        msg = await channel.fetch_message(int(module["message_id"]))
        display_name = overrides.get("display_name", module.get("display_name") or module.get("role_name"))
        role_desc    = overrides.get("role_desc",    module.get("role_desc", ""))
        new_role_id  = overrides.get("role_id",      None)
        embed = discord.Embed(
            title=f"🏷️ {display_name}",
            description=role_desc,
            color=discord.Color.blurple()
        )
        embed.set_footer(text=f"Modul-ID: {module['id']}")
        role_id = int(new_role_id) if new_role_id else int(module["role_id"])
        view = RoleAssignView(role_id, module["id"])
        await msg.edit(embed=embed, view=view)
    except Exception as e:
        print(f"[RoleSystem] _update_role_message Fehler: {e}")


class RoleChangePickerView(discord.ui.View):
    def __init__(self, module: dict):
        super().__init__(timeout=60)
        self.module = module
        sel = discord.ui.RoleSelect(placeholder="Neue Discord-Rolle wählen...", min_values=1, max_values=1)
        async def cb(interaction: discord.Interaction):
            new_role_id = interaction.data["values"][0]
            new_role    = interaction.guild.get_role(int(new_role_id))
            await interaction.response.defer(ephemeral=True)
            try:
                supabase.table("role_modules").update({
                    "role_id":   str(new_role_id),
                    "role_name": new_role.name if new_role else "Unbekannt"
                }).eq("id", self.module["id"]).execute()
                await _update_role_message(interaction.guild, self.module, role_id=new_role_id)
                await interaction.followup.send(
                    f"✅ Rolle geändert zu **{new_role.name if new_role else new_role_id}**.", ephemeral=True
                )
            except Exception as e:
                await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)
        sel.callback = cb
        self.add_item(sel)


# ═════════════════════════════════════════════════════════════════════════════
# WELCOMER SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

WELCOME_MESSAGES = [
    "**{mention} hat zu diesem Server gefunden! Willkommen!  <:pepelove:1362364214995324928>**",
    "**wow, wie toll! {mention} ist jetzt hier! <:welcome:1362364322772160513>**",
    "**{mention} hat zur Insel gefunden! <:pepehappy:1362364194967781598>**",
    "**Juhu, {mention} hat zur Insel gefunden!**",
    "**Kuckt mal wer hier ist: {mention} ! <:pepehappy:1362364194967781598>**",
    "**Herzlich Willkommen {mention} ! Du bist nun bei der Insel!  <:pepelove:1362364214995324928>**",
    "**{mention} ist dem Insel-Discord beigetreten! 🫡**",
    "**Endlich! {mention} ist hier! 😇**",
    "**Huhu {mention} . Willkommen 🙂**",
    "**Ein wildes  {mention} ist auf die Insel geschlittert 😄**",
    "**Wilkommen {mention} bei der Insel! <:pepehappy:1362364194967781598>**",
    "**{mention}, was geht yallah <:welcome:1362364322772160513>**",
    "**Oh halloo! {mention} 🙂 **",
    "**Heyyyy was geeeht {mention} 😀 **",
    "**{mention} Du bist Kanidat, gewinnen wir die Runde bekommst du einen Händedruck!**",
    "**Seht Seht {mention} hat es auf den Server geschafft.<:welcome:1362364322772160513>**",
    "**Boar das schmeckt, {mention} ist nun hier!🙃**",
]


class SetupWelcomerView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        cfg = current_config or {}
        self.welcome_channel_id: str | None = str(cfg["welcome_channel_id"]) if cfg.get("welcome_channel_id") else None
        self.goodbye_channel_id: str | None = str(cfg["goodbye_channel_id"]) if cfg.get("goodbye_channel_id") else None
        self.welcome_enabled: bool = bool(cfg.get("welcome_enabled", True))
        self.goodbye_enabled: bool = bool(cfg.get("goodbye_enabled", True))

        self.ch_welcome = discord.ui.ChannelSelect(
            placeholder="👋 Willkommens-Kanal auswählen",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.ch_welcome.callback = self.welcome_channel_callback
        self.add_item(self.ch_welcome)

        self.ch_goodbye = discord.ui.ChannelSelect(
            placeholder="👋 Abschied-Kanal auswählen (nur für Mods sichtbar)",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        self.ch_goodbye.callback = self.goodbye_channel_callback
        self.add_item(self.ch_goodbye)

        self._add_buttons()

    def _add_buttons(self):
        for item in [i for i in self.children if isinstance(i, discord.ui.Button)]:
            self.remove_item(item)

        welcome_btn = discord.ui.Button(
            label=f"Willkommen: {'AN ✅' if self.welcome_enabled else 'AUS ❌'}",
            emoji="👋",
            style=discord.ButtonStyle.success if self.welcome_enabled else discord.ButtonStyle.secondary,
        )
        async def toggle_welcome(interaction: discord.Interaction):
            self.welcome_enabled = not self.welcome_enabled
            self._add_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        welcome_btn.callback = toggle_welcome
        self.add_item(welcome_btn)

        goodbye_btn = discord.ui.Button(
            label=f"Abschied: {'AN ✅' if self.goodbye_enabled else 'AUS ❌'}",
            emoji="🚪",
            style=discord.ButtonStyle.success if self.goodbye_enabled else discord.ButtonStyle.secondary,
        )
        async def toggle_goodbye(interaction: discord.Interaction):
            self.goodbye_enabled = not self.goodbye_enabled
            self._add_buttons()
            await interaction.response.edit_message(embed=self._build_embed(), view=self)
        goodbye_btn.callback = toggle_goodbye
        self.add_item(goodbye_btn)

        save_btn = discord.ui.Button(
            label="💾 Speichern",
            style=discord.ButtonStyle.primary,
        )
        save_btn.callback = self.save_callback
        self.add_item(save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Welcomer Setup",
            description="Konfiguriere Willkommens- und Abschiedsnachrichten.",
            color=discord.Color.blurple()
        )
        embed.add_field(
            name="👋 Willkommens-Kanal",
            value=f"<#{self.welcome_channel_id}>" if self.welcome_channel_id else "*Nicht ausgewählt*",
            inline=True
        )
        embed.add_field(
            name="📊 Willkommen",
            value="✅ AN" if self.welcome_enabled else "❌ AUS",
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(
            name="🚪 Abschied-Kanal",
            value=f"<#{self.goodbye_channel_id}>" if self.goodbye_channel_id else "*Nicht ausgewählt*",
            inline=True
        )
        embed.add_field(
            name="📊 Abschied",
            value="✅ AN" if self.goodbye_enabled else "❌ AUS",
            inline=True
        )
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.set_footer(text="Der Abschied-Kanal sollte nur für Moderatoren sichtbar sein.")
        return embed

    async def welcome_channel_callback(self, interaction: discord.Interaction):
        self.welcome_channel_id = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def goodbye_channel_callback(self, interaction: discord.Interaction):
        self.goodbye_channel_id = interaction.data["values"][0]
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            data = {
                "guild_id":         str(self.guild_id),
                "welcome_channel_id": self.welcome_channel_id,
                "goodbye_channel_id": self.goodbye_channel_id,
                "welcome_enabled":    self.welcome_enabled,
                "goodbye_enabled":    self.goodbye_enabled,
            }
            existing = supabase.table("settings").select("id").eq("guild_id", str(self.guild_id)).execute()
            if existing.data:
                supabase.table("settings").update(data).eq("guild_id", str(self.guild_id)).execute()
            else:
                supabase.table("settings").insert(data).execute()
            settings_cache.invalidate(str(self.guild_id))
            embed = self._build_embed()
            embed.color = discord.Color.green()
            embed.title = "✅ Welcomer gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# ═════════════════════════════════════════════════════════════════════════════
# EVENT SYSTEM
# ═════════════════════════════════════════════════════════════════════════════

TZ = timezone(timedelta(hours=1))  # MEZ


async def has_event_rights(interaction: discord.Interaction) -> bool:
    if str(interaction.user.id) == str(os.getenv("MBL")):
        return True
    if interaction.user.guild_permissions.administrator:
        return True
    config = await get_settings(str(interaction.guild_id))
    if not config:
        return False
    role_ids_str = config.get("event_role_ids", "")
    if not role_ids_str:
        return False
    event_role_ids = [r.strip() for r in role_ids_str.split(",") if r.strip()]
    user_role_ids  = [str(r.id) for r in interaction.user.roles]
    return any(rid in event_role_ids for rid in user_role_ids)


def parse_event_time(time_str: str):
    """
    Parst DD.MM.YYYY HH:MM oder HH:MM (= heute) in timezone-aware datetime.
    Gibt None zurück bei ungültigem Format.
    Gibt das String-Sentinel 'tba' zurück wenn Eingabe '-1' ist (Datum noch unbekannt).
    Gibt das String-Sentinel 'open_end' zurück wenn Eingabe '-1' für Endzeit ist (Ende unbekannt).
    """
    time_str = time_str.strip()

    # Sentinel für unbekanntes Datum
    if time_str == "-1":
        return "-1"

    tz = TZ
    for fmt in ["%d.%m.%Y %H:%M", "%d.%m. %H:%M"]:
        try:
            parsed = datetime.strptime(time_str, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now(tz).year)
            return parsed.replace(tzinfo=tz)
        except ValueError:
            continue
    try:
        parts = time_str.split(":")
        now = datetime.now(tz)
        target = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target
    except Exception:
        return None


def build_event_embed(event: dict) -> discord.Embed:
    """Erstellt das Embed für ein Event je nach Status."""
    status   = event.get("status", "upcoming")
    start_dt = None
    end_dt   = None

    if event.get("start_time"):
        start_dt = datetime.fromisoformat(event["start_time"]).replace(tzinfo=TZ)
    if event.get("end_time"):
        end_dt = datetime.fromisoformat(event["end_time"]).replace(tzinfo=TZ)

    now = datetime.now(TZ)

    # ── Farben ────────────────────────────────────────────────────────────────
    color_map = {
        "upcoming":  0x3498DB,   # Blau  – geplant
        "tba":       0x9B59B6,   # Lila  – Datum unbekannt
        "live":      0x2ECC71,   # Grün  – läuft
        "open_end":  0x1ABC9C,   # Türkis – läuft, Ende unbekannt
        "delayed":   0xE67E22,   # Orange – verschoben
        "cancelled": 0xED4245,   # Rot   – abgesagt
        "ended":     0x95A5A6,   # Grau  – beendet
    }

    # ── Status-Labels ─────────────────────────────────────────────────────────
    status_labels = {
        "upcoming":  "📅 Geplant",
        "tba":       "❓ Datum noch unbekannt",
        "live":      "🟢 Läuft gerade",
        "open_end":  "🟢 Läuft gerade (Ende unbekannt)",
        "delayed":   "⏸️ Verschoben (unbestimmt)",
        "cancelled": "❌ Abgesagt",
        "ended":     "✅ Beendet",
    }

    color = color_map.get(status, 0x3498DB)

    embed = discord.Embed(
        title=f"🎉 {event['title']}",
        description=event.get("description", ""),
        color=color
    )
    embed.add_field(name="📊 Status",   value=status_labels.get(status, status), inline=True)
    embed.add_field(name="👥 Follower", value=str(len(event.get("followers") or [])), inline=True)
    embed.add_field(name="\u200b",      value="\u200b", inline=True)

    # ── Zeitfelder ────────────────────────────────────────────────────────────
    if status == "tba":
        embed.add_field(name="🕐 Start", value="❓ Wird noch bekannt gegeben", inline=True)
        embed.add_field(name="🏁 Ende",  value="❓ Wird noch bekannt gegeben", inline=True)
        embed.add_field(name="\u200b",   value="\u200b", inline=True)
        embed.add_field(
            name="ℹ️ Hinweis",
            value="Datum und Uhrzeit werden noch bekannt gegeben. Folge dem Event um benachrichtigt zu werden!",
            inline=False
        )
    else:
        if start_dt:
            embed.add_field(name="🕐 Start", value=f"<t:{int(start_dt.timestamp())}:F>", inline=True)
        else:
            embed.add_field(name="🕐 Start", value="❓ Unbekannt", inline=True)

        if end_dt:
            embed.add_field(name="🏁 Ende", value=f"<t:{int(end_dt.timestamp())}:F>", inline=True)
        elif status in ("live", "open_end"):
            embed.add_field(name="🏁 Ende", value="⏳ Ende nicht festgelegt", inline=True)
        else:
            embed.add_field(name="🏁 Ende", value="❓ Unbekannt", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        # Dynamische Zeitfelder je nach Status
        if status == "upcoming" and start_dt and start_dt > now:
            embed.add_field(name="⏳ Startet in", value=f"<t:{int(start_dt.timestamp())}:R>", inline=False)
        elif status == "live" and end_dt and end_dt > now:
            embed.add_field(name="⏱️ Endet in", value=f"<t:{int(end_dt.timestamp())}:R>", inline=False)
        elif status == "open_end":
            embed.add_field(
                name="ℹ️ Hinweis",
                value="Das Event läuft bis es manuell beendet wird.",
                inline=False
            )
        elif status == "delayed":
            embed.add_field(name="ℹ️ Hinweis", value="Das Event wurde auf unbestimmte Zeit verschoben.", inline=False)
        elif status == "cancelled":
            embed.add_field(name="ℹ️ Hinweis", value="Dieses Event wurde abgesagt.", inline=False)

    embed.set_footer(text=f"Event-ID: {event.get('id', '?')}")
    return embed


async def _update_event_message(event: dict):
    try:
        channel = bot.get_channel(int(event["channel_id"]))
        if not channel:
            return
        message = await channel.fetch_message(int(event["message_id"]))
        await message.edit(embed=build_event_embed(event), view=EventFollowView())
    except Exception as e:
        print(f"[Event] _update_event_message: {e}")


async def _notify_thread(event: dict, text: str, ping: bool = False):
    try:
        thread = bot.get_channel(int(event["thread_id"]))
        if not thread:
            return
        mention_str = ""
        if ping and event.get("followers"):
            mention_str = " ".join(f"<@{uid}>" for uid in event["followers"]) + "\n"
        await thread.send(f"{mention_str}{text}")
    except Exception as e:
        print(f"[Event] _notify_thread: {e}")


async def _archive_event(event: dict):
    try:
        thread = bot.get_channel(int(event["thread_id"]))
        if thread and isinstance(thread, discord.Thread):
            await thread.send("🗄️ *Dieser Event-Thread wird jetzt archiviert.*")
            await thread.edit(archived=True, locked=True)
    except Exception as e:
        print(f"[Event] Archivierung Thread: {e}")
    try:
        supabase.table("events").delete().eq("id", event["id"]).execute()
    except Exception as e:
        print(f"[Event] Archivierung DB: {e}")


# ── Follow/Unfollow View ──────────────────────────────────────────────────────

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
            result = supabase.table("events").select("*").eq("message_id", message_id).execute()
            if not result.data:
                await interaction.response.send_message("❌ Event nicht gefunden!", ephemeral=True)
                return
            event     = result.data[0]
            followers = list(event.get("followers") or [])
            if follow:
                if user_id not in followers:
                    followers.append(user_id)
                msg = "✅ Du folgst jetzt diesem Event!" if user_id in followers else "ℹ️ Du folgst bereits."
            else:
                was_in = user_id in followers
                followers = [f for f in followers if f != user_id]
                msg = "🔕 Du folgst dem Event nicht mehr." if was_in else "ℹ️ Du hast nicht gefolgt."
            supabase.table("events").update({"followers": followers}).eq("id", event["id"]).execute()
            updated = {**event, "followers": followers}
            await interaction.message.edit(embed=build_event_embed(updated), view=self)
            await interaction.response.send_message(msg, ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


# ── Modals ────────────────────────────────────────────────────────────────────

class EventCreateModal(discord.ui.Modal, title="Event erstellen"):
    e_title = discord.ui.TextInput(
        label="Event-Name",
        placeholder="z.B. Community Turnier",
        required=True, max_length=100
    )
    e_desc = discord.ui.TextInput(
        label="Beschreibung",
        placeholder="Worum geht es?",
        required=False,
        style=discord.TextStyle.paragraph, max_length=800
    )
    e_start = discord.ui.TextInput(
        label="Startzeit  (-1 = noch unbekannt)",
        placeholder="z.B. 25.12.2025 20:00  |  -1 = TBA",
        required=True, max_length=30
    )
    e_end = discord.ui.TextInput(
        label="Endzeit  (-1 = kein festes Ende)",
        placeholder="z.B. 25.12.2025 23:00  |  -1 = offen",
        required=True, max_length=30
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            config = await get_settings(str(interaction.guild_id))
            if not config or not config.get("event_channel_id"):
                await interaction.followup.send("❌ Bitte führe zuerst `/setup_event` aus!", ephemeral=True)
                return
            channel = bot.get_channel(int(config["event_channel_id"]))
            if not channel:
                await interaction.followup.send("❌ Event-Kanal nicht gefunden!", ephemeral=True)
                return

            # ── Start parsen ──────────────────────────────────────────────────
            raw_start = self.e_start.value.strip()
            raw_end   = self.e_end.value.strip()

            start_tba = raw_start == "-1"
            end_open  = raw_end   == "-1"

            start_dt = None
            end_dt   = None

            if not start_tba:
                start_dt = parse_event_time(raw_start)
                if start_dt is None or start_dt == "-1":
                    await interaction.followup.send(
                        "❌ Ungültiges Startzeit-Format! Nutze: DD.MM.YYYY HH:MM  oder  -1 für TBA",
                        ephemeral=True
                    )
                    return

            if not end_open:
                end_dt = parse_event_time(raw_end)
                if end_dt is None or end_dt == "-1":
                    await interaction.followup.send(
                        "❌ Ungültiges Endzeit-Format! Nutze: DD.MM.YYYY HH:MM  oder  -1 für offenes Ende",
                        ephemeral=True
                    )
                    return
                if start_dt and end_dt <= start_dt:
                    await interaction.followup.send("❌ Endzeit muss nach Startzeit liegen!", ephemeral=True)
                    return

            # ── Initalen Status bestimmen ─────────────────────────────────────
            # TBA  → Datum noch unbekannt (auch Endzeit egal, da kein Start)
            # open_end → hat Startzeit, aber Ende unbekannt
            # upcoming → hat Start + Ende
            if start_tba:
                initial_status = "tba"
            elif end_open:
                initial_status = "upcoming"   # Wird zu open_end sobald gestartet
            else:
                initial_status = "upcoming"

            tmp_event = {
                "id":          "?",
                "title":       self.e_title.value,
                "description": self.e_desc.value or "",
                "start_time":  start_dt.isoformat() if start_dt else None,
                "end_time":    end_dt.isoformat()   if end_dt   else None,
                "status":      initial_status,
                "followers":   [],
                # Merker ob Ende offen ist (für check_events)
                "end_open":    end_open,
            }

            message = await channel.send(embed=build_event_embed(tmp_event), view=EventFollowView())
            thread  = await message.create_thread(name=f"💬 {self.e_title.value}", auto_archive_duration=10080)
            await thread.send(
                f"👋 Willkommen im Event-Thread für **{self.e_title.value}**!\n"
                "Hier könnt ihr diskutieren, Fragen stellen und euch absprechen."
            )

            data = {
                "guild_id":       str(interaction.guild_id),
                "message_id":     str(message.id),
                "thread_id":      str(thread.id),
                "channel_id":     str(channel.id),
                "title":          self.e_title.value,
                "description":    self.e_desc.value or "",
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat()   if end_dt   else None,
                "end_open":       end_open,   # True = Ende unbekannt
                "status":         initial_status,
                "followers":      [],
                "creator_id":     str(interaction.user.id),
                "reminded_1h":    False,
                "reminded_start": False,
                "archived":       False,
            }
            result = supabase.table("events").insert(data).execute()
            if result.data:
                event_id = result.data[0]["id"]
                await message.edit(embed=build_event_embed({**tmp_event, "id": event_id}))

            await interaction.followup.send(
                f"✅ Event **{self.e_title.value}** erstellt! {message.jump_url}", ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventRescheduleModal(discord.ui.Modal, title="Event verschieben"):
    new_start = discord.ui.TextInput(
        label="Neue Startzeit  (-1 = TBA)",
        placeholder="DD.MM.YYYY HH:MM  |  -1 = Datum noch unbekannt",
        required=True, max_length=30
    )
    new_end = discord.ui.TextInput(
        label="Neue Endzeit  (-1 = kein festes Ende)",
        placeholder="DD.MM.YYYY HH:MM  |  -1 = offenes Ende",
        required=True, max_length=30
    )

    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            raw_start = self.new_start.value.strip()
            raw_end   = self.new_end.value.strip()

            start_tba = raw_start == "-1"
            end_open  = raw_end   == "-1"

            start_dt = None
            end_dt   = None

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

            new_status = "tba" if start_tba else "upcoming"
            updated = {
                **self.event,
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat()   if end_dt   else None,
                "end_open":       end_open,
                "status":         new_status,
                "reminded_1h":    False,
                "reminded_start": False,
            }
            supabase.table("events").update({
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat()   if end_dt   else None,
                "end_open":       end_open,
                "status":         new_status,
                "reminded_1h":    False,
                "reminded_start": False,
            }).eq("id", self.event["id"]).execute()

            await _update_event_message(updated)

            if start_tba:
                notif = "📅 **Zeitänderung!**\nStartzeit ist noch unbekannt – wird bekannt gegeben."
            else:
                notif = (
                    f"📅 **Zeitänderung!**\n"
                    f"▶️ Start: <t:{int(start_dt.timestamp())}:F>\n"
                    f"🏁 Ende: {'Ende offen' if end_open else f'<t:{int(end_dt.timestamp())}:F>'}"
                )
            await _notify_thread(self.event, notif, ping=True)
            await interaction.followup.send("✅ Event neu terminiert!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventResumeModal(discord.ui.Modal, title="Event fortsetzen"):
    new_start = discord.ui.TextInput(
        label="Neue Startzeit  (-1 = TBA)",
        placeholder="DD.MM.YYYY HH:MM  |  -1 = Datum noch unbekannt",
        required=True, max_length=30
    )
    new_end = discord.ui.TextInput(
        label="Neue Endzeit  (-1 = kein festes Ende)",
        placeholder="DD.MM.YYYY HH:MM  |  -1 = offenes Ende",
        required=True, max_length=30
    )

    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            raw_start = self.new_start.value.strip()
            raw_end   = self.new_end.value.strip()

            start_tba = raw_start == "-1"
            end_open  = raw_end   == "-1"

            start_dt = None
            end_dt   = None

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

            new_status = "tba" if start_tba else "upcoming"
            updated = {
                **self.event,
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat()   if end_dt   else None,
                "end_open":       end_open,
                "status":         new_status,
                "reminded_1h":    False,
                "reminded_start": False,
            }
            supabase.table("events").update({
                "start_time":     start_dt.isoformat() if start_dt else None,
                "end_time":       end_dt.isoformat()   if end_dt   else None,
                "end_open":       end_open,
                "status":         new_status,
                "reminded_1h":    False,
                "reminded_start": False,
            }).eq("id", self.event["id"]).execute()

            await _update_event_message(updated)

            if start_tba:
                notif = "🔄 **Event fortgesetzt!**\nStartzeit wird noch bekannt gegeben."
            else:
                notif = (
                    f"🔄 **Event fortgesetzt!**\n"
                    f"▶️ Start: <t:{int(start_dt.timestamp())}:F>\n"
                    f"🏁 Ende: {'Ende offen' if end_open else f'<t:{int(end_dt.timestamp())}:F>'}"
                )
            await _notify_thread(self.event, notif, ping=True)
            await interaction.followup.send("✅ Event fortgesetzt!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


class EventTextEditModal(discord.ui.Modal):
    def __init__(self, event: dict, field: str):
        label_map = {"title": "Neuer Titel", "description": "Neue Beschreibung", "news": "News-Inhalt"}
        super().__init__(title=label_map.get(field, "Bearbeiten"))
        self.event = event
        self.field = field
        self.text_input = discord.ui.TextInput(
            label=label_map.get(field, "Text"), placeholder="Eingabe...", required=True,
            style=discord.TextStyle.paragraph if field in ("description", "news") else discord.TextStyle.short,
            max_length=800
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            value = self.text_input.value
            if self.field == "news":
                await _notify_thread(self.event, f"📢 **Event-News:**\n{value}", ping=True)
            elif self.field == "title":
                supabase.table("events").update({"title": value}).eq("id", self.event["id"]).execute()
                await _update_event_message({**self.event, "title": value})
                await _notify_thread(self.event, f"✏️ **Titel geändert:** {value}", ping=False)
            elif self.field == "description":
                supabase.table("events").update({"description": value}).eq("id", self.event["id"]).execute()
                await _update_event_message({**self.event, "description": value})
                await _notify_thread(self.event, "📝 **Beschreibung wurde aktualisiert.**", ping=False)
            await interaction.followup.send("✅ Erledigt!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ── Event Edit Dropdown ───────────────────────────────────────────────────────

class EventEditSelectView(discord.ui.View):
    def __init__(self, events: list[dict], action: str):
        super().__init__(timeout=60)
        self.action     = action
        self.events_map = {str(e["id"]): e for e in events}

        options = [
            discord.SelectOption(
                label=e["title"][:100],
                description=f"ID {e['id']} | Status: {e.get('status','?')}",
                value=str(e["id"])
            )
            for e in events[:25]
        ]
        select = discord.ui.Select(placeholder="Wähle ein Event…", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        event_id = interaction.data["values"][0]
        fresh = supabase.table("events").select("*").eq("id", event_id).execute()
        event = fresh.data[0] if fresh.data else self.events_map[event_id]

        action = self.action
        if action in ("start_time", "end_time"):
            await interaction.response.send_modal(EventRescheduleModal(event))
        elif action == "cancel":
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "cancelled"}).eq("id", event_id).execute()
            await _update_event_message({**event, "status": "cancelled"})
            await _notify_thread(event, "❌ **Das Event wurde leider abgesagt.**", ping=True)
            await interaction.followup.send("✅ Event abgesagt.", ephemeral=True)
        elif action == "delay":
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "delayed"}).eq("id", event_id).execute()
            await _update_event_message({**event, "status": "delayed"})
            await _notify_thread(event, "⏸️ **Das Event wurde auf unbestimmte Zeit verschoben.**", ping=True)
            await interaction.followup.send("✅ Event als 'delayed' markiert.", ephemeral=True)
        elif action == "resume":
            if event.get("status") not in ("delayed", "tba"):
                await interaction.response.send_message(
                    "❌ Das Event ist weder 'delayed' noch 'tba' – Resume nicht möglich.", ephemeral=True
                )
                return
            await interaction.response.send_modal(EventResumeModal(event))
        elif action in ("title", "description", "news"):
            await interaction.response.send_modal(EventTextEditModal(event, action))
        elif action == "end_now":
            # Manuell beenden (z.B. für open_end Events)
            await interaction.response.defer(ephemeral=True)
            supabase.table("events").update({"status": "ended"}).eq("id", event_id).execute()
            await _update_event_message({**event, "status": "ended"})
            await _notify_thread(event, "✅ **Das Event wurde manuell beendet. Danke fürs Mitmachen!**", ping=True)
            await interaction.followup.send("✅ Event manuell beendet.", ephemeral=True)
        elif action == "set_date":
            # TBA-Event: Datum nachträglich setzen
            await interaction.response.send_modal(EventSetDateModal(event))
        else:
            await interaction.response.send_message("❌ Unbekannte Aktion.", ephemeral=True)


# ── Neues Modal: Datum für TBA-Event nachträglich setzen ─────────────────────

class EventSetDateModal(discord.ui.Modal, title="Datum festlegen"):
    new_start = discord.ui.TextInput(
        label="Startzeit",
        placeholder="DD.MM.YYYY HH:MM",
        required=True, max_length=30
    )
    new_end = discord.ui.TextInput(
        label="Endzeit  (-1 = kein festes Ende)",
        placeholder="DD.MM.YYYY HH:MM  |  -1 = offenes Ende",
        required=True, max_length=30
    )

    def __init__(self, event: dict):
        super().__init__()
        self.event = event

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            raw_start = self.new_start.value.strip()
            raw_end   = self.new_end.value.strip()
            end_open  = raw_end == "-1"

            start_dt = parse_event_time(raw_start)
            if start_dt is None or start_dt == "-1":
                await interaction.followup.send("❌ Ungültiges Startzeit-Format!", ephemeral=True)
                return

            end_dt = None
            if not end_open:
                end_dt = parse_event_time(raw_end)
                if end_dt is None or end_dt == "-1":
                    await interaction.followup.send("❌ Ungültiges Endzeit-Format!", ephemeral=True)
                    return
                if end_dt <= start_dt:
                    await interaction.followup.send("❌ Endzeit muss nach Startzeit liegen!", ephemeral=True)
                    return

            updated = {
                **self.event,
                "start_time":     start_dt.isoformat(),
                "end_time":       end_dt.isoformat() if end_dt else None,
                "end_open":       end_open,
                "status":         "upcoming",
                "reminded_1h":    False,
                "reminded_start": False,
            }
            supabase.table("events").update({
                "start_time":     start_dt.isoformat(),
                "end_time":       end_dt.isoformat() if end_dt else None,
                "end_open":       end_open,
                "status":         "upcoming",
                "reminded_1h":    False,
                "reminded_start": False,
            }).eq("id", self.event["id"]).execute()

            await _update_event_message(updated)
            notif = (
                f"📅 **Datum wurde festgelegt!**\n"
                f"▶️ Start: <t:{int(start_dt.timestamp())}:F>\n"
                f"🏁 Ende: {'Ende offen' if end_open else f'<t:{int(end_dt.timestamp())}:F>'}"
            )
            await _notify_thread(self.event, notif, ping=True)
            await interaction.followup.send("✅ Datum gesetzt! Status auf 'Geplant' geändert.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ── Setup Event View ──────────────────────────────────────────────────────────

class SetupEventView(discord.ui.View):
    def __init__(self, guild_id: int, current_config: dict | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        cfg = current_config or {}
        self.event_channel_id: str | None = str(cfg["event_channel_id"]) if cfg.get("event_channel_id") else None
        self.event_role_ids:   list[str]  = [r for r in cfg.get("event_role_ids", "").split(",") if r] if cfg.get("event_role_ids") else []

        ch_sel = discord.ui.ChannelSelect(
            placeholder="📢 Kanal für Event-Ankündigungen",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        ch_sel.callback = self.channel_callback
        self.add_item(ch_sel)

        role_sel = discord.ui.RoleSelect(
            placeholder="🔐 Rollen mit Event-Berechtigung",
            min_values=1, max_values=10,
        )
        role_sel.callback = self.role_callback
        self.add_item(role_sel)

        self.save_btn = discord.ui.Button(
            label="💾 Speichern", style=discord.ButtonStyle.success,
            disabled=not (self.event_channel_id and self.event_role_ids)
        )
        self.save_btn.callback = self.save_callback
        self.add_item(self.save_btn)

    def _build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="⚙️ Event-System Setup",
            description="Lege fest, in welchem Kanal Events gepostet werden und wer Events erstellen darf.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📢 Event-Kanal",
                        value=f"<#{self.event_channel_id}>" if self.event_channel_id else "*Nicht ausgewählt*",
                        inline=False)
        embed.add_field(name="🔐 Berechtigte Rollen",
                        value=" ".join(f"<@&{r}>" for r in self.event_role_ids if r) or "*Nicht ausgewählt*",
                        inline=False)
        embed.set_footer(text="Admins und MBL haben immer Zugriff.")
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
            data = {
                "guild_id":         str(self.guild_id),
                "event_channel_id": self.event_channel_id,
                "event_role_ids":   ",".join(self.event_role_ids),
            }
            existing = supabase.table("settings").select("id").eq("guild_id", str(self.guild_id)).execute()
            if existing.data:
                supabase.table("settings").update(data).eq("guild_id", str(self.guild_id)).execute()
            else:
                supabase.table("settings").insert(data).execute()
            settings_cache.invalidate(str(self.guild_id))
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


# ── Event Slash Commands ──────────────────────────────────────────────────────

@bot.tree.command(name="setup_event", description="Konfiguriere das Event-System")
async def setup_event(interaction: discord.Interaction):
    if not has_rights(interaction):
        await interaction.response.send_message("❌ Nur Admins können das Event-System einrichten.", ephemeral=True)
        return
    current = await get_settings(str(interaction.guild_id))
    view    = SetupEventView(interaction.guild_id, current)
    await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)


@bot.tree.command(name="event_erstellen", description="Erstelle ein neues Event")
async def event_erstellen(interaction: discord.Interaction):
    if not await has_event_rights(interaction):
        await interaction.response.send_message("❌ Du hast keine Berechtigung für Events.", ephemeral=True)
        return
    await interaction.response.send_modal(EventCreateModal())


@bot.tree.command(name="event_list", description="Zeigt die Follower-Liste aller aktiven Events")
async def event_list(interaction: discord.Interaction):
    if not await has_event_rights(interaction):
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = supabase.table("events").select("*")\
                         .eq("guild_id", str(interaction.guild_id))\
                         .eq("archived", False).execute()
        if not result.data:
            await interaction.followup.send("❌ Keine aktiven Events gefunden.", ephemeral=True)
            return
        embed = discord.Embed(title="📋 Event Follower-Liste", color=discord.Color.blurple())
        for ev in result.data:
            followers = ev.get("followers") or []
            mentions  = ", ".join(f"<@{uid}>" for uid in followers) if followers else "*Niemand*"
            embed.add_field(
                name=f"{ev['title']} (ID {ev['id']}) — {len(followers)} Follower",
                value=mentions[:1024], inline=False
            )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


@bot.tree.command(name="event_edit", description="Bearbeite ein bestehendes Event")
@app_commands.describe(aktion="Was soll geändert werden?")
@app_commands.choices(aktion=[
    app_commands.Choice(name="⏰ Startzeit ändern",          value="start_time"),
    app_commands.Choice(name="🏁 Endzeit ändern",            value="end_time"),
    app_commands.Choice(name="❌ Absagen",                   value="cancel"),
    app_commands.Choice(name="⏸️ Delay (unbestimmt)",       value="delay"),
    app_commands.Choice(name="▶️ Resume (nach Delay/TBA)",  value="resume"),
    app_commands.Choice(name="📅 Datum festlegen (TBA)",     value="set_date"),
    app_commands.Choice(name="🏁 Manuell beenden",           value="end_now"),
    app_commands.Choice(name="✏️ Titel ändern",             value="title"),
    app_commands.Choice(name="📝 Beschreibung ändern",       value="description"),
    app_commands.Choice(name="📢 News senden",               value="news"),
])
async def event_edit(interaction: discord.Interaction, aktion: str):
    if not await has_event_rights(interaction):
        await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        result = supabase.table("events").select("*")\
                         .eq("guild_id", str(interaction.guild_id))\
                         .eq("archived", False)\
                         .neq("status", "cancelled").execute()
        if not result.data:
            await interaction.followup.send("❌ Keine bearbeitbaren Events gefunden.", ephemeral=True)
            return
        view = EventEditSelectView(result.data, aktion)
        await interaction.followup.send(
            f"Welches Event möchtest du bearbeiten? *(Aktion: {aktion})*",
            view=view, ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)


# ── Background Task: Events ───────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def check_events():
    """Aktualisiert Status, sendet Erinnerungen, archiviert abgelaufene Events."""
    try:
        now    = datetime.now(TZ)
        result = supabase.table("events").select("*").eq("archived", False).execute()

        for ev in result.data:
            try:
                status   = ev.get("status", "upcoming")
                end_open = ev.get("end_open", False)

                start_dt = None
                end_dt   = None
                if ev.get("start_time"):
                    start_dt = datetime.fromisoformat(ev["start_time"]).replace(tzinfo=TZ)
                if ev.get("end_time"):
                    end_dt = datetime.fromisoformat(ev["end_time"]).replace(tzinfo=TZ)

                # ── Bereits abgeschlossene / statische Stati ──────────────────
                if status in ("cancelled", "delayed", "ended"):
                    # Nach 24h archivieren
                    if end_dt and (now - end_dt).total_seconds() >= 86400:
                        await _archive_event(ev)
                    elif status == "ended" and not end_dt:
                        # ended ohne Endzeit → nach 24h ab jetzt nicht archivierbar,
                        # aber wir merken uns keinen ended_at → einmalig nach 24h ab Jetzt nicht löschen
                        # Lösung: DB-Feld ended_at setzen wenn vorhanden, sonst skip
                        pass
                    continue

                # ── TBA – Datum noch unbekannt, nichts automatisch tun ────────
                if status == "tba":
                    continue

                # ── open_end – läuft, kein Auto-Ende ─────────────────────────
                if status == "open_end":
                    continue

                # ── upcoming → live / open_end ────────────────────────────────
                if status == "upcoming" and start_dt and now >= start_dt:
                    updates = {}
                    if end_open or not end_dt:
                        # Kein festes Ende → open_end Status
                        updates["status"] = "open_end"
                        status = "open_end"
                        await _notify_thread(
                            ev,
                            "🟢 **Das Event hat begonnen!** (Kein festes Ende – wird manuell beendet)",
                            ping=True
                        )
                    else:
                        # Normaler Start mit Endzeit
                        updates["status"] = "live"
                        status = "live"
                        await _notify_thread(ev, "🟢 **Das Event hat begonnen!**", ping=True)

                    if not ev.get("reminded_start"):
                        updates["reminded_start"] = True

                    supabase.table("events").update(updates).eq("id", ev["id"]).execute()
                    await _update_event_message({**ev, **updates})
                    continue

                # ── live → ended ──────────────────────────────────────────────
                if status == "live" and end_dt and now >= end_dt:
                    updates = {"status": "ended"}
                    supabase.table("events").update(updates).eq("id", ev["id"]).execute()
                    await _notify_thread(ev, "✅ **Das Event ist beendet. Danke fürs Mitmachen!**", ping=True)
                    await _update_event_message({**ev, **updates})
                    continue

                # ── Erinnerungen für upcoming ─────────────────────────────────
                if status == "upcoming" and start_dt:
                    diff_min = (start_dt - now).total_seconds() / 60
                    updates = {}

                    if 59 <= diff_min <= 61 and not ev.get("reminded_1h"):
                        updates["reminded_1h"] = True
                        await _notify_thread(ev, "⏰ **Noch 1 Stunde bis zum Event!**", ping=True)

                    if updates:
                        supabase.table("events").update(updates).eq("id", ev["id"]).execute()

                # ── Archivierung ended Events ─────────────────────────────────
                if status == "ended" and end_dt:
                    if (now - end_dt).total_seconds() >= 86400:
                        await _archive_event(ev)

            except Exception as e:
                print(f"[check_events] Event {ev.get('id')}: {e}")

    except Exception as e:
        print(f"[check_events] Fehler: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# EVENT HANDLERS (Discord)
# ═════════════════════════════════════════════════════════════════════════════

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = f"❌ Fehler: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    print(f"[AppCommandError] {error}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        await bot.process_commands(message)
        return
    if not message.guild:
        await bot.process_commands(message)
        return

    image_attachments = []
    video_attachments = []
    for a in message.attachments:
        fn = a.filename.lower()
        is_image = (a.content_type and a.content_type.startswith("image/")) or any(fn.endswith(e) for e in IMAGE_EXTENSIONS)
        is_video = (a.content_type and a.content_type.startswith("video/")) or any(fn.endswith(e) for e in VIDEO_EXTENSIONS)
        if is_image:   image_attachments.append(a)
        elif is_video: video_attachments.append(a)

    youtube_urls = YOUTUBE_PATTERN.findall(message.content) if message.content else []
    twitch_urls  = TWITCH_CLIP_PATTERN.findall(message.content) if message.content else []

    has_media = image_attachments or video_attachments or youtube_urls or twitch_urls
    if not has_media:
        await bot.process_commands(message)
        return

    try:
        config = await get_settings(str(message.guild.id))
        if not config:
            await bot.process_commands(message)
            return

        image_channel_id = config.get("image_channel_id")
        if not image_channel_id:
            await bot.process_commands(message)
            return

        image_channel = bot.get_channel(int(image_channel_id))
        if not image_channel or message.channel.id == image_channel.id:
            await bot.process_commands(message)
            return

        fw_images  = config.get("forward_images",  True)
        fw_videos  = config.get("forward_videos",  True)
        fw_youtube = config.get("forward_youtube", True)
        fw_twitch  = config.get("forward_twitch",  True)

        active = (
            (fw_images  and image_attachments) or
            (fw_videos  and video_attachments) or
            (fw_youtube and youtube_urls)       or
            (fw_twitch  and twitch_urls)
        )
        if not active:
            await bot.process_commands(message)
            return

        await asyncio.sleep(INSTANCE_DELAY)
        if await check_already_posted(image_channel, message.id):
            print(f"[Dedup] Nachricht {message.id} bereits weitergeleitet – skip.")
            await bot.process_commands(message)
            return

        if fw_images:
            for attachment in image_attachments:
                await forward_image(image_channel, attachment, message)
        if fw_videos:
            for attachment in video_attachments:
                await forward_video(image_channel, attachment, message)
        if fw_youtube:
            for url in youtube_urls:
                await forward_youtube(image_channel, url, message)
        if fw_twitch:
            for url in twitch_urls:
                await forward_twitch_clip(image_channel, url, message)

    except Exception as e:
        print(f"[on_message] Fehler beim Weiterleiten: {e}")

    await bot.process_commands(message)


@bot.event
async def on_ready():
    print(f"✅ Bot ist online als {bot.user}")
    bot.add_view(SpielabendView())
    bot.add_view(EventFollowView())
    await register_role_views()
    await bot.tree.sync()
    print("✅ Commands synchronisiert")
    if not check_reminders.is_running():
        check_reminders.start()
    if not check_events.is_running():
        check_events.start()
    print("✅ Bot ist bereit!")


# ── Start ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    bot.run(TOKEN)
