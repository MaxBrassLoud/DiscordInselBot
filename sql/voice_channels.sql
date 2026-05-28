create table public.voice_channels (
  id bigserial not null,
  server_id text not null,
  owner_id text not null,
  main_channel_id text not null,
  wait_channel_id text null,
  is_open boolean not null default true,
  created_at timestamp with time zone null default now(),
  last_empty_at timestamp with time zone null,
  panel_message_id text null,
  user_limit integer not null default 0,
  access_role_id text null,
  rejected_user_ids jsonb null default '[]'::jsonb,
  reject_duration_minutes integer null default 5,
  banned_user_ids jsonb null default '[]'::jsonb,
  constraint voice_channels_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_voice_channels_server on public.voice_channels using btree (server_id) TABLESPACE pg_default;

create index IF not exists idx_voice_channels_main on public.voice_channels using btree (main_channel_id) TABLESPACE pg_default;

create index IF not exists idx_voice_channels_wait on public.voice_channels using btree (wait_channel_id) TABLESPACE pg_default;

create index IF not exists idx_voice_channels_owner on public.voice_channels using btree (server_id, owner_id) TABLESPACE pg_default;