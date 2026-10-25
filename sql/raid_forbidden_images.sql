CREATE TABLE IF NOT EXISTS public.raid_forbidden_images (
    sha1       TEXT PRIMARY KEY,
    phash      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_raid_forbidden_images_created
    ON public.raid_forbidden_images (created_at);