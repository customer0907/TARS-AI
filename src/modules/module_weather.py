"""
module_weather.py

Lightweight weather for TARS via Open-Meteo (free HTTP API, no API key).
Supports current conditions and up to 7-day forecast for a configured location.
"""

import re
from datetime import datetime

import requests

from modules.module_config import load_config
from modules.module_messageQue import queue_message

CONFIG = load_config()

# WMO weather interpretation codes (Open-Meteo)
_WMO_KO = {
    0: "맑음",
    1: "대체로 맑음",
    2: "약간 흐림",
    3: "흐림",
    45: "안개",
    48: "안개",
    51: "가벼운 이슬비",
    53: "이슬비",
    55: "강한 이슬비",
    61: "약한 비",
    63: "비",
    65: "강한 비",
    71: "약한 눈",
    73: "눈",
    75: "강한 눈",
    80: "소나기",
    81: "소나기",
    82: "강한 소나기",
    95: "뇌우",
    96: "우박 뇌우",
    99: "강한 우박 뇌우",
}

_WEEKDAY_KO = ["월", "화", "수", "목", "금", "토", "일"]

_FORECAST_HINT = re.compile(
    r"(일주일|7\s*일|주간|이번\s*주|내일\s*부터|며칠|앞으로|주\s*날씨|다음\s*주)",
    re.I,
)


def _wmo_label(code):
    try:
        return _WMO_KO.get(int(code), "변덕스러움")
    except (TypeError, ValueError):
        return "알 수 없음"


def _cfg():
    w = CONFIG.get("WEATHER", {})
    return {
        "latitude": float(w.get("latitude", 37.447)),
        "longitude": float(w.get("longitude", 126.731)),
        "location_name": w.get("location_name", "인천 남동구"),
        "timezone": w.get("timezone", "Asia/Seoul"),
        "forecast_days": int(w.get("forecast_days", 7)),
    }


def _fetch_open_meteo(forecast_days=7):
    cfg = _cfg()
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": cfg["latitude"],
        "longitude": cfg["longitude"],
        "timezone": cfg["timezone"],
        "forecast_days": min(max(forecast_days, 1), 16),
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
    }
    resp = requests.get(url, params=params, timeout=8)
    resp.raise_for_status()
    return resp.json()


def _format_current(data, location_name):
    cur = data.get("current", {})
    temp = cur.get("temperature_2m")
    hum = cur.get("relative_humidity_2m")
    wind = cur.get("wind_speed_10m")
    code = cur.get("weather_code")
    label = _wmo_label(code)
    parts = [f"{location_name} 지금 {label}"]
    if temp is not None:
        parts.append(f"기온 {temp:.0f}도")
    if hum is not None:
        parts.append(f"습도 {hum:.0f}%")
    if wind is not None:
        parts.append(f"풍속 {wind:.0f}km/h")
    return ", ".join(parts) + "."


def _format_weekly(data, location_name):
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    codes = daily.get("weather_code", [])
    tmax = daily.get("temperature_2m_max", [])
    tmin = daily.get("temperature_2m_min", [])
    pop = daily.get("precipitation_probability_max", [])

    lines = [f"{location_name} 주간 날씨:"]
    for i, d in enumerate(dates[:7]):
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            day = _WEEKDAY_KO[dt.weekday()]
            label = _wmo_label(codes[i] if i < len(codes) else None)
            hi = tmax[i] if i < len(tmax) else None
            lo = tmin[i] if i < len(tmin) else None
            rain = pop[i] if i < len(pop) else None
            seg = f"{dt.month}/{dt.day}({day}) {label}"
            if hi is not None and lo is not None:
                seg += f" {lo:.0f}~{hi:.0f}도"
            if rain is not None:
                seg += f" 강수 {rain:.0f}%"
            lines.append(seg)
        except (ValueError, IndexError):
            continue
    return "\n".join(lines)


def get_weather(user_input=""):
    """
    Fetch weather for the configured location. Returns a short Korean summary
    for the LLM (current or weekly based on query hints).
    """
    cfg = _cfg()
    want_week = bool(_FORECAST_HINT.search(user_input or ""))
    days = cfg["forecast_days"] if want_week else 1

    try:
        queue_message(f"INFO: Open-Meteo weather ({cfg['location_name']})")
        data = _fetch_open_meteo(forecast_days=days)
        if want_week:
            return _format_weekly(data, cfg["location_name"])
        return _format_current(data, cfg["location_name"])
    except requests.RequestException as e:
        queue_message(f"ERROR: Weather API failed: {e}")
        return f"{cfg['location_name']} 날씨를 가져오지 못했어. 잠시 후 다시 시도해줘."
