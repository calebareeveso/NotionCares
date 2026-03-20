# NotionCares — Telegram MCP

Exposes two tools that Notion Custom Agents call to message the user via Telegram:

| Tool | What it does |
|---|---|
| `send_message` | Sends a message. Does not wait for a reply. |
| `send_message_await_response` | Sends a message and **blocks until the user replies**. Returns the reply text. |

---

## How it works

```
Notion Custom Agent
       │
       │  calls MCP tool
       ▼
  MCP Server (port 8001)
  send_message_await_response("What did you eat?")
       │
       │  POSTs to Telegram Bot API
       ▼
  User's Telegram app
       │
       │  user types reply
       ▼
  Telegram Bot API
       │
       │  POSTs update to your server
       ▼
  Webhook Receiver (port 8002)  →  resolves pending Event
       │
       ▼
  MCP tool unblocks → returns reply to Notion Agent
```

Both servers run in the **same Python process** (same asyncio event loop), so they share state directly — no Redis, no database needed.

---

## Step-by-step setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts — choose a name and a username (must end in `bot`) I USED @NotionCaresBot
4. BotFather replies with your **bot token**: `123456789:ABCdef...`
5. Copy it — you'll need it in step 3

### 2. Install dependencies

```bash
cd telegram-mcp
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure .env

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNO...   # from BotFather
TELEGRAM_CHAT_ID=                                   # fill in after step 4
PUBLIC_BASE_URL=                                    # fill in after step 5
MCP_PORT=8001
WEBHOOK_PORT=8002
```

### 4. Get your Telegram chat ID

1. Open Telegram, find your new bot, send it any message (e.g. `hello`)
2. Run:
   ```bash
   python get_chat_id.py
   ```
3. Copy the Chat ID printed → paste into `.env` as `TELEGRAM_CHAT_ID`

### 5. Set up ngrok (for local dev / hackathon)

Telegram requires a **public HTTPS URL** to send webhook updates to. ngrok provides this.

```bash
# In a separate terminal:
ngrok http 8002
```

ngrok prints something like:
```
Forwarding  https://abcd-1234.ngrok-free.app → http://localhost:8002
```

Copy the `https://...` URL → paste into `.env` as `PUBLIC_BASE_URL`:
```env
PUBLIC_BASE_URL=https://abcd-1234.ngrok-free.app
```

> **Every time you restart ngrok you get a new URL.** Update `.env` and re-run step 6.

### 6. Start the servers

```bash
python main.py
```

You should see:
```
Starting MCP server on port 8001  →  Notion connects at /mcp
Starting webhook server on port 8002  →  register via GET /setup
```

### 7. Register the Telegram webhook

Run this once (and again every time ngrok restarts):

```bash
curl http://localhost:8002/setup
```

Expected response:
```json
{
  "webhook_registered": "https://abcd-1234.ngrok-free.app/webhook",
  "telegram": { "ok": true, "result": true, "description": "Webhook was set" }
}
```

If `ok` is `false`, the most common causes are:
- `PUBLIC_BASE_URL` is HTTP not HTTPS
- ngrok URL has changed — update `.env` and retry
- Bot token is wrong

### 8. Test it manually

Send your bot a message in Telegram — you should see it logged in the server terminal as:

```
Received message from chat_id=987654321: 'hello'
Message from chat_id=987654321 — no pending tool call waiting for a reply.
```

The second line is expected — there's no agent waiting for a reply right now. That's fine.

---

## Connecting to Notion Custom Agent

In your Notion workspace:

1. Go to **Settings → Notion AI → AI connectors**
2. Enable **Custom MCP servers** (workspace admin only)
3. Open each Custom Agent's **Settings → Tools & Access**
4. Click **Add connection → Custom MCP server**
5. Enter the MCP URL: `https://your-public-url.com/mcp`
   - Local dev: use the ngrok URL for port 8001, e.g. `https://xxxx.ngrok-free.app/mcp`
   - Or run a second ngrok tunnel: `ngrok http 8001`
6. Enable the tools you want:
   - `send_message` → set to **Run automatically**
   - `send_message_await_response` → set to **Run automatically**
7. Save

> **Important:** Each Notion Custom Agent needs its own separate MCP connection.
> Connections are not shared across agents.

---

## Writing agent Instructions (prompt examples)

### NutritionBot — asking what the user ate

```
When this agent runs, call send_message_await_response with the following message,
adapting the meal name to the time of day (Breakfast at 08:00, Lunch at 13:00,
Dinner at 19:00):

"Hey! 🍽️ What did you have for [MEAL]? Just reply in plain text — e.g. 'oats,
banana, black coffee' — and I'll log it for you."

Wait for the user's reply. Parse it into:
- Meal type: Breakfast / Lunch / Dinner (based on current time)
- Description: the user's raw reply text
- Calories: your best estimate (integer)
- Carbs (C): grams
- Fat (F): grams
- Protein (P): grams

If exact values are not known, make a reasonable nutritional estimate.
Do not ask follow-up questions.

Then create a new page in the Healthy Meals database with these fields.
```

### MedReminder — with phone call fallback

```
Read the Medication & Supplements database.
For each medication where Schedule Time matches the current run time (within 15 minutes):

1. Call send_message_await_response:
   "💊 Time for your [MEDICATION NAME] — [PURPOSE]. Reply YES to confirm you've taken it."
   Use timeout_seconds: 900 (15 minutes).

2. If the reply contains YES (case-insensitive):
   Update the Last Taken field to now. Increment Streak Days by 1.

3. If send_message_await_response raises a TimeoutError:
   Log a note that Telegram timed out.
   The next step is to trigger the Phone Call MCP — call call_user_await_response
   with the same script.

4. If still no confirmation after the call:
   Increment Missed Doses by 1. Add a note to the database row.
```

### SleepTracker — daily sleep check-in with Fitbit data

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

---

## Production deployment (Cloud Run)

For production, replace ngrok with a proper public URL. Google Cloud Run is a good fit
since it provides HTTPS automatically and scales to zero when idle.

```dockerfile
# Dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8001 8002
CMD ["python", "main.py"]
```

```bash
# Deploy to Cloud Run
gcloud run deploy telegram-mcp \
  --source . \
  --region europe-west2 \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_BOT_TOKEN=...,TELEGRAM_CHAT_ID=...,PUBLIC_BASE_URL=https://your-cloud-run-url.run.app
```

> Cloud Run only exposes one port publicly. For production, combine both servers
> onto a single port using a reverse proxy (nginx) or restructure as a single
> FastAPI app with the MCP ASGI app mounted at `/mcp`.

---

## CLI smoke tests (without Notion)

From the repo root, with `python main.py` running and your venv optional (scripts only need `curl` + `python3`):

```bash
# Telegram: send_message_await_response (prompts; press Enter to keep each default)
./scripts/test-mcp.sh telegram

# Voice: call_user_await_response — places a real outbound call (prompts with defaults)
./scripts/test-mcp.sh call

# Use a public URL (e.g. ngrok http 8001)
MCP_URL=https://xxxx.ngrok-free.app/mcp ./scripts/test-mcp.sh telegram
```

Shared session helpers live in `scripts/mcp_session.sh` (sourced by `test-mcp.sh`).

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Webhook was not set` | Make sure `PUBLIC_BASE_URL` starts with `https://` |
| Bot sends message but reply never resolves | ngrok URL changed — update `.env`, re-run `/setup` |
| `TELEGRAM_BOT_TOKEN not found` | Make sure `.env` is in the same directory as the script |
| Notion can't connect to MCP | Check that port 8001 is also publicly reachable (run a second ngrok tunnel or use Cloud Run) |
| `TimeoutError` in agent | User didn't reply in time — expected. Wire up Phone Call MCP as fallback. |
| Tool shows as "Always ask" in Notion | Go to Agent Settings → Tools & Access → expand the MCP connection → toggle to "Run automatically" |
