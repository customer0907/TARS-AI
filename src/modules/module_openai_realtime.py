"""OpenAI Realtime (gpt-realtime) native-audio session, the second Live engine.

Reuses GeminiLiveSession's mic/speaker handling, idle timeout, system prompt and
tool execution; only the wire protocol differs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

from modules.module_gemini_live import (
    OUTPUT_BLOCK_FRAMES,
    OUTPUT_SAMPLE_RATE,
    GeminiLiveSession,
)
from modules.module_messageQue import queue_live_update, queue_message

# Realtime voices: (gender, style) as heard; OpenAI recommends marin and cedar.
OPENAI_VOICES = {
    "marin": ("female", "natural"), "cedar": ("male", "natural"),
    "alloy": ("neutral", "even"), "ash": ("male", "clear"),
    "ballad": ("male", "expressive"), "coral": ("female", "warm"),
    "echo": ("male", "calm"), "sage": ("female", "calm"),
    "shimmer": ("female", "bright"), "verse": ("male", "lively"),
}

REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"


def _json_schema(schema):
    """Convert Gemini's upper-case schema types to JSON Schema."""
    if isinstance(schema, dict):
        return {
            key: (value.lower() if key == "type" and isinstance(value, str) else _json_schema(value))
            for key, value in schema.items()
        }
    if isinstance(schema, list):
        return [_json_schema(item) for item in schema]
    return schema


class OpenAIRealtimeSession(GeminiLiveSession):
    """One wake-triggered gpt-realtime conversation over the Realtime WebSocket."""

    PROVIDER = "openai"
    VOICES = OPENAI_VOICES
    VOICE_SETTING = "openai_voice"
    TARGET_INPUT_RATE = 24000

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.realtime_config = self.config.get("OPENAI_REALTIME", {})
        self._ws = None
        self._pending_calls: list[dict] = []

    def _api_key(self) -> str:
        return self.realtime_config.get("api_key") or ""

    def voice_name(self) -> str:
        voice = super().voice_name()
        return voice if voice in self.VOICES else self.realtime_config.get("voice_name", "cedar")

    def build_system_instruction(self) -> str:
        instruction = super().build_system_instruction()
        style = self.realtime_config.get("speaking_style")
        return f"{instruction}\n\n### Voice Delivery\n{style}" if style else instruction

    def build_session_update(self) -> dict:
        language = self.config.get("CHAR", {}).get("language", "en")
        transcription = {"model": self.realtime_config.get("transcription_model", "gpt-4o-mini-transcribe")}
        if language:
            transcription["language"] = language
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.build_system_instruction(),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": self.TARGET_INPUT_RATE},
                        "turn_detection": {"type": "semantic_vad"},
                        "transcription": transcription,
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": OUTPUT_SAMPLE_RATE},
                        "voice": self.voice_name(),
                        "speed": self.realtime_config.get("speed", 1.0),
                    },
                },
                "tools": [
                    {
                        "type": "function",
                        "name": declaration["name"],
                        "description": declaration.get("description", ""),
                        "parameters": _json_schema(declaration.get("parameters") or {"type": "OBJECT"}),
                    }
                    for declaration in self.tool_declarations()
                ],
                "tool_choice": "auto",
            },
        }

    async def _send(self, event: dict) -> None:
        await self._ws.send(json.dumps(event))

    async def _run_async(self, api_key: str) -> None:
        import sounddevice as sd
        from websockets.asyncio.client import connect

        self._loop = asyncio.get_running_loop()
        self._input_queue = asyncio.Queue(maxsize=64)
        self._flush_pending_input()
        self._stop = asyncio.Event()
        self._playback.clear()
        self._ratecv_state = None
        self._highpass_previous_input = 0.0
        self._highpass_previous_output = 0.0
        self._input_gain = 1.0

        model = self.realtime_config.get("model", "gpt-realtime-2.1")
        async with connect(
            REALTIME_URL.format(model=model),
            additional_headers={"Authorization": f"Bearer {api_key}"},
            max_size=None,
        ) as ws:
            self._ws = ws
            await self._send(self.build_session_update())
            queue_message(f"INFO: OpenAI Realtime connected ({model}, voice {self.voice_name()}).")
            self._emit({"type": "state", "state": "listening", "engine": self.PROVIDER})
            self._touch()
            with self._audio_streams(sd):
                sender = asyncio.create_task(self._send_audio(ws))
                receiver = asyncio.create_task(self._receive(ws))
                external_stop = asyncio.create_task(self._monitor_external_stop())
                timeout = asyncio.create_task(self._session_timeout())
                idle = asyncio.create_task(self._idle_timeout())
                stop_waiter = asyncio.create_task(self._stop.wait())
                tasks = (sender, receiver, external_stop, timeout, idle, stop_waiter)
                if self.opening_prompt:
                    await self._send(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": self.opening_prompt}],
                            },
                        }
                    )
                    await self._send({"type": "response.create"})

                done, _ = await asyncio.wait(
                    (receiver, stop_waiter), return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done and not receiver.cancelled():
                    error = receiver.exception()
                    if error:
                        raise error

                self._stop.set()
                await asyncio.to_thread(self._playback.wait_empty, 5.0)
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        queue_message("INFO: OpenAI Realtime session ended; returning to wake word.")

    async def _send_audio(self, ws) -> None:
        while not self._stop.is_set():
            try:
                data = await asyncio.wait_for(self._input_queue.get(), timeout=0.25)
            except asyncio.TimeoutError:
                continue
            data = self._prepare_input_audio(data)
            await self._send(
                {"type": "input_audio_buffer.append", "audio": base64.b64encode(data).decode()}
            )

    async def _receive(self, ws) -> None:
        async for raw in ws:
            await self._handle_event(json.loads(raw))
            if self._stop.is_set():
                return

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("type", "")
        if kind == "response.output_audio.delta":
            self._queue_playback_audio(base64.b64decode(event.get("delta", "")))
        elif kind == "response.output_audio_transcript.delta":
            self._output_transcript += event.get("delta", "")
            if self._output_transcript and self.live_config.get("stream_output_transcript", True):
                self._log_input_transcript()
                queue_live_update(self._output_stream_key(), f"TARS: {self._output_transcript}")
                self._output_stream_started = True
        elif kind == "input_audio_buffer.speech_started":
            # The server cancels its own reply on barge-in; drop what is still queued locally.
            self._touch()
            self._clear_playback()
        elif kind == "conversation.item.input_audio_transcription.completed":
            transcript = (event.get("transcript") or "").strip()
            if transcript:
                self._touch()
                self._input_transcript = transcript
                self._log_input_transcript()
        elif kind == "response.done":
            await self._handle_response_done(event.get("response") or {})
        elif kind == "error":
            error = event.get("error") or {}
            queue_message(f"ERROR: OpenAI Realtime: {error.get('message') or error}")

    async def _handle_response_done(self, response: dict) -> None:
        calls = [item for item in response.get("output") or [] if item.get("type") == "function_call"]
        if not calls:
            await self._finish_turn()
            if self._end_after_turn:
                self._stop.set()
            return

        for call in calls:
            name = call.get("name", "")
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            self._tools_in_flight += 1
            try:
                result = await self._execute_tool(name, args)
            except Exception as exc:
                queue_message(f"ERROR: OpenAI Realtime tool {name} failed: {exc}")
                result = {"error": f"{name} failed: {exc}"}
            finally:
                self._tools_in_flight -= 1
                self._touch()
            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": json.dumps({"result": result}, ensure_ascii=False, default=str),
                    },
                }
            )
        with contextlib.suppress(Exception):
            await self._send({"type": "response.create"})
