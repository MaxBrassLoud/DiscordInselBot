create table public.application_messages (
  id bigserial not null,
  server_id text not null,
  app_id integer not null,
  user_id text null,
  user_name text null,
  content text null,
  attachments jsonb null default '[]'::jsonb,
  timestamp timestamp with time zone null default now(),
  is_deleted boolean null default false,
  deleted_at timestamp with time zone null,
  edit_history jsonb null default '[]'::jsonb,
  discord_message_id text null,
  constraint application_messages_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_application_messages on public.application_messages using btree (server_id, app_id) TABLESPACE pg_default;

create index IF not exists idx_app_messages_app on public.application_messages using btree (server_id, app_id) TABLESPACE pg_default;