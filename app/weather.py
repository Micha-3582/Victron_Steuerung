"""
Wettervorhersage fuer den Standort der Anlage (nur Anzeige, steuert nichts).

Quelle: Open-Meteo (kostenlos, ohne Schluessel). Es gehen nur die Koordinaten an Open-Meteo. Das Ergebnis wird
30 Minuten zwischengespeichert. Standort in der Config: weather_lat, weather_lon, weather_name.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import requests

import store

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
CACHE_TTL_S = 30 * 60
ERROR_TTL_S = 5 * 60
TIMEOUT = 10

_lock = threading.Lock()
_cache: dict = {"key": None, "until": 0.0, "data": None}


class WeatherError(Exception):
    pass


# WMO-Wettercode -> (Text, Symbol tagsueber, Symbol nachts)
_WMO = {
    0: ("Klar", "☀️", "🌙"),
    1: ("Überwiegend klar", "🌤️", "🌙"),
    2: ("Teils bewölkt", "⛅", "☁️"),
    3: ("Bewölkt", "☁️", "☁️"),
    45: ("Nebel", "🌫️", "🌫️"), 48: ("Reifnebel", "🌫️", "🌫️"),
    51: ("Leichter Nieselregen", "🌦️", "🌧️"), 53: ("Nieselregen", "🌦️", "🌧️"), 55: ("Starker Nieselregen", "🌧️", "🌧️"),
    56: ("Gefrierender Nieselregen", "🌧️", "🌧️"), 57: ("Gefrierender Nieselregen", "🌧️", "🌧️"),
    61: ("Leichter Regen", "🌧️", "🌧️"), 63: ("Regen", "🌧️", "🌧️"), 65: ("Starker Regen", "🌧️", "🌧️"),
    66: ("Gefrierender Regen", "🌧️", "🌧️"), 67: ("Gefrierender Regen", "🌧️", "🌧️"),
    71: ("Leichter Schneefall", "🌨️", "🌨️"), 73: ("Schneefall", "🌨️", "🌨️"), 75: ("Starker Schneefall", "🌨️", "🌨️"),
    77: ("Schneegriesel", "🌨️", "🌨️"),
    80: ("Leichte Regenschauer", "🌦️", "🌧️"), 81: ("Regenschauer", "🌦️", "🌧️"), 82: ("Starke Regenschauer", "🌧️", "🌧️"),
    85: ("Schneeschauer", "🌨️", "🌨️"), 86: ("Starke Schneeschauer", "🌨️", "🌨️"),
    95: ("Gewitter", "⛈️", "⛈️"), 96: ("Gewitter mit Hagel", "⛈️", "⛈️"), 99: ("Gewitter mit starkem Hagel", "⛈️", "⛈️"),
}


def describe(code, day: bool = True) -> tuple[str, str]:
    text, sun, night = _WMO.get(int(code) if code is not None else -1, ("–", "❓", "❓"))
    return text, (sun if day else night)


_COMPASS = ("N", "NO", "O", "SO", "S", "SW", "W", "NW")


def compass(deg) -> str:
    try:
        return _COMPASS[int((float(deg) + 22.5) // 45) % 8]
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------- Standort
def get_location() -> dict:
    cfg = store.load_config()
    lat, lon = cfg.get("weather_lat"), cfg.get("weather_lon")
    ok = isinstance(lat, (int, float)) and isinstance(lon, (int, float))
    return {"configured": ok, "lat": lat if ok else None, "lon": lon if ok else None,
            "name": str(cfg.get("weather_name") or "")}


def save_location(lat, lon, name: str = ""):
    """lat/lon = None entfernt den Standort."""
    cfg = store.load_config()
    if lat is None and lon is None:
        for k in ("weather_lat", "weather_lon", "weather_name"):
            cfg.pop(k, None)
    else:
        try:
            lat, lon = float(str(lat).replace(",", ".")), float(str(lon).replace(",", "."))
        except (TypeError, ValueError):
            raise WeatherError("Breiten- und Längengrad müssen Zahlen sein")
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise WeatherError("Koordinaten außerhalb des gültigen Bereichs")
        cfg["weather_lat"], cfg["weather_lon"] = round(lat, 5), round(lon, 5)
        cfg["weather_name"] = str(name or "").strip()[:80]
    store.save_config(cfg)
    with _lock:
        _cache.update(key=None, until=0.0, data=None)


def search_places(query: str) -> list[dict]:
    q = str(query or "").strip()
    if len(q) < 2:
        raise WeatherError("Bitte mindestens 2 Buchstaben eingeben")
    try:
        r = requests.get(GEOCODE_URL, params={"name": q, "count": 6, "language": "de", "format": "json"}, timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results") or []
    except (requests.RequestException, ValueError) as e:
        raise WeatherError(f"Ortssuche nicht erreichbar: {e}")
    out = []
    for p in results:
        try:
            out.append({"name": p["name"], "region": ", ".join(x for x in (p.get("admin1"), p.get("country")) if x),
                        "lat": round(float(p["latitude"]), 5), "lon": round(float(p["longitude"]), 5)})
        except (KeyError, TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------- Vorhersage
def _hhmm(iso: str | None) -> str:
    return iso[11:16] if iso and len(iso) >= 16 else ""


def _parse(raw: dict, name: str) -> dict:
    cur, hr, dy = raw.get("current") or {}, raw.get("hourly") or {}, raw.get("daily") or {}
    if "temperature_2m" not in cur:
        raise WeatherError("Unerwartete Antwort von Open-Meteo")
    is_day = bool(cur.get("is_day", 1))
    text, icon = describe(cur.get("weather_code"), is_day)
    current = {"temp": round(cur["temperature_2m"], 1), "feels": round(cur.get("apparent_temperature", cur["temperature_2m"]), 1),
               "text": text, "icon": icon, "humidity": cur.get("relative_humidity_2m"), "cloud": cur.get("cloud_cover"),
               "wind": round(cur.get("wind_speed_10m") or 0), "wind_dir": compass(cur.get("wind_direction_10m")),
               "precip": cur.get("precipitation") or 0.0}
    times = hr.get("time") or []
    now_h = (cur.get("time") or "")[:13]                 # "YYYY-MM-DDTHH" in der Ortszeit des Standorts
    start = next((i for i, t in enumerate(times) if t[:13] >= now_h), 0)
    hourly = []
    for i in range(start, min(start + 24, len(times))):
        t = times[i]
        h = int(t[11:13])
        day_ = 6 <= h < 21
        _, ic = describe((hr.get("weather_code") or [None] * len(times))[i], day_)
        hourly.append({"time": t, "hour": h, "icon": ic, "temp": round(hr["temperature_2m"][i]),
                       "pop": (hr.get("precipitation_probability") or [None] * len(times))[i],
                       "precip": (hr.get("precipitation") or [0] * len(times))[i],
                       "cloud": (hr.get("cloud_cover") or [None] * len(times))[i],
                       "wind": round((hr.get("wind_speed_10m") or [0] * len(times))[i] or 0)})
    daily = []
    for i, d in enumerate(dy.get("time") or []):
        text_d, icon_d = describe((dy.get("weather_code") or [None])[i], True)
        sun_s = (dy.get("sunshine_duration") or [None] * (i + 1))[i]
        daily.append({"date": d, "icon": icon_d, "text": text_d,
                      "tmax": round(dy["temperature_2m_max"][i]), "tmin": round(dy["temperature_2m_min"][i]),
                      "precip": round((dy.get("precipitation_sum") or [0] * (i + 1))[i] or 0, 1),
                      "pop": (dy.get("precipitation_probability_max") or [None] * (i + 1))[i],
                      "wind": round((dy.get("wind_speed_10m_max") or [0] * (i + 1))[i] or 0),
                      "sunrise": _hhmm((dy.get("sunrise") or [None] * (i + 1))[i]),
                      "sunset": _hhmm((dy.get("sunset") or [None] * (i + 1))[i]),
                      "sun_h": round(sun_s / 3600, 1) if sun_s is not None else None})
    return {"current": current, "hourly": hourly, "daily": daily, "name": name}


def forecast(force: bool = False) -> dict:
    """{'configured', 'error', 'name', 'updated', 'current', 'hourly', 'daily'}"""
    loc = get_location()
    if not loc["configured"]:
        return {"configured": False, "error": None}
    key = (loc["lat"], loc["lon"])
    with _lock:
        if not force and _cache["key"] == key and time.time() < _cache["until"] and _cache["data"]:
            return _cache["data"]
    params = {"latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto", "forecast_days": 7, "wind_speed_unit": "kmh",
              "current": "temperature_2m,apparent_temperature,relative_humidity_2m,is_day,precipitation,weather_code,cloud_cover,wind_speed_10m,wind_direction_10m",
              "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,cloud_cover,wind_speed_10m",
              "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,wind_speed_10m_max,sunrise,sunset,sunshine_duration"}
    try:
        r = requests.get(FORECAST_URL, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        data = _parse(r.json(), loc["name"])
        data.update(configured=True, error=None, updated=datetime.now().isoformat(timespec="seconds"))
        with _lock:
            _cache.update(key=key, until=time.time() + CACHE_TTL_S, data=data)
        return data
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError, WeatherError) as e:
        msg = str(e) if isinstance(e, WeatherError) else f"Wetterdaten nicht erreichbar: {e}"
        with _lock:
            old = _cache["data"] if _cache["key"] == key else None
            _cache.update(key=key, until=time.time() + ERROR_TTL_S, data=old)
        if old:                                            # alte Werte weiter zeigen, Fehler dazu melden
            return {**old, "error": msg}
        return {"configured": True, "error": msg, "name": loc["name"]}
