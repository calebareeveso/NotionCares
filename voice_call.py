"""
voice_call.py — Voice Call Support (Vonage + ElevenLabs)
───────────────────────────────────────────────────────────
Implements outbound voice calls via Vonage Voice API with ElevenLabs
Conversational AI handling the agent side. Audio is bridged between
Vonage (caller) and ElevenLabs (AI) via WebSocket.

Endpoints added to the webhook server:
  GET  /call/answer            — Vonage Answer URL (fallback NCCO)
  GET  /call/event             — Vonage WebSocket-leg events (query params)
  POST /call/event             — Vonage main-leg events (JSON body)
  WS   /call/media-stream      — Vonage audio WebSocket (bidirectional PCM)

MCP tool:
  call_user_await_response(phone_number, questions, timeout_seconds)
"""

import asyncio
import base64
import json
import logging
import os
import time
import uuid as uuid_mod

import httpx
import jwt
import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from dotenv import load_dotenv

import shared

load_dotenv()

log = logging.getLogger("notioncares.voice")

# ── Config ───────────────────────────────────────────────────────────────────

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
VONAGE_APPLICATION_ID = os.environ.get("VONAGE_APPLICATION_ID", "")
VONAGE_PRIVATE_KEY_PATH = os.environ.get("VONAGE_PRIVATE_KEY", "./private.key")
VONAGE_PHONE_NUMBER = os.environ.get("VONAGE_PHONE_NUMBER", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")

# ── FastAPI router ───────────────────────────────────────────────────────────

call_router = APIRouter()


# ── Vonage JWT ───────────────────────────────────────────────────────────────

def _generate_vonage_jwt() -> str:
    """Generate a short-lived JWT for Vonage API authentication."""
    with open(VONAGE_PRIVATE_KEY_PATH) as f:
        private_key = f.read()
    now = int(time.time())
    payload = {
        "application_id": VONAGE_APPLICATION_ID,
        "iat": now,
        "jti": str(uuid_mod.uuid4()),
        "exp": now + 3600,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


# ── ElevenLabs tool setup ────────────────────────────────────────────────────

async def ensure_end_call_tool() -> None:
    """Create 'end_call' client tool on ElevenLabs and assign it to the agent.

    Safe to call multiple times — skips if already set up.
    """
    if not ELEVENLABS_API_KEY or not ELEVENLABS_AGENT_ID:
        log.warning("ElevenLabs not configured — skipping end_call tool setup")
        return

    headers = {"xi-api-key": ELEVENLABS_API_KEY, "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient() as client:
            # 1. Get current agent config
            resp = await client.get(
                f"https://api.elevenlabs.io/v1/convai/agents/{ELEVENLABS_AGENT_ID}",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            agent = resp.json()

            current_tool_ids = (
                agent.get("conversation_config", {})
                .get("agent", {})
                .get("prompt", {})
                .get("tool_ids", [])
            )

            # 2. List workspace tools — look for existing end_call
            resp = await client.get(
                "https://api.elevenlabs.io/v1/convai/tools",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            tools_data = resp.json()
            tools_list = (
                tools_data
                if isinstance(tools_data, list)
                else tools_data.get("tools", [])
            )

            end_call_tool_id = None
            for tool in tools_list:
                if tool.get("tool_config", {}).get("name") == "end_call":
                    end_call_tool_id = tool.get("id")
                    break

            # 3. Create the tool if it doesn't exist
            if not end_call_tool_id:
                resp = await client.post(
                    "https://api.elevenlabs.io/v1/convai/tools",
                    headers=headers,
                    json={
                        "tool_config": {
                            "type": "client",
                            "name": "end_call",
                            "description": (
                                "End the phone call. Call this tool ONLY when "
                                "the user has answered ALL the questions you "
                                "were asked to ask. Thank the user before "
                                "calling this tool."
                            ),
                            "expects_response": False,
                            "response_timeout_secs": 10,
                            "parameters": {
                                "type": "object",
                                "properties": {},
                                "required": [],
                            },
                        }
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                result = resp.json()
                end_call_tool_id = result.get("id") or result.get("tool_id")
                log.info("Created end_call tool: %s", end_call_tool_id)
            else:
                log.info("end_call tool already exists: %s", end_call_tool_id)

            # 4. Assign tool to agent if not already assigned
            if end_call_tool_id and end_call_tool_id not in current_tool_ids:
                new_tool_ids = current_tool_ids + [end_call_tool_id]

                current_prompt = (
                    agent.get("conversation_config", {})
                    .get("agent", {})
                    .get("prompt", {})
                    .get("prompt", "")
                )
                updated_prompt = current_prompt
                if "end_call" not in current_prompt:
                    updated_prompt = (
                        current_prompt.rstrip()
                        + "\n\nIMPORTANT: When you have asked all your questions "
                        "and the user has answered them, you MUST call the "
                        "'end_call' tool to end the conversation. Thank the "
                        "user before ending."
                    )

                resp = await client.patch(
                    f"https://api.elevenlabs.io/v1/convai/agents/{ELEVENLABS_AGENT_ID}",
                    headers=headers,
                    json={
                        "conversation_config": {
                            "agent": {
                                "prompt": {
                                    "prompt": updated_prompt,
                                    "tool_ids": new_tool_ids,
                                }
                            }
                        }
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                log.info("Assigned end_call tool to agent and updated system prompt")
            else:
                log.info("end_call tool already assigned to agent")

            # 5. Ensure prompt override is allowed (required for per-call questions)
            prompt_override_allowed = (
                agent.get("platform_settings", {})
                .get("overrides", {})
                .get("conversation_config_override", {})
                .get("agent", {})
                .get("prompt", {})
                .get("prompt", False)
            )
            if not prompt_override_allowed:
                resp = await client.patch(
                    f"https://api.elevenlabs.io/v1/convai/agents/{ELEVENLABS_AGENT_ID}",
                    headers=headers,
                    json={
                        "platform_settings": {
                            "overrides": {
                                "conversation_config_override": {
                                    "agent": {
                                        "prompt": {
                                            "prompt": True,
                                        }
                                    }
                                }
                            }
                        }
                    },
                    timeout=15,
                )
                resp.raise_for_status()
                log.info("Enabled prompt override on agent")
            else:
                log.info("Prompt override already enabled")
    except Exception as e:
        log.error("Failed to ensure end_call tool: %s", e)


# ── ElevenLabs WebSocket connection ──────────────────────────────────────────

async def _connect_elevenlabs(
    questions: list[str],
) -> websockets.ClientConnection:
    """Connect to ElevenLabs Conversational AI via signed WebSocket URL.

    Overrides the system prompt per-conversation to inject the specific
    questions that need to be asked.
    """
    log.info("Getting ElevenLabs signed URL for agent=%s", ELEVENLABS_AGENT_ID)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.elevenlabs.io/v1/convai/conversation/get_signed_url"
            f"?agent_id={ELEVENLABS_AGENT_ID}",
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            timeout=10,
        )
        resp.raise_for_status()
        signed_url = resp.json()["signed_url"]

    # Disable compression — ElevenLabs expects raw frames, and the default
    # per-message-deflate in websockets v13+ can cause immediate disconnects.
    ws = await websockets.connect(
        signed_url,
        compression=None,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
    )
    log.info("ElevenLabs WebSocket connected")

    questions_text = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))

    init_msg = {
        "type": "conversation_initiation_client_data",
        "conversation_config_override": {
            "agent": {
                "prompt": {
                    "prompt": (
                        "You are a friendly health assistant making a phone "
                        "call to check in on the user. You must ask the "
                        "following questions one at a time. Wait for the "
                        "user's answer before moving to the next question. "
                        "Be warm, conversational, and natural.\n\n"
                        f"Questions to ask:\n{questions_text}\n\n"
                        "Start by greeting the user and letting them know "
                        "you're calling to check in. Once ALL questions have "
                        "been answered, thank them for their time and call "
                        "the 'end_call' tool to end the conversation. Do NOT "
                        "end the call until all questions are answered."
                    )
                }
            },
            "tts": {
                "output_format": "pcm_16000",
            },
        },
    }

    log.info("Sending conversation init to ElevenLabs")
    await ws.send(json.dumps(init_msg))

    return ws


# ── Vonage REST API ──────────────────────────────────────────────────────────

async def _make_vonage_call(phone_number: str, call_id: str) -> str:
    """Initiate an outbound call via Vonage Voice API. Returns the call UUID."""
    token = _generate_vonage_jwt()
    ws_url = (
        PUBLIC_BASE_URL.replace("https", "wss").replace("http", "ws")
        + f"/call/media-stream?call_id={call_id}"
    )

    log.info("Making Vonage call to %s, ws_url=%s", phone_number, ws_url)

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.nexmo.com/v1/calls",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "to": [{"type": "phone", "number": phone_number.lstrip("+")}],
                "from": {
                    "type": "phone",
                    "number": VONAGE_PHONE_NUMBER.lstrip("+"),
                },
                "ncco": [
                    {
                        "action": "connect",
                        "endpoint": [
                            {
                                "type": "websocket",
                                "uri": ws_url,
                                "content-type": "audio/l16;rate=16000",
                            }
                        ],
                    }
                ],
                "event_url": [f"{PUBLIC_BASE_URL}/call/event"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        call_uuid = result.get("uuid", "")

    log.info("Vonage call initiated — UUID: %s, call_id: %s", call_uuid, call_id)
    return call_uuid


# ── Vonage webhooks ──────────────────────────────────────────────────────────


@call_router.get("/call/answer")
async def call_answer(request: Request) -> JSONResponse:
    """Vonage Answer URL — returns an NCCO.

    For outbound calls the NCCO is sent inline with the API request, so this
    endpoint is only hit as a fallback or for inbound calls.
    """
    log.info("Vonage Answer URL hit (fallback)")
    return JSONResponse([{"action": "talk", "text": "Goodbye."}])


@call_router.get("/call/event")
async def call_event_get(request: Request) -> JSONResponse:
    """Vonage sends WebSocket-leg events as GET with query parameters."""
    status = request.query_params.get("status", "")
    call_uuid = request.query_params.get("uuid", "")
    log.info("Vonage GET event: status=%s uuid=%s", status, call_uuid)

    # Resolve pending call on terminal statuses
    terminal = {"completed", "busy", "timeout", "failed", "unanswered", "rejected", "disconnected"}
    if status in terminal:
        call_id = shared._call_uuid_to_id.get(call_uuid)
        if call_id and call_id in shared._pending_calls:
            state = shared._pending_calls[call_id]
            if not state["event"].is_set():
                if status not in ("completed", "disconnected"):
                    state["transcript"].append(f"[Call ended: {status}]")
                log.info("Call %s resolved via GET event (status=%s)", call_id, status)
                state["event"].set()

    return JSONResponse({"ok": True})


@call_router.post("/call/event")
async def call_event_post(request: Request) -> JSONResponse:
    """Vonage sends main-leg events as POST with JSON body."""
    body = await request.json()
    status = body.get("status", "")
    call_uuid = body.get("uuid", "")
    log.info("Vonage POST event: status=%s uuid=%s", status, call_uuid)

    # Resolve pending call on terminal statuses
    terminal = {"completed", "busy", "timeout", "failed", "unanswered", "rejected"}
    if status in terminal:
        call_id = shared._call_uuid_to_id.get(call_uuid)
        if call_id and call_id in shared._pending_calls:
            state = shared._pending_calls[call_id]
            if not state["event"].is_set():
                if status != "completed":
                    state["transcript"].append(f"[Call ended: {status}]")
                log.info("Call %s resolved via POST event (status=%s)", call_id, status)
                state["event"].set()

    return JSONResponse({"ok": True})


# ── Vonage media WebSocket ───────────────────────────────────────────────────


@call_router.websocket("/call/media-stream")
async def call_media_stream(websocket: WebSocket) -> None:
    """Vonage connects here when the call is answered.

    Bridges PCM audio bidirectionally between Vonage and ElevenLabs,
    collects transcripts, and handles the end_call tool callback.
    """
    await websocket.accept()

    call_id = websocket.query_params.get("call_id", "")
    log.info("Vonage WebSocket connected — call_id=%s", call_id)

    if not call_id or call_id not in shared._pending_calls:
        log.warning("Unknown call_id=%s — closing WebSocket", call_id)
        await websocket.close()
        return

    state = shared._pending_calls[call_id]
    questions = state["questions"]
    transcript: list[str] = state["transcript"]
    event: asyncio.Event = state["event"]

    # Connect to ElevenLabs
    try:
        el_ws = await _connect_elevenlabs(questions)
    except Exception as e:
        log.error("Failed to connect to ElevenLabs: %s", e)
        transcript.append(f"[Error: Failed to connect to ElevenLabs — {e}]")
        event.set()
        await websocket.close()
        return

    log.info("ElevenLabs connected for call_id=%s", call_id)

    # ── Vonage → ElevenLabs (caller audio) ───────────────────────────────

    async def vonage_to_elevenlabs() -> None:
        audio_chunks_sent = 0
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    log.info("Vonage WS disconnect frame received")
                    break
                if msg.get("bytes"):
                    # Binary PCM audio from caller → base64 JSON to ElevenLabs
                    audio_b64 = base64.b64encode(msg["bytes"]).decode()
                    try:
                        await el_ws.send(
                            json.dumps({"user_audio_chunk": audio_b64})
                        )
                        audio_chunks_sent += 1
                        if audio_chunks_sent == 1:
                            log.info("First audio chunk forwarded to ElevenLabs")
                    except Exception as e:
                        log.warning("Failed to send audio to ElevenLabs: %s", e)
                        break
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                        log.info("Vonage WS text event: %s", data.get("event", data))
                    except json.JSONDecodeError:
                        pass
        except WebSocketDisconnect:
            log.info("Vonage WebSocket disconnected (sent %d audio chunks)", audio_chunks_sent)
        except Exception as e:
            log.info("vonage_to_elevenlabs ended: %s (sent %d chunks)", e, audio_chunks_sent)

    # ── ElevenLabs → Vonage (agent audio + events) ──────────────────────

    async def elevenlabs_to_vonage() -> None:
        audio_chunks_received = 0
        try:
            async for raw in el_ws:
                data = json.loads(raw) if isinstance(raw, str) else json.loads(raw)
                msg_type = data.get("type")

                if msg_type == "audio":
                    audio_b64 = data.get("audio_event", {}).get("audio_base_64")
                    if audio_b64:
                        try:
                            await websocket.send_bytes(
                                base64.b64decode(audio_b64)
                            )
                            audio_chunks_received += 1
                            if audio_chunks_received == 1:
                                log.info("First audio chunk sent to Vonage")
                        except Exception:
                            break

                elif msg_type == "interruption":
                    log.info("ElevenLabs: interruption")
                    try:
                        await websocket.send_text(
                            json.dumps({"action": "clear"})
                        )
                    except Exception:
                        pass

                elif msg_type == "ping":
                    eid = data.get("ping_event", {}).get("event_id")
                    if eid:
                        await el_ws.send(
                            json.dumps({"type": "pong", "event_id": eid})
                        )

                elif msg_type == "user_transcript":
                    text = (
                        data.get("user_transcription_event", {})
                        .get("user_transcript", "")
                    )
                    if text:
                        transcript.append(f"User: {text}")
                        log.info("Transcript — User: %s", text)

                elif msg_type == "agent_response":
                    text = (
                        data.get("agent_response_event", {})
                        .get("agent_response", "")
                    )
                    if text:
                        transcript.append(f"Agent: {text}")
                        log.info("Transcript — Agent: %s", text)

                elif msg_type == "client_tool_call":
                    tool = data.get("client_tool_call", {})
                    tool_name = tool.get("tool_name")
                    log.info("ElevenLabs client_tool_call: %s", tool_name)
                    if tool_name == "end_call":
                        log.info("end_call tool invoked — ending call session")
                        # Acknowledge the tool call
                        await el_ws.send(
                            json.dumps(
                                {
                                    "type": "client_tool_result",
                                    "tool_call_id": tool["tool_call_id"],
                                    "result": "Call ended successfully.",
                                    "is_error": False,
                                }
                            )
                        )
                        # Resolve the pending call
                        event.set()
                        return

                elif msg_type == "conversation_initiation_metadata":
                    meta = data.get(
                        "conversation_initiation_metadata_event", {}
                    )
                    log.info(
                        "ElevenLabs ready — in=%s out=%s conv_id=%s",
                        meta.get("user_input_audio_format"),
                        meta.get("agent_output_audio_format"),
                        meta.get("conversation_id"),
                    )

                else:
                    # Log any unhandled message types for debugging
                    log.info("ElevenLabs message type=%s", msg_type)

        except websockets.exceptions.ConnectionClosedError as e:
            log.warning(
                "ElevenLabs connection closed with ERROR: code=%s reason=%s",
                e.code,
                e.reason,
            )
        except websockets.exceptions.ConnectionClosedOK as e:
            log.info(
                "ElevenLabs connection closed normally: code=%s reason=%s",
                e.code,
                e.reason,
            )
        except Exception as e:
            log.error("ElevenLabs handler error: %s (%s)", e, type(e).__name__)

        log.info(
            "elevenlabs_to_vonage ended (received %d audio chunks)",
            audio_chunks_received,
        )

    # ── Run both directions concurrently ─────────────────────────────────

    v2e = asyncio.create_task(vonage_to_elevenlabs())
    e2v = asyncio.create_task(elevenlabs_to_vonage())

    done, pending = await asyncio.wait(
        [v2e, e2v], return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    # Ensure the MCP tool is unblocked on any exit path (e.g. early hangup)
    if not event.is_set():
        event.set()

    # Cleanup
    try:
        await el_ws.close()
    except Exception:
        pass
    try:
        await websocket.close()
    except Exception:
        pass

    log.info("Media stream ended for call_id=%s", call_id)
