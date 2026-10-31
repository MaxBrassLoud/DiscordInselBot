"""
bot/features/stream_notifications/manager.py
=============================================
Zentrale Verwaltung der Streamer-Datensätze in Supabase.

Alle Funktionen sind synchron; Aufrufer nutzen sie direkt oder via
`asyncio.to_thread`, falls der Eventloop geschont werden soll.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.core.supabase_client import get_supabase
from bot.utils.logger import get_logger

logger = get_logger("stream_notifications.manager")

INVITATION_TTL_HOURS = 24 * 7  # 7 Tage


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class StreamAccountManager:

    # ── Config ────────────────────────────────────────────────────────────
    @staticmethod
    def get_config(guild_id: str) -> dict:
        try:
            r = get_supabase().table("stream_notifications_config") \
                .select("*").eq("guild_id", str(guild_id)).limit(1).execute()
            return dict(r.data[0]) if r.data else {}
        except Exception as e:
            logger.error(f"[manager] get_config: {e}")
            return {}

    @staticmethod
    def upsert_config(guild_id: str, data: dict) -> bool:
        try:
            sb = get_supabase()
            payload = {**data, "guild_id": str(guild_id)}
            existing = sb.table("stream_notifications_config") \
                .select("id").eq("guild_id", str(guild_id)).execute()
            if existing.data:
                sb.table("stream_notifications_config").update(payload) \
                    .eq("guild_id", str(guild_id)).execute()
            else:
                sb.table("stream_notifications_config").insert(payload).execute()
            return True
        except Exception as e:
            logger.error(f"[manager] upsert_config: {e}")
            return False

    # ── Accounts ──────────────────────────────────────────────────────────
    @staticmethod
    def list_accounts(guild_id: str) -> list[dict]:
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("guild_id", str(guild_id)) \
                .order("added_at", desc=False).execute()
            return r.data or []
        except Exception as e:
            logger.error(f"[manager] list_accounts: {e}")
            return []

    @staticmethod
    def get_account(account_id: int) -> Optional[dict]:
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("id", account_id).limit(1).execute()
            return dict(r.data[0]) if r.data else None
        except Exception as e:
            logger.error(f"[manager] get_account: {e}")
            return None

    @staticmethod
    def find_account_by_token(token: str) -> Optional[dict]:
        if not token:
            return None
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("invitation_token", token).limit(1).execute()
            return dict(r.data[0]) if r.data else None
        except Exception as e:
            logger.error(f"[manager] find_account_by_token: {e}")
            return None

    @staticmethod
    def get_accounts_for_discord_user(discord_user_id: str) -> list[dict]:
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("discord_user_id", str(discord_user_id)) \
                .order("added_at", desc=True).execute()
            return r.data or []
        except Exception as e:
            logger.error(f"[manager] get_accounts_for_discord_user: {e}")
            return []

    @staticmethod
    def get_active_accounts() -> list[dict]:
        """Alle akzeptierten Accounts (über alle Server) für den Checker."""
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("invitation_status", "accepted").execute()
            return r.data or []
        except Exception as e:
            logger.error(f"[manager] get_active_accounts: {e}")
            return []

    @staticmethod
    def get_pending_invitations() -> list[dict]:
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .select("*").eq("invitation_status", "pending").execute()
            return r.data or []
        except Exception as e:
            logger.error(f"[manager] get_pending_invitations: {e}")
            return []

    @staticmethod
    def create_account(
        guild_id: str,
        platform: str,
        account_id: str,
        account_name: str,
        discord_user_id: Optional[str],
        alert_enabled: bool,
        added_by: str,
    ) -> Optional[dict]:
        token: Optional[str] = None
        expires: Optional[str] = None
        status = "none"
        if discord_user_id:
            token = secrets.token_urlsafe(32)
            expires = (
                datetime.now(timezone.utc) + timedelta(hours=INVITATION_TTL_HOURS)
            ).isoformat()
            status = "pending"

        payload = {
            "guild_id":               str(guild_id),
            "platform":               platform,
            "account_id":             account_id,
            "account_name":           account_name,
            "discord_user_id":        discord_user_id,
            "alert_enabled":          alert_enabled,
            "added_by":               str(added_by),
            "invitation_status":      status,
            "invitation_token":       token,
            "invitation_expires_at":  expires,
            "is_live":                False,
            "live_role_given":        False,
            "streamer_role_given":    False,
        }
        try:
            r = get_supabase().table("stream_notifications_accounts") \
                .insert(payload).execute()
            return dict(r.data[0]) if r.data else None
        except Exception as e:
            logger.error(f"[manager] create_account: {e}")
            return None

    @staticmethod
    def update_account(account_id: int, fields: dict) -> bool:
        try:
            fields = {**fields, "added_at": fields.get("added_at")} \
                if "added_at" in fields else fields
            # leeres added_at wieder entfernen, um Unfug zu vermeiden
            fields.pop("added_at", None)
            get_supabase().table("stream_notifications_accounts") \
                .update(fields).eq("id", account_id).execute()
            return True
        except Exception as e:
            logger.error(f"[manager] update_account: {e}")
            return False

    @staticmethod
    def delete_account(account_id: int) -> bool:
        try:
            get_supabase().table("stream_notifications_accounts") \
                .delete().eq("id", account_id).execute()
            return True
        except Exception as e:
            logger.error(f"[manager] delete_account: {e}")
            return False

    # ── Refresh-Token ─────────────────────────────────────────────────────
    @staticmethod
    def issue_new_invitation_token(account_id: int) -> Optional[str]:
        token = secrets.token_urlsafe(32)
        expires = (
            datetime.now(timezone.utc) + timedelta(hours=INVITATION_TTL_HOURS)
        ).isoformat()
        ok = StreamAccountManager.update_account(account_id, {
            "invitation_status":      "pending",
            "invitation_token":       token,
            "invitation_expires_at":  expires,
        })
        return token if ok else None

    @staticmethod
    def is_expired(account: dict) -> bool:
        exp = account.get("invitation_expires_at")
        if not exp:
            return False
        try:
            exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            return exp_dt < datetime.now(timezone.utc)
        except Exception:
            return False