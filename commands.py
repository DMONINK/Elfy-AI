"""
Discord bot commands for managing conversation history, threads, the
designated AI-chat channel, and welcome-message customization.

Every command's actual logic lives in a module-level `do_*` function that
takes plain data (IDs, objects, strings) rather than an Interaction or
Message — that's what lets both the slash command (registered below in
setup_commands) and the "@Elfy <command>" mention trigger (see
mention_commands.py) share one implementation instead of maintaining the
same logic twice. Owner-only commands (status/restart/the cross-server
memory lookup) are the exception: they're never registered as slash
commands at all (see setup_commands' docstring for why), so their do_*
functions are only ever called from mention_commands.py.
"""
import os
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import discord
from discord import Interaction, app_commands, TextChannel
from discord.ext import commands

import conversation_log
import dashboard_settings
import core_memory
from help_command import send_help_slash
from storage import ChatDataManager


def _member_has_permission_or_is_owner(member, permission_name: str) -> bool:
    """
    True if member has the named guild_permissions attribute (e.g.
    'manage_guild'), OR is a configured bot owner (see the Owner IDs
    field on the dashboard's Settings page) — owners can run every
    command regardless of server permissions.

    Use this for any command that currently gates on a permission and
    should still let the owner through. member is interaction.user (slash)
    or message.author (mention) — both are discord.Member in guild
    contexts, which is all this is ever called for.
    """
    if isinstance(member, discord.Member) and getattr(member.guild_permissions, permission_name, False):
        return True
    return member.id in dashboard_settings.owner_ids()


def is_owner(user_id: int) -> bool:
    """
    The single, live, dashboard-editable owner check used to gate the
    owner-only commands (status/restart/memory-lookup/mhelp) in
    mention_commands.py. Backed by dashboard_settings.owner_ids() — NOT
    settings.is_owner(), which exists in settings.py but is dead code
    (nothing calls it) and wouldn't pick up an owner added via the
    dashboard without a redeploy. This is the exact same check
    _member_has_permission_or_is_owner() falls back to above.
    """
    return user_id in dashboard_settings.owner_ids()


def _build_embed(
    title: str,
    description: str,
    color: discord.Color = discord.Color.blurple(),
) -> discord.Embed:
    """
    Build a consistently-styled embed for slash command responses.

    All slash command responses use embeds; plain text is reserved for AI
    chat replies and mention-triggered command replies (see
    message_handler.py / mention_commands.py / welcome.py).
    """
    return discord.Embed(title=title, description=description, color=color)


def _format_uptime(delta: timedelta) -> str:
    """Human-readable uptime, e.g. '2d 4h 13m' or '13m' for a fresh boot."""
    total_seconds = int(delta.total_seconds())
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


@dataclass
class CommandResult:
    """
    A command's outcome, translated into whatever the caller (a slash
    command or an '@Elfy ...' mention) needs to actually send. Sharing
    one of these between both entry points is what gives every command
    real slash/mention parity without duplicating logic — see the do_*
    functions below, and mention_commands.py for the mention side.
    """
    title: str
    description: str
    color: discord.Color = discord.Color.blurple()
    ephemeral: bool = False  # only meaningful for slash responses


def _embed_from_result(result: CommandResult) -> discord.Embed:
    return _build_embed(result.title, result.description, result.color)


async def _send_and_auto_delete(
    interaction: Interaction,
    embed: discord.Embed,
    *,
    ephemeral: bool = False,
    delay: int = 10,
) -> None:
    """
    Send a slash command response, then delete it after `delay` seconds —
    the standard behavior for every command's response except /help
    (help_command.py's send_help_slash intentionally does not use this).
    Interaction responses don't support delete_after directly, so this
    sleeps then explicitly deletes — the same pattern /help itself used
    to use before help was made persistent.
    """
    await interaction.response.send_message(embed=embed, ephemeral=ephemeral)
    if delay is not None:
        await asyncio.sleep(delay)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass


# ── Shared command logic (do_*) — used by both slash commands below and ──
# ── the "@Elfy <command>" mention trigger in mention_commands.py ─────────

async def do_forget(
    guild_id: Optional[int],
    channel_id: int,
    persona: Optional[str],
    ai_service,
) -> CommandResult:
    """
    /forget — channel-scoped: clears THIS CHANNEL's rolling conversation
    history (and its one-off persona override, if any), not any specific
    person's. Never touches core memory — core memory is a per-person,
    per-server durable thing now, out of scope for a channel-level
    command (see /forgetme for the user-scoped equivalent).
    """
    scope_had_something = bool(ai_service.get_channel_history(guild_id, channel_id))
    ai_service.delete_channel_history(guild_id, channel_id)
    ChatDataManager.delete_chat_history((guild_id, channel_id))

    if persona:
        temp_template = dashboard_settings.build_bot_template()
        temp_template.append({
            'role': 'user',
            'parts': [f"Forget what I said earlier! You are {persona}"],
        })
        temp_template.append({'role': 'model', 'parts': ["Ok!"]})
        ai_service.reset_channel_history(guild_id, channel_id, temp_template)
        ChatDataManager.save_chat_history(
            (guild_id, channel_id), ai_service.get_channel_history(guild_id, channel_id)
        )
        return CommandResult(
            "History Erased",
            f"This channel's history is erased, and I'll be **{persona}** here from now on.",
        )
    elif scope_had_something:
        return CommandResult(
            "History Erased", "This channel's conversation history with me has been erased."
        )
    else:
        return CommandResult(
            "Already Empty", "There was nothing to forget in this channel yet — nothing changed."
        )


async def do_forgetme(user_id: int, ai_service) -> CommandResult:
    """
    /forgetme — user-scoped, and genuinely global: wipes this user's core
    memory in EVERY server (and DMs) at once, not just the one it's run
    in — the whole point of "forget me" is that you shouldn't have to
    repeat it in every server you've talked to Elfy in. Distinct from
    /forget, which only ever touches one channel.
    """
    cleared_count = core_memory.clear_all_for_user(user_id)
    ChatDataManager.delete_legacy_user_history(user_id)  # scrub pre-migration leftovers too

    if cleared_count == 0:
        return CommandResult(
            "Already Empty", "I didn't have anything stored about you yet — nothing to forget."
        )
    server_word = "server" if cleared_count == 1 else "servers"
    return CommandResult(
        "Memories Erased",
        f"Everything I'd learned about you is gone — across {cleared_count} {server_word}.",
    )


async def do_mymemories(guild_id: Optional[int], user_id: int) -> CommandResult:
    """
    /mymemories — shows the invoking user their own core memories, scoped
    to the CURRENT server/DM only (not aggregated across every server
    they've talked to Elfy in) — "what's shaping my replies right here,"
    which is more predictable and doesn't surface a fact from an unrelated
    server context by surprise. Contrast with the owner-only cross-server
    lookup, which deliberately does aggregate (see do_memory_lookup).
    """
    facts = core_memory.get_facts(guild_id, user_id)
    if not facts:
        description = (
            "I don't have any long-term memories about you here yet — "
            "chat with me a bit more and I'll start picking things up!"
        )
    else:
        description = "\n".join(f"• {fact}" for fact in facts)
    return CommandResult("What I remember about you here", description, ephemeral=True)


async def do_createthread(channel, name: str, tracked_threads_manager) -> CommandResult:
    """/createthread — start a thread Elfy will respond to every message in."""
    if channel is None:
        return CommandResult("Error", "Cannot determine channel.", discord.Color.red())
    if not isinstance(channel, TextChannel):
        return CommandResult(
            "Error", "Can only create threads in text channels.", discord.Color.red()
        )
    thread = await channel.create_thread(name=name, auto_archive_duration=60)
    tracked_threads_manager.add_thread(thread.id)
    return CommandResult(
        "Thread Created", f"Thread **{name}** created! I'll respond to every message in it."
    )


async def do_setchat(guild: discord.Guild, channel: TextChannel, member, chat_channel_manager) -> CommandResult:
    """/setchat — set the one channel Elfy holds AI conversations in for this server."""
    if not _member_has_permission_or_is_owner(member, "manage_guild"):
        return CommandResult(
            "Permission Denied",
            "You need the **Manage Server** permission to set the chat channel.",
            discord.Color.red(),
            ephemeral=True,
        )

    current_channel_id = chat_channel_manager.get_channel(guild.id)
    if current_channel_id == channel.id:
        return CommandResult(
            "Already Set", f"{channel.mention} is already my designated chat channel here — nothing changed."
        )

    chat_channel_manager.set_channel(guild.id, channel.id)

    if current_channel_id is not None:
        old_channel = guild.get_channel(current_channel_id)
        old_ref = old_channel.mention if old_channel else "the previous channel"
        description = f"Updated from {old_ref} to {channel.mention}. I'll chat using AI there now."
    else:
        description = (
            f"I'll now chat using AI only in {channel.mention}. "
            "If someone @mentions me elsewhere, I'll point them back here."
        )
    return CommandResult("Chat Channel Set", description)


async def do_setwelcome(guild_id: int, member, text: str) -> CommandResult:
    """
    /setwelcome — append custom text to the end of Elfy's welcome message
    for new members, persisted per-server (see welcome.py, which hooks
    into the SAME assembly point the base greeting already uses, rather
    than building a parallel welcome path).
    """
    if not _member_has_permission_or_is_owner(member, "manage_guild"):
        return CommandResult(
            "Permission Denied",
            "You need the **Manage Server** permission to set the welcome message.",
            discord.Color.red(),
            ephemeral=True,
        )
    text = text.strip()
    if not text:
        return CommandResult(
            "Nothing to Add",
            "Give me some text to append, e.g. `/setwelcome Check out #rules and say hi!`",
            discord.Color.red(),
        )
    ChatDataManager.save_welcome_suffix(guild_id, text)
    return CommandResult("Welcome Message Updated", f"New members will now also see:\n\n{text}")


# ── Owner-only logic — never registered as a slash command (see          ──
# ── setup_commands' docstring); only ever called from mention_commands.py ─

async def do_status(bot: commands.Bot) -> discord.Embed:
    """
    Owner-only status: uptime and live stats, pulled from the exact same
    live state (bot.guilds, conversation_log.py) the web dashboard's
    Overview page reads, so the two can never disagree. Returns a full
    Embed directly (unlike the other do_* functions) since it needs
    several individual fields, not just a title+description.
    """
    uptime = datetime.now(timezone.utc) - bot.launched_at
    embed = _build_embed("Elfy — Status", "Live stats, pulled fresh right now.")
    embed.add_field(name="Uptime", value=_format_uptime(uptime), inline=True)
    embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
    embed.add_field(
        name="DM conversations", value=str(len(conversation_log.list_dm_conversations())), inline=True
    )
    embed.add_field(
        name="Active server channels",
        value=str(len(conversation_log.list_guild_conversations())),
        inline=True,
    )
    embed.add_field(
        name="People I've talked to", value=str(conversation_log.total_distinct_users()), inline=True
    )
    embed.add_field(
        name="Messages logged", value=str(conversation_log.total_message_count()), inline=True
    )
    return embed


async def do_restart(bot: commands.Bot, requester) -> None:
    """
    Owner-only restart. Elfy runs on a Replit Reserved VM deployment (see
    .replit's `deploymentTarget = "vm"`), and Replit's own docs state
    that published apps are restarted automatically if the process exits.
    So "restart" here means: disconnect cleanly, then end this process,
    and let Replit's deployment supervisor relaunch it — there's no
    in-process way to trigger a fresh `python main.py` otherwise. This
    auto-relaunch is specific to a *published Deployment*: running via
    the in-editor Run button for local dev/testing will NOT auto-restart.
    """
    print(f"[restart] Restart requested by {requester} ({requester.id})")
    await bot.close()
    os._exit(0)


async def do_memory_lookup(bot: commands.Bot, target_user_id: int) -> CommandResult:
    """
    Owner-only: everything Elfy has learned about a given Discord user ID,
    broken out by server (or DMs) — a deliberate, explicit exception to
    the normal per-guild isolation every other memory read goes through
    (see core_memory.get_all_scopes_for_user), restricted to owners for
    that reason.
    """
    records = core_memory.get_all_scopes_for_user(target_user_id)
    if not records:
        return CommandResult(
            "No Memories Found", f"I don't have any stored memories for user ID `{target_user_id}`."
        )

    sections = []
    for guild_id, record in records.items():
        if guild_id is None:
            label = "Direct Messages"
        else:
            guild_obj = bot.get_guild(guild_id)
            label = guild_obj.name if guild_obj else f"Server ID {guild_id}"
        bullet_list = "\n".join(f"• {f}" for f in record.get("facts", []))
        sections.append(f"**{label}**\n{bullet_list}")

    description = "\n\n".join(sections)
    if len(description) > 4000:  # embed description hard limit is 4096
        description = description[:3997] + "..."
    return CommandResult(f"Memories for user {target_user_id}", description)


def setup_commands(
    bot: commands.Bot,
    ai_service,
    tracked_threads_manager,
    chat_channel_manager,
):
    """
    Register all PUBLIC slash commands (owner-only commands — restart,
    status, the cross-server memory lookup, and mhelp — are deliberately
    NEVER registered here or anywhere else as slash commands: doing so
    would make them discoverable/visible in Discord's UI to every member,
    which defeats the point of restricting them. They're reachable only
    by tagging the bot — see mention_commands.py, which owns the
    owner-ID gate for all four).

    Every command registered here also works by tagging the bot instead
    of using slash (e.g. "@Elfy forget") — see mention_commands.py, which
    calls the exact same do_* functions defined above.

    Args:
        bot: The Discord bot instance
        ai_service: The AI service instance
        tracked_threads_manager: Handler for tracked threads
        chat_channel_manager: Handler for each server's designated AI-chat channel
    """

    @bot.tree.command(name='help', description="Show what Elfy can do and how to use her.")
    async def help_cmd(interaction: Interaction):
        """Show the help summary as an embed. Mirrors the "@Elfy help"
        mention trigger — both use help_command.py for the content."""
        await send_help_slash(interaction)

    @bot.tree.command(
        name='forget',
        description="Clear this channel's conversation history with me.",
    )
    @app_commands.describe(persona='Optional new persona for me to use in this channel')
    async def forget(interaction: Interaction, persona: Optional[str] = None):
        try:
            guild_id = interaction.guild.id if interaction.guild else None
            result = await do_forget(guild_id, interaction.channel.id, persona, ai_service)
            await _send_and_auto_delete(interaction, _embed_from_result(result))
        except Exception as e:
            print(f"Error in forget command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error", "An error occurred while processing your command.", discord.Color.red()
                )
            )

    @bot.tree.command(
        name='forgetme',
        description="Erase everything I remember about you, across every server.",
    )
    async def forgetme(interaction: Interaction):
        try:
            result = await do_forgetme(interaction.user.id, ai_service)
            await _send_and_auto_delete(interaction, _embed_from_result(result), ephemeral=result.ephemeral)
        except Exception as e:
            print(f"Error in forgetme command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error", "An error occurred while erasing your memories.", discord.Color.red()
                )
            )

    @bot.tree.command(name='mymemories', description="See what I've learned and remembered about you here")
    async def mymemories(interaction: Interaction):
        try:
            guild_id = interaction.guild.id if interaction.guild else None
            result = await do_mymemories(guild_id, interaction.user.id)
            await _send_and_auto_delete(interaction, _embed_from_result(result), ephemeral=result.ephemeral)
        except Exception as e:
            print(f"Error in mymemories command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error", "An error occurred while looking up your memories.", discord.Color.red()
                ),
                ephemeral=True,
            )

    @bot.tree.command(
        name='createthread',
        description='Create a thread in which bot will respond to every message.'
    )
    @app_commands.describe(name='Thread name')
    async def create_thread(interaction: Interaction, name: str):
        try:
            result = await do_createthread(interaction.channel, name, tracked_threads_manager)
            await _send_and_auto_delete(interaction, _embed_from_result(result))
        except Exception as e:
            print(f"Error in createthread command: {e}")
            await interaction.response.send_message(
                embed=_build_embed("Error", "Error creating thread!", discord.Color.red())
            )

    @bot.tree.command(
        name='setchat',
        description='Set the one channel where I will chat using AI.'
    )
    @app_commands.describe(channel='The channel to designate for AI chat')
    async def setchat(interaction: Interaction, channel: TextChannel):
        try:
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Error", "This command can only be used in a server.", discord.Color.red()
                    ),
                    ephemeral=True,
                )
                return
            result = await do_setchat(interaction.guild, channel, interaction.user, chat_channel_manager)
            await _send_and_auto_delete(interaction, _embed_from_result(result), ephemeral=result.ephemeral)
        except Exception as e:
            print(f"Error in setchat command: {e}")
            await interaction.response.send_message(
                embed=_build_embed("Error", "An error occurred while setting the chat channel.", discord.Color.red())
            )

    @bot.tree.command(
        name='setwelcome',
        description="Add a custom line to the end of my welcome message for new members.",
    )
    @app_commands.describe(text='Text to append to the welcome message')
    async def setwelcome(interaction: Interaction, text: str):
        try:
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Error", "This command can only be used in a server.", discord.Color.red()
                    ),
                    ephemeral=True,
                )
                return
            result = await do_setwelcome(interaction.guild.id, interaction.user, text)
            await _send_and_auto_delete(interaction, _embed_from_result(result), ephemeral=result.ephemeral)
        except Exception as e:
            print(f"Error in setwelcome command: {e}")
            await interaction.response.send_message(
                embed=_build_embed("Error", "An error occurred while updating the welcome message.", discord.Color.red())
            )

    # NOTE: /status, /botrestart, and the cross-server memory lookup are
    # intentionally NOT registered here — see this function's docstring.
    # Their logic lives in do_status/do_restart/do_memory_lookup above;
    # mention_commands.py calls them directly after checking is_owner().


class TrackedThreadsManager:
    """Manages tracked threads."""

    def __init__(self):
        """Initialize and load tracked threads."""
        self.threads = ChatDataManager.load_tracked_threads()

    def add_thread(self, thread_id: int) -> None:
        """
        Add a thread to tracked threads.

        Args:
            thread_id: The Discord thread ID
        """
        if thread_id not in self.threads:
            self.threads.append(thread_id)
            self.save()

    def remove_thread(self, thread_id: int) -> None:
        """
        Remove a thread from tracked threads.

        Args:
            thread_id: The Discord thread ID
        """
        if thread_id in self.threads:
            self.threads.remove(thread_id)
            self.save()

    def get_all_threads(self) -> list:
        """Get all tracked thread IDs."""
        return self.threads

    def save(self) -> None:
        """Save tracked threads to persistent storage."""
        ChatDataManager.save_tracked_threads(self.threads)


class ChatChannelManager:
    """Manages the single designated AI-chat channel for each server."""

    def __init__(self):
        """Initialize and load designated chat channels (per guild)."""
        self.channels: Dict[int, int] = ChatDataManager.load_chat_channels()

    def set_channel(self, guild_id: int, channel_id: int) -> None:
        """
        Set the designated AI-chat channel for a guild.

        Args:
            guild_id: The Discord guild (server) ID
            channel_id: The Discord channel ID to designate
        """
        self.channels[guild_id] = channel_id
        self.save()

    def get_channel(self, guild_id: Optional[int]) -> Optional[int]:
        """
        Get the designated AI-chat channel ID for a guild, if any.

        Args:
            guild_id: The Discord guild (server) ID, or None (e.g. DMs)

        Returns:
            The designated channel ID, or None if unset/not applicable
        """
        if guild_id is None:
            return None
        return self.channels.get(guild_id)

    def save(self) -> None:
        """Save the designated chat channels to persistent storage."""
        ChatDataManager.save_chat_channels(self.channels)
