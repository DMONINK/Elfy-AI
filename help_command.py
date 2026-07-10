"""
Help command — shared content and senders for both the "@Elfy help"
mention trigger and the "/help" slash command (see message_handler.py /
mention_commands.py and commands.py). Both surfaces show the same public
information, as an embed, and — unlike every other command's response —
never auto-delete: help is meant to stick around for people to read.

Also home to "mhelp", the owner-only counterpart: shows ONLY the
owner-only commands (restart/status/memory-lookup), not the public ones
again — an owner already has /help for those. mhelp is deliberately
tag-only, with no slash-command version — see build_mhelp_embed()'s
docstring.
"""
import re

import discord
from discord import Interaction, Message

HELP_TITLE = "🧚 Elfy — Help"
MHELP_TITLE = "🔑 Elfy — Owner Help"

# Single source of truth for the public help content: (field name, field
# value), rendered as embed fields for both /help and "@Elfy help" — the
# two entry points share one embed builder now, see build_help_embed().
# Must NOT list bot restart, bot status, or the memory-lookup command —
# see OWNER_ONLY_SECTIONS below for those.
HELP_SECTIONS = [
    (
        "💬 Chat with me",
        "Talk in my home channel, a tracked thread, or DM me directly. "
        "Every command below also works by tagging me instead of using slash — "
        "e.g. `@Elfy forget` — and works anywhere, not just where I normally chat.",
    ),
    (
        "🎨 Images",
        'Ask me to generate or edit an image right in chat (e.g. "generate an image of...").\n'
        'For a picture of me specifically: create/show/send/make a selfie, picture, or '
        'image of you, with an optional description — e.g. "send a selfie of you at the beach".',
    ),
    (
        "🏷️ My nickname",
        'Ask me to change my own nickname in this server, e.g. "change your name to '
        'Sparkling Angel".',
    ),
    (
        "⚡ Slash commands",
        "`/help` — show this message\n"
        "`/forget [persona]` — clear this channel's history, optionally with a new persona\n"
        "`/forgetme` — erase everything I remember about you, across every server\n"
        "`/mymemories` — see what I remember about you here\n"
        "`/createthread <name>` — start a thread I'll respond in fully\n"
        "`/setchat <channel>` — set the one channel I'll chat in (needs Manage Server)\n"
        "`/setwelcome <text>` — add a line to new members' welcome message (needs Manage Server)",
    ),
]

# mhelp's ENTIRE content — deliberately just this, not HELP_SECTIONS plus
# this. An owner already sees the public commands via /help; mhelp is a
# focused, separate reference for the extra tag-only powers, not a
# combined dump of everything.
OWNER_ONLY_SECTIONS = [
    (
        "🔧 Only you can run these",
        "Tag-only, never slash — by design, so they never show up in anyone else's "
        "command list.\n\n"
        "`@Elfy restart` — restart the bot process\n"
        "`@Elfy status` — uptime and live stats\n"
        "`@Elfy memories <user id>` — see everything I remember about that user, "
        "across every server\n"
        "`@Elfy mhelp` — show this message",
    ),
]

_MENTION_RE = re.compile(r"<@!?\d+>")


def build_help_embed() -> discord.Embed:
    """The public help embed — used by both /help and "@Elfy help"."""
    embed = discord.Embed(
        title=HELP_TITLE,
        description="Here's everything I can do! ✨",
        color=discord.Color.blurple(),
    )
    for name, value in HELP_SECTIONS:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Tip: every command also works by tagging me instead of using slash!")
    return embed


def build_mhelp_embed() -> discord.Embed:
    """
    The owner-only mhelp embed — ONLY the owner-only commands, not the
    public ones again (see OWNER_ONLY_SECTIONS' docstring). Deliberately
    tag-only, with no slash-command counterpart: registering owner
    commands as slash commands would make them discoverable/visible in
    Discord's UI to every member, which defeats the point of restricting
    them. A distinct gold color from the public help embed, so it's
    visually obvious at a glance which one you're looking at. See
    mention_commands.py for the owner-ID gate on who can actually
    trigger this.
    """
    embed = discord.Embed(
        title=MHELP_TITLE,
        description="Extra commands only you can see or run.",
        color=discord.Color.gold(),
    )
    for name, value in OWNER_ONLY_SECTIONS:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="These never show up as slash commands, on purpose.")
    return embed


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
    """Reply to a '@Elfy help' mention with the help embed. Deliberately
    does NOT auto-delete, unlike every other command's response — help is
    meant to stick around for people to actually read, not vanish after
    10 seconds."""
    try:
        await message.reply(embed=build_help_embed(), mention_author=False)
    except Exception as e:
        print(f"[help] Mention reply failed: {e}")


async def send_mhelp_mention(message: Message) -> None:
    """Reply to a '@Elfy mhelp' mention with the owner help embed. Same
    no-auto-delete treatment as send_help_mention. Callers must already
    have verified the sender is an owner — see mention_commands.py — this
    function doesn't re-check."""
    try:
        await message.reply(embed=build_mhelp_embed(), mention_author=False)
    except Exception as e:
        print(f"[mhelp] Mention reply failed: {e}")


async def send_help_slash(interaction: Interaction) -> None:
    """Respond to /help with the same embed the mention trigger uses.
    Deliberately does NOT auto-delete (previously self-deleted after 5
    seconds — help is meant to stick around now, unlike every other
    command's response)."""
    await interaction.response.send_message(embed=build_help_embed())
