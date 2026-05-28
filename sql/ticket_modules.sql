create table public.ticket_modules (
  id bigserial not null,
  server_id text not null,
  name text not null,
  description text null default ''::text,
  max_tickets integer null default 1,
  modal_question text null default 'Bitte beschreibe dein Anliegen.'::text,
  created_at timestamp with time zone null default now(),
  category_id text null,
  button_emoji text null default '🎫'::text,
  constraint ticket_modules_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_ticket_modules_server on public.ticket_modules using btree (server_id) TABLESPACE pg_default;