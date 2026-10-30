"""Browser pages for the LinkedIn MCP server: image upload and post approval.

This tiny app is meant to run on a host that allows public web pages (for example Vercel), next to
an MCP server whose host puts a login in front of every URL. It needs only DATABASE_URL.

What it can do:   store uploaded images, and record that the user approved a publish/delete.
What it can't do: post to LinkedIn or read the LinkedIn token. The MCP server finishes the job
                  (finish_publish / finish_delete_post) after it sees the approval in the database.

Every page needs a one-time link created by the MCP server, so the pages are useless without one.
"""
import asyncio
import base64
import hashlib
import html
import os
import secrets

import psycopg
from psycopg.rows import dict_row
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

MAX_FILES = 20
MAX_STORED_IMAGES = int(os.getenv("MAX_STORED_IMAGES", "30"))
MAX_IMAGE_BYTES = 3_500_000   # the browser shrinks photos far below this
MAX_THUMB_BYTES = 150_000
MAX_BODY_BYTES = 4_000_000    # Vercel rejects request bodies above roughly 4.5 MB
JPEG_MAGIC = b"\xff\xd8\xff"

VISIBLE_TO = {"PUBLIC": "Anyone on LinkedIn", "CONNECTIONS": "Your connections only"}

# ---------------------------------------------------------------- database


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def _connect() -> psycopg.AsyncConnection:
    last = None
    for _attempt in range(2):  # a sleeping Neon database may need a moment to wake up
        try:
            return await psycopg.AsyncConnection.connect(
                os.environ["DATABASE_URL"],
                row_factory=dict_row,
                prepare_threshold=None,
                connect_timeout=6,
            )
        except psycopg.OperationalError as e:
            last = e
            await asyncio.sleep(1)
    raise last


async def _with_db(fn):
    conn = await _connect()
    async with conn:  # commits when the block ends without an error
        return await fn(conn)


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#f5f5f0;--surface:#fff;--ink:#17201c;--muted:#5b6660;--line:#d9ddd6;
--accent:#1d6b57;--accent-ink:#fff;--danger:#a9261f}
@media (prefers-color-scheme: dark){:root{--bg:#101412;--surface:#171c19;--ink:#e8ece9;--muted:#9aa5a0;
--line:#2b332f;--accent:#5fc2a5;--accent-ink:#0b1410;--danger:#f08a82}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
main{max-width:34rem;margin:0 auto;padding:1.5rem 1rem 3rem}
h1{font-size:1.45rem;line-height:1.25;margin:.5rem 0 1rem;font-weight:650}
p{margin:.5rem 0}
.muted{color:var(--muted)}
.card{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:1rem;margin:1rem 0}
.post{white-space:pre-wrap;overflow-wrap:anywhere}
.grid{display:grid;gap:6px;margin-top:.75rem;grid-template-columns:repeat(auto-fill,minmax(7.5rem,1fr))}
.grid img,.grid .noimg{width:100%;aspect-ratio:1;object-fit:cover;border-radius:8px;display:block;background:var(--line)}
.grid .noimg{display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.85rem}
.facts{display:flex;gap:.25rem 1.5rem;flex-wrap:wrap;margin:0 0 1rem;padding:0;list-style:none}
.notice{border-left:4px solid var(--danger);padding:.4rem .75rem;margin:1rem 0}
.btn{display:block;width:100%;padding:.95rem 1rem;border:0;border-radius:10px;background:var(--accent);
color:var(--accent-ink);font:inherit;font-weight:650;cursor:pointer}
.btn.danger{background:var(--danger);color:#fff}
.btn[disabled]{opacity:.5;cursor:default}
.btn:focus-visible,.drop:focus-within{outline:3px solid var(--ink);outline-offset:2px}
.drop{display:block;border:2px dashed var(--line);border-radius:12px;padding:1.75rem 1rem;text-align:center;
cursor:pointer;margin:1rem 0;background:var(--surface)}
.drop input{position:absolute;width:1px;height:1px;opacity:0}
.files{list-style:none;margin:1rem 0;padding:0}
.files li{display:flex;justify-content:space-between;gap:1rem;padding:.45rem 0;border-bottom:1px solid var(--line)}
.files .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.files .state{flex:none;color:var(--muted)}
.files .ok .state{color:var(--accent)}
.files .bad .state{color:var(--danger)}
"""

UPLOAD_JS = r"""
(() => {
  const MAX_FILES = __MAX_FILES__, LIMIT = __LIMIT__, THUMB_LIMIT = __THUMB_LIMIT__;
  const input = document.getElementById('files');
  const go = document.getElementById('go');
  const list = document.getElementById('list');
  const summary = document.getElementById('summary');
  const token = document.getElementById('t').value;
  let picked = [];

  input.addEventListener('change', () => {
    picked = Array.from(input.files).slice(0, MAX_FILES);
    list.textContent = '';
    picked.forEach((f) => {
      const li = document.createElement('li');
      const name = document.createElement('span'); name.className = 'name'; name.textContent = f.name;
      const state = document.createElement('span'); state.className = 'state'; state.textContent = 'Ready';
      li.append(name, state); list.append(li);
    });
    summary.textContent = input.files.length > MAX_FILES ? 'Only the first ' + MAX_FILES + ' images will be uploaded.' : '';
    go.disabled = picked.length === 0;
  });

  async function decode(file) {
    if (window.createImageBitmap) {
      try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); } catch (e) {}
      try { return await createImageBitmap(file); } catch (e) {}
    }
    return new Promise((resolve, reject) => {
      const url = URL.createObjectURL(file);
      const img = new Image();
      img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
      img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('This file is not a readable image.')); };
      img.src = url;
    });
  }

  function encode(src, maxDim, quality) {
    const w = src.naturalWidth || src.width, h = src.naturalHeight || src.height;
    const k = Math.min(1, maxDim / Math.max(w, h));
    const cw = Math.max(1, Math.round(w * k)), ch = Math.max(1, Math.round(h * k));
    const c = document.createElement('canvas'); c.width = cw; c.height = ch;
    const g = c.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, cw, ch); g.drawImage(src, 0, 0, cw, ch);
    return new Promise((res) => c.toBlob(res, 'image/jpeg', quality));
  }

  async function shrink(file) {
    const src = await decode(file);
    let main = null;
    for (const [dim, q] of [[2048, 0.85], [1600, 0.8], [1200, 0.75], [900, 0.7]]) {
      main = await encode(src, dim, q);
      if (main && main.size <= LIMIT) break;
    }
    const thumb = await encode(src, 320, 0.7);
    if (src.close) src.close();
    if (!main || main.size > LIMIT) throw new Error('Could not make this image small enough.');
    if (!thumb || thumb.size > THUMB_LIMIT) throw new Error('Could not make a preview of this image.');
    return { main, thumb };
  }

  async function send(main, thumb) {
    const fd = new FormData();
    fd.append('t', token);
    fd.append('file', main, 'image.jpg');
    fd.append('thumb', thumb, 'thumb.jpg');
    const r = await fetch('/upload', { method: 'POST', body: fd });
    let j = {};
    try { j = await r.json(); } catch (e) {}
    if (!r.ok || !j.ok) throw new Error(j.error || 'Upload failed (' + r.status + ').');
  }

  go.addEventListener('click', async () => {
    go.disabled = true; input.disabled = true;
    const rows = Array.from(list.children);
    let done = 0;
    for (let i = 0; i < picked.length; i++) {
      const state = rows[i].querySelector('.state');
      try {
        state.textContent = 'Resizing\u2026';
        const { main, thumb } = await shrink(picked[i]);
        state.textContent = 'Uploading\u2026';
        await send(main, thumb);
        state.textContent = 'Uploaded'; rows[i].className = 'ok'; done++;
      } catch (e) {
        state.textContent = e.message; rows[i].className = 'bad';
        if (/link|limit|full/i.test(e.message)) break;
      }
    }
    summary.textContent = done === picked.length
      ? 'All ' + done + ' uploaded. Go back to your assistant and ask it to create the draft.'
      : done + ' of ' + picked.length + ' uploaded. For the rest, ask your assistant for a new upload link.';
  });
})();
"""


def _page(title: str, body: str, *, status: int = 200, script: str | None = None) -> HTMLResponse:
    nonce = secrets.token_urlsafe(16)
    csp = (
        f"default-src 'none'; style-src 'nonce-{nonce}'; img-src data: blob:; "
        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    scr = ""
    if script:
        csp += f"; script-src 'nonce-{nonce}'; connect-src 'self'"
        scr = f'<script nonce="{nonce}">{script}</script>'
    doc = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f'<title>{html.escape(title)}</title><style nonce="{nonce}">{CSS}</style></head>'
        f"<body><main>{body}</main>{scr}</body></html>"
    )
    return HTMLResponse(
        doc,
        status_code=status,
        headers={
            "Content-Security-Policy": csp,
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _dead_link(reason: str = "It expired or was already used.") -> HTMLResponse:
    return _page(
        "Link does not work",
        f"<h1>This link does not work</h1><p>{html.escape(reason)}</p>"
        '<p class="muted">Ask your assistant for a new link.</p>',
        status=400,
    )


# ---------------------------------------------------------------- upload


async def upload_page(request: Request):
    token = request.query_params.get("t", "")

    async def check(conn):
        cur = await conn.execute(
            "SELECT 1 AS ok FROM upload_tickets WHERE token_hash = %s AND used_at IS NULL "
            "AND uses_left > 0 AND expires_at > now()",
            (_h(token),),
        )
        return await cur.fetchone()

    if not token or not await _with_db(check):
        return _dead_link()
    body = (
        "<h1>Upload images</h1>"
        f"<p>Choose up to {MAX_FILES} photos. They are resized on your device before they are sent, "
        "and location data is removed.</p>"
        '<label class="drop"><input id="files" type="file" accept="image/*" multiple>'
        "<span>Choose images</span></label>"
        f'<input id="t" type="hidden" value="{html.escape(token)}">'
        '<ul id="list" class="files" aria-live="polite"></ul>'
        '<p id="summary" class="muted" role="status"></p>'
        '<button id="go" class="btn" type="button" disabled>Upload</button>'
    )
    js = (
        UPLOAD_JS.replace("__MAX_FILES__", str(MAX_FILES))
        .replace("__LIMIT__", str(MAX_IMAGE_BYTES))
        .replace("__THUMB_LIMIT__", str(MAX_THUMB_BYTES))
    )
    return _page("Upload images", body, script=js)


def _json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": message}, status_code=status, headers={"Cache-Control": "no-store"}
    )


async def upload_post(request: Request):
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return _json_error("This image is too large. Try a smaller one.", 413)
    form = await request.form()
    token = str(form.get("t", ""))
    main, thumb = form.get("file"), form.get("thumb")
    if not token or not hasattr(main, "read") or not hasattr(thumb, "read"):
        return _json_error("Upload is incomplete. Reload the page and try again.")
    data = await main.read(MAX_IMAGE_BYTES + 1)
    thumb_data = await thumb.read(MAX_THUMB_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        return _json_error("This image is too large. Try a smaller one.", 413)
    if not thumb_data or len(thumb_data) > MAX_THUMB_BYTES:
        return _json_error("The preview is too large.")
    if not data.startswith(JPEG_MAGIC) or not thumb_data.startswith(JPEG_MAGIC):
        return _json_error("Only images prepared by the upload page are accepted.")

    async def save(conn):
        cur = await conn.execute("SELECT count(*) AS n FROM images")
        if (await cur.fetchone())["n"] >= MAX_STORED_IMAGES:
            return "full"
        cur = await conn.execute(
            "UPDATE upload_tickets SET uses_left = uses_left - 1, "
            "used_at = CASE WHEN uses_left - 1 <= 0 THEN now() ELSE used_at END "
            "WHERE token_hash = %s AND used_at IS NULL AND uses_left > 0 AND expires_at > now() "
            "RETURNING uses_left",
            (_h(token),),
        )
        if not await cur.fetchone():
            return "invalid"
        await conn.execute(
            "INSERT INTO images (mime, data, size_bytes, thumb) VALUES ('image/jpeg', %s, %s, %s)",
            (data, len(data), thumb_data),
        )
        return "ok"

    result = await _with_db(save)
    if result == "invalid":
        return _json_error("This upload link expired or reached its limit.", 400)
    if result == "full":
        return _json_error("Storage is full. Delete old drafts, then ask for a new link.", 400)
    return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- approval

TICKET_COLS = (
    "token_hash, draft_id, action, fingerprint, used_at, approved_at, visibility, dry_run, "
    "(expires_at > now()) AS live"
)


async def _load(conn, token: str):
    """Returns (state, ticket, draft, images). state: invalid | changed | approved | ready."""
    cur = await conn.execute(
        f"SELECT {TICKET_COLS} FROM approval_tickets WHERE token_hash = %s", (_h(token),)
    )
    t = await cur.fetchone()
    if not t or t["used_at"] is not None or not t["live"]:
        return "invalid", None, None, []
    cur = await conn.execute("SELECT content, status FROM drafts WHERE id = %s", (t["draft_id"],))
    d = await cur.fetchone()
    if not d:
        return "invalid", None, None, []
    cur = await conn.execute(
        "SELECT di.image_id, i.thumb FROM draft_images di JOIN images i ON i.id = di.image_id "
        "WHERE di.draft_id = %s ORDER BY di.position",
        (t["draft_id"],),
    )
    imgs = await cur.fetchall()
    # Must match ops.fingerprint() in the MCP server.
    raw = d["content"] + "|" + ",".join(str(r["image_id"]) for r in imgs)
    if hashlib.sha256(raw.encode()).hexdigest() != t["fingerprint"]:
        return "changed", t, d, imgs
    if t["action"] == "publish":
        status_ok = d["status"] in ("draft", "failed", "publishing")
    else:
        status_ok = d["status"] == "published"
    if not status_ok:
        return "invalid", None, None, []
    if t["approved_at"] is not None:
        return "approved", t, d, imgs
    return "ready", t, d, imgs


def _dry_notice(t) -> str:
    if t["dry_run"]:
        return '<p class="notice">Dry run is on: nothing will be sent to LinkedIn, even after you approve.</p>'
    return ""


def _after_approval(t) -> HTMLResponse:
    verb = "publish the post" if t["action"] == "publish" else "remove the post from LinkedIn"
    return _page(
        "Approved",
        f"<h1>Approved</h1><p>Go back to your assistant and tell it you approved. It will {verb} "
        f'right away.</p><p class="muted">The approval stays valid for 10 minutes.</p>{_dry_notice(t)}',
    )


async def approve_page(request: Request):
    token = request.query_params.get("t", "")
    if token:
        state, t, d, imgs = await _with_db(lambda conn: _load(conn, token))
    else:
        state, t, d, imgs = "invalid", None, None, []
    if state == "invalid":
        return _dead_link()
    if state == "changed":
        return _dead_link("The draft changed after this link was created.")
    if state == "approved":
        return _after_approval(t)

    publish = t["action"] == "publish"
    facts = ""
    grid = ""
    if publish:
        visible = VISIBLE_TO.get(t["visibility"], t["visibility"])
        facts = (
            f"<li>Visible to: <strong>{html.escape(visible)}</strong></li>"
            f"<li>Images: <strong>{len(imgs)}</strong></li>"
        )
        cells = []
        for r in imgs:
            if r["thumb"]:
                src = "data:image/jpeg;base64," + base64.b64encode(bytes(r["thumb"])).decode()
                cells.append(f'<img src="{src}" alt="">')
            else:
                cells.append('<div class="noimg">No preview</div>')
        grid = f'<div class="grid">{"".join(cells)}</div>' if cells else ""
    heading = "Publish this post to LinkedIn?" if publish else "Remove this post from LinkedIn?"
    label = "Approve publishing" if publish else "Approve removal"
    body = (
        f"<h1>{heading}</h1>"
        f'<ul class="facts">{facts}</ul>{_dry_notice(t)}'
        f'<article class="card"><div class="post">{html.escape(d["content"])}</div>{grid}</article>'
        '<form method="post" action="/approve">'
        f'<input type="hidden" name="t" value="{html.escape(token)}">'
        f'<button class="btn{"" if publish else " danger"}" type="submit">{label}</button></form>'
        '<p class="muted">To cancel, close this page.</p>'
    )
    return _page("Approval", body)


async def approve_post(request: Request):
    form = await request.form()
    token = str(form.get("t", ""))

    async def work(conn):
        state, t, _d, _imgs = await _load(conn, token)
        if state != "ready":
            return state, t
        cur = await conn.execute(
            "UPDATE approval_tickets SET approved_at = now() WHERE token_hash = %s AND used_at IS NULL "
            "AND approved_at IS NULL AND expires_at > now() RETURNING action",
            (_h(token),),
        )
        if not await cur.fetchone():
            return "invalid", t
        return "done", t

    if token:
        state, t = await _with_db(work)
    else:
        state, t = "invalid", None
    if state in ("done", "approved"):
        return _after_approval(t)
    if state == "changed":
        return _dead_link("The draft changed after this link was created.")
    return _dead_link()


app = Starlette(
    routes=[
        Route("/upload", upload_page, methods=["GET"]),
        Route("/upload", upload_post, methods=["POST"]),
        Route("/approve", approve_page, methods=["GET"]),
        Route("/approve", approve_post, methods=["POST"]),
    ]
)
