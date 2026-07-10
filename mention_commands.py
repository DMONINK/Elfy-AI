"""
"@Elfy <command> [args]" dispatcher — the mention-based counterpart to
the slash commands in commands.py, giving every command real
slash/mention parity (see message_handler.handle_message, which checks
this before anything else, same position the old help-only mention check
used to occupy — so every command here works in any channel, not just
Elfy's designated chat channel).

PUBLIC commands (also registered as slash commands in commands.py) share
their actual logic with the slash versions via the do_* functions
imported from commands.py — this module only handles: recognizing the
command word, parsing whatever plain-text arguments it needs, running the
matching owner check for owner-only ones, and formatting/sending the
reply with the right auto-delete behavior.

OWNER-ONLY commands (status, restart, the cross-server memory lookup,
mhelp) are reachable ONLY through this module — they're deliberately
never registered as slash commands (see commands.setup_commands'
docstring for why). A non-owner typing one of these command words gets
treated exactly as if they'd typed an unrecognized word: this module
returns False and lets the caller fall through to its normal handling
(redirect notice / regular chat), rather than confirming the word means
anything. That's deliberate — a "no permission" reply would still leak
that the command exists, which defeats the point of keeping it off the
slash-command list in the first place.
"""
import re
from typing import Optional, Tuple

from discord import Message

import commands
import help_command

_MENTION_RE = re.compile(r"<@!?\d+>")
_USER_ID_RE = re.compile(r"<@!?(\d+)>|(\d{15,25})")

_PUBLIC_COMMAND_WORDS = {
    "help", "forget", "forgetme", "mymemories", "createthread", "setchat", "setwelcome",
}
_OWNER_COMMAND_WORDS = {"mhelp", "restart", "botrestart", "status", "memories"}


def _parse_mention_command(message: Message) -> Optional[Tuple[str, str]]:
    """
    If this message @mentions the bot and has a recognized command word
    right after the mention, return (command_word, rest_of_text) with the
    mention stripped and the word lowercased. Returns None if the bot
    isn't mentioned, there's no text after the mention, or the first word
    isn't one of the known command words above (in which case the caller
    should treat this exactly like any other mention — see this module's
    docstring on why unrecognized words, including owner-only ones typed
    by non-owners, fall through rather than getting a response here).

    Guild-only, matching is_help_mention/is_redirectable_mention: DMs
    reach the bot without needing an @mention at all.
    """
    if message.guild is None:
        return None
    bot_user = message.guild.me
    if not bot_user or not bot_user.mentioned_in(message):
        return None

    remaining = _MENTION_RE.sub("", message.content).strip()
    if not remaining:
        return None

    parts = remaining.split(maxsplit=1)
    command_word = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if command_word not in _PUBLIC_COMMAND_WORDS and command_word not in _OWNER_COMMAND_WORDS:
        return None
    return command_word, rest


def _parse_user_id(text: str) -> Optional[int]:
    """Pull a Discord user ID out of free text — either a raw snowflake
    or an <@id>/<@!id> mention, whichever comes first."""
    match = _USER_ID_RE.search(text)
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _format_result_text(result: "commands.CommandResult") -> str:
    """Plain-text rendering of a CommandResult for a mention reply —
    matches the existing plain-text convention for mention-triggered
    replies (see help_command.send_help_mention /
    message_handler.send_redirect_notice); embeds are reserved for slash
    command responses."""
    return f"**{result.title}**\n{result.description}"


async def _reply(message: Message, text: str, *, persist: bool = False) -> None:
    """Send a mention-command reply. Every command's response auto-deletes
    after 10 seconds EXCEPT help/mhelp (persist=True) — matching the
    slash-command side's _send_and_auto_delete in commands.py."""
    try:
        await message.reply(text, mention_author=False, delete_after=None if persist else 10)
    except Exception as e:
        print(f"[mention_commands] Reply failed: {e}")


async def handle_mention_command(
    message: Message,
    bot,
    ai_service,
    storage_manager,
    chat_channel_manager,
    tracked_threads_manager,
) -> bool:
    """
    Entry point called from message_handler.handle_message before any
    other routing. Returns True if this message was a recognized command
    mention (caller should stop processing it any further), False
    otherwise (caller should continue with its normal flow — redirect
    notice, or regular AI chat).
    """
    parsed = _parse_mention_command(message)
    if parsed is None:
        return False
    command_word, rest = parsed

    is_owner_command = command_word in _OWNER_COMMAND_WORDS
    if is_owner_command and not commands.is_owner(message.author.id):
        # Deliberately silent — see this module's docstring.
        return False

    guild_id = message.guild.id  # guaranteed not None — _parse_mention_command is guild-only

    try:
        if command_word == "help":
            await help_command.send_help_mention(message)

        elif command_word == "forget":
            result = await commands.do_forget(guild_id, message.channel.id, rest or None, ai_service)
            await _reply(message, _format_result_text(result))

        elif command_word == "forgetme":
            result = await commands.do_forgetme(message.author.id, ai_service)
            await _reply(message, _format_result_text(result))

        elif command_word == "mymemories":
            result = await commands.do_mymemories(guild_id, message.author.id)
            await _reply(message, _format_result_text(result))

        elif command_word == "createthread":
            if not rest:
                await _reply(message, "Give me a thread name, e.g. `@Elfy createthread movie-night`.")
                return True
            result = await commands.do_createthread(message.channel, rest, tracked_threads_manager)
            await _reply(message, _format_result_text(result))

        elif command_word == "setchat":
            if not message.channel_mentions:
                await _reply(message, "Mention the channel you want, e.g. `@Elfy setchat #general`.")
                return True
            target_channel = message.channel_mentions[0]
            result = await commands.do_setchat(message.guild, target_channel, message.author, chat_channel_manager)
            await _reply(message, _format_result_text(result))

        elif command_word == "setwelcome":
            result = await commands.do_setwelcome(guild_id, message.author, rest)
            await _reply(message, _format_result_text(result))

        elif command_word == "mhelp":
            await help_command.send_mhelp_mention(message)

        elif command_word == "status":
            embed = await commands.do_status(bot)
            try:
                await message.reply(embed=embed, mention_author=False, delete_after=10)
            except Exception as e:
                print(f"[mention_commands] Status reply failed: {e}")

        elif command_word in ("restart", "botrestart"):
            await message.reply("Restarting... 🔄", mention_author=False)
            await commands.do_restart(bot, message.author)

        elif command_word == "memories":
            target_user_id = _parse_user_id(rest)
            if target_user_id is None:
                await _reply(message, "Give me a user ID or mention, e.g. `@Elfy memories 123456789012345678`.")
                return True
            result = await commands.do_memory_lookup(bot, target_user_id)
            await _reply(message, _format_result_text(result))

    except Exception as e:
        print(f"[mention_commands] '{command_word}' failed: {e}")
        await _reply(message, "Something went wrong running that command — sorry!")

    return True
