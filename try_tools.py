import asyncio
import json
import sys

from fastmcp import Client


async def main():
    name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    async with Client("http://localhost:8000/mcp") as c:
        r = await c.call_tool(name, args)
        print(r.data)


asyncio.run(main())