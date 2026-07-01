import io

import httpx

from modules.module_config import load_config
from modules.module_messageQue import queue_message


CONFIG = load_config()


async def synthesize_fishaudio(text):
    """
    Synthesize text using Fish Audio and return an in-memory audio buffer.
    """
    api_key = CONFIG["TTS"]["fishaudio_api_key"]
    reference_id = CONFIG["TTS"]["fishaudio_reference_id"]
    model = CONFIG["TTS"]["fishaudio_model"]
    audio_format = CONFIG["TTS"]["fishaudio_format"]

    if not api_key:
        queue_message("ERROR: Fish Audio API key is required for Fish Audio TTS")
        return None
    if not reference_id:
        queue_message("ERROR: Fish Audio reference_id is required for Fish Audio TTS")
        return None

    body = {
        "text": text,
        "reference_id": reference_id,
        "format": audio_format,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "model": model,
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.fish.audio/v1/tts",
                headers=headers,
                json=body,
            )
            response.raise_for_status()

        if not response.content:
            queue_message("ERROR: Fish Audio returned an empty response")
            return None

        audio_buffer = io.BytesIO(response.content)
        audio_buffer.seek(0)
        return audio_buffer

    except httpx.HTTPStatusError as e:
        response_text = e.response.text[:500] if e.response is not None else ""
        queue_message(
            f"ERROR: Fish Audio TTS failed with status {e.response.status_code}: {response_text}"
        )
        return None
    except Exception as e:
        queue_message(f"ERROR: Fish Audio TTS synthesis failed: {e}")
        return None


async def text_to_speech_with_pipelining_fishaudio(text):
    """
    Converts text to speech using Fish Audio.

    Yields:
    - BytesIO: Processed audio buffer.
    """
    text = text.strip()
    if not text:
        return

    audio_buffer = await synthesize_fishaudio(text)
    if audio_buffer:
        yield audio_buffer
