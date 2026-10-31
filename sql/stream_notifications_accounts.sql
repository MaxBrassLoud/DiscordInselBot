-- ============================================================================
-- Stream Notifications – komplette, idempotente Migration
-- In Supabase einmal ausführen.  Alle Statements sind wiederholbar.
-- ============================================================================

-- ── 1. Config-Tabelle ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stream_notifications_config (
    id          BIGSERIAL PRIMARY KEY,
    guild_id    TEXT NOT NULL UNIQUE,
    channel_id  TEXT,
    enabled     BOOLEAN DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE public.stream_notifications_config
    ADD COLUMN IF NOT EXISTS streamer_role_id TEXT,
    ADD COLUMN IF NOT EXISTS live_role_id     TEXT,
    ADD COLUMN IF NOT EXISTS log_channel_id   TEXT;

-- ── 2. Accounts-Tabelle ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS public.stream_notifications_accounts (
    id              BIGSERIAL PRIMARY KEY,
    guild_id        TEXT NOT NULL,
    platform        TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
    account_id      TEXT NOT NULL,
    account_name    TEXT,
    channel_id      TEXT,
    role_id         TEXT,
    last_known_id   TEXT,
    is_live         BOOLEAN DEFAULT FALSE,
    added_at        TIMESTAMPTZ DEFAULT now(),
    UNIQUE (guild_id, platform, account_id)
);

-- Legacy-Spalten (v1)
ALTER TABLE public.stream_notifications_accounts
    ADD COLUMN IF NOT EXISTS discord_user_id          TEXT,
    ADD COLUMN IF NOT EXISTS live_role_given          BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS nickname_prefix_applied  BOOLEAN DEFAULT FALSE;

-- v2 – Alert-/Einladungssystem
ALTER TABLE public.stream_notifications_accounts
    ADD COLUMN IF NOT EXISTS alert_enabled          BOOLEAN     NOT NULL DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS invitation_status      TEXT        NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS invitation_token       TEXT,
    ADD COLUMN IF NOT EXISTS invitation_expires_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS added_by               TEXT,
    ADD COLUMN IF NOT EXISTS live_since             TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_check_at          TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_error             TEXT,
    ADD COLUMN IF NOT EXISTS streamer_role_given    BOOLEAN     NOT NULL DEFAULT FALSE;

-- Deprecated-Spalte kommentieren (bleibt aus Kompatibilität erhalten)
COMMENT ON COLUMN public.stream_notifications_accounts.role_id IS
    'DEPRECATED (v1). Rollen werden über config.streamer_role_id + alert_enabled gesteuert.';

-- ── 3. Status-Check (idempotent) ─────────────────────────────────────────────
ALTER TABLE public.stream_notifications_accounts
    DROP CONSTRAINT IF EXISTS stream_notifications_accounts_invitation_status_check;

-- Vor dem Hinzufügen der Check-Constraint: ungültige Werte auf 'none' setzen
UPDATE public.stream_notifications_accounts
SET invitation_status = 'none'
WHERE invitation_status IS NULL
   OR invitation_status NOT IN ('none','pending','accepted','declined','expired');

ALTER TABLE public.stream_notifications_accounts
    ADD CONSTRAINT stream_notifications_accounts_invitation_status_check
    CHECK (invitation_status IN ('none','pending','accepted','declined','expired'));

-- ── 4. Indizes ───────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_guild
    ON public.stream_notifications_accounts (guild_id);

CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_token
    ON public.stream_notifications_accounts (invitation_token)
    WHERE invitation_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_status
    ON public.stream_notifications_accounts (guild_id, invitation_status);

CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_live
    ON public.stream_notifications_accounts (guild_id, is_live);

CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_user
    ON public.stream_notifications_accounts (discord_user_id)
    WHERE discord_user_id IS NOT NULL;

-- ── 5. Unique-Constraint sicherstellen ──────────────────────────────────────
ALTER TABLE public.stream_notifications_accounts
    DROP CONSTRAINT IF EXISTS stream_notifications_accounts_guild_id_platform_account_id_key;

ALTER TABLE public.stream_notifications_accounts
    ADD CONSTRAINT stream_notifications_accounts_guild_id_platform_account_id_key
    UNIQUE (guild_id, platform, account_id);