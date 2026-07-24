"""Voice: speech-to-text (transcribe) and text-to-speech (speak).

Uses OpenAI. Whisper handles mixed Hindi/English voice notes; TTS returns Ogg/Opus
audio which Telegram plays as a native voice message.
"""
from __future__ import annotations

import io

from openai import OpenAI

import config

_client = OpenAI(api_key=config.OPENAI_API_KEY)

# Cap TTS input so a very long reply doesn't make a huge/expensive audio clip.
_TTS_MAX_CHARS = 1200


def transcribe(audio_bytes: bytes, filename: str = "voice.oga") -> str:
    """Turn a voice recording into text (biased to English/Hindi, not Urdu)."""
    f = io.BytesIO(audio_bytes)
    f.name = filename                     # the SDK infers the format from the name
    resp = _client.audio.transcriptions.create(
        model=config.STT_MODEL, file=f,
        language=config.STT_LANGUAGE or None,
        # Context hint nudges Whisper toward Hindi/English spelling over Urdu.
        prompt="This is a casual voice note in English and Hindi (Hinglish).",
    )
    return (resp.text or "").strip()


def speak(text: str) -> bytes:
    """Turn text into spoken Ogg/Opus audio (bytes) for a Telegram voice note."""
    clip = text[:_TTS_MAX_CHARS]
    resp = _client.audio.speech.create(
        model=config.TTS_MODEL, voice=config.TTS_VOICE,
        input=clip, response_format="opus",
    )
    return resp.content
