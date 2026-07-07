"""
Persistence layer for storing and retrieving conversation history, tracked
threads, chat-channel settings, and VIP greeted-status.

REPLIT GOTCHA THIS FILE WORKS AROUND: local files (the old shelve-based
'chatdata' db) do NOT survive a Replit republish. Each new deployment gets
a fresh filesystem built from the repo, so anything written to disk at
runtime — including a local shelve/dbm file — disappears on the next
publish, even on a Reserved VM. Replit's own guidance for exactly this
situation is to use Replit DB (a small persistent key-value store that
lives outside the deployment's filesystem and survives redeploys) instead
of local files for this kind of app state.

This module prefers Replit DB (via the `replit` package, which reads the
REPLIT_DB_URL Replit provides automatically — no setup needed) and
transparently falls back to a local shelve file if Replit DB isn't
available (e.g. running outside Replit, for local development), so
nothing breaks in a non-Replit environment.
"""
import shelve
from typing import Any, Dict, List, Optional

try:
    from replit import db as _replit_db  # type: ignore
    _HAS_REPLIT_DB = True
except Exception:
    _replit_db = None
    _HAS_REPLIT_DB = False

_SHELVE_NAME = "chatdata"

# NOTE ON THIS PREFIX: conversation history used to be keyed by Discord
# *channel* ID (under the old "history:" prefix). It's now keyed by
# Discord *user* ID instead — each person's conversation with Elfy stays
# with them across every channel/DM they use, rather than being shared
# by everyone chatting in the same channel (see core_memory.py and
# ai_service.py for why). This uses a distinct prefix, deliberately NOT
# reusing "history:", so old channel-keyed entries are simply never read
# again instead of being misinterpreted as some user's history. They're
# harmless leftover data — clear them from Replit DB / the dashboard
# whenever convenient, or just ignore them.
_HISTORY_PREFIX = "userhistory:"
_CORE_MEMORY_PREFIX = "coremem:"


class _KeyValueStore:
    """
    Minimal persistent key-value store used by ChatDataManager below.
    Prefers Replit DB (survives redeploys); falls back to a local shelve
    file (survives restarts only — the old behavior) if Replit DB raises
    for any reason, e.g. REPLIT_DB_URL isn't set because we're not
    actually running on Replit.
    """

    @staticmethod
    def get(key: str, default=None):
        if _HAS_REPLIT_DB:
            try:
                return _replit_db.get(key, default)
            except Exception as e:
                print(f"[storage] Replit DB get({key!r}) failed, falling back to shelve: {e}")
        with shelve.open(_SHELVE_NAME) as db:
            return db.get(key, default)

    @staticmethod
    def set(key: str, value) -> None:
        if _HAS_REPLIT_DB:
            try:
                _replit_db[key] = value
                return
            except Exception as e:
                print(f"[storage] Replit DB set({key!r}) failed, falling back to shelve: {e}")
        with shelve.open(_SHELVE_NAME) as db:
            db[key] = value

    @staticmethod
    def delete(key: str) -> None:
        if _HAS_REPLIT_DB:
            try:
                if key in _replit_db:
                    del _replit_db[key]
                return
            except Exception as e:
                print(f"[storage] Replit DB delete({key!r}) failed, falling back to shelve: {e}")
        with shelve.open(_SHELVE_NAME) as db:
            if key in db:
                del db[key]

    @staticmethod
    def keys_with_prefix(prefix: str) -> List[str]:
        if _HAS_REPLIT_DB:
            try:
                return [k for k in _replit_db.keys() if k.startswith(prefix)]
            except Exception as e:
                print(f"[storage] Replit DB keys() failed, falling back to shelve: {e}")
        with shelve.open(_SHELVE_NAME) as db:
            return [k for k in db.keys() if k.startswith(prefix)]


class ChatDataManager:
    """Manages persistent storage of chat history, tracked threads, chat channels, and VIP greeted-status."""

    @staticmethod
    def load_chat_history() -> Dict[int, List]:
        """Load chat history (all users) from persistent storage."""
        history = {}
        for key in _KeyValueStore.keys_with_prefix(_HISTORY_PREFIX):
            user_id_str = key[len(_HISTORY_PREFIX):]
            if user_id_str.isnumeric():
                history[int(user_id_str)] = _KeyValueStore.get(key, [])
        return history

    @staticmethod
    def load_tracked_threads() -> List[int]:
        """Load list of tracked threads from persistent storage."""
        return _KeyValueStore.get("tracked_threads", [])

    @staticmethod
    def save_chat_history(user_id: int, history: List) -> None:
        """Save chat history for a specific user."""
        _KeyValueStore.set(f"{_HISTORY_PREFIX}{user_id}", history)

    @staticmethod
    def save_tracked_threads(threads: List[int]) -> None:
        """Save list of tracked threads."""
        _KeyValueStore.set("tracked_threads", threads)

    @staticmethod
    def delete_chat_history(user_id: int) -> None:
        """Delete chat history for a specific user."""
        _KeyValueStore.delete(f"{_HISTORY_PREFIX}{user_id}")

    @staticmethod
    def load_chat_channels() -> Dict[int, int]:
        """Load the mapping of guild_id -> designated AI-chat channel_id."""
        raw = _KeyValueStore.get("chat_channels", {})
        # Replit DB round-trips values through JSON, and JSON object keys
        # are always strings — so dict keys come back as str even though
        # we stored int guild IDs. Normalize back to int either way (a
        # plain shelve dict already has int keys, so this is a no-op then).
        return {int(k): v for k, v in raw.items()}

    @staticmethod
    def save_chat_channels(chat_channels: Dict[int, int]) -> None:
        """Save the mapping of guild_id -> designated AI-chat channel_id."""
        _KeyValueStore.set("chat_channels", chat_channels)

    @staticmethod
    def load_vip_greeted() -> List[int]:
        """Load the list of VIP user IDs already given their one-time
        session greeting (see vip_users.py) — persists across restarts
        and redeploys, so each VIP is only ever greeted once, not once
        per republish."""
        return _KeyValueStore.get("vip_greeted", [])

    @staticmethod
    def save_vip_greeted(greeted: List[int]) -> None:
        """Save the list of VIP user IDs already greeted."""
        _KeyValueStore.set("vip_greeted", greeted)

    # ── Dashboard-managed settings (see dashboard_settings.py) ─────────
    @staticmethod
    def load_settings() -> Dict[str, Any]:
        """Load dashboard settings overrides. Only keys the user has
        actually changed from their settings.py default live here."""
        return _KeyValueStore.get("dashboard_settings", {})

    @staticmethod
    def save_settings(values: Dict[str, Any]) -> None:
        """Save dashboard settings overrides."""
        _KeyValueStore.set("dashboard_settings", values)

    # ── VIP user config (see vip_users.py) ──────────────────────────────
    @staticmethod
    def load_vip_config() -> Optional[Dict[str, Dict[str, Any]]]:
        """Load the VIP user dict from persistent storage, or None if
        it's never been saved — signals vip_users.py to seed storage
        from its built-in defaults on first run."""
        return _KeyValueStore.get("vip_config", None)

    @staticmethod
    def save_vip_config(vip_config: Dict[str, Dict[str, Any]]) -> None:
        """Save the full VIP user dict to persistent storage."""
        _KeyValueStore.set("vip_config", vip_config)

    # ── Per-user core memories (see core_memory.py) ─────────────────────
    @staticmethod
    def load_all_core_memories() -> Dict[int, Dict[str, Any]]:
        """Load every user's core-memory record from persistent storage,
        keyed by Discord user ID. Used once at startup; see
        core_memory.py for the in-memory cache built from this."""
        result: Dict[int, Dict[str, Any]] = {}
        for key in _KeyValueStore.keys_with_prefix(_CORE_MEMORY_PREFIX):
            user_id_str = key[len(_CORE_MEMORY_PREFIX):]
            if user_id_str.isnumeric():
                result[int(user_id_str)] = _KeyValueStore.get(key, {})
        return result

    @staticmethod
    def save_core_memories(user_id: int, data: Dict[str, Any]) -> None:
        """Save one user's core-memory record (their distilled facts +
        bookkeeping) to persistent storage."""
        _KeyValueStore.set(f"{_CORE_MEMORY_PREFIX}{user_id}", data)

    @staticmethod
    def delete_core_memories(user_id: int) -> None:
        """Delete a specific user's core-memory record entirely."""
        _KeyValueStore.delete(f"{_CORE_MEMORY_PREFIX}{user_id}")

    # ── Elfy reference portrait (see ai_service.generate_character_image) ─
    @staticmethod
    def load_elfy_reference_image() -> Optional[str]:
        """Load the base64-encoded reference portrait used to keep Elfy's
        appearance consistent across image generations, or None if one
        hasn't been generated yet (bootstrapped lazily on first request)."""
        return _KeyValueStore.get("elfy_reference_image_b64", None)

    @staticmethod
    def save_elfy_reference_image(b64_data: str) -> None:
        """Save the base64-encoded reference portrait."""
        _KeyValueStore.set("elfy_reference_image_b64", b64_data)

    @staticmethod
    def delete_elfy_reference_image() -> None:
        """Drop the cached reference portrait (e.g. because the appearance
        description was edited on the dashboard) so a fresh one gets
        bootstrapped from the new description next time it's needed."""
        _KeyValueStore.delete("elfy_reference_image_b64")

    # ── Dashboard conversation log (see conversation_log.py) ────────────
    @staticmethod
    def load_conversation_log(channel_id: int) -> List[Dict[str, Any]]:
        """Load the human-readable message log for one channel (DM or
        guild), used by the dashboard's conversation viewer. This is
        separate from load_chat_history(), which stores Gemini-format
        history used to prime the model, not to be displayed to a person."""
        return _KeyValueStore.get(f"convlog:{channel_id}", [])

    @staticmethod
    def save_conversation_log(channel_id: int, entries: List[Dict[str, Any]]) -> None:
        """Save the human-readable message log for one channel."""
        _KeyValueStore.set(f"convlog:{channel_id}", entries)

    @staticmethod
    def load_channel_meta() -> Dict[str, Dict[str, Any]]:
        """Load the lightweight index (one entry per channel that's ever
        been logged) the dashboard's overview pages read from, keyed by
        str(channel_id): who's in it, message count, last activity, etc."""
        return _KeyValueStore.get("channel_meta", {})

    @staticmethod
    def save_channel_meta(meta: Dict[str, Dict[str, Any]]) -> None:
        """Save the channel metadata index."""
        _KeyValueStore.set("channel_meta", meta)


def log_error(text: str, error_traceback: str, history: str,
              candidates: str, parts: str, prompt_feedbacks: str) -> None:
    """Log errors to file for debugging."""
    with open('errors.log', 'a+', encoding='utf-8') as errorlog:
        errorlog.write('\n##########################\n')
        errorlog.write('Message: ' + text)
        errorlog.write('\n-------------------\n')
        errorlog.write('Traceback:\n' + error_traceback)
        errorlog.write('\n-------------------\n')
        errorlog.write(f'History:\n{history}')
        errorlog.write('\n-------------------\n')
        errorlog.write('Candidates:\n' + str(candidates))
        errorlog.write('\n-------------------\n')
        errorlog.write('Parts:\n' + str(parts))
        errorlog.write('\n-------------------\n')
        errorlog.write('Prompt feedbacks:\n' + str(prompt_feedbacks))
