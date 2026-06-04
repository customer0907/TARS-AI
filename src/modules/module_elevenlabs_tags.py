"""
ElevenLabs v3 bracket audio tags — strip for console, keep for TTS.
"""

import re

# Any [english tag] up to 48 chars inside brackets (v3 style).
ELEVENLABS_TAG_BRACKET = re.compile(r'\s*\[[^\]]{1,48}\]\s*', re.IGNORECASE)


def strip_elevenlabs_tags(text: str) -> str:
    """Remove bracket audio tags from console/log display."""
    if not text:
        return text
    cleaned = ELEVENLABS_TAG_BRACKET.sub(' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()
