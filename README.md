# 🧚 Elfy

**A Gemini-powered Discord companion with a memory that actually works.**

Elfy chats like a real presence in your server, remembers who you are (without mixing you up with anyone else), draws pictures of herself and anything else, and adapts to whatever personality you give her — all backed by a self-hosted web dashboard.

---

## ✨ What she does

- 💬 **Chats naturally** — her home channel, tracked threads, or straight to your DMs
- 🧠 **Actually remembers you** — facts picked up over time, kept fully separate per server
- 🎨 **Draws things** — including herself, and she looks the same every time
- ✏️ **Edits your images** — upload one, describe the change
- 👋 **Greets new members** — AI-written, customizable per server
- 🏷️ **Renames herself** — just ask nicely
- 🎛️ **Fully dashboard-controlled** — personality, safety, memory size — live, no redeploy

## 🕹️ Commands

Every command works two ways: as a slash command, or by tagging her (`@Elfy forget`) — anywhere, not just her home channel.

| Command | Does what |
|---|---|
| `/help` | Shows what she can do |
| `/forget [persona]` | Wipes this channel's memory — optionally give her a new personality too |
| `/forgetme` | Erases everything she's learned about you, everywhere |
| `/mymemories` | Shows what she remembers about you, here |
| `/createthread <name>` | Starts a thread she'll fully chat in |
| `/setchat <channel>` | Sets her one home channel |
| `/setwelcome <text>` | Adds your own line to new-member greetings |

Owner-only, tag-only (never shown as slash commands, on purpose): `status`, `restart`, `memories <id>`, `mhelp`.

## 🧠 How her memory works

Two layers. A short **rolling window** per channel — what's actually being said right now. And long-term **core memory** per person *per server* — the facts worth keeping. Nothing you tell her in one server ever follows you into another.

## 🏗️ Under the hood

Discord bot + web dashboard, one process, one event loop. Gemini for chat, memory, and image editing. Pollinations for general image generation. Replit DB (or local `shelve` when running elsewhere) for storage.

## 🚀 Running it

```bash
pip install -r requirements.txt
python main.py
```

Needs `DISCORD_TOKEN` and `GOOGLE_AI_KEY` at minimum — see `.env.development` for the full list.

## 📁 The codebase, at a glance

```
main.py                 entry point
ai_service.py            Gemini + image generation
core_memory.py           long-term memory
storage.py                persistence layer
commands.py               every command's logic
mention_commands.py      the "@Elfy <command>" side of every command
message_handler.py       the message pipeline
web_dashboard.py          the control panel
tests/                     offline test suite
```

## 📄 License

GPL-3.0 — see `LICENSE`.
