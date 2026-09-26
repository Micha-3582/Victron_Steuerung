"""
Victron-VRM-Portal: Solar-Ertragsprognose (`solar_yield_forecast`) per VRM-API abholen.

Zugang: persoenlicher Zugriffstoken (VRM -> Einstellungen -> Integrationen) + Installations-ID.
Beides liegt in `vrm_cloud.json` (nicht im Repo) und geht nie zum Browser. Die Prognose aendert
sich nur langsam, deshalb wird das Ergebnis 30 min zwischengespeichert.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta

import requests

CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vrm_cloud.json")
API = "https://vrmapi.victronenergy.com/v2"
CACHE_TTL_S = 30 * 60
ERROR_TTL_S = 5 * 60              # nach einem Fehler nicht sofort wieder anfragen
TIMEOUT = 15

_lock = threading.Lock()
_cache: dict = {"until": 0.0, "data": None, "error": None}


class VrmError(Exception):
    pass


def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def credentials_public() -> dict:
    c = load_credentials()
    return {"configured": bool(c.get("token") and c.get("installation_id")),
            "installation_id": c.get("installation_id", "")}


def save_credentials(installation_id, token=None):
    """Token leer/None = vorhandenen behalten."""
    inst = str(installation_id or "").strip()
    if not inst.isdigit():
        raise VrmError("Die Installations-ID besteht nur aus Ziffern (z. B. 844297)")
    c = load_credentials()
    tok = str(token or "").strip() or c.get("token", "")
    if not tok:
        raise VrmError("Bitte den Zugriffstoken eintragen")
    tmp = CREDENTIALS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"installation_id": inst, "token": tok}, f)
    os.replace(tmp, CREDENTIALS_PATH)
    with _lock:
        _cache.update(until=0.0, data=None, error=None)


def _request(c: dict, params: dict) -> dict:
    try:
        r = requests.get(f"{API}/installations/{c['installation_id']}/stats", params=params,
                         headers={"X-Authorization": f"Token {c['token']}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise VrmError(f"VRM nicht erreichbar: {e}")
    if r.status_code in (401, 403):
        raise VrmError("VRM hat den Zugriffstoken abgelehnt (Token oder Installations-ID prüfen)")
    if r.status_code == 404:
        raise VrmError("Installation nicht gefunden (Installations-ID prüfen)")
    if r.status_code == 429:
        raise VrmError("VRM: zu viele Anfragen, später wieder")
    if not r.ok:
        raise VrmError(f"VRM-Fehler {r.status_code}")
    try:
        return r.json()
    except ValueError:
        raise VrmError("Ungültige Antwort von VRM")


def _parse(data: dict) -> list[dict]:
    rec = (data.get("records") or {}) if isinstance(data, dict) else {}
    rows = rec.get("solar_yield_forecast") if isinstance(rec, dict) else None
    out = []
    for row in rows or []:
        try:
            ts, val = row[0], row[1]
            if val is None:
                continue
            out.append({"ts": int(ts) // 1000, "wh": round(float(val), 1)})
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda x: x["ts"])


def fetch(c: dict, now: datetime | None = None) -> list[dict]:
    """Stuendliche Prognose (Wh je Stunde) ab heute 0 Uhr bis morgen Ende."""
    now = now or datetime.now()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    base = {"type": "custom", "attributeCodes[]": "solar_yield_forecast", "interval": "hours"}
    rows = _parse(_request(c, {**base, "start": int(day0.timestamp()),
                               "end": int((day0 + timedelta(days=2)).timestamp())}))
    if not rows:                                   # manche Installationen liefern nur ohne Zeitraum
        rows = _parse(_request(c, {"type": "custom", "attributeCodes[]": "solar_yield_forecast"}))
    return rows


def _summarize(rows: list[dict], now: datetime) -> dict:
    today, tomorrow = now.date(), (now + timedelta(days=1)).date()
    tot = {today: 0.0, tomorrow: 0.0}
    remaining = 0.0
    hours = []
    for r in rows:
        dt = datetime.fromtimestamp(r["ts"])
        if dt.date() in tot:
            tot[dt.date()] += r["wh"]
            hours.append({"ts": r["ts"], "day": "today" if dt.date() == today else "tomorrow",
                          "hour": dt.hour, "wh": r["wh"]})
        if dt.date() == today and dt + timedelta(hours=1) > now:
            remaining += r["wh"]
    return {"hours": hours, "today_kwh": round(tot[today] / 1000, 2),
            "tomorrow_kwh": round(tot[tomorrow] / 1000, 2),
            "remaining_today_kwh": round(remaining / 1000, 2)}


def forecast(force: bool = False) -> dict:
    """{'configured': bool, 'error': str|None, 'hours': [...], 'today_kwh', 'tomorrow_kwh', 'remaining_today_kwh', 'updated'}"""
    c = load_credentials()
    if not (c.get("token") and c.get("installation_id")):
        return {"configured": False, "error": None, "hours": []}
    with _lock:
        if not force and time.time() < _cache["until"]:
            return _cache["data"] or {"configured": True, "error": _cache["error"], "hours": []}
    now = datetime.now()
    try:
        data = _summarize(fetch(c, now), now)
        data.update(configured=True, error=None, updated=now.isoformat(timespec="seconds"))
        if not data["hours"]:
            data["error"] = "VRM liefert für diese Installation keine Prognose"
        with _lock:
            _cache.update(until=time.time() + CACHE_TTL_S, data=data, error=None)
        return data
    except VrmError as e:
        with _lock:
            old = _cache["data"]
            _cache.update(until=time.time() + ERROR_TTL_S, error=str(e))
        if old:                                    # alte Werte weiter zeigen, Fehler dazu melden
            return {**old, "error": str(e)}
        return {"configured": True, "error": str(e), "hours": []}
