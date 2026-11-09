"""
module_phone_bridge.py

WebSocket bridge for the TARS Android app. The phone is only a mic and speaker:
it streams PCM16 mic audio here and plays back what the Live session says, while
the session (engine, voice, tools, memory) runs on this machine.

Protocol, one conversation per connection:
    phone -> server  text   {"type": "hello", "token": "...", "input_rate": 16000}
    phone -> server  binary PCM16 mono mic audio at input_rate
    phone -> server  text   {"type": "stop"}
    server -> phone  binary PCM16 mono 24 kHz speech
    server -> phone  text   {"type": "state", "state": "connecting|listening|reconnecting"}
                            {"type": "user" | "assistant", "text": "..."}
                            {"type": "clear"}   drop queued speech (user interrupted)
                            {"type": "end", "reason": "..."}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time

from modules.module_config import load_config
from modules.module_messageQue import queue_message

config = load_config()

PHONE_CONTEXT = (
    "The user is talking through the TARS phone app and may be away from home; "
    "the light, air conditioner and alarm speaker are at home."
)


class PhoneAudioIO:
    """Audio and events for one phone connection, safe to call from the session thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, outbox: asyncio.Queue, input_rate: int):
        self.input_rate = input_rate
        self.context_note = PHONE_CONTEXT
        self._loop = loop
        self._outbox = outbox
        self._lock = threading.Lock()
        self._session = None
        self._pending: list[bytes] = []

    def attach(self, session) -> None:
        """Called by each Live session it serves (a voice/engine change makes a new one)."""
        with self._lock:
            self._session = session
            pending, self._pending = self._pending, []
        for data in pending:
            session.feed_input(data)

    def feed(self, data: bytes) -> None:
        with self._lock:
            session = self._session
            if session is None:
                self._pending.append(data)
                return
        session.feed_input(data)

    def _put(self, item) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._outbox.put_nowait, item)

    def play(self, data: bytes) -> None:
        self._put(bytes(data))

    def clear(self) -> None:
        self._put(json.dumps({"type": "clear"}))

    def event(self, event: dict) -> None:
        self._put(json.dumps(event, ensure_ascii=False))


async def _writer(ws, outbox: asyncio.Queue) -> None:
    from websockets.exceptions import ConnectionClosed

    while True:
        item = await outbox.get()
        if item is None:
            await ws.close()
            return
        try:
            await ws.send(item)
        except ConnectionClosed:
            return


async def _handle(ws, run_conversation) -> None:
    peer = ws.remote_address[0] if ws.remote_address else "?"
    try:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except (asyncio.TimeoutError, ValueError, TypeError):
        await ws.close(code=4400, reason="expected hello")
        return
    token = str(config.get("PHONE_BRIDGE", {}).get("token") or "")
    if not token or not hmac.compare_digest(str(hello.get("token", "")), token):
        queue_message(f"WARNING: Phone bridge rejected a connection from {peer} (bad token).")
        await ws.close(code=4401, reason="unauthorized")
        return

    loop = asyncio.get_running_loop()
    outbox: asyncio.Queue = asyncio.Queue()
    audio_io = PhoneAudioIO(loop, outbox, int(hello.get("input_rate") or 16000))
    stop_event = threading.Event()
    queue_message(f"INFO: Phone connected from {peer}.")
    audio_io.event({"type": "state", "state": "connecting"})

    def conversation() -> None:
        reason = "done"
        try:
            run_conversation(stop_event, audio_io)
        except Exception as exc:  # noqa: BLE001 - report to the phone and close
            reason = f"error: {exc}"
            queue_message(f"ERROR: Phone conversation failed: {exc}")
        audio_io.event({"type": "end", "reason": reason})
        audio_io._put(None)

    writer = asyncio.create_task(_writer(ws, outbox))
    worker = threading.Thread(target=conversation, name="PhoneConversation", daemon=True)
    worker.start()
    try:
        async for message in ws:
            if isinstance(message, bytes):
                audio_io.feed(message)
            elif json.loads(message).get("type") == "stop":
                break
    except Exception:  # connection dropped; ending the session below is enough
        pass
    finally:
        stop_event.set()
        await asyncio.to_thread(worker.join, 15)
        if not writer.done():
            writer.cancel()
        queue_message(f"INFO: Phone disconnected ({peer}).")


def start_phone_bridge(run_conversation, shutdown_event: threading.Event) -> None:
    """Serve forever on the configured host/port; retries while the address is unavailable."""
    from websockets.asyncio.server import serve

    cfg = config.get("PHONE_BRIDGE", {})
    host, port = cfg.get("host", "127.0.0.1"), int(cfg.get("port", 8790))
    if not cfg.get("token"):
        queue_message("WARNING: Phone bridge disabled: PHONE_BRIDGE_TOKEN missing from .env.")
        return

    async def main() -> None:
        async with serve(
            lambda ws: _handle(ws, run_conversation), host, port, max_size=2**22, ping_interval=20
        ):
            queue_message(f"INFO: Phone bridge listening on {host}:{port}.")
            while not shutdown_event.is_set():
                await asyncio.sleep(0.5)

    while not shutdown_event.is_set():
        try:
            asyncio.run(main())
            return
        except OSError as exc:  # e.g. Tailscale not up yet
            queue_message(f"WARNING: Phone bridge cannot bind {host}:{port} ({exc}); retrying in 30s.")
            time.sleep(30)
