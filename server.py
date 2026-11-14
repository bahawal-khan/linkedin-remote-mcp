import asyncio
import logging
import os
import re
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

# Required for async psycopg on Windows (no effect on Linux/Horizon)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

log = logging.getLogger("linkedin-remote")

# BASE_URL: public URL of this MCP server (only needed for the optional Google login).
# PAGES_URL: set this when the browser pages (image upload, approval) are hosted separately,
# e.g. on Vercel (see pages/). Needed when the MCP host protects every URL with a login (like Horizon).
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").strip().strip("\"'").rstrip("/")
PAGES_URL = os.getenv("PAGES_URL", "").rstrip("/")
SPLIT = bool(PAGES_URL)
LINK_BASE = PAGES_URL if SPLIT else BASE_URL
MAX_UPLOAD_FILES = 20

# Optional Google login. Not needed if the host (Horizon) authentication is ON.
AUTH = None
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").strip().lower()
_google_id = os.getenv("GOOGLE_CLIENT_ID", "").strip(" #\"'")  # "#" or quotes alone count as empty
if _google_id:
    from fastmcp.server.auth.providers.google import GoogleProvider

    if not OWNER_EMAIL:
        raise SystemExit("OWNER_EMAIL must be set when Google login is enabled.")
    AUTH = GoogleProvider(
        client_id=_google_id,
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        base_url=BASE_URL,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
    )
elif not BASE_URL.startswith("http://localhost"):
    log.warning("The server has no login of its own. Host authentication must be ON!")

INSTRUCTIONS = (
    "Manage LinkedIn post drafts for the user. Drafts, edits and image uploads are safe. "
    "Nothing is posted by you: publish_draft / delete_linkedin_post only create an approval link that the "
    "USER must open in a browser and tap. Never say a post is published or deleted until the final tool "
    "result says so. Show the user the exact text before asking for approval.\n\n"
    "WRITING STYLE for post text (LinkedIn does not render Markdown, so never use ** or # headings): "
    "plain text only. Start with a short hook line, then short paragraphs separated by blank lines "
    "(one idea each). Use line breaks and, if it helps, simple '- ' or numbered lines. Optional emoji, "
    "sparingly. End with a question or call to action, then 3-5 relevant hashtags on the last line "
    "(#Hashtag, no spaces inside a tag); they become real hashtags. Tagging a person needs their LinkedIn "
    "ID, written @[Full Name](urn:li:person:ID); if the user has no ID, write the name as plain text and "
    "tell them mentions cannot be added from here. Keep it under 3000 characters."
)
mcp = FastMCP("linkedin-remote", instructions=INSTRUCTIONS, auth=AUTH, mask_error_details=True)
if not SPLIT:
    import web  # same-host mode: this server also serves /upload and /approve

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
        await db.purge_old_tickets()
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
    if "**" in text or re.search(r"(?m)^#{1,6}\s", text):
        raise ToolError(
            "LinkedIn does not render Markdown. Remove ** and '# ' headings and write plain text "
            "(hashtags like #AI at the end are fine)."
        )
    if len(linkedin_client.format_commentary(text)) > linkedin_client.MAX_COMMENTARY:
        raise ToolError("After LinkedIn formatting the text is over 3000 characters. Shorten it a little.")
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
    t = await db.create_upload_ticket(15, MAX_UPLOAD_FILES if SPLIT else 1)
    return (
        f"Open this link in your browser and choose your images (up to {MAX_UPLOAD_FILES} at a time, "
        "valid for 15 minutes, do not share it):\n"
        f"{LINK_BASE}/upload?t={t}\n\nTell me when the upload is done."
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

    if upload_images and linkedin_client.DRY_RUN:
        out.append("SKIP image upload test: DRY_RUN is on, so nothing is sent to LinkedIn")
    elif upload_images:
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
    """Create an approval link to publish a draft to LinkedIn (with 0, 1 or several images). This does NOT publish by itself. Give the link to the user; the post goes out only after the user opens it and approves."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] not in ("draft", "failed", "publishing"):
        return "Draft cannot be published (not found, or already published)."
    token = await db.create_approval(
        did, "publish", await ops.fingerprint(did), 15, linkedin_client.VISIBILITY, linkedin_client.DRY_RUN
    )
    head = (
        f"Type: {_kind(d['image_count'])}, visibility {linkedin_client.VISIBILITY}. "
        f"DRY_RUN: {'ON' if linkedin_client.DRY_RUN else 'OFF'}.\n---\n{d['content']}\n---\n"
        f"Give the user this link (valid 15 minutes):\n{LINK_BASE}/approve?t={token}\n"
    )
    if SPLIT:
        return head + (
            "The user must open it and tap 'Approve publishing'. You cannot publish by yourself. "
            "When the user says they approved, call finish_publish with this draft ID."
        )
    return head + (
        "The post is published only when the user opens it and taps 'Yes, publish'. "
        "You cannot publish by yourself. Ask the user to tap the button, then tell you."
    )


@mcp.tool
async def delete_linkedin_post(draft_id: str) -> str:
    """Create an approval link to delete a published post from LinkedIn. This does NOT delete by itself; the post is removed only after the user opens the link and approves."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] != "published" or not d["linkedin_post_urn"]:
        return "This draft is not a published post."
    token = await db.create_approval(
        did, "delete", await ops.fingerprint(did), 15, linkedin_client.VISIBILITY, linkedin_client.DRY_RUN
    )
    head = (
        f"This post will be removed from LinkedIn: {d['linkedin_post_urn']}\n---\n{d['content']}\n---\n"
        f"Give the user this link (valid 15 minutes):\n{LINK_BASE}/approve?t={token}\n"
    )
    if SPLIT:
        return head + (
            "The user must open it and tap 'Approve removal'. When the user says they approved, "
            "call finish_delete_post with this draft ID."
        )
    return head + "The post is deleted only when the user opens the link and taps 'Yes, delete'."


@mcp.tool
async def finish_publish(draft_id: str) -> str:
    """Complete a publish AFTER the user approved it in the browser (link from publish_draft, valid 10 minutes after approval). Call this only when the user says they approved. Without a fresh approval nothing happens."""
    await guard()
    return await ops.finish(_uuid(draft_id), "publish")


@mcp.tool
async def finish_delete_post(draft_id: str) -> str:
    """Complete the removal of a LinkedIn post AFTER the user approved it in the browser (link from delete_linkedin_post). Call this only when the user says they approved. Without a fresh approval nothing happens."""
    await guard()
    return await ops.finish(_uuid(draft_id), "delete")


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
    mcp.run(transport="http", host="127.0.0.1", port=8000)
