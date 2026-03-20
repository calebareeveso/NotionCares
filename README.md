# NotionCares — Telegram & Phone Call MCP

An MCP server that gives Notion Custom Agents two communication channels to reach a user:

| Tool | What it does |
|---|---|
| `send_message` | Sends a Telegram message. Does not wait for a reply. |
| `send_message_await_response` | Sends a Telegram message and **blocks until the user replies**. Returns the reply text. |
| `call_user_await_response` | **Calls the user's phone**, asks questions via an AI voice agent (ElevenLabs), and returns the full conversation transcript. |

---

## Architecture

```
                    Port 8001                          Port 8002
              +------------------+             +----------------------+
              |   MCP Server     |             |   Webhook Server     |
              |   (FastMCP)      |             |   (FastAPI)          |
              |                  |             |                      |
              | send_message     |  shared.py  | POST /webhook        | <-- Telegram
              | send_message_    |<----------->| GET  /setup          |
              |  await_response  |  _pending   | GET  /health         |
              | call_user_       |  dict       | GET  /call/answer    | <-- Vonage
              |  await_response  |<----------->| GET  /call/event     | <-- Vonage (WS leg)
              |                  | _pending_   | POST /call/event     | <-- Vonage (main leg)
              |                  |  calls dict | WS   /call/media-    | <-- Vonage audio
              +------------------+             |        stream        |
                                               | GET  /call/check     |
                                               +----------------------+
```

Both servers run in the **same Python process** (same asyncio event loop) via `asyncio.gather()` in `main.py`. They share module-level dicts in `shared.py` directly -- no Redis, no database, no IPC needed.

---

## How Telegram tools work

```
Notion Custom Agent
       |
       |  calls MCP tool
       v
  MCP Server (port 8001)
  send_message_await_response("What did you eat?")
       |
       | 1. Creates asyncio.Event + empty list
       | 2. Stores (Event, []) in shared._pending[CHAT_ID]
       | 3. POSTs to Telegram Bot API: sendMessage
       | 4. await event.wait(timeout=900)
       |    -- BLOCKS HERE --
       |
       |              User sees message in Telegram, types reply
       |                            |
       |              Telegram POSTs to /webhook
       |                            v
       |              Webhook Server (port 8002)
       |                | Extracts chat_id + text
       |                | Finds (Event, []) in shared._pending[chat_id]
       |                | Appends text to the list
       |                | Calls event.set()
       |                v
       |    -- UNBLOCKS --
       | 5. Reads reply from list[0]
       | 6. Returns reply string to Notion Agent
       v
Notion Agent receives "I had eggs and toast"
```

---

## How phone calls work

### Services involved

| Service | Role |
|---|---|
| **Vonage Voice API** | Places the outbound phone call, bridges PSTN audio to a WebSocket |
| **ElevenLabs Conversational AI** | AI voice agent -- speaks, listens, decides what to say, calls tools |
| **Our server (port 8002)** | Bridges audio between Vonage and ElevenLabs, collects transcripts |

### Call flow

```
Notion Custom Agent
  |
  |  calls MCP tool: call_user_await_response("+447930002899",
  |                    ["What did you eat?", "Did you exercise?"])
  v
mcp_server.py
  | 1. Generates unique call_id (UUID)
  | 2. Creates asyncio.Event + empty transcript list
  | 3. Stores in shared._pending_calls[call_id]
  | 4. POST https://api.nexmo.com/v1/calls
  |      (JWT auth with private.key, NCCO inline)
  |      NCCO: connect to wss://<ngrok>/call/media-stream?call_id=<uuid>
  | 5. await event.wait(timeout=300)
  |    -- BLOCKS HERE --
  |
  v
User's phone rings...
  |
  | Vonage events:  POST /call/event -> started, ringing, answered
  |
  | Vonage opens WebSocket to /call/media-stream?call_id=<uuid>
  |                    |
  v                    v
  call_media_stream() handler (voice_call.py)
    |
    | 1. Looks up call_id in shared._pending_calls
    | 2. Connects to ElevenLabs:
    |      GET /v1/convai/conversation/get_signed_url -> signed WS URL
    |      websockets.connect(signed_url, compression=None)
    |      Sends conversation_initiation_client_data with:
    |        - Per-call system prompt (injected questions)
    |        - TTS output format: pcm_16000
    |
    | 3. Starts two concurrent async tasks:
    |
    +-- vonage_to_elevenlabs()       +-- elevenlabs_to_vonage()
    |     |                          |     |
    |     | Receives binary PCM      |     | Receives JSON messages
    |     | from Vonage               |     | from ElevenLabs:
    |     |     |                    |     |
    |     |     v                    |     | type=audio ->
    |     | base64 encode ->         |     |   decode base64 ->
    |     | send as JSON:            |     |   send binary PCM
    |     | {user_audio_chunk:".."}  |     |   to Vonage
    |     |                          |     |
    |                                |     | type=user_transcript ->
    |                                |     |   append "User: ..." to transcript
    |                                |     |
    |                                |     | type=agent_response ->
    |                                |     |   append "Agent: ..." to transcript
    |                                |     |
    |                                |     | type=client_tool_call (end_call) ->
    |                                |     |   send tool_result back
    |                                |     |   event.set() -> UNBLOCKS MCP TOOL
    |                                |     |   return
    +--------------------------------+
  |
  |    -- UNBLOCKS --
  | 6. Reads transcript list, joins with newline
  | 7. Returns transcript to Notion Agent
  v
Notion Agent receives:
  "Agent: Hello! This is the NotionCares AI assistant...
   Agent: What did you eat today?
   User: I had eggs and toast for breakfast
   Agent: And did you exercise today?
   User: Yes I went for a run
   Agent: Thank you for your time!"
```

### Audio format chain

```
User's phone (PSTN)
  -> Vonage (converts to PCM L16 @ 16kHz)
  -> WebSocket binary frames to our server
  -> base64 encode -> JSON {user_audio_chunk: "..."} -> ElevenLabs

ElevenLabs (generates speech)
  -> JSON {type: "audio", audio_event: {audio_base_64: "..."}}
  -> our server -> base64 decode -> binary PCM frames
  -> Vonage WebSocket -> Vonage (converts to PSTN audio)
  -> User's phone speaker
```

### The `end_call` tool

The ElevenLabs agent has a client tool called `end_call`. When the agent determines all questions have been answered, it invokes this tool. Our WebSocket handler:

1. Receives the `client_tool_call` event with `tool_name: "end_call"`
2. Sends a `client_tool_result` back to ElevenLabs
3. Sets the `asyncio.Event` -- which unblocks the MCP tool
4. The MCP tool returns the collected transcript

If the user hangs up early (before all questions are answered), the Vonage WebSocket disconnects, the event is set anyway, and the partial transcript is returned.

---

## Step-by-step setup

### 1. Create a Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Follow the prompts -- choose a name and a username (must end in `bot`)
4. BotFather replies with your **bot token**: `123456789:ABCdef...`
5. Copy it -- you'll need it in step 3

### 2. Install dependencies

```bash
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
# Telegram
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNO...   # from BotFather
TELEGRAM_CHAT_ID=                                   # fill in after step 4
PUBLIC_BASE_URL=                                    # fill in after step 5
MCP_PORT=8001
WEBHOOK_PORT=8002

# Voice Call (Vonage + ElevenLabs)
ELEVENLABS_API_KEY=...
ELEVENLABS_AGENT_ID=...
VONAGE_APPLICATION_ID=...
VONAGE_PRIVATE_KEY=./private.key
VONAGE_PHONE_NUMBER=+44...
```

### 4. Get your Telegram chat ID

1. Open Telegram, find your new bot, send it any message (e.g. `hello`)
2. Run:
   ```bash
   python get_chat_id.py
   ```
3. Copy the Chat ID printed and paste into `.env` as `TELEGRAM_CHAT_ID`

### 5. Set up Vonage

1. Create a Vonage Application at [dashboard.nexmo.com/applications](https://dashboard.nexmo.com/applications)
2. Enable **Voice** capability
3. Download the `private.key` file and place it in the project root
4. Copy the **Application ID** into `.env` as `VONAGE_APPLICATION_ID`
5. Buy a virtual phone number and link it to the application
6. Copy the number into `.env` as `VONAGE_PHONE_NUMBER`

### 6. Set up ElevenLabs

1. Create an agent at [elevenlabs.io/conversational-ai](https://elevenlabs.io/conversational-ai)
2. Copy the **Agent ID** into `.env` as `ELEVENLABS_AGENT_ID`
3. Copy your **API key** into `.env` as `ELEVENLABS_API_KEY`
4. The server will automatically:
   - Create an `end_call` client tool on your ElevenLabs workspace
   - Assign it to the agent
   - Update the system prompt to mention `end_call`
   - Enable prompt override (so per-call questions can be injected)

### 7. Set up ngrok

Both Telegram and Vonage need a **public HTTPS URL** to reach your server. One ngrok tunnel covers everything -- both Telegram webhooks and Vonage WebSocket/events run on port 8002.

```bash
# In a separate terminal:
ngrok http 8002
```

ngrok prints something like:
```
Forwarding  https://abcd-1234.ngrok-free.app -> http://localhost:8002
```

Copy the `https://...` URL into `.env` as `PUBLIC_BASE_URL`:
```env
PUBLIC_BASE_URL=https://abcd-1234.ngrok-free.app
```

> **Every time you restart ngrok you get a new URL.** Update `.env`, restart the server, and re-run the setup steps below.

### 8. Configure Vonage webhooks

In the Vonage Dashboard, under your application's **Voice** settings:

| Setting | Value |
|---|---|
| **Answer URL** | `https://<your-ngrok-url>/call/answer` (GET) |
| **Event URL** | `https://<your-ngrok-url>/call/event` (POST) |

### 9. Start the servers

```bash
python main.py
```

You should see:
```
Starting MCP server on port 8001  ->  Notion connects at /mcp
Starting webhook server on port 8002  ->  register via GET /setup
end_call tool already exists: tool_xxxx
end_call tool already assigned to agent
Prompt override already enabled
```

### 10. Register the Telegram webhook

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

### 11. Verify voice call setup

```bash
curl http://localhost:8002/call/check
```

This validates:
- All env vars are present
- Vonage private key exists and JWT generation works
- ElevenLabs agent is reachable and has the `end_call` tool assigned
- Shows the exact URLs configured for Vonage

### 12. Test manually

**Telegram:** Send your bot a message in Telegram -- you should see it logged:
```
Received message from chat_id=987654321: 'hello'
Message from chat_id=987654321 -- no pending tool call waiting for a reply.
```

**Phone call:** Use the test script:
```bash
./scripts/test-mcp.sh call
```

---

## Connecting to Notion Custom Agent

In your Notion workspace:

1. Go to **Settings -> Notion AI -> AI connectors**
2. Enable **Custom MCP servers** (workspace admin only)
3. Open each Custom Agent's **Settings -> Tools & Access**
4. Click **Add connection -> Custom MCP server**
5. Enter the MCP URL: `https://your-public-url.com/mcp`
   - Local dev: use the ngrok URL for port 8001, e.g. `https://xxxx.ngrok-free.app/mcp`
   - Or run a second ngrok tunnel: `ngrok http 8001`
6. Enable the tools you want:
   - `send_message` -> set to **Run automatically**
   - `send_message_await_response` -> set to **Run automatically**
   - `call_user_await_response` -> set to **Run automatically**
7. Save

> **Important:** Each Notion Custom Agent needs its own separate MCP connection.

---

## MCP Tools Reference

### `send_message`

| | |
|---|---|
| **Purpose** | Fire-and-forget Telegram message |
| **Parameters** | `text: str` -- the message (supports HTML: `<b>`, `<i>`, `<code>`) |
| **Returns** | `"Message sent successfully."` |
| **Use case** | One-way notifications, reminders that don't need a reply |

### `send_message_await_response`

| | |
|---|---|
| **Purpose** | Send a Telegram message and block until the user replies |
| **Parameters** | `text: str` -- the question to ask |
| | `timeout_seconds: int` (default 900) -- how long to wait |
| **Returns** | The user's reply as a plain string |
| **Raises** | `TimeoutError` if no reply within timeout |
| **Use case** | Interactive check-ins: "What did you eat?", "Did you take your medication?" |

### `call_user_await_response`

| | |
|---|---|
| **Purpose** | Phone call the user, ask questions via AI voice agent, return transcript |
| **Parameters** | `phone_number: str` -- e.g. `"+447930002899"` |
| | `questions: list[str]` -- e.g. `["What did you eat?", "Did you exercise?"]` |
| | `timeout_seconds: int` (default 300) -- max call duration |
| **Returns** | Conversation transcript, each line prefixed `User:` or `Agent:` |
| **Raises** | `TimeoutError` if call doesn't complete in time |
| **Use case** | Escalation when Telegram gets no reply, or when voice is preferred |

---

## Shared state (`shared.py`)

```python
# Telegram: chat_id -> (asyncio.Event, reply_holder: list[str])
_pending: dict[int, tuple[asyncio.Event, list[str]]] = {}

# Voice: call_id -> {"event", "transcript", "questions", "call_uuid"}
_pending_calls: dict[str, dict[str, Any]] = {}

# Vonage UUID -> call_id (for correlating Vonage events)
_call_uuid_to_id: dict[str, str] = {}
```

All three dicts are plain Python dicts. No locks needed -- everything runs in one asyncio event loop.

---

## Webhook server endpoints

| Method | Path | Source | Purpose |
|---|---|---|---|
| POST | `/webhook` | Telegram | User message -> resolves `send_message_await_response` |
| GET | `/setup` | You (curl) | Registers webhook URL with Telegram API |
| GET | `/health` | You (curl) | Shows pending Telegram + call counts |
| GET | `/call/answer` | Vonage | Answer URL fallback (returns simple NCCO) |
| GET | `/call/event` | Vonage | WebSocket-leg events (query params) |
| POST | `/call/event` | Vonage | Main-leg events (JSON body) |
| WS | `/call/media-stream` | Vonage | Bidirectional PCM audio bridge |
| GET | `/call/check` | You (curl) | Validates voice call config |

---

## Environment variables

| Variable | Purpose |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your personal chat ID (from `get_chat_id.py`) |
| `PUBLIC_BASE_URL` | ngrok HTTPS URL pointing to port 8002 |
| `MCP_PORT` | MCP server port (default 8001) |
| `WEBHOOK_PORT` | Webhook server port (default 8002) |
| `ELEVENLABS_API_KEY` | ElevenLabs API key |
| `ELEVENLABS_AGENT_ID` | ElevenLabs Conversational AI agent ID |
| `VONAGE_APPLICATION_ID` | Vonage Voice application ID |
| `VONAGE_PRIVATE_KEY` | Path to Vonage private key file (default `./private.key`) |
| `VONAGE_PHONE_NUMBER` | Vonage virtual number (caller ID) |

---

## Writing agent instructions (prompt examples)

### NutritionBot -- asking what the user ate

```
When this agent runs, call send_message_await_response with the following message,
adapting the meal name to the time of day (Breakfast at 08:00, Lunch at 13:00,
Dinner at 19:00):

"Hey! What did you have for [MEAL]? Just reply in plain text -- e.g. 'oats,
banana, black coffee' -- and I'll log it for you."

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

### MedReminder -- with phone call escalation

```
Read the Medication & Supplements database.
For each medication where Schedule Time matches the current run time (within 15 minutes):

1. Call send_message_await_response:
   "Time for your [MEDICATION NAME] -- [PURPOSE]. Reply YES to confirm you've taken it."
   Use timeout_seconds: 900 (15 minutes).

2. If the reply contains YES (case-insensitive):
   Update the Last Taken field to now. Increment Streak Days by 1.

3. If send_message_await_response raises a TimeoutError:
   Escalate to a phone call. Call call_user_await_response with:
     phone_number: "+447930002899"
     questions: ["Have you taken your [MEDICATION NAME] today?",
                 "Is everything okay?"]
     timeout_seconds: 120

4. Parse the phone call transcript for confirmation.
   If confirmed: update the database as in step 2.
   If not confirmed or call failed: Increment Missed Doses by 1.
```

---

## CLI smoke tests (without Notion)

From the repo root, with `python main.py` running:

```bash
# Telegram: send_message_await_response (prompts; press Enter to keep each default)
./scripts/test-mcp.sh telegram

# Voice: call_user_await_response -- places a real outbound call
./scripts/test-mcp.sh call

# Use a public URL (e.g. ngrok http 8001)
MCP_URL=https://xxxx.ngrok-free.app/mcp ./scripts/test-mcp.sh telegram
```

Shared session helpers live in `scripts/mcp_session.sh` (sourced by `test-mcp.sh`).

---

## Production deployment (Cloud Run)

For production, replace ngrok with a proper public URL.

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
gcloud run deploy notioncares-mcp \
  --source . \
  --region europe-west2 \
  --allow-unauthenticated \
  --set-env-vars TELEGRAM_BOT_TOKEN=...,TELEGRAM_CHAT_ID=...,PUBLIC_BASE_URL=https://your-url.run.app,...
```

> Cloud Run only exposes one port publicly. For production, combine both servers
> onto a single port using a reverse proxy or restructure as a single FastAPI app
> with the MCP ASGI app mounted at `/mcp`.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `Webhook was not set` | Make sure `PUBLIC_BASE_URL` starts with `https://` |
| Bot sends message but reply never resolves | ngrok URL changed -- update `.env`, restart server, re-run `/setup` |
| `TELEGRAM_BOT_TOKEN not found` | Make sure `.env` is in the same directory as `main.py` |
| Notion can't connect to MCP | Check that port 8001 is also publicly reachable (second ngrok tunnel or Cloud Run) |
| `TimeoutError` in agent | User didn't reply in time -- expected. Use `call_user_await_response` as fallback. |
| Tool shows as "Always ask" in Notion | Agent Settings -> Tools & Access -> expand MCP connection -> toggle to "Run automatically" |
| Call ends immediately after answering | Check `curl http://localhost:8002/call/check` -- usually a missing env var or ElevenLabs config issue |
| `Override for field 'prompt' is not allowed` | Restart the server -- `ensure_end_call_tool()` enables prompt override automatically |
| ElevenLabs connection closes instantly | Ensure `compression=None` in `websockets.connect()` (already set in code) |
| Vonage GET /call/event returns 405 | Ensure you're running the latest code -- both GET and POST handlers are needed |
| `end_call` tool not invoked | Check the agent's system prompt includes end_call instructions; restart server to run `ensure_end_call_tool()` |
