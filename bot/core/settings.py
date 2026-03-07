from datetime import datetime
from bot.core.supabase_client import get_supabase

CACHE_TTL = 5 * 60  # 5 Minuten


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


_settings_cache = SettingsCache()


async def get_settings(guild_id: str) -> dict | None:
    cached = _settings_cache.get(guild_id)
    if cached is not None:
        return cached
    supabase = get_supabase()
    result = supabase.table("settings").select("*").eq("guild_id", guild_id).execute()
    if result.data:
        _settings_cache.set(guild_id, result.data[0])
        return result.data[0]
    return None


async def upsert_settings(guild_id: str, data: dict):
    supabase = get_supabase()
    full_data = {"guild_id": str(guild_id), **data}
    existing = supabase.table("settings").select("id").eq("guild_id", str(guild_id)).execute()
    if existing.data:
        supabase.table("settings").update(full_data).eq("guild_id", str(guild_id)).execute()
    else:
        supabase.table("settings").insert(full_data).execute()
    _settings_cache.invalidate(str(guild_id))


def invalidate_settings(guild_id: str):
    _settings_cache.invalidate(guild_id)