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
        start = dt.replace(minute=0, second=0, microsecond=0)
        if start.date() == today and start + timedelta(hours=1) > now:
            # laufende Stunde nur anteilig (der schon vergangene Teil steckt in der Messung)
            remaining += r["wh"] * min(1.0, (start + timedelta(hours=1) - now).total_seconds() / 3600)
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


# ---------------------------------------------------------------- Verlauf (Energieflüsse) aus dem VRM
# VRM-Kürzel der 7 Energiepfade (type=kwh) -> unsere Schlüssel in history.json
KWH_CODES = {"Pc": "s_load", "Pb": "s_batt", "Pg": "s_grid", "Gc": "g_load", "Gb": "g_batt",
             "Bc": "b_load", "Bg": "b_grid"}
CHUNK_DAYS = 7


def _slot_key(dt: datetime) -> str:
    return f"{dt:%Y-%m-%dT%H}:{(dt.minute // 15) * 15:02d}"


def _points(rec, code) -> list[tuple[int, float]]:
    out = []
    for row in (rec.get(code) or []) if isinstance(rec, dict) else []:
        try:
            if row[1] is not None:
                out.append((int(row[0]) // 1000, float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _spread(points, hourly: bool, divide: bool = True) -> dict[str, float]:
    """Punkte -> {Slot-Schlüssel: Wert}. Stundenwerte kommen auf alle 4 Viertelstunden
    (kWh werden dabei geteilt, Prozentwerte wie der SOC nur kopiert)."""
    out: dict[str, float] = {}
    for ts, v in points:
        dt = datetime.fromtimestamp(ts)
        if hourly:
            base = dt.replace(minute=0, second=0, microsecond=0)
            for q in range(4):
                out[_slot_key(base + timedelta(minutes=15 * q))] = v / 4 if divide else v
        else:
            out[_slot_key(dt)] = v
    return out


def fetch_flow_slots(c: dict, start: datetime, end: datetime) -> tuple[dict[str, dict], set[str]]:
    """Energieflüsse (kWh je Viertelstunde) aus dem VRM für [start, end).
    Rückgabe: ({Slot: {flow: kWh}}, benutzte Auflösungen). Bevorzugt 15-Min-Werte, sonst Stundenwerte."""
    slots: dict[str, dict] = {}
    used: set[str] = set()
    t = start
    while t < end:
        t2 = min(end, t + timedelta(days=CHUNK_DAYS))
        for interval in ("15mins", "hours"):
            data = _request(c, {"type": "kwh", "interval": interval,
                                "start": int(t.timestamp()), "end": int(t2.timestamp())})
            rec = data.get("records") if isinstance(data, dict) else None
            series = {code: _points(rec, code) for code in KWH_CODES} if isinstance(rec, dict) else {}
            if not any(series.values()):
                continue
            for code, pts in series.items():
                for key, v in _spread(pts, interval == "hours").items():
                    slots.setdefault(key, {})[KWH_CODES[code]] = v
            used.add(interval)
            break
        t = t2
    return slots, used


def fetch_soc_slots(c: dict, start: datetime, end: datetime) -> dict[str, float]:
    """Batterie-SOC (%) je Viertelstunde - optional; bei jedem Fehler bleibt der SOC im Verlauf einfach leer."""
    out: dict[str, float] = {}
    try:
        t = start
        while t < end:
            t2 = min(end, t + timedelta(days=CHUNK_DAYS))
            for interval in ("15mins", "hours"):
                data = _request(c, {"type": "custom", "attributeCodes[]": "bs", "interval": interval,
                                    "start": int(t.timestamp()), "end": int(t2.timestamp())})
                pts = _points((data or {}).get("records") or {}, "bs")
                if pts:
                    out.update(_spread(pts, interval == "hours", divide=False))
                    break
            t = t2
    except (VrmError, AttributeError, TypeError):
        pass
    return out


# ---------------------------------------------------------------- Tages-Solarertrag laut VRM (Vergleich im Solarlogbuch)
_daily_cache: dict = {"until": 0.0, "data": {}}
DAILY_TTL_S = 6 * 3600


def daily_solar() -> dict[str, float]:
    """Vom VRM gemessener Solarertrag je Tag (kWh, ISO-Datum), letzte 35 Tage. 6 h zwischengespeichert,
    leer ohne Zugang oder bei Fehlern (das Logbuch funktioniert dann einfach ohne diese Zeile)."""
    c = load_credentials()
    if not (c.get("token") and c.get("installation_id")):
        return {}
    with _lock:
        if time.time() < _daily_cache["until"]:
            return dict(_daily_cache["data"])
    now = datetime.now()
    start = (now - timedelta(days=34)).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        data = _request(c, {"type": "kwh", "interval": "days", "start": int(start.timestamp()),
                            "end": int((now + timedelta(days=1)).timestamp())})
        rec = data.get("records") if isinstance(data, dict) else None
        out: dict[str, float] = {}
        for code in ("Pc", "Pb", "Pg"):                      # Solar = zum Verbrauch + zur Batterie + ins Netz
            for ts, v in _points(rec, code):
                d = datetime.fromtimestamp(ts).date().isoformat()
                out[d] = round(out.get(d, 0.0) + v, 2)
        with _lock:
            _daily_cache.update(until=time.time() + DAILY_TTL_S, data=out)
        return dict(out)
    except VrmError:
        with _lock:
            _daily_cache["until"] = time.time() + ERROR_TTL_S
            return dict(_daily_cache["data"])


# ---------------------------------------------------------------- VRM als Steuerquelle (mit Rueckfall auf Open-Meteo)
MAX_AGE_H = 3          # aeltere Prognosewerte (z. B. nach laengerem VRM-Ausfall) gelten nicht mehr


def control_forecast(vf: dict, now: datetime, measured_today_kwh: float) -> tuple[dict | None, str | None]:
    """Prueft, ob die VRM-Prognose zur Steuerung taugt.
    Rueckgabe: ({'today_kwh': gemessen + Rest laut VRM, 'tomorrow_kwh': ...|None}, None) oder (None, Grund)."""
    if not vf or not vf.get("configured"):
        return None, None                                   # kein VRM eingerichtet -> stiller Rueckfall
    hours = vf.get("hours") or []
    why = f" ({vf['error']})" if vf.get("error") else ""
    try:
        upd = datetime.fromisoformat(vf["updated"])
    except (KeyError, ValueError, TypeError):
        return None, "PV-Prognose (VRM) nicht verfügbar" + why + " – Rückfall auf Open-Meteo"
    if upd.date() != now.date() or now - upd > timedelta(hours=MAX_AGE_H) or not any(h["day"] == "today" for h in hours):
        return None, "PV-Prognose (VRM) veraltet" + why + " – Rückfall auf Open-Meteo"
    tom = vf["tomorrow_kwh"] if any(h["day"] == "tomorrow" for h in hours) else None
    return {"today_kwh": round(measured_today_kwh + vf["remaining_today_kwh"], 2), "tomorrow_kwh": tom}, None
