import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv

from bot.core.supabase_client import init_supabase
from bot.core.keep_alive import keep_alive
from bot.utils.logger import get_logger

logger = get_logger("server")

# ── Env & Supabase ────────────────────────────────────────────────────────────
_base_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_base_dir, "../../.env"))

init_supabase(
    url=os.getenv("SUPABASE_URL"),
    key=os.getenv("SUPABASE_KEY"),
)



# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="?", intents=intents)

# ── Feature Cogs ─────────────────────────────────────────────────────────────
FEATURE_COGS = [
    "bot.features.spieleabend.cog",
    "bot.features.media.cog",
    "bot.features.events.cog",
    "bot.features.welcomer.cog",
    "bot.features.rollen.cog",
    "bot.features.tickets.cog",
    "bot.features.applications.cog",
]


@bot.event
async def on_ready():
    logger.info(f"✅ Bot ist online als {bot.user}")
    await bot.tree.sync()
    logger.info("✅ Commands synchronisiert")
    logger.info("✅ Bot ist bereit!")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    msg = f"❌ Fehler: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)
    logger.error(f"[AppCommandError] {error}")


@bot.command()
async def Ping(ctx):
    await ctx.send("Pong!")


async def main():
    await keep_alive()
    async with bot:
        for cog in FEATURE_COGS:
            try:
                await bot.load_extension(cog)
                logger.info(f"✅ Geladen: {cog}")
            except Exception as e:
                logger.error(f"❌ Fehler beim Laden von {cog}: {e}")
        await bot.start(os.getenv("DISCORD_TOKEN"))


if __name__ == "__main__":
    asyncio.run(main())