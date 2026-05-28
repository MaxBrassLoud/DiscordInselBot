create table public.events (
  id bigint generated always as identity not null,
  guild_id text not null,
  message_id text not null,
  thread_id text not null,
  channel_id text not null,
  title text not null,
  description text null default ''::text,
  start_time text null,
  end_time text null,
  status text null default 'upcoming'::text,
  followers jsonb null default '[]'::jsonb,
  creator_id text null,
  reminded_1h boolean null default false,
  reminded_start boolean null default false,
  archived boolean null default false,
  end_open boolean not null default false,
  constraint events_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_events_guild on public.events using btree (guild_id) TABLESPACE pg_default;

create index IF not exists idx_events_archived on public.events using btree (archived) TABLESPACE pg_default;