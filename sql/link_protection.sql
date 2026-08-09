-- Tabelle für die Link-Protection-Konfiguration
CREATE TABLE IF NOT EXISTS link_protection_config (
    server_id TEXT PRIMARY KEY,
    enabled BOOLEAN DEFAULT FALSE,
    moderation_log_channel_id TEXT,
    allowed_channel_ids TEXT DEFAULT '',
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Tabelle für erlaubte URLs/Domains (globale Whitelist)
CREATE TABLE IF NOT EXISTS link_protection_allowed (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    url TEXT NOT NULL,
    channel_id TEXT,          -- optional: für spezifischen Kanal
    user_id TEXT,             -- optional: für spezifischen User
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (server_id, url)
);

-- Tabelle für YouTube/Twitch-Kanal-Whitelist
CREATE TABLE IF NOT EXISTS link_protection_platform_whitelist (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    platform TEXT NOT NULL CHECK (platform IN ('youtube', 'twitch')),
    channel_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (server_id, platform, channel_id)
);

-- Tabelle für User-Freigaben
CREATE TABLE IF NOT EXISTS link_protection_user_allow (
    server_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    allowed_until TIMESTAMPTZ,   -- NULL = dauerhaft
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (server_id, user_id)
);

-- Tabelle für Logs
CREATE TABLE IF NOT EXISTS link_protection_logs (
    id BIGSERIAL PRIMARY KEY,
    server_id TEXT NOT NULL,
    action TEXT NOT NULL,      -- block, allow_user, allow_global, deny
    user_id TEXT NOT NULL,
    target_url TEXT,
    moderator_id TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);