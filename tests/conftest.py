"""Test setup.

The tests need a PostgreSQL server. Point TEST_DATABASE_URL at any database on it (for example
postgresql://postgres:pw@127.0.0.1:5432/postgres). A throw-away database is created for the run and
dropped afterwards. Without TEST_DATABASE_URL every test is skipped.
"""
import asyncio
import importlib.util
import io
import os
import re
import secrets
import sys
import urllib.parse
from pathlib import Path

import psycopg
import pytest
from cryptography.fernet import Fernet
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PAGES_URL = "https://pages.example.test"


def _swap_db(url: str, name: str) -> str:
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse(p._replace(path="/" + name))


@pytest.fixture(scope="session")
def db_url():
    admin = os.environ.get("TEST_DATABASE_URL")
    if not admin:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = "lmcp_test_" + secrets.token_hex(4)
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'CREATE DATABASE "{name}"')
    url = _swap_db(admin, name)
    os.environ["DATABASE_URL"] = url
    os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    os.environ["PAGES_URL"] = PAGES_URL
    os.environ.pop("GOOGLE_CLIENT_ID", None)
    with psycopg.connect(url) as c:
        c.execute((ROOT / "schema.sql").read_text(encoding="utf-8"))
    yield url
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(autouse=True)
def clean(db_url):
    with psycopg.connect(db_url) as c:
        c.execute(
            "TRUNCATE images, drafts, draft_images, approval_tickets, upload_tickets, linkedin_token CASCADE"
        )


@pytest.fixture(scope="session")
def srv(db_url):
    import server  # imported late, after the environment is ready

    return server


@pytest.fixture(scope="session")
def pages(db_url):
    spec = importlib.util.spec_from_file_location("pages_index", ROOT / "pages" / "api" / "index.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def web_client(pages):
    from starlette.testclient import TestClient

    return TestClient(pages.app)


@pytest.fixture()
def q(db_url):
    def run(sql, params=()):
        with psycopg.connect(db_url, row_factory=psycopg.rows.dict_row) as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else cur.rowcount

    return run


def call(server, name, **args):
    """Call an MCP tool in memory and return its text result."""
    from fastmcp import Client

    async def go():
        async with Client(server.mcp) as c:
            return (await c.call_tool(name, args)).data

    return asyncio.run(go())


def token_in(text: str) -> str:
    return re.search(r"[?&]t=([A-Za-z0-9_\-]+)", text).group(1)


def jpeg(w=64, h=48, color=(200, 30, 30)) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


def upload(client, token, data=None, thumb=None, **extra):
    files = {
        "file": ("image.jpg", data if data is not None else jpeg(), "image/jpeg"),
        "thumb": ("thumb.jpg", thumb if thumb is not None else jpeg(16, 12), "image/jpeg"),
    }
    return client.post("/upload", data={"t": token, **extra}, files=files)
