"""The two actions that touch LinkedIn: publish a draft and delete a published post.

They are only called after the user approved (approval page or finish_* tools).
"""
import asyncio
import hashlib

import db
import linkedin_client

MAX_POST_IMAGES = 9


async def fingerprint(draft_id) -> str | None:
    """Hash of the draft text + image IDs. If the draft changes after the user approved, the approval is void.

    pages/api/index.py computes the same value; keep both in sync."""
    d = await db.get_draft(draft_id)
    if not d:
        return None
    imgs = await db.get_draft_images(draft_id)
    raw = d["content"] + "|" + ",".join(str(i["image_id"]) for i in imgs)
    return hashlib.sha256(raw.encode()).hexdigest()


async def do_publish(draft_id, expected_fingerprint: str | None = None) -> str:
    if linkedin_client.DRY_RUN:
        return "DRY RUN is on: nothing was sent to LinkedIn. Set DRY_RUN=0 on the server for live posting."
    d = await db.claim_for_publish(draft_id)
    if not d:
        return "The draft cannot be published (not found, already published, or another request is running)."
    # The draft is locked now ('publishing' cannot be edited). Check once more that it is exactly
    # what the user approved, so an edit that slipped in between the check and the claim cannot go out.
    if expected_fingerprint is not None and await fingerprint(draft_id) != expected_fingerprint:
        await db.release_claim(draft_id)
        return "The draft changed after approval, nothing was published. Create a new approval link."
    try:
        rows = await db.get_draft_images_data(draft_id)
        images = [(bytes(r["data"]), r["alt_text"]) for r in rows]
        if len(images) > MAX_POST_IMAGES:
            raise linkedin_client.LinkedInError(f"A post cannot have more than {MAX_POST_IMAGES} images.")
        urn = await linkedin_client.publish_post(d["content"], images)
    except linkedin_client.LinkedInError as e:
        await db.mark_failed(draft_id, str(e))
        return f"Publish failed: {e}"
    except Exception:
        await db.mark_failed(draft_id, "unexpected error")
        return "Publish failed: unexpected error."
    # The post exists now. Try 3 times to record it, otherwise the draft would stay in 'publishing'.
    for _ in range(3):
        try:
            await db.mark_published(draft_id, urn)
            break
        except Exception:
            await asyncio.sleep(2)
    else:
        return f"The post was created on LinkedIn ({urn}) but the database was not updated. Do NOT publish again."
    await db.cleanup_images()
    return f"Published. LinkedIn ID: {urn}"


async def do_delete_post(draft_id) -> str:
    d = await db.get_draft(draft_id)
    if not d or d["status"] != "published" or not d["linkedin_post_urn"]:
        return "This draft is not a published post."
    if linkedin_client.DRY_RUN:
        return "DRY RUN is on: nothing was deleted from LinkedIn."
    try:
        await linkedin_client.delete_post(d["linkedin_post_urn"])
    except linkedin_client.LinkedInError as e:
        return f"Could not delete the post from LinkedIn: {e}"
    await db.mark_deleted(draft_id)
    return "The post was removed from LinkedIn."


async def finish(draft_id, action: str) -> str:
    """Split mode: complete a publish/delete after the user approved it on the pages app."""
    row = await db.consume_approved(draft_id, action)
    if not row:
        return (
            "No fresh approval found. Ask the user to open the approval link and tap the button "
            "(an approval is valid for 10 minutes), then try again. If the link expired, create a new one."
        )
    if await fingerprint(draft_id) != row["fingerprint"]:
        return "The draft changed after the link was created, so the approval was discarded. Create a new approval link."
    # An approval made while DRY_RUN was on must never turn into a live post if DRY_RUN is switched off
    # afterwards. A visibility change (e.g. CONNECTIONS -> PUBLIC) voids the approval for the same reason.
    if not linkedin_client.DRY_RUN and (
        row["dry_run"] or (action == "publish" and row["visibility"] != linkedin_client.VISIBILITY)
    ):
        return (
            "This approval was made under different settings (dry run or visibility) than the server "
            "has now, so it was discarded. Create a new approval link."
        )
    if action == "publish":
        return await do_publish(draft_id, row["fingerprint"])
    return await do_delete_post(draft_id)
