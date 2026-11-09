"""Gemini Live native-audio session for the optional TARS voice pipeline."""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from modules import module_alarm, module_homeassistant, module_weather
from modules.module_messageQue import queue_live_update, queue_message


# Prebuilt Gemini voices: (gender, Google's one-word style) per name.
GEMINI_VOICES = {
    "Zephyr": ("female", "bright"), "Puck": ("male", "upbeat"),
    "Charon": ("male", "informative"), "Kore": ("female", "firm"),
    "Fenrir": ("male", "excitable"), "Leda": ("female", "youthful"),
    "Orus": ("male", "firm"), "Aoede": ("female", "breezy"),
    "Callirrhoe": ("female", "easy-going"), "Autonoe": ("female", "bright"),
    "Enceladus": ("male", "breathy"), "Iapetus": ("male", "clear"),
    "Umbriel": ("male", "easy-going"), "Algieba": ("male", "smooth"),
    "Despina": ("female", "smooth"), "Erinome": ("female", "clear"),
    "Algenib": ("male", "gravelly"), "Rasalgethi": ("male", "informative"),
    "Laomedeia": ("female", "upbeat"), "Achernar": ("female", "soft"),
    "Alnilam": ("male", "firm"), "Schedar": ("male", "even"),
    "Gacrux": ("female", "mature"), "Pulcherrima": ("female", "forward"),
    "Achird": ("male", "friendly"), "Zubenelgenubi": ("male", "casual"),
    "Vindemiatrix": ("female", "gentle"), "Sadachbia": ("male", "lively"),
    "Sadaltager": ("male", "knowledgeable"), "Sulafat": ("female", "warm"),
}

# Choices made by voice command (e.g. the voice) survive app.py restarts here.
SETTINGS_PATH = Path.home() / ".local/state/tars/live_settings.json"


def load_live_settings() -> dict:
    try:
        return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_live_settings(**changes) -> None:
    settings = load_live_settings()
    settings.update(changes)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


INPUT_SAMPLE_RATE = 16000
OUTPUT_SAMPLE_RATE = 24000
OUTPUT_BLOCK_FRAMES = 960   # 40 ms at 24 kHz


class _PlaybackBuffer:
    """Thread-safe PCM buffer consumed by PortAudio's output callback."""

    def __init__(self):
        self._data = bytearray()
        self._condition = threading.Condition()

    def append(self, data: bytes) -> None:
        if not data:
            return
        with self._condition:
            self._data.extend(data)
            self._condition.notify_all()

    def take(self, size: int) -> bytes:
        with self._condition:
            available = min(size, len(self._data))
            chunk = bytes(self._data[:available])
            del self._data[:available]
            if not self._data:
                self._condition.notify_all()
        if available < size:
            chunk += b"\x00" * (size - available)
        return chunk

    def clear(self) -> None:
        with self._condition:
            self._data.clear()
            self._condition.notify_all()

    def has_data(self) -> bool:
        with self._condition:
            return bool(self._data)

    def wait_empty(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(timeout=remaining)


def _merge_transcript(current: str, fragment: str) -> str:
    """Handle either incremental or cumulative SDK transcript fragments."""
    fragment = (fragment or "").strip()
    if not fragment:
        return current
    if not current:
        return fragment
    if fragment.startswith(current):
        return fragment
    if current.endswith(fragment):
        return current
    separator = "" if fragment[:1] in ".,?!;:、。！？" else " "
    return f"{current}{separator}{fragment}".strip()


# Engines TARS can talk through; switched by voice command via switch_live_model.
LIVE_PROVIDERS = {"gemini": "Gemini (Google)", "openai": "GPT (OpenAI)"}


class GeminiLiveSession:
    """Own one wake-triggered Gemini Live full-duplex audio session."""

    PROVIDER = "gemini"
    VOICES = GEMINI_VOICES
    VOICE_SETTING = "gemini_voice"
    TARGET_INPUT_RATE = INPUT_SAMPLE_RATE

    def __init__(
        self,
        config: dict,
        character_manager=None,
        memory_manager=None,
        input_device=None,
        traditional_delegate: Optional[Callable[[str], Any]] = None,
        homeassistant_delegate: Optional[Callable[[str], Any]] = None,
        turn_callback: Optional[Callable[[str, str, bool], None]] = None,
        shutdown_event: Optional[threading.Event] = None,
        opening_prompt: Optional[str] = None,
        audio_io=None,
    ):
        self.config = config
        self.live_config = config.get("GEMINI_LIVE", {})
        self.character_manager = character_manager
        self.memory_manager = memory_manager
        self.input_device = input_device
        self.traditional_delegate = traditional_delegate
        self.homeassistant_delegate = homeassistant_delegate
        self.turn_callback = turn_callback
        self.shutdown_event = shutdown_event
        # Sent as text right after connecting so TARS speaks first.
        self.opening_prompt = opening_prompt
        # Set by change_voice: the caller reconnects with this opening prompt.
        self.restart_prompt: Optional[str] = None
        # A remote device (the phone app) carrying mic and speaker audio instead of
        # local sounddevice streams: has input_rate, play(data), clear(), event(dict).
        self.audio_io = audio_io
        self._pending_input: list[bytes] = []
        self._feed_lock = threading.Lock()
        if audio_io is not None and hasattr(audio_io, "attach"):
            audio_io.attach(self)

        self._loop = None
        self._input_queue = None
        self._stop = None
        self._types = None
        self._playback = _PlaybackBuffer()
        self._input_sample_rate = INPUT_SAMPLE_RATE
        self._ratecv_state = None
        self._highpass_previous_input = 0.0
        self._highpass_previous_output = 0.0
        self._input_gain = 1.0
        self._mute_input_until = 0.0
        self._input_transcript = ""
        self._input_transcript_logged = False
        self._output_transcript = ""
        self._output_stream_started = False
        self._turn_index = 0
        self._delegated_current_turn = False
        self._end_after_turn = False
        self._last_activity = time.monotonic()
        self._tools_in_flight = 0

    def build_system_instruction(self) -> str:
        char_cfg = self.config.get("CHAR", {})
        llm_cfg = self.config.get("LLM", {})
        language = char_cfg.get("language", "en")
        char_name = getattr(self.character_manager, "char_name", "TARS") or "TARS"
        user_name = char_cfg.get("user_name", "the user")

        parts = []
        if language == "ko":
            ko_persona = getattr(self.character_manager, "ko_persona_block", "") or ""
            if ko_persona.strip():
                parts.append(ko_persona.strip())
        character_card = getattr(self.character_manager, "character_card", "") or ""
        if character_card.strip():
            parts.append(f"### Character\n{character_card.strip()}")
        # Korean sessions take systemprompt_ko; instructionprompt stays English-only
        # because its _ko variant is a per-turn directive, not a session rule.
        system_key = "systemprompt_ko" if language == "ko" else "systemprompt"
        if llm_cfg.get(system_key):
            parts.append(llm_cfg[system_key])
        elif llm_cfg.get("systemprompt"):
            parts.append(llm_cfg["systemprompt"])
        if language != "ko" and llm_cfg.get("instructionprompt"):
            parts.append(llm_cfg["instructionprompt"])

        recent_context = ""
        if self.memory_manager:
            try:
                recent_context = self.memory_manager.get_shortterm_memories_tokenlimit(800)
            except Exception as exc:
                queue_message(f"WARNING: Gemini Live recent-memory seed failed: {exc}")
        if recent_context:
            parts.append(f"### Recent Conversation\n{recent_context}")

        tool_rules = [
            "You are now in a continuous, low-latency native-audio conversation.",
            "The user lives in Korea (KST, UTC+9). Your own sense of the current time is UTC "
            "and is wrong for the user: call get_current_time whenever you need the current "
            "time or date, and always speak times in KST.",
            "Speak naturally and briefly. Do not output Markdown or speaker labels.",
            "Use the same language as the user; default to Korean when the configured language is ko.",
            "The server's automatic voice activity detection controls each speech turn. "
            "Allow natural pauses and allow the user to interrupt you.",
            "Call end_live_session when the user says goodbye, asks you to sleep, or signals they "
            "are done (for example '됐어', '고마워', 'that's all'). "
            "Never end only because of a brief silence.",
        ]
        if self.live_config.get("native_google_search"):
            tool_rules.append(
                "Use Google Search for current public web facts when fresh information is needed."
            )
        if self.live_config.get("traditional_tool_fallback"):
            tool_rules.append(
                "Use delegate_to_traditional_pipeline for TARS tools, configured search providers, "
                "or facts/actions you cannot reliably handle inside this Live session. "
                "Do not use it for ordinary conversation."
            )
        if self._homeassistant_tool_enabled():
            tool_rules.append(
                "Use control_home_assistant only for an explicit smart-home control request."
            )
            tool_rules.append(
                "For turning the room light on or off use control_light, not control_home_assistant."
            )
            tool_rules.append(
                "For the air conditioner use control_air_conditioner (power, target temperature, "
                "mode, fan speed) and get_air_conditioner_state instead of control_home_assistant. "
                "For relative requests such as 'a bit cooler', read the state first."
            )
        if self._alarm_tool_enabled():
            now = datetime.now().astimezone()
            tool_rules.append(
                f"The local time when this session started is {now.strftime('%Y-%m-%d %H:%M')} "
                f"({now.strftime('%A')}, UTC{now.strftime('%z')})."
            )
            tool_rules.append(
                "Every alarm speaks its message aloud when it rings. If the user says what the "
                "alarm should say, put those exact words in message; otherwise write one short, "
                "casual Korean line that fits the purpose and time (e.g. '일어나, 7시야!'). "
                "Use set_alarms for alarm requests and pass every requested alarm in a single call. "
                "Give absolute local times as YYYY-MM-DDTHH:MM, resolving words like tomorrow from "
                "the session start time; for 'in N minutes' requests use in_minutes instead. "
                "If AM or PM is not stated, choose the next upcoming occurrence. "
                "Between midnight and 5 AM, 'tomorrow morning' usually means the coming morning "
                "(today's date); if unsure, say the exact date you mean. "
                "After the tool returns, read back the confirmed times and mention any errors."
            )
            tool_rules.append(
                "Use list_alarms to answer what alarms exist, cancel_alarms to remove them "
                "(look up ids with list_alarms first), and stop_ringing_alarm when the user wants "
                "a sounding alarm silenced."
            )
        tool_rules.append(
            "For weather, rain or temperature questions call get_weather (days=1 for today, "
            "2 to include tomorrow, up to 7 for the week)."
        )
        tool_rules.append(
            "Invoke tools only through function calls; never say a tool call or its arguments "
            "aloud. Never claim a tool failed unless its response contains an error."
        )
        tool_rules.append(
            "After a tool response, report its result faithfully and never change returned facts."
        )
        note = getattr(self.audio_io, "context_note", "")
        if note:
            tool_rules.append(note)
        parts.append("### Live Session Rules\n- " + "\n- ".join(tool_rules))

        instruction = "\n\n".join(str(part) for part in parts if str(part).strip())
        return instruction.replace("{char}", char_name).replace("{user}", user_name)

    def tool_declarations(self) -> list:
        """Function tools in Gemini schema form; other providers convert them."""
        voice_catalog = ", ".join(
            f"{name} ({gender}, {style})" for name, (gender, style) in self.VOICES.items()
        )
        others = [label for key, label in LIVE_PROVIDERS.items() if key != self.PROVIDER]
        declarations = [
            {
                "name": "get_current_time",
                "description": (
                    "Return the user's current local date, time and weekday (Korea, KST). "
                    "Call it for every question about the current time or date."
                ),
                "parameters": {"type": "OBJECT", "properties": {}},
            },
            {
                "name": "get_weather",
                "description": (
                    "Current weather and daily forecast (sky, low/high temperature, rain chance) "
                    "for the user's home in Incheon."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "days": {
                            "type": "INTEGER",
                            "description": "Forecast days starting today, 1-7.",
                        }
                    },
                },
            },
            {
                "name": "switch_live_model",
                "description": (
                    "Switch the conversation engine when the user asks to use another AI model. "
                    f"Current engine: {LIVE_PROVIDERS[self.PROVIDER]}. Other: {', '.join(others)}. "
                    "The conversation reconnects on the new engine."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "provider": {"type": "STRING", "enum": list(LIVE_PROVIDERS)},
                    },
                    "required": ["provider"],
                },
            },
            {
                "name": "change_voice",
                "description": (
                    "Switch TARS to another voice when the user asks for a different voice. "
                    "The conversation reconnects in the new voice. Current voice: "
                    f"{self.voice_name()}. Available: {voice_catalog}."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "voice": {"type": "STRING", "enum": list(self.VOICES)},
                    },
                    "required": ["voice"],
                },
            },
            {
                "name": "end_live_session",
                "description": (
                    "End this wake-triggered Live conversation after a short spoken goodbye. "
                    "Use only when the user explicitly wants to finish or put TARS back to sleep."
                ),
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "reason": {
                            "type": "STRING",
                            "description": "Short reason the session should end.",
                        }
                    },
                },
            }
        ]

        if self.live_config.get("traditional_tool_fallback") and self.traditional_delegate:
            declarations.append(
                {
                    "name": "delegate_to_traditional_pipeline",
                    "description": (
                        "Run the existing TARS LLM/tool pipeline. Use for current facts, web/news "
                        "search through configured providers, weather, vision, volume, image tools, "
                        "or another TARS capability not directly available in Live."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "query": {
                                "type": "STRING",
                                "description": "The user's complete request, preserving dates and context.",
                            }
                        },
                        "required": ["query"],
                    },
                }
            )

        if self._homeassistant_tool_enabled():
            declarations.append(
                {
                    "name": "control_home_assistant",
                    "description": "Send an explicit smart-home command through TARS Home Assistant.",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "command": {
                                "type": "STRING",
                                "description": "The user's complete smart-home command.",
                            }
                        },
                        "required": ["command"],
                    },
                }
            )

        if self._homeassistant_tool_enabled():
            declarations.extend(
                [
                    {
                        "name": "control_light",
                        "description": "Turn the room light on or off.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {"power": {"type": "STRING", "enum": ["on", "off"]}},
                            "required": ["power"],
                        },
                    },
                    {
                        "name": "control_air_conditioner",
                        "description": (
                            "Change the air conditioner through Home Assistant. "
                            "Only pass the settings the user asked to change."
                        ),
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "power": {"type": "STRING", "enum": ["on", "off"]},
                                "temperature": {
                                    "type": "NUMBER",
                                    "description": "Target temperature in Celsius.",
                                },
                                "mode": {
                                    "type": "STRING",
                                    "description": (
                                        "Operating mode such as cool, heat, dry, fan_only or auto; "
                                        "must be one of the unit's available_modes."
                                    ),
                                },
                                "fan_mode": {
                                    "type": "STRING",
                                    "description": (
                                        "Fan speed; must be one of available_fan_modes from "
                                        "get_air_conditioner_state."
                                    ),
                                },
                                "entity_id": {
                                    "type": "STRING",
                                    "description": "Only when several air conditioners exist.",
                                },
                            },
                        },
                    },
                    {
                        "name": "get_air_conditioner_state",
                        "description": (
                            "Read the air conditioner's power state, target and room temperature, "
                            "and the modes and fan speeds it supports."
                        ),
                        "parameters": {"type": "OBJECT", "properties": {}},
                    },
                ]
            )

        if self._alarm_tool_enabled():
            declarations.extend(
                [
                    {
                        "name": "set_alarms",
                        "description": (
                            "Create one or more one-shot alarms that ring on the desktop speaker."
                        ),
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "alarms": {
                                    "type": "ARRAY",
                                    "items": {
                                        "type": "OBJECT",
                                        "properties": {
                                            "time": {
                                                "type": "STRING",
                                                "description": "Absolute local time, YYYY-MM-DDTHH:MM.",
                                            },
                                            "in_minutes": {
                                                "type": "NUMBER",
                                                "description": (
                                                    "Minutes from now; use instead of time for "
                                                    "relative requests."
                                                ),
                                            },
                                            "message": {
                                                "type": "STRING",
                                                "description": (
                                                    "Words spoken aloud when the alarm rings."
                                                ),
                                            },
                                            "label": {
                                                "type": "STRING",
                                                "description": "Optional short name for the alarm.",
                                            },
                                        },
                                    },
                                }
                            },
                            "required": ["alarms"],
                        },
                    },
                    {
                        "name": "list_alarms",
                        "description": "List pending alarms and any alarm that is ringing now.",
                        "parameters": {"type": "OBJECT", "properties": {}},
                    },
                    {
                        "name": "cancel_alarms",
                        "description": "Cancel pending alarms by id, or all of them.",
                        "parameters": {
                            "type": "OBJECT",
                            "properties": {
                                "ids": {
                                    "type": "ARRAY",
                                    "items": {"type": "INTEGER"},
                                    "description": "Alarm ids from list_alarms.",
                                },
                                "all": {
                                    "type": "BOOLEAN",
                                    "description": "True to cancel every pending alarm.",
                                },
                            },
                        },
                    },
                    {
                        "name": "stop_ringing_alarm",
                        "description": "Silence the alarm that is ringing right now.",
                        "parameters": {"type": "OBJECT", "properties": {}},
                    },
                ]
            )

        return declarations

    def build_connect_config(self) -> dict:
        tools = []
        if self.live_config.get("native_google_search"):
            tools.append({"google_search": {}})
        tools.append({"function_declarations": self.tool_declarations()})

        live_config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.build_system_instruction(),
            "speech_config": {
                "language_code": self.live_config.get("language_code")
                or ("ko-KR" if self.config.get("CHAR", {}).get("language") == "ko" else "en-US"),
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": self.voice_name()
                    }
                }
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            # Leave silence/prefix values unset so Gemini owns turn timing.
            "realtime_input_config": {
                "automatic_activity_detection": {"disabled": False}
            },
            "tools": tools,
            "temperature": self.live_config.get("temperature", 0.8),
        }
        thinking_level = self.live_config.get("thinking_level", "minimal")
        if thinking_level:
            live_config["thinking_config"] = {"thinking_level": thinking_level}
        return live_config

    def _homeassistant_tool_enabled(self) -> bool:
        ha_cfg = self.config.get("HOME_ASSISTANT", {})
        return bool(
            self.live_config.get("home_assistant_tool", True)
            and ha_cfg.get("enabled") == "True"
            and self.homeassistant_delegate
        )

    def voice_name(self) -> str:
        voice = load_live_settings().get(self.VOICE_SETTING)
        if voice in self.VOICES:
            return voice
        return self.live_config.get("voice_name", "Kore")

    def _api_key(self) -> str:
        return self.live_config.get("api_key") or ""

    def alarm_voice(self) -> str:
        """Alarm speech is rendered by Gemini TTS, so it always uses the Gemini voice."""
        voice = load_live_settings().get("gemini_voice")
        return voice if voice in GEMINI_VOICES else self.live_config.get("voice_name", "Kore")

    def _alarm_tool_enabled(self) -> bool:
        return bool(self.config.get("ALARM", {}).get("enabled"))

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def run(self) -> bool:
        api_key = self._api_key()
        name = LIVE_PROVIDERS[self.PROVIDER]
        if not api_key:
            queue_message(f"ERROR: {name} live session has no API key in the TARS .env file.")
            return False
        try:
            asyncio.run(self._run_async(api_key))
            return True
        except ModuleNotFoundError as exc:
            queue_message(f"ERROR: {name} live session dependency missing ({exc}).")
        except Exception as exc:
            queue_message(f"ERROR: {name} live session failed: {exc}")
        return False

    async def _run_async(self, api_key: str) -> None:
        from google import genai
        from google.genai import types
        import sounddevice as sd

        self._types = types
        self._loop = asyncio.get_running_loop()
        self._input_queue = asyncio.Queue(maxsize=64)
        self._flush_pending_input()
        self._stop = asyncio.Event()
        self._playback.clear()
        self._ratecv_state = None
        self._highpass_previous_input = 0.0
        self._highpass_previous_output = 0.0
        self._input_gain = 1.0

        client = genai.Client(api_key=api_key)
        model = self.live_config.get("model", "gemini-3.1-flash-live-preview")
        connect_config = self.build_connect_config()

        async with client.aio.live.connect(model=model, config=connect_config) as session:
            queue_message(f"INFO: Gemini Live connected ({model}).")
            self._emit({"type": "state", "state": "listening", "engine": self.PROVIDER})
            self._touch()
            with self._audio_streams(sd):
                sender = asyncio.create_task(self._send_audio(session))
                receiver = asyncio.create_task(self._receive(session))
                external_stop = asyncio.create_task(self._monitor_external_stop())
                timeout = asyncio.create_task(self._session_timeout())
                idle = asyncio.create_task(self._idle_timeout())
                stop_waiter = asyncio.create_task(self._stop.wait())
                tasks = (sender, receiver, external_stop, timeout, idle, stop_waiter)
                if self.opening_prompt:
                    await session.send_realtime_input(text=self.opening_prompt)

                done, _ = await asyncio.wait(
                    (receiver, stop_waiter), return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done and not receiver.cancelled():
                    error = receiver.exception()
                    if error:
                        raise error

                self._stop.set()
                with contextlib.suppress(Exception):
                    await session.send_realtime_input(audio_stream_end=True)
                await asyncio.to_thread(self._playback.wait_empty, 5.0)

                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        queue_message("INFO: Gemini Live session ended; returning to wake word.")

    def _select_input_sample_rate(self, sd) -> int:
        """Prefer native Live 16 kHz, fall back to TARS/device rate when unsupported."""
        try:
            sd.check_input_settings(
                device=self.input_device,
                channels=1,
                dtype="int16",
                samplerate=self.TARGET_INPUT_RATE,
            )
            return self.TARGET_INPUT_RATE
        except Exception:
            try:
                device_info = sd.query_devices(self.input_device, "input")
                fallback_rate = int(device_info.get("default_samplerate") or 44100)
            except Exception:
                fallback_rate = 44100
            queue_message(
                f"INFO: Microphone does not expose {self.TARGET_INPUT_RATE} Hz; using "
                f"{fallback_rate} Hz and locally resampling."
            )
            return fallback_rate

    def _input_callback(self, indata, frames, time_info, status) -> None:
        del frames, time_info, status
        if not self._loop or self._loop.is_closed() or self._input_queue is None:
            return
        if self._input_is_muted():
            # Keep the continuous audio stream alive, but send silence instead of
            # feeding Gemini's speaker output back into its own VAD.
            data = b"\x00" * len(indata)
        else:
            data = bytes(indata)
        self._loop.call_soon_threadsafe(self._offer_input_audio, data)

    def _audio_streams(self, sd):
        """Open the desktop mic/speaker, or nothing when a remote device carries audio."""
        if self.audio_io:
            self._input_sample_rate = self.audio_io.input_rate
            return contextlib.nullcontext()
        self._input_sample_rate = self._select_input_sample_rate(sd)
        streams = contextlib.ExitStack()
        streams.enter_context(
            sd.RawInputStream(
                samplerate=self._input_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=max(1, self._input_sample_rate // 10),
                device=self.input_device,
                latency="high",
                callback=self._input_callback,
            )
        )
        streams.enter_context(
            sd.RawOutputStream(
                samplerate=OUTPUT_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=OUTPUT_BLOCK_FRAMES,
                latency="low",
                callback=self._output_callback,
            )
        )
        return streams

    def feed_input(self, data: bytes) -> None:
        """Accept remote mic audio from any thread; held until the session is ready."""
        with self._feed_lock:
            if self._loop is None or self._input_queue is None:
                self._pending_input.append(data)
                return
        self._loop.call_soon_threadsafe(self._offer_input_audio, data)

    def _flush_pending_input(self) -> None:
        with self._feed_lock:
            pending, self._pending_input = self._pending_input, []
        for data in pending:
            self._offer_input_audio(data)

    def _clear_playback(self) -> None:
        self._playback.clear()
        if self.audio_io:
            self.audio_io.clear()

    def _emit(self, event: dict) -> None:
        if self.audio_io:
            self.audio_io.event(event)

    def _input_is_muted(self) -> bool:
        if self.audio_io:
            return False
        return bool(
            self.live_config.get("mute_input_during_playback", True)
            and time.monotonic() < self._mute_input_until
        )

    def _extend_input_mute(self, playback_seconds: float = 0.0) -> None:
        if not self.live_config.get("mute_input_during_playback", True):
            return
        tail_seconds = float(self.live_config.get("echo_tail_ms", 600)) / 1000.0
        self._mute_input_until = max(
            self._mute_input_until,
            time.monotonic() + max(0.0, playback_seconds) + tail_seconds,
        )

    def _queue_playback_audio(self, data: bytes) -> None:
        if not data:
            return
        self._touch()
        if self.audio_io:
            self.audio_io.play(data)
            return
        self._extend_input_mute()
        if self._input_queue is not None:
            while True:
                try:
                    self._input_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
        self._playback.append(data)

    def _offer_input_audio(self, data: bytes) -> None:
        if self._input_queue is None or not data:
            return
        if self._input_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._input_queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self._input_queue.put_nowait(data)

    def _output_callback(self, outdata, frames, time_info, status) -> None:
        del time_info, status
        if self._playback.has_data():
            self._touch()
            self._extend_input_mute(frames / OUTPUT_SAMPLE_RATE)
        outdata[:] = self._playback.take(len(outdata))

    async def _send_audio(self, session) -> None:
        while not self._stop.is_set():
            try:
                data = await asyncio.wait_for(self._input_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            data = self._prepare_input_audio(data)
            await session.send_realtime_input(
                audio=self._types.Blob(
                    data=data,
                    mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                )
            )

    def _prepare_input_audio(self, data: bytes) -> bytes:
        """Apply conservative mic cleanup and produce Gemini-native 16 kHz PCM16."""
        if not data:
            return data

        processed = data
        if self.live_config.get("input_preprocessing", True):
            import numpy as np

            samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            highpass_hz = float(self.live_config.get("input_highpass_hz", 80.0))
            if highpass_hz > 0 and samples.size:
                dt = 1.0 / self._input_sample_rate
                rc = 1.0 / (2.0 * math.pi * highpass_hz)
                alpha = rc / (rc + dt)
                previous_input = self._highpass_previous_input
                previous_output = self._highpass_previous_output
                for index in range(samples.size):
                    current_input = float(samples[index])
                    previous_output = alpha * (
                        previous_output + current_input - previous_input
                    )
                    samples[index] = previous_output
                    previous_input = current_input
                self._highpass_previous_input = previous_input
                self._highpass_previous_output = previous_output

            rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
            rms_dbfs = 20.0 * math.log10(max(rms, 1e-8))
            gate_dbfs = float(self.live_config.get("input_noise_gate_dbfs", -48.0))
            if rms_dbfs < gate_dbfs:
                attenuation_db = float(
                    self.live_config.get("input_noise_attenuation_db", 18.0)
                )
                samples *= 10.0 ** (-attenuation_db / 20.0)
            else:
                target_dbfs = float(self.live_config.get("input_target_dbfs", -24.0))
                max_gain_db = float(self.live_config.get("input_max_gain_db", 12.0))
                desired_gain_db = min(max(target_dbfs - rms_dbfs, -6.0), max_gain_db)
                desired_gain = 10.0 ** (desired_gain_db / 20.0)
                self._input_gain = (0.85 * self._input_gain) + (0.15 * desired_gain)
                samples *= self._input_gain

            processed = (
                np.clip(samples, -1.0, 0.999969) * 32768.0
            ).astype(np.int16).tobytes()

        if self._input_sample_rate != self.TARGET_INPUT_RATE:
            processed, self._ratecv_state = audioop.ratecv(
                processed,
                2,
                1,
                self._input_sample_rate,
                self.TARGET_INPUT_RATE,
                self._ratecv_state,
            )
        return processed

    async def _receive(self, session) -> None:
        while not self._stop.is_set():
            async for response in session.receive():
                await self._handle_response(session, response)
                if self._stop.is_set():
                    return
            await asyncio.sleep(0)

    async def _handle_response(self, session, response) -> None:
        tool_call = getattr(response, "tool_call", None)
        if tool_call:
            await self._handle_tool_call(session, tool_call)

        server_content = getattr(response, "server_content", None)
        if not server_content:
            return

        if getattr(server_content, "interrupted", False):
            self._clear_playback()
            self._finish_output_stream()
            self._output_transcript = ""

        found_part_audio = False
        model_turn = getattr(server_content, "model_turn", None)
        for part in getattr(model_turn, "parts", None) or []:
            inline_data = getattr(part, "inline_data", None)
            mime_type = getattr(inline_data, "mime_type", "") or ""
            data = getattr(inline_data, "data", None)
            if data and mime_type.startswith("audio/"):
                self._queue_playback_audio(data)
                found_part_audio = True

        if not found_part_audio:
            data = getattr(response, "data", None)
            if data:
                self._queue_playback_audio(data)

        input_tx = getattr(server_content, "input_transcription", None)
        if input_tx:
            if (getattr(input_tx, "text", "") or "").strip():
                self._touch()
            self._input_transcript = _merge_transcript(
                self._input_transcript, getattr(input_tx, "text", "")
            )
            if getattr(input_tx, "finished", False):
                self._log_input_transcript()
        output_tx = getattr(server_content, "output_transcription", None)
        if output_tx:
            self._output_transcript = _merge_transcript(
                self._output_transcript, getattr(output_tx, "text", "")
            )
            if (
                self._output_transcript
                and self.live_config.get("stream_output_transcript", True)
            ):
                self._log_input_transcript()
                queue_live_update(
                    self._output_stream_key(),
                    f"TARS: {self._output_transcript}",
                )
                self._output_stream_started = True

        if getattr(server_content, "turn_complete", False):
            await self._finish_turn()
            if self._end_after_turn:
                self._stop.set()

    async def _handle_tool_call(self, session, tool_call) -> None:
        responses = []
        for function_call in getattr(tool_call, "function_calls", None) or []:
            name = getattr(function_call, "name", "")
            args = dict(getattr(function_call, "args", None) or {})
            self._tools_in_flight += 1
            try:
                result = await self._execute_tool(name, args)
            except Exception as exc:
                queue_message(f"ERROR: Gemini Live tool {name} failed: {exc}")
                result = {"error": f"{name} failed: {exc}"}
            finally:
                self._tools_in_flight -= 1
                self._touch()
            responses.append(
                self._types.FunctionResponse(
                    id=getattr(function_call, "id", None),
                    name=name,
                    response={"result": result},
                )
            )
        if responses:
            await session.send_tool_response(function_responses=responses)

    async def _execute_tool(self, name: str, args: dict) -> Any:
        if name == "delegate_to_traditional_pipeline" and self.traditional_delegate:
            query = str(args.get("query", "")).strip()
            if not query:
                return "The traditional pipeline needs a non-empty query."
            self._delegated_current_turn = True
            queue_message(f"TOOL: Gemini Live -> traditional pipeline: {query}")
            result = await asyncio.to_thread(self.traditional_delegate, query)
            return result or "The traditional pipeline returned no answer."

        if name == "control_home_assistant" and self.homeassistant_delegate:
            command = str(args.get("command", "")).strip()
            if not command:
                return {"error": "A non-empty Home Assistant command is required."}
            queue_message(f"TOOL: Gemini Live -> Home Assistant: {command}")
            return await asyncio.to_thread(self.homeassistant_delegate, command)

        if self._homeassistant_tool_enabled():
            if name == "control_light":
                queue_message(f"TOOL: Live -> light {args.get('power')}")
                return await asyncio.to_thread(module_homeassistant.control_light, args.get("power"))
            if name == "control_air_conditioner":
                queue_message(f"TOOL: Gemini Live -> air conditioner: {args}")
                return await asyncio.to_thread(
                    module_homeassistant.control_climate,
                    power=args.get("power"),
                    temperature=args.get("temperature"),
                    mode=args.get("mode"),
                    fan_mode=args.get("fan_mode"),
                    entity_id=args.get("entity_id"),
                )
            if name == "get_air_conditioner_state":
                return await asyncio.to_thread(module_homeassistant.get_climate_state)

        if self._alarm_tool_enabled():
            if name == "set_alarms":
                alarms = [dict(item) for item in args.get("alarms") or []]
                if not alarms:
                    return {"error": "At least one alarm is required."}
                for alarm in alarms:
                    alarm.setdefault("voice", self.alarm_voice())
                queue_message(f"TOOL: Gemini Live -> set alarms: {alarms}")
                return await asyncio.to_thread(module_alarm.set_alarms, alarms)
            if name == "list_alarms":
                result = await asyncio.to_thread(module_alarm.list_alarms)
                summary = result.get("error") or [
                    f"#{a['id']} {a['time']}" for a in result.get("alarms", [])
                ]
                queue_message(f"TOOL: Live -> list alarms: {summary}")
                return result
            if name == "cancel_alarms":
                ids = [int(value) for value in args.get("ids") or []]
                cancel_all = bool(args.get("all"))
                if not ids and not cancel_all:
                    return {"error": "Give alarm ids or all=true."}
                queue_message(
                    f"TOOL: Gemini Live -> cancel alarms: {'all' if cancel_all else ids}"
                )
                return await asyncio.to_thread(module_alarm.cancel_alarms, ids, cancel_all)
            if name == "stop_ringing_alarm":
                return await asyncio.to_thread(module_alarm.dismiss_ringing)

        if name == "change_voice":
            voice = str(args.get("voice", "")).strip()
            if voice not in self.VOICES:
                return {"error": f"Unknown voice {voice}.", "available": list(self.VOICES)}
            save_live_settings(**{self.VOICE_SETTING: voice})
            queue_message(f"TOOL: Live -> change voice: {voice}")
            self.restart_prompt = (
                f"(System: you now speak with the {voice} voice. In one short Korean sentence, "
                "tell the user the voice changed and ask how it sounds.)"
            )
            self._end_after_turn = True
            return (
                f"Voice saved as {voice}. Reply with only a two- or three-word acknowledgement "
                "such as '바꿔볼게'; the new voice will ask how it sounds after reconnecting."
            )

        if name == "get_weather":
            queue_message(f"TOOL: Live -> weather {args.get('days', 1)} day(s)")
            return await asyncio.to_thread(module_weather.weather_report, args.get("days", 1))

        if name == "get_current_time":
            now = datetime.now().astimezone()
            return {
                "local_time": now.strftime("%Y-%m-%d %H:%M"),
                "weekday": now.strftime("%A"),
                "timezone": f"KST (UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})",
            }

        if name == "switch_live_model":
            provider = str(args.get("provider", "")).strip()
            if provider not in LIVE_PROVIDERS:
                return {"error": f"Unknown engine {provider}.", "available": list(LIVE_PROVIDERS)}
            if provider == self.PROVIDER:
                return f"Already using {LIVE_PROVIDERS[provider]}."
            missing = provider_missing_key(self.config, provider)
            if missing:
                return {"error": missing}
            save_live_settings(live_provider=provider)
            queue_message(f"TOOL: Live -> switch engine: {provider}")
            self.restart_prompt = (
                f"(System: you are now running on {LIVE_PROVIDERS[provider]}. In one short Korean "
                "sentence, tell the user the switch is done.)"
            )
            self._end_after_turn = True
            return (
                "Engine switch saved. Reply with only a two- or three-word acknowledgement; "
                "the new engine greets the user after reconnecting."
            )

        if name == "end_live_session":
            self._end_after_turn = True
            return "Session will end after you say a brief goodbye."

        return {"error": f"Unknown or disabled tool: {name}"}

    async def _finish_turn(self) -> None:
        user_text = self._input_transcript.strip()
        assistant_text = self._output_transcript.strip()
        delegated = self._delegated_current_turn
        streamed_output = self._output_stream_started

        if assistant_text and streamed_output:
            self._finish_output_stream()
        if user_text and not self._input_transcript_logged:
            queue_message(f"USER: {user_text}")
            self._emit({"type": "user", "text": user_text})
        if assistant_text and not streamed_output:
            queue_message(f"TARS: {assistant_text}")
        if assistant_text:
            self._emit({"type": "assistant", "text": assistant_text, "final": True})
        if user_text and assistant_text and self.turn_callback:
            await asyncio.to_thread(
                self.turn_callback, user_text, assistant_text, delegated
            )

        self._input_transcript = ""
        self._input_transcript_logged = False
        self._output_transcript = ""
        self._delegated_current_turn = False
        self._turn_index += 1

    def _output_stream_key(self) -> str:
        return f"gemini-live-output-{self._turn_index}"

    def _log_input_transcript(self) -> None:
        if self._input_transcript_logged:
            return
        user_text = self._input_transcript.strip()
        if user_text:
            queue_message(f"USER: {user_text}")
            self._emit({"type": "user", "text": user_text})
            self._input_transcript_logged = True

    def _finish_output_stream(self) -> None:
        if not self._output_stream_started:
            return
        queue_live_update(
            self._output_stream_key(),
            f"TARS: {self._output_transcript}",
            final=True,
        )
        self._output_stream_started = False

    async def _monitor_external_stop(self) -> None:
        while not self._stop.is_set():
            if self.shutdown_event and self.shutdown_event.is_set():
                self._stop.set()
                return
            await asyncio.sleep(0.2)

    async def _session_timeout(self) -> None:
        seconds = int(self.live_config.get("session_timeout_seconds", 540) or 0)
        if seconds <= 0:
            await self._stop.wait()
            return
        await asyncio.sleep(seconds)
        if not self._stop.is_set():
            queue_message(
                f"INFO: Gemini Live safety timeout reached ({seconds}s); returning to wake word."
            )
            self._stop.set()


    async def _idle_timeout(self) -> None:
        seconds = int(self.live_config.get("idle_timeout_seconds", 25) or 0)
        if seconds <= 0:
            await self._stop.wait()
            return
        while not self._stop.is_set():
            await asyncio.sleep(0.5)
            if self._tools_in_flight:
                continue
            if time.monotonic() - self._last_activity >= seconds:
                queue_message(
                    f"INFO: Gemini Live idle for {seconds}s; returning to wake word."
                )
                self._stop.set()
                return


def provider_missing_key(config: dict, provider: str) -> str:
    """Return why a provider cannot start, or "" when its API key is configured."""
    if provider == "openai" and not config.get("OPENAI_REALTIME", {}).get("api_key"):
        return "OPENAI_REALTIME_API_KEY is not set in the TARS .env file, so GPT cannot be used yet."
    if provider == "gemini" and not config.get("GEMINI_LIVE", {}).get("api_key"):
        return "GEMINI_API_KEY is not set in the TARS .env file."
    return ""


def current_live_provider(config: dict) -> str:
    """The engine picked by voice command, else config, falling back to Gemini."""
    provider = load_live_settings().get("live_provider") or config.get(
        "VOICE_PIPELINE", {}
    ).get("live_provider", "gemini")
    if provider not in LIVE_PROVIDERS or provider_missing_key(config, provider):
        return "gemini"
    return provider


def run_gemini_live_session(**kwargs) -> GeminiLiveSession:
    """Synchronous entrypoint used by the STT wake-word thread; picks the current engine."""
    if current_live_provider(kwargs["config"]) == "openai":
        from modules.module_openai_realtime import OpenAIRealtimeSession

        session = OpenAIRealtimeSession(**kwargs)
    else:
        session = GeminiLiveSession(**kwargs)
    session.run()
    return session
