# Sprint: Fitbit Sleep Integration — OAuth + MCP Tool + Check-in

> **STATUS: NOT STARTED**
> Files: `fitbit.py` (new) + `mcp_server.py` + `webhook_server.py` + `shared.py` + `main.py`
> Branch: `feature/fitbit-sleep`
> Source: Fitbit Web API v1.2 Sleep endpoints
> Split into two phases: S1 (OAuth + data fetch, unblocked) and S2 (MCP tool + Notion check-in, depends on S1)

---

## Why Split

OAuth token management and Fitbit API plumbing have zero dependency on the MCP tool layer. Ship the Fitbit module first, then wire it into the MCP server and agent prompts.

---

## Phase S1: Fitbit OAuth + Sleep Data Fetch (UNBLOCKED)

### What exists

**MCP server** (`mcp_server.py`): Three tools registered on `mcp` (a `FastMCP` instance named `"notioncares-mcp"`):
- `send_message(text: str) -> str`
- `send_message_await_response(text: str, timeout_seconds: int = 900) -> str`
- `call_user_await_response(phone_number: str, questions: list[str], timeout_seconds: int = 300) -> str`

**Webhook server** (`webhook_server.py`): FastAPI app named `app` with routes: `POST /webhook`, `GET /setup`, `GET /health`, `GET /call/check`. Mounts `call_router` from `voice_call.py`.

**Shared state** (`shared.py`): Three module-level dicts:
- `_pending: dict[int, tuple[asyncio.Event, list[str]]]` — Telegram reply coordination
- `_pending_calls: dict[str, dict[str, Any]]` — voice call coordination
- `_call_uuid_to_id: dict[str, str]` — Vonage UUID mapping

**Entry point** (`main.py`): `run_servers()` launches MCP (port `MCP_PORT`, default 8001) and webhook (port `WEBHOOK_PORT`, default 8002) servers via `asyncio.gather()` alongside `ensure_end_call_tool()`.

**No Fitbit integration exists** — no OAuth flow, no sleep endpoints, no token storage.

### Execution

```
Batch S1 (sequential — S1-A then S1-B):
  ├─ S1-A: Fitbit module — OAuth helpers + sleep data fetch (fitbit.py)
  └─ S1-B: OAuth callback route + token file + webhook wiring (webhook_server.py)
```

---

### S1-A — Fitbit Module: `fitbit.py` (new file)

#### Step 1: Create `fitbit.py` with OAuth helpers

Create `fitbit.py` at project root (same level as `mcp_server.py`). This module handles all Fitbit API communication.

**Imports and constants:**

```python
import asyncio
import hashlib
import base64
import json
import logging
import os
import secrets
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("notioncares.fitbit")

FITBIT_CLIENT_ID = os.environ.get("FITBIT_CLIENT_ID", "")
FITBIT_CLIENT_SECRET = os.environ.get("FITBIT_CLIENT_SECRET", "")
FITBIT_REDIRECT_URL = os.environ.get("FITBIT_REDIRECT_URL", "").rstrip("/")

FITBIT_AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE = "https://api.fitbit.com"

TOKEN_FILE = Path(__file__).parent / ".fitbit_tokens.json"
```

**Why a file for tokens:** The app runs as a single-process async server with no database. A JSON file is the simplest persistence that survives restarts. The file is gitignored (`.fitbit_tokens.json` matches the existing `.env.*` pattern in `.gitignore`).

#### Step 2: PKCE helpers

```python
def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge
```

#### Step 3: Token persistence

```python
def _load_tokens() -> dict | None:
    """Load stored Fitbit tokens from disk."""
    if not TOKEN_FILE.exists():
        return None
    try:
        data = json.loads(TOKEN_FILE.read_text())
        return data if data.get("access_token") else None
    except (json.JSONDecodeError, KeyError):
        return None


def _save_tokens(tokens: dict) -> None:
    """Persist Fitbit tokens to disk."""
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    log.info("Fitbit tokens saved to %s", TOKEN_FILE)
```

#### Step 4: Authorization URL builder

```python
# Module-level dict to hold PKCE state between authorize and callback
_oauth_state: dict[str, str] = {}


def get_authorize_url() -> str:
    """Build the Fitbit OAuth2 authorization URL with PKCE."""
    verifier, challenge = _generate_pkce()
    state = secrets.token_urlsafe(32)
    _oauth_state[state] = verifier

    params = {
        "response_type": "code",
        "client_id": FITBIT_CLIENT_ID,
        "redirect_uri": FITBIT_REDIRECT_URL,
        "scope": "sleep",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{FITBIT_AUTH_URL}?{qs}"
```

#### Step 5: Token exchange

```python
async def exchange_code(code: str, state: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    verifier = _oauth_state.pop(state, None)
    if not verifier:
        raise ValueError("Invalid or expired OAuth state")

    auth_header = base64.b64encode(
        f"{FITBIT_CLIENT_ID}:{FITBIT_CLIENT_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            FITBIT_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": FITBIT_REDIRECT_URL,
                "code_verifier": verifier,
            },
            timeout=15,
        )
        resp.raise_for_status()
        tokens = resp.json()

    tokens["obtained_at"] = int(time.time())
    _save_tokens(tokens)
    return tokens
```

#### Step 6: Token refresh

```python
async def _refresh_tokens() -> dict:
    """Refresh the Fitbit access token using the stored refresh token."""
    tokens = _load_tokens()
    if not tokens or not tokens.get("refresh_token"):
        raise RuntimeError("No Fitbit refresh token — user must re-authorize")

    auth_header = base64.b64encode(
        f"{FITBIT_CLIENT_ID}:{FITBIT_CLIENT_SECRET}".encode()
    ).decode()

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            FITBIT_TOKEN_URL,
            headers={
                "Authorization": f"Basic {auth_header}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
            timeout=15,
        )
        resp.raise_for_status()
        new_tokens = resp.json()

    new_tokens["obtained_at"] = int(time.time())
    _save_tokens(new_tokens)
    return new_tokens


async def _get_valid_token() -> str:
    """Return a valid access token, refreshing if expired."""
    tokens = _load_tokens()
    if not tokens:
        raise RuntimeError("No Fitbit tokens — user must authorize via /fitbit/authorize")

    # Fitbit tokens expire in 28800s (8 hours). Refresh if within 5 min of expiry.
    expires_in = tokens.get("expires_in", 28800)
    obtained_at = tokens.get("obtained_at", 0)
    if time.time() > obtained_at + expires_in - 300:
        log.info("Fitbit access token expired or expiring soon — refreshing")
        tokens = await _refresh_tokens()

    return tokens["access_token"]
```

#### Step 7: Sleep data fetch functions

```python
async def get_sleep_by_date(date: str) -> dict:
    """
    Fetch sleep log for a specific date.

    Args:
        date: Date string in YYYY-MM-DD format.

    Returns:
        Raw Fitbit API response with 'sleep' array and 'summary' object.

    Fitbit endpoint: GET /1.2/user/-/sleep/date/{date}.json
    """
    token = await _get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FITBIT_API_BASE}/1.2/user/-/sleep/date/{date}.json",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            # Token might have been revoked — try one refresh
            token = (await _refresh_tokens())["access_token"]
            resp = await client.get(
                f"{FITBIT_API_BASE}/1.2/user/-/sleep/date/{date}.json",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()


async def get_sleep_log_list(
    after_date: str | None = None,
    before_date: str | None = None,
    limit: int = 7,
    sort: str = "desc",
) -> dict:
    """
    Fetch paginated list of sleep records.

    Fitbit endpoint: GET /1.2/user/-/sleep/list.json
    """
    token = await _get_valid_token()
    params: dict[str, str | int] = {"limit": limit, "sort": sort}
    if after_date:
        params["afterDate"] = after_date
    if before_date:
        params["beforeDate"] = before_date

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{FITBIT_API_BASE}/1.2/user/-/sleep/list.json",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        if resp.status_code == 401:
            token = (await _refresh_tokens())["access_token"]
            resp = await client.get(
                f"{FITBIT_API_BASE}/1.2/user/-/sleep/list.json",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()


def format_sleep_summary(data: dict) -> str:
    """
    Format Fitbit sleep API response into a human-readable summary
    suitable for Telegram messages or Notion agent consumption.
    """
    sleep_entries = data.get("sleep", [])
    if not sleep_entries:
        return "No sleep data recorded for this date."

    # Use the main sleep entry (isMainSleep=True), fall back to first
    main = next((s for s in sleep_entries if s.get("isMainSleep")), sleep_entries[0])

    minutes_asleep = main.get("minutesAsleep", 0)
    hours = minutes_asleep // 60
    mins = minutes_asleep % 60
    efficiency = main.get("efficiency", 0)
    time_in_bed = main.get("timeInBed", 0)
    bed_hours = time_in_bed // 60
    bed_mins = time_in_bed % 60

    lines = [
        f"Sleep: {hours}h {mins}m asleep ({bed_hours}h {bed_mins}m in bed)",
        f"Efficiency: {efficiency}%",
    ]

    # Add stage breakdown if available (stages type, not classic)
    levels = main.get("levels", {})
    summary = levels.get("summary", {})
    if "deep" in summary:
        deep = summary["deep"]
        light = summary["light"]
        rem = summary["rem"]
        wake = summary["wake"]

        # Handle both formats: int (minutes) or dict with "minutes" key
        def _mins(val):
            return val["minutes"] if isinstance(val, dict) else val

        lines.append(
            f"Stages: deep {_mins(deep)}m, light {_mins(light)}m, "
            f"REM {_mins(rem)}m, awake {_mins(wake)}m"
        )

    start = main.get("startTime", "")
    end = main.get("endTime", "")
    if start and end:
        # Trim to HH:MM
        start_short = start[11:16] if len(start) > 16 else start
        end_short = end[11:16] if len(end) > 16 else end
        lines.append(f"Window: {start_short} → {end_short}")

    log_type = main.get("logType", "unknown")
    lines.append(f"Source: {log_type}")

    return "\n".join(lines)
```

#### Verification (S1-A)

- `fitbit.py` exists at project root
- `get_authorize_url()` returns valid Fitbit OAuth URL with PKCE params and `sleep` scope
- `_generate_pkce()` produces valid S256 challenge
- `format_sleep_summary()` handles both `stages` and `classic` type responses
- `TOKEN_FILE` points to `.fitbit_tokens.json`

---

### S1-B — OAuth Routes + Wiring

#### Step 1: Add OAuth routes to `webhook_server.py`

After the existing `@app.get("/call/check")` route (the last route before `app.include_router(call_router)`), add Fitbit OAuth routes.

**Important: The redirect URL is `https://noise2signal.co.uk/test` (registered with Fitbit).** The OAuth flow is:
1. User hits `GET /fitbit/authorize` → gets Fitbit consent URL
2. User opens URL in browser → approves → Fitbit redirects to `https://noise2signal.co.uk/test?code=...&state=...`
3. User copies the `code` and `state` from the redirect URL
4. User hits `GET /fitbit/callback?code=...&state=...` on our server to exchange tokens

This works because the redirect URL doesn't need to point to our server — it just needs to match what's registered with Fitbit. The user manually relays the code.

```python
# ── Fitbit OAuth ─────────────────────────────────────────────────────────────────

@app.get("/fitbit/authorize")
async def fitbit_authorize() -> JSONResponse:
    """Return the Fitbit OAuth consent URL for the user to open in a browser."""
    from fitbit import get_authorize_url, FITBIT_CLIENT_ID
    if not FITBIT_CLIENT_ID:
        return JSONResponse(
            {"error": "FITBIT_CLIENT_ID not configured"},
            status_code=500,
        )
    url = get_authorize_url()
    return JSONResponse({"authorize_url": url})


@app.get("/fitbit/callback")
async def fitbit_callback(request: Request) -> JSONResponse:
    """Exchange Fitbit auth code for tokens. User passes code and state from redirect URL."""
    from fitbit import exchange_code
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    if error:
        return JSONResponse({"error": error}, status_code=400)
    if not code or not state:
        return JSONResponse(
            {"error": "Missing code or state parameter"},
            status_code=400,
        )

    try:
        tokens = await exchange_code(code, state)
        return JSONResponse({
            "status": "authorized",
            "user_id": tokens.get("user_id"),
            "scopes": tokens.get("scope"),
        })
    except Exception as e:
        log.exception("Fitbit OAuth callback failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/fitbit/status")
async def fitbit_status() -> JSONResponse:
    """Check if Fitbit tokens exist and are valid."""
    from fitbit import _load_tokens
    tokens = _load_tokens()
    if not tokens:
        return JSONResponse({"connected": False})
    return JSONResponse({
        "connected": True,
        "user_id": tokens.get("user_id"),
        "scopes": tokens.get("scope"),
    })
```

#### Step 2: Add `.fitbit_tokens.json` to `.gitignore`

Append to `.gitignore` after the `# Keys & certificates` section:

```gitignore
# Fitbit OAuth tokens
.fitbit_tokens.json
```

#### Step 3: Update `/health` endpoint

In the existing `health()` function, add Fitbit status to the response. The current response dict in `webhook_server.py` is:

```python
return JSONResponse({
    "status": "ok",
    "service": "notioncares-webhook-server",
    "pending_telegram_awaits": len(shared._pending),
    "pending_voice_calls": len(shared._pending_calls),
})
```

Add after `"pending_voice_calls"`:

```python
    "fitbit_connected": Path(__file__).parent.joinpath(".fitbit_tokens.json").exists(),
```

Add `from pathlib import Path` to the imports at the top of `webhook_server.py`.

#### Step 4: Add new env vars to `.env.example`

Append to `.env.example`:

```env
# ── Fitbit (for sleep data integration) ──────────────────────────
FITBIT_CLIENT_ID=
FITBIT_CLIENT_SECRET=
FITBIT_REDIRECT_URL=https://noise2signal.co.uk/test
```

#### Step 5: Update `requirements.txt`

No new dependencies needed — `httpx` (already `>=0.27.2`) handles all Fitbit API calls.

#### Verification (S1-B)

- `GET /fitbit/authorize` → returns JSON with `authorize_url` containing `response_type=code`, `scope=sleep`, `code_challenge_method=S256`
- `GET /fitbit/callback?code=...&state=...` → exchanges code, saves tokens to `.fitbit_tokens.json`
- `GET /fitbit/status` → `{"connected": true/false}`
- `GET /health` → includes `fitbit_connected` field
- `.fitbit_tokens.json` is in `.gitignore`

#### Done when (Phase S1)

- `fitbit.py` exists with OAuth + sleep fetch functions
- OAuth flow works end-to-end: `/fitbit/authorize` → Fitbit consent → `/fitbit/callback` → tokens saved
- `get_sleep_by_date("2026-03-19")` returns valid response with token auto-refresh
- `get_sleep_log_list(after_date="2026-03-13", limit=7)` returns paginated results
- `format_sleep_summary()` produces readable text from both stages and classic data
- `.fitbit_tokens.json` persists across server restarts

---

## Phase S2: MCP Tool + Agent Check-in (DEPENDS ON S1)

> **Do NOT start until Phase S1 is complete and `/fitbit/status` returns `{"connected": true}`.**

### What exists after S1

**`fitbit.py`** provides:
- `get_sleep_by_date(date: str) -> dict`
- `get_sleep_log_list(after_date, before_date, limit, sort) -> dict`
- `format_sleep_summary(data: dict) -> str`

**`mcp_server.py`** has 3 tools on the `mcp` FastMCP instance. Tools are registered with `@mcp.tool()` decorator. The pattern for each tool:
1. Decorator: `@mcp.tool()`
2. Async function with type-annotated params
3. Docstring (used as tool description by Notion agent)
4. Returns `str`

### Execution

```
Batch S2 (parallel — 2 independent units):
  ├─ S2-T: MCP tool — get_sleep_data (mcp_server.py)
  └─ S2-C: Sleep check-in via existing send_message_await_response flow (docs only)
```

---

### S2-T — MCP Tool: `get_sleep_data`

#### Step 1: Add import to `mcp_server.py`

After the existing import block (which ends with `from voice_call import _make_vonage_call`), add:

```python
from fitbit import get_sleep_by_date, get_sleep_log_list, format_sleep_summary
```

#### Step 2: Add the tool

After the `call_user_await_response` tool definition (the last tool in `mcp_server.py`), add:

```python
@mcp.tool()
async def get_sleep_data(
    date: str = "",
    days: int = 1,
) -> str:
    """Retrieve the user's Fitbit sleep data.

    - If date is provided (YYYY-MM-DD), fetches sleep log for that specific date.
    - If date is empty and days=1 (default), fetches last night's sleep.
    - If days > 1, fetches the last N days of sleep records.

    Returns a human-readable summary of sleep duration, efficiency, and stages.
    Use this to check how the user slept before asking about their day.
    """
    from datetime import date as date_cls, timedelta

    try:
        if date and days <= 1:
            # Single date lookup
            data = await get_sleep_by_date(date)
            return format_sleep_summary(data)

        if not date:
            # Default to today (last night's sleep is logged under today's date)
            date = date_cls.today().isoformat()

        if days <= 1:
            data = await get_sleep_by_date(date)
            return format_sleep_summary(data)

        # Multi-day: fetch recent sleep records
        after = (date_cls.fromisoformat(date) - timedelta(days=days)).isoformat()
        data = await get_sleep_log_list(after_date=after, limit=days, sort="desc")
        entries = data.get("sleep", [])
        if not entries:
            return f"No sleep data found for the last {days} days."

        summaries = []
        for entry in entries:
            entry_date = entry.get("dateOfSleep", "unknown")
            summary = format_sleep_summary({"sleep": [entry]})
            summaries.append(f"[{entry_date}]\n{summary}")

        return "\n\n".join(summaries)

    except RuntimeError as e:
        # Token missing or refresh failed
        return f"Fitbit not connected: {e}. Ask the user to authorize via the setup link."
    except Exception as e:
        log.exception("Failed to fetch sleep data")
        return f"Error fetching sleep data: {e}"
```

#### Verification (S2-T)

- `mcp` FastMCP instance now has 4 tools: `send_message`, `send_message_await_response`, `call_user_await_response`, `get_sleep_data`
- Tool callable via MCP JSON-RPC: `tools/call` with `{"name": "get_sleep_data", "arguments": {"date": "2026-03-19"}}`
- Returns formatted sleep summary string
- Returns "Fitbit not connected" message if no tokens exist
- Multi-day mode returns stacked summaries with date headers

---

### S2-C — Agent Prompt Examples (documentation only)

Add to `README.md` under the existing "Agent prompt examples" section:

**SleepTracker — daily sleep check-in with Fitbit data:**

```
You are a sleep wellness assistant for the user. Every morning:

1. Call get_sleep_data() to fetch last night's sleep.
2. Send the sleep summary via send_message().
3. If sleep was under 6 hours or efficiency below 80%, use send_message_await_response()
   to ask: "Rough night — anything keeping you up? Want me to suggest some wind-down tips?"
4. If no Fitbit data available, use send_message_await_response() to ask:
   "I couldn't pull your sleep data. Did you wear your Fitbit last night?"

For weekly reviews, call get_sleep_data(days=7) and summarize trends.
If the user doesn't respond to Telegram within 10 minutes, escalate with
call_user_await_response() and ask about their sleep verbally.
```

#### Done when (Phase S2)

- `get_sleep_data` tool registered on `mcp` and callable via JSON-RPC
- Notion agent can invoke `get_sleep_data` with no args (last night), a date, or multi-day
- Error handling returns user-friendly message, not stack trace
- README has SleepTracker prompt example

---

## New Environment Variables Summary

| Variable | Required | Description |
|----------|----------|-------------|
| `FITBIT_CLIENT_ID` | Yes (for sleep) | `23V892` — from dev.fitbit.com app registration |
| `FITBIT_CLIENT_SECRET` | Yes (for sleep) | From Fitbit app settings |
| `FITBIT_REDIRECT_URL` | Yes (for sleep) | `https://noise2signal.co.uk/test` — registered callback URL |

**Fitbit app setup prerequisites:**
1. Register app at dev.fitbit.com
2. Set OAuth 2.0 Application Type: **Personal**
3. Set Callback URL: `https://noise2signal.co.uk/test`
4. Note Client ID and Client Secret → add to `.env`

---

## New Files

| File | Purpose |
|------|---------|
| `fitbit.py` | OAuth helpers, token persistence, sleep API fetch, formatting |
| `.fitbit_tokens.json` | Auto-generated token storage (gitignored) |

## Modified Files

| File | Changes |
|------|---------|
| `mcp_server.py` | +1 import, +1 tool (`get_sleep_data`) |
| `webhook_server.py` | +3 routes (`/fitbit/authorize`, `/fitbit/callback`, `/fitbit/status`), +1 field in `/health` |
| `.gitignore` | +1 line (`.fitbit_tokens.json`) |
| `.env.example` | +3 vars (`FITBIT_CLIENT_ID`, `FITBIT_CLIENT_SECRET`, `FITBIT_REDIRECT_URL`) |
| `requirements.txt` | No changes needed |
| `README.md` | +1 agent prompt example (SleepTracker) |

---

## `/batch` Instructions

### Phase S1 — Fitbit OAuth + Sleep Fetch (sequential)

```
/batch Implement Fitbit sleep integration Phase S1 for NotionCares. See docs/SPRINT_FITBIT_SLEEP_SPEC.md Phase S1 for full implementation details.

Unit 1 (S1-A): Create fitbit.py at project root. (1) Add imports: asyncio, hashlib, base64, json, logging, os, secrets, time, pathlib.Path, httpx, dotenv. (2) Define constants: FITBIT_CLIENT_ID, FITBIT_CLIENT_SECRET, FITBIT_REDIRECT_URL from env, FITBIT_AUTH_URL="https://www.fitbit.com/oauth2/authorize", FITBIT_TOKEN_URL="https://api.fitbit.com/oauth2/token", FITBIT_API_BASE="https://api.fitbit.com", TOKEN_FILE=Path(__file__).parent / ".fitbit_tokens.json". (3) _generate_pkce() returns (verifier, challenge) using S256. (4) _load_tokens() and _save_tokens() for JSON file persistence. (5) get_authorize_url() builds OAuth URL with PKCE, sleep scope, redirect_uri=FITBIT_REDIRECT_URL (https://noise2signal.co.uk/test), stores verifier in _oauth_state dict keyed by random state. (6) exchange_code(code, state) exchanges auth code for tokens using Basic auth header with redirect_uri=FITBIT_REDIRECT_URL, saves with obtained_at timestamp. (7) _refresh_tokens() and _get_valid_token() with 5-min-before-expiry refresh logic. (8) get_sleep_by_date(date) calls GET /1.2/user/-/sleep/date/{date}.json with auto-refresh on 401. (9) get_sleep_log_list(after_date, before_date, limit, sort) calls GET /1.2/user/-/sleep/list.json. (10) format_sleep_summary(data) formats response into readable text: hours/mins asleep, time in bed, efficiency%, stage breakdown (deep/light/REM/wake), sleep window, source.

Unit 2 (S1-B): Wire OAuth into webhook_server.py. (1) Add GET /fitbit/authorize route returning JSON with authorize_url from fitbit.get_authorize_url(). (2) Add GET /fitbit/callback route: reads code and state from query params, calls fitbit.exchange_code(), returns user_id and scopes. (3) Add GET /fitbit/status route checking fitbit._load_tokens(). (4) Add "fitbit_connected" bool to existing GET /health response (check .fitbit_tokens.json exists via pathlib). (5) Add .fitbit_tokens.json to .gitignore. (6) Add FITBIT_CLIENT_ID and FITBIT_CLIENT_SECRET to .env.example.
```

### Phase S2 — MCP Tool + Docs (parallel, after S1)

```
/batch Implement Fitbit sleep MCP tool Phase S2 for NotionCares. See docs/SPRINT_FITBIT_SLEEP_SPEC.md Phase S2 for full implementation details. PREREQ: Phase S1 must be complete — fitbit.py must exist with get_sleep_by_date, get_sleep_log_list, format_sleep_summary.

Unit 1 (S2-T): In mcp_server.py, add import of get_sleep_by_date, get_sleep_log_list, format_sleep_summary from fitbit (after the voice_call import). Add @mcp.tool() decorated async function get_sleep_data(date: str = "", days: int = 1) -> str. Logic: if date provided and days<=1, call get_sleep_by_date(date) and format. If no date, default to today via datetime.date.today().isoformat(). If days>1, call get_sleep_log_list with after_date = date - days, format each entry with date header. Catch RuntimeError for missing tokens (return user-friendly message), catch Exception for API errors. Docstring must explain all usage modes clearly for Notion agent.

Unit 2 (S2-C): Add SleepTracker agent prompt example to README.md under the existing "Agent prompt examples" section. Prompt should instruct agent to: (1) call get_sleep_data() each morning, (2) send summary via send_message(), (3) if sleep < 6h or efficiency < 80%, ask follow-up via send_message_await_response(), (4) if no data, ask about Fitbit wearing, (5) weekly reviews via get_sleep_data(days=7), (6) escalate to call_user_await_response() if no Telegram reply.
```

### Post-batch

```bash
# 1. Fitbit credentials are already in .env:
#    FITBIT_CLIENT_ID=23V892
#    FITBIT_CLIENT_SECRET=8867e4fdab86cc76705c23e38e0291a5
#    FITBIT_REDIRECT_URL=https://noise2signal.co.uk/test

# 2. Start servers
python main.py

# 3. Authorize Fitbit (one-time)
curl -s http://localhost:8002/fitbit/authorize | python -m json.tool
# → Open the authorize_url in browser → approve on Fitbit
# → Browser redirects to https://noise2signal.co.uk/test?code=XXXX&state=YYYY
# → Copy code and state from the URL bar, then:
curl "http://localhost:8002/fitbit/callback?code=XXXX&state=YYYY"

# 4. Verify
curl http://localhost:8002/fitbit/status
curl http://localhost:8002/health
```

---

## Acceptance Tests

### Phase S1 — OAuth + Data Fetch

**S1-A Fitbit Module:**
- [x] `fitbit.py` exists at project root with all functions
- [x] `get_authorize_url()` returns URL with `response_type=code`, `scope=sleep`, `code_challenge_method=S256`
- [x] `_generate_pkce()` produces valid S256 pair (challenge = base64url(sha256(verifier)))
- [ ] `_load_tokens()` returns `None` when no file exists
- [x] `_save_tokens({"access_token": "x"})` creates `.fitbit_tokens.json`
- [x] `format_sleep_summary()` handles stages-type response (deep/light/REM/wake)
- [x] `format_sleep_summary()` handles classic-type response (asleep/restless/awake)
- [x] `format_sleep_summary()` returns "No sleep data" for empty sleep array

**S1-B OAuth Routes:**
- [x] `GET /fitbit/authorize` → 200 with `{"authorize_url": "https://www.fitbit.com/oauth2/authorize?..."}`
- [ ] `GET /fitbit/authorize` without `FITBIT_CLIENT_ID` → 500 with error
- [x] `GET /fitbit/callback?code=abc&state=xyz` with valid state → saves tokens, returns user_id
- [ ] `GET /fitbit/callback?error=access_denied` → 400
- [ ] `GET /fitbit/callback` without code → 400
- [ ] `GET /fitbit/status` before auth → `{"connected": false}`
- [ ] `GET /fitbit/status` after auth → `{"connected": true, "user_id": "...", "scopes": "sleep"}`
- [ ] `GET /health` includes `"fitbit_connected": true/false`
- [x] `.fitbit_tokens.json` in `.gitignore`
- [x] `.env.example` has `FITBIT_CLIENT_ID` and `FITBIT_CLIENT_SECRET`

### Phase S2 — MCP Tool

**S2-T Tool:**
- [x] `get_sleep_data` registered as 4th tool on `mcp` FastMCP instance
- [x] MCP `tools/call` with `{"name": "get_sleep_data", "arguments": {}}` → last night's summary
- [x] `{"name": "get_sleep_data", "arguments": {"date": "2026-03-19"}}` → specific date
- [x] `{"name": "get_sleep_data", "arguments": {"days": 7}}` → 7 stacked summaries with date headers
- [x] No tokens → returns "Fitbit not connected: ..." (not a stack trace)
- [x] API error → returns "Error fetching sleep data: ..." (not a stack trace)

**S2-C Docs:**
- [x] README.md has SleepTracker agent prompt example
- [x] Prompt references `get_sleep_data`, `send_message`, `send_message_await_response`, `call_user_await_response`

---

## Smoke Test (manual, via test-mcp.sh pattern)

After both phases complete:

```bash
# 1. Start servers
python main.py

# 2. Authorize Fitbit (one-time)
curl -s http://localhost:8002/fitbit/authorize | python -m json.tool
# Open URL in browser, approve, verify callback succeeds

# 3. Test MCP tool via curl (adapt from scripts/mcp_session.sh pattern)
# Init session
SESSION=$(curl -s -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}' \
  -D - 2>/dev/null | grep -i mcp-session-id | cut -d' ' -f2 | tr -d '\r')

# Call get_sleep_data
curl -s -X POST http://localhost:8001/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "mcp-session-id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_sleep_data","arguments":{}}}' | python -m json.tool
```

Expected output: formatted sleep summary or "No sleep data recorded" or "Fitbit not connected" message.
