-- ============================================================================
-- Wortfilter (Teil des Raid-Schutz-/Moderationssystems)
-- In Supabase einmal ausfuehren.  Alle Befehle sind wiederholbar.
-- ============================================================================

-- Konfiguration pro Server -------------------------------------------------
create table if not exists public.word_filter_config (
  server_id text primary key,
  enabled boolean not null default false,
  -- Trockenlauf: es wird nur protokolliert, nichts geloescht/gebannt.
  dry_run boolean not null default false,
  -- Standard-Aktion bei einem Treffer: delete | warn | timeout | kick | ban
  default_action text not null default 'timeout',
  timeout_minutes integer not null default 10080,
  log_channel_id text null,
  -- Komma-getrennte IDs von Kanaelen/Rollen, die nicht geprueft werden.
  exempt_channel_ids text not null default '',
  exempt_role_ids text not null default '',
  check_edits boolean not null default true,
  delete_message boolean not null default true,
  notify_user boolean not null default true,
  min_message_length integer not null default 1,
  updated_at timestamp with time zone null default now(),
  constraint word_filter_config_action_check
    check (default_action in ('delete', 'warn', 'timeout'))
) TABLESPACE pg_default;

-- Eintraege (Woerter / Zahlen / Regex) -------------------------------------
create table if not exists public.word_filter_entries (
  id bigserial primary key,
  server_id text not null,
  -- Das Muster genau so, wie der Moderator es eingegeben hat.
  pattern text not null,
  -- word | token | substring | regex
  match_type text not null default 'word',
  -- NULL = Standard-Aktion aus word_filter_config verwenden.
  action text null,
  severity integer not null default 1,
  -- Leetspeak / Homoglyphe / Zeichenwiederholungen / Trenner erkennen.
  fuzzy boolean not null default true,
  bypass_urls boolean not null default true,
  bypass_code boolean not null default true,
  note text null,
  enabled boolean not null default true,
  hit_count integer not null default 0,
  last_hit_at timestamp with time zone null,
  created_by text null,
  created_at timestamp with time zone null default now(),
  updated_at timestamp with time zone null default now(),
  constraint word_filter_entries_match_type_check
    check (match_type in ('word', 'token', 'substring', 'regex')),
  constraint word_filter_entries_action_check
    check (action is null or action in ('delete', 'warn', 'timeout')),
  constraint word_filter_entries_unique unique (server_id, pattern)
) TABLESPACE pg_default;

create index if not exists idx_word_filter_entries_server
  on public.word_filter_entries using btree (server_id, enabled);

-- Whitelist gegen Fehlalarme -----------------------------------------------
create table if not exists public.word_filter_allow (
  id bigserial primary key,
  server_id text not null,
  pattern text not null,
  created_by text null,
  created_at timestamp with time zone null default now(),
  constraint word_filter_allow_unique unique (server_id, pattern)
) TABLESPACE pg_default;

create index if not exists idx_word_filter_allow_server
  on public.word_filter_allow using btree (server_id);

-- Sicher fuer bestehende Installationen erneut ausfuehrbar: ---------------
alter table public.word_filter_config
  add column if not exists min_message_length integer not null default 1;
alter table public.word_filter_config
  add column if not exists notify_user boolean not null default true;
alter table public.word_filter_entries
  add column if not exists last_hit_at timestamp with time zone null;
alter table public.word_filter_entries
  add column if not exists severity integer not null default 1;
alter table public.word_filter_entries
  add column if not exists fuzzy boolean not null default true;
alter table public.word_filter_entries
  add column if not exists bypass_urls boolean not null default true;
alter table public.word_filter_entries
  add column if not exists bypass_code boolean not null default true;

-- ============================================================================
-- Beispiele (optional, nur fuer den eigenen Server anpassen)
-- ============================================================================
-- Einzelne Zahl: trifft "123", aber NICHT "1234", "4123" oder "abc123".
-- insert into public.word_filter_entries (server_id, pattern, match_type, action)
-- values ('<SERVER_ID>', '123', 'token', 'ban');
--
-- Wortstamm (bewusst unscharf, trifft z. B. auch "badsomething"):
-- insert into public.word_filter_entries (server_id, pattern, match_type)
-- values ('<SERVER_ID>', 'bad', 'substring');
--
-- Regulaerer Ausdruck:
-- insert into public.word_filter_entries (server_id, pattern, match_type)
-- values ('<SERVER_ID>', '\bd[a4@]+mn\b', 'regex');

UPDATE word_filter_entries SET action = NULL WHERE action IN ('ban','kick');
UPDATE word_filter_config SET default_action = 'timeout' WHERE default_action IN ('ban','kick');
