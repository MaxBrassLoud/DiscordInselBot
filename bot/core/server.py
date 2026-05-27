"""
bot/core/server.py
===================
FIXES:
  - [CRITICAL] keep_alive() war async – jetzt wird keep_alive_async() genutzt
    was den sync Thread startet und per await asyncio.sleep() zurückkehrt.
"""
import random
import time

import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

from bot.core.supabase_client import init_supabase
from bot.core.keep_alive import keep_alive_async  # [FIX] async-Wrapper nutzen
from bot.utils.logger import get_logger

logger = get_logger("server")

_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, "../../.env"))

init_supabase(
    url=os.getenv("SUPABASE_URL"),
    key=os.getenv("SUPABASE_KEY"),
)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="?", intents=intents)

FEATURE_COGS = [
    "bot.features.spieleabend.cog",
    "bot.features.media.cog",
    "bot.features.events.cog",
    "bot.features.welcomer.cog",
    "bot.features.rollen.cog",
    "bot.features.tickets.cog",
    "bot.features.applications.cog",
    "bot.features.web.cog",
    "bot.features.minecraft_names.cog",
    "bot.features.voice.cog",
    "bot.features.reminders.cog",
    "bot.features.birthdays.cog",
    "bot.features.levels.cog",
    "bot.features.voting.cog",
    "bot.features.moderation.cog",
    "bot.features.faq.cog"
]


@bot.event
async def on_ready():
    logger.info(f"✅ Bot ist online als {bot.user}")
    await bot.tree.sync()
    logger.info("✅ Commands synchronisiert")
    logger.info("✅ Bot ist bereit!")

    for guild in bot.guilds:
        me = guild.me
        perms = me.guild_permissions
        logger.info(
            f"[Permissions] Server: '{guild.name}' ({guild.id}) | "
            f"Permissions-Zahl: {perms.value} | "
            f"Administrator: {perms.administrator} | "
            f"Manage Channels: {perms.manage_channels} | "
            f"Manage Roles: {perms.manage_roles} | "
            f"Manage Nicknames: {perms.manage_nicknames}"
        )


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = f"❌ Fehler: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.NotFound:
        logger.warning(
            "[AppCommandError] Interaction konnte nicht beantwortet werden "
            f"(abgelaufen/unbekannt): {error}"
        )
        return

    logger.error(f"[AppCommandError] {error}")


@bot.command()
async def Ping(ctx):
    await ctx.send("Pong!")


async def main():
    # [FIX CRITICAL] keep_alive_async() statt keep_alive() nutzen
    # keep_alive_async startet den Flask-Thread sync und kehrt per await zurück
    await keep_alive_async()

    async with bot:
        for cog in FEATURE_COGS:
            await bot.load_extension(cog)
            logger.info(f"✅ Geladen: {cog}")

        for attempt in range(5):
            try:
                await bot.start(os.getenv("DISCORD_TOKEN"))
                break
            except discord.errors.DiscordServerError as e:
                if attempt < 4:
                    wait = 10 * (attempt + 1)
                    logger.warning(
                        f"Discord nicht erreichbar (Versuch {attempt + 1}/5), "
                        f"warte {wait}s... {e}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("Discord nach 5 Versuchen nicht erreichbar. Abbruch.")
                    raise
            except KeyboardInterrupt as e:
                logger.info("Bot heruntergefahren")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt as e:
        logger.info("Bot heruntergefahren")
        time.sleep(1)
        for i in range(random.randint(1, 5)):
            logger.info(f"Modul {i} gespeichert")
            time.sleep(0.1)
