# linkedin-mcp

A personal, self-hosted [MCP](https://modelcontextprotocol.io) server that lets an AI assistant (Claude)
draft, edit, publish and delete LinkedIn posts on **your own account**: text posts, one image, or several images.

You run your own copy with your own LinkedIn app and your own database. Nothing is shared with anyone else.

> **A human approves every post.** The assistant can only create an *approval link*. The post goes out
> only after **you** open that link in a browser and tap the button. There is no scheduling and no
> auto-posting. LinkedIn's API terms restrict automating posting, so you are responsible for using
> this tool in line with [LinkedIn's terms](https://www.linkedin.com/legal/l/api-terms-of-use).

## What it can do

| Tool | What it does |
|---|---|
| `create_draft`, `edit_draft`, `read_draft`, `list_drafts`, `delete_draft` | Manage drafts (text plus 0 to 9 images, alt text per image) |
| `upload_image_link`, `list_images` | One-time browser link to upload images (works on a phone) |
| `check_publish` | Dry check of a draft and the LinkedIn token. Posts nothing |
| `publish_draft` | Creates the approval link |
| `finish_publish` | Publishes, but only after you approved in the browser |
| `delete_linkedin_post`, `finish_delete_post` | Same two-step flow for removing a published post |
| `linkedin_status` | Token expiry, dry-run state |

## How it works

```
Claude ──► MCP server (login) ── publish_draft ──► approval link
                 ▲                                       │
                 │ finish_publish                        ▼
                 │                          your browser: upload / approve pages
                 └──────────── database (Postgres) ◄─────┘
```

* **MCP server** (`server.py`): the tools. It holds the encrypted LinkedIn token and is the only
  component that talks to LinkedIn.
* **Pages** (`pages/`): a tiny web app for the two things that need a browser: uploading images and tapping
  *Approve*. It can only store images and record an approval. It cannot post and cannot read the token.
* **Database**: Postgres (the free tier of [Neon](https://neon.tech) is enough).

Why two parts? Some MCP hosts (for example Prefect Horizon) require a login on **every** URL, so a browser
page on the same host shows `Bearer token required`. Hosting the two pages somewhere public (for example
Vercel) solves that. If your host has no such login, skip `pages/`: leave `PAGES_URL` empty and the MCP
server serves `/upload` and `/approve` itself (one tap then publishes immediately).

### Security model

* The LinkedIn **Client Secret never leaves your computer**. `auth.py` runs locally and stores only an
  encrypted access token (Fernet, key in `ENCRYPTION_KEY`) in the database.
* Links are one-time, short-lived and stored only as hashes.
* An approval is bound to the exact text and images. Edit the draft afterwards and the approval is void.
* Uploaded photos are shrunk in your browser and metadata (location) is removed.
* Optional: give the pages app a database role that cannot read the token (`scripts/pages_role.sql`).
* Never expose the MCP server without a login. Anyone who can call its tools can create approval links
  (they still cannot publish without your tap), and can read your drafts.

## Setup

### 1. Database
Create a Postgres database (Neon free tier works) and note the connection string.

### 2. LinkedIn app
1. Create an app at <https://www.linkedin.com/developers/apps> (it needs a LinkedIn Page, any Page will do).
2. Under **Products**, add **Share on LinkedIn** and **Sign In with LinkedIn using OpenID Connect**.
3. Under **Auth**, add the redirect URL `http://localhost:8080/callback`.

### 3. Configure and connect your account (on your own computer)
```bash
cp .env.example .env            # fill in DATABASE_URL, ENCRYPTION_KEY, LINKEDIN_CLIENT_ID/SECRET
pip install -r requirements.txt
python scripts/migrate.py       # creates the tables
python auth.py                  # opens LinkedIn, stores the encrypted token
```
Generate the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
and keep a copy somewhere safe: without it the stored token cannot be decrypted.
The token lasts about 60 days; run `python auth.py` again to renew it.

### 4. Try it locally
```bash
python server.py                       # http://127.0.0.1:8000/mcp (same-host mode)
python try_tools.py linkedin_status
```
Keep `DRY_RUN=1` and `POST_VISIBILITY=CONNECTIONS` for your first tests.

## Deploy

### MCP server (Prefect Horizon or any host that runs FastMCP)
* Entrypoint: `server.py:mcp`
* Environment variables: `DATABASE_URL`, `ENCRYPTION_KEY`, `LINKEDIN_VERSION`, `POST_VISIBILITY`,
  `DRY_RUN`, and `PAGES_URL` (the URL of the pages app, see below). Do **not** put `LINKEDIN_CLIENT_SECRET` on the server.
* Add the server URL in Claude: *Settings → Connectors → Add custom connector*.

### Pages (Vercel)
1. Import this repository in Vercel and set **Root Directory** to `pages`.
2. Environment variable: `DATABASE_URL` (ideally the restricted role from `scripts/pages_role.sql`).
3. Deploy, then set `PAGES_URL=https://your-project.vercel.app` on the MCP server and redeploy it.

Run `python scripts/migrate.py` once before the first deploy so the tables exist.

## Configuration

| Variable | Used by | Meaning |
|---|---|---|
| `DATABASE_URL` | server, pages | Postgres connection string |
| `ENCRYPTION_KEY` | server, `auth.py` | Fernet key for the stored token |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET` | `auth.py` only | Your LinkedIn app |
| `LINKEDIN_VERSION` | server | LinkedIn API version (`YYYYMM`). Change it if LinkedIn reports an inactive version |
| `POST_VISIBILITY` | server | `PUBLIC` or `CONNECTIONS` |
| `DRY_RUN` | server | `1` sends nothing to LinkedIn |
| `PAGES_URL` | server | Public URL of `pages/`. Empty = same-host mode |
| `BASE_URL`, `GOOGLE_CLIENT_ID/SECRET`, `OWNER_EMAIL` | server | Optional built-in Google login (for hosts without their own) |

## Tests

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/postgres pytest
```
The tests create and drop a throw-away database and use a fake LinkedIn, so nothing is posted.

## Limits

* Up to 9 images per post. Image posts, multi-image posts and deleting posts depend on LinkedIn's API,
  which can change. Check the LinkedIn docs if a call is rejected.
* Self-serve LinkedIn apps get no refresh token: renew every ~60 days.
* Text posts only support what LinkedIn's commenting format supports (`#hashtags` are left unescaped).

## License

MIT
