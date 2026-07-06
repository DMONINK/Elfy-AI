"""
AI service layer for interacting with Google's Generative AI API.
Handles chat session management, response generation, and image generation.

Image generation uses Pollinations.AI gen.pollinations.ai API with your API key.
"""

import asyncio
import re
import time
import traceback
import urllib.parse
from functools import partial
from typing import Dict, List, Any, Optional, Tuple

import aiohttp
from google import genai
from google.genai import types

from settings import GOOGLE_AI_KEY, POLLINATIONS_API_KEY
import dashboard_settings
from storage import log_error

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
# IMPORTANT: this must be a genuine text-to-image model. "kontext" (the
# previous value here) is Pollinations' image-EDITING model — it expects
# an existing reference image via an `image=` URL parameter and transforms
# it. generate_image() below never supplies one, so kontext was being
# asked to "edit" nothing, which is exactly why output was bland, mostly
# ignored the prompt, and only ever rendered one salient subject. flux is
# a proper from-scratch generator and follows multi-element prompts (e.g.
# "a cat catching a butterfly in a beautiful jungle") much more faithfully.
POLLINATIONS_IMAGE_URL = (
    "https://gen.pollinations.ai/image/{prompt}"
    "?model=kontext&width=1024&height=1024&nologo=true&enhance=true"
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

        self._chats: Dict[int, Any] = {}
        self._history: Dict[int, List[Dict[str, Any]]] = {}
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
        Re-apply current dashboard settings (generation params + safety
        thresholds) to every channel with an active chat session, without
        losing any conversation so far. Call this right after a dashboard
        settings save so changes take effect immediately instead of
        waiting for the next bot restart.

        Personality changes are the one exception: BOT_TEMPLATE is only
        the *opening* turn of a conversation, already baked into each
        channel's history, so editing it only affects brand-new
        conversations (or ones reset with /forget) — same as before the
        dashboard existed, just now the "code" you'd edit is a text box.
        """
        self._text_config = self._build_text_config()
        for channel_id, history in list(self._history.items()):
            self._make_chat(channel_id, history)

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

    def _make_chat(self, channel_id: int, history: List[Dict[str, Any]]) -> None:
        safe_history = self._normalize_history(history)
        self._chats[channel_id] = self.client.chats.create(
            model=CHAT_MODEL,
            history=safe_history,
            config=self._text_config,
        )
        self._history[channel_id] = list(history)

    def load_history(self, history_data: Dict[int, List[Dict[str, Any]]]) -> None:
        for channel_id, history in history_data.items():
            try:
                self._make_chat(channel_id, history)
            except Exception as e:
                print(
                    f"[load_history] Skipping channel {channel_id} — "
                    f"saved history incompatible (will start fresh): {e}"
                )
                self._make_chat(channel_id, dashboard_settings.build_bot_template())

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

    def _send_message_sync(self, channel_id: int, prompt_parts: List[Any]) -> Any:
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

        response = self._chats[channel_id].send_message(message)

        user_text = " ".join(
            p if isinstance(p, str) else "" for p in prompt_parts
        ).strip()
        if user_text:
            self._history[channel_id].append(
                {"role": "user", "parts": [user_text]}
            )
        if response and response.text:
            self._history[channel_id].append(
                {"role": "model", "parts": [response.text]}
            )
        return response

    async def generate_response(
        self,
        channel_id: int,
        attachments: List[Dict[str, Any]],
        text: str,
    ) -> Tuple[str, Optional[bytes]]:
        # Image-intent detection and image prompts must only ever be built
        # from what the sender themselves said — never a prepended VIP
        # note or an appended reply-quote from someone else's message.
        # See _extract_own_spoken_text's docstring for why.
        spoken_text = _extract_own_spoken_text(text)

        # ---- Image editing path (uploaded image + "turn this into..." etc) ----
        if is_image_edit_request(spoken_text, attachments):
            image_bytes = await self.edit_image_with_attachment(spoken_text, attachments)
            if image_bytes:
                return ("Here's your transformed image! 🎨", image_bytes)
            else:
                return (
                    "Sorry, I wasn't able to transform that image. "
                    "Please try a different prompt.",
                    None,
                )

        # ---- Image generation path (text-to-image, no input image) ----
        if is_image_request(spoken_text):
            image_bytes = await self.generate_image(spoken_text)
            if image_bytes:
                return ("Here's your generated image! 🎨", image_bytes)
            else:
                return (
                    "Sorry, I wasn't able to generate that image. "
                    "Please try a different prompt.",
                    None,
                )

        # ---- Normal text chat path ----
        response = None
        try:
            prompt_parts: List[Any] = attachments.copy()
            prompt_parts.append(text)

            if channel_id not in self._chats:
                self._make_chat(channel_id, dashboard_settings.build_bot_template())

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                partial(self._send_message_sync, channel_id, prompt_parts),
            )

            raw_text = response.text if response else ""
            final_text = await self._enforce_reply_length(raw_text)

            # If we shortened it, keep the persisted/in-context history in
            # sync with what was actually sent — otherwise the model's own
            # memory of "what it said" would drift from what users saw.
            if final_text != raw_text and self._history.get(channel_id):
                last_entry = self._history[channel_id][-1]
                if last_entry.get('role') == 'model':
                    last_entry['parts'] = [final_text]

            return (final_text, None)

        except Exception:
            try:
                history_info = str(self._history.get(channel_id, []))
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
        channel_id: int,
        custom_template: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if custom_template is None:
            custom_template = dashboard_settings.build_bot_template()
        self._make_chat(channel_id, list(custom_template))

    def delete_channel_history(self, channel_id: int) -> None:
        self._chats.pop(channel_id, None)
        self._history.pop(channel_id, None)

    def get_history(self, channel_id: int) -> List[Dict[str, Any]]:
        return self._history.get(channel_id, [])
