# Elfy — Gemini-Powered Discord Companion Bot

Elfy is a persistent, personality-driven Discord chatbot built on Google's
Gemini API. She holds real per-person conversations (not shared per-channel
ones), remembers durable facts about individual users across restarts,
generates and edits images, gives configured people ("VIPs") their own
private relationship dynamic with her, and is fully configurable — and
inspectable — through a built-in password-protected web dashboard, with
zero code edits or redeploys required for most changes.

This document describes **everything the current codebase actually does**,
file by file and feature by feature, plus how to run it and what to expect
from each piece. It reflects the code as of this scan — if you change
behavior, please keep this file in sync.

> **Note on this README:** the version of this file previously at the
> project root was actually the `tests/` folder's own README (a guide to
> running the project's offline test suite), not a description of the bot
> itself. That content, if you still need it, belongs in `tests/README.md`
> — this file replaces it as the actual project README. The `tests/`
> directory itself (referenced throughout `CHANGES.md`) is not part of
> this specific code drop, so its contents aren't described here beyond
> what `CHANGES.md` documents about it.

---

## Table of contents

1. [What Elfy is, in one paragraph](#what-elfy-is-in-one-paragraph)
2. [Architecture overview](#architecture-overview)
3. [Core concept: channel-scoped conversation, guild-scoped memory](#core-concept-channel-scoped-conversation-guild-scoped-memory)
4. [Everything Elfy can do in chat](#everything-elfy-can-do-in-chat)
5. [Slash commands — full reference](#slash-commands--full-reference)
6. [Owner-only commands (tag-only, not slash)](#owner-only-commands-tag-only-not-slash)
7. [The VIP system](#the-vip-system)
8. [The web dashboard — full reference](#the-web-dashboard--full-reference)
9. [Message batching](#message-batching)
10. [Image generation and editing, in depth](#image-generation-and-editing-in-depth)
11. [Welcome messages for new members](#welcome-messages-for-new-members)
12. [Reply-length enforcement](#reply-length-enforcement)
13. [Attachments — what files Elfy can read](#attachments--what-files-elfy-can-read)
14. [Storage and persistence](#storage-and-persistence)
15. [Configuration reference (every setting, where it lives)](#configuration-reference-every-setting-where-it-lives)
16. [Safety settings](#safety-settings)
17. [File-by-file map of the codebase](#file-by-file-map-of-the-codebase)
18. [Setup and deployment](#setup-and-deployment)
19. [Environment variables](#environment-variables)
20. [Known rough edges / things worth knowing](#known-rough-edges--things-worth-knowing)
21. [License](#license)

---

## What Elfy is, in one paragraph

Elfy is a Discord bot that behaves like a real person with a consistent
personality ("a witty, warm, and confident girl chatting with friends on
Discord") rather than a generic assistant. She chats in one designated
channel per server (or in DMs, or in threads created for her), keeps a
bounded rolling memory of her recent conversation with *each individual
person* plus a small set of durable long-term facts about them, generates
or edits images on request (including pictures of herself, with a
consistent appearance), greets new server members with a freshly
AI-written welcome message every time, treats a configurable list of
specific people as "VIPs" with their own private relationship dynamic and
one-time greeting, and exposes almost all of the above — personality,
generation parameters, safety thresholds, VIP roster, live conversation
logs — through a small self-hosted web control panel.

## Architecture overview

Elfy runs as a single Python process (`main.py`). Inside that one process,
two things run concurrently on the same `asyncio` event loop:

- **The Discord bot itself** (`discord.py`'s `commands.Bot`), handling
  gateway events (messages, member joins) and slash commands.
- **The web dashboard** (an `aiohttp` web app), served on port `8080`.

Running both in the same event loop (rather than a separate thread or
process for the dashboard) is a deliberate choice: it lets dashboard
request handlers read live bot state directly (`bot.guilds`, active AI
sessions) and safely `await` real bot calls — e.g. updating Discord
presence the instant a setting is saved — without any cross-thread
locking or message-passing.

High-level request flow for an ordinary chat message:

```
Discord message
  -> main.py's on_message
    -> message_handler.handle_message()
      -> "@Elfy <command>"?  -> mention_commands.py (see below), stop here if so
      -> gating: is it a DM / the designated channel / a tracked thread?
      -> VIP one-time greeting check
      -> attachment download (attachments.py)
      -> message batching buffer (message_handler.py)
        -> (after a short debounce) construct the query text
          -> ai_service.AIService.generate_response()
            -> image request?     -> Pollinations or Gemini image-edit model
            -> nickname request?  -> signaled back for message_handler.py to execute
            -> otherwise          -> Gemini chat model, per-(server, channel) session
          -> reply sent to Discord (split if too long), or nickname change applied
          -> chat history persisted, scoped by (server, channel) (storage.py)
          -> exchange logged for the dashboard (conversation_log.py)
          -> every N messages: background core-memory extraction, scoped by (server, person)
```

## Core concept: channel-scoped conversation, guild-scoped memory

This is the single most important architectural fact about Elfy, and it
shapes almost every other feature, so it's worth explaining up front.

**Two earlier models, in order (neither is how this codebase works now):**
conversation history started out keyed by Discord *channel* — everyone
talking to Elfy in the same channel shared one running conversation, which
grew without bound the longer the bot stayed up, and one person's private
conversation could leak into what Elfy "remembered" while talking to
someone else in the same channel. That was fixed by re-keying everything
to Discord *user ID* instead — bounded per person, no more same-channel
bleed — but that fix had its own blind spot: the *same* Discord user
chatting in two *different* servers shared one history too, so specifics
from one server (including secrets or named individuals) could surface in
a completely unrelated server. That was a real, shipped bug — see
`CHANGES.md` — not a hypothetical.

**The current model** fixes both at once by using *two different scopes*
for the two different kinds of memory:

1. **Rolling short-term window** (`ai_service.py`'s `self._history`, sized
   by the `core_memory_window_size` setting, default **100** entries) is
   scoped per **(server, channel)** — every distinct channel/thread/DM has
   its own bounded transcript, shared by everyone talking to Elfy in it
   (each message is tagged with who sent it, so she can still tell
   speakers apart within one shared transcript — see
   `message_handler.construct_query`). This is the actual recent
   back-and-forth re-sent to Gemini on every reply, and it's what `/forget`
   clears — for the channel it's run in, not for a specific person.
   Once a channel's window exceeds the size limit, the oldest entries are
   dropped immediately (not just at read time), so both the next prompt
   sent to Gemini *and* what's persisted to storage stay bounded no matter
   how long a channel has been active in total.

2. **Durable "core memory"** (`core_memory.py`, capped by
   `core_memory_fact_cap`, default **100** facts) is scoped per
   **(server, person)** — a fact distilled about someone in one server
   never surfaces while Elfy is replying to that same person in a
   different server, and DMs get their own separate bucket per person too.
   Every `core_memory_extraction_interval` messages (default **20**) from
   a given person in a given server, a background task asks Gemini to
   distill anything genuinely worth remembering long-term about *that
   specific person* — their name/nickname, relationships, pets,
   job/school, ongoing situations, strong preferences, running jokes,
   things they explicitly asked to be remembered — as opposed to routine
   greetings or one-off small talk (and, since the transcript it reads
   from can now include other people's messages too, it's explicitly
   prompted to only extract facts attributable to the one named person,
   not whoever else was also in the channel). New facts are merged into
   that (server, person) pair's stored list (deduplicated,
   case-insensitively). If the list grows past the cap, a second Gemini
   call *consolidates* it — merging overlapping facts and dropping the
   least useful ones — rather than simply chopping off the oldest entries,
   so a durable fact doesn't get silently evicted by a run of trivial
   recent ones. If that consolidation call itself fails for any reason,
   the code falls back to keeping just the most recent `cap` facts, so a
   fact list can never get permanently stuck over its limit.

Two commands are deliberate, explicit exceptions to per-server memory
isolation — see `core_memory.py`'s cross-guild helpers:

- **`/forgetme`** wipes a person's core memory in *every* server at once
  (plus a legacy storage key from before this scoping existed), not just
  the one it's run in — the point of "forget me" is not having to repeat
  it in every server you've talked to Elfy in. Contrast with `/forget`,
  which only ever touches the one channel it's run in and never reaches
  into anyone's core memory.
- The **owner-only cross-server memory lookup** (`@Elfy memories <user
  id>` — see "Owner-only commands" below) shows everything stored about a
  given user, broken out by server, for moderation/support purposes.

Every single turn, right before sending a message to Gemini, Elfy builds a
brand-new (never persisted, never reused) "session" from scratch, made of:
the channel's personality template (or a one-off custom persona set via
`/forget <persona>`, which now applies to that channel rather than a
specific person) -> a freshly formatted "what you remember about the
person speaking *this* turn, in *this* server" note built live from their
current core memory -> the channel's bounded rolling window of actual
recent messages. Building this fresh every turn is cheap (it's a local,
in-memory SDK call, not a network request) and is what guarantees a
personality change made on the dashboard takes effect on the very next
message in a channel, even mid-conversation — nothing about an ongoing
chat is "baked in" the way it would be with one long-lived session object.

One consequence worth knowing: **`/mymemories` only ever shows what's
remembered about *you*, in the *current* server or DM** — not aggregated
across every server you've talked to Elfy in (that aggregation is
deliberately reserved for the owner-only lookup above).

## Everything Elfy can do in chat

None of this requires a slash command — it all happens through ordinary
conversation in whichever channel/thread/DM Elfy is listening in.

### Ordinary conversation

Talk to her like a person. She replies in character as "Elfy" — casual,
warm, a little playful, first-person — and every reply is capped at a
configurable number of lines (**4** by default) so she never dumps a wall
of text into a fast-moving group chat. If Gemini's first draft comes back
longer than that, Elfy asks Gemini to rewrite it more concisely once; if
that *still* doesn't fit, the reply is hard-truncated as a guaranteed
last resort. See [Reply-length enforcement](#reply-length-enforcement).

### Replying to a specific message

If you reply to one of Elfy's messages (or anyone else's) rather than
sending a fresh message, Elfy is told who you were quoting and what that
message said, and factors that context into her answer, including any
image attached to the message you replied to.

### Sending a burst of messages in a row

If you send several messages within a few seconds of each other, Elfy
doesn't reply to each one individually — she waits briefly, combines them
into a single connected thought, and sends one reply. See
[Message batching](#message-batching).

### Asking for an image

Say things like *"generate an image of..."*, *"draw a picture of..."*,
*"show me a photo of..."*, etc., and Elfy generates one and posts it
directly in chat. For a picture of Elfy herself specifically, any of
create/make/generate/draw/show/send/take/give combined with
selfie/picture/pic/image/photo reliably works — e.g. *"send a selfie"*,
*"show me a picture of you at the beach"* — see
[Image generation and editing, in depth](#image-generation-and-editing-in-depth)
for exactly which phrasings trigger this and how the two different
generation paths (herself vs. anything else) work.

### Asking her to edit an uploaded image

Attach an image and say something like *"turn this photo into..."* or
*"edit this picture to..."*, and Elfy sends your image plus your
instruction to Gemini's native image model and returns a transformed
version, rather than generating something new from text alone.

### Asking her to change her own nickname

Say something like *"change your name to Sparkling Angel"* or *"rename
yourself to Star"* and Elfy attempts to change her own nickname in that
server (needs the **Change Nickname** permission — the one Discord uses
for a member editing their *own* nickname, distinct from **Manage
Nicknames**, which covers editing everyone else's — if she doesn't have
it, she says so rather than failing silently). Available to any member,
not just server managers.

### Uploading files

Elfy can accept and read images, several document/text formats, and a
couple of audio formats as attachments — see
[Attachments](#attachments--what-files-elfy-can-read) for the exact
supported list.

### "@Elfy \<command\>"

Every command in this README — slash or not — also works by @mentioning
the bot followed by the command word instead of using Discord's slash-command
UI, e.g. `@Elfy forget`, `@Elfy setchat #general`, `@Elfy mymemories`. This
works in any channel, including ones Elfy would otherwise ignore or
redirect you from, and every response auto-deletes after 10 seconds except
`help`/`mhelp`, which persist. See `mention_commands.py` for the full
command-word list and argument parsing, and "Owner-only commands" below —
those are reachable *only* this way, never as a slash command.

### Being @mentioned outside her designated channel

If you @mention Elfy in a server channel that *isn't* her designated chat
channel and isn't a tracked thread (and it's not a recognized command —
see above), she doesn't ignore you and doesn't chat there either — she
replies once, pointing you to the correct channel (or telling you none has
been set up yet, if that's the case), and that notice auto-deletes after
~5 seconds. Ordinary conversation with no @mention in a channel she's not
listening to is simply ignored entirely — no response, no notice.

### DMs

Elfy responds to every DM sent to her — no designated-channel concept
applies in DMs, and no @mention is needed.

## Slash commands — full reference

Eight public slash commands are registered via Discord's application-command
tree (`bot.tree.command`) and synced on startup — every one of them also
works as `@Elfy <command>` instead (see
["@Elfy \<command\>"](#elfy-command) above). Every slash command response is
a Discord **embed** — plain text is reserved for ordinary AI chat replies,
welcome/redirect messages, and `@Elfy`-triggered command replies. Every
response auto-deletes after 10 seconds except `/help`, which persists.

Four more commands are **owner-only and deliberately never registered as
slash commands at all** — see
["Owner-only commands"](#owner-only-commands-tag-only-not-slash) below.

### `/help`
Shows a summary of everything Elfy can do, as an embed. Does not
auto-delete — unlike every other command here, it's meant to stick around
for people to actually read.

### `/forget [persona]`
**Argument:** `persona` (optional, free text).

Clears **this channel's** conversation history — not a specific person's.
Anyone in a shared channel can run this, and it affects everyone talking
to Elfy there, which is intentional now that conversation context is
channel-scoped (see
[the core concept section](#core-concept-channel-scoped-conversation-guild-scoped-memory)
above). It never touches anyone's core memory — see `/forgetme` for that.
The response accurately reflects what actually happened: if the channel
had history, it says it was erased; if not, it says there was nothing to
forget rather than falsely claiming something was cleared.

If you supply `persona`, Elfy also adopts a one-off custom personality for
**this channel**, lasting until the next `/forget` run there (with or
without a new persona) — e.g. `/forget persona: a grumpy pirate` makes her
talk like a grumpy pirate to everyone in that channel from then on.

### `/forgetme`
No arguments. The person-scoped counterpart to `/forget`: wipes
**everything** Elfy's core-memory system has learned about **you**,
across **every server** you've talked to her in — not just the one you
run it in, since the point of "forget me" is not having to repeat it
everywhere. Never touches any channel's conversation history (that's
`/forget`'s job). Reports how many servers actually had something to
erase.

### `/mymemories`
No arguments. Shows **you**, and only you (the response is ephemeral —
visible only to you), the list of durable facts Elfy's core-memory system
has picked up about you **in the current server or DM** — not aggregated
across every server you've talked to her in. If nothing's been learned
yet here, it tells you that and suggests chatting a bit more.

### `/createthread <name>`
**Argument:** `name` (required, the thread's title).

Creates a new Discord thread (in the channel the command was run in, which
must be a text channel) and registers it as a "tracked thread" — Elfy will
respond to *every* message sent in that thread from then on, the same way
she would in her one designated chat channel, regardless of which channel
the thread was created under. Tracked threads persist across restarts.

### `/setchat <channel>`
**Argument:** `channel` (required, a text channel).

Sets the single channel, per server, where Elfy holds AI conversations.
Requires the **Manage Server** permission (or being a configured bot
owner — see [Configuration reference](#configuration-reference-every-setting-where-it-lives)).
Running it again with a different channel updates the setting and reports
what it changed from/to; running it with the channel that's already set
tells you nothing changed. Each server has exactly one designated chat
channel at a time (separate from however many tracked threads exist).

### `/setwelcome <text>`
**Argument:** `text` (required, free text).

Appends a custom line to the end of Elfy's AI-generated welcome message
for new members, persisted per server. Requires the **Manage Server**
permission (or being a configured bot owner), same as `/setchat`. Hooks
into the exact same place the base greeting is assembled
(`welcome.handle_member_join`) rather than a separate welcome path.

## Owner-only commands (tag-only, not slash)

`status`, `restart`, `memories <user id>`, and `mhelp` are restricted to
configured bot owners (see
[Configuration reference](#configuration-reference-every-setting-where-it-lives))
and are **deliberately never registered as slash commands** — doing so
would make them discoverable/visible in Discord's slash-command
autocomplete to every member, which defeats the point of restricting them.
They're only reachable by tagging the bot (`@Elfy status`, etc. — see
`mention_commands.py`), and a non-owner typing one of these words gets
treated exactly as if they'd typed an unrecognized word — no "no
permission" reply either, since even that would confirm the word means
something.

### `@Elfy status`
Live stats as an embed: uptime since last start, number of servers she's
in, number of logged DM conversations, number of active server channels,
total distinct people she's ever talked to, and total messages logged.
Pulls from the exact same underlying data (`conversation_log.py`) as the
web dashboard's Overview page, so the two can never disagree.

### `@Elfy restart` (or `@Elfy botrestart`)
Announces the restart, closes the Discord connection cleanly, and exits
the process. On a published Replit deployment, Replit's own deployment
supervisor automatically relaunches any process that exits, which is what
actually brings the bot back up — there's no in-process way to trigger a
fresh `python main.py` otherwise. **This auto-relaunch only applies to a
published Deployment** — running the bot via Replit's in-editor Run
button for local development will *not* auto-restart after this command;
you'd need to click Run again yourself.

### `@Elfy memories <user id>`
Shows everything stored about the given Discord user ID's core memory,
broken out by server (or DMs), bypassing the normal per-server isolation
every other memory read goes through — an explicit, restricted exception
for moderation/support purposes.

### `@Elfy mhelp`
Everything `/help` shows, plus this section.

## The VIP system

Separately from ordinary per-person memory, Elfy supports a small
configurable roster of specific Discord users — "VIPs" — each given their
own custom relationship to Elfy, a private personality note that shapes
how she talks to that person specifically, and a one-time greeting sent
automatically the very first time they ever message her.

### How it works in chat

- The very first time a configured VIP sends *any* message Elfy would
  otherwise respond to, she immediately sends their configured one-time
  greeting as a standalone message (before anything else), then proceeds
  with the normal reply pipeline for that message. This is a **one-time,
  per-VIP** greeting — it's persisted, so it survives bot restarts and
  redeploys and never repeats for that person again (short of the
  dashboard's "re-arm greeting" action — see below).
- On **every** message from a VIP, a hidden context note (never shown to
  the user, and Elfy is explicitly instructed never to mention, quote, or
  echo it) is silently prepended to what gets sent to Gemini, explaining
  who this person is in relation to her, plus their configured personality
  note — so her tone, warmth, and the way she addresses that specific
  person can differ meaningfully from how she talks to everyone else.
- The note also explicitly tells Elfy that the person's raw Discord
  username and their configured VIP display name refer to the same
  individual, so she doesn't get confused by messages formatted as
  "username said ...".

### Managing VIPs

VIPs are **not** meant to be edited by hand in `vip_users.py` on an
ongoing basis — that file's built-in list only matters as a one-time seed
the very first time the bot ever runs (so nothing is lost when the
feature was first added). After that, the live source of truth is
persistent storage, and VIPs are added, edited, or removed entirely from
the web dashboard's **VIPs** page (see below) — no code edits or
redeploys needed for day-to-day changes.

The dashboard's VIPs page also offers a **"Sync from code"** action, which
does the reverse: it re-reads whatever is currently in `vip_users.py`'s
built-in list and overwrites the live (storage-backed) VIP roster with it.
This is meant for deliberately pushing a code-level edit to `vip_users.py`
out to the running bot without a full redeploy — using it will discard
any VIPs that were only ever added through the dashboard and aren't also
present in the code file, so it's a destructive action to use carefully,
and the dashboard asks for confirmation before running it.

### What a VIP record contains

Each VIP entry has: the Discord user ID, a display name, a short
relationship label (e.g. "best friend", "bestie", "long-lost brother"),
a longer personality note (private context shaping how Elfy treats that
person), and their one-time greeting text. All four fields are editable
from the dashboard's Add/Edit VIP form.

## The web dashboard — full reference

The dashboard is a small self-hosted control panel served over plain HTTP
on port `8080` (mapped to external port `80` in the Replit deployment
config), sharing the same process and event loop as the Discord bot
itself. It's styled as a single dark, minimalist theme ("Elfy Control
Room") with a small set of custom CSS variables, Sora/Inter Google Fonts,
and a soft violet/pink accent palette — no separate frontend framework or
build step; every page is server-rendered HTML from Python.

### Authentication

Access is gated by a **single shared password** — the `DASHBOARD_PASSWORD`
environment variable/Replit Secret. There is no per-user login and no
username, just one password for whoever operates the bot.

- **If `DASHBOARD_PASSWORD` is not set at all, the dashboard fails
  closed**: every route (except showing the setup message itself) serves
  a "one step left" instructions page instead of any real content,
  regardless of what URL is requested. This is deliberate — the dashboard
  can show private DM contents and change how the bot behaves, so it
  never silently runs open to the internet.
- Logging in sets an HTTP-only, `SameSite=Lax` session cookie, valid for
  **7 days**, checked against an in-memory set of valid session tokens.
  Sessions are entirely in-process — restarting the bot invalidates every
  active dashboard session, and everyone has to log in again.
- Passwords are compared using a constant-time comparison
  (`secrets.compare_digest`) to avoid leaking timing information.
- A **Log out** button is available in the top navigation on every
  authenticated page.

### Pages and routes

| Method | Path | What it does |
|---|---|---|
| GET/POST | `/login` | Password entry form / submits the password |
| POST | `/logout` | Clears the session cookie and ends the session |
| GET | `/` | **Overview** — headline stats: server count, distinct people who've talked to Elfy, DM conversation count, active server-channel count, total messages logged, VIP count, plus quick links to every other page |
| GET | `/servers` | Every server Elfy is currently in: icon, member count, its designated chat channel (or a note that none is set), and how many distinct people are chatting with her there |
| GET | `/users` | Every logged conversation, **DMs and server channels shown in separate tables** (a DM is one person's private conversation; a server channel is inherently shared, so the two are never mixed together) — each row shows who/where, message count, and last-active time, linking to the full transcript |
| GET | `/conversation/{channel_id}` | The full logged transcript for one specific channel (DM or server), rendered as a chat-bubble-style back-and-forth with avatars and timestamps — the most recent 300 exchanges are kept per channel |
| GET | `/vips` | The full VIP roster: name, Discord ID, relationship, whether they've received their one-time greeting yet, with per-row Edit / Re-arm greeting / Remove actions, plus **+ Add VIP** and **Sync from code** buttons |
| GET | `/vips/new` | Blank form to add a new VIP |
| GET | `/vips/edit/{user_id}` | Pre-filled form to edit an existing VIP |
| POST | `/vips/save` | Creates a new VIP or overwrites an existing one (same form/route for both) |
| POST | `/vips/delete` | Removes a VIP entirely, including clearing their one-time-greeted status |
| POST | `/vips/sync-from-code` | Overwrites the live VIP roster with whatever is currently hardcoded in `vip_users.py` (see [The VIP system](#the-vip-system) above) |
| POST | `/vips/reset-greeting` | Re-arms one VIP's one-time greeting, without touching any other part of their entry, so they get greeted again the next time they message Elfy |
| GET | `/settings` | Every dashboard-editable bot setting, grouped into cards (see below) |
| POST | `/settings` | Saves whichever settings were submitted; invalid values reject the *entire* save with a clear error naming which fields failed, rather than silently saving a partial/garbage change |
| POST | `/settings/reset` | Reverts **every** dashboard setting back to its built-in `settings.py` default in one action |

### The Settings page, field by field

Settings are grouped into cards. Every value shown is the *current*
value — whatever's been saved to storage, or the built-in default if
nothing's been changed yet.

- **Presence** — the Discord "Activity" text shown under Elfy's name
  (e.g. "Playing *with your feelings*").
- **Personality** — three separate free-text fields:
  - Elfy's core personality/system prompt (applies to brand-new
    conversations and anyone who runs `/forget`; conversations already in
    progress keep whatever personality was active when they started,
    since the template is only re-injected on a fresh/reset session —
    though see the note below about live application).
  - The instruction guiding what kind of welcome message gets generated
    for new members.
  - Elfy's fixed physical appearance description, used to keep her look
    consistent whenever someone asks for a picture of her specifically.
- **Chat generation** — temperature, top-p, top-k, and max output tokens
  for ordinary chat replies.
- **Image generation** — the same four parameters, but for the prompt-
  enhancement step that expands a short image request into a detailed
  Pollinations prompt (not for chat itself).
- **Reply shape** — max reply lines (the hard cap discussed in
  [Reply-length enforcement](#reply-length-enforcement)) and max message
  length in characters (how long a single Discord message chunk can be
  before it gets split into multiple messages).
- **Content safety** — four independent dropdowns (Harassment, Hate
  speech, Sexual content, Dangerous content), each set to one of Gemini's
  four blocking thresholds. See [Safety settings](#safety-settings).
- **Access** — a comma-separated list of Discord user IDs treated as bot
  owners, who can run every slash command regardless of server
  permissions (currently affects `/setchat` and `/botrestart`).

**Settings apply immediately, without a bot restart, in almost every
case.** Saving triggers a live push: chat-generation and safety settings
are rebuilt into the config object used for the *next* message from
anyone (since a fresh session is built per-turn anyway — see
[the per-user memory section](#core-concept-per-user-memory-not-per-channel-history)),
and if the Activity text changed, Elfy's Discord presence is updated on
the spot via a real `await` call to the bot. If either of those live-push
steps fails for some reason, the setting is still saved successfully to
storage — the page tells you specifically what didn't refresh live (so
you know a restart may be needed for that particular piece) rather than
reporting a blanket, possibly-false "success."

One specific side effect: if you edit **Elfy's appearance description**
and save (or reset all settings to defaults), her cached reference
portrait — the image used to keep her look consistent across
self-portrait requests — is deliberately discarded, so the very next
"picture of Elfy" request generates a brand-new reference from the
updated description instead of reusing her old look.

### Data shown on the dashboard vs. what Elfy actually "thinks in"

The dashboard's conversation transcripts are a **separate, human-readable
log** (`conversation_log.py`) kept purely for display — clean text, no
raw formatting artifacts, capped at 300 exchanges per channel. This is
deliberately not the same data structure as the Gemini-format history
`ai_service.py` actually sends to the API to prime replies, which can
contain things like the hidden VIP note or Discord mention-tag formatting
that would look confusing rendered directly to a human. Logging a message
for the dashboard can never fail in a way that breaks an actual chat
reply — a logging error is caught and printed, never raised.

## Message batching

If you send several messages in quick succession — a "stream of
consciousness" burst rather than one complete thought — Elfy doesn't
generate and send a separate reply to each one. Instead:

- Each new message from the same person, in the same channel, resets a
  short debounce timer (**5 seconds** by default).
- Once nothing new arrives from that person for that channel within the
  debounce window, every buffered message is combined into a single
  request: earlier messages in the burst are folded in as quoted
  fragments of what was said, and the *last* message in the burst still
  gets full treatment (VIP note, reply-quote handling if it was itself a
  reply, attachment phrasing).
- One combined reply is generated and sent for the whole burst, and the
  whole burst is logged to the dashboard as a single exchange (rather
  than several dashboard entries where all but one show an empty Elfy
  reply).
- To make sure a very long, continuous burst still eventually gets a
  reply rather than waiting forever, there's a hard ceiling on total wait
  time (**25 seconds** by default) — even if messages keep arriving
  faster than the 5-second debounce, the batch will flush once that
  ceiling is hit.
- This buffering is scoped to **(channel, author)** pairs specifically —
  two different people talking at the same time in the same shared
  channel are never merged into one request, even if their messages
  happen to interleave.
- A single, solo message is simply a "burst of one" and goes through
  exactly the same code path — there's no behavior difference for
  ordinary one-off messages.
- This buffering is intentionally in-memory only and is not persisted —
  it's a few seconds of short-lived debounce bookkeeping, not
  conversation data, and it's wiped out on restart with no ill effect
  (worst case: an in-flight burst at the exact moment of a restart gets
  processed as however many messages had already arrived).

## Image generation and editing, in depth

Elfy has **two distinct capabilities** here, triggered by different
phrasing, using different underlying models:

### 1. Generating a brand-new image from text

Triggered by phrases like *"generate an image of..."*, *"create a
picture of..."*, *"draw a photo of..."*, *"show me an image of..."*, and
close variants (covering "image", "picture", and "photo" as the noun, and
"generate/create/make/draw/show me" as the verb). Matching is done on
whole words/phrases, specifically so that, e.g., the word "paint" showing
up inside "repaint" or "imagine" inside "imagining" doesn't falsely
trigger image generation during ordinary conversation.

What happens next depends on *who* the image is supposed to be of:

- **A picture of Elfy herself** — `ai_service.is_self_portrait_request`
  checks this *independently* of (not nested inside) the general
  image-request check above, matching any of
  create/make/generate/draw/show/send/take/give combined with any of
  selfie/picture/pic/image/photo, with an optional trailing prompt (e.g.
  *"send a selfie of you at the beach"*), plus a few fixed phrasings like
  *"what do you look like"*. "Selfie" alone is always enough — no "of
  you" needed — while picture/pic/image/photo need an explicit
  you/yourself/Elfy reference nearby, since "create a picture" alone
  doesn't say who it's a picture of. This used to be checked *only after*
  the general image-request check passed, which was unreliable: that
  check had no "selfie" entry at all and only recognized "show" as the
  exact phrase "show me a [picture/image/photo]", so plain requests like
  *"send a selfie"* or *"send an image of yourself"* failed the outer
  check and fell straight into ordinary chat instead of generating
  anything — see `CHANGES.md`. Once matched, it routes to a special path
  that keeps her appearance consistent across every image:
  1. A cached reference portrait of Elfy (generated once and reused,
     re-generated only if her appearance description is edited on the
     dashboard) is fed as an actual **input image** to Gemini's native
     image-editing model, asking it to keep the same character but
     change the outfit, pose, or setting to match the request. This gives
     genuine pixel-level visual consistency, not just prompt-text
     similarity.
  2. If there's no cached reference yet, one is generated on the spot
     from her fixed appearance description (a simple neutral portrait)
     and cached for next time.
  3. If that reference-image path fails for any reason, it falls back to
     the same general-purpose Pollinations pipeline described below, with
     her fixed appearance description prepended as plain text instead —
     so a hiccup in the stronger path never leaves the user with nothing.
- **Anything else** (a landscape, an animal, an object, another
  character, etc.) goes through the general-purpose path: the short
  request is first expanded by Gemini into a single, detailed
  image-generation prompt (subject details, art style, lighting, color
  palette, composition, mood, texture/quality cues — capped around 75
  words), then sent to **Pollinations.AI**'s image-generation endpoint
  using the `flux` model at 1024x1024, with enhancement enabled and, if
  configured, an authenticated API key. Very long or newline-heavy
  prompts are cleaned up and capped in length before being sent, since
  that can otherwise trigger request failures once URL-encoded.

### 2. Editing an image you upload

Triggered by attaching an image *and* saying something like *"turn this
image into..."*, *"transform this photo..."*, *"edit this picture to
..."*, etc. — this only fires if there's actually an image attachment
present alongside one of these phrases; text alone with no attachment
falls through to ordinary generation instead. Your uploaded image plus
your text instruction are sent together to Gemini's native image-editing
model, which returns a genuinely transformed version of your original
image (not a new image generated from scratch).

### Generated images always come back as a Discord attachment

Whichever path produced it, the resulting image is sent back as a
`generated_image.png` file attached to Elfy's reply (accompanied by a
short confirmation line), not as an embedded link.

## Welcome messages for new members

Whenever someone joins a server Elfy is in, she posts a greeting in that
server's configured **System Messages Channel** (Discord's own built-in
setting under Server Settings > Overview), @mentioning the new member with
a real Discord mention tag.

The greeting text itself is freshly generated by Gemini every single
time — it is explicitly *not* a fixed template. Generation is retried a
few times against an in-memory log of recent greetings specifically so
Elfy never repeats herself verbatim across new members; if generation
keeps failing or keeps colliding with something recently said, a varied
fallback greeting (with a nudged emoji so it's never byte-identical to a
prior fallback) is used instead. Every greeting is limited to 2-3
sentences and hard-capped at 2 lines in the final message.

If a server has no System Messages Channel configured, Elfy simply
skips the welcome message for that server (this is logged, not treated
as an error).

**Per-server custom addition:** if a server has run `/setwelcome <text>`,
that text is appended on its own line after the AI-generated greeting,
every time, for every new member — see `storage.ChatDataManager.load_welcome_suffix`
/ `save_welcome_suffix` and `commands.do_setwelcome`. This hooks into the
exact same assembly point described above rather than a separate welcome
path, so the AI-generated part and the custom part are always sent
together as one message.

## Reply-length enforcement

Because Elfy is meant to feel like someone texting in a fast-moving group
chat rather than writing essays, every ordinary AI chat reply is
guaranteed to be at most a configurable number of lines (**4**, by
default — the `max_reply_lines` setting). This is enforced in three
layered steps, in order:

1. If the raw model output is already within the line limit, it's used
   as-is — no extra API call, no delay.
2. If it's too long, Elfy asks Gemini once to rewrite the *same* message
   more concisely — preserving meaning, tone, and personality — down to
   the line limit.
3. If it's *still* too long after that rewrite attempt (or the rewrite
   call itself fails), the reply is hard-truncated to the first N lines
   as a guaranteed, no-API-call-required fallback. This step can never
   fail and always produces something within the limit.

If a reply had to be shortened, Elfy's own record of "what I just said"
(used to prime her next reply to that person) is updated to match what
was *actually* sent — so her own memory of the conversation never drifts
from what the user actually saw on screen.

This same three-step enforcement is not applied to welcome messages,
which have their own separate, simpler 2-line hard cap.

## Attachments — what files Elfy can read

Elfy downloads and processes attachments on any message she'll respond to
(and, when replying to a quoted message, on that quoted message's
attachments too). Supported types, by extension:

| Category | Extensions | Notes |
|---|---|---|
| **Images** | `.png`, `.jpeg`/`.jpg`, `.webp`, `.heic`, `.heif` | Can be viewed directly by Gemini, generated/edited into new images, or read as reference photos |
| **Audio** | `.wav`, `.mp3`, `.aiff`, `.aac`, `.ogg`, `.flac` | |
| **Text** | `.html`, `.css`, `.md`, `.csv`, `.xml`, `.rtf` | |
| **Documents/code** | `.pdf`, `.js`, `.py` | |

If a message includes attachments but *none* of them are a recognized
type, Elfy replies that the attachment type isn't supported, rather than
silently ignoring the files or crashing. If the download itself fails for
any attachment (network error, non-200 response), Elfy reports a generic
processing error instead of proceeding with a partial/broken set.

## Storage and persistence

Nearly everything Elfy needs to remember across restarts and redeploys is
stored through a single small abstraction (`storage.py`) that prefers
**Replit DB** — a small persistent key-value store Replit provides
automatically outside the deployment's own filesystem — and transparently
falls back to a local `shelve` file (`chatdata`) if Replit DB isn't
available (e.g. running locally, outside Replit). This fallback exists
specifically so local development works without any special setup, not
because it's meant as the primary storage mechanism in production.

**Why this matters on Replit specifically:** a plain local file written
at runtime (the old approach) does **not** survive a Replit republish —
every new deployment gets a completely fresh filesystem built from the
repo, so anything written to disk during a previous run, including a
local database file, silently disappears the next time you publish.
Replit DB lives outside that filesystem and survives redeploys, which is
exactly why it's preferred here.

### What's actually persisted

- **Per-(server, channel) conversation history** (the rolling window
  discussed earlier), keyed by guild ID + channel ID (DMs get their own
  isolated bucket per channel).
- **Per-(server, person) core-memory records** (durable facts +
  bookkeeping), keyed by guild ID + Discord user ID (DMs isolated
  per person, same as above).
- **Tracked thread IDs** (from `/createthread`).
- **Each server's designated chat channel** (from `/setchat`).
- **Each server's custom welcome-message suffix** (from `/setwelcome`).
- **Which VIPs have already received their one-time greeting.**
- **The live VIP roster itself** (name, relationship, personality note,
  greeting — everything editable from the dashboard).
- **Every dashboard settings override** — only keys actually changed from
  their `settings.py` default are stored; anything untouched simply falls
  back to its built-in default.
- **Elfy's cached reference portrait** (base64-encoded), used for
  consistent self-portrait image generation.
- **The dashboard's human-readable conversation log**, per channel, plus
  a lightweight metadata index (participants, message counts, last
  activity) used to power the Overview/Servers/Users pages without
  re-scanning every logged message on every page load.

### A note on old, now-unused storage key prefixes

Conversation history has been through three key schemes: keyed by channel
ID under `"history:"` originally, then by Discord user ID alone under
`"userhistory:"` (which was the cross-server memory leak described earlier
in this document — see `CHANGES.md`), and now by (guild ID, channel ID)
under `"channelhistory:"`. Core memory went from user-ID-only under
`"coremem:"` to (guild ID, user ID) under `"guildcoremem:"` for the same
reason. Each rename deliberately used a fresh prefix rather than
reformatting keys in place, so old entries are simply never read again
instead of being misinterpreted under the new key shape — they're harmless
leftover data, clearable from Replit DB whenever convenient, or just
ignorable indefinitely. `/forgetme` additionally scrubs the legacy
`"userhistory:"` entry for the specific person running it, as a belt-and-
suspenders cleanup — see `storage.ChatDataManager.delete_legacy_user_history`.

### `backup.py` — removed

`backup.py` used to implement a complete daily-backup system (export every
Replit DB key to a timestamped local JSON file, restore any of those files
back via `python backup.py restore backups/<filename>`). It was already
disconnected from `main.py`'s startup wiring as of a previous revision
(per `CHANGES.md`) and has now been deleted outright, along with every
reference to it — there were none left to remove beyond the file itself;
the `main.py` wiring removal had already happened. The `backups/` folder
of old JSON snapshots, if present, is untouched leftover data, same
treatment as the old storage-key prefixes above.

## Configuration reference (every setting, where it lives)

There are, in effect, **two layers** of configuration:

1. **`settings.py`** — loaded once from environment variables / hardcoded
   defaults at process startup. This is the ultimate fallback for every
   value below, and the *only* place secrets (API keys, the bot token,
   the dashboard password) live — those are never dashboard-editable, by
   design.
2. **`dashboard_settings.py`**, backed by storage — a persisted,
   live-editable override layer on top of (1). Any of the settings listed
   below can be changed from the dashboard's Settings page and takes
   effect immediately without a restart; anything never changed simply
   falls back to its `settings.py` default.

| Setting (dashboard key) | Built-in default | What it controls |
|---|---|---|
| `bot_activity` | `"with your feelings"` | Discord "Playing ..." presence text |
| `bot_personality` | (Elfy's full system prompt) | Core personality applied to new/reset conversations |
| `welcome_instruction` | (welcome-generation instruction) | Guides the style of AI-generated new-member greetings |
| `elfy_appearance` | (fixed physical description) | Keeps Elfy's look consistent across self-portrait image requests |
| `chat_temperature` | `0.95` | Chat model creativity/randomness |
| `chat_top_p` | `0.96` | Chat model nucleus sampling |
| `chat_top_k` | `40` | Chat model top-k sampling |
| `chat_max_output_tokens` | `500` | Chat reply length cap, in tokens (~400 words) |
| `image_temperature` | `0.9` | Image-prompt-enhancer creativity |
| `image_top_p` | `0.5` | Image-prompt-enhancer nucleus sampling |
| `image_top_k` | `40` | Image-prompt-enhancer top-k sampling |
| `image_max_output_tokens` | `300` | Image-prompt-enhancer output cap |
| `max_reply_lines` | `4` | Hard cap on lines per chat reply (see [Reply-length enforcement](#reply-length-enforcement)) |
| `max_message_length` | `1900` | Characters per Discord message chunk before splitting into multiple messages |
| `owner_ids` | (from `OWNER_IDS` env var) | Comma-separated Discord user IDs that bypass permission checks on every command |
| `safety_harassment` | `BLOCK_MEDIUM_AND_ABOVE` | Gemini safety threshold — harassment |
| `safety_hate_speech` | `BLOCK_MEDIUM_AND_ABOVE` | Gemini safety threshold — hate speech |
| `safety_sexually_explicit` | `BLOCK_NONE` | Gemini safety threshold — sexual content |
| `safety_dangerous_content` | `BLOCK_MEDIUM_AND_ABOVE` | Gemini safety threshold — dangerous content |
| `core_memory_window_size` | `12` | Rolling per-user recent-message window size |
| `core_memory_extraction_interval` | `15` | How often (in messages) Elfy re-evaluates what to remember about someone |
| `core_memory_fact_cap` | `25` | Max durable facts kept per person before consolidation |

Every numeric field is clamped to a sane min/max range on save (e.g.
`max_reply_lines` is clamped between 1 and 20) specifically so a typo or
blank field submitted through the dashboard's form can't silently push the
bot into a broken configuration. An invalid value anywhere in a submitted
settings form rejects the **entire** save with a clear message naming
which fields failed, rather than partially applying some changes and
silently discarding others.

## Safety settings

Elfy's chat and image-editing calls to Gemini pass an explicit safety
configuration for four content categories, each independently
configurable (see the table above and the dashboard's Content Safety
card). The Gemini API's four available thresholds, from least to most
restrictive, are:

- `BLOCK_NONE` — don't block anything in this category
- `BLOCK_ONLY_HIGH` — block only high-risk content
- `BLOCK_MEDIUM_AND_ABOVE` — block medium- and high-risk content
- `BLOCK_LOW_AND_ABOVE` — block low-, medium-, and high-risk content (the
  strictest available setting)

The built-in defaults block harassment, hate speech, and dangerous
content at the medium-and-above threshold, while sexual content defaults
to fully unblocked (`BLOCK_NONE`) — consistent with Elfy's design as a
personality-driven companion bot rather than a general-purpose assistant.
All four can be changed independently at any time from the dashboard.

## File-by-file map of the codebase

| File | Role |
|---|---|
| `main.py` | Entry point. Builds the Discord bot, wires up event handlers, loads persisted state, and runs the bot and the web dashboard together on one event loop. |
| `settings.py` | Loads environment variables and defines every built-in default value/constant, including Elfy's base personality prompt, her appearance description, and the welcome-message instruction. Also defines `is_owner()`, which is unused dead code — see `dashboard_settings.owner_ids()` below for the actual live owner check. |
| `dashboard_settings.py` | The live-editable override layer on top of `settings.py`, backed by storage. Every dashboard-adjustable value, including the owner-ID list (`owner_ids()` — the actual check every owner-only command/gate uses), is read through here, not directly from `settings.py`. Handles type coercion, range clamping, and building the actual Gemini config objects/safety-settings list from current values. |
| `ai_service.py` | The largest module. Owns all direct communication with Google's Gemini API and Pollinations.AI: chat session management (per-channel, rebuilt fresh every turn), image generation and editing (including the self-portrait detection fix), nickname-change detection, welcome-message generation, reply-length enforcement, and background core-memory extraction/consolidation. The only module that imports `google.genai` directly. |
| `core_memory.py` | Storage/formatting logic for per-(server, person) durable facts, plus the two deliberate cross-server exceptions (`clear_all_for_user`, `get_all_scopes_for_user`) used by `/forgetme` and the owner-only memory lookup. Deliberately knows nothing about Gemini itself (all actual "what should we remember" decisions are Gemini calls living in `ai_service.py`). |
| `message_handler.py` | Orchestrates incoming Discord messages: the `@Elfy <command>` router (delegated to `mention_commands.py`) runs first, then gating logic (should this message get a response, or a redirect, or nothing?), constructing the query text sent to the AI (including VIP notes and reply-quote context), message batching/debouncing, executing nickname-change requests signaled back from `ai_service.py`, and splitting long replies across multiple Discord messages. |
| `commands.py` | Implements every command's actual logic as a shared `do_*` function, and registers the eight PUBLIC ones as slash commands (plus `TrackedThreadsManager`/`ChatChannelManager`, which back `/createthread`/`/setchat`). Owner-only commands (`status`/`restart`/the cross-server memory lookup) are deliberately never registered as slash commands — see `mention_commands.py`. |
| `mention_commands.py` | The `@Elfy <command>` dispatcher — gives every command in `commands.py` slash/mention parity by calling the exact same `do_*` functions, and is the *only* entry point for the owner-only commands (which a non-owner can't even discover, let alone run — see the module docstring). |
| `help_command.py` | Single source of truth for help content, shared between the `/help` slash command (embed) and the "@Elfy help" / "@Elfy mhelp" mention triggers (plain text). Unlike every other command, these never auto-delete. |
| `attachments.py` | Downloads Discord attachments and maps file extensions to MIME types for supported image/audio/text/document formats. |
| `vip_users.py` | The VIP system: seed defaults (used only once, on first run), and all read/write helpers for the live, storage-backed VIP roster, greeted-status tracking, and building the hidden per-VIP context note injected into prompts. |
| `welcome.py` | Handles the `on_member_join` Discord event — generates and posts a fresh AI welcome message (plus each server's optional `/setwelcome` addition) in a server's system channel whenever someone new joins. |
| `conversation_log.py` | A clean, human-readable (separate from Gemini-format) log of every chat exchange, purpose-built for the dashboard's transcript/overview pages and the `status` command. Logging failures are caught and never allowed to break an actual reply. |
| `storage.py` | The persistence abstraction: prefers Replit DB, falls back to a local `shelve` file. Every other module that needs to persist something goes through `ChatDataManager` here, including the (guild, channel)/(guild, user) scope encoding that keeps every server's data isolated. Also contains the plain-file error logger (`errors.log`). |
| `web_dashboard.py` | The entire self-hosted web control panel: authentication, HTML rendering (no external template engine — pages are built as Python strings), and every route listed in [The web dashboard](#the-web-dashboard--full-reference). |
| `tests/` | Offline (`unittest`, standard library only) test suite covering `storage.py`/`core_memory.py`'s guild-scoping and `ai_service.py`'s routing logic — see `tests/README.md` for what's covered, what isn't, and how the offline `google.genai`/`aiohttp` stand-ins work. |
| `requirements.txt` | Python dependencies: `aiohttp`, `python-dotenv`, `discord.py`, `google-genai`, `replit`. |
| `CHANGES.md` | A changelog documenting significant reworks — useful project history, not something the running bot reads or depends on. |
| `LICENSE` | GNU General Public License, version 3. |
| `.replit` | Replit's own project/deployment configuration: Python 3.12, Reserved VM deployment target, port 8080 mapped to external port 80, plus a one-off "Clear" workflow for wiping old local database files. |
| `.env.development` | A local-development environment template (blank values) listing which environment variables the bot needs. |
| `.gitignore` | Standard ignore list — env files, error logs, `__pycache__`, old local database files, editor/OS files. |

## Setup and deployment

Elfy is built to run as a **Replit Reserved VM Deployment**, and several
pieces of its design assume that environment specifically (persistent
Replit DB storage, the `.replit` port mapping, the `/botrestart`
command's reliance on Replit's own auto-relaunch-on-exit behavior). It
can also run anywhere Python 3.12 and the dependencies in
`requirements.txt` are available, with the storage layer transparently
falling back to a local file instead of Replit DB.

### Discord Developer Portal setup

Beyond creating a bot application and inviting it to your server, one
setting is **mandatory**: under **Bot > Privileged Gateway Intents**, the
**Server Members Intent** must be turned on. Elfy's code already requests
this intent (`intents.members = True`, required for the welcome-message
feature to fire at all), but Discord additionally requires it to be
explicitly enabled in the portal — without that, the bot will fail to
start with a `PrivilegedIntentsRequired` error.

### Running it

1. Install dependencies: `pip install -r requirements.txt`
2. Set the required environment variables (see below) — via Replit
   Secrets in production, or a `.env`/`.env.development` file locally.
3. Run `python main.py`.
4. On successful startup, you'll see a confirmation printout with the
   bot's logged-in username and the dashboard's listening port.
5. If `DASHBOARD_PASSWORD` isn't set, a warning is printed at startup, and
   the dashboard will show setup instructions instead of anything useful
   until it's added.

### Local development note

`.env` and `.env.development` are both loaded (in that order) by
`settings.py` via `python-dotenv`, so local values can be split across
either file as convenient; neither is meant to be committed (see
`.gitignore`).

## Environment variables

| Variable | Required? | Purpose |
|---|---|---|
| `DISCORD_BOT_TOKEN` | **Yes** | Bot login token from the Discord Developer Portal. The bot refuses to start without this. |
| `GOOGLE_AI_KEY` | **Yes** | Google AI (Gemini) API key — powers every chat reply, image-prompt enhancement, image editing, welcome message, and core-memory extraction/consolidation call. |
| `DASHBOARD_PASSWORD` | Strongly recommended | Single shared password gating the entire web dashboard. Without it, the dashboard fails closed and shows only setup instructions. |
| `OWNER_IDS` | Optional | Comma-separated Discord user ID(s) that bypass permission checks on every slash command (e.g. `/setchat`, `/botrestart`) regardless of server role. Also editable later from the dashboard's Settings > Access field. |
| `POLLINATIONS_API_KEY` | Optional | Authenticates image-generation requests to Pollinations.AI. Image generation still works without it (as an unauthenticated request), but a key is recommended for reliability/rate limits. |

## Known rough edges / things worth knowing

A few things worth being aware of, since they affect what to expect from
the bot as it stands today:

- **`/forget` has no per-person granularity within a channel.** It's
  channel-scoped now (see
  [the core concept section](#core-concept-channel-scoped-conversation-guild-scoped-memory)),
  which means there's no way for one person to wipe just *their own*
  messages out of a shared channel's short-term window without clearing
  it for everyone talking to Elfy there. `/forgetme` covers the durable,
  person-specific side (core memory) — the short-term window is
  genuinely shared, by design, once it's channel-scoped.
- **`/status` and `/botrestart` changed from public slash commands to
  owner-only, tag-only commands** (`@Elfy status` / `@Elfy restart`) in
  this revision — see
  [Owner-only commands](#owner-only-commands-tag-only-not-slash). If
  anything other than a bot owner was relying on running these, that's a
  real behavior change worth knowing about, not just an internal
  refactor.
- **The image-generation model constant has regressed once before.**
  There's a comment directly in `ai_service.py` noting that the
  Pollinations model was previously found set to `kontext` (an
  image-*editing* model that doesn't work well for from-scratch
  generation with no input image) despite an existing comment already
  explaining why that's wrong — it's currently correctly set to `flux`,
  but worth knowing this specific value has drifted back before.

## License

This project is licensed under the **GNU General Public License v3.0** —
see `LICENSE` for the full text.
