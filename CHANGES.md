# Cross-server memory leak fixed (P0) + channel-locked memory + command batch + selfie routing fix + nickname changes

Fixed the cross-server data leak first and verified it, then did the rest
of the batch on top of the new schema so nothing had to be built twice.

## The P0 leak

Neither memory layer recorded guild ID at all — `userhistory:{user_id}`
and `coremem:{user_id}` were both pure user-ID keys, so the same Discord
account chatting in two different servers shared one conversation and one
set of durable facts. Root cause was the schema, not scattered call-site
bugs — confirmed by grepping every read/write/injection site.

Fixed by re-keying both layers:
- Short-term rolling window: `(guild_id, channel_id)`, new
  `channelhistory:` prefix.
- Core memory (durable facts): `(guild_id, user_id)`, new
  `guildcoremem:` prefix.

Both old prefixes (`userhistory:`, `coremem:`) are dead — never read by
current code, left in place as harmless orphaned data, same pattern this
codebase already used once before (see the next entry below, for the
`history:` -> `userhistory:` migration that fixed a different bug).

Tests: `tests/test_storage.py` and `tests/test_core_memory.py` reproduce
the exact scenario (same user, two different guilds, same numeric
channel/user-id component) and confirm zero bleed at both the storage and
in-memory-cache level. `tests/test_ai_service.py` reproduces it again
end-to-end through `generate_response()` itself, inspecting exactly what
gets sent to (a fake) Gemini for each reply.

## Architecture change: user-locked -> channel-locked, core memory now per-(guild, person)

General conversational memory is channel-scoped now (this *is* the P0 fix
above — same change). Core memory stays scoped to a person, but now
per-(guild, person) rather than globally per-person, closing the same
leak for durable facts. Two commands deliberately reach across that
per-guild isolation: `/forgetme` (wipes a person's core memory in every
guild at once) and the new owner-only `@Elfy memories <id>` lookup
(read-only, shows every guild a person has a record in).

## Commands

- `/forget` — now channel-scoped (used to be user-scoped). The optional
  persona override moved with it, so it's now a channel's persona, not a
  person's.
- `/forgetme` — new. The user-scoped wipe `/forget` used to do, now
  reaching across every guild instead of just the current one.
- New owner-only `@Elfy memories <user id>` — cross-server lookup, reuses
  `dashboard_settings.owner_ids()` (the live, dashboard-editable check
  `/botrestart` already used — NOT `settings.is_owner()`, which turned
  out to be dead code, never called anywhere in the codebase).
- New `/setwelcome <text>` — per-server, appends to the existing welcome
  message assembly in `welcome.py` rather than a parallel path.
- `/help` split into public + owner-only `mhelp` (tag-only, adds
  restart/status/memory-lookup on top of everything `/help` shows).
  `/status` and `/botrestart` moved from public slash commands to
  owner-only, tag-only commands — flagged this explicitly since it's a
  real behavior change, not just a refactor.
- Every public command now works both as a slash command and as
  `@Elfy <command>` (new `mention_commands.py`), sharing one
  implementation per command (`commands.py`'s `do_*` functions) rather
  than duplicating logic. Owner-only commands are tag-only and never
  registered as slash commands at all, and a non-owner typing one of
  those words gets treated as if it meant nothing — no "no permission"
  reply either, since that would still confirm the word does something.
- Every command's response auto-deletes after 10 seconds now, except
  `/help`/`mhelp` — those used to auto-delete after 5 seconds and now
  persist instead, which is a flip from the previous behavior, not new.
- `backup.py` fully removed. Turned out an earlier session (see below)
  had already reported this done, but the file was still present with
  nothing left referencing it — deleted it for real this time.

## Selfie generation reliability + create/show consistency

Root cause of the ~90%-failure-rate bug: `is_self_portrait_request()` was
only ever checked *after* `is_image_request()` already returned True, and
`IMAGE_KEYWORDS` had no "selfie" entry and no bare "send"/"show" as an
action word. Plain requests like "send a selfie" failed the outer check
and fell straight into ordinary chat. Rebuilt `is_self_portrait_request`
as an independent action-word x subject-word matcher (checked before, not
inside, the generic image check), which also fixes create/show behaving
inconsistently — they're pure synonyms now, same as
make/generate/draw/send/take/give.

## Nickname change (new)

Any user can ask Elfy to change her own server nickname in chat (e.g.
"change your name to Sparkling Angel"). Detection lives in `ai_service.py`
as a pure text function returning the requested name; the actual
`guild.me.edit(nick=...)` call happens in `message_handler.py`
(`ai_service.py` still doesn't import `discord`), wrapped in a
Forbidden/HTTPException handler for the missing-permission case.

## Tests

`tests/` didn't make it into the zip this session — only a misplaced
`tests/google/genai/__init__.py`, sitting at the project root as a bare
`__init__.py` — despite the entry below describing a full suite. Moved
that file back to where it belongs and wrote fresh coverage for
everything above in `test_storage.py` / `test_core_memory.py` /
`test_ai_service.py`, all passing (`python -m unittest discover -s tests`).
Didn't have the old `test_message_handler.py` / web-dashboard coverage to
restore, since those files weren't recoverable from what was uploaded —
see `tests/README.md` for what's covered now versus what still needs the
real Discord/Gemini environment to verify.

---

# Core memory rework, integrated into your updated codebase + backup.py removed

You sent a newer snapshot than what I built the core-memory system
against last time — this one already has the self-reply-loop fix,
message batching, self-portrait image consistency, owner restart/status
commands, and a real offline test suite. I re-read everything fresh and
integrated the memory rework into *this* version rather than pasting my
old files over it, so none of that other work gets lost.

One thing worth knowing: this codebase had ALSO already grown its own
fix for the slow-replies bug — a simpler per-*channel* cap
(`MAX_HISTORY_ENTRIES = 60`) that trimmed old history but didn't
distinguish between people or do any distillation. That's now fully
replaced by the per-user core-memory system, not layered on top of it.

## Files changed

**Core rework (8 files, 1 new):**
- `core_memory.py` (new) — same as last time: storage/formatting only,
  no Gemini calls.
- `ai_service.py` — per-user sessions built fresh each turn; removed the
  old `MAX_HISTORY_ENTRIES`/`_trim_history_if_needed` mechanism; kept
  the self-portrait/character-image routing, Pollinations fix, and
  everything else in this file completely untouched.
- `message_handler.py` — smaller change than last time: the batching
  refactor already isolated the actual Gemini call into
  `_process_batch()`, and a batch is already guaranteed to be one
  author's messages, so it's a one-line-ish swap (`author.id` instead
  of `channel.id`) rather than restructuring `handle_message` itself.
- `commands.py` — `/forget` rescoped to per-user, on top of the
  had-history/already-empty messaging this version added. Added
  `/mymemories`.
- `settings.py` / `dashboard_settings.py` / `storage.py` — same 3 new
  dashboard settings and `coremem:`/`userhistory:` storage keys as
  before, layered onto the appearance-consistency additions already
  here (`ELFY_APPEARANCE_DESCRIPTION`, elfy reference-image storage) —
  those are left alone.
- `main.py` — only change: the backup.py wiring removed (see below).

**backup.py — removed entirely, as asked:**
- Deleted the file.
- Removed its 2 references in `main.py` (the import and the
  `asyncio.gather(...)` entry).
- Nothing else referenced it.
- Not touched: the `backups/` folder with old JSON snapshots it already
  produced — that's just leftover data now, delete it too if you want,
  or keep it around as a one-time archive.

**Tests — updated to match, plus new coverage:**
- `tests/test_message_handler.py` — the batching tests already keyed
  fake calls by `(channel, author)`, which turned out to line up really
  well with this rework. Updated `FakeAIService` for the new
  `display_name` param, and added an explicit check that two different
  authors in the same channel get their OWN user ID passed to
  `generate_response` — not the shared channel's ID.
- `tests/test_ai_service.py` — added a full section exercising
  `generate_response` end-to-end: window trimming, per-user isolation,
  extraction firing at the right count, the memory note showing up in
  the next session, cap/consolidation, `/forget`, restart reload,
  corrupted-data handling.
- `tests/test_core_memory.py` (new) — direct tests of the storage
  module itself: dedup, capping, parsing, persistence.
- `tests/google/genai/__init__.py` — extended so it can actually run a
  fake chat/generation exchange (previously it only supported the
  pure-function tests by raising on any real call). Still raises by
  default; a test opts in by setting `client.canned_chat_reply` /
  `client.canned_model_response`. This doesn't change what the
  pre-existing tests check — they never touch `Client` at all.
- `tests/README.md` — updated to list the new file.

Ran the full suite before and after: 47 checks passed on your snapshot
as-is, 87 pass now (47 original + 40 new/updated), 0 failures.

## How to verify on Replit

Same as before:
1. Deploy, chat — should be fast immediately.
2. ~15 messages in, `/mymemories` should show what she's picked up.
3. Two people in the shared channel — one's info shouldn't leak into
   the other's replies.

Plus, from this round: confirm `/forget` now only clears your own
stuff (ask a friend to check theirs is untouched), and that everything
else — restart, status, self-portraits, batching — still behaves the
same as before I touched anything.
