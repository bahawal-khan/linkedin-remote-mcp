import asyncio
import hashlib

import db
import linkedin_client

MAX_POST_IMAGES = 9


async def fingerprint(draft_id) -> str | None:
    """Draft ka text + images ka hash. Manzoori ke baad draft badle to manzoori bekaar ho jati hai."""
    d = await db.get_draft(draft_id)
    if not d:
        return None
    imgs = await db.get_draft_images(draft_id)
    raw = d["content"] + "|" + ",".join(str(i["image_id"]) for i in imgs)
    return hashlib.sha256(raw.encode()).hexdigest()


async def do_publish(draft_id) -> str:
    if linkedin_client.DRY_RUN:
        return "DRY RUN on hai: LinkedIn par kuch nahi gaya. Asli posting ke liye server ke env me DRY_RUN=0 karen."
    d = await db.claim_for_publish(draft_id)
    if not d:
        return "Draft publish nahi ho sakta (nahi mila, pehle hi publish hai, ya koi aur request chal rahi hai)."
    try:
        rows = await db.get_draft_images_data(draft_id)
        images = [(bytes(r["data"]), r["alt_text"]) for r in rows]
        if len(images) > MAX_POST_IMAGES:
            raise linkedin_client.LinkedInError(f"{MAX_POST_IMAGES} se zyada images nahi ho sakti.")
        urn = await linkedin_client.publish_post(d["content"], images)
    except linkedin_client.LinkedInError as e:
        await db.mark_failed(draft_id, str(e))
        return f"Publish fail: {e}"
    except Exception:
        await db.mark_failed(draft_id, "unexpected error")
        return "Publish fail: unexpected error."
    # Post ban chuki hai: status save karne me 3 koshish (warna draft 'publishing' me atak jata)
    for attempt in range(3):
        try:
            await db.mark_published(draft_id, urn)
            break
        except Exception:
            await asyncio.sleep(2)
    else:
        return f"Post LinkedIn par ban gayi ({urn}) lekin database update nahi hua. Dobara publish MAT karen."
    await db.cleanup_images()
    return f"Publish ho gaya. LinkedIn ID: {urn}"


async def do_delete_post(draft_id) -> str:
    d = await db.get_draft(draft_id)
    if not d or d["status"] != "published" or not d["linkedin_post_urn"]:
        return "Yeh draft publish ki hui post nahi hai."
    if linkedin_client.DRY_RUN:
        return "DRY RUN on hai: LinkedIn se kuch delete nahi hua."
    try:
        await linkedin_client.delete_post(d["linkedin_post_urn"])
    except linkedin_client.LinkedInError as e:
        return f"LinkedIn se delete nahi hui: {e}"
    await db.mark_deleted(draft_id)
    return "Post LinkedIn se hata di gayi."
