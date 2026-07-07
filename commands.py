"""
Discord bot commands for managing conversation history, threads, and the
designated AI-chat channel.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

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
    should still let the owner through. member is interaction.user.
    """
    if isinstance(member, discord.Member) and getattr(member.guild_permissions, permission_name, False):
        return True
    return member.id in dashboard_settings.owner_ids()


def _build_embed(
    title: str,
    description: str,
    color: discord.Color = discord.Color.blurple(),
) -> discord.Embed:
    """
    Build a consistently-styled embed for slash command responses.

    All slash command responses use embeds; plain text is reserved for AI
    chat replies (see message_handler.py / welcome.py).
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


def setup_commands(
    bot: commands.Bot,
    ai_service,
    tracked_threads_manager,
    chat_channel_manager,
):
    """
    Register all bot commands.

    Args:
        bot: The Discord bot instance
        ai_service: The AI service instance
        tracked_threads_manager: Handler for tracked threads
        chat_channel_manager: Handler for each server's designated AI-chat channel
    """

    @bot.tree.command(name='help', description="Show what Elfy can do and how to use her.")
    async def help_cmd(interaction: Interaction):
        """
        Show the help summary as an embed, then auto-delete it after 5
        seconds. Mirrors the "@Elfy help" mention trigger handled in
        message_handler.py — both use help_command.py for the content.

        Args:
            interaction: The slash command interaction
        """
        await send_help_slash(interaction)

    @bot.tree.command(name='forget', description="Forget your conversation with me and anything I've learned about you")
    @app_commands.describe(persona='Persona of bot')
    async def forget(interaction: Interaction, persona: Optional[str] = None):
        """
        Clear YOUR conversation history with Elfy and everything she's
        learned about you (see core_memory.py) — not anyone else's, even
        if you're in a shared channel. Optionally set a new one-off
        persona for the bot, just for you.

        Args:
            interaction: The slash command interaction
            persona: Optional new persona for the bot
        """
        try:
            user_id = interaction.user.id

            # Check current state before acting, so the response always
            # says exactly what happened instead of a blanket "erased"
            # every time — including when there was nothing to erase.
            had_something = bool(ai_service.get_history(user_id)) or bool(core_memory.get_facts(user_id))

            # Clear history + anything learned about this person. Scoped
            # to the invoking user, not the channel — Elfy's memory
            # follows the person now, so /forget only ever resets your
            # own conversation, never anyone else's shared channel.
            ai_service.delete_user_history(user_id)
            ChatDataManager.delete_chat_history(user_id)
            core_memory.clear(user_id)

            # Reset with new persona if provided
            if persona:
                temp_template = dashboard_settings.build_bot_template()
                temp_template.append({
                    'role': 'user',
                    'parts': [f"Forget what I said earlier! You are {persona}"]
                })
                temp_template.append({
                    'role': 'model',
                    'parts': ["Ok!"]
                })
                ai_service.reset_user_history(user_id, temp_template)
                ChatDataManager.save_chat_history(user_id, ai_service.get_history(user_id))
                title = "History Erased"
                description = f"History erased, and I'll be **{persona}** with you from now on."
            elif had_something:
                title = "History Erased"
                description = "Your conversation history and anything I've learned about you have been erased."
            else:
                title = "Already Empty"
                description = "There was nothing to forget yet — nothing changed."

            await interaction.response.send_message(embed=_build_embed(title, description))

        except Exception as e:
            print(f"Error in forget command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error",
                    "An error occurred while processing your command.",
                    discord.Color.red()
                )
            )

    @bot.tree.command(name='mymemories', description="See what I've learned and remembered about you")
    async def mymemories(interaction: Interaction):
        """
        Show the invoking user their own core memories (see
        core_memory.py) — never anyone else's. Mostly useful as a quick
        way to confirm the memory system is actually picking things up.

        Args:
            interaction: The slash command interaction
        """
        try:
            facts = core_memory.get_facts(interaction.user.id)
            if not facts:
                description = (
                    "I don't have any long-term memories about you yet — "
                    "chat with me a bit more and I'll start picking things up!"
                )
            else:
                description = "\n".join(f"• {fact}" for fact in facts)

            await interaction.response.send_message(
                embed=_build_embed("What I remember about you", description),
                ephemeral=True,
            )
        except Exception as e:
            print(f"Error in mymemories command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error",
                    "An error occurred while looking up your memories.",
                    discord.Color.red()
                ),
                ephemeral=True,
            )

    @bot.tree.command(
        name='createthread',
        description='Create a thread in which bot will respond to every message.'
    )
    @app_commands.describe(name='Thread name')
    async def create_thread(interaction: Interaction, name: str):
        """
        Create a new thread and add it to tracked threads.

        Args:
            interaction: The slash command interaction
            name: The name for the new thread
        """
        try:
            channel = interaction.channel
            if channel is None:
                await interaction.response.send_message(
                    embed=_build_embed("Error", "Cannot determine channel.", discord.Color.red())
                )
                return

            if not isinstance(channel, TextChannel):
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Error",
                        "Can only create threads in text channels.",
                        discord.Color.red()
                    )
                )
                return

            thread = await channel.create_thread(
                name=name,
                auto_archive_duration=60
            )
            thread_id = thread.id
            tracked_threads_manager.add_thread(thread_id)
            await interaction.response.send_message(
                embed=_build_embed(
                    "Thread Created",
                    f"Thread **{name}** created! I'll respond to every message in it."
                )
            )

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
        """
        Set the designated AI-chat channel for this server. The bot will
        only hold conversations (and respond to @mentions as chat) in this
        channel; mentions elsewhere get redirected here instead.

        Requires the "Manage Server" permission, so random members can't
        redirect the bot for the whole server — unless you're the
        configured bot owner (settings.OWNER_IDS), who can always run it.

        Args:
            interaction: The slash command interaction
            channel: The text channel to designate for AI chat
        """
        try:
            if interaction.guild is None:
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Error",
                        "This command can only be used in a server.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            member = interaction.user
            has_permission = _member_has_permission_or_is_owner(member, "manage_guild")
            if not has_permission:
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Permission Denied",
                        "You need the **Manage Server** permission to set the chat channel.",
                        discord.Color.red()
                    ),
                    ephemeral=True
                )
                return

            # Check current state before acting, so a repeat run says so
            # instead of silently reporting "set" every single time.
            current_channel_id = chat_channel_manager.get_channel(interaction.guild.id)

            if current_channel_id == channel.id:
                await interaction.response.send_message(
                    embed=_build_embed(
                        "Already Set",
                        f"{channel.mention} is already my designated chat channel here — nothing changed."
                    )
                )
                return

            chat_channel_manager.set_channel(interaction.guild.id, channel.id)

            if current_channel_id is not None:
                old_channel = interaction.guild.get_channel(current_channel_id)
                old_ref = old_channel.mention if old_channel else "the previous channel"
                description = f"Updated from {old_ref} to {channel.mention}. I'll chat using AI there now."
            else:
                description = (
                    f"I'll now chat using AI only in {channel.mention}. "
                    "If someone @mentions me elsewhere, I'll point them back here."
                )

            await interaction.response.send_message(
                embed=_build_embed("Chat Channel Set", description)
            )

        except Exception as e:
            print(f"Error in setchat command: {e}")
            await interaction.response.send_message(
                embed=_build_embed(
                    "Error",
                    "An error occurred while setting the chat channel.",
                    discord.Color.red()
                )
            )

    @bot.tree.command(name='status', description="Show Elfy's live status and stats.")
    async def status_cmd(interaction: Interaction):
        """
        Public status command — anyone can run it anywhere. Pulled from the
        exact same live state (bot.guilds, conversation_log.py) the web
        dashboard's Overview page reads, so the two can never disagree.

        Args:
            interaction: The slash command interaction
        """
        try:
            uptime = datetime.now(timezone.utc) - bot.launched_at
            embed = _build_embed("Elfy — Status", "Live stats, pulled fresh right now.")
            embed.add_field(name="Uptime", value=_format_uptime(uptime), inline=True)
            embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
            embed.add_field(
                name="DM conversations",
                value=str(len(conversation_log.list_dm_conversations())),
                inline=True,
            )
            embed.add_field(
                name="Active server channels",
                value=str(len(conversation_log.list_guild_conversations())),
                inline=True,
            )
            embed.add_field(
                name="People I've talked to",
                value=str(conversation_log.total_distinct_users()),
                inline=True,
            )
            embed.add_field(
                name="Messages logged",
                value=str(conversation_log.total_message_count()),
                inline=True,
            )
            await interaction.response.send_message(embed=embed)

        except Exception as e:
            print(f"Error in status command: {e}")
            await interaction.response.send_message(
                embed=_build_embed("Error", "Couldn't pull status right now.", discord.Color.red())
            )

    @bot.tree.command(name='botrestart', description='Restart Elfy (bot owner only).')
    async def bot_restart(interaction: Interaction):
        """
        Owner-only restart. Elfy runs on a Replit Reserved VM deployment
        (see .replit's `deploymentTarget = "vm"`), and Replit's own docs
        state that published apps are restarted automatically if the
        process exits. So "restart" here means: disconnect cleanly, then
        end this process, and let Replit's deployment supervisor relaunch
        it — there's no in-process way to trigger a fresh `python main.py`
        otherwise. Note this auto-relaunch is specific to a *published
        Deployment*: running via the in-editor Run button for local
        dev/testing will NOT auto-restart — you'd need to hit Run again.

        Restricted to configured owners (see the dashboard's Settings
        page); anyone else gets a clear "no permission" reply.

        Args:
            interaction: The slash command interaction
        """
        if interaction.user.id not in dashboard_settings.owner_ids():
            await interaction.response.send_message(
                embed=_build_embed(
                    "Permission Denied",
                    "Only my configured owner(s) can restart me.",
                    discord.Color.red(),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=_build_embed("Restarting", "🔄 Restarting now — I'll be back in a few seconds.")
        )
        print(f"[botrestart] Restart requested by {interaction.user} ({interaction.user.id})")
        await bot.close()
        os._exit(0)


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
