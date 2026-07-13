"""Gemini Live native-audio session for the optional TARS voice pipeline."""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import math
import threading
import time
from typing import Any, Callable, Optional

from modules.module_messageQue import queue_live_update, queue_message


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


class GeminiLiveSession:
    """Own one wake-triggered Gemini Live full-duplex audio session."""

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
        self._output_transcript = ""
        self._output_stream_started = False
        self._turn_index = 0
        self._delegated_current_turn = False
        self._end_after_turn = False

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
        if llm_cfg.get("systemprompt"):
            parts.append(llm_cfg["systemprompt"])
        if llm_cfg.get("instructionprompt"):
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
            "Speak naturally and briefly. Do not output Markdown or speaker labels.",
            "Use the same language as the user; default to Korean when the configured language is ko.",
            "The server's automatic voice activity detection controls each speech turn. "
            "Allow natural pauses and allow the user to interrupt you.",
            "Call end_live_session only when the user explicitly says goodbye, asks you to sleep, "
            "or clearly asks to end the conversation. Never end only because of a brief silence.",
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
            "After a tool response, report its result faithfully and never change returned facts."
        )
        parts.append("### Live Session Rules\n- " + "\n- ".join(tool_rules))

        instruction = "\n\n".join(str(part) for part in parts if str(part).strip())
        return instruction.replace("{char}", char_name).replace("{user}", user_name)

    def build_connect_config(self) -> dict:
        declarations = [
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

        tools = []
        if self.live_config.get("native_google_search"):
            tools.append({"google_search": {}})
        tools.append({"function_declarations": declarations})

        live_config = {
            "response_modalities": ["AUDIO"],
            "system_instruction": self.build_system_instruction(),
            "speech_config": {
                "language_code": self.live_config.get("language_code")
                or ("ko-KR" if self.config.get("CHAR", {}).get("language") == "ko" else "en-US"),
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": self.live_config.get("voice_name", "Kore")
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

    def run(self) -> bool:
        api_key = self.live_config.get("api_key")
        if not api_key:
            queue_message(
                "ERROR: Gemini Live mode requires GEMINI_API_KEY in the TARS .env file."
            )
            return False
        try:
            asyncio.run(self._run_async(api_key))
            return True
        except ModuleNotFoundError as exc:
            queue_message(
                f"ERROR: Gemini Live dependency missing ({exc}). Install google-genai and sounddevice."
            )
        except Exception as exc:
            queue_message(f"ERROR: Gemini Live session failed: {exc}")
        return False

    async def _run_async(self, api_key: str) -> None:
        from google import genai
        from google.genai import types
        import sounddevice as sd

        self._types = types
        self._loop = asyncio.get_running_loop()
        self._input_queue = asyncio.Queue(maxsize=64)
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
            self._input_sample_rate = self._select_input_sample_rate(sd)

            input_stream = sd.RawInputStream(
                samplerate=self._input_sample_rate,
                channels=1,
                dtype="int16",
                blocksize=max(1, self._input_sample_rate // 10),
                device=self.input_device,
                latency="high",
                callback=self._input_callback,
            )
            output_stream = sd.RawOutputStream(
                samplerate=OUTPUT_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=OUTPUT_BLOCK_FRAMES,
                latency="low",
                callback=self._output_callback,
            )

            with input_stream, output_stream:
                sender = asyncio.create_task(self._send_audio(session))
                receiver = asyncio.create_task(self._receive(session))
                external_stop = asyncio.create_task(self._monitor_external_stop())
                timeout = asyncio.create_task(self._session_timeout())
                stop_waiter = asyncio.create_task(self._stop.wait())
                tasks = (sender, receiver, external_stop, timeout, stop_waiter)

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
                samplerate=INPUT_SAMPLE_RATE,
            )
            return INPUT_SAMPLE_RATE
        except Exception:
            try:
                device_info = sd.query_devices(self.input_device, "input")
                fallback_rate = int(device_info.get("default_samplerate") or 44100)
            except Exception:
                fallback_rate = 44100
            queue_message(
                f"INFO: Microphone does not expose 16 kHz; using {fallback_rate} Hz "
                "and locally resampling to 16 kHz."
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

    def _input_is_muted(self) -> bool:
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

        if self._input_sample_rate != INPUT_SAMPLE_RATE:
            processed, self._ratecv_state = audioop.ratecv(
                processed,
                2,
                1,
                self._input_sample_rate,
                INPUT_SAMPLE_RATE,
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
            self._playback.clear()
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
            self._input_transcript = _merge_transcript(
                self._input_transcript, getattr(input_tx, "text", "")
            )
        output_tx = getattr(server_content, "output_transcription", None)
        if output_tx:
            self._output_transcript = _merge_transcript(
                self._output_transcript, getattr(output_tx, "text", "")
            )
            if (
                self._output_transcript
                and self.live_config.get("stream_output_transcript", True)
            ):
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
            try:
                result = await self._execute_tool(name, args)
            except Exception as exc:
                queue_message(f"ERROR: Gemini Live tool {name} failed: {exc}")
                result = {"error": f"{name} failed: {exc}"}
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
        if user_text:
            queue_message(f"USER: {user_text}")
        if assistant_text and not streamed_output:
            queue_message(f"TARS: {assistant_text}")
        if user_text and assistant_text and self.turn_callback:
            await asyncio.to_thread(
                self.turn_callback, user_text, assistant_text, delegated
            )

        self._input_transcript = ""
        self._output_transcript = ""
        self._delegated_current_turn = False
        self._turn_index += 1

    def _output_stream_key(self) -> str:
        return f"gemini-live-output-{self._turn_index}"

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


def run_gemini_live_session(**kwargs) -> bool:
    """Synchronous entrypoint used by the STT wake-word thread."""
    return GeminiLiveSession(**kwargs).run()
