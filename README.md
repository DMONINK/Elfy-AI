[![Try Now](https://img.shields.io/badge/Try%20Now-%F0%9F%8C%B8%20Live%20Demo-b19fdd?style=for-the-badge)](https://dmonink.github.io/Elfy-Image-Generator/)

# 🌙 Elfy

**A Discord bot that actually feels like a person.**

inspired by [hihumanzone's Gemini-Discord-Bot](https://github.com/hihumanzone/Gemini-Discord-Bot) Elfy is not another boring "how can I assist you today" bot. Elfy chats like a real friend, remembers *you* specifically, draws pictures on request, and has her own little dashboard you can control her from — no coding needed. She's powered by Google's Gemini API, and every conversation is private to the person having it.

---

## ✨ What makes her cool

| | |
|---|---|
| 🧠 **Remembers you** | She keeps her own private memory of *you* — separate from everyone else, even in the same channel. Facts stick around even after she restarts. |
| 💬 **Talks like a human** | Short, casual replies capped at a few lines. If her first draft runs long, she rewrites it shorter herself — no walls of text, ever. |
| 🎨 **Draws pictures** | Just ask — "draw me a dragon" or "show me a picture of you." She even keeps her own look consistent across selfies. |
| ✂️ **Edits your photos** | Upload an image and say "turn this into a painting" — she'll transform it, not just describe it. |
| 👑 **VIP mode** | Give your best friends a special relationship with her — she'll greet them once and treat them differently than everyone else from then on. |
| 🎛️ **Web dashboard** | Change her personality, settings, and more from your phone's browser — zero code, zero redeploys. |
| 👋 **Welcomes new members** | Every new person gets a fresh, AI-written greeting — never the same one twice. |
| 📦 **Handles message bursts** | Send five messages in a row? She waits, reads them as one thought, and replies once. |
| 📎 **Reads your files** | Images, PDFs, audio clips, code files, and more — drop one in and she'll actually look at it. |
| 🧵 **Works in threads & DMs** | One main channel per server by default, but you can add extra threads she'll follow, and she always replies in DMs. |

---

## 🕹️ Try the commands

| Command | What it does |
|---|---|
| `/forget [persona]` | Wipe *your* history and memories with her. Add a persona and she'll roleplay as it just for you — e.g. `persona: a grumpy pirate` |
| `/mymemories` | See exactly what she's picked up about you, as a private list only you can see |
| `/createthread` | Spin up a new thread she'll actively chat in |
| `/setchat` | Pick the one channel she talks in per server |
| `/status` | Quick stats — uptime, servers, total chats |
| `/botrestart` | Restart her (bot owners only) |
| `/help` | Get a quick cheat-sheet of everything above |

---

## 👑 The VIP system, quickly explained

VIPs are your inner circle — a handful of people Elfy treats differently from everyone else:

- The **first time** a VIP ever messages her, she sends a special one-time greeting before anything else. It only fires once (unless you re-arm it from the dashboard).
- On **every** message after that, she quietly knows who they are to her — best friend, sibling, whatever label you gave them — and it shapes her tone without her ever mentioning it out loud.
- Each VIP has four editable bits: their **name**, a **relationship label** (e.g. "bestie"), a **personality note** (private context for how she treats them), and their **greeting text**. All four live on the dashboard — no code required.

---

## 🚀 Getting it running

1. `pip install -r requirements.txt`
2. Set your environment variables (see below)
3. Run `python main.py`
4. Done — she's online! 🎉

> ⚠️ One must-do: turn on **Server Members Intent** in the Discord Developer Portal, or she'll refuse to start.

Runs anywhere with Python 3.12 — Replit, a VPS, even a phone with Termux.

| Variable | Needed? | For |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ Yes | Logging the bot in |
| `GOOGLE_AI_KEY` | ✅ Yes | Every reply, image, and memory (Gemini) |
| `DASHBOARD_PASSWORD` | 🔒 Recommended | Locks the web dashboard |
| `OWNER_IDS` | Optional | Discord IDs that bypass permission checks |
| `POLLINATIONS_API_KEY` | Optional | More reliable image generation |

---

## 🛠️ Good to know

- Her dashboard is password-protected — no password set means it stays locked down until you add one.
- Everything important survives a restart: memories, VIPs, settings, even her cached selfie look.
- Content filters (harassment, hate speech, sexual content, dangerous content) are each independently adjustable from the dashboard — no code changes needed.
- She can read images, PDFs, audio (`.mp3`, `.wav`, and friends), and a handful of text/code formats as attachments. Anything unsupported, she'll just tell you so instead of silently ignoring it.
- Sending a burst of messages doesn't spam multiple replies — she waits a few seconds, treats it as one thought, and answers once, even if you're mid-ramble.
- A few small things are slightly out of date under the hood (like `/help`'s printed command list) — nothing that affects how she actually runs day-to-day.

---

📜 Licensed under **GPL v3.0**.

*Made with 💜 for a bot who's more friend than assistant.*
