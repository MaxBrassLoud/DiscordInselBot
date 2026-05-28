create table public.ticket_participants (
  id bigserial not null,
  ticket_id integer not null,
  server_id text not null,
  user_id text not null,
  user_name text null,
  avatar_url text null,
  action text null default 'message'::text,
  first_seen timestamp with time zone null default now(),
  last_seen timestamp with time zone null default now(),
  message_count integer null default 0,
  constraint ticket_participants_pkey primary key (id),
  constraint ticket_participants_ticket_id_server_id_user_id_key unique (ticket_id, server_id, user_id)
) TABLESPACE pg_default;