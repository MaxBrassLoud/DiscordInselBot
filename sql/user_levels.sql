create table public.user_levels (
  id bigserial not null,
  user_id text not null,
  server_id text not null,
  xp integer null default 0,
  level integer null default 0,
  messages integer null default 0,
  voice_minutes integer null default 0,
  reactions integer null default 0,
  last_msg_at timestamp with time zone null,
  updated_at timestamp with time zone null default now(),
  constraint user_levels_pkey primary key (id),
  constraint unique_user_server unique (user_id, server_id),
  constraint user_levels_user_id_server_id_key unique (user_id, server_id)
) TABLESPACE pg_default;

create index IF not exists idx_user_levels_server on public.user_levels using btree (server_id, xp desc) TABLESPACE pg_default;