create table public.settings (
  id bigserial not null,
  guild_id text not null,
  ping_role_id text not null,
  channel_id text not null,
  delete_role_ids text not null,
  created_at timestamp with time zone null default now(),
  image_channel_id text null,
  event_channel_id text null,
  event_role_ids text null,
  forward_images boolean null default true,
  forward_videos boolean null default true,
  forward_youtube boolean null default true,
  forward_twitch boolean null default true,
  welcome_channel_id text null,
  goodbye_channel_id text null,
  welcome_enabled boolean null default true,
  goodbye_enabled boolean null default true,
  birthday_channel_id text null,
  level_channel_id text null,
  levels_enabled boolean null default true,
  moderation_log_channel_id text null,
  constraint settings_pkey primary key (id),
  constraint settings_guild_id_key unique (guild_id)
) TABLESPACE pg_default;

create index IF not exists idx_settings_guild on public.settings using btree (guild_id) TABLESPACE pg_default;