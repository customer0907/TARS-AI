"""
module_gemini_live_web.py

Phase 0 browser-audio bridge for the full-duplex experiment.

Goal: prove that the browser's built-in WebRTC echo cancellation gives TARS
real full duplex over one shared speaker/mic (e.g. EMeet), while the Gemini Live
session stays on the server. Pure audio round-trip, nothing else:

    browser mic  (getUserMedia echoCancellation)  --SocketIO-->  server
        server  <->  Gemini Live
    Gemini audio  --SocketIO-->  browser  (AudioContext playback = AEC reference)

Tools, memory, persona seeding and wake word are intentionally NOT wired here.
Those belong to Phase 1, once AEC full duplex is confirmed. The async Gemini
session runs in its own thread so it never fights the eventlet/Flask-SocketIO
loop; audio crosses the thread boundary through plain thread-safe queues and
callbacks (see module_chatui for the SocketIO side).
"""

from __future__ import annotations

import asyncio
import queue
import threading

from modules.module_messageQue import queue_message

INPUT_SAMPLE_RATE = 16000   # Gemini Live native mic rate; browser downsamples to this
OUTPUT_SAMPLE_RATE = 24000  # Gemini Live native output rate


class WebGeminiLiveBridge:
    """One browser-driven Gemini Live audio session, isolated in its own thread."""

    def __init__(self, config, on_output_audio, on_event=None):
        self.config = config
        self.live_config = config.get("GEMINI_LIVE", {})
        self._on_output_audio = on_output_audio            # callable(bytes) -> browser
        self._on_event = on_event or (lambda kind, text="": None)
        self._input_queue: "queue.Queue" = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._thread = None
        self._types = None

    # ---- public API (called from the SocketIO / eventlet side) ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._clear_input()
        self._thread = threading.Thread(
            target=self._run, name="WebGeminiLive", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._input_queue.put_nowait(None)  # unblock the sender's get()
        except queue.Full:
            pass

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def feed_input_audio(self, data: bytes) -> None:
        """16 kHz PCM16 mono from the browser mic. Non-blocking; drops oldest if full."""
        if self._stop.is_set() or not data:
            return
        try:
            self._input_queue.put_nowait(data)
        except queue.Full:
            try:
                self._input_queue.get_nowait()
                self._input_queue.put_nowait(data)
            except queue.Empty:
                pass

    def _clear_input(self) -> None:
        while True:
            try:
                self._input_queue.get_nowait()
            except queue.Empty:
                break

    # ---- session thread ----

    def _run(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception as exc:  # noqa: BLE001 - report and end the session cleanly
            queue_message(f"ERROR: Gemini Live web bridge crashed: {exc}")
            self._on_event("error", str(exc))
        finally:
            self._on_event("status", "ended")

    async def _run_async(self) -> None:
        try:
            from google import genai
            from google.genai import types
        except ModuleNotFoundError as exc:
            queue_message(f"ERROR: Gemini Live web needs google-genai ({exc}).")
            self._on_event("error", "google-genai not installed")
            return

        api_key = self.live_config.get("api_key")
        if not api_key:
            queue_message("ERROR: Gemini Live web needs GEMINI_API_KEY in the .env file.")
            self._on_event("error", "GEMINI_API_KEY missing")
            return

        self._types = types
        client = genai.Client(api_key=api_key)
        model = self.live_config.get("model", "gemini-3.1-flash-live-preview")
        connect_config = self._build_connect_config()

        async with client.aio.live.connect(model=model, config=connect_config) as session:
            queue_message(f"INFO: Gemini Live web bridge connected ({model}).")
            self._on_event("status", "connected")

            sender = asyncio.create_task(self._send(session))
            receiver = asyncio.create_task(self._receive(session))
            stopper = asyncio.create_task(self._wait_stop())
            done, pending = await asyncio.wait(
                {sender, receiver, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

            # Surface a real sender/receiver failure (not a normal stop).
            for task in done:
                if task is stopper or task.cancelled():
                    continue
                exc = task.exception()
                if exc:
                    raise exc

        queue_message("INFO: Gemini Live web bridge session ended.")

    def _build_connect_config(self) -> dict:
        language_code = self.live_config.get("language_code") or (
            "ko-KR" if self.config.get("CHAR", {}).get("language") == "ko" else "en-US"
        )
        return {
            "response_modalities": ["AUDIO"],
            "system_instruction": (
                "You are TARS in a low-latency, full-duplex voice conversation. "
                "Speak naturally and briefly. Use the same language as the user and "
                "default to Korean. Allow the user to interrupt you at any time."
            ),
            "speech_config": {
                "language_code": language_code,
                "voice_config": {
                    "prebuilt_voice_config": {
                        "voice_name": self.live_config.get("voice_name", "Kore")
                    }
                },
            },
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            # Server VAD owns turn-taking so the user can barge in (full duplex).
            "realtime_input_config": {
                "automatic_activity_detection": {"disabled": False}
            },
        }

    async def _wait_stop(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(0.1)

    async def _send(self, session) -> None:
        while not self._stop.is_set():
            data = await asyncio.to_thread(self._next_input, 0.25)
            if not data:
                continue
            await session.send_realtime_input(
                audio=self._types.Blob(
                    data=data,
                    mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}",
                )
            )

    def _next_input(self, timeout):
        try:
            return self._input_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    async def _receive(self, session) -> None:
        while not self._stop.is_set():
            async for response in session.receive():
                self._handle_response(response)
                if self._stop.is_set():
                    return
            await asyncio.sleep(0)

    def _handle_response(self, response) -> None:
        server_content = getattr(response, "server_content", None)

        # Barge-in: tell the browser to flush whatever it is still playing.
        if server_content is not None and getattr(server_content, "interrupted", False):
            self._on_event("interrupted", "")

        found_audio = False
        model_turn = getattr(server_content, "model_turn", None)
        for part in getattr(model_turn, "parts", None) or []:
            inline_data = getattr(part, "inline_data", None)
            mime_type = getattr(inline_data, "mime_type", "") or ""
            data = getattr(inline_data, "data", None)
            if data and mime_type.startswith("audio/"):
                self._on_output_audio(data)
                found_audio = True

        if not found_audio:
            data = getattr(response, "data", None)
            if data:
                self._on_output_audio(data)

        if server_content is None:
            return

        input_tx = getattr(server_content, "input_transcription", None)
        if input_tx and getattr(input_tx, "text", ""):
            self._on_event("input_transcript", input_tx.text)
        output_tx = getattr(server_content, "output_transcription", None)
        if output_tx and getattr(output_tx, "text", ""):
            self._on_event("output_transcript", output_tx.text)
