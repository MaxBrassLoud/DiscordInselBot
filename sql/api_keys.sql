-- sql/api_keys.sql
CREATE TABLE IF NOT EXISTS public.api_keys (
    id           BIGSERIAL PRIMARY KEY,
    key_hash     TEXT NOT NULL UNIQUE,      -- SHA-256 Hash des Keys (nie den Key selbst speichern!)
    label        TEXT,                       -- z.B. "Insel Website", "Monitoring"
    guild_id     TEXT,                       -- optional: nur für einen Server gültig
    scopes       TEXT NOT NULL DEFAULT 'users.read',  -- Komma-separiert
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    rate_limit   INTEGER NOT NULL DEFAULT 60,          -- Anfragen pro Minute
    last_used_at TIMESTAMPTZ,
    created_by   TEXT,
    created_at   TIMESTAMPTZ DEFAULT now(),
    expires_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON public.api_keys (key_hash) WHERE enabled = TRUE;