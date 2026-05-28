create table public.ticket_servers (
  id bigserial not null,
  server_id text not null,
  category_id text null,
  panel_channel_id text null,
  log_channel_id text null,
  staff_ping_channel_id text null,
  ticket_counter integer null default 0,
  created_at timestamp with time zone null default now(),
  updated_at timestamp with time zone null default now(),
  panel_message_id text null,
  web_admin_role_ids text null default ''::text,
  constraint ticket_servers_pkey primary key (id),
  constraint ticket_servers_server_id_key unique (server_id)
) TABLESPACE pg_default;

create index IF not exists idx_ticket_servers_server on public.ticket_servers using btree (server_id) TABLESPACE pg_default;