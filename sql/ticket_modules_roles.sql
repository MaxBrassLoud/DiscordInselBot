create table public.ticket_module_roles (
  id bigserial not null,
  module_id bigint not null,
  role_id text not null,
  constraint ticket_module_roles_pkey primary key (id),
  constraint ticket_module_roles_module_id_role_id_key unique (module_id, role_id),
  constraint ticket_module_roles_module_id_fkey foreign KEY (module_id) references ticket_modules (id) on delete CASCADE
) TABLESPACE pg_default;