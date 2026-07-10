"""
Handles the on_member_join event: greets new members with a short,
unique, Gemini-generated welcome message, optionally followed by a
per-server custom line set via /setwelcome (see commands.do_setwelcome).
"""
import traceback

import discord

from storage import ChatDataManager


async def handle_member_join(member: discord.Member, ai_service) -> None:
    """
    Send a short AI-generated welcome message in the same channel Discord's
    own built-in "member just joined" system message appears in (the
    server's configured system channel), @mentioning the new member with a
    real Discord mention.

    Args:
        member: The Discord member who just joined
        ai_service: The AI service instance used to generate the greeting
    """
    channel = member.guild.system_channel
    if channel is None:
        print(
            f"[on_member_join] '{member.guild.name}' has no system channel "
            "configured (Server Settings > Overview > System Messages "
            "Channel) — skipping welcome message."
        )
        return

    try:
        greeting = await ai_service.generate_welcome_message(member.display_name)
    except Exception:
        print(traceback.format_exc())
        greeting = "Welcome aboard! So glad you're here 🎉"

    # Real discord.py mention object, not a manually built "@name" string —
    # and plain text only, since chat-style content never uses embeds.
    text = f"{member.mention} {greeting}"

    # /setwelcome's custom per-server addition, appended to the end of the
    # base greeting above — this is the ONE place the welcome message gets
    # assembled, so this is the one place that addition needs to hook in
    # (see commands.do_setwelcome, which just persists the text; this is
    # where it actually gets used).
    welcome_suffix = ChatDataManager.load_welcome_suffix(member.guild.id)
    if welcome_suffix:
        text = f"{text}\n{welcome_suffix}"

    try:
        await channel.send(text)
    except discord.Forbidden:
        print(f"[on_member_join] Missing permission to send in #{channel.name} ({channel.id}).")
    except Exception:
        print(traceback.format_exc())
