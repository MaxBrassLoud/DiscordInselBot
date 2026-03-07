import re
import asyncio
import yt_dlp
import discord

YOUTUBE_PATTERN = re.compile(
    r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)[\w\-&=?%]+)'
)
TWITCH_CLIP_PATTERN = re.compile(
    r'(https?://(?:www\.)?twitch\.tv/[\w]+/clip/[\w\-]+|https?://clips\.twitch\.tv/[\w\-]+)'
)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".flv", ".m4v", ".wmv"}


async def get_youtube_info(url: str) -> dict | None:
    try:
        ydl_opts = {'quiet': True, 'no_warnings': True, 'skip_download': True}
        loop = asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=False))
        return info
    except Exception as e:
        print(f"[YouTube] Fehler: {e}")
        return None


async def check_already_posted(bot: discord.Client, image_channel: discord.TextChannel, message_id: int) -> bool:
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


async def forward_image(image_channel: discord.TextChannel, attachment: discord.Attachment, message: discord.Message):
    embed = discord.Embed(color=0x5865F2, timestamp=message.created_at)
    embed.set_image(url=attachment.url)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal",     value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    if message.content:
        embed.add_field(name="💬 Text",  value=message.content[:500], inline=False)
    embed.set_footer(text="📷 Bild")
    await image_channel.send(embed=embed)


async def forward_video(image_channel: discord.TextChannel, attachment: discord.Attachment, message: discord.Message):
    embed = discord.Embed(color=0xFEE75C, timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal",     value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    embed.add_field(name="🎬 Video",     value=f"[{attachment.filename}]({attachment.url})", inline=False)
    if message.content:
        embed.add_field(name="💬 Text",  value=message.content[:500], inline=False)
    embed.set_footer(text="🎥 Video")
    await image_channel.send(embed=embed)


async def forward_youtube(image_channel: discord.TextChannel, url: str, message: discord.Message):
    info = await get_youtube_info(url)
    embed = discord.Embed(
        color=0xED4245, timestamp=message.created_at,
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
            hours, minutes   = divmod(minutes, 60)
            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"
            embed.add_field(name="⏱️ Länge", value=duration_str, inline=True)
    embed.add_field(name="📌 Kanal",     value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    clean = message.content.replace(url, "").strip() if message.content else ""
    if clean:
        embed.add_field(name="💬 Text",  value=clean[:500], inline=False)
    embed.set_footer(text="🎬 YouTube")
    await image_channel.send(embed=embed)


async def forward_twitch_clip(image_channel: discord.TextChannel, url: str, message: discord.Message):
    embed = discord.Embed(color=0x9B59B6, timestamp=message.created_at)
    embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
    embed.add_field(name="📌 Kanal",     value=message.channel.mention, inline=True)
    embed.add_field(name="🔗 Nachricht", value=f"[Zum Original]({message.jump_url})", inline=True)
    embed.add_field(name="🎮 Clip",      value=url, inline=False)
    clean = message.content.replace(url, "").strip() if message.content else ""
    if clean:
        embed.add_field(name="💬 Text",  value=clean[:500], inline=False)
    embed.set_footer(text="💜 Twitch Clip")
    await image_channel.send(content=url, embed=embed)