"""
main.py — NotionCares Telegram & Phone Call MCP
─────────────────────────────────────────────────
Runs two servers in the same asyncio event loop:

  Port MCP_PORT (default 8001) — the MCP server
    → Notion Custom Agents connect here
    → URL to paste into Notion: https://your-url.com/mcp

  Port WEBHOOK_PORT (default 8002) — the webhook / WebSocket server
    → Telegram POSTs incoming messages to /webhook
    → Vonage POSTs call events to /call/event
    → Vonage connects media WebSocket to /call/media-stream
    → Register Telegram webhook: GET http://localhost:8002/setup
    → Verify voice call setup:   GET http://localhost:8002/call/check
    → PUBLIC_BASE_URL in .env must point to this server (HTTPS)

Usage
─────
  pip install -r requirements.txt
  cp .env.example .env          # fill in all values

  ngrok http 8002               # in another terminal
  # copy the ngrok https URL into .env → PUBLIC_BASE_URL

  python main.py

  curl http://localhost:8002/setup       # register Telegram webhook
  curl http://localhost:8002/call/check  # verify voice call setup
"""

import asyncio
import logging
import os

import uvicorn
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(name)-28s  %(levelname)s  %(message)s",
)
log = logging.getLogger("notioncares.main")

MCP_PORT     = int(os.getenv("MCP_PORT", "8001"))
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8002"))


async def run_servers() -> None:
    # Import here so all modules share the same shared state dicts
    from mcp_server import mcp
    from webhook_server import app as webhook_app
    from voice_call import ensure_end_call_tool

    # FastMCP's streamable-http ASGI app
    # The /mcp path is served by uvicorn wrapping FastMCP's built-in ASGI app.
    mcp_asgi = mcp.streamable_http_app()

    mcp_config = uvicorn.Config(
        app=mcp_asgi,
        host="0.0.0.0",
        port=MCP_PORT,
        log_level="info",
        log_config=None,  # use our logging config above
    )

    webhook_config = uvicorn.Config(
        app=webhook_app,
        host="0.0.0.0",
        port=WEBHOOK_PORT,
        log_level="info",
        log_config=None,
    )

    mcp_server     = uvicorn.Server(mcp_config)
    webhook_server = uvicorn.Server(webhook_config)

    log.info("Starting MCP server on port %d  →  Notion connects at /mcp", MCP_PORT)
    log.info("Starting webhook server on port %d  →  register via GET /setup", WEBHOOK_PORT)

    # Run both servers concurrently in the same event loop.
    # asyncio.gather keeps them both alive — if one crashes the other stops too.
    # ensure_end_call_tool() runs once at startup to set up the ElevenLabs agent.
    await asyncio.gather(
        mcp_server.serve(),
        webhook_server.serve(),
        ensure_end_call_tool(),
    )


if __name__ == "__main__":
    asyncio.run(run_servers())
