create table public.tickets (
  id bigserial not null,
  ticket_id integer not null,
  server_id text not null,
  module text not null,
  creator_id text not null,
  claimed_by text null,
  status text null default 'open'::text,
  channel_id text null,
  created_at timestamp with time zone null default now(),
  closed_at timestamp with time zone null,
  description text null,
  creator_name text null,
  closed_by text null,
  added_users jsonb null default '[]'::jsonb,
  title text not null default ''::text,
  imported boolean not null default false,
  import_source text null,
  external_channel_id text null,
  content text null,
  constraint tickets_pkey primary key (id),
  constraint tickets_server_id_ticket_id_key unique (server_id, ticket_id)
) TABLESPACE pg_default;

create index IF not exists idx_tickets_server on public.tickets using btree (server_id) TABLESPACE pg_default;

create index IF not exists idx_tickets_status on public.tickets using btree (status) TABLESPACE pg_default;

create index IF not exists idx_tickets_creator on public.tickets using btree (creator_id) TABLESPACE pg_default;

create index IF not exists idx_tickets_imported on public.tickets using btree (server_id, imported) TABLESPACE pg_default
where
  (imported = true);

create unique INDEX IF not exists idx_tickets_external_channel on public.tickets using btree (server_id, external_channel_id) TABLESPACE pg_default
where
  (external_channel_id is not null);