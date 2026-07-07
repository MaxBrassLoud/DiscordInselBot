from zoneinfo import ZoneInfo
from bot.core.settings import get_settings

async def get_guild_timezone(guild_id: int) -> ZoneInfo:
    settings = await get_settings(str(guild_id))
    tz_name = settings.get("timezone", "Europe/Berlin")
    return ZoneInfo(tz_name)