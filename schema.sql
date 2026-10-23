CREATE TABLE IF NOT EXISTS images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mime TEXT NOT NULL CHECK (mime IN ('image/png', 'image/jpeg')),
    data BYTEA NOT NULL,
    size_bytes INT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    content TEXT NOT NULL CHECK (char_length(content) <= 3000),
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'publishing', 'published', 'failed', 'deleted')),
    linkedin_post_urn TEXT,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS draft_images (
    draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    image_id UUID NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    position INT NOT NULL,
    alt_text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (draft_id, position)
);

-- Purani single-image drafts ko naye table me le jao, phir purane columns hata do
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'drafts' AND column_name = 'image_id'
    ) THEN
        INSERT INTO draft_images (draft_id, image_id, position, alt_text)
        SELECT id, image_id, 0, alt_text FROM drafts WHERE image_id IS NOT NULL
        ON CONFLICT DO NOTHING;
        ALTER TABLE drafts DROP COLUMN image_id;
        ALTER TABLE drafts DROP COLUMN alt_text;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS linkedin_token (
    id INT PRIMARY KEY CHECK (id = 1),
    access_token_enc TEXT NOT NULL,
    person_urn TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS upload_tickets (
    token_hash TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS approval_tickets (
    token_hash TEXT PRIMARY KEY,
    draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('publish', 'delete')),
    fingerprint TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);
