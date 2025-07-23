import requests
from modules.module_config import load_config

from modules.module_messageQue import queue_message

config = load_config()

HEADERS = {
    "Authorization": f"Bearer {config['HOME_ASSISTANT']['HA_TOKEN']}",
    "Content-Type": "application/json"
}

def clean_prompt(prompt):
    """
    Cleans and validates the prompt for Home Assistant.

    Parameters:
    - prompt (str): The natural language query.

    Returns:
    - str: Cleaned and formatted prompt.
    """
    # Basic cleanup: strip extra spaces and ensure proper capitalization
    if isinstance(prompt, dict):
        prompt = prompt.get("text", "")
    return str(prompt).strip()

def map_prompt_to_ha_service(prompt: str):
    prompt = prompt.lower().strip()

    lights_off_list = [
        "turn off the lights", "turn off the light", "lights off", "light off", "turn off"
    ]
    lights_on_list = [
        "turn on the lights", "turn on the light", "lights on", "light on", "turn on"
    ]

    if any(phrase in prompt for phrase in lights_off_list):
        return {
            "service": "input_boolean/turn_off",
            "data": {"entity_id": "input_boolean.tars_light"}
        }

    elif any(phrase in prompt for phrase in lights_on_list):
        return {
            "service": "input_boolean/turn_on",
            "data": {"entity_id": "input_boolean.tars_light"}
        }

    return None

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
    data = { "text": cleaned_prompt }
    queue_message(data["text"])
    response = requests.post(url, json=data, headers=HEADERS)
    if response.ok:
        resp_json = response.json()
        speech = resp_json.get("speech") or resp_json.get("response", {}).get("speech", "")
        queue_message(speech)
        return resp_json
    else:
        raise Exception(f"Failed to send prompt: {response.status_code}, {response.text}")
