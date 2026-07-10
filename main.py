"""
Main entry point for the Discord Gemini Chatbot.

Runs the Discord bot and its web dashboard together, on the same asyncio
event loop, in the same process — this is what lets the dashboard read
live bot state (bot.guilds, active AI sessions) directly and safely
`await` bot calls (e.g. updating presence) with no threads or
cross-thread synchronization involved.
"""
import asyncio
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands
from aiohttp import web

from settings import DISCORD_BOT_TOKEN, BOT_PREFIX
import dashboard_settings
from ai_service import AIService
from storage import ChatDataManager
from message_handler import handle_message
from commands import setup_commands, TrackedThreadsManager, ChatChannelManager
from welcome import handle_member_join
from web_dashboard import create_dashboard_app

DASHBOARD_PORT = 8080  # matches the externally-exposed port in .replit


class GeminiBot:
    """Main bot class managing initialization and event handling."""

    def __init__(self):
        """Initialize the bot and all services."""
        intents = discord.Intents.default()
        intents.message_content = True
        # Required for on_member_join to fire at all. This is a privileged
        # intent — it must ALSO be turned on in the Discord Developer
        # Portal under Bot > Privileged Gateway Intents > Server Members
        # Intent, or the bot will fail to start with a
        # PrivilegedIntentsRequired error.
        intents.members = True

        self.bot = commands.Bot(
            command_prefix=BOT_PREFIX,
            intents=intents,
            help_command=None,
            activity=discord.Game(dashboard_settings.get("bot_activity")),
        )
        # Used by the /status command to report uptime.
        self.bot.launched_at = datetime.now(timezone.utc)

        self.ai_service = AIService()
        self.storage_manager = ChatDataManager()
        self.threads_manager = TrackedThreadsManager()
        self.chat_channel_manager = ChatChannelManager()

        self._load_persisted_data()
        self._register_event_handlers()
        setup_commands(
            self.bot,
            self.ai_service,
            self.threads_manager,
            self.chat_channel_manager,
        )

    def _load_persisted_data(self) -> None:
        history_data = ChatDataManager.load_chat_history()
        self.ai_service.load_history(history_data)
        self.threads_manager.threads = ChatDataManager.load_tracked_threads()

    def _register_event_handlers(self) -> None:
        @self.bot.event
        async def on_ready():
            await self.bot.tree.sync()
            print("----------------------------------------")
            print(f'Gemini Bot Logged in as {self.bot.user}')
            print(f'Dashboard listening on port {DASHBOARD_PORT}')
            print("----------------------------------------")

        @self.bot.event
        async def on_message(message: discord.Message):
            await handle_message(
                message,
                self.bot,
                self.ai_service,
                self.storage_manager,
                self.chat_channel_manager,
                self.threads_manager,
            )

        @self.bot.event
        async def on_member_join(member: discord.Member):
            await handle_member_join(member, self.ai_service)

    async def start(self) -> None:
        if not DISCORD_BOT_TOKEN:
            raise ValueError("DISCORD_BOT_TOKEN environment variable is not set")
        await self.bot.start(DISCORD_BOT_TOKEN)


async def run_dashboard(gemini_bot: GeminiBot, port: int = DASHBOARD_PORT) -> None:
    """Serve the web dashboard on the same event loop as the bot. Also
    doubles as Replit's health check — any 200 response on this port
    (the dashboard's login page, if no session yet) satisfies it."""
    app = create_dashboard_app(
        bot=gemini_bot.bot,
        ai_service=gemini_bot.ai_service,
        chat_channel_manager=gemini_bot.chat_channel_manager,
        threads_manager=gemini_bot.threads_manager,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    if not os.getenv("DASHBOARD_PASSWORD"):
        print(
            "[dashboard] WARNING: DASHBOARD_PASSWORD is not set — the "
            "dashboard will show setup instructions instead of serving "
            "anything until you add it (Replit Secrets, or "
            ".env.development for local dev)."
        )


async def main() -> None:
    gemini_bot = GeminiBot()
    await asyncio.gather(
        run_dashboard(gemini_bot),
        gemini_bot.start(),
    )


if __name__ == '__main__':
    asyncio.run(main())
