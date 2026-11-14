import asyncio
import base64
import html
import io
import os
import warnings
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageOps
from starlette.requests import Request
from starlette.responses import HTMLResponse

load_dotenv(Path(__file__).parent / ".env")

import db  # noqa: E402
import linkedin_client  # noqa: E402
import ops  # noqa: E402

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000").rstrip("/")
MAX_BYTES = 5 * 1024 * 1024        # per image
MAX_FILES = 20                     # per upload
MAX_TOTAL = 30 * 1024 * 1024        # all files together
Image.MAX_IMAGE_PIXELS = 36_000_000  # guards against decompression bombs

SEC_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'; base-uri 'none'",
}


def page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:560px;margin:40px auto;padding:0 16px;line-height:1.5}}
input{{width:100%;padding:8px;margin:8px 0 16px;box-sizing:border-box}}
button{{padding:12px 18px;cursor:pointer;font-size:16px}}
pre{{white-space:pre-wrap;word-wrap:break-word;background:#f4f4f4;padding:12px;border-radius:8px}}
img{{max-width:100%;border-radius:6px;margin:4px 0}}
.warn{{background:#fff3cd;padding:8px 12px;border-radius:6px}}
</style></head><body>{body}</body></html>"""
    return HTMLResponse(doc, status_code=status, headers=SEC_HEADERS)


def bad_link() -> HTMLResponse:
    return page(
        "Link does not work",
        "<h2>This link does not work</h2><p>It expired or was already used. "
        "Ask your assistant to run <b>upload_image_link</b> again.</p>",
        400,
    )


def _clean_image(raw: bytes):
    """Validate the image and re-encode it (this strips EXIF/GPS metadata)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        im = Image.open(io.BytesIO(raw))
        fmt = im.format
        if fmt not in ("PNG", "JPEG", "WEBP"):
            raise ValueError("Only PNG, JPG or WEBP images are allowed.")
        im.load()
    im = ImageOps.exif_transpose(im)
    out = io.BytesIO()
    if fmt == "JPEG":
        im.convert("RGB").save(out, "JPEG", quality=90, optimize=True)
        return "image/jpeg", out.getvalue()
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGBA")
    im.save(out, "PNG", optimize=True)
    return "image/png", out.getvalue()


def register(mcp) -> None:
    @mcp.custom_route("/upload", methods=["GET"])
    async def upload_form(request: Request):
        token = request.query_params.get("t", "")
        if not token or not await db.peek_upload_ticket(token):
            return bad_link()
        body = f"""<h2>Upload images</h2>
<p>PNG, JPG or WEBP. Up to {MAX_FILES} at a time, 5 MB each. You can pick several at once.</p>
<form method="post" action="/upload" enctype="multipart/form-data">
<input type="hidden" name="t" value="{html.escape(token)}">
<input type="file" name="file" accept="image/png,image/jpeg,image/webp" multiple required>
<button type="submit">Upload images</button>
</form>"""
        return page("Upload images", body)

    @mcp.custom_route("/upload", methods=["POST"])
    async def upload_submit(request: Request):
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > MAX_TOTAL + 200_000:
            return page("Too large", "<h2>The files are too large (keep the total under 30 MB)</h2>", 413)
        form = await request.form()
        token = str(form.get("t", ""))
        if not token or not await db.peek_upload_ticket(token):
            return bad_link()
        files = [f for f in form.getlist("file") if hasattr(f, "read")]
        if not files:
            return page("Error", "<h2>No file received</h2>", 400)
        if len(files) > MAX_FILES:
            return page("Too many files", f"<h2>No more than {MAX_FILES} images at a time</h2>", 400)

        cleaned = []
        for n, f in enumerate(files, 1):
            raw = await f.read(MAX_BYTES + 1)
            if not raw or len(raw) > MAX_BYTES:
                return page("Error", f"<h2>File {n} is empty or larger than 5 MB</h2>", 400)
            try:
                cleaned.append(await asyncio.to_thread(_clean_image, raw))
            except Exception:
                return page(
                    "Error",
                    f"<h2>File {n} is not a valid image (PNG, JPG or WEBP needed)</h2>",
                    400,
                )
        if await db.count_images() + len(cleaned) > db.MAX_IMAGES:
            return page("Full", "<h2>Too many stored images. Delete old drafts first.</h2>", 400)
        if not await db.consume_upload_ticket(token):
            return bad_link()
        for mime, data in cleaned:
            await db.save_image(mime, data)
        return page(
            "Uploaded",
            f"<h2>{len(cleaned)} image(s) saved</h2><p>Go back to your assistant and ask it to "
            "<b>create a draft with these images</b>.</p>",
        )

    def _thumb(data: bytes) -> str:
        im = Image.open(io.BytesIO(data))
        im.thumbnail((480, 480))
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()

    def bad_approval() -> HTMLResponse:
        return page(
            "Link does not work",
            "<h2>This link does not work</h2><p>It expired, was already used, or the draft changed. "
            "Ask your assistant to create a new approval link.</p>",
            400,
        )

    @mcp.custom_route("/approve", methods=["GET"])
    async def approve_form(request: Request):
        token = request.query_params.get("t", "")
        t = await db.peek_approval(token) if token else None
        if not t:
            return bad_approval()
        d = await db.get_draft(t["draft_id"])
        if not d or await ops.fingerprint(t["draft_id"]) != t["fingerprint"]:
            return bad_approval()
        publish = t["action"] == "publish"
        thumbs = ""
        if publish:
            rows = await db.get_draft_images_data(t["draft_id"])
            for r in rows:
                src = await asyncio.to_thread(_thumb, bytes(r["data"]))
                thumbs += f'<img src="{src}" alt="">'
        dry = (
            '<p class="warn">DRY RUN is on: nothing will be sent to LinkedIn even if you tap the button.</p>'
            if linkedin_client.DRY_RUN
            else ""
        )
        title = "Publish this post to LinkedIn?" if publish else "Remove this post from LinkedIn?"
        label = "Yes, publish" if publish else "Yes, delete"
        body = f"""<h2>{title}</h2>{dry}
<p>Visibility: <b>{html.escape(linkedin_client.VISIBILITY)}</b></p>
<pre>{html.escape(d["content"])}</pre>{thumbs}
<form method="post" action="/approve">
<input type="hidden" name="t" value="{html.escape(token)}">
<button type="submit">{label}</button>
</form>
<p>To cancel, just close this page.</p>"""
        return page("Approval", body)

    @mcp.custom_route("/approve", methods=["POST"])
    async def approve_submit(request: Request):
        form = await request.form()
        token = str(form.get("t", ""))
        t = await db.peek_approval(token) if token else None
        if not t or await ops.fingerprint(t["draft_id"]) != t["fingerprint"]:
            return bad_approval()
        t = await db.consume_approval(token)
        if not t:
            return bad_approval()
        if t["action"] == "publish":
            msg = await ops.do_publish(t["draft_id"], t["fingerprint"])
        else:
            msg = await ops.do_delete_post(t["draft_id"])
        return page("Result", f"<h2>{html.escape(msg)}</h2><p>You can go back to your assistant now.</p>")
