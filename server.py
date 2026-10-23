import asyncio
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_access_token

load_dotenv(Path(__file__).parent / ".env")

import db  # noqa: E402
import linkedin_client  # noqa: E402
import ops  # noqa: E402
import web  # noqa: E402

# Required for async psycopg on Windows (no effect on Linux/Horizon)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

log = logging.getLogger("linkedin-remote")

# Optional Google login. Not needed if the host (Horizon) authentication is ON.
AUTH = None
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").strip().lower()
if os.getenv("GOOGLE_CLIENT_ID"):
    from fastmcp.server.auth.providers.google import GoogleProvider

    if not OWNER_EMAIL:
        raise SystemExit("OWNER_EMAIL must be set when Google login is enabled.")
    AUTH = GoogleProvider(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        base_url=web.BASE_URL,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
    )
elif not web.BASE_URL.startswith("http://localhost"):
    log.warning("The server has no login of its own. Host authentication must be ON!")

mcp = FastMCP("linkedin-remote", auth=AUTH, mask_error_details=True)
web.register(mcp)

MAX_TEXT = 3000
MAX_DRAFTS = 100
MAX_POST_IMAGES = ops.MAX_POST_IMAGES
READ_ONLY = {"readOnlyHint": True}
STATUSES = {"draft", "publishing", "published", "failed", "deleted"}
_ready = False


async def guard() -> None:
    global _ready
    if not _ready:
        await db.init_db()
        _ready = True
    if AUTH is not None:
        token = get_access_token()
        email = ((token.claims or {}).get("email") or "").lower() if token else ""
        if email != OWNER_EMAIL:
            raise ToolError("Access denied.")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise ToolError("Invalid ID. Copy the full ID from list_drafts / list_images.")


def _text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ToolError("Text is empty.")
    if len(text) > MAX_TEXT:
        raise ToolError(f"Text is longer than {MAX_TEXT} characters.")
    return text


async def _resolve_images(refs) -> list:
    """Each ref: an image ID, 'latest' (newest 1) or 'latest:N' (newest N, oldest first)."""
    ids = []
    for ref in refs or []:
        ref = ref.strip()
        if not ref:
            continue
        low = ref.lower()
        if low == "latest" or low.startswith("latest:"):
            n = 1
            if ":" in low:
                try:
                    n = int(low.split(":", 1)[1])
                except ValueError:
                    raise ToolError("In latest:N, N must be a number (e.g. latest:3).")
            if n < 1 or n > MAX_POST_IMAGES:
                raise ToolError(f"In latest:N, N must be between 1 and {MAX_POST_IMAGES}.")
            found = await db.latest_image_ids(n)
            if len(found) < n:
                raise ToolError(
                    f"Only {len(found)} uploaded image(s) found, {n} needed. Run upload_image_link first."
                )
            ids.extend(str(i) for i in found)
        else:
            iid = _uuid(ref)
            if not await db.image_exists(iid):
                raise ToolError("Image not found.")
            ids.append(iid)
    unique = list(dict.fromkeys(ids))
    if len(unique) > MAX_POST_IMAGES:
        raise ToolError(f"A post cannot have more than {MAX_POST_IMAGES} images.")
    return unique


def _items(ids: list, alts) -> list:
    alts = alts or []
    return [(i, alts[k].strip()[:300] if k < len(alts) else "") for k, i in enumerate(ids)]


def _kind(n: int) -> str:
    if n == 0:
        return "text only"
    if n == 1:
        return "text + 1 image"
    return f"text + {n} images (multi-image post)"


@mcp.tool
async def upload_image_link() -> str:
    """Get a one-time link for uploading images. Open it in a browser (works on phones too, several images at once)."""
    await guard()
    t = await db.create_upload_ticket(15)
    return (
        f"Open this link in your browser and choose your images (up to {web.MAX_FILES} at a time, "
        "valid for 15 minutes, works only once, do not share it):\n"
        f"{web.BASE_URL}/upload?t={t}\n\nTell me when the upload is done."
    )


@mcp.tool(annotations=READ_ONLY)
async def list_images() -> str:
    """List uploaded images (ID, size, and whether they are attached to a draft)."""
    await guard()
    rows = await db.list_images()
    if not rows:
        return "No uploaded images."
    return "\n".join(
        f"{r['id']}  {r['mime']}  {r['size_bytes'] // 1024} KB  "
        f"{r['created_at']:%Y-%m-%d %H:%M} UTC  {'attached to a draft' if r['attached'] else 'free'}"
        for r in rows
    )


@mcp.tool
async def create_draft(
    text: str, images: list[str] | None = None, alt_texts: list[str] | None = None
) -> str:
    """Save a draft. images: list of image IDs, or ['latest'] (newest 1) or ['latest:3'] (newest 3). alt_texts: optional, one per image. Does not publish."""
    await guard()
    text = _text(text)
    if await db.count_drafts() >= MAX_DRAFTS:
        raise ToolError("Draft limit reached. Delete some old drafts.")
    ids = await _resolve_images(images)
    d = await db.create_draft(text, _items(ids, alt_texts))
    return f"Draft saved ({_kind(len(ids))}). ID: {d['id']}"


@mcp.tool(annotations=READ_ONLY)
async def list_drafts(status: str = "draft") -> str:
    """List drafts. status: draft, publishing, published, failed or deleted."""
    await guard()
    if status not in STATUSES:
        raise ToolError("status must be draft, publishing, published, failed or deleted.")
    rows = await db.list_drafts(status)
    if not rows:
        return f"No {status} drafts found."
    out = []
    for r in rows:
        img = f"\n[images: {r['image_count']}]" if r["image_count"] else ""
        post = f"\n[LinkedIn: {r['linkedin_post_urn']}]" if r["linkedin_post_urn"] else ""
        err = f"\n[error: {r['error']}]" if r["error"] else ""
        out.append(f"ID: {r['id']} [{r['status']}]{img}{post}{err}\n{r['content']}")
    return "\n\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def read_draft(draft_id: str) -> str:
    """Read one draft in full (text, each image ID and alt text)."""
    await guard()
    d = await db.get_draft(_uuid(draft_id))
    if not d:
        return "Draft not found."
    lines = [f"ID: {d['id']} [{d['status']}]"]
    for im in await db.get_draft_images(d["id"]):
        alt = f", alt: {im['alt_text']}" if im["alt_text"] else ""
        lines.append(
            f"Image {im['position'] + 1}: {im['image_id']} ({im['mime']}, {im['size_bytes'] // 1024} KB{alt})"
        )
    if d["linkedin_post_urn"]:
        lines.append(f"LinkedIn: {d['linkedin_post_urn']}")
    if d["error"]:
        lines.append(f"Error: {d['error']}")
    lines.append("\n" + d["content"])
    return "\n".join(lines)


@mcp.tool
async def edit_draft(
    draft_id: str,
    text: str = "",
    images: list[str] | None = None,
    alt_texts: list[str] | None = None,
    remove_images: bool = False,
) -> str:
    """Edit a draft. To change the text, pass the full new text. Passing images replaces all old images with that list. remove_images=True removes all images. Passing only alt_texts updates the alt text of the current images."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] not in ("draft", "failed"):
        return "Draft not found or cannot be edited."
    new_text = _text(text) if text.strip() else d["content"]

    items = None  # None = keep images as they are
    if remove_images:
        items = []
    elif images:
        items = _items(await _resolve_images(images), alt_texts)
    elif alt_texts:
        current = await db.get_draft_images(did)
        items = [
            (str(im["image_id"]), alt_texts[k].strip()[:300] if k < len(alt_texts) else im["alt_text"])
            for k, im in enumerate(current)
        ]
    if not await db.update_draft(did, new_text, items):
        return "Draft was not updated."
    await db.cleanup_images()
    return "Draft updated."


@mcp.tool
async def delete_draft(draft_id: str) -> str:
    """Delete a draft from the database (a published post must be removed from LinkedIn first)."""
    await guard()
    ok = await db.delete_draft(_uuid(draft_id))
    await db.cleanup_images()
    return "Draft deleted." if ok else "Draft not found or cannot be deleted."


@mcp.tool
async def check_publish(draft_id: str, upload_images: bool = False) -> str:
    """Full pre-publish check. Does NOT post to LinkedIn. With upload_images=True the images are uploaded to LinkedIn to test the upload path (still no post is created)."""
    await guard()
    d = await db.get_draft(_uuid(draft_id))
    if not d:
        return "Draft not found."
    out = []
    ready = d["status"] in ("draft", "failed")
    out.append(f"{'OK  ' if ready else 'FAIL'} status '{d['status']}' (must be draft or failed to publish)")
    out.append(f"OK   text {len(d['content'])}/{MAX_TEXT} characters")

    rows = await db.get_draft_images_data(d["id"])
    if len(rows) > MAX_POST_IMAGES:
        out.append(f"FAIL {len(rows)} images, maximum is {MAX_POST_IMAGES}")
    elif rows:
        sizes = ", ".join(f"{r['mime']} {len(r['data']) // 1024} KB" for r in rows)
        out.append(f"OK   {len(rows)} image(s): {sizes}")
    else:
        out.append("INFO no images")

    try:
        owner = await linkedin_client.verify_token()
        out.append("OK   LinkedIn accepted the token (read only, nothing written)")
    except linkedin_client.LinkedInError as e:
        out.append(f"FAIL {e}")
        return "\n".join(out)

    if upload_images:
        if not rows:
            out.append("INFO no images in the draft to test uploading")
        else:
            try:
                urns = await linkedin_client.test_image_upload(
                    [(bytes(r["data"]), r["alt_text"]) for r in rows]
                )
                out.append(f"OK   upload path works for {len(urns)} image(s), no post created")
            except linkedin_client.LinkedInError as e:
                out.append(f"FAIL {e}")

    body = linkedin_client.build_body(owner, d["content"])
    out.append(
        f"\nPost type: {_kind(len(rows))}, visibility {body['visibility']}.\n"
        f"This is what would be sent:\n{body['commentary']}"
        f"\n\nDRY_RUN: {'ON' if linkedin_client.DRY_RUN else 'OFF'}. Nothing was posted to LinkedIn."
    )
    return "\n".join(out)


@mcp.tool
async def publish_draft(draft_id: str) -> str:
    """Create an approval link to publish a draft to LinkedIn (with 0, 1 or several images). This does NOT publish by itself. Give the link to the user; the post goes out only when the user opens it and taps the button."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] not in ("draft", "failed", "publishing"):
        return "Draft cannot be published (not found, or already published)."
    token = await db.create_approval(did, "publish", await ops.fingerprint(did), 15)
    return (
        f"Type: {_kind(d['image_count'])}, visibility {linkedin_client.VISIBILITY}. "
        f"DRY_RUN: {'ON' if linkedin_client.DRY_RUN else 'OFF'}.\n---\n{d['content']}\n---\n"
        "Give the user this link (valid 15 minutes, works once). The post is published only when the user "
        f"opens it and taps 'Yes, publish':\n{web.BASE_URL}/approve?t={token}\n"
        "You cannot publish by yourself. Ask the user to open the link and tap the button, then tell you."
    )


@mcp.tool
async def delete_linkedin_post(draft_id: str) -> str:
    """Create an approval link to delete a published post from LinkedIn. This does NOT delete by itself; the post is removed only when the user opens the link and taps the button."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] != "published" or not d["linkedin_post_urn"]:
        return "This draft is not a published post."
    token = await db.create_approval(did, "delete", await ops.fingerprint(did), 15)
    return (
        f"This post will be removed from LinkedIn: {d['linkedin_post_urn']}\n---\n{d['content']}\n---\n"
        "Give the user this link (valid 15 minutes, works once). The post is deleted only when the user "
        f"opens it and taps 'Yes, delete':\n{web.BASE_URL}/approve?t={token}"
    )


@mcp.tool(annotations=READ_ONLY)
async def linkedin_status() -> str:
    """Show whether LinkedIn is connected, when the token expires, and whether DRY_RUN is on."""
    await guard()
    mode = (
        "DRY RUN ON (nothing is sent to LinkedIn)"
        if linkedin_client.DRY_RUN
        else f"DRY RUN OFF (live posting, visibility {linkedin_client.VISIBILITY})"
    )
    tok = await db.get_token()
    if not tok:
        return f"{mode}. LinkedIn is not connected. Run `python auth.py`."
    left = (tok["expires_at"] - datetime.now(timezone.utc)).days
    if left < 0:
        return f"{mode}. Token has expired. Run `python auth.py` again."
    return f"{mode}. LinkedIn is connected, about {left} days left on the token."


if __name__ == "__main__":
    # Local: 127.0.0.1:8000. On hosts like Render, PORT is set and we listen on all interfaces.
    port = int(os.getenv("PORT", "8000"))
    host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    mcp.run(transport="http", host=host, port=port)