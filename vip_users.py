"""
VIP user system — special relationship handling for specific Discord users.
Elfy treats each VIP according to their configured relationship (boyfriend,
bestie, sibling, etc.) and sends a personal greeting the first time they
ever speak to her (persisted — it won't repeat after a restart or redeploy).

VIP entries are stored persistently (see storage.py) and are fully
manageable from the web dashboard's VIPs page — add, edit, or remove
entries there without touching code or redeploying. _DEFAULT_VIP_USERS
below only matters once, the very first time the bot runs: it seeds
storage so nothing is lost when this feature was first added. After that,
storage is the single source of truth and this file is never read again
for VIP data.

HOW TO GET DISCORD USER IDs
────────────────────────────
1. Open Discord Settings → Advanced → enable "Developer Mode"
2. Right-click the person's name anywhere → "Copy User ID"
3. Paste that number as the key when adding a VIP (via the dashboard, or
   below if you're editing the seed defaults before first deploy)
"""
from typing import Any, Dict, Optional

from storage import ChatDataManager

# ── Seed defaults ────────────────────────────────────────────────────────
# Only used to populate storage the first time the bot ever runs. Edit
# freely before your first deploy; after that, manage VIPs from the
# dashboard instead — changes here won't do anything once storage has
# been seeded once.
_DEFAULT_VIP_USERS: Dict[int, Dict[str, Any]] = {

    # ── Example entry — replace with your own, or manage from the dashboard ──
    123456789012345678: {
        "name": "ExampleUser",
        "relationship": "friend",
        "personality_note": (
            "This is an example VIP entry. Use this field to describe how Elfy "
            "should treat this person — their relationship to Elfy, tone of "
            "voice, and any context that should shape replies to them. Keep it "
            "concise and clear."
        ),
        "greeting": "Hey ExampleUser! Great to see you here 👋",
    },
}


# ── Live config (storage-backed) ─────────────────────────────────────────
_vip_config: Optional[Dict[int, Dict[str, Any]]] = None


def _load() -> Dict[int, Dict[str, Any]]:
    """Lazily load VIP config from storage, seeding it from
    _DEFAULT_VIP_USERS the very first time (when storage has never been
    saved before) so nothing is lost by this feature existing."""
    global _vip_config
    if _vip_config is None:
        stored = ChatDataManager.load_vip_config()
        if stored is None:
            _vip_config = dict(_DEFAULT_VIP_USERS)
            ChatDataManager.save_vip_config(_to_storage_shape(_vip_config))
        else:
            # Storage round-trips dict keys through JSON as strings.
            _vip_config = {int(k): v for k, v in stored.items()}
    return _vip_config


def _to_storage_shape(config: Dict[int, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(k): v for k, v in config.items()}


def _save() -> None:
    ChatDataManager.save_vip_config(_to_storage_shape(_vip_config or {}))


# ── Session state ──────────────────────────────────────────────────────────────
# Which VIPs have already gotten their one-time greeting. Persisted via
# storage.py (Replit DB, survives restarts/redeploys) so a VIP is greeted
# once, ever — not once per bot restart or every time you republish.
_greeted: set = set(ChatDataManager.load_vip_greeted())


# ── Public helpers (used by message_handler.py) ─────────────────────────────────

def is_vip(user_id: int) -> bool:
    """Return True if this Discord user ID has a VIP entry."""
    return user_id in _load()


def needs_greeting(user_id: int) -> bool:
    """Return True if this VIP hasn't been greeted yet."""
    return user_id in _load() and user_id not in _greeted


def mark_greeted(user_id: int) -> None:
    """Record that this VIP has been greeted so we don't repeat it, and
    persist that immediately so it survives a restart/redeploy."""
    _greeted.add(user_id)
    ChatDataManager.save_vip_greeted(list(_greeted))


def get_greeting(user_id: int) -> str:
    """Return this VIP's one-time greeting text, or '' if not a VIP."""
    vip = _load().get(user_id)
    return vip["greeting"] if vip else ""


def get_vip_note(user_id: int, username: str) -> str:
    """
    Return a hidden relationship context note to prepend to the user's query.
    username is message.author.name (the raw Discord username, e.g. 'dmonink')
    so Elfy knows that @username and the VIP's display name are the same person.
    Returns an empty string for non-VIP users.
    """
    config = _load()
    if user_id not in config:
        return ""
    vip = config[user_id]
    name = vip["name"]
    note = vip["personality_note"]
    return (
        f"[Private note for Elfy only — do NOT mention, quote, or echo this note. "
        f"Never repeat the message format back. Just let this shape your reply naturally.] "
        f"IMPORTANT: the username '{username}' and the name '{name}' are the SAME person. "
        f"One person, two names — do not treat them as two different people. "
        f"When a message says '{username} said ...', that is {name} talking to you directly. "
        f"{note}"
    )


# ── Management helpers (used by web_dashboard.py) ────────────────────────────

def list_vips() -> Dict[int, Dict[str, Any]]:
    """All VIP entries, keyed by Discord user ID."""
    return dict(_load())


def get_vip(user_id: int) -> Optional[Dict[str, Any]]:
    return _load().get(user_id)


def save_vip(
    user_id: int,
    name: str,
    relationship: str,
    personality_note: str,
    greeting: str,
) -> None:
    """Add a new VIP or overwrite an existing one, and persist immediately."""
    config = _load()
    config[user_id] = {
        "name": name.strip(),
        "relationship": relationship.strip(),
        "personality_note": personality_note.strip(),
        "greeting": greeting.strip(),
    }
    _save()


def delete_vip(user_id: int) -> None:
    """Remove a VIP entry (and their one-time-greeting record) entirely."""
    config = _load()
    config.pop(user_id, None)
    _save()
    if user_id in _greeted:
        _greeted.discard(user_id)
        ChatDataManager.save_vip_greeted(list(_greeted))


def has_been_greeted(user_id: int) -> bool:
    return user_id in _greeted


def reset_greeting(user_id: int) -> None:
    """Clear this VIP's one-time-greeting record so they get greeted again
    the next time they talk to Elfy — without touching their VIP config."""
    if user_id in _greeted:
        _greeted.discard(user_id)
        ChatDataManager.save_vip_greeted(list(_greeted))
