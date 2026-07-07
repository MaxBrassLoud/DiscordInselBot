import os
import json
import time
import hmac
import hashlib
import requests
from typing import Optional, Dict, Any
from bot.utils.logger import get_logger

logger = get_logger("whw_bridge")

class WHWClient:
    def __init__(self, base_url: str = None, secret: str = None):
        # Stelle sicher, dass base_url mit http:// oder https:// beginnt
        raw_url = base_url or os.getenv("WHW_BASE_URL", "http://localhost:8080")
        if not raw_url.startswith(("http://", "https://")):
            raw_url = "http://" + raw_url
        self.base_url = raw_url.rstrip('/')
        self.secret = secret or os.getenv("WHW_SECRET", "")
        if not self.secret:
            logger.warning("WHW_SECRET nicht gesetzt – Whitelist-Operationen fehlschlagen!")

    def _sign(self, method: str, path: str, body: str, timestamp: str) -> str:
        msg = f"{method}{path}{body}{timestamp}".encode()
        return hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()

    def _call(self, endpoint: str, method: str = "POST", data: Optional[Dict] = None) -> Optional[Dict]:
        if not self.secret:
            logger.error("Kein WHW-Secret konfiguriert")
            return None
        path = f"/api/{endpoint}"
        body = json.dumps(data, separators=(',', ':'), ensure_ascii=False) if data else ""
        timestamp = str(int(time.time()))
        signature = self._sign(method, path, body, timestamp)
        headers = {
            "X-API-Timestamp": timestamp,
            "X-API-Signature": signature,
            "Content-Type": "application/json"
        }
        url = self.base_url + path
        try:
            if method.upper() == "GET":
                resp = requests.get(url, headers=headers, timeout=5)
            else:
                resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"WHW-API-Fehler ({endpoint}): {e}")
            return None

    # ---- Öffentliche Methoden ----
    def add_whitelist(self, player: str, duration: Optional[str] = None) -> Optional[Dict]:
        data = {"player": player}
        if duration:
            data["duration"] = duration
        return self._call("whitelist/add", data=data)

    def remove_whitelist(self, player: str) -> Optional[Dict]:
        return self._call("whitelist/remove", data={"player": player})

    def add_ban(self, player: str, duration: Optional[str] = None, reason: Optional[str] = None) -> Optional[Dict]:
        data = {"player": player}
        if duration:
            data["duration"] = duration
        if reason:
            data["reason"] = reason
        return self._call("ban/add", data=data)

    def remove_ban(self, player: str) -> Optional[Dict]:
        return self._call("ban/remove", data={"player": player})

    def kick(self, player: str, reason: Optional[str] = None) -> Optional[Dict]:
        data = {"player": player}
        if reason:
            data["reason"] = reason
        return self._call("kick", data=data)

    def get_whitelist(self) -> Optional[Dict]:
        return self._call("whitelist", method="GET")

    def get_bans(self) -> Optional[Dict]:
        return self._call("bans", method="GET")