# Elfy — Discord AI Chatbot

A conversational Discord bot powered by **[Google Gemini](https://ai.google.dev/gemini-api/docs/api-key)** for chat & **[Pollinations.ai](https://pollinations.ai)** for image generation. Elfy maintains per-channel memory, handles images and documents as attachments, and can generate or transform images on request.

[![Try Now](https://img.shields.io/badge/Try%20Now-%F0%9F%8C%B8%20Live%20Demo-b19fdd?style=for-the-badge)](https://dmonink.github.io/Elfy-Image-Generator/)

---

## Features

- **Conversational chat** — Gemini-backed responses with persistent per-channel history across bot restarts
- **Single-channel AI chat** — `/setchat #channel` designates the one channel per server where Elfy chats; mentioning her elsewhere gets a quick, self-deleting redirect instead of a reply
- **AI-generated welcome messages** — greets new members in the server's system channel with a short, unique Gemini-generated greeting (never the same message twice)
- **Text-to-image generation** — routes image requests to Pollinations.ai (Flux model); Gemini first expands the prompt for better detail
- **Image editing** — attach a photo and ask Elfy to transform it (anime, cartoon, painting, etc.) using Gemini's native image model

  **Note:** Image editing will not work with free tier API, you'll need paid Gemini API for this to work.
- **Multimodal inputs** — accepts images, audio, PDFs, code files, and CSVs as Discord attachments
- **Custom personas** — `/forget [persona]` resets history and sets a new personality on the fly
- **Tracked threads** — `/createthread` spawns a dedicated thread where Elfy responds to every message automatically
- **DM support** — talk to Elfy directly without mentioning her

---

## Setup

### 1. Prerequisites

- Python 3.10+
- A Discord bot token — [discord.com/developers/applications](https://discord.com/developers/applications)
- A Google Gemini API key — [Google AI Studio](https://aistudio.google.com/) (free tier: up to 60 req/min)
- A Pollinations.ai API key — [Pollinations.ai](https://pollinations.ai) (required for authenticated image generation)

### 2. Clone and install

```bash
git clone https://github.com/DMONINK/Elfy-AI
cd Elfy-AI
pip install -r requirements.txt
```

### 3. Configure environment

Create a `.env` file in the project root:

```env
DISCORD_BOT_TOKEN=your_discord_bot_token
GOOGLE_AI_KEY=your_gemini_api_key
POLLINATIONS_API_KEY=your_pollinations_api_key
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Bot token from the Discord developer portal |
| `GOOGLE_AI_KEY` | ✅ | Gemini API key from Google AI Studio |
| `POLLINATIONS_API_KEY` | ✅ | Pollinations.ai key for authenticated image generation |

### 4. Enable required Discord intents

In the [Discord Developer Portal](https://discord.com/developers/applications), open your bot's page → **Bot** → **Privileged Gateway Intents**, and turn on:

- **Server Members Intent** — required for the welcome-message feature (`on_member_join`); without it, the bot fails to start with a `PrivilegedIntentsRequired` error
- **Message Content Intent** — required for the bot to read message text at all

### 5. Run

```bash
python main.py
```

---

## Usage

Elfy responds when:
- A message is sent in the server's **designated AI-chat channel** (set with `/setchat #channel` — see below)
- Messaged in a **DM**
- Active in a **tracked thread** (created via `/createthread`)

If Elfy is @mentioned in any *other* channel, she won't chat there — instead she replies with a short redirect ("Please talk to me in #channel") that deletes itself after a few seconds, and otherwise ignores conversation in that channel entirely.

All AI chat replies are plain text and capped at 4 lines; longer responses are automatically shortened before sending.

### Commands

| Command | Description |
|---|---|
| `/setchat #channel` | Set the one channel where Elfy chats (requires **Manage Server** permission) |
| `/forget` | Clear chat history for the current channel |
| `/forget [persona]` | Clear history and set a new persona (e.g. `/forget a pirate`) |
| `/createthread [name]` | Create a thread where Elfy responds to every message |

Slash command responses are formatted as embeds; AI chat replies are always plain text.

### Image generation

Say anything containing phrases like *generate an image*, *create a picture*, *draw*, *imagine*, *render*, etc. and Elfy will route the request to Pollinations.ai.

```
generate an image of a fox in a neon city at night
```

### Image editing

Attach a photo and use phrases like *turn this into*, *anime style*, *cartoonify*, *restyle this*, etc.

```
[attach photo] make this anime style
```

---

## Customization

All configuration lives in `settings.py`.

### Bot persona / system prompt

Edit `BOT_TEMPLATE` to change Elfy's personality or starting instructions:

```python
BOT_TEMPLATE = [
    {'role': 'user', 'parts': ["You are a helpful assistant."]},
    {'role': 'model', 'parts': ["Got it! Ready to help."]},
]
```

### AI-chat channel

The designated channel is no longer set in `settings.py` — it's configured per-server at runtime with `/setchat #channel` and persisted in the shelve database, so each server can pick its own without editing code or redeploying. `/createthread` still works the same way for one-off threads that respond to everything.

### Reply length

```python
MAX_REPLY_LINES = 4
```

Controls the line cap enforced on every AI chat reply (see [How it works](#how-it-works)).

### Generation parameters

```python
TEXT_GENERATION_CONFIG = {
    "temperature": 0.75,
    "top_p": 0.96,
    "top_k": 40,
    "max_output_tokens": 512,
}

IMAGE_GENERATION_CONFIG = {
    "temperature": 0.2,
    "top_p": 0.9,
    "top_k": 32,
    "max_output_tokens": 800,
}
```

### Content safety

```python
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT",        "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH",        "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT",  "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT",  "threshold": "BLOCK_MEDIUM_AND_ABOVE"},
]
```

Set any threshold to `"BLOCK_NONE"` to disable that filter.

---

## How it works

```
Discord message
      │
      ▼
 Channel check
 ┌──────────────────────────────────────────┐
 │ @mentioned outside the chat channel?      │
 │   → self-deleting redirect reply, stop    │
 │ not the chat channel / DM / tracked thread│
 │   → ignored silently, stop                │
 │ otherwise → continue                      │
 └──────────────────────────────────────────┘
      │
      ▼
 Intent detection
 ┌────────────────────────────────────┐
 │ image edit? → Gemini image model   │
 │ image gen?  → Gemini prompt boost  │
 │              → Pollinations.ai Flux│
 │ text chat?  → Gemini chat session  │
 │              → shortened to 4 lines│
 └────────────────────────────────────┘
      │
      ▼
 Reply in Discord (plain text, split to 1900 chars, images as file attachments)
      │
      ▼
 History saved to local shelve DB
```

New member welcomes run as a separate pipeline, outside the message flow above:

```
Member joins server
      │
      ▼
 Gemini generates a fresh 1–2 sentence greeting
 (retried if it matches a recent greeting)
      │
      ▼
 Posted in the server's System Messages channel, @mentioning the new member
```

---

## File structure

```
├── main.py             # Entry point, bot initialization
├── ai_service.py       # Gemini + Pollinations.ai integration, reply-length enforcement
├── message_handler.py  # Discord message routing, channel gating, redirect notices
├── welcome.py          # Gemini-generated welcome messages (on_member_join)
├── commands.py         # Slash commands (/setchat, /forget, /createthread)
├── attachments.py      # Attachment download and MIME detection
├── storage.py          # Persistent chat history + chat-channel settings (shelve)
├── settings.py         # All configuration and environment loading
└── requirements.txt
```

---

## Notes

- Chat history persists between bot restarts via Python's `shelve` module (`chatdata.*` files)
- The AI-chat channel set via `/setchat` is also stored in `chatdata.*` and is remembered per-server
- Welcome messages require the **Server Members Intent** to be enabled in the Discord Developer Portal (see [Setup](#setup)) — without it the bot won't start
- Discord's API only supports true ephemeral (visible-to-one-person) messages for slash command responses, not plain @mentions — the redirect notice uses a self-deleting reply instead, which is the closest available equivalent
- Error logs are written to `errors.log` at runtime
- A lightweight health-check HTTP server runs on port `8080` (useful for uptime monitors and hosting platforms)
- Image attachment context is not retained in chat history due to API limitations

---

## License

MIT — see [LICENSE](LICENSE) for details.
