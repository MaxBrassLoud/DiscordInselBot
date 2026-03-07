from supabase import create_client, Client

_supabase: Client | None = None


def init_supabase(url: str, key: str):
    global _supabase
    _supabase = create_client(url, key)


def get_supabase() -> Client:
    if _supabase is None:
        raise RuntimeError("Supabase not initialized. Call init_supabase() first.")
    return _supabase