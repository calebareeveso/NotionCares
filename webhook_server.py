"""
webhook_server.py — Webhook & WebSocket Server
────────────────────────────────────────────────
Handles all inbound webhooks and WebSocket connections:

  /webhook              — Telegram message updates
  /setup                — Register Telegram webhook with Telegram API
  /call/event           — Vonage call lifecycle events
  /call/media-stream    — Vonage ↔ ElevenLabs audio WebSocket bridge
  /call/check           — Verify voice call setup (Vonage + ElevenLabs)
  /health               — Health check

Runs on WEBHOOK_PORT (default 8002) inside the same asyncio event loop
as the MCP server (see main.py).
"""

import logging
import os
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import fitbit
import shared
from voice_call import call_router

load_dotenv()

log = logging.getLogger("notioncares.webhook")

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
PUBLIC_BASE  = os.environ["PUBLIC_BASE_URL"].rstrip("/")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="NotionCares Webhook Server",
    description="Telegram webhooks, Vonage call events, and ElevenLabs media bridge.",
    docs_url=None,
)

# Mount voice call endpoints (/call/event, /call/media-stream)
app.include_router(call_router)


@app.get("/fitbit/authorize")
async def fitbit_authorize():
    try:
        url = fitbit.get_authorize_url()
        return {"authorize_url": url}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/fitbit/callback")
async def fitbit_callback(code: str = "", state: str = ""):
    if not code or not state:
        return JSONResponse({"error": "Missing code or state parameter"}, status_code=400)
    try:
        tokens = await fitbit.exchange_code(code, state)
        return {"user_id": tokens.get("user_id"), "scopes": tokens.get("scope", "").split(" ")}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/fitbit/status")
async def fitbit_status():
    return {"connected": fitbit.is_connected()}


@app.post("/webhook")
async def telegram_webhook(request: Request) -> JSONResponse:
    """
    Telegram calls this endpoint for every message sent to the bot.

    If there is a pending send_message_await_response call waiting for a
    reply (tracked in shared._pending), this handler resolves it with the
    user's message text, unblocking the MCP tool and returning the reply
    to the Notion Custom Agent.

    Telegram expects a 200 OK back quickly — we do no heavy work here.
    """
    body: dict[str, Any] = await request.json()

    # Telegram sends either `message` or `edited_message`
    message = body.get("message") or body.get("edited_message")

    if not message:
        return JSONResponse({"ok": True})

    chat_id: int = message["chat"]["id"]
    text: str = message.get("text", "").strip()

    if not text:
        # Ignore non-text updates (stickers, photos, etc.)
        return JSONResponse({"ok": True})

    log.info("Received message from chat_id=%s: %r", chat_id, text)

    # Resolve any pending await_response call for this chat
    if chat_id in shared._pending:
        event, holder = shared._pending[chat_id]
        holder.append(text)
        event.set()
        log.info("Resolved pending reply for chat_id=%s", chat_id)
    else:
        log.info(
            "Message from chat_id=%s — no pending tool call waiting for a reply.",
            chat_id,
        )

    return JSONResponse({"ok": True})


@app.get("/setup")
async def setup_webhook() -> JSONResponse:
    """
    Registers this server's /webhook URL with Telegram.

    Call this ONCE after every new deployment or ngrok restart:
        GET http://localhost:8002/setup

    Telegram will then POST all bot updates to {PUBLIC_BASE_URL}/webhook.

    NOTE: If PUBLIC_BASE_URL in .env is wrong or not HTTPS, Telegram will
    reject the registration. Check the response body for Telegram's error.
    """
    webhook_url = f"{PUBLIC_BASE}/webhook"
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TELEGRAM_API}/setWebhook",
            json={
                "url": webhook_url,
                "allowed_updates": ["message"],
                "drop_pending_updates": True,  # ignore messages sent while offline
            },
            timeout=10,
        )
        result = resp.json()

    log.info("setWebhook → %s: %s", webhook_url, result)
    return JSONResponse({"webhook_registered": webhook_url, "telegram": result})


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "notioncares-webhook-server",
        "pending_telegram_awaits": len(shared._pending),
        "pending_voice_calls": len(shared._pending_calls),
    })


@app.get("/call/check")
async def call_check() -> JSONResponse:
    """Verify that Vonage + ElevenLabs voice call setup is correct.

    Checks:
      1. All required env vars are set
      2. Vonage private key file exists and is readable
      3. Vonage JWT generation works
      4. ElevenLabs agent is reachable and has end_call tool assigned
      5. PUBLIC_BASE_URL is reachable (self-check)
    """
    import os
    from voice_call import (
        ELEVENLABS_API_KEY,
        ELEVENLABS_AGENT_ID,
        VONAGE_APPLICATION_ID,
        VONAGE_PRIVATE_KEY_PATH,
        VONAGE_PHONE_NUMBER,
        PUBLIC_BASE_URL as VOICE_PUBLIC_URL,
        _generate_vonage_jwt,
    )

    checks: dict[str, dict] = {}

    # 1. Env vars
    env_vars = {
        "ELEVENLABS_API_KEY": ELEVENLABS_API_KEY,
        "ELEVENLABS_AGENT_ID": ELEVENLABS_AGENT_ID,
        "VONAGE_APPLICATION_ID": VONAGE_APPLICATION_ID,
        "VONAGE_PRIVATE_KEY": VONAGE_PRIVATE_KEY_PATH,
        "VONAGE_PHONE_NUMBER": VONAGE_PHONE_NUMBER,
        "PUBLIC_BASE_URL": VOICE_PUBLIC_URL,
    }
    missing = [k for k, v in env_vars.items() if not v]
    checks["env_vars"] = {
        "ok": len(missing) == 0,
        "missing": missing,
    }

    # 2. Private key file
    key_exists = os.path.isfile(VONAGE_PRIVATE_KEY_PATH)
    checks["vonage_private_key"] = {
        "ok": key_exists,
        "path": VONAGE_PRIVATE_KEY_PATH,
    }

    # 3. Vonage JWT
    try:
        token = _generate_vonage_jwt()
        checks["vonage_jwt"] = {"ok": True, "token_prefix": token[:20] + "..."}
    except Exception as e:
        checks["vonage_jwt"] = {"ok": False, "error": str(e)}

    # 4. ElevenLabs agent + end_call tool
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.elevenlabs.io/v1/convai/agents/{ELEVENLABS_AGENT_ID}",
                headers={"xi-api-key": ELEVENLABS_API_KEY},
                timeout=10,
            )
            resp.raise_for_status()
            agent = resp.json()
            tool_ids = (
                agent.get("conversation_config", {})
                .get("agent", {})
                .get("prompt", {})
                .get("tool_ids", [])
            )
            prompt = (
                agent.get("conversation_config", {})
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", "")
            )
            has_end_call_in_prompt = "end_call" in prompt
            checks["elevenlabs_agent"] = {
                "ok": True,
                "agent_name": agent.get("name"),
                "tool_ids": tool_ids,
                "has_end_call_tool": len(tool_ids) > 0,
                "prompt_mentions_end_call": has_end_call_in_prompt,
            }
    except Exception as e:
        checks["elevenlabs_agent"] = {"ok": False, "error": str(e)}

    # 5. Vonage webhook URLs (what to put in dashboard)
    vonage_urls = {
        "answer_url_or_ncco": "Not needed — NCCO is sent inline with API call",
        "event_url": f"{VOICE_PUBLIC_URL}/call/event",
        "media_websocket": f"{VOICE_PUBLIC_URL.replace('https', 'wss').replace('http', 'ws')}/call/media-stream",
    }
    checks["vonage_webhook_urls"] = vonage_urls

    all_ok = all(
        c.get("ok", True) for c in checks.values() if isinstance(c.get("ok"), bool)
    )

    return JSONResponse(
        {
            "all_ok": all_ok,
            "checks": checks,
            "instructions": {
                "vonage_dashboard": (
                    "In your Vonage Application settings "
                    f"(App ID: {VONAGE_APPLICATION_ID}), set: "
                    f"Event URL → {VOICE_PUBLIC_URL}/call/event (POST)"
                ),
                "note": (
                    "Answer URL is NOT needed in the dashboard — the NCCO "
                    "is sent inline when the call is initiated via API. "
                    "The media WebSocket URL is also embedded in the NCCO."
                ),
            },
        },
        status_code=200 if all_ok else 500,
    )
