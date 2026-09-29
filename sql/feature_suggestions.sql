-- Feature Suggestions Tabelle
CREATE TABLE IF NOT EXISTS feature_suggestions (
    id              BIGSERIAL PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'proposed',
    priority        TEXT NOT NULL DEFAULT 'normal',
    creator_id      TEXT NOT NULL,
    creator_name    TEXT,
    is_mbl_created  BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Kommentare / Verlauf
CREATE TABLE IF NOT EXISTS feature_suggestion_comments (
    id              BIGSERIAL PRIMARY KEY,
    suggestion_id   BIGINT NOT NULL REFERENCES feature_suggestions(id) ON DELETE CASCADE,
    author_id       TEXT NOT NULL,
    author_name     TEXT,
    author_is_mbl   BOOLEAN DEFAULT FALSE,
    content         TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Web-Token für User-Links
CREATE TABLE IF NOT EXISTS feature_suggestion_tokens (
    token           TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    suggestion_id   BIGINT,
    used            BOOLEAN DEFAULT FALSE,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_fs_status     ON feature_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_fs_creator    ON feature_suggestions(creator_id);
CREATE INDEX IF NOT EXISTS idx_fsc_sugg      ON feature_suggestion_comments(suggestion_id);
CREATE INDEX IF NOT EXISTS idx_fst_user      ON feature_suggestion_tokens(user_id);

-- Status: proposed | accepted | in_progress | done | rejected
-- Priority: important | normal | can_wait | bug