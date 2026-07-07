"""
Help command — shared content and senders for both the "@Elfy help"
mention trigger (see message_handler.py) and the "/help" slash command
(see commands.py). Both surfaces show the same information and
self-delete after 5 seconds.
"""
import asyncio
import re

import discord
from discord import Interaction, Message

HELP_TITLE = "Elfy — Help"

# Single source of truth for the help content: (section heading, body).
# Rendered as embed fields for /help and as plain-text lines for the
# "@Elfy help" mention trigger.
HELP_SECTIONS = [
    (
        "Chat with me",
        "Talk in the designated chat channel, a tracked thread, or DM me directly.",
    ),
    (
        "Images",
        'Ask me to generate or edit an image right in chat (e.g. "generate an image of...").',
    ),
    (
        "Slash commands",
        "`/help` — show this message\n"
        "`/forget [persona]` — clear this channel's history, optionally with a new persona\n"
        "`/createthread <name>` — start a thread I'll respond in fully\n"
        "`/setchat <channel>` — set the one channel I'll chat in (needs Manage Server)\n"
        "`/status` — my uptime and live stats",
    ),
]

_MENTION_RE = re.compile(r"<@!?\d+>")


def build_help_embed() -> discord.Embed:
    """Embed version of the help text — used by the /help slash command
    (slash command responses are always embeds, see commands.py)."""
    embed = discord.Embed(title=HELP_TITLE, color=discord.Color.blurple())
    for name, value in HELP_SECTIONS:
        embed.add_field(name=name, value=value, inline=False)
    return embed


def build_help_text() -> str:
    """Plain-text version — used by the "@Elfy help" mention trigger
    (mention-triggered replies are plain text, never embeds, matching the
    rest of message_handler.py's non-slash responses)."""
    lines = [f"**{HELP_TITLE}**"]
    for name, value in HELP_SECTIONS:
        lines.append(f"**{name}** — {value}")
    return "\n".join(lines)


def is_help_mention(message: Message) -> bool:
    """
    True if this message @mentions the bot and, once the mention itself is
    stripped out, the only text left is 'help' (case-insensitive).

    Guild-only — DMs don't need an @mention to reach the bot, so this
    mirrors the guild-only scoping already used for redirect mentions in
    message_handler.py.
    """
    if message.guild is None:
        return False
    bot_user = message.guild.me
    if not bot_user or not bot_user.mentioned_in(message):
        return False
    remaining = _MENTION_RE.sub("", message.content).strip().lower()
    return remaining == "help"


async def send_help_mention(message: Message) -> None:
    """Reply to a '@Elfy help' mention with the plain-text help summary,
    self-deleting after 5 seconds (same pattern as the redirect notice in
    message_handler.py)."""
    try:
        await message.reply(build_help_text(), mention_author=False, delete_after=5)
    except Exception as e:
        print(f"[help] Mention reply failed: {e}")


async def send_help_slash(interaction: Interaction) -> None:
    """Respond to /help with the embed version, then delete it after 5
    seconds. Interaction responses don't reliably support delete_after
    directly across discord.py versions, so this deletes explicitly."""
    await interaction.response.send_message(embed=build_help_embed())
    await asyncio.sleep(5)
    try:
        await interaction.delete_original_response()
    except discord.HTTPException:
        pass
