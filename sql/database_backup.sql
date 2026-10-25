CREATE TABLE IF NOT EXISTS public.database_backups (
    id           BIGSERIAL PRIMARY KEY,
    filename     TEXT NOT NULL UNIQUE,
    content      BYTEA NOT NULL,
    table_counts JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_rows   INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_database_backups_created
    ON public.database_backups (created_at DESC);