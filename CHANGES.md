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
