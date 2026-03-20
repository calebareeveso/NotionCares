# NotionCares

MCP server that lets Notion Custom Agents communicate with users via Telegram messages and Vonage voice calls (with ElevenLabs Conversational AI).

## Architecture

Two servers run concurrently in a **single asyncio event loop**, sharing state via plain Python dicts in `shared.py`:

- **MCP Server** (`mcp_server.py`, port 8001) — exposes 3 tools to Notion agents via FastMCP streamable-http transport
- **Webhook Server** (`webhook_server.py`, port 8002) — FastAPI app receiving Telegram updates and Vonage call events

**Entry point:** `python main.py` starts both servers via `asyncio.gather()`.

### MCP Tools

| Tool | Purpose |
|------|---------|
| `send_message` | Fire-and-forget Telegram message |
| `send_message_await_response` | Send message + block until user replies (asyncio.Event) |
| `call_user_await_response` | Outbound Vonage call with ElevenLabs AI asking questions, returns transcript |

### Coordination Pattern

MCP tools store an `asyncio.Event` in `shared._pending` (Telegram) or `shared._pending_calls` (voice). Webhook handlers resolve these events when replies/call completions arrive, unblocking the MCP tool to return the result to Notion.

## Key Files

| File | Role |
|------|------|
| `main.py` | Entry point, launches both servers |
| `mcp_server.py` | MCP tool definitions (send_message, await_response, call) |
| `webhook_server.py` | Telegram webhook, /setup, /health, /call/check endpoints |
| `voice_call.py` | Vonage + ElevenLabs integration, WebSocket audio bridge |
| `shared.py` | Shared async state (pending events, call tracking dicts) |
| `get_chat_id.py` | One-time setup utility to find Telegram chat ID |
| `scripts/test-mcp.sh` | CLI smoke tests for Telegram and voice call tools |
| `scripts/mcp_session.sh` | Shared bash helpers for MCP JSON-RPC session management |

## Environment Variables

Required in `.env`:
- `TELEGRAM_BOT_TOKEN` — from @BotFather
- `TELEGRAM_CHAT_ID` — from `python get_chat_id.py`
- `PUBLIC_BASE_URL` — HTTPS URL (ngrok for local dev), no trailing slash

Optional (voice calls):
- `ELEVENLABS_API_KEY`, `ELEVENLABS_AGENT_ID`
- `VONAGE_APPLICATION_ID`, `VONAGE_PRIVATE_KEY` (path to .key file), `VONAGE_PHONE_NUMBER`

Optional:
- `MCP_PORT` (default 8001), `WEBHOOK_PORT` (default 8002)
- `MCP_DISABLE_HOST_CHECK=1` — disable DNS rebinding protection for public URLs

## Development

```bash
pip install -r requirements.txt
python main.py                          # start both servers
curl http://localhost:8002/setup        # register Telegram webhook
curl http://localhost:8002/health       # check status
curl http://localhost:8002/call/check   # verify Vonage + ElevenLabs setup
```

### Testing

No automated test suite. Use CLI smoke tests:
```bash
./scripts/test-mcp.sh telegram    # test send_message_await_response
./scripts/test-mcp.sh call        # test call_user_await_response
```

Override MCP endpoint: `MCP_URL=http://localhost:8001/mcp ./scripts/test-mcp.sh telegram`

## Conventions

- All HTTP calls use `httpx` (async), not the `python-telegram-bot` SDK
- Vonage auth uses RS256 JWT generated in `voice_call.py:_generate_vonage_jwt()`
- ElevenLabs `end_call` tool is auto-created on startup via `ensure_end_call_tool()`
- Audio bridge in `call_media_stream()` handles bidirectional PCM between Vonage WebSocket and ElevenLabs WebSocket
- Python 3.11+
