import json
import os
import secrets
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import psycopg
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")

from crypto import encrypt  # noqa: E402

CLIENT_ID = os.environ["LINKEDIN_CLIENT_ID"]
CLIENT_SECRET = os.environ["LINKEDIN_CLIENT_SECRET"]
REDIRECT = "http://localhost:8080/callback"
SCOPE = "openid profile w_member_social"
state = secrets.token_urlsafe(16)
result = {}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if q.get("state", [""])[0] == state and "code" in q:
            result["code"] = q["code"][0]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Done. Ab terminal par wapis jayen.")

    def log_message(self, *a):
        pass


url = "https://www.linkedin.com/oauth/v2/authorization?" + urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT,
        "state": state,
        "scope": SCOPE,
    }
)
print("Browser khul raha hai. Na khule to yeh link kholen:\n", url)
webbrowser.open(url)
HTTPServer(("localhost", 8080), Handler).handle_request()

if "code" not in result:
    raise SystemExit("Login fail hua ya state match nahi hui")

tok = httpx.post(
    "https://www.linkedin.com/oauth/v2/accessToken",
    data={
        "grant_type": "authorization_code",
        "code": result["code"],
        "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    },
).json()
if "access_token" not in tok:
    raise SystemExit("Token nahi mila. Client ID/Secret aur Redirect URL check karen.")

info = httpx.get(
    "https://api.linkedin.com/v2/userinfo",
    headers={"Authorization": f"Bearer {tok['access_token']}"},
).json()

with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
    conn.execute((BASE / "schema.sql").read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO linkedin_token (id, access_token_enc, person_urn, expires_at) "
        "VALUES (1, %s, %s, now() + make_interval(secs => %s)) "
        "ON CONFLICT (id) DO UPDATE SET access_token_enc = EXCLUDED.access_token_enc, "
        "person_urn = EXCLUDED.person_urn, expires_at = EXCLUDED.expires_at, updated_at = now()",
        (
            encrypt(tok["access_token"]),
            f"urn:li:person:{info['sub']}",
            float(tok.get("expires_in", 5184000)),
        ),
    )
print("Token encrypted ho kar database me save ho gaya.")