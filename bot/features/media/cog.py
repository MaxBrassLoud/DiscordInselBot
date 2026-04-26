import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import re
import random

from bot.core.settings import get_settings, upsert_settings
from bot.utils.permissions import has_admin_rights
from bot.utils.logger import get_logger
from .forwarder import (
    forward_image, forward_video, forward_youtube, forward_twitch_clip,
    check_already_posted, YOUTUBE_PATTERN, TWITCH_CLIP_PATTERN,
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
)

logger = get_logger("media")

INSTANCE_DELAY = random.uniform(0.2, 1.5)


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
        channel_select = discord.ui.ChannelSelect(
            placeholder="📺 Ziel-Kanal für weitergeleitete Medien",
            min_values=1, max_values=1,
            channel_types=[discord.ChannelType.text],
        )
        channel_select.callback = self.channel_callback
        self.add_item(channel_select)
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        for item in [i for i in self.children if isinstance(i, discord.ui.Button)]:
            self.remove_item(item)

        def make_toggle(label, emoji, feature, state):
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

        self.add_item(make_toggle("Bilder",  "📷", "feature_images",  self.feature_images))
        self.add_item(make_toggle("Videos",  "🎥", "feature_videos",  self.feature_videos))
        self.add_item(make_toggle("YouTube", "▶️", "feature_youtube", self.feature_youtube))
        self.add_item(make_toggle("Twitch",  "💜", "feature_twitch",  self.feature_twitch))

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
            description="Wähle den Ziel-Kanal und aktiviere/deaktiviere die Features.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="📺 Ziel-Kanal", value=f"<#{self.image_channel_id}>" if self.image_channel_id else "*Nicht ausgewählt*", inline=False)
        embed.add_field(name="🔧 Features", value=(
            f"📷 Bilder: {'✅ AN' if self.feature_images else '❌ AUS'}\n"
            f"🎥 Videos: {'✅ AN' if self.feature_videos else '❌ AUS'}\n"
            f"▶️ YouTube: {'✅ AN' if self.feature_youtube else '❌ AUS'}\n"
            f"💜 Twitch: {'✅ AN' if self.feature_twitch else '❌ AUS'}"
        ), inline=False)
        return embed

    async def channel_callback(self, interaction: discord.Interaction):
        self.image_channel_id = interaction.data['values'][0]
        self._rebuild_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    async def save_callback(self, interaction: discord.Interaction):
        try:
            await upsert_settings(str(self.guild_id), {
                "image_channel_id": self.image_channel_id,
                "forward_images":   self.feature_images,
                "forward_videos":   self.feature_videos,
                "forward_youtube":  self.feature_youtube,
                "forward_twitch":   self.feature_twitch,
            })
            embed = self._build_embed()
            embed.color = discord.Color.green()
            embed.title = "✅ Media-Setup gespeichert!"
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            if interaction.response.is_done():
                await interaction.followup.send(f"❌ Fehler: {e}", ephemeral=True)
            else:
                await interaction.response.send_message(f"❌ Fehler: {e}", ephemeral=True)


class MediaCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    media = app_commands.Group(
        name="media",
        description="Medien Weiterleitungssystem"
    )
    @media.command(name="setup_media", description="Konfiguriere die Medien-Weiterleitung")
    async def setup_media(self, interaction: discord.Interaction):
        if not has_admin_rights(interaction):
            await interaction.response.send_message("❌ Keine Berechtigung.", ephemeral=True)
            return
        current_config = await get_settings(str(interaction.guild_id))
        view = SetupMediaView(interaction.guild_id, current_config)
        await interaction.response.send_message(embed=view._build_embed(), view=view, ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if not message.guild:
            return

        image_attachments, video_attachments = [], []
        for a in message.attachments:
            fn = a.filename.lower()
            is_image = (a.content_type and a.content_type.startswith("image/")) or any(fn.endswith(e) for e in IMAGE_EXTENSIONS)
            is_video = (a.content_type and a.content_type.startswith("video/")) or any(fn.endswith(e) for e in VIDEO_EXTENSIONS)
            if is_image:   image_attachments.append(a)
            elif is_video: video_attachments.append(a)

        youtube_urls = YOUTUBE_PATTERN.findall(message.content) if message.content else []
        twitch_urls  = TWITCH_CLIP_PATTERN.findall(message.content) if message.content else []

        if not (image_attachments or video_attachments or youtube_urls or twitch_urls):
            return

        try:
            config = await get_settings(str(message.guild.id))
            if not config or not config.get("image_channel_id"):
                return
            image_channel = self.bot.get_channel(int(config["image_channel_id"]))
            if not image_channel or message.channel.id == image_channel.id:
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
                return

            await asyncio.sleep(INSTANCE_DELAY)
            if await check_already_posted(self.bot, image_channel, message.id):
                return

            if fw_images:
                for a in image_attachments:
                    await forward_image(image_channel, a, message)
            if fw_videos:
                for a in video_attachments:
                    await forward_video(image_channel, a, message)
            if fw_youtube:
                for url in youtube_urls:
                    await forward_youtube(image_channel, url, message)
            if fw_twitch:
                for url in twitch_urls:
                    await forward_twitch_clip(image_channel, url, message)
        except Exception as e:
            logger.error(f"[on_message] Fehler: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaCog(bot))