import re
import requests
from modules.module_config import load_config

from modules.module_messageQue import queue_message

config = load_config()

HEADERS = {
    "Authorization": f"Bearer {config['HOME_ASSISTANT']['HA_TOKEN']}",
    "Content-Type": "application/json"
}

# --- Light HA rule engine (no LLM) -----------------------------------------
# Match when score >= MIN_SCORE and strictly beats the opposite intent.

MIN_SCORE = 2

# STT often mishears 불 → 둘/돌/물
_LIGHT_WORD = r"(?:불|조명|전등|불들|라이트|둘|돌|물|lights?)"
_FILLERS = r"(?:야|어|이|그|저|아|음+|흠+|근데|미안|규나야|타스야|토니야|헤이|hey)"

# Strong one-shot phrases (compact, spaces removed)
_OFF_PHRASES = [
    r"turnoff(?:the)?(?:light|lights)?",
    r"lightsoff",
    r"rightsoff",
    r"불(?:좀)?꺼(?:줘|져)?",
    r"조명(?:좀)?꺼(?:줘|져)?",
    r"전등(?:좀)?꺼(?:줘|져)?",
    r"(?:둘|돌|물)(?:좀)?꺼(?:줘|져)?",
    r"(?:둘|돌|불|물)(?:좀)?거(?:줘)?",
    r"(?:둘|돌|불|물)고",
    r"불꺼(?:져|줘)?",
    r"물꺼(?:져|줘)?",
]
_ON_PHRASES = [
    r"turnon(?:the)?(?:light|lights)?",
    r"lightson",
    r"rightson",
    r"불(?:좀)?켜(?:줘|져)?",
    r"조명(?:좀)?켜(?:줘|져)?",
    r"전등(?:좀)?켜(?:줘|져)?",
    r"(?:둘|돌|물)(?:좀)?켜(?:줘|져)?",
    r"불(?:좀)?키(?:워|라)(?:줘|고)?",
    r"물(?:좀)?키(?:워|라)(?:줘|고)?",
    r"불켜(?:져|줘)?",
]

# Action tokens (used with light subject OR very short utterances)
_OFF_ACTION = re.compile(
    r"(?:꺼(?:줘|져|라|라고|어|)?|끄(?:어|어줘|라)?|turnoff|lightsoff)",
    re.I,
)
_ON_ACTION = re.compile(
    r"(?:켜(?:줘|져|라|라고|)?|키(?:워|라)(?:줘|고|)?|turnon|lightson)",
    re.I,
)
_LIGHT_SUBJECT = re.compile(_LIGHT_WORD, re.I)

# Standalone commands after wake (strict length)
_SHORT_OFF = re.compile(r"^(?:꺼(?:줘|져)?|끄(?:어|어줘)?)$", re.I)
_SHORT_ON = re.compile(r"^(?:켜(?:줘|져)?|키(?:워|라)(?:줘)?)$", re.I)


def _normalize_prompt(prompt: str) -> tuple[str, str]:
    """Return (spoken text, compact text without spaces/punctuation)."""
    if isinstance(prompt, dict):
        prompt = prompt.get("text", "")
    text = str(prompt).strip().lower()
    text = re.sub(rf"(?:{_FILLERS})[\s,]*", "", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip()
    compact = re.sub(r"[\s\.!,?~…·]+", "", text)
    return text, compact


def _score_phrases(compact: str, patterns: list[str]) -> int:
    return sum(3 for pat in patterns if re.search(pat, compact, re.I))


def _detect_light_intent(prompt: str) -> str | None:
    """
    Rule-based light on/off intent. Returns 'on', 'off', or None.
    Requires enough signal; rejects ambiguous on+off ties.
    """
    text, compact = _normalize_prompt(prompt)
    if not compact:
        return None

    off_score = _score_phrases(compact, _OFF_PHRASES)
    on_score = _score_phrases(compact, _ON_PHRASES)

    has_light = bool(_LIGHT_SUBJECT.search(compact) or _LIGHT_SUBJECT.search(text))
    has_off = bool(_OFF_ACTION.search(compact))
    has_on = bool(_ON_ACTION.search(compact))

    # Short utterance: "켜줘", "꺼", "꺼줘" (wake already happened)
    if len(compact) <= 8:
        if _SHORT_OFF.match(compact):
            off_score += 2
        if _SHORT_ON.match(compact):
            on_score += 2

    # Light subject + action verb in same sentence
    # e.g. "야 규나야 불좀 꺼줘" → 불 + 꺼줘
    if has_light and has_off:
        off_score += 2
    if has_light and has_on:
        on_score += 2

    # Action only, but utterance is short and looks like a command
    if not has_light and len(compact) <= 12:
        if has_off and not has_on:
            off_score += 1
        if has_on and not has_off:
            on_score += 1

    # Long casual chat without any light word → not HA
    if len(compact) > 40 and not has_light:
        return None

    if off_score >= MIN_SCORE and off_score > on_score:
        return "off"
    if on_score >= MIN_SCORE and on_score > off_score:
        return "on"
    return None


def clean_prompt(prompt):
    """
    Cleans and validates the prompt for Home Assistant.

    Parameters:
    - prompt (str): The natural language query.

    Returns:
    - str: Cleaned and formatted prompt.
    """
    text, _ = _normalize_prompt(prompt)
    return text


def map_prompt_to_ha_service(prompt: str):
    intent = _detect_light_intent(prompt)
    if intent == "off":
        return {
            "service": "input_boolean/turn_off",
            "data": {"entity_id": "input_boolean.tars_light"},
        }
    if intent == "on":
        return {
            "service": "input_boolean/turn_on",
            "data": {"entity_id": "input_boolean.tars_light"},
        }
    return None


# --- Air conditioner (climate entity) ----------------------------------------

def _ha_request(method: str, path: str, payload: dict | None = None):
    url = f"{config['HOME_ASSISTANT']['url']}{path}"
    response = requests.request(method, url, headers=HEADERS, json=payload, timeout=15)
    if not response.ok:
        raise Exception(f"Home Assistant {response.status_code}: {response.text[:200]}")
    return response.json()


def _resolve_climate(entity_id: str | None = None) -> dict:
    """Return the climate entity state to act on, or {"error": ...}."""
    entity_id = (entity_id or config['HOME_ASSISTANT'].get('climate_entity') or "").strip()
    if entity_id:
        return _ha_request("GET", f"/api/states/{entity_id}")
    climates = [
        state for state in _ha_request("GET", "/api/states")
        if state["entity_id"].startswith("climate.")
    ]
    if not climates:
        return {"error": "No air conditioner is registered in Home Assistant."}
    if len(climates) > 1:
        return {
            "error": "Several air conditioners exist; pass entity_id.",
            "choices": [
                {"entity_id": s["entity_id"], "name": s["attributes"].get("friendly_name")}
                for s in climates
            ],
        }
    return climates[0]


def _climate_summary(state: dict) -> dict:
    attrs = state.get("attributes", {})
    return {
        "entity_id": state["entity_id"],
        "name": attrs.get("friendly_name"),
        "state": state.get("state"),
        "target_temperature": attrs.get("temperature"),
        "current_temperature": attrs.get("current_temperature"),
        "fan_mode": attrs.get("fan_mode"),
        "available_modes": attrs.get("hvac_modes"),
        "available_fan_modes": attrs.get("fan_modes"),
        "min_temperature": attrs.get("min_temp"),
        "max_temperature": attrs.get("max_temp"),
    }


def get_climate_state(entity_id: str | None = None) -> dict:
    state = _resolve_climate(entity_id)
    return state if "error" in state else _climate_summary(state)


def control_climate(power=None, temperature=None, mode=None, fan_mode=None, entity_id=None) -> dict:
    """Apply the given air conditioner settings; omitted settings are left alone."""
    state = _resolve_climate(entity_id)
    if "error" in state:
        return state
    attrs = state.get("attributes", {})
    target = {"entity_id": state["entity_id"]}

    power = str(power).lower() if power else None
    mode = str(mode).lower() if mode else None
    if power not in (None, "on", "off"):
        return {"error": "power must be on or off."}
    if mode and mode not in (attrs.get("hvac_modes") or []):
        return {"error": f"Unsupported mode {mode}.", "available_modes": attrs.get("hvac_modes")}
    if fan_mode and fan_mode not in (attrs.get("fan_modes") or []):
        return {
            "error": f"Unsupported fan mode {fan_mode}.",
            "available_fan_modes": attrs.get("fan_modes"),
        }
    if temperature is not None:
        temperature = float(temperature)
        low, high = attrs.get("min_temp"), attrs.get("max_temp")
        if (low is not None and temperature < low) or (high is not None and temperature > high):
            return {"error": f"Temperature must be between {low} and {high}."}

    done = []
    if power == "off":
        _ha_request("POST", "/api/services/climate/turn_off", target)
        done.append("turned off")
    else:
        wants_setting = mode or fan_mode or temperature is not None
        # Asking for a temperature or fan speed on a stopped unit implies starting it.
        if not mode and (power == "on" or (wants_setting and state.get("state") == "off")):
            _ha_request("POST", "/api/services/climate/turn_on", target)
            done.append("turned on")
        if mode:
            _ha_request("POST", "/api/services/climate/set_hvac_mode", {**target, "hvac_mode": mode})
            done.append(f"mode {mode}")
        if temperature is not None:
            _ha_request(
                "POST", "/api/services/climate/set_temperature", {**target, "temperature": temperature}
            )
            done.append(f"temperature {temperature:g}")
        if fan_mode:
            _ha_request("POST", "/api/services/climate/set_fan_mode", {**target, "fan_mode": fan_mode})
            done.append(f"fan {fan_mode}")
    if not done:
        return {"error": "Nothing to change; give power, temperature, mode or fan_mode."}

    queue_message(f"Air conditioner: {', '.join(done)}")
    return {"applied": done, "name": attrs.get("friendly_name"), "entity_id": state["entity_id"]}


def control_light(power: str) -> dict:
    """Turn the TARS-controlled room light on or off."""
    power = str(power or "").lower()
    if power not in ("on", "off"):
        return {"error": "power must be on or off."}
    _ha_request(
        "POST",
        f"/api/services/input_boolean/turn_{power}",
        {"entity_id": "input_boolean.tars_light"},
    )
    queue_message(f"Light turned {power}.")
    return {"applied": f"light {power}"}


def send_prompt_to_homeassistant(prompt):
    print("homeassistant func starts.")
    queue_message(f"sending prompt {prompt}")

    if config['HOME_ASSISTANT']['enabled'] != "True":
        return {"error": "Home Assistant is disabled"}

    mapped = map_prompt_to_ha_service(prompt)

    if mapped:  # rule 기반 매핑이 가능한 경우
        service = mapped["service"]
        data = mapped["data"]
        url = f"{config['HOME_ASSISTANT']['url']}/api/services/{service}"
        response = requests.post(url, headers=HEADERS, json=data)
        if response.ok:
            queue_message("Lights status changed.")
            return response.json()
        else:
            raise Exception(f"Failed to call HA service: {response.status_code}, {response.text}")

    # fallback → 기존 conversation.process 사용
    url = f"{config['HOME_ASSISTANT']['url']}/api/conversation/process"
    cleaned_prompt = clean_prompt(prompt)
    data = {"text": cleaned_prompt}
    queue_message(data["text"])
    response = requests.post(url, json=data, headers=HEADERS)
    if response.ok:
        resp_json = response.json()
        # HA nests the reply as response.speech.plain.speech.
        speech = resp_json.get("response", {}).get("speech", {})
        if isinstance(speech, dict):
            speech = speech.get("plain", {}).get("speech", "")
        speech = str(speech or "").strip()
        if speech:
            queue_message(f"HA: {speech}")
        return {"home_assistant_reply": speech or "No reply from Home Assistant."}
    else:
        raise Exception(f"Failed to send prompt: {response.status_code}, {response.text}")
