"""
AI service layer for interacting with Google's Generative AI API.
Handles chat session management, response generation, and image generation.

Image generation uses Pollinations.AI gen.pollinations.ai API with your API key.
"""

import asyncio
import base64
import re
import traceback
import urllib.parse
from functools import partial
from typing import Dict, List, Any, Optional, Tuple

import aiohttp
from google import genai
from google.genai import types

from settings import GOOGLE_AI_KEY, POLLINATIONS_API_KEY
import core_memory
import dashboard_settings
from storage import ChatDataManager, log_error

# Keywords that indicate a user wants an image generated
IMAGE_KEYWORDS = [
    "generate image", "generate a image", "generate an image",
    "create image", "create a image", "create an image",
    "make image", "make a image", "make an image",
    "draw image", "draw a image", "draw an image",
    "generate picture", "create picture", "make picture", "draw picture",
    "generate a picture", "create a picture", "make a picture", "draw a picture",
    "generate photo", "create photo", "make photo",
    "generate a photo", "create a photo", "make a photo",
    "show me a picture", "show me an image", "show me a photo",
]

# Keywords that indicate a user wants an existing image edited/restyled
# (as opposed to generating a brand new image from a text prompt only)
IMAGE_EDIT_KEYWORDS = [
    "turn this image", "turn this photo", "turn this picture",
    "transform this image", "transform this photo", "transform this picture",
    "convert this image", "convert this photo", "convert this picture",
    "make this image", "make this photo", "make this picture",
    "edit this image", "edit this photo", "edit this picture",
]

# Pollinations gen API — authenticated endpoint
# Model options: flux, zimage, gptimage, seedream5, nanobanana, nanobanana-pro,
# klein. See https://gen.pollinations.ai/image/models for the live list.
# enhance=true lets Pollinations apply its own internal prompt boosting on
# top of ours.
#
# IMPORTANT: this must be a genuine text-to-image model. "kontext" is
# Pollinations' image-EDITING model — it expects an existing reference
# image via an `image=` URL parameter and transforms it. generate_image()
# below never supplies one, so kontext was being asked to "edit" nothing,
# which is exactly why output was bland, mostly ignored the prompt, and
# only ever rendered one salient subject. flux is a proper from-scratch
# generator and follows multi-element prompts (e.g. "a cat catching a
# butterfly in a beautiful jungle") much more faithfully.
#
# NOTE: this had regressed back to "kontext" despite this exact comment
# already explaining why that's wrong (a previous fix apparently didn't
# make it into this checkout) — fixed back to "flux" here.
POLLINATIONS_IMAGE_URL = (
    "https://gen.pollinations.ai/image/{prompt}"
    "?model=flux&width=1024&height=1024&nologo=true&enhance=true"
)

CHAT_MODEL = "gemini-3.1-flash-lite"

# Gemini native image generation/editing model ("Nano Banana"). Supports
# text-to-image AND image+text-to-image (i.e. editing an uploaded photo).
IMAGE_EDIT_MODEL = "gemini-2.5-flash-image"

# Lightweight text model used to expand short/simple image prompts into
# detailed, descriptive ones before sending to Pollinations.
PROMPT_ENHANCER_MODEL = "gemini-3.1-flash-lite"

PROMPT_ENHANCER_INSTRUCTION = (
    "You are an expert prompt engineer for AI image generation (Flux model). "
    "Expand the user's short image request into a single, richly detailed "
    "image-generation prompt. Include: subject details, art style, lighting, "
    "color palette, composition/camera angle, mood, and texture/quality cues "
    "(e.g. 'highly detailed', '8k', 'cinematic lighting') where appropriate. "
    "Keep it under 75 words. Do not add commentary, explanations, quotes, or "
    "labels — output ONLY the final image prompt itself, nothing else."
)

# WELCOME_MESSAGE_INSTRUCTION now lives in settings.py (as the dashboard's
# default for the editable "welcome_instruction" setting) — imported below.

# Used by AIService._extract_core_memory (see core_memory.py for the
# storage side of this). Asks Gemini what's actually worth remembering
# long-term about ONE specific person, from a recent stretch of their
# conversation with Elfy.
MEMORY_EXTRACTION_INSTRUCTION = (
    "You are Elfy's long-term memory system, not Elfy herself. You'll be "
    "shown a recent stretch of conversation in one Discord channel, plus "
    "what's already remembered about ONE specific person in it. The "
    "transcript may include OTHER people talking to Elfy too — each line "
    "is tagged with a Discord mention showing who said it. Identify "
    "anything NEW and genuinely worth remembering long-term about THAT "
    "ONE NAMED PERSON specifically, based only on lines that are actually "
    "theirs — stable facts, not passing small talk, and never something "
    "someone else in the channel said about themselves. "
    "Good: their name/nickname, relationships, pets, job/school, ongoing "
    "situations, strong preferences, running jokes, things they "
    "explicitly asked to be remembered. "
    "Bad: routine greetings, one-off questions, anything already listed "
    "as known, anything that's more about Elfy (or about someone else in "
    "the channel) than about this specific person. "
    "Output each new fact on its own line, as a short plain statement "
    "under 15 words (e.g. 'Has a cat named Bean'). No bullets, no "
    "numbering, no extra commentary. If there's truly nothing new worth "
    "remembering about this specific person, output exactly: NONE"
)

# Used by AIService._consolidate_core_memory when one person's fact list
# has grown past the cap — compresses it back down instead of just
# dropping the oldest entries, so a durable fact doesn't get bumped out
# by a run of trivial recent ones.
MEMORY_CONSOLIDATION_INSTRUCTION = (
    "The list below of remembered facts about one Discord user has grown "
    "too long. Compress it to at most {cap} lines: merge overlapping or "
    "duplicate facts, drop the least useful or most trivial ones, and "
    "keep whichever are most important and durable. Each line should be "
    "a short plain statement under 15 words. Output ONLY the resulting "
    "fact lines, one per line — no bullets, no numbering, no commentary."
)


def _contains_keyword(text: str, keywords: List[str]) -> bool:
    """
    True if any keyword appears in text as a whole word/phrase — not as a
    substring of some unrelated longer word. Plain `kw in text` matching
    was firing on things like "paint" inside "repaint"/"painting" or
    "imagine" inside "imagining", which made ordinary conversation
    misfire into image generation.
    """
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", lower) for kw in keywords)


def is_image_request(text: str) -> bool:
    return _contains_keyword(text, IMAGE_KEYWORDS)


# Self-portrait detection: action word (create/make/show/send/...) +
# subject word (selfie/picture/pic/image/photo), optionally needing an
# explicit self-reference (you/yourself/elfy) depending on the subject.
#
# THIS IS DELIBERATELY INDEPENDENT OF is_image_request()/IMAGE_KEYWORDS.
# It used to be nested inside it (only checked once is_image_request()
# already returned True), which was the actual bug behind unreliable
# selfie generation: IMAGE_KEYWORDS has no "selfie" entry at all, and only
# recognizes "show" as the exact phrase "show me a [picture/image/photo]"
# — no bare "show", no "send" as an action word. So plain requests like
# "send a selfie" or "send an image of yourself" failed that outer gate
# and fell straight into normal chat, never reaching a self-portrait (or
# any image) check — Elfy's persona would then improvise a flirty
# non-answer rather than actually generate anything. It also explains why
# "create" and "show" behaved inconsistently: "create" had decent
# picture/image/photo coverage, "show" didn't.
_SELF_PORTRAIT_ACTION_WORDS = {"create", "make", "generate", "draw", "show", "send", "take", "give"}
_SELF_PORTRAIT_SUBJECT_WORDS = {"selfie", "picture", "pic", "image", "photo"}
_SELF_REFERENCE_WORDS = {"you", "yourself", "elfy"}
_ACTION_TO_SUBJECT_WORD_GAP = 6   # max words between action and subject
_SUBJECT_TO_REFERENCE_WORD_GAP = 4  # max words between subject and "you"/"elfy"/etc

# A handful of fixed phrasings that mean the same thing but don't fit the
# action+subject shape above (kept from the original pattern list).
_FIXED_SELF_PORTRAIT_PATTERNS = [
    r"\bwhat (?:do |does )?(?:you|elfy) look like\b",
    r"\byour(?:self)?'?s? (?:appearance|face|look|outfit)\b",
    r"\b(?:you|yourself|elfy)('?s)? (?:in|wearing|as|dressed)\b",
]


def is_self_portrait_request(text: str) -> bool:
    """True if this looks like a request for a picture of Elfy herself —
    e.g. "create a selfie", "show me a picture of you", "send an image of
    yourself at the beach". create/make/generate/draw/show/send/take/give
    are treated as pure synonyms here, paired with any of
    selfie/picture/pic/image/photo, with an optional trailing
    prompt/style modifier (the rest of the message is simply passed
    through as-is to generate_character_image() — no separate extraction
    needed, Gemini handles "at the beach" as scene direction fine).

    Two ways to match:
      1. action + "selfie" — a selfie is inherently a picture of
         yourself, no "of you" qualifier needed.
      2. action + (picture/pic/image/photo/selfie) + an explicit
         self-reference (you/yourself/elfy) nearby — needed for the other
         subject words, since "create a picture" alone doesn't say who
         it's a picture of.
    Falls back to a few fixed phrasings (see _FIXED_SELF_PORTRAIT_PATTERNS)
    that don't fit the action+subject shape, e.g. "what do you look like".
    """
    lower = text.lower()
    words = re.findall(r"[a-z']+", lower)

    action_positions = [i for i, w in enumerate(words) if w in _SELF_PORTRAIT_ACTION_WORDS]
    subject_positions = [i for i, w in enumerate(words) if w in _SELF_PORTRAIT_SUBJECT_WORDS]
    reference_positions = [i for i, w in enumerate(words) if w in _SELF_REFERENCE_WORDS]

    for a in action_positions:
        for s in subject_positions:
            if abs(s - a) > _ACTION_TO_SUBJECT_WORD_GAP:
                continue
            if words[s] == "selfie":
                return True
            if any(abs(r - s) <= _SUBJECT_TO_REFERENCE_WORD_GAP for r in reference_positions):
                return True

    return any(re.search(pattern, lower) for pattern in _FIXED_SELF_PORTRAIT_PATTERNS)


# Imperative-only phrasings for "change your nickname" — deliberately
# narrow (change/set/update ... to X, rename yourself (to) X) so it
# doesn't misfire on ordinary conversational uses of "your name", e.g.
# "your name is so pretty" or "your name suits you".
_NICKNAME_CHANGE_RE = re.compile(
    r"\b(?:change|set|update)\s+your\s+(?:nick)?name\s+to\s+(.+)"
    r"|\brename\s+yourself\s*(?:to\s+)?(.+)",
    re.IGNORECASE,
)


def is_nickname_change_request(text: str) -> Optional[str]:
    """If this looks like a request for Elfy to change her own server
    nickname (e.g. "change your name to Sparkling Angel"), return the
    requested nickname (trimmed, surrounding quotes stripped, capped at
    Discord's 32-character nickname limit). Returns None otherwise."""
    match = _NICKNAME_CHANGE_RE.search(text)
    if not match:
        return None
    raw = next(g for g in match.groups() if g)
    nickname = raw.strip().strip('"\u201c\u201d\'').strip()[:32].strip()
    return nickname if nickname else None


def is_image_edit_request(text: str, attachments: List[Dict[str, Any]]) -> bool:
    """True if the user attached an image and wants it transformed/restyled."""
    if not attachments:
        return False
    has_image_attachment = any(
        isinstance(a, dict) and str(a.get("mime_type", "")).startswith("image/")
        for a in attachments
    )
    if not has_image_attachment:
        return False
    return _contains_keyword(text, IMAGE_EDIT_KEYWORDS)


def _extract_own_spoken_text(text: str) -> str:
    """
    Isolate what the sender themselves actually said out of the full text
    generate_response() receives — which may also carry a prepended VIP
    note (see vip_users.py) and/or an appended "... while quoting <@id>
    "..."" reply-quote block (see message_handler.construct_query).

    Image-intent detection and image prompts should only ever be built
    from the sender's own words. Previously the whole combined string was
    used directly, so a keyword sitting in a *quoted* message from
    someone else could trigger image generation on an unrelated reply,
    and — even for a genuine request — the extracted prompt could run
    past the sender's own closing quote into the quoted block, dragging
    a raw <@id> mention and someone else's message into the prompt.

    Falls back to the original text unchanged if the expected 'said "'
    marker isn't present, so non-standard input is handled the same as
    before.
    """
    marker = 'said "'
    if marker not in text:
        return text

    start = text.index(marker) + len(marker)
    quoting_marker = '" while quoting '
    quoting_idx = text.find(quoting_marker, start)
    end = quoting_idx if quoting_idx != -1 else text.rfind('"')

    if end <= start:
        return text
    return text[start:end]


class AIService:
    """Manages interactions with Google's Generative AI API."""

    def __init__(self):
        self.client = genai.Client(api_key=GOOGLE_AI_KEY)
        self._text_config = self._build_text_config()

        # Conversation state is keyed by (guild_id, channel_id) — a
        # "channel scope", None for guild_id in DMs. This used to be keyed
        # by Discord USER ID alone, which fixed two real problems (the
        # ever-growing per-channel history that made replies get slower
        # over time, and multiple people sharing one channel sharing the
        # SAME Gemini history) at the cost of introducing a cross-server
        # data leak: the same Discord user chatting in two different
        # servers shared one history, so specifics from Server A —
        # including secrets or named individuals — could surface in
        # Server B. Channel scoping fixes the leak while keeping both of
        # the original fixes: each channel's rolling window is still
        # small and bounded (see CORE_MEMORY_WINDOW_SIZE in settings.py),
        # and multiple people chatting in the same channel now share that
        # channel's transcript on purpose (each turn is tagged with who
        # said it — see message_handler.construct_query — so Gemini can
        # still tell speakers apart within it).
        #
        # self._history holds ONLY the raw back-and-forth (no persona,
        # no memory notes baked in) — see _build_session_history for how
        # persona + core memory get layered on top fresh, every turn.
        # There is deliberately no long-lived "chat session" object
        # stored anywhere: a fresh one is created from bounded history
        # right before every send and then discarded (see
        # _send_message_sync) — chats.create() is a local, in-memory SDK
        # call, not a network request, so doing this every turn costs
        # nothing extra.
        self._history: Dict[Tuple[Optional[int], int], List[Dict[str, Any]]] = {}

        # Optional one-off custom persona set via /forget's `persona`
        # argument — just for that channel, until its next /forget. Moved
        # to channel scoping alongside self._history above, for the same
        # reason.
        self._custom_persona: Dict[Tuple[Optional[int], int], List[Dict[str, Any]]] = {}

        # Cache of each (guild, user) pair's current display name,
        # refreshed on every generate_response() call, so background
        # methods (memory extraction, session building) can reference
        # "Micky" by name without needing it threaded through every call.
        # Keyed by (guild_id, user_id) rather than just user_id since
        # Discord nicknames are per-server — the same person can display
        # differently in two servers, and core memory (which this feeds)
        # is now per-server too.
        self._display_names: Dict[Tuple[Optional[int], int], str] = {}

        # Recent welcome-message texts (most recent last), used so
        # generate_welcome_message() never sends the exact same greeting
        # twice in a row.
        self._recent_welcome_messages: List[str] = []

    @staticmethod
    def _build_text_config() -> Any:
        """Build the chat GenerateContentConfig from current dashboard
        settings (temperature/top_p/top_k/max_output_tokens + safety
        thresholds), read fresh so dashboard edits are picked up."""
        cfg = dashboard_settings.chat_generation_config()
        return types.GenerateContentConfig(
            temperature=cfg["temperature"],
            top_p=cfg["top_p"],
            top_k=cfg["top_k"],
            max_output_tokens=cfg["max_output_tokens"],
            safety_settings=dashboard_settings.safety_settings_list(),
        )

    def refresh_active_sessions(self) -> None:
        """
        Re-read dashboard settings (generation params + safety
        thresholds) into self._text_config so the NEXT message from
        anyone picks them up immediately, instead of waiting for a bot
        restart. Call this right after a dashboard settings save.

        There's nothing else to eagerly rebuild here: every user's live
        session is created fresh from bounded history right before each
        send (see _send_message_sync / _build_session_history), not
        cached long-term, so it always reflects whatever's current the
        moment it's built. That also means personality edits now apply
        immediately to every ongoing conversation, not just new ones —
        persona is injected fresh each turn rather than baked into old
        history, unlike before this per-user rework (and there's no
        longer a per-channel rebuild loop that a single corrupted
        history entry could interrupt for everyone else — see
        load_history for where that same safety property now lives).
        """
        self._text_config = self._build_text_config()

    @staticmethod
    def _normalize_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        chats.create(history=...) requires each part to be a dict (or Part)
        with a 'text' key — plain strings are rejected by the SDK's
        validator. This normalizes any {"role":..., "parts": ["str", ...]}
        entries into the dict form the SDK expects, regardless of whether
        the history came from BOT_TEMPLATE, saved storage, or elsewhere.
        """
        normalized = []
        for entry in history:
            parts = entry.get("parts", [])
            norm_parts = [
                {"text": p} if isinstance(p, str) else p
                for p in parts
            ]
            normalized.append({**entry, "parts": norm_parts})
        return normalized

    def load_history(self, history_data: Dict[Tuple[Optional[int], int], List[Dict[str, Any]]]) -> None:
        """
        Restore each channel's rolling conversation window from persisted
        storage at startup (history_data is keyed by (guild_id,
        channel_id), guild_id None for DMs).

        This is deliberately lightweight: it just populates
        self._history. There's no eager per-channel Gemini session
        created here — the live session for each channel is built fresh
        (persona + current core memory + this window) the next time
        someone actually sends a message in it, via
        _build_session_history / _send_message_sync. That keeps startup
        fast no matter how many channels have history.

        The one thing this does before trusting saved data is confirm
        it's still a shape the SDK will accept — via a throwaway, purely
        local chats.create() call (no network request, so this is cheap
        even for many channels). If a particular channel's saved history
        is corrupted or incompatible, only that channel resets to an
        empty window; everyone else loads normally (same "one bad entry
        can't take down everyone else" guarantee the old per-channel
        refresh_active_sessions loop used to provide).
        """
        window_size = dashboard_settings.get("core_memory_window_size")
        for scope, history in history_data.items():
            try:
                self.client.chats.create(
                    model=CHAT_MODEL,
                    history=self._normalize_history(history),
                    config=self._text_config,
                )
                self._history[scope] = list(history)[-window_size:]
            except Exception as e:
                print(
                    f"[load_history] Skipping channel scope {scope} — saved "
                    f"history incompatible (will start fresh): {e}"
                )
                self._history[scope] = []

    def _build_session_history(
        self,
        guild_id: Optional[int],
        channel_id: int,
        user_id: int,
    ) -> List[Dict[str, Any]]:
        """
        Assemble the full history used to seed THIS turn's live Gemini
        session: persona template (or this channel's one-off /forget
        persona override) + a freshly-built "what you remember about
        [the current speaker]" note (never persisted — rebuilt from
        current core memory every single turn, so it's always up to
        date) + this channel's bounded rolling window of actual recent
        exchanges.

        Two different scopes are deliberately in play here: the window
        itself is per-CHANNEL (guild_id, channel_id) — everyone talking
        to Elfy in this channel shares it, each turn tagged with who
        said it (see message_handler.construct_query) — while the memory
        note is about whoever is speaking THIS turn specifically, looked
        up per-(guild, user) so it can never include what was learned
        about them in a different server. This is what keeps per-reply
        cost bounded no matter how long a channel has been active, and
        what keeps Server A's facts about Micky from ever surfacing while
        replying to Micky in Server B, even though it's the same person.
        """
        channel_scope = (guild_id, channel_id)
        template = self._custom_persona.get(channel_scope) or dashboard_settings.build_bot_template()
        session_history = list(template)

        display_name = self._display_names.get((guild_id, user_id), "this user")
        memory_note = core_memory.format_for_prompt(guild_id, user_id, display_name)
        if memory_note:
            session_history.append({"role": "user", "parts": [{"text": memory_note}]})
            session_history.append({"role": "model", "parts": [{"text": "Got it, noted 💭"}]})

        session_history.extend(self._history.get(channel_scope, []))
        return session_history

    # ------------------------------------------------------------------
    # Welcome messages — on_member_join greeting
    # ------------------------------------------------------------------

    async def generate_welcome_message(self, member_name: str) -> str:
        """
        Generate a short (1-2 sentence), unique welcome greeting for a new
        member using Gemini. Retries a few times against an in-memory log
        of recent greetings so the bot never repeats itself verbatim;
        falls back to a varied default if generation keeps failing.

        Args:
            member_name: Display name of the member who just joined

        Returns:
            A short greeting string (caller adds the actual member.mention)
        """
        text = ""
        for _ in range(3):
            text = await self._call_welcome_gemini(member_name)
            normalized = text.strip()
            if normalized and normalized not in self._recent_welcome_messages:
                self._remember_welcome_message(normalized)
                return self._hard_truncate_lines(normalized, max_lines=2)

        # Every attempt either failed or collided with a recent greeting —
        # nudge it so it's never byte-for-byte identical to a prior one.
        fallback = text.strip() if text.strip() else "Welcome aboard! So glad you're here"
        unique_fallback = f"{fallback} 🎉" if not fallback.endswith("🎉") else f"{fallback} ✨"
        self._remember_welcome_message(unique_fallback)
        return self._hard_truncate_lines(unique_fallback, max_lines=2)

    def _remember_welcome_message(self, text: str) -> None:
        self._recent_welcome_messages.append(text)
        if len(self._recent_welcome_messages) > 50:
            self._recent_welcome_messages.pop(0)

    async def _call_welcome_gemini(self, member_name: str) -> str:
        def _call() -> Any:
            return self.client.models.generate_content(
                model=PROMPT_ENHANCER_MODEL,
                contents=[
                    dashboard_settings.get("welcome_instruction"),
                    f"New member's display name: {member_name}",
                ],
                config=types.GenerateContentConfig(
                    temperature=1.3,
                    top_p=0.97,
                    top_k=64,
                    max_output_tokens=120,
                ),
            )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)
            return (response.text or "").strip().strip('"')
        except Exception as e:
            print(f"[_call_welcome_gemini] Generation failed: {e}")
            return ""

    # ------------------------------------------------------------------
    # Image editing — Gemini native image model (image-in, image-out)
    # ------------------------------------------------------------------

    async def edit_image_with_attachment(
        self,
        prompt: str,
        attachments: List[Dict[str, Any]],
    ) -> Optional[bytes]:
        """
        Send an uploaded image + text instruction to Gemini's native image
        model and return the newly generated image bytes, or None on failure.
        """
        parts: List[Any] = []
        for a in attachments:
            if isinstance(a, dict) and str(a.get("mime_type", "")).startswith("image/"):
                parts.append(
                    types.Part.from_bytes(data=a["data"], mime_type=a["mime_type"])
                )
        parts.append(prompt)

        def _call() -> Any:
            return self.client.models.generate_content(
                model=IMAGE_EDIT_MODEL,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_modalities=["TEXT", "IMAGE"],
                    safety_settings=dashboard_settings.safety_settings_list(),
                ),
            )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)

            if not response or not response.candidates:
                return None

            for part in response.candidates[0].content.parts:
                if getattr(part, "inline_data", None) is not None:
                    return part.inline_data.data

            print("[edit_image_with_attachment] No inline image data in response")
            return None
        except Exception:
            log_error(
                text=prompt,
                error_traceback=traceback.format_exc(),
                history="N/A (image editing)",
                candidates="N/A",
                parts="N/A",
                prompt_feedbacks="N/A",
            )
            raise

    # ------------------------------------------------------------------
    # Image generation — Pollinations gen API (authenticated)
    # ------------------------------------------------------------------

    async def _enhance_image_prompt(self, prompt: str) -> str:
        """
        Expand a short/simple image prompt into a detailed one using Gemini,
        so Pollinations/Flux has more to work with. Falls back to the
        original prompt if enhancement fails for any reason.
        """
        img_cfg = dashboard_settings.image_generation_config()

        def _call() -> Any:
            return self.client.models.generate_content(
                model=PROMPT_ENHANCER_MODEL,
                contents=[PROMPT_ENHANCER_INSTRUCTION, f"User request: {prompt}"],
                config=types.GenerateContentConfig(
                    temperature=img_cfg["temperature"],
                    top_p=img_cfg["top_p"],
                    top_k=img_cfg["top_k"],
                    max_output_tokens=img_cfg["max_output_tokens"],
                ),
            )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)
            enhanced = (response.text or "").strip().strip('"')
            return enhanced if enhanced else prompt
        except Exception as e:
            print(f"[_enhance_image_prompt] Falling back to raw prompt: {e}")
            return prompt

    async def generate_image(self, prompt: str) -> Optional[bytes]:
        """
        Generate an image via Pollinations gen API using your API key.
        The prompt is first enriched by Gemini for better detail/quality.
        Returns raw image bytes or None on failure.
        """
        enhanced_prompt = await self._enhance_image_prompt(prompt)

        # Collapse whitespace/newlines and cap length — very long or
        # newline-containing prompts can trigger HTTP 400 once URL-encoded.
        cleaned_prompt = " ".join(enhanced_prompt.split())
        if len(cleaned_prompt) > 500:
            cleaned_prompt = cleaned_prompt[:500].rsplit(" ", 1)[0]

        encoded = urllib.parse.quote(cleaned_prompt)
        url = POLLINATIONS_IMAGE_URL.format(prompt=encoded)

        headers = {}
        if POLLINATIONS_API_KEY:
            headers["Authorization"] = f"Bearer {POLLINATIONS_API_KEY}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    else:
                        body = await resp.text()
                        print(
                            f"[generate_image] Pollinations returned HTTP {resp.status} "
                            f"— body: {body[:500]} — prompt length: {len(cleaned_prompt)} "
                            f"— url length: {len(url)}"
                        )
                        return None
        except Exception:
            log_error(
                text=prompt,
                error_traceback=traceback.format_exc(),
                history="N/A (image generation)",
                candidates="N/A",
                parts="N/A",
                prompt_feedbacks="N/A",
            )
            raise

    async def generate_character_image(self, prompt: str) -> Optional[bytes]:
        """
        Generate an image of Elfy herself, keeping her hairstyle/hair color/
        face consistent across every generation — only the outfit, pose,
        and setting should vary per request (see settings.py's
        ELFY_APPEARANCE_DESCRIPTION / dashboard "elfy_appearance" setting).

        Two layers, strongest first:
          1. Reference-image conditioning: reuse a cached portrait of Elfy
             (bootstrapped once below, and re-bootstrapped whenever the
             appearance text is edited on the dashboard — see
             storage.delete_elfy_reference_image) as an actual input image
             to Gemini's native image-edit model (the same one
             edit_image_with_attachment already uses for user uploads),
             asking it to keep the same character but change the
             outfit/scene. This gives real pixel-level consistency, not
             just prompt-text similarity.
          2. Prompt-only fallback: if there's no reference yet and
             bootstrapping one fails, or the edit call itself fails, fall
             back to the same Pollinations pipeline generate_image() uses,
             with the fixed appearance description prepended to the
             prompt. Never leaves the user with nothing just because the
             stronger path had a hiccup.
        """
        appearance = dashboard_settings.get("elfy_appearance")
        full_prompt = (
            f"{appearance} Now depict her: {prompt}. Keep her hairstyle, hair "
            "color, and face exactly as described above — only the outfit, "
            "pose, and setting should change."
        )

        reference_b64 = ChatDataManager.load_elfy_reference_image()
        if reference_b64 is None:
            reference_b64 = await self._bootstrap_reference_image(appearance)

        if reference_b64 is not None:
            try:
                reference_bytes = base64.b64decode(reference_b64)
                edited = await self.edit_image_with_attachment(
                    full_prompt,
                    [{"mime_type": "image/png", "data": reference_bytes}],
                )
                if edited is not None:
                    return edited
                print("[generate_character_image] Reference-image edit returned nothing, falling back")
            except Exception as e:
                print(f"[generate_character_image] Reference-image path failed, falling back: {e}")

        # Fallback: same Pollinations pipeline generate_image() uses, just
        # with the fixed appearance baked into the prompt text instead of
        # an actual reference image. Wrapped here (generate_image() itself
        # re-raises on failure) so a hiccup falls back to "no image" rather
        # than an unhandled exception.
        try:
            return await self.generate_image(full_prompt)
        except Exception as e:
            print(f"[generate_character_image] Fallback generation also failed: {e}")
            return None

    async def _bootstrap_reference_image(self, appearance: str) -> Optional[str]:
        """
        Generate and cache a neutral reference portrait of Elfy the first
        time one's needed, so future self-portrait requests can condition
        on an actual image instead of prompt text alone. Returns the
        base64-encoded image, or None if generation itself failed (in
        which case generate_character_image() falls back to prompt-only).
        """
        try:
            image_bytes = await self.generate_image(
                f"{appearance} A simple, friendly portrait, plain neutral "
                "background, casual everyday outfit."
            )
            if image_bytes is None:
                return None
            b64_data = base64.b64encode(image_bytes).decode("ascii")
            try:
                ChatDataManager.save_elfy_reference_image(b64_data)
            except Exception as e:
                # Storage rejected it (e.g. size limits) — still usable for
                # *this* request, just won't be cached for next time.
                print(f"[_bootstrap_reference_image] Couldn't cache reference image: {e}")
            return b64_data
        except Exception as e:
            print(f"[_bootstrap_reference_image] Failed to bootstrap reference image: {e}")
            return None

    # ------------------------------------------------------------------
    # Reply length enforcement — cap AI chat replies at MAX_REPLY_LINES
    # ------------------------------------------------------------------

    async def _enforce_reply_length(self, text: str) -> str:
        """
        Guarantee a chat reply is MAX_REPLY_LINES lines or fewer. First
        tries asking Gemini to rewrite it more concisely (preserves tone
        and meaning); if that still doesn't fit, hard-truncates as a
        guaranteed fallback so the cap is always honored before anything
        reaches Discord.

        Args:
            text: The raw model response text

        Returns:
            The same text if already within the limit, otherwise a
            shortened/truncated version
        """
        if not text:
            return text

        max_lines = dashboard_settings.get("max_reply_lines")
        if len(text.splitlines()) <= max_lines:
            return text

        shortened = await self._shorten_to_line_limit(text, max_lines)
        return self._hard_truncate_lines(shortened, max_lines)

    async def _shorten_to_line_limit(self, text: str, max_lines: int) -> str:
        """Ask Gemini to compress an over-long reply down to the line cap."""
        instruction = (
            f"Rewrite the following Discord chat message so it is "
            f"{max_lines} lines or fewer, keeping the same meaning, "
            "tone, and personality. Do not add commentary or explanations "
            "— output ONLY the rewritten message."
        )

        def _call() -> Any:
            return self.client.models.generate_content(
                model=PROMPT_ENHANCER_MODEL,
                contents=[instruction, f"Original message:\n{text}"],
                config=types.GenerateContentConfig(
                    temperature=0.6,
                    max_output_tokens=150,
                ),
            )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)
            shortened = (response.text or "").strip()
            return shortened if shortened else text
        except Exception as e:
            print(f"[_shorten_to_line_limit] Falling back to hard truncation: {e}")
            return text

    @staticmethod
    def _hard_truncate_lines(text: str, max_lines: Optional[int] = None) -> str:
        """Guaranteed, no-API-call fallback: keep only the first max_lines lines."""
        if max_lines is None:
            max_lines = dashboard_settings.get("max_reply_lines")
        lines = text.splitlines()
        if len(lines) <= max_lines:
            return text
        return "\n".join(lines[:max_lines]).rstrip()

    # ------------------------------------------------------------------
    # Text chat
    # ------------------------------------------------------------------

    def _send_message_sync(
        self,
        guild_id: Optional[int],
        channel_id: int,
        user_id: int,
        prompt_parts: List[Any],
    ) -> Any:
        # chats.send_message() accepts: a single str/File/Part, OR a list of
        # str/File/FileDict/Part/PartDict. It does NOT accept a types.Content
        # object directly (that raises "Message must be a valid part type").
        def _to_part(p: Any) -> Any:
            if isinstance(p, str):
                return p
            if isinstance(p, types.Part):
                return p
            if isinstance(p, dict):
                # Expect a dict like {"mime_type": "image/jpeg", "data": b"..."}
                mime_type = p.get("mime_type") or p.get("mimeType")
                data = p.get("data")
                if mime_type and data is not None:
                    return types.Part.from_bytes(data=data, mime_type=mime_type)
                raise ValueError(f"Unrecognized attachment dict shape: {list(p.keys())}")
            raise TypeError(f"Unsupported prompt part type: {type(p)}")

        if len(prompt_parts) == 1 and isinstance(prompt_parts[0], str):
            message = prompt_parts[0]
        else:
            message = [_to_part(p) for p in prompt_parts]

        # Build a session fresh from bounded history (persona + current
        # core-memory note about the current speaker + this channel's
        # trimmed rolling window) every single turn, then use it once and
        # let it go. chats.create() is local/in-memory — it doesn't call
        # the API — so this costs nothing extra over reusing a stored
        # session object, and it's what guarantees the prompt sent to
        # Gemini never grows no matter how long this channel has been
        # active in total.
        session_history = self._build_session_history(guild_id, channel_id, user_id)
        chat = self.client.chats.create(
            model=CHAT_MODEL,
            history=self._normalize_history(session_history),
            config=self._text_config,
        )
        response = chat.send_message(message)

        user_text = " ".join(
            p if isinstance(p, str) else "" for p in prompt_parts
        ).strip()
        channel_scope = (guild_id, channel_id)
        window = self._history.setdefault(channel_scope, [])
        if user_text:
            window.append({"role": "user", "parts": [user_text]})
        if response and response.text:
            window.append({"role": "model", "parts": [response.text]})

        # Trim immediately, not just at read time — this is what keeps
        # both the next prompt AND what gets persisted to storage
        # bounded, rather than only bounding what's sent to Gemini while
        # quietly letting storage grow forever.
        window_size = dashboard_settings.get("core_memory_window_size")
        if len(window) > window_size:
            del window[: len(window) - window_size]

        return response

    async def generate_response(
        self,
        guild_id: Optional[int],
        channel_id: int,
        user_id: int,
        attachments: List[Dict[str, Any]],
        text: str,
        display_name: str = "this user",
    ) -> Tuple[str, Optional[bytes], Optional[str]]:
        """Returns (reply_text, image_bytes, requested_nickname) — exactly
        one of image_bytes/requested_nickname is ever non-None, and
        message_handler.py branches on which. requested_nickname is a
        plain string signal rather than an executed action: this module
        deliberately never imports discord/touches Member objects, so the
        actual guild.me.edit(nick=...) call happens in message_handler.py,
        which has the live Discord objects — same separation of concerns
        as image generation, where this module only ever returns raw
        bytes and message_handler.py handles wrapping/sending them."""
        # Cache the display name so background helpers (memory-note
        # formatting, extraction prompts) can refer to this person by
        # name without needing it threaded through every method call.
        # Keyed by (guild, user) — see __init__ for why.
        self._display_names[(guild_id, user_id)] = display_name

        # Image-intent detection and image prompts must only ever be built
        # from what the sender themselves said — never a prepended VIP
        # note or an appended reply-quote from someone else's message.
        # See _extract_own_spoken_text's docstring for why.
        spoken_text = _extract_own_spoken_text(text)

        # ---- Image editing path (uploaded image + "turn this into..." etc) ----
        if is_image_edit_request(spoken_text, attachments):
            image_bytes = await self.edit_image_with_attachment(spoken_text, attachments)
            if image_bytes:
                return ("Here's your transformed image! 🎨", image_bytes, None)
            else:
                return (
                    "Sorry, I wasn't able to transform that image. "
                    "Please try a different prompt.",
                    None,
                    None,
                )

        # ---- Image generation path (text-to-image, no input image) ----
        # Self-portrait detection runs FIRST and independently of
        # is_image_request() — nesting it inside that check was the
        # actual routing bug (see is_self_portrait_request's docstring):
        # a plain "send a selfie" never satisfied the generic gate, so
        # the self-portrait check was never even reached.
        if is_self_portrait_request(spoken_text):
            image_bytes = await self.generate_character_image(spoken_text)
            if image_bytes:
                return ("Here's your generated image! 🎨", image_bytes, None)
            else:
                return (
                    "Sorry, I wasn't able to generate that image. "
                    "Please try a different prompt.",
                    None,
                    None,
                )
        if is_image_request(spoken_text):
            image_bytes = await self.generate_image(spoken_text)
            if image_bytes:
                return ("Here's your generated image! 🎨", image_bytes, None)
            else:
                return (
                    "Sorry, I wasn't able to generate that image. "
                    "Please try a different prompt.",
                    None,
                    None,
                )

        # ---- Nickname-change path ----
        requested_nickname = is_nickname_change_request(spoken_text)
        if requested_nickname is not None:
            return (
                f"On it — trying to change my name to {requested_nickname}...",
                None,
                requested_nickname,
            )

        # ---- Normal text chat path ----
        response = None
        channel_scope = (guild_id, channel_id)
        try:
            prompt_parts: List[Any] = attachments.copy()
            prompt_parts.append(text)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                partial(self._send_message_sync, guild_id, channel_id, user_id, prompt_parts),
            )

            raw_text = response.text if response else ""
            final_text = await self._enforce_reply_length(raw_text)

            # If we shortened it, keep the persisted/in-context history in
            # sync with what was actually sent — otherwise the model's own
            # memory of "what it said" would drift from what users saw.
            if final_text != raw_text and self._history.get(channel_scope):
                last_entry = self._history[channel_scope][-1]
                if last_entry.get('role') == 'model':
                    last_entry['parts'] = [final_text]

            # Background: every CORE_MEMORY_EXTRACTION_INTERVAL messages,
            # distill what's actually worth remembering long-term about
            # this specific person, in this specific server (see
            # core_memory.py). Scheduled as a fire-and-forget task — never
            # blocks or can fail this reply. The counter resets now
            # (before the task even runs) so a burst of fast messages
            # can't trigger it twice in a row.
            count = core_memory.bump_message_count(guild_id, user_id)
            if count >= dashboard_settings.get("core_memory_extraction_interval"):
                core_memory.reset_message_count(guild_id, user_id)
                asyncio.create_task(
                    self._extract_core_memory(guild_id, channel_id, user_id, display_name)
                )

            return (final_text, None, None)

        except Exception:
            try:
                history_info = str(self._history.get(channel_scope, []))
                candidates = str(response.candidates) if response else "N/A"
                parts_info = str(response.parts) if response else "N/A"
                prompt_feedbacks = str(response.prompt_feedbacks) if response else "N/A"
            except Exception:
                history_info = candidates = parts_info = prompt_feedbacks = "N/A"

            log_error(
                text=text,
                error_traceback=traceback.format_exc(),
                history=history_info,
                candidates=candidates,
                parts=parts_info,
                prompt_feedbacks=prompt_feedbacks,
            )
            raise

    def reset_channel_history(
        self,
        guild_id: Optional[int],
        channel_id: int,
        custom_template: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Clear this channel's rolling conversation window and, if given,
        set a one-off custom persona override just for this channel (used
        by /forget's optional `persona` argument) until its next /forget.
        Does NOT touch anyone's core memories — core memory is a
        per-person/per-server thing now, out of scope for a channel-level
        command. See core_memory.clear_all_for_user() for /forgetme's
        wipe, which is the user-scoped counterpart to this."""
        scope = (guild_id, channel_id)
        self._history[scope] = []
        if custom_template is not None:
            self._custom_persona[scope] = list(custom_template)
        else:
            self._custom_persona.pop(scope, None)

    def delete_channel_history(self, guild_id: Optional[int], channel_id: int) -> None:
        """Clear this channel's rolling conversation window and any
        one-off custom persona override. Does NOT touch core memory —
        see reset_channel_history's docstring."""
        scope = (guild_id, channel_id)
        self._history.pop(scope, None)
        self._custom_persona.pop(scope, None)

    def get_channel_history(self, guild_id: Optional[int], channel_id: int) -> List[Dict[str, Any]]:
        return self._history.get((guild_id, channel_id), [])

    # ------------------------------------------------------------------
    # Core memory extraction — the only place besides the main chat path
    # that talks to Gemini about a user's conversation (see core_memory.py
    # for the storage/formatting side of this).
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten_parts(parts: List[Any]) -> str:
        """Turn a history entry's parts list (each either a plain string
        or a {"text": ...} dict — see _normalize_history) into plain
        text, for feeding a conversation window into a Gemini prompt."""
        texts = []
        for p in parts:
            if isinstance(p, str):
                texts.append(p)
            elif isinstance(p, dict) and "text" in p:
                texts.append(p["text"])
        return " ".join(texts)

    async def _extract_core_memory(
        self,
        guild_id: Optional[int],
        channel_id: int,
        user_id: int,
        display_name: str,
    ) -> None:
        """
        Background task (see generate_response's asyncio.create_task
        call): ask Gemini what's actually worth remembering long-term
        about this specific person, in this specific server, from the
        channel's current rolling window — which may now include OTHER
        people's messages too, since the window is channel-scoped rather
        than user-scoped (see MEMORY_EXTRACTION_INSTRUCTION, which is
        written to pick this one person's lines out of a shared
        transcript). Merges any new facts into core memory under this
        (guild, user) scope, and consolidates if that pushes it over the
        cap. Runs fire-and-forget — any failure here is logged and
        swallowed, never surfaced to the user or allowed to affect their
        actual reply.
        """
        try:
            recent_turns = self._history.get((guild_id, channel_id), [])
            if not recent_turns:
                return

            conversation_blob = "\n".join(
                f"{turn.get('role', 'user')}: {self._flatten_parts(turn.get('parts', []))}"
                for turn in recent_turns
            )
            existing_facts = core_memory.get_facts(guild_id, user_id)
            known_block = (
                f"Already known: {'; '.join(existing_facts)}"
                if existing_facts else "Already known: nothing yet"
            )

            def _call() -> Any:
                return self.client.models.generate_content(
                    model=PROMPT_ENHANCER_MODEL,
                    contents=[
                        MEMORY_EXTRACTION_INSTRUCTION,
                        known_block,
                        (
                            f"Extract facts about: {display_name}\n"
                            f"Recent channel conversation (may include other people too):\n"
                            f"{conversation_blob}"
                        ),
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.3,
                        max_output_tokens=250,
                    ),
                )

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)
            new_facts = core_memory.parse_fact_lines(response.text or "")
            if not new_facts:
                return

            cap = dashboard_settings.get("core_memory_fact_cap")
            over_cap = core_memory.merge_new_facts(guild_id, user_id, new_facts, cap=cap)
            if over_cap:
                await self._consolidate_core_memory(guild_id, user_id, cap)

        except Exception as e:
            print(f"[core_memory extraction] Failed for user {user_id} in guild {guild_id}: {e}")

    async def _consolidate_core_memory(self, guild_id: Optional[int], user_id: int, cap: int) -> None:
        """
        Compress one (guild, user) scope's fact list back down to `cap`
        entries via Gemini (merging overlaps, dropping trivia) rather
        than blindly dropping the oldest ones. Falls back to keeping just
        the most recent `cap` facts if the consolidation call itself
        fails, so a long list never gets stuck permanently over the cap.
        """
        facts = core_memory.get_facts(guild_id, user_id)
        instruction = MEMORY_CONSOLIDATION_INSTRUCTION.format(cap=cap)

        def _call() -> Any:
            return self.client.models.generate_content(
                model=PROMPT_ENHANCER_MODEL,
                contents=[instruction, "\n".join(f"- {f}" for f in facts)],
                config=types.GenerateContentConfig(
                    temperature=0.3,
                    max_output_tokens=400,
                ),
            )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, _call)
            consolidated = core_memory.parse_fact_lines(response.text or "")
            core_memory.replace_facts(
                guild_id, user_id, consolidated[:cap] if consolidated else facts[-cap:]
            )
        except Exception as e:
            print(
                f"[core_memory consolidation] Failed for user {user_id} in "
                f"guild {guild_id}, keeping most recent {cap}: {e}"
            )
            core_memory.replace_facts(guild_id, user_id, facts[-cap:])
