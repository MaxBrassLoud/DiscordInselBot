create table public.voice_creator_config (
  id bigserial not null,
  server_id text not null,
  category_id text null,
  channel_id text not null,
  channel_name text not null default 'Kanal erstellen'::text,
  empty_timeout integer not null default 30,
  allowed_role_ids text not null default ''::text,
  creator_role_ids text not null default ''::text,
  constraint voice_creator_config_pkey primary key (id),
  constraint voice_creator_config_server_id_key unique (server_id)
) TABLESPACE pg_default;