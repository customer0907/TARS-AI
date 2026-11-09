"""Home Assistant connection test from Windows (no full TARS startup).

Examples:
  # 1) Token + URL only
  python ha_test.py --url http://192.168.0.10:8123 --token eyJhbG...

  # 2) Keyword match only (no HA call)
  python ha_test.py --text "불 좀 꺼져" --match-only

  # 3) Actually toggle input_boolean.tars_light off
  python ha_test.py --url http://192.168.0.10:8123 --token eyJhbG... --text "불 좀 꺼져" --fire
"""

from __future__ import annotations

import argparse
import re
import sys

import requests


def korean_light_intent(text: str):
    t = text.strip().lower()
    if not t:
        return None
    light_words = r"(?:불|조명|전등|lights?)"
    off_verbs = r"(?:꺼|끄|꺼줘|꺼져|꺼줄래|꺼드릴|turn\s+off|lights?\s+off)"
    on_verbs = r"(?:켜|켜줘|켜져|켜줄래|turn\s+on|lights?\s+on)"
    if re.search(rf"{light_words}.{{0,12}}{off_verbs}|{off_verbs}.{{0,12}}{light_words}", t):
        return "off"
    if re.search(rf"{light_words}.{{0,12}}{on_verbs}|{on_verbs}.{{0,12}}{light_words}", t):
        return "on"
    return None


def map_prompt(text: str):
    prompt = text.strip().lower()
    off_phrases = [
        "turn off the light", "turn off the lights", "lights off", "light off", "turn off",
        "불 꺼", "불 꺼줘", "불 좀 꺼", "불 좀 꺼줘", "불 꺼져", "불 좀 꺼져",
        "조명 꺼", "불꺼",
    ]
    on_phrases = [
        "turn on the light", "turn on the lights", "lights on", "light on", "turn on",
        "불 켜", "불 켜줘", "불 좀 켜", "조명 켜", "불켜",
    ]
    intent = korean_light_intent(prompt)
    if intent == "off" or any(p in prompt for p in off_phrases):
        return "input_boolean/turn_off", {"entity_id": "input_boolean.tars_light"}
    if intent == "on" or any(p in prompt for p in on_phrases):
        return "input_boolean/turn_on", {"entity_id": "input_boolean.tars_light"}
    return None


def ping(url: str, token: str) -> int:
    r = requests.get(
        f"{url.rstrip('/')}/api/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    return r.status_code


def fire_service(url: str, token: str, service: str, data: dict) -> requests.Response:
    return requests.post(
        f"{url.rstrip('/')}/api/services/{service}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=data,
        timeout=10,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Test HA token and TARS light keywords from Windows")
    ap.add_argument("--url", help="HA base URL, e.g. http://192.168.0.10:8123")
    ap.add_argument("--token", help="Long-Lived Access Token (Bearer)")
    ap.add_argument("--text", default="불 좀 꺼져", help="Utterance to map (default: 불 좀 꺼져)")
    ap.add_argument("--match-only", action="store_true", help="Only test keyword mapping, no network")
    ap.add_argument("--fire", action="store_true", help="Call input_boolean turn_on/off on HA")
    args = ap.parse_args()

    mapped = map_prompt(args.text)
    print(f"text   : {args.text}")
    print(f"mapped : {mapped}")

    if args.match_only:
        return 0 if mapped else 1

    if not args.url or not args.token:
        print("ERROR: --url and --token required (unless --match-only)", file=sys.stderr)
        return 2

    code = ping(args.url, args.token)
    print(f"GET /api/ -> HTTP {code}")
    if code != 200:
        print("ERROR: token or URL wrong (expected 200)", file=sys.stderr)
        return 3

    if not args.fire:
        print("OK: token works. Add --fire to toggle input_boolean.tars_light")
        return 0

    if not mapped:
        print("ERROR: text did not map to a light service", file=sys.stderr)
        return 4

    service, data = mapped
    print(f"POST /api/services/{service}  {data}")
    resp = fire_service(args.url, args.token, service, data)
    print(f"response: HTTP {resp.status_code}")
    if resp.ok:
        print("OK: HA service called. Check input_boolean.tars_light / your automation.")
        return 0
    print(resp.text, file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
