"""Attack-style and safety tests. Nothing here can reach LinkedIn (fake client, DRY_RUN checks)."""
import asyncio
import re
import threading

import pytest
from conftest import ROOT, call, jpeg, token_in, upload
from fastmcp.exceptions import ToolError
from test_flow import approve, fake_linkedin, new_draft  # noqa: F401  (fixture re-used)

import linkedin_client as lc


# ------------------------------------------------------------- text formatting
def test_formatting_hashtags_breaks_and_escaping():
    out = lc.format_commentary(
        "Big news!\r\n\r\nShipped (finally) a *tool* #AI #MachineLearning | a_b @Ali\n#1 C#dev site.com/#x  "
    )
    assert out == (
        "Big news!\n\nShipped \\(finally\\) a \\*tool\\* {hashtag|\\#|AI} {hashtag|\\#|MachineLearning} "
        "\\| a\\_b \\@Ali\n\\#1 C\\#dev site.com/\\#x"
    )


def test_mention_only_with_strict_urn():
    ok = lc.format_commentary("Thanks @[Ali Khan](urn:li:person:abc_12)!")
    assert "@[Ali Khan](urn:li:person:abc_12)" in ok and "\\@" not in ok
    bad = lc.format_commentary("@[Evil](urn:li:person:x) y)(javascript:1)")
    assert lc.format_commentary("@[A](https://x.com)").startswith("\\@\\[A\\]\\(")
    assert "{" not in lc.format_commentary("{hashtag|x} {evil|y}").replace("\\{", "")
    assert bad  # no crash


def test_hashtag_template_cannot_be_injected():
    # a user-typed template is escaped, so LinkedIn shows it literally
    assert lc.format_commentary("{hashtag|\\#|Fake}").startswith("\\{hashtag\\|")


def test_markdown_and_overlong_text_rejected(srv):
    with pytest.raises(ToolError):
        call(srv, "create_draft", text="**bold** claim")
    with pytest.raises(ToolError):
        call(srv, "create_draft", text="# Heading\nbody")
    with pytest.raises(ToolError):
        call(srv, "create_draft", text="x" * 3001)
    # escaping makes it longer than 3000 even though raw is shorter
    with pytest.raises(ToolError):
        call(srv, "create_draft", text="(" * 2000)
    call(srv, "create_draft", text="Good post\n\nline two #Hiring #AI")  # accepted


def test_null_byte_and_weird_unicode_do_not_crash(srv):
    for text in ["a\x00b", "emoji \U0001F680 \u202e rtl", "\ufeffbom"]:
        try:
            call(srv, "create_draft", text=text)
        except ToolError:
            pass  # a clean refusal is fine; anything else would raise a different error type


# ------------------------------------------------------------- dry run safety
def test_check_publish_never_uploads_in_dry_run(srv, monkeypatch, q):
    monkeypatch.setattr(lc, "DRY_RUN", True)
    seen = []

    async def boom(*a, **k):
        seen.append(1)

    monkeypatch.setattr(lc, "test_image_upload", boom)

    async def fake_verify():
        return "urn:li:person:me"

    monkeypatch.setattr(lc, "verify_token", fake_verify)
    t = token_in(call(srv, "upload_image_link"))
    from conftest import upload as up
    from starlette.testclient import TestClient
    import importlib.util
    spec = importlib.util.spec_from_file_location("pg2", ROOT / "pages" / "api" / "index.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    up(TestClient(m.app), t)
    did = new_draft(srv, images=["latest"])
    out = call(srv, "check_publish", draft_id=did, upload_images=True)
    assert "SKIP image upload" in out and not seen


def test_client_functions_refuse_in_dry_run(monkeypatch):
    monkeypatch.setattr(lc, "DRY_RUN", True)
    for coro in (lc.publish_post("x"), lc.delete_post("urn:li:share:1"), lc.test_image_upload([(b"x", "")])):
        with pytest.raises(lc.LinkedInError):
            asyncio.run(coro)


def test_dry_run_approval_cannot_become_live_post(srv, web_client, fake_linkedin, monkeypatch, q):
    monkeypatch.setattr(lc, "DRY_RUN", True)
    did = new_draft(srv)
    link = call(srv, "publish_draft", draft_id=did)
    approve(web_client, link)                        # user approves a link marked DRY RUN
    monkeypatch.setattr(lc, "DRY_RUN", False)        # server is switched to live afterwards
    out = call(srv, "finish_publish", draft_id=did)
    assert "different settings" in out and fake_linkedin["publish"] == []
    assert q("SELECT status FROM drafts")[0]["status"] == "draft"


def test_visibility_change_voids_approval(srv, web_client, fake_linkedin, monkeypatch):
    did = new_draft(srv)
    approve(web_client, call(srv, "publish_draft", draft_id=did))    # approved as CONNECTIONS
    monkeypatch.setattr(lc, "VISIBILITY", "PUBLIC")
    out = call(srv, "finish_publish", draft_id=did)
    assert "different settings" in out and fake_linkedin["publish"] == []


def test_draft_edited_between_check_and_claim_is_not_published(srv, fake_linkedin, q):
    import db, ops
    did = new_draft(srv, text="original")
    fp = asyncio.run(ops.fingerprint(did))
    call(srv, "edit_draft", draft_id=did, text="tampered after approval")
    out = asyncio.run(ops.do_publish(did, fp))
    assert "changed after approval" in out and fake_linkedin["publish"] == []
    assert q("SELECT status FROM drafts")[0]["status"] == "draft"


def test_published_text_is_formatted(srv, web_client, fake_linkedin):
    did = new_draft(srv, text="Hello (world)\n\n#AI")
    approve(web_client, call(srv, "publish_draft", draft_id=did))
    call(srv, "finish_publish", draft_id=did)
    # fake publish_post receives the raw text; the real one formats it
    assert fake_linkedin["publish"][0][0] == "Hello (world)\n\n#AI"
    assert lc.build_body("urn:li:person:x", "Hello (world)\n\n#AI")["commentary"] == \
        "Hello \\(world\\)\n\n{hashtag|\\#|AI}"


# ------------------------------------------------------------- attacks on the pages app
EVIL_TOKENS = ["' OR '1'='1", "'; DROP TABLE drafts;--", "../../etc/passwd", "%00", "A" * 5000, "\u202e", "null"]


@pytest.mark.parametrize("tok", EVIL_TOKENS)
def test_pages_reject_hostile_tokens(web_client, q, tok):
    for path in ("/upload", "/approve"):
        r = web_client.get(path, params={"t": tok})
        assert r.status_code == 400 and "does not work" in r.text
    assert upload(web_client, tok).status_code == 400
    assert web_client.post("/approve", data={"t": tok}).status_code == 400
    assert q("SELECT count(*) AS n FROM drafts")[0]["n"] == 0  # tables still there


def test_unknown_paths_and_methods(web_client):
    for path in ("/", "/admin", "/api/index", "/.env", "/upload/../approve", "/approve/x", "/server.py", "/schema.sql"):
        r = web_client.get(path)
        assert r.status_code in (404, 400, 405) and "DATABASE" not in r.text and "postgres" not in r.text.lower()
    assert web_client.put("/upload").status_code in (404, 405)
    assert web_client.delete("/approve").status_code in (404, 405)
    r = web_client.get("/api/index", params={"path": "../approve"})
    assert r.status_code == 404


def test_tokens_are_not_interchangeable(srv, web_client):
    up_t = token_in(call(srv, "upload_image_link"))
    did = new_draft(srv)
    ap_t = token_in(call(srv, "publish_draft", draft_id=did))
    assert web_client.get(f"/approve?t={up_t}").status_code == 400
    assert web_client.get(f"/upload?t={ap_t}").status_code == 400
    assert upload(web_client, ap_t).status_code == 400


def test_xss_in_draft_text_is_escaped_everywhere(srv, web_client):
    did = new_draft(srv, text='<script>alert(1)</script> <img src=x onerror=alert(2)> "quoted"')
    link = call(srv, "publish_draft", draft_id=did)
    page = web_client.get(f"/approve?t={token_in(link)}")
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text
    assert "<img src=x" not in page.text
    csp = page.headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"


def test_approve_replay_and_wrong_state(srv, web_client, fake_linkedin):
    did = new_draft(srv)
    t, _page, r = approve(web_client, call(srv, "publish_draft", draft_id=did))
    assert r.status_code == 200
    call(srv, "finish_publish", draft_id=did)
    assert fake_linkedin["publish"].__len__() == 1
    # replay everything: approval link, finish
    assert web_client.get(f"/approve?t={t}").status_code == 400
    assert web_client.post("/approve", data={"t": t}).status_code == 400
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)
    assert len(fake_linkedin["publish"]) == 1


def test_finish_without_user_tap_does_nothing(srv, fake_linkedin):
    did = new_draft(srv)
    call(srv, "publish_draft", draft_id=did)       # link exists, user never taps
    assert "No fresh approval" in call(srv, "finish_publish", draft_id=did)
    assert "No fresh approval" in call(srv, "finish_delete_post", draft_id=did)
    assert fake_linkedin["publish"] == [] and fake_linkedin["delete"] == []


def test_cannot_delete_unpublished_or_publish_deleted(srv, fake_linkedin):
    did = new_draft(srv)
    assert "not a published post" in call(srv, "delete_linkedin_post", draft_id=did)
    call(srv, "delete_draft", draft_id=did)
    assert "cannot be published" in call(srv, "publish_draft", draft_id=did)


def test_bad_ids_are_refused(srv):
    for bad in ["x", "", "1' OR 1=1", "00000000-0000-0000-0000-000000000000"]:
        for tool in ("read_draft", "delete_draft", "publish_draft", "finish_publish", "delete_linkedin_post"):
            try:
                out = call(srv, tool, draft_id=bad)
                assert isinstance(out, str)
            except ToolError:
                pass


# ------------------------------------------------------------- concurrency / "many users" load
def test_parallel_uploads_respect_ticket_limit(srv, web_client, pages, q):
    from starlette.testclient import TestClient
    t = token_in(call(srv, "upload_image_link"))            # 20 uses
    results = []

    def one():
        c = TestClient(pages.app)
        results.append(upload(c, t).status_code)

    threads = [threading.Thread(target=one) for _ in range(40)]
    [x.start() for x in threads]; [x.join() for x in threads]
    assert results.count(200) == 20 and results.count(400) == 20
    assert q("SELECT count(*) AS n FROM images")[0]["n"] == 20


def test_parallel_finish_publishes_once_and_many_drafts(srv, web_client, fake_linkedin, q):
    ids = [new_draft(srv, text=f"post {i}") for i in range(15)]
    for did in ids:
        approve(web_client, call(srv, "publish_draft", draft_id=did))

    async def go():
        from fastmcp import Client
        async with Client(srv.mcp) as c:
            await asyncio.gather(*[c.call_tool("finish_publish", {"draft_id": d}) for d in ids for _ in range(3)])
    asyncio.run(go())
    assert len(fake_linkedin["publish"]) == 15
    assert q("SELECT count(*) AS n FROM drafts WHERE status = 'published'")[0]["n"] == 15


# ------------------------------------------------------------- repo hygiene (public GitHub)
def test_no_secrets_in_tracked_files():
    bad = re.compile(
        r"postgres(?:ql)?://[^\s:/@]+:(?!pw\b|pass\b|PASSWORD|PASS\b|rolepw|\*|%s)[^\s@]{6,}@"
        r"|npg_[A-Za-z0-9]{10,}|ENCRYPTION_KEY\s*=\s*[A-Za-z0-9_\-]{43}=?\s*$|AQ[A-Za-z0-9_\-]{80,}"
    )
    for f in ROOT.rglob("*"):
        if f.is_file() and not any(p in f.parts for p in (".venv", "venv", "__pycache__", ".git", ".pytest_cache")) \
                and f.suffix in {".py", ".md", ".json", ".sql", ".txt", ".example", ".ini", ""}:
            text = f.read_text(errors="ignore")
            assert not bad.search(text), f"possible secret in {f}"
    assert ".env" in (ROOT / ".gitignore").read_text()


# ------------------------------------------------------------- the real HTTP client against a fake LinkedIn
def test_real_client_payloads_against_mock_linkedin(srv, monkeypatch, q):
    import httpx
    from datetime import datetime, timedelta, timezone
    from crypto import encrypt

    secret = "SECRET-ACCESS-TOKEN"
    q("INSERT INTO linkedin_token (id, access_token_enc, person_urn, expires_at) VALUES (1, %s, %s, %s)",
      (encrypt(secret), "urn:li:person:me", datetime.now(timezone.utc) + timedelta(days=30)))
    assert secret not in q("SELECT access_token_enc FROM linkedin_token")[0]["access_token_enc"]
    monkeypatch.setattr(lc, "DRY_RUN", False)
    monkeypatch.setattr(lc, "VISIBILITY", "CONNECTIONS")
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, str(req.url), req.content))
        assert req.url.host in ("api.linkedin.com", "up.example")      # only LinkedIn is ever contacted
        if "initializeUpload" in str(req.url):
            n = len([s for s in seen if "initializeUpload" in s[1]])
            return httpx.Response(200, json={"value": {"uploadUrl": "https://up.example/u", "image": f"urn:li:image:{n}"}})
        if req.url.host == "up.example":
            return httpx.Response(201)
        if req.method == "POST" and req.url.path == "/rest/posts":
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:77"})
        if req.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(500)

    real = httpx.AsyncClient
    monkeypatch.setattr(lc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    import json
    urn = asyncio.run(lc.publish_post("Hi #AI (x)", [(b"a", "alt1"), (b"b", ""), (b"c", "alt3")]))
    assert urn == "urn:li:share:77"
    post = next(json.loads(c) for m, u, c in seen if m == "POST" and u.endswith("/rest/posts"))
    assert post["commentary"] == "Hi {hashtag|\\#|AI} \\(x\\)"
    assert post["visibility"] == "CONNECTIONS" and post["author"] == "urn:li:person:me"
    assert [i["id"] for i in post["content"]["multiImage"]["images"]] == ["urn:li:image:1", "urn:li:image:2", "urn:li:image:3"]
    assert post["content"]["multiImage"]["images"][0]["altText"] == "alt1" and "altText" not in post["content"]["multiImage"]["images"][1]
    asyncio.run(lc.delete_post("urn:li:share:77"))
    assert seen[-1][0] == "DELETE" and "urn%3Ali%3Ashare%3A77" in seen[-1][1]


def test_linkedin_status_and_check_publish_full_path(srv, monkeypatch, q):
    from datetime import datetime, timedelta, timezone
    from crypto import encrypt

    monkeypatch.setattr(lc, "DRY_RUN", True)
    assert "not connected" in call(srv, "linkedin_status") and "DRY RUN ON" in call(srv, "linkedin_status")
    q("INSERT INTO linkedin_token (id, access_token_enc, person_urn, expires_at) VALUES (1, %s, %s, %s)",
      (encrypt("t"), "urn:li:person:me", datetime.now(timezone.utc) + timedelta(days=30)))
    assert "about 29 days" in call(srv, "linkedin_status") or "about 30 days" in call(srv, "linkedin_status")
    monkeypatch.setattr(lc, "DRY_RUN", False)
    assert "DRY RUN OFF" in call(srv, "linkedin_status")

    async def fake_verify():
        return "urn:li:person:me"

    monkeypatch.setattr(lc, "verify_token", fake_verify)
    did = new_draft(srv, text="Check me #AI")
    out = call(srv, "check_publish", draft_id=did)
    assert "OK   LinkedIn accepted the token" in out and "{hashtag|\\#|AI}" in out
