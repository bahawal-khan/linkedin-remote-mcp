<div align="center">

# LinkedIn MCP

**Write, edit and publish LinkedIn posts by chatting with Claude, from your phone or your desktop.**

Drafts · Images · Approval before posting · Self-hosted

![Python](https://img.shields.io/badge/python-3.11+-3776ab?logo=python&logoColor=white)
![FastMCP](https://img.shields.io/badge/FastMCP-4.x-7c5cff)
![Postgres](https://img.shields.io/badge/Postgres-Neon-27c39a?logo=postgresql&logoColor=white)
![Tests](https://img.shields.io/badge/tests-47%20passing-2ea44f)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

<img src="docs/architecture.svg" alt="How the pieces fit together" width="760">

</div>

## What you can do

| | |
|---|---|
| **Drafts** | Save, read, edit and delete drafts |
| **Images** | Upload from your phone, attach 1 to 9 images to a post |
| **Formatting** | Line breaks kept, `#hashtags` become real hashtags |
| **Publish** | Text only, one image, or a multi-image post |
| **Delete** | Remove a published post from LinkedIn |
| **Safe by default** | Nothing is posted until you tap Approve, and `DRY_RUN` is on unless you turn it off |

13 tools in total. Everything runs on your own accounts, so nobody else can see your drafts.

## Using it

Just talk to Claude:

> "Write a post about my new project, with hashtags, and save it as a draft."
> "Give me an upload link." → open it on your phone, pick your photos
> "Attach the 3 latest images to that draft."
> "Publish it." → open the link Claude gives you, check the post, tap **Approve**
> "I approved." → Claude publishes
> "Delete that post from LinkedIn." → approve the removal the same way

## Safety

- Claude can **never** publish or delete by itself. It only creates a one-time approval link that you tap.
- Links expire in 15 minutes and work once. If the draft changes after you approve, the approval is void.
- Your LinkedIn token is encrypted in the database. The app secret stays on your own computer.
- Images are resized and stripped of location data before they are stored.
- Server and pages are tested against fake logins, injection, replay and heavy parallel use.

## Set it up (about 30 minutes)

1. **Database:** create a free Postgres on [Neon](https://neon.tech) and copy the connection string.
2. **LinkedIn app:** create one at [developers.linkedin.com](https://www.linkedin.com/developers/apps), add the products *Share on LinkedIn* and *Sign In with LinkedIn using OpenID Connect*, and set the redirect URL to `http://localhost:8080/callback`.
3. **On your computer:**
   ```bash
   cp .env.example .env        # fill in DATABASE_URL, ENCRYPTION_KEY, LinkedIn client ID and secret
   pip install -r requirements.txt
   python scripts/migrate.py   # create the tables
   python auth.py              # log in to LinkedIn once
   ```
   Make the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep a copy.
4. **MCP server:** deploy `server.py:mcp` on [Prefect Horizon](https://horizon.prefect.io) (keep its authentication ON) with `DATABASE_URL`, `ENCRYPTION_KEY`, `DRY_RUN`, `POST_VISIBILITY`, `PAGES_URL`. Do not add the LinkedIn client secret there.
5. **Pages app (for phone upload and approve):** import this repo in Vercel with Root Directory `pages`, add `DATABASE_URL`, turn off *Vercel Authentication*, then set `PAGES_URL` on the MCP server.
6. **Claude:** *Settings → Connectors → Add custom connector* with your server URL.

Start with `DRY_RUN=1` and `POST_VISIBILITY=CONNECTIONS`. When a test looks right, set `DRY_RUN=0`.

## Good to know

- The LinkedIn token lasts about 60 days. Run `python auth.py` again to renew it (`linkedin_status` shows the days left).
- Tagging a person needs their LinkedIn ID: `@[Full Name](urn:li:person:ID)`. Without it the name stays plain text.
- LinkedIn does not render Markdown, so `**bold**` and `# headings` are refused.
- Never share your `.env`, your database URL or approval links.

## Settings

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `ENCRYPTION_KEY` | Encrypts the stored LinkedIn token |
| `DRY_RUN` | `1` (default) sends nothing to LinkedIn, `0` posts for real |
| `POST_VISIBILITY` | `CONNECTIONS` or `PUBLIC` |
| `PAGES_URL` | URL of the Vercel pages app (empty = same-host mode) |
| `LINKEDIN_VERSION` | LinkedIn API version, change if LinkedIn says it is inactive |

## Tests

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/postgres pytest
```

No test ever contacts LinkedIn.

## License

MIT, made by Bahawal Khan.
