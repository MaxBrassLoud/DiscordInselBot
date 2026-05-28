create table public.votings (
  id text not null,
  server_id text not null,
  title text not null,
  description text null,
  questions jsonb not null default '[]'::jsonb,
  allowed_users text null,
  public_key text null,
  is_active boolean null default true,
  created_by text not null,
  created_at timestamp with time zone null default now(),
  ends_at timestamp with time zone null,
  constraint votings_pkey primary key (id)
) TABLESPACE pg_default;