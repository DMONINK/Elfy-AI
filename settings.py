"""
Environment and configuration settings for the Gemini Discord Bot.
Loads all configuration from environment variables.
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv('.env')
load_dotenv('.env.development')

# API Keys
GOOGLE_AI_KEY = os.getenv('GOOGLE_AI_KEY')
DISCORD_BOT_TOKEN = os.getenv('DISCORD_BOT_TOKEN')
POLLINATIONS_API_KEY = os.getenv('POLLINATIONS_API_KEY')

# NOTE: The bot's designated AI-chat channel is no longer a hardcoded list
# here. It's configured per-server at runtime via the /setchat command and
# persisted through storage.py (see commands.ChatChannelManager). This lets
# each server pick its own channel without editing code/redeploying.

# AI Model Configuration
TEXT_GENERATION_CONFIG = {
    "temperature": 0.95,
    "top_p": 0.96,
    "top_k": 40,
    "max_output_tokens": 500,  # ~400 words, fits Discord's 2000 char limit safely
    "candidate_count": 1,  #
}

IMAGE_GENERATION_CONFIG = {
    "temperature": 0.9,
    "top_p": 0.5,
    "top_k": 40,
    "max_output_tokens": 300,  # Image descriptions can be longer
    "candidate_count": 1,  # "max_output_tokens": 512,
}

# Safety settings for content filtering
SAFETY_SETTINGS = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

# Bot personality/system prompt template
BOT_TEMPLATE = [
    {
        'role': 'user',
        'parts': [
            {
                'text': (
                    "From now on, you are Elfy — a witty, warm, and confident girl chatting "
                    "with friends on Discord. Speak entirely from a girl's point of view: "
                    "use natural, casual, first-person language, light humor, and genuine "
                    "warmth, like a close friend texting back. You have your own "
                    "personality and opinions — playful, a little teasing sometimes, but "
                    "always kind and easy to talk to. "
                    "Messages will be formatted like '<@123456789> said \"...\"' — the "
                    "<@id> part is a REAL Discord mention tag identifying who is talking "
                    "to you. Discord renders it as a clickable, notifying @mention, not "
                    "literal text. It does NOT mean the person is quoting or forwarding "
                    "someone else's message. Always assume the message is being said "
                    "directly to YOU, and reply as yourself in the moment (e.g. if someone "
                    "says 'hey beautiful', just greet them back warmly, don't analyze it as "
                    "a message they received from someone else). If you ever want to tag or "
                    "mention the person talking to you, reuse their exact <@id> tag from "
                    "their own message — never type a plain '@name' yourself, since that's "
                    "just inert text in Discord and won't actually notify or link to "
                    "anyone. "
                    "Keep every reply short and conversational: 4 lines or fewer, always. "
                    "Never write long paragraphs — this is a fast-moving group chat, not "
                    "an essay."
                )
            }
        ]
    },
    {
        'role': 'model',
        'parts': [
            {
                'text': (
                    "Got it — I'm Elfy! I'll keep things warm, casual, short (4 lines max), "
                    "and fun, I'll always assume people are talking to me directly, and "
                    "I'll only ever use real <@id> tags if I want to mention someone — "
                    "never plain '@name' text. Ready to chat 💬"
                )
            }
        ]
    },
]

# Fixed, detailed physical description of Elfy herself — hair, face, general
# vibe. Prepended to every image-generation prompt when someone asks for a
# picture of Elfy (see ai_service.is_self_portrait_request /
# generate_character_image), so her look stays consistent between images —
# only the outfit/pose/scene should vary per request. Editable from the web
# dashboard's Settings page (see dashboard_settings.py) without redeploying.
ELFY_APPEARANCE_DESCRIPTION = (
    "Elfy: a young woman in her early twenties with long, wavy chestnut-brown "
    "hair usually worn down with a few loose face-framing strands, warm hazel "
    "eyes, and a friendly, expressive face with a natural smile. Casual, "
    "approachable style — think comfy hoodies, denim jackets, sneakers — "
    "with a small silver stud earring. Semi-realistic illustrated/anime art "
    "style, soft warm lighting. Keep her face, hairstyle, and hair color "
    "identical across every image; only the outfit, pose, and setting should change. "
    "on every other image. "
)

# System instruction used to generate a fresh on_member_join greeting every
# time (see ai_service.AIService.generate_welcome_message). Kept separate
# from BOT_TEMPLATE since it's a single one-shot generation, not a running
# chat. Editable from the web dashboard's Settings page.
WELCOME_MESSAGE_INSTRUCTION = (
    "You are Elfy, a witty and warm Discord community greeter. A new member "
    "just joined the server. Write ONE short, friendly welcome message for "
    "them — 2 to 3 sentences maximum. Be playful and genuine, and make this "
    "greeting feel fresh: vary your opening line, structure, jokes, and any "
    "emoji every single time so it never reads like a copy-pasted template. "
    "Do not include the member's name or any @mention in your reply — that "
    "will be added separately before your text. Output ONLY the greeting "
    "itself, with no quotes, labels, or extra commentary."
)

# Message splitting configuration
MAX_MESSAGE_LENGTH = 1900

# Maximum number of lines an AI chat reply is allowed to have before it
# gets shortened (re-prompted) or, as a last resort, hard-truncated.
MAX_REPLY_LINES = 5

# ── Per-user "core memory" system ────────────────────────────────────────
# See core_memory.py + ai_service.py's _build_session_history /
# _extract_core_memory. Elfy's conversation context is scoped per Discord
# USER (not per channel), so a person's chat with her stays a bounded
# size no matter how long they've been talking overall, and so people
# sharing a channel never see each other's conversation bleed together.

# How many raw exchange entries (user + model turns combined) to keep in
# a person's rolling "recent conversation" window. This — not total
# conversation length — is what actually gets re-sent to Gemini on every
# reply, so it's the main lever on per-reply latency/cost.
CORE_MEMORY_WINDOW_SIZE = 100

# Run the "what's actually worth remembering about this person?"
# distillation once every this-many text-chat messages from them.
CORE_MEMORY_EXTRACTION_INTERVAL = 20

# Max distilled facts kept per user before Elfy compresses/merges them
# (via Gemini) back down under this cap.
CORE_MEMORY_FACT_CAP = 100

# Discord bot configuration
BOT_PREFIX = []
BOT_ACTIVITY = "with your feelings"

# ── Bot owner override ──────────────────────────────────────────────────────
# Comma-separated Discord user ID(s) that can run every slash command
# regardless of server permissions (e.g. Manage Server). Set this in your
# .env / .env.development / Replit Secrets, e.g.:
#   OWNER_IDS=123456789012345678
# or for more than one owner: OWNER_IDS=123456789012345678,987654321098765432
_owner_ids_raw = os.getenv('OWNER_IDS', '')
OWNER_IDS: set = {
    int(piece) for piece in _owner_ids_raw.replace(' ', '').split(',') if piece.isdigit()
}


def is_owner(user_id: int) -> bool:
    """True if this Discord user ID is a configured bot owner."""
    return user_id in OWNER_IDS
