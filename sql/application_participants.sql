create table public.application_participants (
  id bigserial not null,
  app_id integer not null,
  server_id text not null,
  user_id text not null,
  user_name text null,
  avatar_url text null,
  action text null default 'message'::text,
  first_seen timestamp with time zone null default now(),
  last_seen timestamp with time zone null default now(),
  message_count integer null default 0,
  constraint application_participants_pkey primary key (id),
  constraint application_participants_app_id_server_id_user_id_key unique (app_id, server_id, user_id)
) TABLESPACE pg_default;