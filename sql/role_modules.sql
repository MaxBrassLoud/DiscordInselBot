create table public.role_modules (
  id bigint generated always as identity not null,
  guild_id text not null,
  role_id text not null,
  role_name text not null,
  role_desc text null default ''::text,
  channel_id text not null,
  display_name text null,
  message_id text null,
  constraint role_modules_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_role_modules_guild on public.role_modules using btree (guild_id) TABLESPACE pg_default;
-- 1) emoji-Spalte zu role_modules hinzufügen
ALTER TABLE role_modules
  ADD COLUMN IF NOT EXISTS emoji TEXT DEFAULT '🎭';

-- 2) Neue Tabelle für die EINE Übersichts-Nachricht pro Server
CREATE TABLE IF NOT EXISTS role_message (
  guild_id    TEXT PRIMARY KEY,
  channel_id  TEXT NOT NULL,
  message_id  TEXT NOT NULL
);

-- Optional: bestehende Module ohne Emoji mit Default befüllen
UPDATE role_modules SET emoji = '🎭' WHERE emoji IS NULL;