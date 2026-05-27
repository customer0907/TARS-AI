"""
module_melotts.py

Korean TTS module for TARS-AI using MeloTTS-Korean (myshell-ai).

This is a drop-in alternative to module_piper.py for Korean output. It exposes
the same async-generator-yielding-BytesIO-wavs interface so module_tts.py can
treat it identically to piper.

Activate by setting in `config.ini`:
    [TTS]
    ttsoption = melotts_ko

Install (first time):
    pip install git+https://github.com/myshell-ai/MeloTTS.git
    pip install g2pkk python-mecab-ko
    python -m unidic download

The first call to the engine downloads ~250 MB of pretrained weights from
HuggingFace (`myshell-ai/MeloTTS-Korean`) to ~/.cache/huggingface.
"""

import asyncio
import os
import re
import tempfile
import wave
from io import BytesIO

import numpy as np

from modules.module_config import load_config
from modules.module_messageQue import queue_message

CONFIG = load_config()

# Loaded lazily inside _ensure_loaded()
_VOICE = None
_SPEAKER_ID = None
_SAMPLE_RATE = None


def _ensure_loaded():
    """Load MeloTTS Korean on first use. Subsequent calls are no-ops."""
    global _VOICE, _SPEAKER_ID, _SAMPLE_RATE
    if _VOICE is not None:
        return
    try:
        # Imported here so that this module is importable even when MeloTTS
        # is not installed (other TTS engines still work).
        from melo.api import TTS

        queue_message(
            "INFO: Loading MeloTTS Korean (first run downloads ~250MB, can take a few minutes)..."
        )
        _VOICE = TTS(language="KR", device="cpu")
        spk2id = _VOICE.hps.data.spk2id
        # pretrained Korean has a single speaker, typically keyed "KR".
        _SPEAKER_ID = next(iter(spk2id.values()))
        _SAMPLE_RATE = int(_VOICE.hps.data.sampling_rate)
        queue_message(
            f"INFO: MeloTTS Korean loaded (sr={_SAMPLE_RATE}, speakers={list(spk2id)})"
        )
    except Exception as e:
        queue_message(f"ERROR: failed to load MeloTTS Korean: {e}")
        _VOICE = None


# Pre-load only when this engine is the configured one.
if CONFIG.get("TTS", {}).get("ttsoption", "").lower() == "melotts_ko":
    _ensure_loaded()


# Split into reasonable utterance chunks at sentence boundaries so the
# downstream player can begin playback before the whole reply is synthesized.
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s*")


def _split_for_pipelining(text: str) -> list:
    """Sentence-level split that works for Korean punctuation."""
    parts = [p.strip() for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]
    # If no punctuation, fall back to the whole text as one chunk.
    return parts if parts else [text.strip()]


def _synth_one(text: str) -> BytesIO:
    """
    Synthesize one chunk of Korean text -> WAV bytes in BytesIO.

    MeloTTS's `tts_to_file` writes a proper WAV file (with header & sample rate);
    we read it back into memory and delete the temp file. This keeps the
    downstream consumer (which uses `soundfile.read`) trivially compatible.
    """
    if _VOICE is None:
        _ensure_loaded()
    if _VOICE is None:
        raise RuntimeError("MeloTTS Korean is not available — check pip install")

    fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="melotts_ko_")
    os.close(fd)
    try:
        _VOICE.tts_to_file(
            text,
            _SPEAKER_ID,
            output_path=tmp_path,
            speed=1.0,
        )
        with open(tmp_path, "rb") as f:
            return BytesIO(f.read())
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def text_to_speech_with_pipelining_melotts(text):
    """
    Async generator yielding WAV BytesIO buffers, one per sentence.

    Mirrors the signature of `text_to_speech_with_pipelining_piper` in
    `module_piper.py`, so `module_tts.py` can swap them transparently.
    """
    text = (text or "").strip()
    if not text:
        return

    loop = asyncio.get_event_loop()
    for chunk in _split_for_pipelining(text):
        try:
            wav_buffer = await loop.run_in_executor(None, _synth_one, chunk)
            yield wav_buffer
        except Exception as e:
            queue_message(f"ERROR: MeloTTS Korean synth failed for {chunk[:40]!r}: {e}")
