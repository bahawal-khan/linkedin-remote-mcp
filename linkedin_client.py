import os
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

# Default me DRY RUN OFF hai (post tab hi jati hai jab user approval link par button dabaye).
# Test ke liye .env me DRY_RUN=1 likh sakte hain.
DRY_RUN = os.getenv("DRY_RUN", "0") != "0"

# Pehle tests ke liye POST_VISIBILITY=CONNECTIONS rakh sakte hain (sirf connections dekhenge).
VISIBILITY = os.getenv("POST_VISIBILITY", "PUBLIC").upper()
if VISIBILITY not in ("PUBLIC", "CONNECTIONS"):
    VISIBILITY = "PUBLIC"

# '#' escape nahi hota taake hashtags chalen. Post ajeeb dikhe to '#' wapas yahan daalen.
_RESERVED = set("\\|{}@[]()<>*_~")


class LinkedInError(Exception):
    pass


def escape_text(text: str) -> str:
    return "".join("\\" + c if c in _RESERVED else c for c in text)


async def _credentials():
    row = await db.get_token()
    if not row:
        raise LinkedInError("LinkedIn connect nahi hai. Apne computer par `python auth.py` chalayen.")
    if row["expires_at"] <= datetime.now(timezone.utc):
        raise LinkedInError("LinkedIn token expire ho gaya. `python auth.py` dobara chalayen.")
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
        "commentary": escape_text(text),
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
    """Token LinkedIn ko dikhata hai. Sirf parhta hai, kuch likhta nahi. Member URN wapas deta hai."""
    token, owner = await _credentials()
    async with httpx.AsyncClient(timeout=20) as http:
        r = await http.get(f"{API}/v2/userinfo", headers={"Authorization": f"Bearer {token}"})
    if r.status_code == 401:
        raise LinkedInError("LinkedIn ne token qubool nahi kiya (401). `python auth.py` dobara chalayen.")
    if r.status_code != 200:
        raise LinkedInError(f"LinkedIn userinfo error {r.status_code}")
    if f"urn:li:person:{r.json().get('sub')}" != owner:
        raise LinkedInError("Token kisi aur member ka lagta hai (member ID match nahi hui).")
    return owner


async def _upload_image(http: httpx.AsyncClient, token: str, owner: str, data: bytes) -> str:
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
    """Images LinkedIn par upload karta hai lekin post NAHI banata. images: [(bytes, alt), ...]"""
    token, owner = await _credentials()
    urns = []
    async with httpx.AsyncClient(timeout=60) as http:
        for data, _alt in images:
            urns.append(await _upload_image(http, token, owner, data))
    return urns


async def publish_post(text: str, images: list | None = None) -> str:
    """images: [(bytes, alt_text), ...]. 0 images: text, 1: single image, 2+: multi-image."""
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
        raise LinkedInError("Token expire ho gaya. `python auth.py` dobara chalayen.")
    if r.status_code != 201:
        raise LinkedInError(f"LinkedIn error {r.status_code}: {r.text[:300]}")
    return r.headers.get("x-restli-id", "unknown")


async def delete_post(post_urn: str) -> None:
    token, _ = await _credentials()
    url = f"{API}/rest/posts/{urllib.parse.quote(post_urn, safe='')}"
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.delete(url, headers=_headers(token, {"X-RestLi-Method": "DELETE"}))
    if r.status_code not in (200, 204):
        raise LinkedInError(f"LinkedIn delete error {r.status_code}: {r.text[:300]}")