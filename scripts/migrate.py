"""Create or upgrade the database tables. Run once before the first deploy:  python scripts/migrate.py

The MCP server does the same thing on startup, so this is only needed when the pages app is
deployed first or when you want to prepare the database by hand.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import db  # noqa: E402

asyncio.run(db.init_db())
print("Database is up to date.")
