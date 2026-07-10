"""
Per-(guild, user) "core memory" storage.

Elfy's conversation context used to be scoped to a Discord *channel*,
shared by everyone chatting in it, and grew without bound for as long as
the bot process stayed up. This module is the storage half of the fix for
that: a small, durable, capped set of distilled facts kept *per person* —
their name/nickname, relationships, preferences, running jokes, whatever
actually matters — separate from the short-term rolling window of raw
recent messages (which lives in ai_service.py's self._history).

GUILD SCOPING: records are keyed by (guild_id, user_id), not just user_id.
This module used to be purely per-user, which meant a fact distilled about
someone in one Discord server could surface while Elfy was talking to that
same person in a completely different server — a cross-server data leak
(see CHANGES.md). guild_id is None for DMs, which get their own isolated
bucket per person (a DM is already 1:1, so this doesn't change DM behavior
at all).

Two commands deliberately reach across this per-guild isolation, by
design, and use the _for_user helpers at the bottom of this file instead
of the normal per-scope ones: /forgetme (a genuine "forget everything
about me, everywhere" — see clear_all_for_user) and the owner-only
cross-server memory lookup (see get_all_scopes_for_user). Every other
caller should go through the normal (guild_id, user_id)-scoped functions.

This module deliberately knows nothing about the Gemini API. Deciding
*what's* worth remembering is a Gemini call, and every Gemini call in
this codebase lives in ai_service.py (the sole module that imports
google.genai) — see AIService._extract_core_memory /
_consolidate_core_memory. This module only handles: storing facts,
capping/deduping them, formatting them for a prompt, and the simple
message-count bookkeeping that decides *when* extraction should run.

Mirrors the lazy-load-then-cache pattern already used by vip_users.py.
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from storage import ChatDataManager

# Default cap on distilled facts kept per (guild, user) scope — see
# settings.py's CORE_MEMORY_FACT_CAP for the dashboard-editable version of
# this; callers (ai_service.py) pass that value in explicitly. This
# constant is just the fallback if a caller doesn't.
DEFAULT_MEMORY_CAP = 100

# A core-memory scope: (guild_id, user_id). guild_id is None for DMs.
Scope = Tuple[Optional[int], int]

# ── Live cache (storage-backed) ─────────────────────────────────────────
_cache: Dict[Scope, Dict[str, Any]] = {}
_loaded_all = False


def _ensure_loaded() -> None:
    """Lazily bulk-load every stored record on first access, same idea as
    vip_users._load() — cheap since this is plain data, no API calls, and
    means we don't hit storage on every single message."""
    global _loaded_all
    if not _loaded_all:
        _cache.update(ChatDataManager.load_all_core_memories())
        _loaded_all = True


def _empty_record() -> Dict[str, Any]:
    return {
        "facts": [],  # List[str] — the distilled memories themselves
        "messages_since_extraction": 0,
        "updated_at": None,
    }


def _persist(scope: Scope) -> None:
    ChatDataManager.save_core_memories(scope, _cache[scope])


# ── Public helpers (used by ai_service.py) — all per-(guild, user) ──────

def get_record(guild_id: Optional[int], user_id: int) -> Dict[str, Any]:
    """The full stored record for one (guild, user) scope (facts +
    bookkeeping). Never None — a scope with no memories yet gets an empty
    record on first access, same shape as everyone else."""
    _ensure_loaded()
    return _cache.setdefault((guild_id, user_id), _empty_record())


def get_facts(guild_id: Optional[int], user_id: int) -> List[str]:
    """Just this (guild, user) scope's list of remembered facts, oldest first."""
    return list(get_record(guild_id, user_id).get("facts", []))


def format_for_prompt(guild_id: Optional[int], user_id: int, display_name: str) -> str:
    """
    A short block to inject into the prompt for this user's turn in this
    guild, or '' if nothing is remembered about them here yet (the common
    case for anyone new — callers should skip adding anything to the
    prompt in that case, not send an empty note).
    """
    facts = get_facts(guild_id, user_id)
    if not facts:
        return ""
    bullet_list = "\n".join(f"- {fact}" for fact in facts)
    return (
        f"[What you remember about {display_name} from past "
        f"conversations — let this shape your reply naturally. Don't "
        f"recite this list, announce that you're 'remembering' "
        f"something, or treat it as something they just said:]\n"
        f"{bullet_list}"
    )


def bump_message_count(guild_id: Optional[int], user_id: int) -> int:
    """Increment this scope's since-last-extraction message counter,
    persist it, and return the new count."""
    scope = (guild_id, user_id)
    record = get_record(guild_id, user_id)
    record["messages_since_extraction"] = record.get("messages_since_extraction", 0) + 1
    _persist(scope)
    return record["messages_since_extraction"]


def reset_message_count(guild_id: Optional[int], user_id: int) -> None:
    """Zero this scope's since-last-extraction counter (called right
    before kicking off a background extraction, so a burst of fast
    messages can't trigger it twice in a row)."""
    scope = (guild_id, user_id)
    record = get_record(guild_id, user_id)
    record["messages_since_extraction"] = 0
    _persist(scope)


def merge_new_facts(
    guild_id: Optional[int],
    user_id: int,
    new_facts: List[str],
    cap: int = DEFAULT_MEMORY_CAP,
) -> bool:
    """
    Append newly-extracted facts, skipping any that are an exact
    (case-insensitive) match for something already stored, and persist.

    Returns True if the merged list is now over `cap` — the caller
    (ai_service.py) should then run consolidation to compress it back
    down, since this function only ever appends and never trims.
    """
    scope = (guild_id, user_id)
    record = get_record(guild_id, user_id)
    existing = record.get("facts", [])
    existing_lower = {f.strip().lower() for f in existing}

    for fact in new_facts:
        cleaned = fact.strip()
        if cleaned and cleaned.lower() not in existing_lower:
            existing.append(cleaned)
            existing_lower.add(cleaned.lower())

    record["facts"] = existing
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _persist(scope)
    return len(existing) > cap


def replace_facts(guild_id: Optional[int], user_id: int, facts: List[str]) -> None:
    """Overwrite the stored fact list wholesale — used after
    consolidation compresses/merges the list down, and as the
    guaranteed-safe fallback (keep the most recent `cap` facts) if
    consolidation itself fails for any reason."""
    scope = (guild_id, user_id)
    record = get_record(guild_id, user_id)
    record["facts"] = [f.strip() for f in facts if f.strip()]
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _persist(scope)


def clear(guild_id: Optional[int], user_id: int) -> None:
    """Wipe core memory for one specific (guild, user) scope. Building
    block for clear_all_for_user() below; no command currently calls this
    directly — /forgetme intentionally reaches across every guild (see
    below), and /forget no longer touches core memory at all now that
    it's channel-scoped."""
    scope = (guild_id, user_id)
    _cache[scope] = _empty_record()
    ChatDataManager.delete_core_memories(scope)


def parse_fact_lines(raw_text: str) -> List[str]:
    """
    Turn a raw Gemini text response into a clean list of fact strings:
    one fact per line, stripping bullet/number prefixes even though the
    prompt asks the model not to use them (defensive — models don't
    always comply), dropping blanks, and treating a bare 'NONE' response
    (the prompted sentinel for "nothing worth remembering") as an empty
    list rather than a literal fact.
    """
    if not raw_text or not raw_text.strip():
        return []
    if raw_text.strip().upper() == "NONE":
        return []

    facts = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.upper() == "NONE":
            continue
        line = re.sub(r"^[\-\u2022\*]\s*", "", line)   # "- " / "• " / "* "
        line = re.sub(r"^\d+[\.\)]\s*", "", line)       # "1. " / "1) "
        line = line.strip()
        if line:
            facts.append(line)
    return facts


def list_all() -> Dict[Scope, Dict[str, Any]]:
    """Every stored record, keyed by (guild_id, user_id). Not currently
    used anywhere directly, but here for a future dashboard page (see
    vip_users.list_vips() for the equivalent pattern), and as the basis
    for get_all_scopes_for_user()/clear_all_for_user() below."""
    _ensure_loaded()
    return dict(_cache)


# ── Cross-guild helpers — deliberate exceptions to per-guild isolation ──
# Both of these intentionally step outside the (guild, user) scoping used
# everywhere else in this module. Restrict their callers appropriately:
# get_all_scopes_for_user() is read-only and meant for the owner-only
# memory-lookup command; clear_all_for_user() is destructive and meant
# for /forgetme, which any user can run on themselves.

def get_all_scopes_for_user(user_id: int) -> Dict[Optional[int], Dict[str, Any]]:
    """Every guild (keys are guild_id, or None for DMs) this user has a
    non-empty core-memory record in, for the owner-only cross-server
    memory-lookup command. Skips scopes with no actual facts (e.g. someone
    who's said a few messages but never hit the extraction interval) so
    the output isn't cluttered with empty entries."""
    _ensure_loaded()
    return {
        guild_id: record
        for (guild_id, uid), record in _cache.items()
        if uid == user_id and record.get("facts")
    }


def clear_all_for_user(user_id: int) -> int:
    """Wipe this user's core memory in every guild (and DMs) at once —
    used by /forgetme, which is intentionally user-scoped rather than
    guild-scoped like everything else here: the whole point of "forget
    me" is that a person shouldn't have to run it separately in every
    server they've talked to Elfy in. Returns how many *non-empty* scopes
    were cleared (for the confirmation message) — empty bookkeeping-only
    scopes are cleared too, but not counted, since reporting those would
    be misleading ("erased across 4 servers" when only 1 had real facts)."""
    _ensure_loaded()
    scopes_to_clear = [scope for scope in _cache.keys() if scope[1] == user_id]
    meaningful_count = sum(1 for scope in scopes_to_clear if _cache[scope].get("facts"))
    for scope in scopes_to_clear:
        _cache[scope] = _empty_record()
        ChatDataManager.delete_core_memories(scope)
    return meaningful_count
