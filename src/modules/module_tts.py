"""
module_tts.py

Text-to-Speech (TTS) module for TARS-AI application.

Handles TTS functionality to convert text into audio using:
- Azure Speech SDK
- Local tools (e.g., espeak-ng)
- Server-based TTS systems

"""

# === Standard Libraries ===
import re
import requests
import os 
from datetime import datetime
import numpy as np
import sounddevice as sd
import soundfile as sf
from io import BytesIO
import asyncio
import librosa

from modules.module_piper import text_to_speech_with_pipelining_piper
from modules.module_silero import text_to_speech_with_pipelining_silero
from modules.module_espeak import text_to_speech_with_pipelining_espeak
from modules.module_alltalk import text_to_speech_with_pipelining_alltalk
from modules.module_elevenlabs import text_to_speech_with_pipelining_elevenlabs
from modules.module_azure import text_to_speech_with_pipelining_azure
# Korean MeloTTS is optional — imported lazily so absent installs don't break other engines.
try:
    from modules.module_melotts import text_to_speech_with_pipelining_melotts
except Exception:
    text_to_speech_with_pipelining_melotts = None
from modules.module_messageQue import queue_message

# ElevenLabs v3 audio tags — stripped for console, kept for TTS synthesis.
ELEVENLABS_AUDIO_TAGS = re.compile(
    r'\s*\[(?:laughs harder|laughs|giggle|cackle|evil laugh|sighs|whispers|softly|'
    r'surprised|excited|curious|sarcastic|teasingly|mocking|smug|condescending)\]\s*',
    re.IGNORECASE,
)

_last_ko_eleven_tag = None


def polish_ko_reply(text: str) -> str:
    """Spoken-style cleanup for Korean voice replies (after LLM, before TTS)."""
    global _last_ko_eleven_tag
    if not text:
        return text

    stripped = text.strip()
    tag_match = re.match(r'^\[([^\]]+)\]\s*', stripped, re.IGNORECASE)
    if tag_match:
        tag = tag_match.group(1).lower()
        if tag == 'teasingly' and _last_ko_eleven_tag == 'teasingly':
            stripped = re.sub(r'^\[[^\]]+\]\s*', '', stripped, count=1, flags=re.IGNORECASE)
            _last_ko_eleven_tag = None
        else:
            _last_ko_eleven_tag = tag
    else:
        _last_ko_eleven_tag = None

    # Casual speech: sentence-final periods -> tilde (keep ? ! and decimals)
    stripped = re.sub(r'(?<![0-9])\.(?=\s|$)', '~', stripped)
    stripped = re.sub(r'~{2,}', '~', stripped)
    stripped = re.sub(r'\s+', ' ', stripped).strip()
    return stripped


def strip_elevenlabs_tags(text: str) -> str:
    """Remove ElevenLabs audio tags from text shown to the user."""
    if not text:
        return text
    cleaned = ELEVENLABS_AUDIO_TAGS.sub(' ', text)
    return re.sub(r'\s+', ' ', cleaned).strip()


_HOUR_KO = (None, '한', '두', '세', '네', '다섯', '여섯', '일곱', '여덟', '아홉', '열', '열한', '열두')
_SINO = ('', '일', '이', '삼', '사', '오', '육', '칠', '팔', '구')


def _ko_sino_number(value: int) -> str:
    if value == 0:
        return '영'
    if value < 10:
        return _SINO[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        text = '십' if tens == 1 else _SINO[tens] + '십'
        if ones:
            text += _SINO[ones]
        return text
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        text = _SINO[hundreds] + '백' if hundreds > 1 else '백'
        if rest:
            text += _ko_sino_number(rest)
        return text
    return str(value)


def _ko_minute(minute: int) -> str:
    if minute == 0:
        return ''
    return _ko_sino_number(minute) + '분'


def _ko_spoken_time(hour: int, minute: int) -> str:
    period = '오전' if hour < 12 else '오후'
    hour12 = hour % 12 or 12
    spoken = f"{period} {_HOUR_KO[hour12]}시"
    if minute:
        spoken = f"{spoken} {_ko_minute(minute)}"
    return spoken


def normalize_for_korean_tts(text: str) -> str:
    """Convert numeric time/temp/percent in display text to spoken Korean for TTS only."""
    if not text:
        return text

    def replace_time_hm(match):
        return _ko_spoken_time(int(match.group(1)), int(match.group(2)))

    def replace_time_colon(match):
        return _ko_spoken_time(int(match.group(1)), int(match.group(2)))

    def replace_hour_only(match):
        return _ko_spoken_time(int(match.group(1)), 0)

    text = re.sub(r'(\d{1,2})시\s*(\d{1,2})분', replace_time_hm, text)
    text = re.sub(r'(?<!\d)(\d{1,2}):(\d{2})(?!\d)', replace_time_colon, text)
    text = re.sub(r'(\d{1,2})시(?![\d분])', replace_hour_only, text)
    text = re.sub(r'(\d+)도\b', lambda m: _ko_sino_number(int(m.group(1))) + '도', text)
    text = re.sub(r'(\d+)\s*%', lambda m: _ko_sino_number(int(m.group(1))) + '퍼센트', text)
    return text

def update_tts_settings(ttsurl):
    """
    Updates TTS settings using a POST request to the specified server.

    Parameters:
    - ttsurl: The URL of the TTS server.
    """

    url = f"{ttsurl}/set_tts_settings"
    headers = {
        'Accept': 'application/json',
        'Content-Type': 'application/json'
    }
    payload = {
        "stream_chunk_size": 100,
        "temperature": 0.75,
        "speed": 1,
        "length_penalty": 1.0,
        "repetition_penalty": 5,
        "top_p": 0.85,
        "top_k": 50,
        "enable_text_splitting": True
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        if response.status_code == 200:
            queue_message(f"LOAD: TTS Settings updated successfully.")
        else:
            queue_message(f"ERROR: Failed to update TTS settings. Status code: {response.status_code}")
            queue_message(f"INFO: Response: {response.text}")
    except Exception as e:
        queue_message(f"ERROR: TTS update failed: {e}")

def play_audio_stream(tts_stream, samplerate=48000, channels=1, gain=1.0, normalize=False):
    """
    Play the audio stream through speakers using SoundDevice with volume/gain adjustment.
    
    Parameters:
    - tts_stream: Stream of audio data in chunks.
    - samplerate: The sample rate of the audio data.
    - channels: The number of audio channels (e.g., 1 for mono, 2 for stereo).
    - gain: A multiplier for adjusting the volume. Default is 1.0 (no change).
    - normalize: Whether to normalize the audio to use the full dynamic range.
    """
    try:
        with sd.OutputStream(samplerate=samplerate, channels=channels, dtype='int16', blocksize=4096) as stream:
            for chunk in tts_stream:
                if chunk:
                    # Convert bytes to int16 using numpy
                    audio_data = np.frombuffer(chunk, dtype='int16')
                    
                    # Normalize the audio (if enabled)
                    if normalize:
                        max_value = np.max(np.abs(audio_data))
                        if max_value > 0:
                            audio_data = audio_data / max_value * 32767
                    
                    # Apply gain adjustment
                    audio_data = np.clip(audio_data * gain, -32768, 32767).astype('int16')

                    # Write the adjusted audio data to the stream
                    stream.write(audio_data)
                else:
                    queue_message(f"ERROR: Received empty chunk.")
    except Exception as e:
        queue_message(f"ERROR: Error during audio playback: {e}")


# async def generate_tts_audio(text, config):
async def generate_tts_audio(text, config, azure_api_key=None, azure_region=None, ttsurl=None, toggle_charvoice=True, tts_voice=None):
    """
    Generate TTS audio for the given text using the specified TTS system.

    Parameters:
    - text (str): The text to convert into speech.
    - ttsoption (str): The TTS system to use (Azure, server-based, or local).
    - ttsurl (str): The base URL of the TTS server (for server-based TTS).
    - toggle_charvoice (bool): Flag indicating whether to use character voice for TTS.
    - tts_voice (str): The TTS speaker/voice configuration.
    """
    if hasattr(config, "ttsoption"):
        # config가 객체인 경우
        ttsoption = config.ttsoption
        tts_voice = config.tts_voice
        ttsurl = config.ttsurl
        voice_id = config.voice_id
        model_id = config.model_id
    else:
        # 문자열만 넘겨졌을 경우
        ttsoption = config
        tts_voice = None
        ttsurl = None
        voice_id = None
        model_id = None

    try:
        # Azure TTS generation
        if ttsoption == "azure":
           async for chunk in text_to_speech_with_pipelining_azure(text):
                yield chunk

        # Local TTS generation using `espeak-ng`
        elif ttsoption == "espeak":
            async for chunk in text_to_speech_with_pipelining_espeak(text):
                print(f"chunk type : {type(chunk)}")
                yield chunk

        elif ttsoption == "alltalk":
            async for chunk in text_to_speech_with_pipelining_alltalk(text):
                yield chunk
                
        # Local TTS generation using local onboard PIPER TTS
        elif ttsoption == "piper":
            async for chunk in text_to_speech_with_pipelining_piper(text):
                yield chunk  

        # Local Korean TTS using MeloTTS-Korean (pretrained)
        elif ttsoption == "melotts_ko":
            if text_to_speech_with_pipelining_melotts is None:
                raise RuntimeError(
                    "melotts_ko selected but MeloTTS is not installed. "
                    "Run: pip install git+https://github.com/myshell-ai/MeloTTS.git"
                )
            async for chunk in text_to_speech_with_pipelining_melotts(text):
                yield chunk

        elif ttsoption == "elevenlabs":
            async for chunk in text_to_speech_with_pipelining_elevenlabs(text):
                yield chunk

        elif ttsoption == "silero":
            async for chunk in text_to_speech_with_pipelining_silero(text):
                yield chunk 

        else:
            raise ValueError(f"ERROR: Invalid TTS option.")

    except Exception as e:
        queue_message(f"ERROR: Text-to-speech generation failed: {e}")

async def play_audio_chunks(text, config):
    """
    Plays audio chunks sequentially from the generate_tts_audio function.
    """
    async for audio_chunk in generate_tts_audio(text, config):
        try:
            # Read the audio chunk into a format playable by sounddevice
            data, samplerate = sf.read(audio_chunk, dtype='float32')
            sd.play(data, samplerate)
            sd.wait()
            await asyncio.sleep(0.05)
        except Exception as e:
            queue_message(f"ERROR: Failed to play audio chunk: {e}")


def play_audio_file(path):
    """Play a local audio file (wake clips). No network/TTS API call."""
    try:
        data, file_sr = sf.read(path, dtype='float32')
        if data.ndim > 1:
            data = data.mean(axis=1)

        out_device = sd.default.device[1]
        if out_device is None or int(out_device) < 0:
            out_device = sd.default.device[0]
        out_info = sd.query_devices(out_device)
        target_sr = int(out_info['default_samplerate'])

        if int(file_sr) != target_sr:
            data = librosa.resample(y=data, orig_sr=file_sr, target_sr=target_sr)
            file_sr = target_sr

        sd.play(data, int(file_sr), device=out_device)
        sd.wait()
    except Exception as e:
        queue_message(f"ERROR: Failed to play audio file {path}: {e}")

