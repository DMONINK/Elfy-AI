[![Try Now](https://img.shields.io/badge/Try%20Now-%F0%9F%8C%B8%20Live%20Demo-b19fdd?style=for-the-badge)](https://dmonink.github.io/Elfy-Image-Generator/)

# 🌙 Elfy

**A Discord bot with a soul, not a script.**

Elfy (inspired by [hihumanzone's Gemini-Discord-Bot](https://github.com/hihumanzone/Gemini-Discord-Bot)) chats like a real friend, remembers *you* specifically, draws and edits pictures, plays favorites with people you choose, and runs her whole brain through a phone-friendly dashboard — no code required. Powered by Google's Gemini API.

---

## 🧠 The big idea: she remembers *people*, not *channels*

Every single person gets their own private memory with Elfy — even if 50 people are typing in the same channel. Nobody sees anybody else's history. Ever.

---

## 💬 Everything she does in normal chat

| Feature | In one breath |
|---|---|
| **Just talk to her** | She replies like a person — short, warm, playful. Max 4 lines, always. |
| **Reply to a message** | Quote her (or anyone) and she knows exactly what you're referencing — pics included. |
| **Text her back-to-back** | Send 5 messages fast? She reads them as one thought and replies once, not five times. |
| **"Draw me a..."** | Say it like a human and she generates an image — no command needed. |
| **"Turn this photo into..."** | Attach a pic + instruction and she transforms it for real. |
| **Drop a file in** | Images, PDFs, audio, code — she reads it. |
| **"@Elfy help"** | Mention her with "help" anywhere and she drops a quick cheat-sheet, then vanishes it after 5 sec. |
| **@Mention her off-channel** | She won't chat there, but she'll politely point you to the right spot. |
| **DM her** | No setup, no channel rules — she just replies. |

---

## 🕹️ Slash commands

| Command | What happens |
|---|---|
| `/forget [persona]` | Nukes *your* memory with her. Add a persona and she'll roleplay it just for you. |
| `/mymemories` | Shows you — privately — everything she's learned about you. |
| `/createthread` | Spins up a thread she'll actively live in. |
| `/setchat` | Sets her one home channel per server. |
| `/status` | Uptime, server count, chat stats — instantly. |
| `/botrestart` | Reboots her. Owners only. |
| `/help` | The cheat-sheet, on demand. |

---

## 👑 VIPs — her inner circle

- **First message ever?** She greets them specially, once, forever remembered.
- **Every message after?** She quietly treats them differently — tone, warmth, vibe — based on a relationship label you set (bestie, sibling, whatever).
- **Fully editable:** name, relationship, private personality note, greeting text — all from the dashboard, zero code.
- **"Sync from code" button** resets the live roster back to whatever's hardcoded — handy, but wipes dashboard-only VIPs, so it double-checks with you first.

---

## 🎛️ The dashboard — her control room

One shared password locks the whole thing down. No password set? It stays sealed until you add one.

| Page | Purpose |
|---|---|
| **Overview** | Big stats at a glance. |
| **Servers** | Every server she's in + her channel there. |
| **VIPs** | Add, edit, remove, re-arm greetings. |
| **Settings** | Personality, creativity sliders, reply length, safety filters, owners. |

Changes go live **instantly** — no restart. Sessions last 7 days but reset on bot restart. Passwords are checked in a way that can't leak timing info.

---

## 🎨 Image magic, explained fast

- **Picture of Elfy herself** → she reuses a cached "reference look" so she's always recognizably *her*, editing that base image instead of starting fresh.
- **Picture of literally anything else** → Gemini writes a rich prompt, then Pollinations.AI paints it.
- **Editing your upload** → your photo + your words go straight into Gemini's image editor — a true transformation, not a redraw.
- Every image comes back as a real file attachment, never a broken embed link.

---

## 👋 Welcome messages

New member joins → she posts a **fresh, never-repeated** AI greeting, tags them by name, keeps it to 2 lines max. No system channel set up? She just skips it — no drama, no error.

---

## ✂️ Reply-length enforcement (how she stays short)

1. **Short already?** Sent as-is, instantly.
2. **Too long?** She asks herself to rewrite it shorter, once.
3. **Still too long?** Hard-cut to size — guaranteed, no exceptions.

Her own memory always matches what you *actually* saw, even after a trim.

---

## 📎 File support

| Type | Formats |
|---|---|
| 🖼️ Images | png, jpg/jpeg, webp, heic, heif |
| 🎧 Audio | wav, mp3, aiff, aac, ogg, flac |
| 📄 Text | html, css, md, csv, xml, rtf |
| 📚 Docs/code | pdf, js, py |

Unsupported file? She'll tell you plainly instead of ghosting you.

---

## 💾 What sticks around forever

Memories, VIPs, thread/channel settings, greeted status, dashboard tweaks, her cached selfie look, and full chat logs — all saved through Replit DB (or a local backup file if you're not on Replit). Restarts and redeploys can't touch any of it.

---

## ⚙️ Every setting, at a glance

| Setting | Default | Controls |
|---|---|---|
| Presence text | "with your feelings" | Her Discord status |
| Personality | (built-in) | Her core vibe |
| Welcome style | (built-in) | New-member greetings |
| Appearance | (built-in) | Her consistent look |
| Chat creativity | 0.95 / 0.96 / 40 | Randomness of replies |
| Reply length cap | 500 tokens / 4 lines | How much she says |
| Image creativity | 0.9 / 0.5 / 40 | Prompt-writing randomness |
| Message chunk size | 1900 chars | When she splits a long message |
| Owner list | (from env) | Who bypasses permissions |
| Safety x4 | see below | What content gets filtered |
| Memory window | 12 messages | How much recent chat she tracks |
| Memory check-in | every 15 msgs | How often she updates facts |
| Memory cap | 25 facts | Max facts stored per person |

Bad value submitted? The **whole save** gets rejected with a clear reason — nothing silently breaks.

---

## 🛡️ Safety filters

Four independent dials — harassment, hate speech, sexual content, dangerous content — each set to one of:

`BLOCK_NONE` → `BLOCK_ONLY_HIGH` → `BLOCK_MEDIUM_AND_ABOVE` → `BLOCK_LOW_AND_ABOVE` (strictest)

Defaults block the first and last at medium+, leave sexual content fully open — all changeable anytime.

---

## 🗂️ What's inside the codebase

| File | Job |
|---|---|
| `main.py` | Boots everything at once |
| `settings.py` | Built-in defaults |
| `dashboard_settings.py` | Live-editable overrides |
| `ai_service.py` | Talks to Gemini + Pollinations |
| `core_memory.py` | Stores durable facts |
| `message_handler.py` | Decides who gets a reply |
| `commands.py` | All 7 slash commands |
| `help_command.py` | Shared help text |
| `attachments.py` | Downloads + reads files |
| `vip_users.py` | VIP roster logic |
| `welcome.py` | New-member greetings |
| `conversation_log.py` | Human-readable chat logs |
| `storage.py` | Saves everything, forever |
| `web_dashboard.py` | The whole control panel |
| `backup.py` | Daily backups (built, not wired in) |
| `requirements.txt` | Her dependency list |

---

## 🚀 Getting her running

1. `pip install -r requirements.txt`
2. Set your environment variables (below)
3. Run `python main.py`
4. She's online 🎉

> ⚠️ **Must-do:** Enable **Server Members Intent** in the Discord Developer Portal or she won't boot.

Built for Replit, but happy anywhere with Python 3.12 — a VPS, even a phone via Termux.

| Variable | Needed? | For |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ Yes | Logging in |
| `GOOGLE_AI_KEY` | ✅ Yes | Every reply, image, and memory |
| `DASHBOARD_PASSWORD` | 🔒 Recommended | Locking the dashboard |
| `OWNER_IDS` | Optional | Command permission bypass |
| `POLLINATIONS_API_KEY` | Optional | Steadier image generation |

---

## 🧩 Known quirks (harmless, just FYI)

- `/help`'s printed list is a bit stale — missing `/mymemories` and `/botrestart`, though both work fine.
- `backup.py` exists and works, but isn't turned on by default.
- An old, unused storage key (`"history:"`) sits around harmlessly — safe to ignore.
- The image-generation model has drifted to a wrong value once before — worth a glance if generation ever looks off.

---

📜 Licensed under **GPL v3.0**.

*Made with 💜 for a bot who's more friend than assistant.*
