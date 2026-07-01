"""
module_mqtt_listener.py

Background MQTT subscriber for HA-driven alerts (e.g. front-door motion).
Speaks payload text via TTS while app.py is running in idle/wake-listen mode.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import paho.mqtt.client as mqtt

from modules.module_config import load_config
from modules.module_messageQue import queue_message
from modules.module_tts import normalize_for_korean_tts, play_audio_chunks

config = load_config()

_stt_manager = None
_speaking_lock = threading.Lock()


def set_stt_manager(stt_mgr):
    global _stt_manager
    _stt_manager = stt_mgr


def _decode_payload(raw: bytes) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        return str(data.get("text") or data.get("message") or text).strip()
    return str(data).strip()


def speak_alert(text: str):
    text = text.strip()
    if not text:
        return

    with _speaking_lock:
        if _stt_manager:
            _stt_manager._release_audio_input(pause=0.1)

        tts_text = text
        if config["CHAR"].get("language") == "ko":
            tts_text = normalize_for_korean_tts(tts_text)

        queue_message(f"MQTT ALERT: {tts_text}")
        asyncio.run(play_audio_chunks(tts_text, config["TTS"]))

        if _stt_manager:
            _stt_manager._release_audio_input(pause=0.05)


def _on_connect(client, userdata, flags, reason_code, properties=None):
    if getattr(reason_code, "is_failure", reason_code not in (0, mqtt.MQTT_ERR_SUCCESS)):
        queue_message(f"MQTT: connect failed ({reason_code})")
        return

    topic = config["MQTT"]["topic_voice"]
    client.subscribe(topic)
    queue_message(f"MQTT: subscribed to {topic}")


def _on_message(client, userdata, msg):
    topic_voice = config["MQTT"]["topic_voice"]
    if msg.topic != topic_voice:
        return

    text = _decode_payload(msg.payload)
    if not text:
        return

    threading.Thread(target=speak_alert, args=(text,), daemon=True).start()


def start_mqtt_listener(shutdown_event):
    mqtt_cfg = config["MQTT"]
    if mqtt_cfg.get("enabled") != "True":
        return

    host = mqtt_cfg["host"]
    port = int(mqtt_cfg["port"])
    client_id = mqtt_cfg.get("client_id") or "tars_mqtt_listener"

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )

    username = (mqtt_cfg.get("username") or "").strip()
    password = (mqtt_cfg.get("password") or "").strip()
    if username:
        client.username_pw_set(username, password or None)

    client.on_connect = _on_connect
    client.on_message = _on_message

    queue_message(f"MQTT: connecting to {host}:{port}")
    try:
        client.connect(host, port, keepalive=60)
    except Exception as e:
        queue_message(f"MQTT: connect error: {e}")
        return

    client.loop_start()
    try:
        while not shutdown_event.is_set():
            time.sleep(0.2)
    finally:
        client.loop_stop()
        client.disconnect()
        queue_message("MQTT: listener stopped")
