"""
mcp_server.py — NotionCares Telegram & Phone Call MCP
─────────────────────────────────────────
This is the MCP server. Notion Custom Agents connect to it at:
    http://localhost:8001/mcp   (local dev)
    https://your-url.com/mcp   (production)

It exposes two tools:

    send_message(text)
        Fire-and-forget. Sends a Telegram message to the configured user.
        Use for one-way notifications — "Your report is ready", reminders
        that don't need a reply.

    send_message_await_response(text, timeout_seconds)
        Sends a message AND waits for the user to reply.
        Returns the user's reply as a plain string.
        This is the key tool — used by NutritionBot, FitnessLogger,
        MedReminder, and any agent that needs input before continuing.

Transport
─────────
Uses MCP's streamable-http transport (the current standard for HTTP-based
MCP servers as of the MCP spec revision 2025-06-18). This is what Notion's
"Custom MCP server" connection type expects.

The MCP endpoint is served at the /mcp path by uvicorn (see main.py).
"""

import asyncio
import logging
import os
import uuid as uuid_mod

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

import shared
from voice_call import _make_vonage_call

load_dotenv()

log = logging.getLogger("notioncares.mcp")

BOT_TOKEN    = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID      = int(os.environ["TELEGRAM_CHAT_ID"])
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── FastMCP server ────────────────────────────────────────────────────────────

# The MCP SDK includes DNS-rebinding protection that validates the incoming
# request `Host` header and (by default) only allows localhost. That breaks
# remote clients hitting your public `/mcp` URL behind a reverse proxy.
#
# For local dev we keep protection; for public URLs we disable it unless
# explicitly overridden via `MCP_DISABLE_HOST_CHECK`.
_disable_host_check = os.getenv("MCP_DISABLE_HOST_CHECK", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
_public_base = os.getenv("PUBLIC_BASE_URL", "").strip()
_looks_like_local = any(s in _public_base for s in ("localhost", "127.0.0.1", "::1"))

# Enable protection only when PUBLIC_BASE_URL looks local.
_enable_dns_rebinding_protection = (
    not _disable_host_check and _looks_like_local
)

mcp = FastMCP(
    name="notioncares-mcp",
    json_response=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=_enable_dns_rebinding_protection
    ),
)


# ── Telegram send helpers ─────────────────────────────────────────────────────

async def _send(text: str) -> None:
    """POST a message to the Telegram Bot API. Fire and forget."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        resp.raise_for_status()
    log.info("Sent Telegram message to chat_id=%s", CHAT_ID)


# ── MCP Tools ─────────────────────────────────────────────────────────────────

@mcp.tool()
async def send_message(text: str) -> str:
    """
    Send a Telegram message to the NotionCares user. Does NOT wait for a reply.

    Use this for one-way notifications, e.g.:
      - "Your medication report is ready. Open Notion to read it."
      - "Good morning! Here's your health snapshot for yesterday."

    HTML formatting is supported in the text parameter:
      <b>bold</b>  <i>italic</i>  <code>monospace</code>

    Args:
        text: The message to send. Plain text or basic HTML.

    Returns:
        Confirmation that the message was sent.
    """
    await _send(text)
    return "Message sent successfully."


@mcp.tool()
async def send_message_await_response(
    text: str,
    timeout_seconds: int = 900,
) -> str:
    """
    Send a Telegram message to the user and WAIT for their reply.

    This is the primary tool for all interactive check-ins in NotionCares:
      - NutritionBot:   "What did you have for breakfast?"
      - FitnessLogger:  "What did you train today? Or was it a rest day?"
      - MedReminder:    "Time for your Vitamin D — reply YES to confirm."

    The tool blocks until the user replies or the timeout expires.
    If the user doesn't reply in time, raise a TimeoutError so the calling
    agent can fall back to the Phone Call MCP.

    Args:
        text:            The question or message to send the user.
        timeout_seconds: How long to wait for a reply (default: 900 = 15 min).
                         For hackathon demos, use 30–60 seconds.

    Returns:
        The user's reply as a plain string, ready to parse or act on.

    Raises:
        TimeoutError: If the user doesn't reply within timeout_seconds.
                      The calling agent should treat this as a signal to
                      escalate — e.g. trigger the Phone Call MCP.
    """
    event: asyncio.Event = asyncio.Event()
    holder: list[str] = []
    shared._pending[CHAT_ID] = (event, holder)

    try:
        await _send(text)
        log.info(
            "Waiting up to %ds for reply from chat_id=%s", timeout_seconds, CHAT_ID
        )
        await asyncio.wait_for(event.wait(), timeout=float(timeout_seconds))
        reply = holder[0]
        log.info("Got reply from chat_id=%s: %r", CHAT_ID, reply)
        return reply

    except asyncio.TimeoutError:
        raise TimeoutError(
            f"User did not reply within {timeout_seconds} seconds. "
            "Consider escalating to the Phone Call MCP."
        )
    finally:
        shared._pending.pop(CHAT_ID, None)


@mcp.tool()
async def call_user_await_response(
    phone_number: str,
    questions: list[str],
    timeout_seconds: int = 300,
) -> str:
    """
    Make an outbound voice call to the user and ask a list of questions.

    Uses Vonage to place the call and ElevenLabs Conversational AI as the
    voice agent. The agent asks each question one at a time, waits for the
    user's answer, and calls the 'end_call' tool when all questions are
    answered. Returns the full conversation transcript.

    Use this when the user did not reply to a Telegram message and you need
    to escalate to a phone call, or when voice interaction is preferred.

    Args:
        phone_number: The phone number to call (e.g. "+447930002899").
        questions:    List of questions to ask (e.g. ["What did you eat?",
                      "Did you take your medication?"]).
        timeout_seconds: Max time to wait for the call to complete
                         (default: 300 = 5 minutes).

    Returns:
        The conversation transcript as a string, with each line prefixed
        by "User:" or "Agent:".

    Raises:
        TimeoutError: If the call doesn't complete within timeout_seconds.
    """
    call_id = str(uuid_mod.uuid4())
    event = asyncio.Event()

    shared._pending_calls[call_id] = {
        "event": event,
        "transcript": [],
        "questions": questions,
        "call_uuid": "",
    }

    try:
        call_uuid = await _make_vonage_call(phone_number, call_id)
        shared._pending_calls[call_id]["call_uuid"] = call_uuid
        shared._call_uuid_to_id[call_uuid] = call_id

        log.info(
            "Waiting up to %ds for call %s to complete", timeout_seconds, call_id
        )
        await asyncio.wait_for(event.wait(), timeout=float(timeout_seconds))

        transcript_lines = shared._pending_calls[call_id]["transcript"]
        transcript = "\n".join(transcript_lines)
        log.info("Call %s completed — %d transcript lines", call_id, len(transcript_lines))
        return transcript or "Call completed but no transcript was recorded."

    except asyncio.TimeoutError:
        raise TimeoutError(
            f"Voice call did not complete within {timeout_seconds} seconds."
        )
    finally:
        call_uuid = shared._pending_calls.get(call_id, {}).get("call_uuid", "")
        shared._pending_calls.pop(call_id, None)
        if call_uuid:
            shared._call_uuid_to_id.pop(call_uuid, None)
