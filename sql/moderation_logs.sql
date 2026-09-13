create table if not exists public.moderation_logs (
  id bigserial not null,
  server_id text not null,
  action text not null,
  target_id text not null,
  target_name text null,
  moderator_id text null,
  moderator_name text null,
  reason text null,
  duration_seconds integer null,
  until timestamp with time zone null,
  -- Discord-Audit-Log-Snowflake.  Sie macht den stündlichen Import idempotent.
  audit_log_id text null,
  audit_action text null,
  audit_details jsonb null,
  source text not null default 'bot',
  imported_at timestamp with time zone null,
  created_at timestamp with time zone null default now(),
  constraint moderation_logs_pkey primary key (id)
) TABLESPACE pg_default;

create index IF not exists idx_moderation_logs_server on public.moderation_logs using btree (server_id, created_at desc) TABLESPACE pg_default;

create index IF not exists idx_moderation_logs_target on public.moderation_logs using btree (server_id, target_id, created_at desc) TABLESPACE pg_default;

-- Sicher für bestehende Installationen erneut ausführbar:
alter table public.moderation_logs add column if not exists audit_log_id text null;
alter table public.moderation_logs add column if not exists audit_action text null;
alter table public.moderation_logs add column if not exists audit_details jsonb null;
alter table public.moderation_logs add column if not exists source text not null default 'bot';
alter table public.moderation_logs add column if not exists imported_at timestamp with time zone null;
create unique index if not exists uq_moderation_logs_audit_log_id
  on public.moderation_logs (audit_log_id);
create index if not exists idx_moderation_logs_audit_action
  on public.moderation_logs (server_id, audit_action, created_at desc);
