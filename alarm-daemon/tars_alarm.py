#!/usr/bin/env python3
"""TARS alarm daemon: persistent one-shot alarms rung on the desktop speaker.

Runs independently of TARS app.py so alarms still ring when the voice app is down.
Standard library only.

    tars_alarm.py serve                       run the daemon (systemd user service)
    tars_alarm.py add "2026-10-12 07:00" [label]
    tars_alarm.py add 07:00 [label]           next upcoming 07:00
    tars_alarm.py add +30 [label]             30 minutes from now
    tars_alarm.py add 07:00 -m "야 일어나"     speak this instead of beeping
    tars_alarm.py list
    tars_alarm.py cancel 3 4 | all
    tars_alarm.py stop                        silence the alarm that is ringing

HTTP API (127.0.0.1 only):
    GET  /alarms    -> {"now", "alarms": [...], "ringing": [...]}
    POST /alarms    {"alarms": [{"time": "YYYY-MM-DDTHH:MM" | "in_minutes": n, "label": "",
                                 "message": "spoken text", "voice": "Charon"}]}
    POST /cancel    {"ids": [1, 2]} or {"all": true}
    POST /dismiss   stop the ringing alarm
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("TARS_ALARM_PORT", "8765"))
STATE_DIR = Path(
    os.environ.get("TARS_ALARM_STATE_DIR")
    or Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "tars-alarm"
)
SOUND_FILE = os.environ.get("TARS_ALARM_SOUND", "")
# Keep ringing this long when nobody dismisses it.
RING_SECONDS = int(os.environ.get("TARS_ALARM_RING_SECONDS", "600"))
# Silence between beep bursts, so the wake word can still be heard.
RING_GAP_SECONDS = float(os.environ.get("TARS_ALARM_RING_GAP_SECONDS", "2.5"))
# An alarm found overdue by more than this (daemon was down, PC was asleep) is dropped.
MISSED_GRACE_SECONDS = int(os.environ.get("TARS_ALARM_MISSED_GRACE_SECONDS", "900"))
# Spoken alarms: the message is synthesized once when the alarm is set, so ringing
# needs no network. GEMINI_API_KEY comes from the environment or TARS_ALARM_ENV_FILE.
# Tried in order: free-tier TTS quotas are a few requests per minute per model.
TTS_MODELS = os.environ.get(
    "TARS_ALARM_TTS_MODELS",
    "gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts",
).split(",")
DEFAULT_VOICE = os.environ.get("TARS_ALARM_VOICE", "Charon")
ENV_FILE = os.environ.get("TARS_ALARM_ENV_FILE", "")
SPEECH_DIR = STATE_DIR / "speech"


def log(message: str) -> None:
    print(message, flush=True)


def now_local() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def parse_time(value: str) -> datetime:
    """Parse an ISO-like timestamp; a value without an offset means local time."""
    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed.astimezone().replace(microsecond=0)


def describe(alarm: dict, now: datetime | None = None) -> dict:
    now = now or now_local()
    when = parse_time(alarm["time"])
    described = {
        "id": alarm["id"],
        "time": when.strftime("%Y-%m-%dT%H:%M:%S" if when.second else "%Y-%m-%dT%H:%M"),
        "weekday": when.strftime("%A"),
        "label": alarm.get("label", ""),
        "minutes_from_now": round((when - now).total_seconds() / 60),
    }
    if alarm.get("message"):
        described["message"] = alarm["message"]
        described["spoken"] = bool(alarm.get("speech"))
    return described


def _gemini_api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key and ENV_FILE:
        try:
            for line in Path(ENV_FILE).read_text(encoding="utf-8").splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "GEMINI_API_KEY":
                    key = value.strip().strip("'\"")
        except OSError:
            pass
    return key


def synthesize(text: str, voice: str) -> Path:
    """Render text to a WAV with Gemini TTS, cached by voice and text."""
    digest = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    path = SPEECH_DIR / f"{digest}.wav"
    if path.exists():
        return path
    key = _gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not available to the alarm daemon")
    # The TTS model reads every word, so the text goes in verbatim with no style prompt.
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    failures = []
    for model in TTS_MODELS:
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model.strip()}:generateContent",
            data=json.dumps(body).encode("utf-8"),
            headers={"x-goog-api-key": key, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:120]
            failures.append(f"{model} HTTP {exc.code}")
            log(f"TTS {model} HTTP {exc.code}: {detail}")
        except OSError as exc:
            failures.append(f"{model} {exc}")
    else:
        raise RuntimeError("TTS failed: " + "; ".join(failures))
    part = data["candidates"][0]["content"]["parts"][0]["inlineData"]
    audio = base64.b64decode(part["data"])
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    if part.get("mimeType", "").startswith("audio/wav"):
        tmp.write_bytes(audio)
    else:  # raw 24 kHz mono PCM16 (audio/l16)
        with wave.open(str(tmp), "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(audio)
    os.replace(tmp, path)
    return path


class Store:
    """Pending alarms, written to disk on every change."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._alarms: list[dict] = []
        self._next_id = 1
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        self._alarms = list(data.get("alarms", []))
        self._next_id = int(data.get("next_id", 1))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"next_id": self._next_id, "alarms": self._alarms},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def add(self, when: datetime, label: str, message: str = "", speech: str = "") -> tuple[dict, bool]:
        """Return (alarm, created). An alarm at the same minute is updated, not duplicated."""
        with self._lock:
            for alarm in self._alarms:
                if parse_time(alarm["time"]).replace(second=0) == when.replace(second=0):
                    changed = False
                    for field, value in (("label", label), ("message", message), ("speech", speech)):
                        if value and alarm.get(field) != value:
                            alarm[field] = value
                            changed = True
                    if changed:
                        self._save()
                    return dict(alarm), False
            alarm = {"id": self._next_id, "time": when.isoformat(), "label": label}
            if message:
                alarm["message"] = message
            if speech:
                alarm["speech"] = speech
            self._next_id += 1
            self._alarms.append(alarm)
            self._alarms.sort(key=lambda item: parse_time(item["time"]))
            self._save()
            return dict(alarm), True

    def list(self) -> list[dict]:
        with self._lock:
            return [dict(alarm) for alarm in self._alarms]

    def cancel(self, ids: list[int] | None) -> list[dict]:
        """Remove the given ids, or everything when ids is None."""
        with self._lock:
            removed = [a for a in self._alarms if ids is None or a["id"] in ids]
            if removed:
                self._alarms = [a for a in self._alarms if a not in removed]
                self._save()
            return removed

    def pop_due(self, now: datetime) -> list[dict]:
        with self._lock:
            due = [a for a in self._alarms if parse_time(a["time"]) <= now]
            if due:
                self._alarms = [a for a in self._alarms if a not in due]
                self._save()
            return due


def ensure_default_sound(path: Path) -> Path:
    """Write the built-in beep pattern (two bursts of four beeps) once."""
    if path.exists():
        return path
    rate = 44100
    frames = bytearray()

    def tone(seconds: float, hz: float = 1000.0) -> None:
        count = int(rate * seconds)
        fade = int(rate * 0.005)
        for index in range(count):
            envelope = min(1.0, index / fade, (count - index) / fade)
            sample = 0.6 * envelope * math.sin(2 * math.pi * hz * index / rate)
            frames.extend(struct.pack("<h", int(sample * 32767)))

    def silence(seconds: float) -> None:
        frames.extend(b"\x00\x00" * int(rate * seconds))

    for _ in range(2):
        for _ in range(4):
            tone(0.12)
            silence(0.08)
        silence(0.4)

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    return path


class _RingSession:
    def __init__(self, alarms: list[dict]):
        self.alarms = list(alarms)
        self.stop = threading.Event()
        self.deadline = time.monotonic() + RING_SECONDS
        self.process: subprocess.Popen | None = None


class Ringer:
    """Loop the alarm sound until dismissed or RING_SECONDS pass."""

    def __init__(self, sound: Path):
        self.sound = sound
        self.player = shutil.which("paplay") or shutil.which("pw-play") or shutil.which("aplay")
        self._lock = threading.Lock()
        self._session: _RingSession | None = None

    def start(self, alarms: list[dict]) -> None:
        with self._lock:
            session = self._session
            if session and not session.stop.is_set():
                session.alarms.extend(alarms)
                session.deadline = time.monotonic() + RING_SECONDS
                return
            session = _RingSession(alarms)
            self._session = session
        threading.Thread(target=self._run, args=(session,), name="Ringer", daemon=True).start()

    def _run(self, session: _RingSession) -> None:
        if not self.player:
            log("ERROR: no audio player found (paplay / pw-play / aplay); alarm is silent")
        while True:
            with self._lock:
                if time.monotonic() >= session.deadline:
                    session.stop.set()
                if session.stop.is_set():
                    break
            for sound in self._sounds(session) if self.player else []:
                if session.stop.is_set():
                    break
                self._play(session, sound)
            session.stop.wait(RING_GAP_SECONDS)
        with self._lock:
            if self._session is session:
                self._session = None
        log("ring ended")

    def _sounds(self, session: _RingSession) -> list[Path]:
        """Each alarm's spoken message, or the beep when none of them has one."""
        with self._lock:
            alarms = list(session.alarms)
        spoken = []
        for alarm in alarms:
            path = Path(alarm["speech"]) if alarm.get("speech") else None
            if path and path.exists() and path not in spoken:
                spoken.append(path)
        return spoken or [self.sound]

    def _play(self, session: _RingSession, sound: Path) -> None:
        try:
            session.process = subprocess.Popen(
                [self.player, str(sound)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            log(f"ERROR: could not start {self.player}: {exc}")
            return
        while session.process.poll() is None:
            if session.stop.wait(0.05):
                session.process.terminate()
                break
        if session.process.wait() != 0 and not session.stop.is_set():
            error = (session.process.stderr.read() or b"").decode(errors="replace")
            log(f"ERROR: {self.player} failed: {error.strip()}")
        session.process.stderr.close()

    def dismiss(self) -> list[dict]:
        with self._lock:
            session = self._session
            if not session or session.stop.is_set():
                return []
            session.stop.set()
            return list(session.alarms)

    def ringing(self) -> list[dict]:
        with self._lock:
            session = self._session
            if not session or session.stop.is_set():
                return []
            return list(session.alarms)


def prune_speech(store: Store, ringer: Ringer) -> None:
    """Delete synthesized messages no pending or ringing alarm uses."""
    keep = {alarm.get("speech") for alarm in store.list() + ringer.ringing()}
    for path in SPEECH_DIR.glob("*.wav"):
        if str(path) not in keep:
            path.unlink(missing_ok=True)


def scheduler(store: Store, ringer: Ringer) -> None:
    # Poll the wall clock so suspend/resume and clock changes need no special handling.
    was_ringing = False
    while True:
        now = now_local()
        fresh = []
        for alarm in store.pop_due(now):
            late = (now - parse_time(alarm["time"])).total_seconds()
            if late > MISSED_GRACE_SECONDS:
                log(f"MISSED alarm {alarm['id']} ({alarm['time']}, {round(late / 60)} min late); dropped")
            else:
                log(f"RING alarm {alarm['id']} ({alarm['time']}) {alarm.get('label', '')}")
                fresh.append(alarm)
        if fresh:
            ringer.start(fresh)
        ringing = bool(ringer.ringing())
        if was_ringing and not ringing:
            prune_speech(store, ringer)
        was_ringing = ringing
        time.sleep(1.0)


def create_alarms(store: Store, requests: list) -> dict:
    now = now_local()
    created, errors = [], []
    for request in requests:
        if not isinstance(request, dict):
            errors.append({"request": request, "error": "each alarm must be an object"})
            continue
        label = str(request.get("label") or "").strip()
        try:
            if request.get("in_minutes") is not None:
                when = (now_local() + timedelta(minutes=float(request["in_minutes"]))).replace(microsecond=0)
            else:
                when = parse_time(request["time"])
        except (KeyError, TypeError, ValueError):
            errors.append(
                {"request": request, "error": "need time as YYYY-MM-DDTHH:MM or in_minutes"}
            )
            continue
        if when <= now:
            errors.append({"request": request, "error": "time is in the past"})
            continue
        message = str(request.get("message") or "").strip()
        speech, speech_error = "", ""
        if message:
            voice = str(request.get("voice") or DEFAULT_VOICE).strip()
            try:
                speech = str(synthesize(message, voice))
            except Exception as exc:  # the alarm still beeps without its speech
                speech_error = str(exc)
                log(f"ERROR: speech synthesis failed: {exc}")
        alarm, was_created = store.add(when, label, message, speech)
        entry = describe(alarm, now)
        if speech_error:
            entry["speech_error"] = f"{speech_error}; this alarm will beep instead"
        if not was_created:
            entry["already_existed"] = True
        else:
            log(f"SET alarm {alarm['id']} ({alarm['time']}) {label} {message}".rstrip())
        created.append(entry)
    result = {"now": now.strftime("%Y-%m-%dT%H:%M"), "alarms": created}
    if errors:
        result["errors"] = errors
    return result


def make_handler(store: Store, ringer: Ringer):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002 - keep the journal quiet
            pass

        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def do_GET(self):  # noqa: N802
            if self.path != "/alarms":
                self._reply(404, {"error": "not found"})
                return
            now = now_local()
            self._reply(
                200,
                {
                    "now": now.strftime("%Y-%m-%dT%H:%M"),
                    "alarms": [describe(a, now) for a in store.list()],
                    "ringing": [describe(a, now) for a in ringer.ringing()],
                },
            )

        def do_POST(self):  # noqa: N802
            try:
                body = self._body()
            except ValueError as exc:
                self._reply(400, {"error": f"invalid JSON: {exc}"})
                return

            if self.path == "/alarms":
                requests = body.get("alarms")
                if not isinstance(requests, list) or not requests:
                    self._reply(400, {"error": "alarms must be a non-empty list"})
                    return
                self._reply(200, create_alarms(store, requests))
            elif self.path == "/cancel":
                if body.get("all"):
                    ids = None
                else:
                    try:
                        ids = [int(value) for value in body.get("ids") or []]
                    except (TypeError, ValueError):
                        self._reply(400, {"error": "ids must be a list of numbers"})
                        return
                    if not ids:
                        self._reply(400, {"error": "need ids or all"})
                        return
                removed = store.cancel(ids)
                if removed:
                    prune_speech(store, ringer)
                for alarm in removed:
                    log(f"CANCEL alarm {alarm['id']} ({alarm['time']})")
                removed_ids = {alarm["id"] for alarm in removed}
                self._reply(
                    200,
                    {
                        "cancelled": [describe(a) for a in removed],
                        "not_found": [i for i in ids or [] if i not in removed_ids],
                    },
                )
            elif self.path == "/dismiss":
                dismissed = ringer.dismiss()
                if dismissed:
                    log("DISMISS " + ", ".join(str(a["id"]) for a in dismissed))
                self._reply(200, {"dismissed": [describe(a) for a in dismissed]})
            else:
                self._reply(404, {"error": "not found"})

    return Handler


def serve() -> None:
    store = Store(STATE_DIR / "alarms.json")
    sound = Path(SOUND_FILE) if SOUND_FILE else ensure_default_sound(STATE_DIR / "alarm.wav")
    ringer = Ringer(sound)
    prune_speech(store, ringer)
    server = ThreadingHTTPServer((HOST, PORT), make_handler(store, ringer))
    threading.Thread(target=scheduler, args=(store, ringer), name="Scheduler", daemon=True).start()
    log(f"tars-alarm listening on {HOST}:{PORT}, {len(store.list())} pending, sound {sound}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ringer.dismiss()


# === CLI ===
def _call(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        f"http://{HOST}:{PORT}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read().decode("utf-8"))
    except OSError as exc:
        sys.exit(f"tars-alarm daemon unreachable on {HOST}:{PORT}: {exc}")


def _cli_time(text: str) -> dict:
    if text.startswith("+"):
        return {"in_minutes": float(text[1:])}
    if len(text) <= 5 and ":" in text:
        hour, minute = (int(part) for part in text.split(":"))
        now = now_local()
        when = now.replace(hour=hour, minute=minute, second=0)
        if when <= now:
            when += timedelta(days=1)
        return {"time": when.isoformat()}
    return {"time": text}


def _print_alarms(alarms: list[dict]) -> None:
    for alarm in alarms:
        extra = "  (already existed)" if alarm.get("already_existed") else ""
        print(
            f"  #{alarm['id']}  {alarm['time'].replace('T', ' ')}  {alarm['weekday'][:3]}"
            f"  in {alarm['minutes_from_now']} min  {alarm['label']}{extra}"
            + (f'  says "{alarm["message"]}"' if alarm.get("message") else "")
        )


def main(argv: list[str]) -> None:
    command = argv[1] if len(argv) > 1 else "list"
    args = argv[2:]
    if command == "serve":
        serve()
    elif command == "add" and args:
        request = _cli_time(args[0])
        rest = args[1:]
        if "-m" in rest:
            index = rest.index("-m")
            request["message"] = " ".join(rest[index + 1:])
            rest = rest[:index]
        request["label"] = " ".join(rest)
        result = _call("POST", "/alarms", {"alarms": [request]})
        _print_alarms(result.get("alarms", []))
        for error in result.get("errors", []):
            print(f"  error: {error['error']}")
        for alarm in result.get("alarms", []):
            if alarm.get("speech_error"):
                print(f"  speech error: {alarm['speech_error']}")
    elif command == "list":
        result = _call("GET", "/alarms")
        if result["ringing"]:
            print("ringing now:")
            _print_alarms(result["ringing"])
        print("pending:" if result["alarms"] else "no pending alarms")
        _print_alarms(result["alarms"])
    elif command == "cancel" and args:
        payload = {"all": True} if args == ["all"] else {"ids": [int(value) for value in args]}
        result = _call("POST", "/cancel", payload)
        print(f"cancelled {len(result.get('cancelled', []))}")
        _print_alarms(result.get("cancelled", []))
        if result.get("not_found"):
            print(f"  not found: {result['not_found']}")
    elif command == "stop":
        result = _call("POST", "/dismiss")
        print("stopped" if result["dismissed"] else "nothing was ringing")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
