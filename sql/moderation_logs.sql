create table public.moderation_logs (
  id bigserial not null,
  server_id text not null,
  action text not null,
  target_id text not null,
  target_name text null,
  moderator_id text null,
  moderator_name text null,
  reason text null,
  duration_seconds integer null,
  until timestamp with time zone null,
  created_at timestamp with time zone null default now(),
  constraint moderation_logs_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_moderation_logs_server on public.moderation_logs using btree (server_id, created_at desc) TABLESPACE pg_default;

create index IF not exists idx_moderation_logs_target on public.moderation_logs using btree (server_id, target_id, created_at desc) TABLESPACE pg_default;