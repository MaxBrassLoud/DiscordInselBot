create table public.ticket_reminders (
  id bigserial not null,
  entity_type text not null,
  entity_id integer not null,
  server_id text not null,
  creator_id text not null,
  last_staff_msg timestamp with time zone null,
  reminded_at timestamp with time zone null,
  constraint ticket_reminders_pkey primary key (id),
  constraint ticket_reminders_entity_type_entity_id_server_id_key unique (entity_type, entity_id, server_id)
) TABLESPACE pg_default;

create index IF not exists idx_ticket_reminders on public.ticket_reminders using btree (entity_type, server_id, entity_id) TABLESPACE pg_default;