"""
fitbit.py — Fitbit OAuth2+PKCE Authentication & Sleep API Client
─────────────────────────────────────────────────────────────────
Handles the full OAuth2 authorization code flow with PKCE for Fitbit,
token persistence, automatic refresh, and sleep data fetching.

Used by webhook_server.py routes:
  /fitbit/authorize  — start OAuth flow
  /fitbit/callback   — handle redirect with auth code
  /fitbit/status     — check if tokens are stored
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

FITBIT_CLIENT_ID = os.getenv("FITBIT_CLIENT_ID", "")
FITBIT_CLIENT_SECRET = os.getenv("FITBIT_CLIENT_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")

FITBIT_AUTH_URL = "https://www.fitbit.com/oauth2/authorize"
FITBIT_TOKEN_URL = "https://api.fitbit.com/oauth2/token"
FITBIT_API_BASE = "https://api.fitbit.com"

TOKEN_FILE = Path(__file__).parent / ".fitbit_tokens.json"

logger = logging.getLogger("notioncares.fitbit")

_STATE_TTL = 600  # 10 minutes

# Maps OAuth state parameter → (PKCE code_verifier, created_at)
_oauth_state: dict[str, tuple[str, float]] = {}

# ── Internal helpers ──────────────────────────────────────────────────────────


def _basic_auth_header() -> str:
    """Return the Basic auth header value for Fitbit client credentials."""
    return "Basic " + base64.b64encode(
        f"{FITBIT_CLIENT_ID}:{FITBIT_CLIENT_SECRET}".encode()
    ).decode()


def _generate_pkce() -> tuple[str, str]:
    """Generate a PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ── Token persistence ─────────────────────────────────────────────────────────


def _load_tokens() -> dict | None:
    """Read stored tokens from disk, or return None if missing/corrupt."""
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_tokens(tokens: dict) -> None:
    """Write tokens dict to disk as JSON (owner-only permissions)."""
    TOKEN_FILE.write_text(json.dumps(tokens, indent=2))
    TOKEN_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600


# ── OAuth2 flow ───────────────────────────────────────────────────────────────


def _prune_stale_states() -> None:
    """Remove OAuth state entries older than _STATE_TTL."""
    cutoff = time.time() - _STATE_TTL
    stale = [k for k, (_, t) in _oauth_state.items() if t < cutoff]
    for k in stale:
        del _oauth_state[k]


def get_authorize_url() -> str:
    """Build the Fitbit OAuth2 authorize URL with PKCE challenge."""
    if not FITBIT_CLIENT_ID or not FITBIT_CLIENT_SECRET:
        raise ValueError("FITBIT_CLIENT_ID and FITBIT_CLIENT_SECRET must be set")

    _prune_stale_states()
    state = secrets.token_urlsafe(32)
    verifier, challenge = _generate_pkce()
    _oauth_state[state] = (verifier, time.time())

    params = {
        "client_id": FITBIT_CLIENT_ID,
        "response_type": "code",
        "scope": "sleep",
        "redirect_uri": f"{PUBLIC_BASE_URL.rstrip('/')}/fitbit/callback",
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    }
    return f"{FITBIT_AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str, state: str) -> dict:
    """Exchange an authorization code for tokens using the stored PKCE verifier."""
    entry = _oauth_state.pop(state, None)
    if entry is None:
        raise ValueError("Unknown or expired OAuth state parameter")
    verifier, _ = entry

    redirect_uri = f"{PUBLIC_BASE_URL.rstrip('/')}/fitbit/callback"

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            FITBIT_TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": FITBIT_CLIENT_ID,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": verifier,
            },
            timeout=15,
        )
        if not resp.is_success:
            logger.error("Fitbit token exchange failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        tokens = resp.json()

    tokens["obtained_at"] = time.time()
    _save_tokens(tokens)
    logger.info("Fitbit tokens obtained and saved")
    return tokens


# ── Token refresh ─────────────────────────────────────────────────────────────


async def _refresh_tokens() -> dict:
    """Use the stored refresh_token to get a fresh access_token."""
    tokens = _load_tokens()
    if not tokens or "refresh_token" not in tokens:
        raise ValueError("No refresh token available — re-authorize via /fitbit/authorize")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            FITBIT_TOKEN_URL,
            headers={
                "Authorization": _basic_auth_header(),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
            timeout=15,
        )
        if not resp.is_success:
            logger.error("Fitbit token refresh failed: %s %s", resp.status_code, resp.text)
        resp.raise_for_status()
        new_tokens = resp.json()

    new_tokens["obtained_at"] = time.time()
    _save_tokens(new_tokens)
    logger.info("Fitbit tokens refreshed and saved")
    return new_tokens


async def _get_valid_token() -> str:
    """Return a valid access_token, refreshing if expired or close to expiry."""
    tokens = _load_tokens()
    if not tokens:
        raise ValueError("Not authenticated — visit /fitbit/authorize first")

    expires_at = tokens.get("obtained_at", 0) + tokens.get("expires_in", 0) - 300
    if time.time() >= expires_at:
        tokens = await _refresh_tokens()

    return tokens["access_token"]


# ── Public status ─────────────────────────────────────────────────────────────


def is_connected() -> bool:
    """Return True if valid Fitbit tokens are stored on disk."""
    return TOKEN_FILE.exists() and _load_tokens() is not None


# ── Sleep API ─────────────────────────────────────────────────────────────────


async def _authed_get(url: str, params: dict | None = None) -> dict:
    """GET a Fitbit API endpoint with Bearer auth, retrying once on 401."""
    token = await _get_valid_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params=params, timeout=15,
        )
        if resp.status_code == 401:
            token = (await _refresh_tokens())["access_token"]
            resp = await client.get(
                url, headers={"Authorization": f"Bearer {token}"},
                params=params, timeout=15,
            )
        resp.raise_for_status()
        return resp.json()


async def get_sleep_by_date(date: str) -> dict:
    """Fetch sleep data for a specific date (YYYY-MM-DD)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ValueError(f"Invalid date format: {date!r} (expected YYYY-MM-DD)")
    return await _authed_get(f"{FITBIT_API_BASE}/1.2/user/-/sleep/date/{date}.json")


async def get_sleep_log_list(
    after_date: str = "",
    before_date: str = "",
    limit: int = 10,
    sort: str = "desc",
) -> dict:
    """Fetch a paginated list of sleep logs."""
    params: dict[str, str | int] = {"limit": limit, "sort": sort, "offset": 0}
    if after_date:
        params["afterDate"] = after_date
    if before_date:
        params["beforeDate"] = before_date
    return await _authed_get(
        f"{FITBIT_API_BASE}/1.2/user/-/sleep/list.json", params=params,
    )


# ── Formatting ────────────────────────────────────────────────────────────────


def format_sleep_summary(data: dict) -> str:
    """Format a Fitbit sleep response into a human-readable summary."""
    sleep_list = data.get("sleep", [])
    if not sleep_list:
        return "No sleep data found."

    # Use the main (longest) sleep record
    record = max(sleep_list, key=lambda s: s.get("duration", 0))

    total_min = record.get("minutesAsleep", 0)
    hours, mins = divmod(total_min, 60)

    lines = [f"Sleep: {hours}h {mins}m asleep"]

    # Fitbit's "timeInBed" field is in minutes despite the name
    minutes_in_bed = record.get("timeInBed", record.get("minutesInBed"))
    if minutes_in_bed is not None:
        lines.append(f"Time in bed: {minutes_in_bed} min")

    efficiency = record.get("efficiency")
    if efficiency is not None:
        lines.append(f"Efficiency: {efficiency}%")

    # Stage breakdown (only present for main sleep with detailed tracking)
    levels = record.get("levels", {})
    summary = levels.get("summary", {})
    if summary:
        stages = []
        for stage in ("deep", "light", "rem", "wake"):
            info = summary.get(stage)
            if info:
                stages.append(f"  {stage}: {info.get('minutes', 0)} min")
        if stages:
            lines.append("Stages:")
            lines.extend(stages)

    start = record.get("startTime", "")
    end = record.get("endTime", "")
    if start:
        lines.append(f"Start: {start}")
    if end:
        lines.append(f"End: {end}")

    device = record.get("deviceName")
    if device:
        lines.append(f"Device: {device}")

    return "\n".join(lines)
