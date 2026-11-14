import os
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import db  # noqa: E402
from crypto import decrypt  # noqa: E402

API = "https://api.linkedin.com"
API_VERSION = os.getenv("LINKEDIN_VERSION", "202609")

# DRY_RUN=1 sends nothing to LinkedIn (posting, deleting and image uploads are blocked).
# Safe by default: live posting happens only when DRY_RUN is explicitly set to 0.
DRY_RUN = os.getenv("DRY_RUN", "1").strip() != "0"

# For first tests use POST_VISIBILITY=CONNECTIONS (only your connections can see the post).
VISIBILITY = os.getenv("POST_VISIBILITY", "PUBLIC").upper()
if VISIBILITY not in ("PUBLIC", "CONNECTIONS"):
    VISIBILITY = "PUBLIC"

MAX_COMMENTARY = 3000

# LinkedIn "little text" format: these characters are special and must be escaped with a backslash.
_RESERVED = set("\\|{}@[]()<>#*_~")
# #Hashtag (needs at least one letter, so "#1" stays plain text) and an explicit mention
# @[Name](urn:li:person:ID). Everything else is escaped.
_TOKENS = re.compile(
    r"(?P<mention>@\[(?P<name>[^\[\]\\\n]{1,100})\]\((?P<urn>urn:li:(?:person|organization):[A-Za-z0-9_-]{1,40})\))"
    r"|(?<![\w#\\/&])#(?P<tag>(?=\w*[^\W\d_])\w+)"
)


class LinkedInError(Exception):
    pass


def escape_text(text: str) -> str:
    return "".join("\\" + c if c in _RESERVED else c for c in text)


def format_commentary(text: str) -> str:
    """Turn the draft text into LinkedIn's commentary format.

    - line breaks and blank lines are kept
    - #Hashtags become real clickable hashtags
    - @[Name](urn:li:person:ID) becomes a real mention (the ID must be known)
    - every other special character is escaped, so it shows up literally
    """
    text = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    out, pos = [], 0
    for m in _TOKENS.finditer(text):
        out.append(escape_text(text[pos : m.start()]))
        if m.group("mention"):
            out.append(f"@[{escape_text(m.group('name'))}]({m.group('urn')})")
        else:
            out.append("{hashtag|\\#|" + m.group("tag") + "}")
        pos = m.end()
    out.append(escape_text(text[pos:]))
    return "".join(out)


async def _credentials():
    row = await db.get_token()
    if not row:
        raise LinkedInError("LinkedIn is not connected. Run `python auth.py` on your own computer.")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise LinkedInError("The LinkedIn token has expired. Run `python auth.py` again.")
    return decrypt(row["access_token_enc"]), row["person_urn"]


def _headers(token: str, extra: dict | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Linkedin-Version": API_VERSION,
    }
    if extra:
        h.update(extra)
    return h


def build_body(owner: str, text: str) -> dict:
    return {
        "author": owner,
        "commentary": format_commentary(text),
        "visibility": VISIBILITY,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


def _media_item(urn: str, alt: str) -> dict:
    item = {"id": urn}
    if alt:
        item["altText"] = alt
    return item


def build_content(uploaded: list) -> dict | None:
    """uploaded: [(image_urn, alt_text), ...]. 0 images: None, 1: media, 2+: multiImage."""
    if not uploaded:
        return None
    if len(uploaded) == 1:
        return {"media": _media_item(*uploaded[0])}
    return {"multiImage": {"images": [_media_item(u, a) for u, a in uploaded]}}


async def verify_token() -> str:
    """Show the token to LinkedIn. Read only, nothing is written. Returns the member URN."""
    token, owner = await _credentials()
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise LinkedInError("LinkedIn rejected the token (401). Run `python auth.py` again.")
    if r.status_code != 200:
        raise LinkedInError(f"LinkedIn userinfo error {r.status_code}")
    if f"urn:li:person:{r.json().get('sub')}" != owner:
        raise LinkedInError("The token belongs to a different member (member ID does not match).")
    return owner


def _no_dry_run() -> None:
    if DRY_RUN:
        raise LinkedInError("DRY_RUN is on: nothing is sent to LinkedIn.")


async def _upload_image(http: httpx.AsyncClient, token: str, owner: str, data: bytes) -> str:
    _no_dry_run()
    r = await http.post(
        f"{API}/rest/images?action=initializeUpload",
        json={"initializeUploadRequest": {"owner": owner}},
        headers=_headers(token),
    )
    if r.status_code != 200:
        raise LinkedInError(f"Image init error {r.status_code}: {r.text[:300]}")
    value = r.json()["value"]
    up = await http.put(
        value["uploadUrl"], content=data, headers={"Authorization": f"Bearer {token}"}
    )
    if up.status_code not in (200, 201):
        raise LinkedInError(f"Image upload error {up.status_code}: {up.text[:300]}")
    return value["image"]


async def test_image_upload(images: list) -> list:
    """Upload images to LinkedIn but do NOT create a post. images: [(bytes, alt), ...]"""
    _no_dry_run()
    token, owner = await _credentials()
    urns = []
    async with httpx.AsyncClient(timeout=60) as http:
        for data, _alt in images:
            urns.append(await _upload_image(http, token, owner, data))
    return urns


async def publish_post(text: str, images: list | None = None) -> str:
    """images: [(bytes, alt_text), ...]. 0 images: text, 1: single image, 2+: multi-image."""
    _no_dry_run()
    token, owner = await _credentials()
    body = build_body(owner, text)
    async with httpx.AsyncClient(timeout=120) as http:
        uploaded = []
        for data, alt in images or []:
            uploaded.append((await _upload_image(http, token, owner, data), alt))
        content = build_content(uploaded)
        if content:
            body["content"] = content
        r = await http.post(f"{API}/rest/posts", json=body, headers=_headers(token))
    if r.status_code == 401:
        raise LinkedInError("The token has expired. Run `python auth.py` again.")
    if r.status_code != 201:
        raise LinkedInError(f"LinkedIn error {r.status_code}: {r.text[:300]}")
    return r.headers.get("x-restli-id", "unknown")


async def delete_post(post_urn: str) -> None:
    _no_dry_run()
    token, _ = await _credentials()
    url = f"{API}/rest/posts/{urllib.parse.quote(post_urn, safe='')}"
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.delete(url, headers=_headers(token, {"X-RestLi-Method": "DELETE"}))
    if r.status_code not in (200, 204):
        raise LinkedInError(f"LinkedIn delete error {r.status_code}: {r.text[:300]}")
