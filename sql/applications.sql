create table public.applications (
  id bigserial not null,
  server_id text not null,
  app_id integer not null,
  creator_id text not null default ''::text,
  creator_name text null,
  minecraft_name text null,
  status text not null default 'open'::text,
  claimed_by text null,
  closed_by text null,
  rejection_reason text null,
  channel_id text null,
  created_at timestamp with time zone null default now(),
  closed_at timestamp with time zone null,
  imported boolean not null default false,
  import_source text null,
  external_channel_id text null,
  content text null,
  constraint applications_pkey primary key (id),
  constraint applications_server_id_app_id_key unique (server_id, app_id)
) TABLESPACE pg_default;

create index IF not exists idx_applications_server on public.applications using btree (server_id) TABLESPACE pg_default;

create index IF not exists idx_applications_creator on public.applications using btree (server_id, creator_id) TABLESPACE pg_default;

create index IF not exists idx_applications_status on public.applications using btree (server_id, status) TABLESPACE pg_default;

create index IF not exists idx_applications_imported on public.applications using btree (server_id, imported) TABLESPACE pg_default
where
  (imported = true);

create unique INDEX IF not exists idx_applications_external_channel on public.applications using btree (server_id, external_channel_id) TABLESPACE pg_default
where
  (external_channel_id is not null);

ALTER TABLE applications ADD COLUMN control_message_id TEXT;