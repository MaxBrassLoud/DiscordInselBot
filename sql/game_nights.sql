create table public.game_nights (
  id bigserial not null,
  guild_id text not null,
  message_id text not null,
  thread_id text not null,
  titel text not null,
  uhrzeit text not null,
  zeitpunkt timestamp with time zone null,
  beschreibung text null,
  dabei text[] null default '{}'::text[],
  vielleicht text[] null default '{}'::text[],
  keine_zeit text[] null default '{}'::text[],
  creator_id text not null,
  reminded_1h boolean null default false,
  reminded_10m boolean null default false,
  reminded_start boolean null default false,
  created_at timestamp with time zone null default now(),
  constraint game_nights_pkey primary key (id),
  constraint game_nights_message_id_key unique (message_id)
) TABLESPACE pg_default;

create index IF not exists idx_game_nights_guild on public.game_nights using btree (guild_id) TABLESPACE pg_default;