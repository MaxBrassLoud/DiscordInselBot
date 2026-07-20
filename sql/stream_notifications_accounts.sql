    CREATE TABLE IF NOT EXISTS stream_notifications_config (
        id          BIGSERIAL PRIMARY KEY,
        guild_id    TEXT NOT NULL UNIQUE,
        channel_id  TEXT,
        enabled     BOOLEAN DEFAULT TRUE,
        created_at  TIMESTAMPTZ DEFAULT now()
    );

    CREATE TABLE IF NOT EXISTS stream_notifications_accounts (
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
    CREATE INDEX IF NOT EXISTS idx_stream_notif_accounts_guild
        ON stream_notifications_accounts (guild_id);