import asyncio
import os
import re
import secrets
import psycopg
import pytest
from conftest import PAGES_URL, ROOT, _swap_db, call, jpeg, token_in, upload
from fastmcp.exceptions import ToolError


@pytest.fixture()
def fake_linkedin(srv, monkeypatch):
    """Live mode with a fake LinkedIn: nothing leaves the machine."""
    import linkedin_client

    calls = {"publish": [], "delete": []}

    async def publish_post(text, images=None):
        calls["publish"].append((text, list(images or [])))
        await asyncio.sleep(0.2)
        return "urn:li:share:123"

    async def delete_post(urn):
        calls["delete"].append(urn)

    monkeypatch.setattr(linkedin_client, "DRY_RUN", False)
    monkeypatch.setattr(linkedin_client, "VISIBILITY", "CONNECTIONS")
    monkeypatch.setattr(linkedin_client, "publish_post", publish_post)
    monkeypatch.setattr(linkedin_client, "delete_post", delete_post)
    return calls


def new_draft(srv, text="Hello LinkedIn", **kw):
    out = call(srv, "create_draft", text=text, **kw)
    return re.search(r"ID: ([0-9a-f-]{36})", out).group(1)


def approve(client, link_text):
    t = token_in(link_text)
    page = client.get(f"/approve?t={t}")
    r = client.post("/approve", data={"t": t})
    return t, page, r


# ------------------------------------------------------------------ uploads


def test_upload_link_goes_to_pages_app_and_uploads_work(srv, web_client, q):
    msg = call(srv, "upload_image_link")
    assert f"{PAGES_URL}/upload?t=" in msg
    t = token_in(msg)

    page = web_client.get(f"/upload?t={t}")
    assert page.status_code == 200 and "Upload images" in page.text
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "nonce-" in csp and "unsafe-inline" not in csp

    for _ in range(3):
        r = upload(web_client, t)
        assert r.status_code == 200 and r.json() == {"ok": True}
    row = q("SELECT count(*) AS n, count(thumb) AS thumbs FROM images")[0]
    assert (row["n"], row["thumbs"]) == (3, 3)
    assert q("SELECT uses_left FROM upload_tickets")[0]["uses_left"] == 17
    assert call(srv, "list_images").count("image/jpeg") == 3


def test_upload_rejects_bad_input(srv, web_client, q):
    t = token_in(call(srv, "upload_image_link"))
    assert upload(web_client, "nope").status_code == 400                      # unknown token
    assert upload(web_client, t, data=b"\x89PNG....").status_code == 400      # not a JPEG
    assert upload(web_client, t, thumb=b"GIF89a").status_code == 400          # thumb not a JPEG
    big = jpeg(2000, 2000) + b"\0" * 3_600_000
    assert upload(web_client, t, data=big).status_code == 413                 # too large
    assert web_client.post("/upload", data={"t": t}).status_code == 400       # files missing
    assert q("SELECT count(*) AS n FROM images")[0]["n"] == 0
    assert q("SELECT uses_left FROM upload_tickets")[0]["uses_left"] == 20    # nothing was used up


def test_upload_ticket_runs_out_and_expires(db_url, web_client, q):
    import db

    t = asyncio.run(db.create_upload_ticket(15, 2))
    assert upload(web_client, t).status_code == 200
    assert upload(web_client, t).status_code == 200
    assert upload(web_client, t).status_code == 400
    assert web_client.get(f"/upload?t={t}").status_code == 400
    t2 = asyncio.run(db.create_upload_ticket(15, 5))
    q("UPDATE upload_tickets SET expires_at = now() - interval '1 minute' WHERE uses_left = 5")
    assert web_client.get(f"/upload?t={t2}").status_code == 400
    assert upload(web_client, t2).status_code == 400


def test_storage_limit(pages, web_client, q, monkeypatch):
    import db

    monkeypatch.setattr(pages, "MAX_STORED_IMAGES", 2)
    t = asyncio.run(db.create_upload_ticket(15, 5))
    assert upload(web_client, t).status_code == 200
    assert upload(web_client, t).status_code == 200
    r = upload(web_client, t)
    assert r.status_code == 400 and "full" in r.json()["error"].lower()


# ------------------------------------------------------------------ approval + publish


def test_full_publish_with_three_images(srv, web_client, fake_linkedin, q):
    t = token_in(call(srv, "upload_image_link"))
    for color in [(255, 0, 0), (0, 255, 0), (0, 0, 255)]:
        assert upload(web_client, t, data=jpeg(color=color)).status_code == 200
    did = new_draft(srv, "Three photos, one story", images=["latest:3"], alt_texts=["red", "green", "blue"])
    assert "Image 3" in call(srv, "read_draft", draft_id=did)

    link = call(srv, "publish_draft", draft_id=did)
    assert f"{PAGES_URL}/approve?t=" in link and "finish_publish" in link
    assert "visibility CONNECTIONS" in link

    # Nothing is approved yet: finishing must do nothing.
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)
    assert fake_linkedin["publish"] == []

    tok, page, post = approve(web_client, link)
    assert page.status_code == 200
    assert "Publish this post to LinkedIn?" in page.text and "Three photos, one story" in page.text
    assert page.text.count("<img") == 3 and "Your connections only" in page.text
    assert "Approved" in post.text and post.status_code == 200
    assert "Approved" in web_client.get(f"/approve?t={tok}").text  # revisiting is harmless

    out = call(srv, "finish_publish", draft_id=did)
    assert out == "Published. LinkedIn ID: urn:li:share:123"
    text, images = fake_linkedin["publish"][0]
    assert text == "Three photos, one story"
    assert [alt for _, alt in images] == ["red", "green", "blue"]
    assert all(data.startswith(b"\xff\xd8\xff") for data, _ in images)

    row = q("SELECT status, linkedin_post_urn FROM drafts")[0]
    assert (row["status"], row["linkedin_post_urn"]) == ("published", "urn:li:share:123")
    assert q("SELECT count(*) AS n FROM draft_images")[0]["n"] == 0
    # The approval is single use.
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)
    assert web_client.get(f"/approve?t={tok}").status_code == 400
    assert len(fake_linkedin["publish"]) == 1


def test_text_only_and_single_image(srv, web_client, fake_linkedin, q):
    did = new_draft(srv, "Just words")
    _, _, _ = approve(web_client, call(srv, "publish_draft", draft_id=did))
    call(srv, "finish_publish", draft_id=did)
    assert fake_linkedin["publish"][0] == ("Just words", [])

    t = token_in(call(srv, "upload_image_link"))
    upload(web_client, t)
    did2 = new_draft(srv, "One photo", images=["latest"])
    approve(web_client, call(srv, "publish_draft", draft_id=did2))
    call(srv, "finish_publish", draft_id=did2)
    assert len(fake_linkedin["publish"][1][1]) == 1


def test_editing_after_link_voids_the_approval(srv, web_client, fake_linkedin):
    did = new_draft(srv, "Version one")
    link = call(srv, "publish_draft", draft_id=did)
    call(srv, "edit_draft", draft_id=did, text="Version two")
    t = token_in(link)
    page = web_client.get(f"/approve?t={t}")
    assert page.status_code == 400 and "changed" in page.text
    assert web_client.post("/approve", data={"t": t}).status_code == 400
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)

    # Approve, then edit, then finish: the approval is discarded.
    link2 = call(srv, "publish_draft", draft_id=did)
    approve(web_client, link2)
    call(srv, "edit_draft", draft_id=did, text="Version three")
    assert "changed" in call(srv, "finish_publish", draft_id=did)
    assert fake_linkedin["publish"] == []


def test_approval_expires_and_goes_stale(srv, web_client, fake_linkedin, q):
    did = new_draft(srv)
    link = call(srv, "publish_draft", draft_id=did)
    q("UPDATE approval_tickets SET expires_at = now() - interval '1 second'")
    assert web_client.get(f"/approve?t={token_in(link)}").status_code == 400
    assert web_client.post("/approve", data={"t": token_in(link)}).status_code == 400

    link = call(srv, "publish_draft", draft_id=did)
    approve(web_client, link)
    q("UPDATE approval_tickets SET approved_at = now() - interval '11 minutes' WHERE approved_at IS NOT NULL")
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)
    assert fake_linkedin["publish"] == []


def test_two_finish_calls_publish_once(srv, web_client, fake_linkedin, q):
    from fastmcp import Client

    did = new_draft(srv)
    approve(web_client, call(srv, "publish_draft", draft_id=did))

    async def go():
        async with Client(srv.mcp) as c:
            return await asyncio.gather(
                c.call_tool("finish_publish", {"draft_id": did}),
                c.call_tool("finish_publish", {"draft_id": did}),
            )

    results = [r.data for r in asyncio.run(go())]
    assert sum(r.startswith("Published") for r in results) == 1
    assert len(fake_linkedin["publish"]) == 1


def test_dry_run_publishes_nothing(srv, web_client, monkeypatch, q):
    import linkedin_client

    monkeypatch.setattr(linkedin_client, "DRY_RUN", True)
    did = new_draft(srv)
    link = call(srv, "publish_draft", draft_id=did)
    _, page, _ = approve(web_client, link)
    assert "Dry run is on" in page.text
    assert "DRY RUN is on" in call(srv, "finish_publish", draft_id=did)
    assert q("SELECT status FROM drafts")[0]["status"] == "draft"


def test_delete_post_flow(srv, web_client, fake_linkedin, q):
    did = new_draft(srv, "To be removed")
    approve(web_client, call(srv, "publish_draft", draft_id=did))
    call(srv, "finish_publish", draft_id=did)

    link = call(srv, "delete_linkedin_post", draft_id=did)
    assert "finish_delete_post" in link
    assert "No fresh approval" in call(srv, "finish_delete_post", draft_id=did)
    _, page, _ = approve(web_client, link)
    assert "Remove this post from LinkedIn?" in page.text and "Approve removal" in page.text
    assert call(srv, "finish_delete_post", draft_id=did) == "The post was removed from LinkedIn."
    assert fake_linkedin["delete"] == ["urn:li:share:123"]
    assert q("SELECT status FROM drafts")[0]["status"] == "deleted"
    # A publish approval can never be used to delete (and vice versa).
    did2 = new_draft(srv, "Other")
    approve(web_client, call(srv, "publish_draft", draft_id=did2))
    assert "No fresh approval" in call(srv, "finish_delete_post", draft_id=did2)


def test_failed_publish_can_be_retried(srv, web_client, fake_linkedin, monkeypatch, q):
    import linkedin_client

    async def boom(text, images=None):
        raise linkedin_client.LinkedInError("LinkedIn error 422: nope")

    good = linkedin_client.publish_post
    monkeypatch.setattr(linkedin_client, "publish_post", boom)
    did = new_draft(srv)
    approve(web_client, call(srv, "publish_draft", draft_id=did))
    assert "Publish failed: LinkedIn error 422" in call(srv, "finish_publish", draft_id=did)
    assert q("SELECT status FROM drafts")[0]["status"] == "failed"

    monkeypatch.setattr(linkedin_client, "publish_post", good)
    approve(web_client, call(srv, "publish_draft", draft_id=did))
    assert call(srv, "finish_publish", draft_id=did).startswith("Published")


# ------------------------------------------------------------------ plain drafts


def test_draft_crud(srv):
    did = new_draft(srv, "first")
    assert "first" in call(srv, "list_drafts")
    call(srv, "edit_draft", draft_id=did, text="second")
    assert "second" in call(srv, "read_draft", draft_id=did)
    assert call(srv, "delete_draft", draft_id=did) == "Draft deleted."
    assert call(srv, "list_drafts") == "No draft drafts found."
    with pytest.raises(ToolError):
        call(srv, "read_draft", draft_id="not-a-uuid")
    with pytest.raises(ToolError):
        call(srv, "create_draft", text="x" * 3001)


# ------------------------------------------------------------------ database


def test_least_privilege_role(db_url, pages, q):
    """The public pages app works with a role that cannot read the LinkedIn token."""
    import db
    from starlette.testclient import TestClient

    role = "lmcp_pages_" + secrets.token_hex(3)  # unique, so a real "linkedin_pages" role is never touched
    script = (
        (ROOT / "scripts" / "pages_role.sql").read_text()
        .replace("CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD", "rolepw")
        .replace("linkedin_pages", role)
    )
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(script)
    p = psycopg.conninfo.conninfo_to_dict(db_url)
    role_url = f"postgresql://{role}:rolepw@{p['host']}:{p.get('port', 5432)}/{p['dbname']}"

    d = asyncio.run(db.create_draft("Role test", []))
    ut = asyncio.run(db.create_upload_ticket(15, 3))
    at = asyncio.run(db.create_approval(d["id"], "publish", asyncio.run(__import__("ops").fingerprint(d["id"])), 15))

    old = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = role_url
    try:
        client = TestClient(pages.app)
        assert upload(client, ut).status_code == 200
        assert client.get(f"/upload?t={ut}").status_code == 200
        page = client.get(f"/approve?t={at}")
        assert page.status_code == 200 and "Role test" in page.text
        assert client.post("/approve", data={"t": at}).status_code == 200
    finally:
        os.environ["DATABASE_URL"] = old
    assert q("SELECT approved_at FROM approval_tickets")[0]["approved_at"] is not None

    with psycopg.connect(role_url) as c:
        for sql in (
            "SELECT * FROM linkedin_token",
            "SELECT data FROM images",
            "SELECT * FROM drafts",
            "UPDATE approval_tickets SET used_at = now()",
            "DELETE FROM images",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                c.execute(sql)
            c.rollback()
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute(f"DROP OWNED BY {role}")
        c.execute(f"DROP ROLE {role}")


OLD_SCHEMA = """
CREATE TABLE images (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), mime TEXT NOT NULL,
  data BYTEA NOT NULL, size_bytes INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE drafts (id UUID PRIMARY KEY DEFAULT gen_random_uuid(), content TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft', linkedin_post_urn TEXT, error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), published_at TIMESTAMPTZ);
CREATE TABLE draft_images (draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  image_id UUID NOT NULL REFERENCES images(id) ON DELETE CASCADE, position INT NOT NULL,
  alt_text TEXT NOT NULL DEFAULT '', PRIMARY KEY (draft_id, position));
CREATE TABLE linkedin_token (id INT PRIMARY KEY, access_token_enc TEXT NOT NULL, person_urn TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE upload_tickets (token_hash TEXT PRIMARY KEY, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
CREATE TABLE approval_tickets (token_hash TEXT PRIMARY KEY, draft_id UUID NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
  action TEXT NOT NULL, fingerprint TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
INSERT INTO drafts (id, content) VALUES ('11111111-1111-1111-1111-111111111111', 'old draft');
INSERT INTO upload_tickets VALUES ('abc', now() + interval '1 hour', NULL);
"""


def test_upgrade_from_previous_schema_keeps_data(db_url):
    admin = _swap_db(db_url, "postgres")
    name = "lmcp_old_" + secrets.token_hex(3)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = _swap_db(db_url, name)
    try:
        with psycopg.connect(url) as c:
            c.execute(OLD_SCHEMA)
        schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
        for _ in range(2):  # running it twice must be harmless
            with psycopg.connect(url) as c:
                c.execute(schema)
        with psycopg.connect(url) as c:
            assert c.execute("SELECT content FROM drafts").fetchone()[0] == "old draft"
            assert c.execute("SELECT uses_left FROM upload_tickets").fetchone()[0] == 1
            cols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'approval_tickets'")}
            assert {"approved_at", "visibility", "dry_run"} <= cols
            assert c.execute(
                "SELECT count(*) FROM information_schema.columns WHERE table_name='images' AND column_name='thumb'"
            ).fetchone()[0] == 1
    finally:
        with psycopg.connect(admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def test_old_tickets_are_purged(srv, q):
    import db

    d = asyncio.run(db.create_draft("x", []))
    asyncio.run(db.create_upload_ticket(15, 1))
    asyncio.run(db.create_approval(d["id"], "publish", "fp", 15))
    q("UPDATE upload_tickets SET expires_at = now() - interval '2 days'")
    q("UPDATE approval_tickets SET expires_at = now() - interval '2 days'")
    asyncio.run(db.create_upload_ticket(15, 1))  # a fresh one must survive
    asyncio.run(db.purge_old_tickets())
    assert q("SELECT count(*) AS n FROM approval_tickets")[0]["n"] == 0
    assert q("SELECT count(*) AS n FROM upload_tickets")[0]["n"] == 1
