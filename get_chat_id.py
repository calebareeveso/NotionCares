"""
get_chat_id.py
───────────────
Run this ONCE after you have sent your bot at least one message in Telegram.
It will print your TELEGRAM_CHAT_ID to put in .env.

Usage:
    python get_chat_id.py
"""

import os
import httpx
from dotenv import load_dotenv

load_dotenv()

token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env")
    raise SystemExit(1)

resp = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
data = resp.json()

if not data.get("ok"):
    print("Telegram API error:", data)
    raise SystemExit(1)

results = data.get("result", [])
if not results:
    print(
        "\nNo messages found.\n"
        "1. Open Telegram\n"
        "2. Search for your bot by its @username\n"
        "3. Send it any message (e.g. 'hello')\n"
        "4. Run this script again\n"
    )
    raise SystemExit(0)

print("\nFound the following chats:\n")
seen = set()
for update in results:
    msg = update.get("message", {})
    chat = msg.get("chat", {})
    cid = chat.get("id")
    if cid and cid not in seen:
        seen.add(cid)
        name = f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
        print(f"  Chat ID : {cid}")
        print(f"  Name    : {name or '(no name)'}")
        print(f"  Type    : {chat.get('type', 'unknown')}")
        print(f"\n  → Add to .env:  TELEGRAM_CHAT_ID={cid}\n")
