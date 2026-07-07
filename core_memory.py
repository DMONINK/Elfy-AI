"""
Per-user "core memory" storage.

Elfy's conversation context used to be scoped to a Discord *channel*,
shared by everyone chatting in it, and grew without bound for as long as
the bot process stayed up. This module is the storage half of the fix:
a small, durable, capped set of distilled facts kept *per person* —
their name/nickname, relationships, preferences, running jokes, whatever
actually matters — separate from the short-term rolling window of raw
recent messages (which lives in ai_service.py's self._history).

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
from typing import Any, Dict, List

from storage import ChatDataManager

# Default cap on distilled facts kept per user — see settings.py's
# CORE_MEMORY_FACT_CAP for the dashboard-editable version of this;
# callers (ai_service.py) pass that value in explicitly. This constant is
# just the fallback if a caller doesn't.
DEFAULT_MEMORY_CAP = 25

# ── Live cache (storage-backed) ─────────────────────────────────────────
_cache: Dict[int, Dict[str, Any]] = {}
_loaded_all = False


def _ensure_loaded() -> None:
    """Lazily bulk-load every user's stored record on first access, same
    idea as vip_users._load() — cheap since this is plain data, no API
    calls, and means we don't hit storage on every single message."""
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


def _persist(user_id: int) -> None:
    ChatDataManager.save_core_memories(user_id, _cache[user_id])


# ── Public helpers (used by ai_service.py) ──────────────────────────────

def get_record(user_id: int) -> Dict[str, Any]:
    """The full stored record for one user (facts + bookkeeping). Never
    None — a user with no memories yet gets an empty record on first
    access, same shape as everyone else."""
    _ensure_loaded()
    return _cache.setdefault(user_id, _empty_record())


def get_facts(user_id: int) -> List[str]:
    """Just this user's list of remembered facts, oldest first."""
    return list(get_record(user_id).get("facts", []))


def format_for_prompt(user_id: int, display_name: str) -> str:
    """
    A short block to inject into the prompt for this user's turn, or ''
    if nothing is remembered about them yet (the common case for anyone
    new — callers should skip adding anything to the prompt in that
    case, not send an empty note).
    """
    facts = get_facts(user_id)
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


def bump_message_count(user_id: int) -> int:
    """Increment this user's since-last-extraction message counter,
    persist it, and return the new count."""
    record = get_record(user_id)
    record["messages_since_extraction"] = record.get("messages_since_extraction", 0) + 1
    _persist(user_id)
    return record["messages_since_extraction"]


def reset_message_count(user_id: int) -> None:
    """Zero this user's since-last-extraction counter (called right
    before kicking off a background extraction, so a burst of fast
    messages can't trigger it twice in a row)."""
    record = get_record(user_id)
    record["messages_since_extraction"] = 0
    _persist(user_id)


def merge_new_facts(user_id: int, new_facts: List[str], cap: int = DEFAULT_MEMORY_CAP) -> bool:
    """
    Append newly-extracted facts, skipping any that are an exact
    (case-insensitive) match for something already stored, and persist.

    Returns True if the merged list is now over `cap` — the caller
    (ai_service.py) should then run consolidation to compress it back
    down, since this function only ever appends and never trims.
    """
    record = get_record(user_id)
    existing = record.get("facts", [])
    existing_lower = {f.strip().lower() for f in existing}

    for fact in new_facts:
        cleaned = fact.strip()
        if cleaned and cleaned.lower() not in existing_lower:
            existing.append(cleaned)
            existing_lower.add(cleaned.lower())

    record["facts"] = existing
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _persist(user_id)
    return len(existing) > cap


def replace_facts(user_id: int, facts: List[str]) -> None:
    """Overwrite the stored fact list wholesale — used after
    consolidation compresses/merges the list down, and as the
    guaranteed-safe fallback (keep the most recent `cap` facts) if
    consolidation itself fails for any reason."""
    record = get_record(user_id)
    record["facts"] = [f.strip() for f in facts if f.strip()]
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    _persist(user_id)


def clear(user_id: int) -> None:
    """Wipe a user's core memory entirely — e.g. the /forget command."""
    _cache[user_id] = _empty_record()
    ChatDataManager.delete_core_memories(user_id)


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


def list_all() -> Dict[int, Dict[str, Any]]:
    """Every user's core-memory record, keyed by Discord user ID. Not
    currently used anywhere, but here for a future dashboard page (see
    vip_users.list_vips() for the equivalent pattern)."""
    _ensure_loaded()
    return dict(_cache)
