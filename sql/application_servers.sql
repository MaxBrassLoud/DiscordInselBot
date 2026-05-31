create table public.application_servers (
  server_id text not null,
  panel_channel_id text null,
  panel_message_id text null,
  category_id text null,
  newbie_role_id text null,
  member_role_id text null,
  log_channel_id text null,
  staff_role_ids text null default ''::text,
  welcome_message text null,
  instruction_message text null,
  app_counter integer null default 0,
  rejection_cooldown_hours integer null default 24,
  web_admin_role_ids text null default ''::text,
  mc_log_channel_id text null,
  panel_message text null,
  constraint application_servers_pkey primary key (server_id)
) TABLESPACE pg_default;

ALTER TABLE application_servers ADD COLUMN acceptance_message TEXT;