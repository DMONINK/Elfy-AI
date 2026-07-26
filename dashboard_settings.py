from typing import Any, Dict, List

import settings as _defaults
from storage import ChatDataManager

SAFETY_THRESHOLDS = [
    "BLOCK_NONE",
    "BLOCK_ONLY_HIGH",
    "BLOCK_MEDIUM_AND_ABOVE",
    "BLOCK_LOW_AND_ABOVE",
]

_safety_defaults = {s["category"]: s["threshold"] for s in _defaults.SAFETY_SETTINGS}

DEFAULTS: Dict[str, Any] = {
    "bot_activity": _defaults.BOT_ACTIVITY,
    "bot_personality": _defaults.BOT_TEMPLATE[0]["parts"][0]["text"],
    "welcome_instruction": _defaults.WELCOME_MESSAGE_INSTRUCTION,
    "elfy_appearance": _defaults.ELFY_APPEARANCE_DESCRIPTION,
    "chat_temperature": _defaults.TEXT_GENERATION_CONFIG.get("temperature", 0.9),
    "chat_top_p": _defaults.TEXT_GENERATION_CONFIG.get("top_p", 1),
    "chat_top_k": _defaults.TEXT_GENERATION_CONFIG.get("top_k", 1),
    "chat_max_output_tokens": _defaults.TEXT_GENERATION_CONFIG.get("max_output_tokens", 500),
    "image_temperature": _defaults.IMAGE_GENERATION_CONFIG.get("temperature", 0.9),
    "image_top_p": _defaults.IMAGE_GENERATION_CONFIG.get("top_p", 0.5),
    "image_top_k": _defaults.IMAGE_GENERATION_CONFIG.get("top_k", 40),
    "image_max_output_tokens": _defaults.IMAGE_GENERATION_CONFIG.get("max_output_tokens", 300),
    "max_reply_lines": _defaults.MAX_REPLY_LINES,
    "max_message_length": _defaults.MAX_MESSAGE_LENGTH,
    "owner_ids": ",".join(str(i) for i in sorted(_defaults.OWNER_IDS)),
    "safety_harassment": _safety_defaults.get("HARM_CATEGORY_HARASSMENT", "BLOCK_MEDIUM_AND_ABOVE"),
    "safety_hate_speech": _safety_defaults.get("HARM_CATEGORY_HATE_SPEECH", "BLOCK_MEDIUM_AND_ABOVE"),
    "safety_sexually_explicit": _safety_defaults.get("HARM_CATEGORY_SEXUALLY_EXPLICIT", "BLOCK_MEDIUM_AND_ABOVE"),
    "safety_dangerous_content": _safety_defaults.get("HARM_CATEGORY_DANGEROUS_CONTENT", "BLOCK_MEDIUM_AND_ABOVE"),
    "core_memory_window_size": _defaults.CORE_MEMORY_WINDOW_SIZE,
    "core_memory_extraction_interval": _defaults.CORE_MEMORY_EXTRACTION_INTERVAL,
    "core_memory_fact_cap": _defaults.CORE_MEMORY_FACT_CAP,
}

_FIELD_TYPES = {
    "chat_temperature": float,
    "chat_top_p": float,
    "chat_top_k": int,
    "chat_max_output_tokens": int,
    "image_temperature": float,
    "image_top_p": float,
    "image_top_k": int,
    "image_max_output_tokens": int,
    "max_reply_lines": int,
    "max_message_length": int,
    "core_memory_window_size": int,
    "core_memory_extraction_interval": int,
    "core_memory_fact_cap": int,
}

_FIELD_BOUNDS = {
    "chat_temperature": (0.0, 2.0),
    "chat_top_p": (0.0, 1.0),
    "chat_top_k": (1, 100),
    "chat_max_output_tokens": (1, 8192),
    "image_temperature": (0.0, 2.0),
    "image_top_p": (0.0, 1.0),
    "image_top_k": (1, 100),
    "image_max_output_tokens": (1, 8192),
    "max_reply_lines": (1, 20),
    "max_message_length": (500, 2000),
    "core_memory_window_size": (4, 40),
    "core_memory_extraction_interval": (5, 50),
    "core_memory_fact_cap": (5, 100),
}

_cache: Dict[str, Any] = None  # type: ignore  # populated lazily from storage


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is None:
        stored = ChatDataManager.load_settings()
        _cache = {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}
    return _cache


def get(key: str) -> Any:
    return _load().get(key, DEFAULTS.get(key))


def get_all() -> Dict[str, Any]:
    return dict(_load())


def coerce(key: str, raw_value: Any) -> Any:
    if key.startswith("safety_"):
        value = str(raw_value).strip()
        if value not in SAFETY_THRESHOLDS:
            raise ValueError(f"{value!r} is not a recognized safety threshold")
        return value

    caster = _FIELD_TYPES.get(key)
    if caster is None:
        return str(raw_value).strip()
    value = caster(str(raw_value).strip())
    bounds = _FIELD_BOUNDS.get(key)
    if bounds:
        value = max(bounds[0], min(bounds[1], value))
    return value


def update(changes: Dict[str, Any]) -> None:
    current = _load()
    current.update({k: v for k, v in changes.items() if k in DEFAULTS})
    ChatDataManager.save_settings({k: current[k] for k in DEFAULTS})


def reset_to_defaults() -> None:
    global _cache
    _cache = dict(DEFAULTS)
    ChatDataManager.save_settings({})


def build_bot_template() -> List[Dict[str, Any]]:
    return [
        {"role": "user", "parts": [{"text": get("bot_personality")}]},
        {
            "role": "model",
            "parts": [{"text": "Got it — that's who I am. Ready to chat 💬"}],
        },
    ]


def chat_generation_config() -> Dict[str, Any]:
    return {
        "temperature": get("chat_temperature"),
        "top_p": get("chat_top_p"),
        "top_k": get("chat_top_k"),
        "max_output_tokens": get("chat_max_output_tokens"),
    }


def image_generation_config() -> Dict[str, Any]:
    return {
        "temperature": get("image_temperature"),
        "top_p": get("image_top_p"),
        "top_k": get("image_top_k"),
        "max_output_tokens": get("image_max_output_tokens"),
    }


def safety_settings_list() -> List[Dict[str, str]]:
    mapping = {
        "HARM_CATEGORY_HARASSMENT": get("safety_harassment"),
        "HARM_CATEGORY_HATE_SPEECH": get("safety_hate_speech"),
        "HARM_CATEGORY_SEXUALLY_EXPLICIT": get("safety_sexually_explicit"),
        "HARM_CATEGORY_DANGEROUS_CONTENT": get("safety_dangerous_content"),
    }
    return [{"category": cat, "threshold": thr} for cat, thr in mapping.items()]


def owner_ids() -> set:
    raw = get("owner_ids") or ""
    return {int(piece) for piece in raw.replace(" ", "").split(",") if piece.isdigit()}
