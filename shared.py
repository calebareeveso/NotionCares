"""
shared.py
─────────
State that is shared between the MCP server, Telegram webhook receiver,
and voice call handler — all run inside the same asyncio event loop in
main.py, so plain module-level dicts are safe and sufficient.

Telegram
────────
_pending maps:
    chat_id (int)  →  (event: asyncio.Event, holder: list[str])

Voice Calls
───────────
_pending_calls maps:
    call_id (str)  →  {
        "event":      asyncio.Event,
        "transcript": list[str],
        "questions":  list[str],
        "call_uuid":  str,
    }

_call_uuid_to_id maps:
    vonage_call_uuid (str)  →  call_id (str)
"""

import asyncio
from typing import Any

# ── Telegram ─────────────────────────────────────────────────────────────────
# chat_id → (asyncio.Event, reply_holder: list[str])
_pending: dict[int, tuple[asyncio.Event, list[str]]] = {}

# ── Voice Calls ──────────────────────────────────────────────────────────────
# call_id → {"event", "transcript", "questions", "call_uuid"}
_pending_calls: dict[str, dict[str, Any]] = {}

# vonage_call_uuid → call_id  (for correlating Vonage events)
_call_uuid_to_id: dict[str, str] = {}
