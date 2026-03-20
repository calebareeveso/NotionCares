"""
video_coach.py — Sports coaching via Gemini video analysis
──────────────────────────────────────────────────────────
Downloads a video from Telegram, uploads it to Gemini,
and returns coaching feedback identifying the sport and
what the user is doing well / badly.
"""

import asyncio
import logging
import os
import tempfile

import httpx
from google import genai

log = logging.getLogger("notioncares.video_coach")

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

COACHING_PROMPT = """\
You are an expert sports coach analyzing a training video.

1. Identify the sport and the specific movement/exercise being performed.
2. List 2-3 things the athlete is doing well (be specific about body mechanics).
3. List 2-3 things that need improvement (be specific and actionable).
4. Give one key drill or cue they should focus on in their next session.

Keep the feedback concise, encouraging, and practical. Use plain text (no markdown).
"""


async def analyze_video(file_id: str) -> str:
    """Download a Telegram video by file_id, send to Gemini, return coaching feedback."""
    if not GEMINI_API_KEY:
        return "Video coaching is not available — GEMINI_API_KEY is not set."

    # 1. Get file path from Telegram
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        # 2. Download the video
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        resp = await client.get(download_url, timeout=60)
        resp.raise_for_status()
        video_bytes = resp.content

    # 3. Write to temp file (Gemini SDK needs a file path)
    suffix = os.path.splitext(file_path)[1] or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(video_bytes)
        tmp_path = tmp.name

    try:
        # 4. Upload to Gemini and analyze
        client = genai.Client(api_key=GEMINI_API_KEY)

        video_file = client.files.upload(file=tmp_path)
        log.info("Uploaded video to Gemini: %s (%s bytes)", video_file.name, len(video_bytes))

        # Wait for Gemini to finish processing the video
        while video_file.state.name == "PROCESSING":
            log.info("Waiting for Gemini to process video %s...", video_file.name)
            await asyncio.sleep(2)
            video_file = client.files.get(name=video_file.name)

        if video_file.state.name != "ACTIVE":
            return f"Video processing failed — Gemini state: {video_file.state.name}"

        log.info("Video %s is ACTIVE, requesting analysis", video_file.name)

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[video_file, COACHING_PROMPT],
        )
        feedback = response.text.strip()
        log.info("Gemini coaching feedback received (%d chars)", len(feedback))
        return feedback
    finally:
        os.unlink(tmp_path)
