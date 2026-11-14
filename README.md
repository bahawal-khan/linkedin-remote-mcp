<h1 align="center">LinkedIn MCP</h1>

<p align="center">
  <b>Write, edit and publish LinkedIn posts by chatting with your AI assistant.</b><br>
  Works from your phone or desktop with any MCP client.
</p>

<p align="center">
  <img alt="python" src="https://img.shields.io/badge/python-3.11+-3776ab?logo=python&logoColor=white">
  <img alt="FastMCP" src="https://img.shields.io/badge/FastMCP-4.x-7c5cff">
  <img alt="Postgres" src="https://img.shields.io/badge/Postgres-Neon-27c39a?logo=postgresql&logoColor=white">
  <img alt="tests" src="https://img.shields.io/badge/tests-47%20passing-2ea44f">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

<p align="center">
  <picture>
    <source media="(max-width: 640px)" srcset="docs/pipeline-mobile.svg">
    <img src="docs/pipeline.svg" alt="Animated 3D pipeline: MCP client, MCP server, Postgres, pages app and LinkedIn" width="100%">
  </picture>
</p>

---

## Features

| Feature | What it does |
|---|---|
| Drafts | Save, read, edit and delete drafts |
| Images | Upload from your phone and attach 1 to 9 images to a post |
| Formatting | Keeps line breaks and turns `#hashtags` into real hashtags |
| Publishing | Text only, one image, or a multi-image post |
| Deleting | Remove a published post from LinkedIn |
| Safety | Nothing is posted until you tap Approve. Dry run is on by default |

13 tools in total. It runs on your own accounts, so nobody else can see your drafts.

## How to use

Talk to your assistant in plain language:

```text
"Write a post about my new project, with hashtags, and save it as a draft."
"Give me an upload link."                  -> open it on your phone and pick photos
"Attach the 3 latest images to the draft."
"Publish it."                              -> open the link, check the post, tap Approve
"I approved."                              -> the post goes live
"Delete that post from LinkedIn."          -> approve the removal the same way
```

## Safety

- The assistant can never publish or delete by itself. It only creates a one-time approval link that you tap.
- Links expire after 15 minutes and work once. If the draft changes after you approve, the approval is void.
- Your LinkedIn token is stored encrypted. The app secret stays on your own computer.
- Images are resized and their location data is removed before storing.
- Tested against fake logins, injection, replay and heavy parallel use.

## Setup

About 30 minutes.

1. **Database.** Create a free Postgres on [Neon](https://neon.tech) and copy the connection string.
2. **LinkedIn app.** Create one at [LinkedIn Developers](https://www.linkedin.com/developers/apps). Add the products *Share on LinkedIn* and *Sign In with LinkedIn using OpenID Connect*. Set the redirect URL to `http://localhost:8080/callback`.
3. **Your computer.**
   ```bash
   cp .env.example .env          # fill in the values
   pip install -r requirements.txt
   python scripts/migrate.py     # create the tables
   python auth.py                # log in to LinkedIn once
   ```
   Create the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` and keep a copy.
4. **MCP server.** Deploy `server.py:mcp` on [Prefect Horizon](https://horizon.prefect.io) and keep its authentication on. Set `DATABASE_URL`, `ENCRYPTION_KEY`, `DRY_RUN`, `POST_VISIBILITY` and `PAGES_URL`. Never add the LinkedIn client secret there.
5. **Pages app.** Import this repo in Vercel with Root Directory `pages`. Set `DATABASE_URL` and turn off *Vercel Authentication*. Then put the Vercel URL in `PAGES_URL` on the server.
6. **Your MCP client.** Add the server URL as a custom connector.

Start with `DRY_RUN=1` and `POST_VISIBILITY=CONNECTIONS`. When a test looks right, set `DRY_RUN=0`.

## Settings

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Postgres connection string |
| `ENCRYPTION_KEY` | Encrypts the stored LinkedIn token |
| `DRY_RUN` | `1` (default) sends nothing to LinkedIn. `0` posts for real |
| `POST_VISIBILITY` | `CONNECTIONS` or `PUBLIC` |
| `PAGES_URL` | URL of the Vercel pages app. Empty means same-host mode |
| `LINKEDIN_VERSION` | LinkedIn API version. Change it if LinkedIn says it is inactive |

## Good to know

- The LinkedIn token lasts about 60 days. Run `python auth.py` again to renew it. `linkedin_status` shows the days left.
- Tagging a person needs their LinkedIn ID, written as `@[Full Name](urn:li:person:ID)`. Without it the name stays plain text.
- LinkedIn does not render Markdown, so drafts with `**bold**` or `# headings` are refused.
- Never share your `.env`, your database URL or an approval link.

## Tests

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/postgres pytest
```

No test ever contacts LinkedIn.

## License

MIT. Made by Bahawal Khan.
