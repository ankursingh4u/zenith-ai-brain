"""One place that builds the LLM client.

The bot talks to any OpenAI-compatible endpoint (OpenAI itself, or a gateway
like OmniRoute) via LLM_BASE_URL + LLM_API_KEY. Speech is deliberately NOT
routed here — transcription/TTS stay on OpenAI, because a gateway may not serve
whisper/tts under the same names.
"""
from __future__ import annotations

from openai import OpenAI

import config

_client: OpenAI | None = None


def client() -> OpenAI:
    """The shared chat/vision client, built once."""
    global _client
    if _client is None:
        kwargs = {"api_key": config.LLM_API_KEY}
        if config.LLM_BASE_URL:
            kwargs["base_url"] = config.LLM_BASE_URL
        _client = OpenAI(**kwargs)
    return _client


def reset() -> None:
    """Forget the cached client (after config changes)."""
    global _client
    _client = None
