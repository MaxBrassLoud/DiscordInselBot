"""
bot/core/supabase_client.py
============================
FIXES:
  - [CRITICAL] Thread-safe Initialisierung via threading.Lock
    Verhindert Race Condition bei threaded=True Flask + gleichzeitigen Requests
"""

import threading
from supabase import create_client, Client

_supabase: Client | None = None
_lock = threading.Lock()


def init_supabase(url: str, key: str):
    global _supabase
    with _lock:
        if _supabase is None:
            _supabase = create_client(url, key)


def get_supabase() -> Client:
    if _supabase is None:
        raise RuntimeError("Supabase not initialized. Call init_supabase() first.")
    return _supabase
