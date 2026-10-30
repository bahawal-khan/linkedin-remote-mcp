-- OPTIONAL hardening for the public pages app (pages/). Run once, as the database owner.
--
-- The pages app only needs to: read approval/upload tickets, read draft text and image previews,
-- insert images, and mark an approval. This role gets exactly that and nothing else, so even if the
-- Vercel environment variable leaked, nobody could read the LinkedIn token or other tables.
--
-- 1. Change the password below, run the script, and use the new role's connection string
--    (same host and database, new user and password) as DATABASE_URL in Vercel.
-- 2. Run the MCP server once first so that all tables exist.

CREATE ROLE linkedin_pages LOGIN PASSWORD 'CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD';

GRANT USAGE ON SCHEMA public TO linkedin_pages;

GRANT SELECT (id, content, status)                         ON drafts           TO linkedin_pages;
GRANT SELECT (draft_id, image_id, position)                ON draft_images     TO linkedin_pages;
GRANT SELECT (id, thumb)                                   ON images           TO linkedin_pages;
GRANT INSERT (mime, data, size_bytes, thumb)               ON images           TO linkedin_pages;

GRANT SELECT (token_hash, expires_at, used_at, uses_left)  ON upload_tickets   TO linkedin_pages;
GRANT UPDATE (uses_left, used_at)                          ON upload_tickets   TO linkedin_pages;

GRANT SELECT (token_hash, draft_id, action, fingerprint, expires_at, used_at,
              approved_at, visibility, dry_run)            ON approval_tickets TO linkedin_pages;
GRANT UPDATE (approved_at)                                 ON approval_tickets TO linkedin_pages;
