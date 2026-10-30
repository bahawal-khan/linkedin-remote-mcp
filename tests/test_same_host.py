"""Same-host mode: the MCP server itself serves /upload and /approve (no PAGES_URL).

Use this when your host does not put a login in front of every URL (local use, your own server)."""
import importlib
import re

from conftest import PAGES_URL, call, jpeg, token_in
from starlette.testclient import TestClient


def test_same_host_mode_publishes_on_button_tap(srv, monkeypatch, q):
    import linkedin_client

    published = []

    async def publish_post(text, images=None):
        published.append((text, len(images or [])))
        return "urn:li:share:999"

    monkeypatch.setattr(linkedin_client, "publish_post", publish_post)
    monkeypatch.setattr(linkedin_client, "DRY_RUN", False)
    monkeypatch.delenv("PAGES_URL")
    monkeypatch.setenv("BASE_URL", "http://localhost:8000")
    server = importlib.reload(srv)
    try:
        assert not server.SPLIT
        client = TestClient(server.mcp.http_app())

        t = token_in(call(server, "upload_image_link"))
        assert "Upload images" in client.get(f"/upload?t={t}").text
        files = [("file", (f"p{i}.jpg", jpeg(color=(i * 40, 10, 10)), "image/jpeg")) for i in range(2)]
        assert "2 image(s) saved" in client.post("/upload", data={"t": t}, files=files).text
        assert client.post("/upload", data={"t": t}, files=files).status_code == 400  # single use

        out = call(server, "create_draft", text="Same host post", images=["latest:2"])
        did = re.search(r"ID: ([0-9a-f-]{36})", out).group(1)
        link = call(server, "publish_draft", draft_id=did)
        assert "http://localhost:8000/approve?t=" in link and "finish_publish" not in link
        a = token_in(link)
        assert "Yes, publish" in client.get(f"/approve?t={a}").text
        assert "Published" in client.post("/approve", data={"t": a}).text
        assert published == [("Same host post", 2)]
        assert q("SELECT status FROM drafts")[0]["status"] == "published"
        assert client.post("/approve", data={"t": a}).status_code == 400  # one tap only
    finally:
        monkeypatch.setenv("PAGES_URL", PAGES_URL)
        importlib.reload(srv)
