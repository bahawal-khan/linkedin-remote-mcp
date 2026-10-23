import asyncio
import hashlib
import os
import secrets
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

MAX_IMAGES = 30  # max images stored in the database at one time


async def _connect() -> psycopg.AsyncConnection:
    """A fresh connection for every query (Neon free scales to zero, so old connections die).
    Errors never include the connection string (it contains the password)."""
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set.")
    last = None
    for attempt in range(3):
        try:
            return await psycopg.AsyncConnection.connect(
                dsn,
                row_factory=dict_row,
                prepare_threshold=None,
                connect_timeout=20,
            )
        except psycopg.OperationalError as e:
            last = e
            await asyncio.sleep(2 * (attempt + 1))
        except psycopg.Error:
            raise RuntimeError("Database URL is invalid. Check DATABASE_URL (e.g. ...?sslmode=require).") from None
    raise RuntimeError(f"Could not connect to the database ({type(last).__name__}).") from None


async def _exec(sql, params, mode):
    conn = await _connect()
    async with conn:  # commit and close at the end of the block
        cur = await conn.execute(sql, params)
        if mode == "one":
            return await cur.fetchone()
        if mode == "all":
            return await cur.fetchall()
        return cur.rowcount


async def _one(sql, params=()):
    return await _exec(sql, params, "one")


async def _all(sql, params=()):
    return await _exec(sql, params, "all")


async def _count(sql, params=()) -> int:
    return await _exec(sql, params, "count")


async def init_db() -> None:
    schema = (BASE / "schema.sql").read_text(encoding="utf-8")
    conn = await _connect()
    async with conn:
        await conn.execute(schema)


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


# ---------- images ----------
async def save_image(mime: str, data: bytes):
    row = await _one(
        "INSERT INTO images (mime, data, size_bytes) VALUES (%s, %s, %s) RETURNING id",
        (mime, data, len(data)),
    )
    return row["id"]


async def get_image(image_id):
    return await _one("SELECT * FROM images WHERE id = %s", (image_id,))


async def image_exists(image_id) -> bool:
    return await _one("SELECT 1 AS ok FROM images WHERE id = %s", (image_id,)) is not None


async def latest_image_ids(n: int) -> list:
    """IDs of the newest n images, ordered oldest to newest."""
    rows = await _all("SELECT id FROM images ORDER BY created_at DESC LIMIT %s", (n,))
    return [r["id"] for r in reversed(rows)]


async def count_images() -> int:
    return (await _one("SELECT count(*) AS n FROM images"))["n"]


async def list_images() -> list:
    return await _all(
        "SELECT i.id, i.mime, i.size_bytes, i.created_at, "
        "EXISTS (SELECT 1 FROM draft_images di WHERE di.image_id = i.id) AS attached "
        "FROM images i ORDER BY i.created_at DESC"
    )


async def cleanup_images() -> None:
    """Remove images not attached to any draft (and older than 1 hour)."""
    await _count(
        "DELETE FROM images i WHERE i.created_at < now() - interval '1 hour' "
        "AND NOT EXISTS (SELECT 1 FROM draft_images di WHERE di.image_id = i.id)"
    )


# ---------- drafts ----------
_DRAFT_SELECT = (
    "SELECT d.*, (SELECT count(*) FROM draft_images di WHERE di.draft_id = d.id) AS image_count "
    "FROM drafts d"
)


async def count_drafts() -> int:
    """Only active drafts are counted (published/deleted history does not count toward the limit)."""
    return (await _one("SELECT count(*) AS n FROM drafts WHERE status IN ('draft', 'failed')"))["n"]


async def create_draft(content: str, items: list) -> dict:
    """items: [(image_id, alt_text), ...]. Draft and images in one transaction."""
    conn = await _connect()
    async with conn:
        cur = await conn.execute(
            "INSERT INTO drafts (content) VALUES (%s) RETURNING *", (content,)
        )
        d = await cur.fetchone()
        for pos, (image_id, alt) in enumerate(items):
            await conn.execute(
                "INSERT INTO draft_images (draft_id, image_id, position, alt_text) "
                "VALUES (%s, %s, %s, %s)",
                (d["id"], image_id, pos, alt),
            )
        return d


async def list_drafts(status: str) -> list:
    return await _all(
        _DRAFT_SELECT + " WHERE d.status = %s ORDER BY d.created_at DESC", (status,)
    )


async def get_draft(draft_id):
    return await _one(_DRAFT_SELECT + " WHERE d.id = %s", (draft_id,))


async def get_draft_images(draft_id) -> list:
    return await _all(
        "SELECT di.image_id, di.position, di.alt_text, i.mime, i.size_bytes "
        "FROM draft_images di JOIN images i ON i.id = di.image_id "
        "WHERE di.draft_id = %s ORDER BY di.position",
        (draft_id,),
    )


async def get_draft_images_data(draft_id) -> list:
    return await _all(
        "SELECT di.position, di.alt_text, i.mime, i.data "
        "FROM draft_images di JOIN images i ON i.id = di.image_id "
        "WHERE di.draft_id = %s ORDER BY di.position",
        (draft_id,),
    )


async def update_draft(draft_id, content: str, items=None):
    """If items is None the images stay as they are, otherwise the whole list is replaced."""
    conn = await _connect()
    async with conn:
        cur = await conn.execute(
            "UPDATE drafts SET content = %s, status = 'draft', error = NULL, updated_at = now() "
            "WHERE id = %s AND status IN ('draft', 'failed') RETURNING id",
            (content, draft_id),
        )
        row = await cur.fetchone()
        if not row:
            return None
        if items is not None:
            await conn.execute("DELETE FROM draft_images WHERE draft_id = %s", (draft_id,))
            for pos, (image_id, alt) in enumerate(items):
                await conn.execute(
                    "INSERT INTO draft_images (draft_id, image_id, position, alt_text) "
                    "VALUES (%s, %s, %s, %s)",
                    (draft_id, image_id, pos, alt),
                )
        return row


async def delete_draft(draft_id) -> bool:
    n = await _count(
        "DELETE FROM drafts WHERE id = %s AND status IN ('draft', 'failed', 'deleted')",
        (draft_id,),
    )
    return n > 0


async def claim_for_publish(draft_id):
    """Atomic: only one request wins, so no double posting.
    A 'publishing' draft stuck for over 10 minutes (server crash) can be retried."""
    return await _one(
        "UPDATE drafts SET status = 'publishing', updated_at = now() "
        "WHERE id = %s AND (status IN ('draft', 'failed') "
        "OR (status = 'publishing' AND updated_at < now() - interval '10 minutes')) RETURNING *",
        (draft_id,),
    )


async def mark_published(draft_id, post_urn: str) -> None:
    conn = await _connect()
    async with conn:
        await conn.execute(
            "UPDATE drafts SET status = 'published', linkedin_post_urn = %s, "
            "published_at = now(), updated_at = now() WHERE id = %s",
            (post_urn, draft_id),
        )
        # Post is live, the database no longer needs the images (saves space)
        await conn.execute("DELETE FROM draft_images WHERE draft_id = %s", (draft_id,))


async def mark_failed(draft_id, error: str) -> None:
    await _count(
        "UPDATE drafts SET status = 'failed', error = %s, updated_at = now() WHERE id = %s",
        (error[:500], draft_id),
    )


async def mark_deleted(draft_id) -> None:
    await _count(
        "UPDATE drafts SET status = 'deleted', updated_at = now() WHERE id = %s", (draft_id,)
    )


# ---------- token ----------
async def get_token():
    return await _one("SELECT * FROM linkedin_token WHERE id = 1")


# ---------- upload tickets ----------
async def create_upload_ticket(minutes: int = 15) -> str:
    token = secrets.token_urlsafe(32)
    await _count(
        "INSERT INTO upload_tickets (token_hash, expires_at) "
        "VALUES (%s, now() + make_interval(mins => %s))",
        (_h(token), minutes),
    )
    return token


async def peek_upload_ticket(token: str):
    return await _one(
        "SELECT 1 AS ok FROM upload_tickets WHERE token_hash = %s "
        "AND used_at IS NULL AND expires_at > now()",
        (_h(token),),
    )


async def consume_upload_ticket(token: str):
    return await _one(
        "UPDATE upload_tickets SET used_at = now() WHERE token_hash = %s "
        "AND used_at IS NULL AND expires_at > now() RETURNING 1 AS ok",
        (_h(token),),
    )


# ---------- approval tickets (user's approval) ----------
async def create_approval(draft_id, action: str, fingerprint: str, minutes: int = 15) -> str:
    token = secrets.token_urlsafe(32)
    await _count(
        "INSERT INTO approval_tickets (token_hash, draft_id, action, fingerprint, expires_at) "
        "VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))",
        (_h(token), draft_id, action, fingerprint, minutes),
    )
    return token


async def peek_approval(token: str):
    return await _one(
        "SELECT draft_id, action, fingerprint FROM approval_tickets WHERE token_hash = %s "
        "AND used_at IS NULL AND expires_at > now()",
        (_h(token),),
    )


async def consume_approval(token: str):
    return await _one(
        "UPDATE approval_tickets SET used_at = now() WHERE token_hash = %s "
        "AND used_at IS NULL AND expires_at > now() RETURNING draft_id, action, fingerprint",
        (_h(token),),
    )