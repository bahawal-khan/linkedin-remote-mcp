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

# Windows par psycopg async ke liye zaroori (Linux/Horizon par asar nahi)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

log = logging.getLogger("linkedin-remote")

# Optional Google login. Horizon par authentication on ho to iski zaroorat nahi.
AUTH = None
OWNER_EMAIL = os.getenv("OWNER_EMAIL", "").strip().lower()
if os.getenv("GOOGLE_CLIENT_ID"):
    from fastmcp.server.auth.providers.google import GoogleProvider

    if not OWNER_EMAIL:
        raise SystemExit("Google login on hai to OWNER_EMAIL zaroor set karen.")
    AUTH = GoogleProvider(
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        base_url=web.BASE_URL,
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
    )
elif not web.BASE_URL.startswith("http://localhost"):
    log.warning("Server me apni login nahi hai. Host ki authentication ON honi chahiye!")

mcp = FastMCP("linkedin-remote", auth=AUTH)
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
            raise ToolError("Access nahi hai.")


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value.strip()))
    except ValueError:
        raise ToolError("ID sahi nahi hai. list_drafts / list_images se poori ID copy karen.")


def _text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ToolError("Text khali hai.")
    if len(text) > MAX_TEXT:
        raise ToolError(f"Text {MAX_TEXT} characters se lamba hai.")
    return text


async def _resolve_images(refs) -> list:
    """Har ref: image ID, 'latest' (sab se nayi 1) ya 'latest:N' (sab se nayi N, purani pehle)."""
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
                    raise ToolError("latest:N me N number hona chahiye (jaise latest:3).")
            if n < 1 or n > MAX_POST_IMAGES:
                raise ToolError(f"latest:N me N 1 se {MAX_POST_IMAGES} ke beech hona chahiye.")
            found = await db.latest_image_ids(n)
            if len(found) < n:
                raise ToolError(
                    f"Sirf {len(found)} uploaded image mili, {n} chahiye. Pehle upload_image_link chalayen."
                )
            ids.extend(str(i) for i in found)
        else:
            iid = _uuid(ref)
            if not await db.image_exists(iid):
                raise ToolError("Yeh image nahi mili.")
            ids.append(iid)
    unique = list(dict.fromkeys(ids))
    if len(unique) > MAX_POST_IMAGES:
        raise ToolError(f"Ek post me {MAX_POST_IMAGES} se zyada images nahi.")
    return unique


def _items(ids: list, alts) -> list:
    alts = alts or []
    return [(i, alts[k].strip()[:300] if k < len(alts) else "") for k, i in enumerate(ids)]


def _kind(n: int) -> str:
    if n == 0:
        return "sirf text"
    if n == 1:
        return "text + 1 image"
    return f"text + {n} images (multi-image post)"


@mcp.tool
async def upload_image_link() -> str:
    """Images upload ka ek-baar ka link do (browser me kholna hai, phone par bhi chalta hai, kai images ek saath)."""
    await guard()
    t = await db.create_upload_ticket(15)
    return (
        f"Yeh link apne browser me kholen aur images chunen (ek baar me {web.MAX_FILES} tak, "
        "15 minute, sirf ek baar chalega, share na karen):\n"
        f"{web.BASE_URL}/upload?t={t}\n\nUpload ke baad mujhe batayen."
    )


@mcp.tool(annotations=READ_ONLY)
async def list_images() -> str:
    """Uploaded images ki list (ID, size, kisi draft se jurti hai ya nahi)."""
    await guard()
    rows = await db.list_images()
    if not rows:
        return "Koi uploaded image nahi."
    return "\n".join(
        f"{r['id']}  {r['mime']}  {r['size_bytes'] // 1024} KB  "
        f"{r['created_at']:%Y-%m-%d %H:%M} UTC  {'draft se juri hai' if r['attached'] else 'free'}"
        for r in rows
    )


@mcp.tool
async def create_draft(
    text: str, images: list[str] | None = None, alt_texts: list[str] | None = None
) -> str:
    """Draft save karo. images: image IDs ki list, ya ['latest'] (sab se nayi 1) ya ['latest:3'] (sab se nayi 3). alt_texts: har image ke liye optional. Publish nahi karta."""
    await guard()
    text = _text(text)
    if await db.count_drafts() >= MAX_DRAFTS:
        raise ToolError("Drafts ki hadd poori ho gayi. Purane delete karen.")
    ids = await _resolve_images(images)
    d = await db.create_draft(text, _items(ids, alt_texts))
    return f"Draft save ho gaya ({_kind(len(ids))}). ID: {d['id']}"


@mcp.tool(annotations=READ_ONLY)
async def list_drafts(status: str = "draft") -> str:
    """Drafts dikhao. status: draft, publishing, published, failed ya deleted."""
    await guard()
    if status not in STATUSES:
        raise ToolError("status draft, publishing, published, failed ya deleted hona chahiye.")
    rows = await db.list_drafts(status)
    if not rows:
        return f"Koi {status} draft nahi mila."
    out = []
    for r in rows:
        img = f"\n[images: {r['image_count']}]" if r["image_count"] else ""
        post = f"\n[LinkedIn: {r['linkedin_post_urn']}]" if r["linkedin_post_urn"] else ""
        err = f"\n[error: {r['error']}]" if r["error"] else ""
        out.append(f"ID: {r['id']} [{r['status']}]{img}{post}{err}\n{r['content']}")
    return "\n\n".join(out)


@mcp.tool(annotations=READ_ONLY)
async def read_draft(draft_id: str) -> str:
    """Ek draft poori tafseel se parho (text, har image ki ID aur alt text)."""
    await guard()
    d = await db.get_draft(_uuid(draft_id))
    if not d:
        return "Draft nahi mila."
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
    """Draft badlo. text dena ho to poora naya text do. images dene par purani saari images ki jagah yeh list lag jati hai. remove_images=True se saari images hat jati hain. Sirf alt_texts dene par maujooda images ke alt text badal jate hain."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] not in ("draft", "failed"):
        return "Draft nahi mila ya edit nahi ho sakta."
    new_text = _text(text) if text.strip() else d["content"]

    items = None  # None = images waisi hi
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
        return "Draft update nahi hua."
    await db.cleanup_images()
    return "Draft update ho gaya."


@mcp.tool
async def delete_draft(draft_id: str) -> str:
    """Draft database se hata do (published draft ko pehle LinkedIn se hatana parta hai)."""
    await guard()
    ok = await db.delete_draft(_uuid(draft_id))
    await db.cleanup_images()
    return "Draft delete ho gaya." if ok else "Draft nahi mila ya delete nahi ho sakta."


@mcp.tool
async def check_publish(draft_id: str, upload_images: bool = False) -> str:
    """Publish ka poora check. LinkedIn par POST NAHI karta. upload_images=True se images LinkedIn par upload karke raasta parkha jata hai (post phir bhi nahi banti)."""
    await guard()
    d = await db.get_draft(_uuid(draft_id))
    if not d:
        return "Draft nahi mila."
    out = []
    ready = d["status"] in ("draft", "failed")
    out.append(f"{'OK  ' if ready else 'FAIL'} status '{d['status']}' (publish ke liye draft ya failed chahiye)")
    out.append(f"OK   text {len(d['content'])}/{MAX_TEXT} characters")

    rows = await db.get_draft_images_data(d["id"])
    if len(rows) > MAX_POST_IMAGES:
        out.append(f"FAIL {len(rows)} images hain, {MAX_POST_IMAGES} se zyada nahi ho sakti")
    elif rows:
        sizes = ", ".join(f"{r['mime']} {len(r['data']) // 1024} KB" for r in rows)
        out.append(f"OK   {len(rows)} image(s): {sizes}")
    else:
        out.append("INFO image nahi hai")

    try:
        owner = await linkedin_client.verify_token()
        out.append("OK   LinkedIn ne token qubool kiya (sirf parha gaya, kuch likha nahi)")
    except linkedin_client.LinkedInError as e:
        out.append(f"FAIL {e}")
        return "\n".join(out)

    if upload_images:
        if not rows:
            out.append("INFO upload test ke liye draft me images nahi hain")
        else:
            try:
                urns = await linkedin_client.test_image_upload(
                    [(bytes(r["data"]), r["alt_text"]) for r in rows]
                )
                out.append(f"OK   {len(urns)} image(s) ka upload raasta chalta hai, post nahi bani")
            except linkedin_client.LinkedInError as e:
                out.append(f"FAIL {e}")

    body = linkedin_client.build_body(owner, d["content"])
    out.append(
        f"\nPost ki qisam: {_kind(len(rows))}, visibility {body['visibility']}.\n"
        f"Yeh bheja jata:\n{body['commentary']}"
        f"\n\nDRY_RUN: {'ON' if linkedin_client.DRY_RUN else 'OFF'}. LinkedIn par koi post nahi bheji gayi."
    )
    return "\n".join(out)


@mcp.tool
async def publish_draft(draft_id: str) -> str:
    """Draft LinkedIn par publish karne ki manzoori ka link banao (0, 1 ya kai images ke saath). Yeh khud publish NAHI karta. Link user ko do; user apne browser/phone me kholkar button dabaye ga tab post jaye gi."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] not in ("draft", "failed", "publishing"):
        return "Draft publish nahi ho sakta (nahi mila ya pehle hi publish hai)."
    token = await db.create_approval(did, "publish", await ops.fingerprint(did), 15)
    return (
        f"Qisam: {_kind(d['image_count'])}, visibility {linkedin_client.VISIBILITY}. "
        f"DRY_RUN: {'ON' if linkedin_client.DRY_RUN else 'OFF'}.\n---\n{d['content']}\n---\n"
        f"User ko yeh link do (15 minute, ek baar chalega). User link kholkar 'Haan, publish karo' dabaye to post jaye gi:\n"
        f"{web.BASE_URL}/approve?t={token}\n"
        "Tum khud publish nahi kar sakte. User se kaho link khole aur button dabaye, phir tumhe bataye."
    )


@mcp.tool
async def delete_linkedin_post(draft_id: str) -> str:
    """Publish ki hui post LinkedIn se hatane ki manzoori ka link banao. Yeh khud delete NAHI karta; user link kholkar button dabaye ga tab post hategi."""
    await guard()
    did = _uuid(draft_id)
    d = await db.get_draft(did)
    if not d or d["status"] != "published" or not d["linkedin_post_urn"]:
        return "Yeh draft publish ki hui post nahi hai."
    token = await db.create_approval(did, "delete", await ops.fingerprint(did), 15)
    return (
        f"LinkedIn se yeh post hategi: {d['linkedin_post_urn']}\n---\n{d['content']}\n---\n"
        f"User ko yeh link do (15 minute, ek baar chalega). User kholkar 'Haan, hata do' dabaye to post hategi:\n"
        f"{web.BASE_URL}/approve?t={token}"
    )


@mcp.tool(annotations=READ_ONLY)
async def linkedin_status() -> str:
    """LinkedIn connect hai ya nahi, token kab expire hoga, aur DRY_RUN on hai ya nahi."""
    await guard()
    mode = (
        "DRY RUN ON (LinkedIn par kuch nahi jata)"
        if linkedin_client.DRY_RUN
        else f"DRY RUN OFF (asli posting chalu, visibility {linkedin_client.VISIBILITY})"
    )
    tok = await db.get_token()
    if not tok:
        return f"{mode}. LinkedIn connect nahi hai. `python auth.py` chalayen."
    left = (tok["expires_at"] - datetime.now(timezone.utc)).days
    if left < 0:
        return f"{mode}. Token expire ho gaya. `python auth.py` dobara chalayen."
    return f"{mode}. LinkedIn connect hai, token mein lagbhag {left} din baaki hain."


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=8000)