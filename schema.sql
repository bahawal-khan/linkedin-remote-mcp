-- Safe to run many times. The MCP server runs it at startup.

CREATE TABLE IF NOT EXISTS images (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mime TEXT NOT NULL CHECK (mime IN ('image/png', 'image/jpeg')),
    data BYTEA NOT NULL,
    size_bytes INT NOT NULL,
    thumb BYTEA,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE images ADD COLUMN IF NOT EXISTS thumb BYTEA;

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

-- One row only: the encrypted LinkedIn access token (written by auth.py).
CREATE TABLE IF NOT EXISTS linkedin_token (
    id INT PRIMARY KEY CHECK (id = 1),
    access_token_enc TEXT NOT NULL,
    person_urn TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One-time links for uploading images. uses_left lets one link upload several images.
CREATE TABLE IF NOT EXISTS upload_tickets (
    token_hash TEXT PRIMARY KEY,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    uses_left INT NOT NULL DEFAULT 1
);
ALTER TABLE upload_tickets ADD COLUMN IF NOT EXISTS uses_left INT NOT NULL DEFAULT 1;

-- One-time links where the user approves a publish / delete.
-- approved_at is set by the pages app; the MCP server then finishes the job.
CREATE TABLE IF NOT EXISTS approval_tickets (
    token_hash TEXT PRIMARY KEY,
    draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('publish', 'delete')),
    fingerprint TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    approved_at TIMESTAMPTZ,
    visibility TEXT NOT NULL DEFAULT 'PUBLIC',
    dry_run BOOLEAN NOT NULL DEFAULT false
);
ALTER TABLE approval_tickets ADD COLUMN IF NOT EXISTS approved_at TIMESTAMPTZ;
ALTER TABLE approval_tickets ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'PUBLIC';
ALTER TABLE approval_tickets ADD COLUMN IF NOT EXISTS dry_run BOOLEAN NOT NULL DEFAULT false;
