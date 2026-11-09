"""
module_alarm.py

Client for the standalone tars-alarm daemon (~/tars-alarm/tars_alarm.py).
The daemon owns storage and ringing so alarms survive app.py restarts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from modules.module_config import load_config

config = load_config()


def is_enabled() -> bool:
    return bool(config.get("ALARM", {}).get("enabled"))


def _call(method: str, path: str, payload: dict | None = None, timeout: float = 3.0) -> dict:
    url = config.get("ALARM", {}).get("url", "http://127.0.0.1:8765").rstrip("/") + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except ValueError:
            return {"error": f"alarm daemon returned HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"error": f"alarm daemon unreachable, nothing was changed: {exc}"}


def set_alarms(alarms: list) -> dict:
    # Spoken messages are synthesized while setting, which takes a few seconds.
    return _call("POST", "/alarms", {"alarms": alarms}, timeout=90.0)


def list_alarms() -> dict:
    return _call("GET", "/alarms")


def cancel_alarms(ids: list | None = None, cancel_all: bool = False) -> dict:
    return _call("POST", "/cancel", {"all": True} if cancel_all else {"ids": ids or []})


def dismiss_ringing(timeout: float = 3.0) -> dict:
    return _call("POST", "/dismiss", {}, timeout=timeout)
