"""
Handles incoming Discord messages and orchestrates message processing.
"""

import asyncio
import io
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

from discord import DMChannel, File, Message

import conversation_log
import dashboard_settings
from attachments import get_attachment_data
from help_command import is_help_mention, send_help_mention
from vip_users import get_greeting, get_vip_note, mark_greeted, needs_greeting

# ── Message batching (see enqueue_for_batch / handle_message) ───────────────
# If a user sends several messages within this many seconds of each other,
# combine them into one Gemini request and send one reply, instead of
# replying to each individually. The wait resets on every new message from
# the same person in the same conversation, so a burst isn't cut off
# mid-thought — but BATCH_MAX_WAIT_SECONDS caps the total delay so a long
# stream-of-consciousness still gets a reply instead of being deferred
# forever. This state is intentionally in-memory only (not persisted): it's
# a few seconds of debounce bookkeeping, not conversation data, and it must
# never be confused with — or leak into — the dashboard's active-conversation
# counts (see conversation_log.py), which only count actual logged replies.
BATCH_WAIT_SECONDS = 5
BATCH_MAX_WAIT_SECONDS = 25


async def construct_query(message: Message, attachments: Optional[List[Dict[str, Any]]] = None) -> str:
    """
    Construct the query string from a Discord message.

    Args:
        message: The Discord message
        attachments: Processed attachment data

    Returns:
        The formatted query string
    """
    # Use the real discord.py mention object (renders as a proper,
    # clickable, notifying tag) instead of a manually built "@username"
    # string. This way, if the model ever echoes a speaker tag back in its
    # own reply, it's a real Discord mention rather than inert plain text.
    if not message.attachments:
        query = f"{message.author.mention} said \"{message.clean_content}\""
    else:
        if not message.content:
            query = f"{message.author.mention} sent attachments:"
        else:
            query = f"{message.author.mention} said \"{message.clean_content}\" while sending attachments:"

    # Add quoted message context if replying
    if message.reference is not None:
        reply_message = await message.channel.fetch_message(message.reference.message_id or 0)

        # Only add if not replying to the bot itself
        if reply_message.author.id != message.guild.me.id if message.guild else False:
            query = f"{query} while quoting {reply_message.author.mention} \"{reply_message.clean_content}\""

            # Include attachments from quoted message
            if reply_message.attachments and attachments is not None:
                reply_attachments = await get_attachment_data(reply_message.attachments)
                if reply_attachments:
                    attachments.extend(reply_attachments)

    # VIP system: prepend a hidden relationship-context note for configured
    # users (see vip_users.py). No-op (empty string) for everyone else.
    vip_note = get_vip_note(message.author.id, message.author.name)
    if vip_note:
        query = f"{vip_note} {query}"

    return query


async def construct_batched_query(
    messages: List[Message],
    attachments: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Combine a burst of rapid-fire messages from the same author/conversation
    (see the batching section below) into one coherent query, so Elfy treats
    a quick multi-message thought as a single turn instead of several
    disconnected ones.

    For the common case of a single message this returns exactly what
    construct_query() would — no behavior change there. For a genuine burst,
    every message except the last is folded in as a plain quoted fragment of
    what the person just said; the last message still gets full treatment
    (VIP note, reply-quote handling, attachment phrasing) via construct_query
    itself, so that logic only lives in one place.

    Args:
        messages: The buffered messages, oldest first, all from the same
            author and channel
        attachments: Processed attachment data (mutated in place if the
            last message is itself a reply that quotes another image)

    Returns:
        The formatted, combined query string
    """
    if len(messages) == 1:
        return await construct_query(messages[0], attachments)

    last_query = await construct_query(messages[-1], attachments)
    earlier_fragments = [m.clean_content for m in messages[:-1] if m.clean_content]
    if not earlier_fragments:
        return last_query

    lead_in = " ".join(f'"{fragment}"' for fragment in earlier_fragments)
    return (
        f"{messages[0].author.mention} sent a few messages in a row within "
        f"a few seconds of each other — first {lead_in}, then: {last_query}"
    )


async def process_message_attachments(message: Message) -> tuple[List[Dict[str, Any]], bool]:
    """
    Process attachments from a message.

    Args:
        message: The Discord message

    Returns:
        Tuple of (attachments_list, success_flag)
    """
    if not message.attachments:
        return [], True

    attachments = await get_attachment_data(message.attachments)

    if attachments is None:
        return [], False

    if len(attachments) == 0:
        return [], True  # No supported attachments, but no error

    return attachments, True


def should_respond_to_message(
    message: Message,
    chat_channel_id: Optional[int],
    tracked_thread_ids: Optional[List[int]] = None,
) -> bool:
    """
    Determine if the bot should generate an AI chat reply to this message.

    The bot chats when:
      - it's a DM, or
      - the message is in this server's designated AI-chat channel
        (set via /setchat), or
      - the message is in a bot-created tracked thread (via /createthread)

    Being @mentioned somewhere outside of those is handled separately by
    `is_redirectable_mention` / `send_redirect_notice`, so the bot can
    point the user to the right channel instead of chatting.

    Args:
        message: The Discord message
        chat_channel_id: This server's designated chat channel, if any
        tracked_thread_ids: Thread IDs the bot fully responds in

    Returns:
        True if bot should respond
    """
    # Don't respond to the bot's own messages or any other bot's messages.
    # NOTE: this used to be `message.guild and message.guild.me and
    # message.author == message.guild.me`, which requires message.guild to
    # be truthy — but message.guild is always None in a DM, so that check
    # silently never fired there, and the bot would process (and reply to)
    # its own outgoing DM messages forever. message.author.bot works
    # correctly in both guild channels and DMs.
    if message.author.bot:
        return False

    # Don't respond to @everyone mentions
    if message.mention_everyone:
        return False

    if isinstance(message.channel, DMChannel):
        return True

    if tracked_thread_ids and message.channel.id in tracked_thread_ids:
        return True

    if chat_channel_id is not None and message.channel.id == chat_channel_id:
        return True

    return False


def is_redirectable_mention(
    message: Message,
    chat_channel_id: Optional[int],
    tracked_thread_ids: Optional[List[int]] = None,
) -> bool:
    """
    True if the bot was @mentioned somewhere it won't otherwise respond to
    chat — i.e. a guild channel that is neither the designated chat
    channel nor a tracked thread. That's the case where the bot should
    redirect the user instead of ignoring them outright or chatting.

    Args:
        message: The Discord message
        chat_channel_id: This server's designated chat channel, if any
        tracked_thread_ids: Thread IDs the bot fully responds in
    """
    if message.guild is None:
        return False  # DMs are never redirected

    bot_user = message.guild.me
    if not bot_user or not bot_user.mentioned_in(message):
        return False

    if message.mention_everyone:
        return False

    if tracked_thread_ids and message.channel.id in tracked_thread_ids:
        return False

    if chat_channel_id is not None and message.channel.id == chat_channel_id:
        return False

    return True


async def send_redirect_notice(message: Message, chat_channel_id: Optional[int]) -> None:
    """
    Reply to an out-of-channel @mention telling the user to use the
    designated chat channel instead, then self-delete after ~5 seconds.

    Note on "ephemeral": Discord's bot API only supports true ephemeral
    (visible-to-only-one-person) messages as responses to slash command /
    component Interactions, which carry an interaction token to attach
    that flag to. A plain @mention in a normal message has no such token,
    so an actually-invisible-to-others reply isn't possible here — this
    auto-deleting reply (gone after ~5s, and only @-pings the tagger) is
    the closest available equivalent for a plain-message trigger.

    Args:
        message: The message that @mentioned the bot
        chat_channel_id: This server's designated chat channel, if any
    """
    if chat_channel_id is not None and message.guild is not None:
        channel_obj = message.guild.get_channel(chat_channel_id)
        channel_ref = channel_obj.mention if channel_obj else "the designated channel"
        text = f"Please talk to me in {channel_ref}."
    else:
        text = (
            "I don't have a chat channel set up yet — ask an admin to run "
            "/setchat in the channel you'd like me to use."
        )

    try:
        await message.reply(text, mention_author=True, delete_after=5)
    except Exception as e:
        print(f"[redirect notice] Channel reply also failed: {e}")


async def split_and_send_messages(message: Message, text: str, max_length: int) -> None:
    """
    Split a long message into chunks and send them as replies.

    Args:
        message: The original Discord message to reply to
        text: The text to send
        max_length: Maximum length of each chunk
    """
    messages = []
    for i in range(0, len(text), max_length):
        sub_message = text[i:i + max_length]
        messages.append(sub_message)

    # Send each part as a separate plain-text reply — AI chat content never
    # uses embeds (only slash command responses do).
    current_message = message
    for string in messages:
        current_message = await current_message.reply(string)


# ── Message batching ─────────────────────────────────────────────────────
# Buffers rapid-fire messages per (channel_id, author_id) so a burst gets one
# combined reply instead of one reply per message. Scoped to (channel,
# author) rather than just channel, so in a shared server chat channel two
# different people talking at once are never merged into the same request.

class _PendingBatch:
    """In-memory-only bookkeeping for one (channel, author) burst in
    progress. Never persisted — see the module-level note above."""

    __slots__ = ("messages", "attachments", "task", "first_arrival")

    def __init__(self) -> None:
        self.messages: List[Message] = []
        self.attachments: List[Dict[str, Any]] = []
        self.task: Optional[asyncio.Task] = None
        self.first_arrival: float = time.monotonic()


_pending_batches: Dict[Tuple[int, int], _PendingBatch] = {}


def enqueue_for_batch(
    message: Message,
    attachments: List[Dict[str, Any]],
    ai_service,
    storage_manager,
) -> None:
    """
    Add a message (already gated as "should respond" and already had its
    own attachments processed) to its (channel, author) burst buffer, and
    (re)schedule the debounce that will eventually process the whole burst
    as one request. Resets on every call for the same key, so a fast burst
    isn't cut off mid-thought — capped by BATCH_MAX_WAIT_SECONDS so a long
    one still gets a reply instead of waiting forever.
    """
    key = (message.channel.id, message.author.id)
    batch = _pending_batches.get(key)
    if batch is None:
        batch = _PendingBatch()
        _pending_batches[key] = batch

    batch.messages.append(message)
    batch.attachments.extend(attachments)

    if batch.task is not None:
        batch.task.cancel()

    elapsed = time.monotonic() - batch.first_arrival
    wait = max(0.0, min(BATCH_WAIT_SECONDS, BATCH_MAX_WAIT_SECONDS - elapsed))
    batch.task = asyncio.create_task(_flush_after_delay(key, wait, ai_service, storage_manager))


async def _flush_after_delay(
    key: Tuple[int, int],
    delay: float,
    ai_service,
    storage_manager,
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        # A newer message in this burst reset the timer — the task that
        # replaced this one will do the flushing instead.
        return

    batch = _pending_batches.pop(key, None)
    if batch is None or not batch.messages:
        return
    await _process_batch(batch.messages, batch.attachments, ai_service, storage_manager)


async def _process_batch(
    messages: List[Message],
    attachments: List[Dict[str, Any]],
    ai_service,
    storage_manager,
) -> None:
    """
    Generate and send one combined reply for a buffered burst of messages
    (a burst of one is the common case — an ordinary single message). This
    is the same generate → reply → save-history → log-conversation pipeline
    handle_message used to run inline per-message.
    """
    last_message = messages[-1]
    try:
        async with last_message.channel.typing():
            query = await construct_batched_query(messages, attachments)

            # Keyed by the author's user ID, not the channel — Elfy's
            # conversation context follows the PERSON now, so it stays
            # bounded regardless of channel traffic and never mixes with
            # anyone else talking to her in the same channel (see
            # ai_service.py / core_memory.py for why). A batch is always
            # one (channel, author) pair already (see enqueue_for_batch),
            # so last_message.author is the same author for the whole
            # burst being processed here.
            response_text, image_bytes = await ai_service.generate_response(
                last_message.author.id,
                attachments,
                query,
                display_name=last_message.author.display_name,
            )

            if image_bytes is not None:
                image_file = File(
                    fp=io.BytesIO(image_bytes),
                    filename="generated_image.png",
                )
                await last_message.reply(response_text, file=image_file)
            else:
                if response_text:
                    max_length = dashboard_settings.get("max_message_length")
                    await split_and_send_messages(last_message, response_text, max_length)

            if image_bytes is None:
                storage_manager.save_chat_history(
                    last_message.author.id,
                    ai_service.get_history(last_message.author.id),
                )

            # Log every message in the burst as one exchange (rather than
            # one log entry per message, which would leave the dashboard's
            # transcript showing several empty "Elfy" replies) so the
            # transcript stays fully readable and message_count reflects
            # actual exchanges, not raw Discord message counts.
            combined_user_text = "\n".join(
                m.clean_content for m in messages if m.clean_content
            ) or "[attachment]"
            await conversation_log.log_message(
                last_message, response_text or "", user_text=combined_user_text
            )

    except Exception as e:
        print(f"Error: {e}")
        print(traceback.format_exc())

        if hasattr(e, 'code') and getattr(e, 'code', None) == 50035:
            await last_message.channel.send("The message is too long for me to process.")
        else:
            await last_message.channel.send("An error occurred while processing your message.")


async def handle_message(
    message: Message,
    ai_service,
    storage_manager,
    chat_channel_manager,
    tracked_threads_manager,
) -> None:
    """
    Main message handler orchestrating the processing pipeline.

    Args:
        message: The Discord message
        ai_service: The AI service instance
        storage_manager: The storage manager instance
        chat_channel_manager: Tracks each server's designated chat channel
        tracked_threads_manager: Tracks threads the bot fully responds in
    """
    # Never react to the bot's own messages or any other bot's messages,
    # including for the redirect path below. See the matching note in
    # should_respond_to_message() above — message.guild is None in DMs, so
    # the old guild-only check never caught the bot replying to itself
    # there, causing a runaway self-reply loop (and burning Gemini quota).
    # message.author.bot correctly covers both guild channels and DMs.
    if message.author.bot:
        return

    # "@Elfy help" short-circuits everything below — works in any channel,
    # including ones that would otherwise get a redirect or be ignored.
    if is_help_mention(message):
        await send_help_mention(message)
        return

    guild_id = message.guild.id if message.guild else None
    chat_channel_id = chat_channel_manager.get_channel(guild_id)
    tracked_thread_ids = tracked_threads_manager.get_all_threads()

    # @mentioned somewhere other than the designated channel/thread?
    # Redirect instead of chatting — don't touch the AI at all.
    if is_redirectable_mention(message, chat_channel_id, tracked_thread_ids):
        await send_redirect_notice(message, chat_channel_id)
        return

    # Completely ignore conversation in non-designated channels (no
    # mention, not a DM, not the chat channel, not a tracked thread).
    if not should_respond_to_message(message, chat_channel_id, tracked_thread_ids):
        return

    # VIP system: send the one-time "welcome" greeting the first time this
    # VIP ever speaks — persisted, so it won't repeat after a restart or
    # redeploy (see vip_users.py). Sent immediately (not deferred by
    # batching below), since it's a standalone greeting, not part of the AI
    # reply itself.
    if needs_greeting(message.author.id):
        mark_greeted(message.author.id)
        greeting = get_greeting(message.author.id)
        if greeting:
            try:
                await message.channel.send(greeting)
            except Exception as e:
                print(f"[vip greeting] Failed to send: {e}")

    print(f"FROM: {message.author.name}: {message.content}")

    # Process this message's own attachments right away, while we still have
    # the live discord.Attachment objects — failures here are reported
    # immediately rather than waiting out the batch window below.
    attachments, success = await process_message_attachments(message)
    if not success:
        await message.channel.send("An error occurred while processing your attachments.")
        return

    if message.attachments and len(attachments) == 0:
        await message.channel.send("Attachments are of unsupported file types.")
        return

    # Hand off to the batcher: if more messages arrive from this same
    # person in this same conversation within BATCH_WAIT_SECONDS, they'll be
    # combined into one reply (see the "Message batching" section above).
    # A solo message still goes through this path — it's just a burst of
    # one, and produces the exact same request/reply as before.
    enqueue_for_batch(message, attachments, ai_service, storage_manager)
