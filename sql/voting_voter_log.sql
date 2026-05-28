create table public.voting_voter_log (
  id bigserial not null,
  voting_id text not null,
  user_id text not null,
  display_name text null,
  username text null,
  avatar_url text null,
  submitted_at timestamp with time zone null default now(),
  constraint voting_voter_log_pkey primary key (id),
  constraint voting_voter_log_voting_id_user_id_key unique (voting_id, user_id),
  constraint voting_voter_log_voting_id_fkey foreign KEY (voting_id) references votings (id)
) TABLESPACE pg_default;