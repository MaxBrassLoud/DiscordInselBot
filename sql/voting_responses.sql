create table public.voting_responses (
  id bigserial not null,
  voting_id text not null,
  voter_hash text not null,
  answers text not null,
  is_encrypted boolean null default false,
  submitted_at timestamp with time zone null default now(),
  constraint voting_responses_pkey primary key (id),
  constraint voting_responses_voting_id_voter_hash_key unique (voting_id, voter_hash),
  constraint voting_responses_voting_id_fkey foreign KEY (voting_id) references votings (id)
) TABLESPACE pg_default;