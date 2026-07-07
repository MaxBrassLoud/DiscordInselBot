CREATE TABLE IF NOT EXISTS whitelist_links (
    id BIGSERIAL PRIMARY KEY,
    discord_id TEXT NOT NULL,
    minecraft_name TEXT NOT NULL,
    server_id TEXT NOT NULL,
    ticket_id INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(discord_id, server_id)
);