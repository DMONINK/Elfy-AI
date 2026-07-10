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
from typing import Any, Dict, List, Optional, Tuple

try:
    from replit import db as _replit_db  # type: ignore
    _HAS_REPLIT_DB = True
except Exception:
    _replit_db = None
    _HAS_REPLIT_DB = False

_SHELVE_NAME = "chatdata"

# NOTE ON THESE PREFIXES: conversation history went channel-keyed ("history:")
# -> user-keyed ("userhistory:") -> and now, as of the cross-server memory
# leak fix, (guild, channel)-keyed ("channelhistory:"). Being user-keyed-only
# meant the SAME Discord user chatting in two different servers shared one
# history — specifics from Server A (including secrets/named individuals)
# could surface in Server B. Core memory (durable facts) had the identical
# problem under the old "coremem:" prefix and is now (guild, user)-keyed
# under "guildcoremem:" for the same reason.
#
# Each rename uses a fresh prefix rather than reformatting keys in place —
# same trick the user-keyed migration used before this one — so old entries
# are simply never read again instead of being misinterpreted under the new
# key shape. They're harmless leftover data; clear them from Replit DB / the
# dashboard whenever convenient, or just ignore them. /forgetme additionally
# scrubs the legacy per-user history key for the specific user running it —
# see delete_legacy_user_history() below.
_LEGACY_USER_HISTORY_PREFIX = "userhistory:"  # pre-channel-lock; read-path removed, kept only for /forgetme cleanup
_LEGACY_CORE_MEMORY_PREFIX = "coremem:"  # pre-guild-scoping; unused, listed here only for documentation
_CHANNEL_HISTORY_PREFIX = "channelhistory:"
_CORE_MEMORY_PREFIX = "guildcoremem:"
_WELCOME_SUFFIX_PREFIX = "welcomesuffix:"


def _encode_scope(guild_id: Optional[int], other_id: int) -> str:
    """Encode a (guild_id, other_id) pair — (guild, channel) for chat
    history, (guild, user) for core memory — into a storage-key suffix.
    guild_id is None for DMs, which get their own 'dm' bucket, distinct
    from every real guild ID. This doesn't change DM behavior at all: a DM
    channel/conversation is already 1:1, so it was always effectively
    isolated per person regardless of scoping scheme."""
    guild_part = "dm" if guild_id is None else str(guild_id)
    return f"{guild_part}:{other_id}"


def _decode_scope(suffix: str) -> Optional[Tuple[Optional[int], int]]:
    """Reverse of _encode_scope(). Returns None if suffix doesn't match the
    expected 'guild_or_dm:other_id' shape — defensive against unrelated or
    corrupted keys turning up under the same prefix."""
    parts = suffix.split(":", 1)
    if len(parts) != 2:
        return None
    guild_part, other_part = parts
    if not other_part.isnumeric():
        return None
    if guild_part == "dm":
        return (None, int(other_part))
    if guild_part.isnumeric():
        return (int(guild_part), int(other_part))
    return None


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
    def load_chat_history() -> Dict[Tuple[Optional[int], int], List]:
        """Load chat history (every channel) from persistent storage, keyed
        by (guild_id, channel_id) — guild_id is None for DMs. Channel-scoped,
        not user-scoped: Elfy's short-term conversational memory follows the
        channel now, so nothing from one server's channel can be read while
        replying in a different server (see the prefix note above)."""
        history: Dict[Tuple[Optional[int], int], List] = {}
        for key in _KeyValueStore.keys_with_prefix(_CHANNEL_HISTORY_PREFIX):
            scope = _decode_scope(key[len(_CHANNEL_HISTORY_PREFIX):])
            if scope is not None:
                history[scope] = _KeyValueStore.get(key, [])
        return history

    @staticmethod
    def load_tracked_threads() -> List[int]:
        """Load list of tracked threads from persistent storage."""
        return _KeyValueStore.get("tracked_threads", [])

    @staticmethod
    def save_chat_history(scope: Tuple[Optional[int], int], history: List) -> None:
        """Save chat history for a specific (guild_id, channel_id) scope."""
        _KeyValueStore.set(f"{_CHANNEL_HISTORY_PREFIX}{_encode_scope(*scope)}", history)

    @staticmethod
    def save_tracked_threads(threads: List[int]) -> None:
        """Save list of tracked threads."""
        _KeyValueStore.set("tracked_threads", threads)

    @staticmethod
    def delete_chat_history(scope: Tuple[Optional[int], int]) -> None:
        """Delete chat history for a specific (guild_id, channel_id) scope."""
        _KeyValueStore.delete(f"{_CHANNEL_HISTORY_PREFIX}{_encode_scope(*scope)}")

    @staticmethod
    def delete_legacy_user_history(user_id: int) -> None:
        """Best-effort cleanup of the pre-channel-lock per-user history
        entry, if one still exists for this user. Nothing in current code
        reads this key (see the prefix migration note above), but
        /forgetme scrubs it anyway so a genuine 'forget everything about
        me' request actually leaves nothing behind."""
        _KeyValueStore.delete(f"{_LEGACY_USER_HISTORY_PREFIX}{user_id}")

    # ── Per-server welcome message customization (see welcome.py) ───────
    @staticmethod
    def load_welcome_suffix(guild_id: int) -> Optional[str]:
        """Load this server's custom text appended to the end of Elfy's
        welcome message (set via /setwelcome), or None if never set."""
        return _KeyValueStore.get(f"{_WELCOME_SUFFIX_PREFIX}{guild_id}", None)

    @staticmethod
    def save_welcome_suffix(guild_id: int, text: str) -> None:
        """Save this server's custom welcome-message suffix."""
        _KeyValueStore.set(f"{_WELCOME_SUFFIX_PREFIX}{guild_id}", text)

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

    # ── Per-(guild, user) core memories (see core_memory.py) ─────────────
    @staticmethod
    def load_all_core_memories() -> Dict[Tuple[Optional[int], int], Dict[str, Any]]:
        """Load every core-memory record from persistent storage, keyed by
        (guild_id, user_id) — guild_id is None for DMs. Guild-scoped, not
        just user-scoped: a fact distilled about someone in one server must
        never surface while Elfy is replying to them in a different server.
        Used once at startup; see core_memory.py for the in-memory cache
        built from this."""
        result: Dict[Tuple[Optional[int], int], Dict[str, Any]] = {}
        for key in _KeyValueStore.keys_with_prefix(_CORE_MEMORY_PREFIX):
            scope = _decode_scope(key[len(_CORE_MEMORY_PREFIX):])
            if scope is not None:
                result[scope] = _KeyValueStore.get(key, {})
        return result

    @staticmethod
    def save_core_memories(scope: Tuple[Optional[int], int], data: Dict[str, Any]) -> None:
        """Save one (guild_id, user_id) scope's core-memory record (its
        distilled facts + bookkeeping) to persistent storage."""
        _KeyValueStore.set(f"{_CORE_MEMORY_PREFIX}{_encode_scope(*scope)}", data)

    @staticmethod
    def delete_core_memories(scope: Tuple[Optional[int], int]) -> None:
        """Delete a specific (guild_id, user_id) scope's core-memory record entirely."""
        _KeyValueStore.delete(f"{_CORE_MEMORY_PREFIX}{_encode_scope(*scope)}")

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
