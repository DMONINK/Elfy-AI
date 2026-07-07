"""
Conversation logging for the web dashboard.

This is deliberately separate from ai_service.py's Gemini-format history
(self._history), which exists to prime the model and isn't meant to be
read by a person — it can contain a raw VIP note prepended, mention
tags, etc. This module keeps a clean, human-readable record of who's
talked to Elfy, in DMs and in servers, and what was actually said, for
the dashboard's Users and Conversation pages.

Logging never raises — a logging failure should never break an actual
chat reply.
"""
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import discord

from storage import ChatDataManager

# Rolling cap on how many exchanges we keep per channel, so the store
# doesn't grow without bound for a very active channel.
_MAX_LOG_ENTRIES_PER_CHANNEL = 300


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def log_message(
    message: discord.Message,
    bot_text: str,
    user_text: Optional[str] = None,
) -> None:
    """
    Record one exchange (the user's message + Elfy's reply) for
    message.channel. Safe to call for both DMs and guild channels.

    Args:
        message: The Discord message this exchange is anchored to (its
            channel/author/avatar identify the conversation)
        bot_text: Elfy's reply text
        user_text: What to display as the user's side of this exchange.
            Defaults to message.clean_content. Callers combining a batched
            burst of messages into one exchange (see message_handler.py's
            message batching) pass the combined text here, so the
            transcript still shows everything the person said even though
            it produced a single reply.
    """
    try:
        channel = message.channel
        channel_id = channel.id
        is_dm = isinstance(channel, discord.DMChannel)
        author = message.author

        meta_all = ChatDataManager.load_channel_meta()
        key = str(channel_id)
        meta = meta_all.get(key) or {
            "is_dm": is_dm,
            "guild_id": message.guild.id if message.guild else None,
            "guild_name": message.guild.name if message.guild else None,
            "channel_name": None if is_dm else getattr(channel, "name", None),
            "participant_ids": [],
            "participant_names": {},
            "message_count": 0,
            "last_active": None,
        }

        if author.id not in meta["participant_ids"]:
            meta["participant_ids"].append(author.id)
        meta["participant_names"][str(author.id)] = author.display_name
        meta["message_count"] = meta.get("message_count", 0) + 1
        meta["last_active"] = _now_iso()
        # Names can change since the channel was first logged — keep fresh.
        if message.guild:
            meta["guild_name"] = message.guild.name
            meta["channel_name"] = getattr(channel, "name", meta.get("channel_name"))

        meta_all[key] = meta
        ChatDataManager.save_channel_meta(meta_all)

        log = ChatDataManager.load_conversation_log(channel_id)
        log.append({
            "author_id": author.id,
            "author_name": author.display_name,
            "author_avatar": str(author.display_avatar.url),
            "user_text": message.clean_content if user_text is None else user_text,
            "bot_text": bot_text,
            "timestamp": _now_iso(),
        })
        if len(log) > _MAX_LOG_ENTRIES_PER_CHANNEL:
            log = log[-_MAX_LOG_ENTRIES_PER_CHANNEL:]
        ChatDataManager.save_conversation_log(channel_id, log)
    except Exception as e:
        print(f"[conversation_log] Failed to log message: {e}")


def list_dm_conversations() -> List[Dict[str, Any]]:
    """All logged DM conversations, most recently active first."""
    meta_all = ChatDataManager.load_channel_meta()
    items = [
        {**meta, "channel_id": int(key)}
        for key, meta in meta_all.items()
        if meta.get("is_dm")
    ]
    items.sort(key=lambda m: m.get("last_active") or "", reverse=True)
    return items


def list_guild_conversations() -> List[Dict[str, Any]]:
    """All logged guild-channel conversations, most recently active first."""
    meta_all = ChatDataManager.load_channel_meta()
    items = [
        {**meta, "channel_id": int(key)}
        for key, meta in meta_all.items()
        if not meta.get("is_dm")
    ]
    items.sort(key=lambda m: m.get("last_active") or "", reverse=True)
    return items


def get_transcript(channel_id: int) -> List[Dict[str, Any]]:
    """The full logged message history for one channel, oldest first."""
    return ChatDataManager.load_conversation_log(channel_id)


def get_channel_meta(channel_id: int) -> Optional[Dict[str, Any]]:
    return ChatDataManager.load_channel_meta().get(str(channel_id))


def total_distinct_users() -> int:
    """Distinct users across every logged conversation (DMs + guild channels)."""
    meta_all = ChatDataManager.load_channel_meta()
    ids = set()
    for meta in meta_all.values():
        ids.update(meta.get("participant_ids", []))
    return len(ids)


def total_message_count() -> int:
    meta_all = ChatDataManager.load_channel_meta()
    return sum(meta.get("message_count", 0) for meta in meta_all.values())


def distinct_users_for_guild(guild_id: int) -> int:
    """Distinct users who've talked to Elfy anywhere in one server (across
    every channel that's ever been logged there, in case the designated
    chat channel was changed at some point)."""
    ids = set()
    for conv in list_guild_conversations():
        if conv.get("guild_id") == guild_id:
            ids.update(conv.get("participant_ids", []))
    return len(ids)
