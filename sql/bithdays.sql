create table public.birthdays (
  user_id text not null,
  server_id text not null,
  birthday date not null,
  constraint birthdays_pkey primary key (user_id, server_id)
) TABLESPACE pg_default;