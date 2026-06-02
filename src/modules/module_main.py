"""
module_main.py

Core logic module for the TARS-AI application.

"""
# === Standard Libraries ===
import os
import threading
import json
import re
import concurrent.futures
import sys
import time
import asyncio
import sounddevice as sd
import soundfile as sf

# === Custom Modules ===
from modules.module_config import load_config
from modules.module_btcontroller import start_controls
from modules.module_discord import *
from modules.module_llm import process_completion
from modules.module_tts import play_audio_chunks, play_audio_file, strip_elevenlabs_tags
from modules.module_homeassistant import map_prompt_to_ha_service, send_prompt_to_homeassistant
from modules.module_messageQue import queue_message

# === Constants and Globals ===
character_manager = None
memory_manager = None
stt_manager = None

CONFIG = load_config()

# Global Variables (if needed)
stop_event = threading.Event()
executor = concurrent.futures.ProcessPoolExecutor(max_workers=4)

# === Threads ===
def start_bt_controller_thread():
    """
    Wrapper to start the BT Controller functionality in a thread.
    """
    try:
        queue_message(f"LOAD: Starting BT Controller thread...")
        while not stop_event.is_set():
            start_controls()
    except Exception as e:
        queue_message(f"ERROR: {e}")

# === Callback Functions ===
def process_discord_message_callback(user_message):
    """
    Processes the user's message and generates a response.

    Parameters:
    - user_message (str): The message content sent by the user.

    Returns:
    - str: The bot's response.
    """
    try:
        # Parse the user message
        #queue_message(user_message)

        match = re.match(r"<@(\d+)> ?(.*)", user_message)

        if match:
            mentioned_user_id = match.group(1)  # Extracted user ID
            message_content = match.group(2).strip()  # Extracted message content (trim leading/trailing spaces)

        #stream_text_nonblocking(f"{mentioned_user_id}: {message_content}")
        #queue_message(message_content)

        # Process the message using process_completion
        reply = process_completion(message_content)  # Process the message

        #queue_message(f"TARS: {reply}")
        #stream_text_nonblocking(f"TARS: {reply}")
        
    except Exception as e:
        queue_message(f"ERROR: {e}")

    return reply

def wake_word_callback(wake_response):
    """
    Play wake response immediately. Uses pre-recorded audio when available (no API latency).
    """
    if isinstance(wake_response, dict):
        text = wake_response.get('text', '')
        audio_path = wake_response.get('audio_path')
        if audio_path and os.path.isfile(audio_path):
            play_audio_file(audio_path)
            return
        if text:
            asyncio.run(play_audio_chunks(text, CONFIG['TTS']['ttsoption']))
            return
        return

    asyncio.run(play_audio_chunks(wake_response, CONFIG['TTS']['ttsoption']))

def utterance_callback(message):
    """
    Process the recognized message from STTManager and stream audio response to speakers.

    Parameters:
    - message (str): The recognized message from the Speech-to-Text (STT) module.
    """
    try:
        # Parse the user message
        message_dict = json.loads(message)
        if not message_dict.get('text'):  # Handles cases where text is "" or missing
            #queue_message(f"TARS: Going Idle...")
            return
        
        #Print or stream the response
        #queue_message(f"USER: {message_dict['text']}")
        queue_message(f"USER: {message_dict['text']}")

        # Check for shutdown command
        if "shutdown pc" in message_dict['text'].lower():
            queue_message(f"SHUTDOWN: Shutting down the PC...")
            os.system('shutdown /s /t 0')
            return  # Exit function after issuing shutdown command

        user_text = message_dict['text']
        ha_cfg = CONFIG.get('HOME_ASSISTANT', {})
        ha_skip_llm = str(ha_cfg.get('ha_skip_llm', 'true')).lower() in ('true', '1', 'yes')
        if (
            ha_cfg.get('enabled') == "True"
            and ha_skip_llm
            and map_prompt_to_ha_service(user_text)
        ):
            try:
                send_prompt_to_homeassistant(user_text)
            except Exception as e:
                queue_message(f"ERROR: HA shortcut failed: {e}")
            else:
                if stt_manager:
                    stt_manager._release_audio_input(pause=0.05)
                return
            # HA failed → fall through to LLM

        reply = process_completion(user_text)

        try:
            match = re.search(r"<think>(.*?)</think>", reply, re.DOTALL)
            thoughts = match.group(1).strip() if match else ""
            reply = re.sub(r"<think>.*?</think>", "", reply, flags=re.DOTALL).strip()
        except Exception:
            thoughts = ""

        if thoughts:
            pass

        queue_message(f"TARS: {strip_elevenlabs_tags(reply)}")

        tts_text = reply
        if CONFIG['TTS']['ttsoption'] != 'elevenlabs':
            tts_text = re.sub(
                r'[^\w\s.,?!;:"\'\-。、，·…]', '', tts_text, flags=re.UNICODE
            )

        if stt_manager:
            stt_manager._release_audio_input(pause=0.05)
        asyncio.run(play_audio_chunks(tts_text, CONFIG['TTS']))
        if stt_manager:
            stt_manager._release_audio_input(pause=0.05)

    except json.JSONDecodeError:
        queue_message("ERROR: Invalid JSON format. Could not process user message.")
    except Exception as e:
        queue_message(f"ERROR: {e}")

def post_utterance_callback():
    """
    Restart listening for another utterance after handling the current one.
    """
    global stt_manager
    stt_manager._transcribe_utterance()
    pass

# === Initialization ===
def initialize_managers(mem_manager, char_manager, stt_mgr):
    """
    Pass in the shared instances for MemoryManager, CharacterManager, and STTManager.
    
    Parameters:
    - mem_manager: The MemoryManager instance from app.py.
    - char_manager: The CharacterManager instance from app.py.
    - stt_mgr: The STTManager instance from app.py.
    """
    global memory_manager, character_manager, stt_manager
    memory_manager = mem_manager
    character_manager = char_manager
    stt_manager = stt_mgr
