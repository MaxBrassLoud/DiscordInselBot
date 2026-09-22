-- Tabelle für ignorierte Rollen bei der Raid-Erkennung
create table public.raid_ignored_roles (
  server_id text not null,
  role_id text not null,
  created_at timestamp with time zone null default now(),
  constraint raid_ignored_roles_pkey primary key (server_id, role_id)
) TABLESPACE pg_default;

-- Index für schnellere Abfragen
create index if not exists idx_raid_ignored_roles_server on public.raid_ignored_roles using btree (server_id) TABLESPACE pg_default;

create table if not exists raid_allowlist (
    server_id text        not null,
    user_id   text        not null,
    until     timestamptz not null,
    added_by  text,
    added_at  timestamptz default now(),
    primary key (server_id, user_id)
);

create index if not exists raid_allowlist_server_idx
    on raid_allowlist (server_id);

create index if not exists raid_allowlist_until_idx
    on raid_allowlist (until);