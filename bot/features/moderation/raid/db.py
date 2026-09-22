from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Optional

import discord

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

from .config import CONFIG

logger = get_logger("raid.db")


def _run_db(func, *args, **kwargs):
    return asyncio.to_thread(func, *args, **kwargs)


class RaidDatabase:
    def __init__(self):
        # server_id -> {"roles": set[str], "timestamp": float}
        self._mod_cache: dict[str, dict[str, Any]] = {}
        # server_id -> {"users": dict[int, datetime], "timestamp": float}
        self._allow_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Moderator-Rollen
    # (DB-Tabelle heisst weiterhin 'raid_ignored_roles' aus Kompatibilitaet)
    # ------------------------------------------------------------------
    async def get_moderator_role_ids(self, guild: discord.Guild) -> set[str]:
        server_id = str(guild.id)
        now = datetime.now().timestamp()
        cache = self._mod_cache.get(server_id)
        if cache and (now - cache["timestamp"]) < 60:
            return cache["roles"]

        def query() -> set[str]:
            response = (
                get_supabase()
                .table("raid_ignored_roles")
                .select("role_id")
                .eq("server_id", server_id)
                .execute()
            )
            return {row["role_id"] for row in response.data} if response.data else set()

        try:
            role_ids = await _run_db(query)
            self._mod_cache[server_id] = {"roles": role_ids, "timestamp": now}
            return role_ids
        except Exception as e:
            logger.warning(f"[raid] moderator roles fetch failed: {e}")
            return set()

    async def add_moderator_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)

        def query() -> bool:
            sb = get_supabase()
            check = (
                sb.table("raid_ignored_roles")
                .select("role_id")
                .eq("server_id", server_id)
                .eq("role_id", role_id)
                .execute()
            )
            if check.data:
                return False
            sb.table("raid_ignored_roles").insert(
                {"server_id": server_id, "role_id": role_id}
            ).execute()
            return True

        try:
            success = await _run_db(query)
            self._mod_cache.pop(server_id, None)
            return success
        except Exception as e:
            logger.warning(f"[raid] moderator role insert failed: {e}")
            return False

    async def remove_moderator_role(self, guild: discord.Guild, role: discord.Role) -> bool:
        server_id = str(guild.id)
        role_id = str(role.id)

        def query() -> None:
            (
                get_supabase()
                .table("raid_ignored_roles")
                .delete()
                .eq("server_id", server_id)
                .eq("role_id", role_id)
                .execute()
            )

        try:
            await _run_db(query)
            self._mod_cache.pop(server_id, None)
            return True
        except Exception as e:
            logger.warning(f"[raid] moderator role delete failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Allow-Liste (User temporaer ausnehmen)
    # ------------------------------------------------------------------
    async def get_allowed_users(self, guild: discord.Guild) -> dict[int, datetime]:
        """Gibt {user_id: until} zurueck, nur noch nicht abgelaufene."""
        server_id = str(guild.id)
        now = datetime.now().timestamp()
        cache = self._allow_cache.get(server_id)
        if cache and (now - cache["timestamp"]) < 60:
            return cache["users"]

        def query() -> dict[int, datetime]:
            response = (
                get_supabase()
                .table("raid_allowlist")
                .select("user_id,until")
                .eq("server_id", server_id)
                .execute()
            )
            result: dict[int, datetime] = {}
            now_utc = datetime.now(timezone.utc)
            for row in response.data or []:
                try:
                    until = datetime.fromisoformat(row["until"].replace("Z", "+00:00"))
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if until > now_utc:
                    try:
                        result[int(row["user_id"])] = until
                    except (TypeError, ValueError):
                        continue
            return result

        try:
            users = await _run_db(query)
            self._allow_cache[server_id] = {"users": users, "timestamp": now}
            return users
        except Exception as e:
            logger.warning(f"[raid] allowlist fetch failed: {e}")
            return {}

    async def add_allowed_user(
        self,
        guild: discord.Guild,
        user_id: int,
        until: datetime,
        added_by: int,
    ) -> bool:
        server_id = str(guild.id)

        def query() -> None:
            sb = get_supabase()
            (
                sb.table("raid_allowlist")
                .delete()
                .eq("server_id", server_id)
                .eq("user_id", str(user_id))
                .execute()
            )
            sb.table("raid_allowlist").insert(
                {
                    "server_id": server_id,
                    "user_id": str(user_id),
                    "until": until.isoformat(),
                    "added_by": str(added_by),
                    "added_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()

        try:
            await _run_db(query)
            self._allow_cache.pop(server_id, None)
            return True
        except Exception as e:
            logger.warning(f"[raid] allowlist insert failed: {e}")
            return False

    async def remove_allowed_user(self, guild: discord.Guild, user_id: int) -> bool:
        server_id = str(guild.id)

        def query() -> int:
            response = (
                get_supabase()
                .table("raid_allowlist")
                .delete()
                .eq("server_id", server_id)
                .eq("user_id", str(user_id))
                .execute()
            )
            return len(response.data) if response.data else 0

        try:
            removed = await _run_db(query)
            self._allow_cache.pop(server_id, None)
            return removed > 0
        except Exception as e:
            logger.warning(f"[raid] allowlist delete failed: {e}")
            return False

    # ------------------------------------------------------------------
    # Log-Channel
    # ------------------------------------------------------------------
    async def get_log_channel(
        self, guild: discord.Guild
    ) -> Optional[discord.TextChannel]:
        if CONFIG["log_channel_override"]:
            channel = guild.get_channel(CONFIG["log_channel_override"])
            if isinstance(channel, discord.TextChannel):
                return channel

        def query() -> Optional[str]:
            response = (
                get_supabase()
                .table("settings")
                .select("moderation_log_channel_id")
                .eq("guild_id", str(guild.id))
                .execute()
            )
            if response.data:
                return response.data[0].get("moderation_log_channel_id")
            return None

        try:
            channel_id = await _run_db(query)
            if channel_id:
                channel = guild.get_channel(int(channel_id))
                if isinstance(channel, discord.TextChannel):
                    return channel
        except Exception as e:
            logger.warning(f"[raid] log channel fetch failed: {e}")
        return None

    # ------------------------------------------------------------------
    # Moderationslog
    # ------------------------------------------------------------------
    async def log_moderation_action(
        self,
        guild_id: str,
        action: str,
        target_id: str,
        target_name: str,
        moderator_id: Optional[str],
        moderator_name: Optional[str],
        reason: str,
        until: Optional[datetime] = None,
    ) -> None:
        def query() -> None:
            get_supabase().table("moderation_logs").insert(
                {
                    "server_id": guild_id,
                    "action": action,
                    "target_id": target_id,
                    "target_name": target_name,
                    "moderator_id": moderator_id,
                    "moderator_name": moderator_name,
                    "reason": reason,
                    "until": until.isoformat() if until else None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()

        try:
            await _run_db(query)
        except Exception as e:
            logger.warning(f"[raid] moderation log insert failed: {e}")