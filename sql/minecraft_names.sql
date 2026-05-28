create table public.minecraft_names (
  user_id text not null,
  server_id text not null,
  mc_name text not null,
  message_id text null,
  updated_at timestamp with time zone null default now(),
  constraint minecraft_names_pkey primary key (user_id, server_id)
) TABLESPACE pg_default;