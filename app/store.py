"""
Persistenz: Config, interner State (PersistentState) und E-Auto-Ladetermine.
Alles als JSON neben der App.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict
from datetime import datetime

from logic import PersistentState

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "config.json")
STATE_PATH = os.path.join(_DIR, "state.json")
EV_PATH = os.path.join(_DIR, "ev_schedules.json")
ENERGY_PATH = os.path.join(_DIR, "energy.json")
CHARGE_LOG_PATH = os.path.join(_DIR, "charge_log.json")
HISTORY_PATH = os.path.join(_DIR, "history.json")

_lock = threading.Lock()

# Pflichtfelder, damit der Wizard weiß, ob die App eingerichtet ist.
REQUIRED_KEYS = ("cerbo_host", "tibber_token", "pv_latitude", "pv_longitude", "pv_planes")

CONFIG_DEFAULTS = {
    "cerbo_host": "",
    "cerbo_port": 502,
    "tibber_token": "",
    "pv_latitude": None,
    "pv_longitude": None,
    "pv_planes": [],
    "dry_run": True,
    "poll_seconds": 300,
    "manual_override": False,
    "web_port": 5005,
}


# --- Config ---------------------------------------------------------------
def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: dict):
    with _lock, open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def is_configured(cfg=None) -> bool:
    """Minimal nötig: Cerbo + Tibber. PV-Anlage (Standort/Flächen) ist optional –
    ohne PV rechnet die Steuerung einfach mit 0 kWh Prognose."""
    cfg = cfg or load_config()
    return bool(cfg.get("cerbo_host") and cfg.get("tibber_token"))


# --- Interner State -------------------------------------------------------
def load_state() -> PersistentState:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        base = PersistentState().__dict__
        return PersistentState(**{k: data[k] for k in data if k in base})
    return PersistentState()


def save_state(state: PersistentState):
    with _lock, open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(asdict(state), f, indent=2, ensure_ascii=False)


# --- E-Auto-Ladetermine ---------------------------------------------------
def _load_ev():
    if os.path.exists(EV_PATH):
        with open(EV_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_ev(items):
    with _lock, open(EV_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def cleanup_ev(now: datetime | None = None):
    """Löscht Termine, deren End-Tag vorbei ist (also am Folgetag um 00:00).
    Ein heute abgelaufener Termin bleibt bis Mitternacht als 'abgelaufen'
    sichtbar und verschwindet dann automatisch."""
    now = now or datetime.now()
    today = now.date()
    items = _load_ev()
    kept = []
    for i in items:
        try:
            end = datetime.fromisoformat(i["end"])
        except (ValueError, KeyError):
            continue  # kaputte Einträge entfernen
        if end.date() >= today:
            kept.append(i)
    if len(kept) != len(items):
        _save_ev(kept)
    return kept


def list_ev():
    return cleanup_ev()


def add_ev(start_iso, end_iso, note=""):
    items = _load_ev()
    entry = {"id": uuid.uuid4().hex[:8], "start": start_iso,
             "end": end_iso, "note": note, "enabled": True}
    items.append(entry)
    _save_ev(items)
    return entry


def delete_ev(eid):
    items = _load_ev()
    new = [i for i in items if i["id"] != eid]
    if len(new) == len(items):
        return False
    _save_ev(new)
    return True


def toggle_ev(eid, enabled):
    items = _load_ev()
    for i in items:
        if i["id"] == eid:
            i["enabled"] = bool(enabled)
            _save_ev(items)
            return i
    return None


def grid_today(import_total, export_total, now: datetime | None = None):
    """Tages-Netzwerte aus den kumulierten Zählern (kWh).
    Merkt sich den Zählerstand um Mitternacht (persistent) und gibt die Differenz
    seit heute 00:00 zurück - genau wie die Victron-App."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = {}
    if os.path.exists(ENERGY_PATH):
        try:
            with open(ENERGY_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            data = {}
    # Neuer Tag ODER Zählerrücksetzung (z.B. Cerbo-Neustart) -> Baseline neu setzen
    reset = (data.get("stamp") != today
             or import_total < data.get("import_base", 0)
             or export_total < data.get("export_base", 0))
    if reset:
        data = {"stamp": today, "import_base": import_total, "export_base": export_total}
        with _lock, open(ENERGY_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    return {
        "import": round(max(0.0, import_total - data["import_base"]), 2),
        "export": round(max(0.0, export_total - data["export_base"]), 2),
    }


def set_grid_today(import_today, export_today, import_total, export_total, now=None):
    """Setzt die Tages-Basis so, dass die heutigen Netzwerte den angegebenen
    Werten entsprechen (z.B. aus der Victron-App übernommen). Danach zählt die
    App vom Cerbo-Gesamtzähler korrekt weiter."""
    now = now or datetime.now()
    data = {
        "stamp": now.date().isoformat(),
        "import_base": round(import_total - import_today, 3),
        "export_base": round(export_total - export_today, 3),
    }
    with _lock, open(ENERGY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return {"import": round(import_today, 2), "export": round(export_today, 2)}


def _load_charge():
    if os.path.exists(CHARGE_LOG_PATH):
        try:
            with open(CHARGE_LOG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            pass
    return {}


def log_charge_state(is_charging, strategy, now=None):
    """Protokolliert automatische Ladevorgänge: öffnet eine Session, wenn geladen
    wird, und schließt sie, wenn nicht mehr. Reset um Mitternacht (per Tages-Stempel)."""
    now = now or datetime.now()
    today = now.date().isoformat()
    ts = now.isoformat(timespec="minutes")
    data = _load_charge()
    if data.get("stamp") != today:
        data = {"stamp": today, "sessions": [], "open": None}
    open_s = data.get("open")
    if is_charging and not open_s:
        data["open"] = {"start": ts, "strategy": strategy}
    elif not is_charging and open_s:
        data["sessions"].append({"start": open_s["start"], "end": ts,
                                 "strategy": open_s.get("strategy", "")})
        data["open"] = None
    with _lock, open(CHARGE_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def list_charge_sessions(now=None):
    now = now or datetime.now()
    data = _load_charge()
    if data.get("stamp") != now.date().isoformat():
        return {"sessions": [], "open": None}
    return {"sessions": data.get("sessions", []), "open": data.get("open")}


# --- Energie-Verlauf (Stundenwerte, nur echte Messwerte) ------------------
# history.json: {"hours": {"YYYY-MM-DDTHH": {verbrauch, solar, soc_min,
# soc_max, soc_sum, soc_n}}, "last": {"ts", "cons_w", "solar_w"}}
_HISTORY_KEEP_HOURS = 48          # rollierend, alte Stunden werden verworfen
_MAX_SAMPLE_GAP_S = 900           # Lücken (z.B. nach Downtime) auf 15 min kappen


def _load_history() -> dict:
    if os.path.exists(HISTORY_PATH):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("hours", {})
            d.setdefault("last", None)
            return d
        except (ValueError, OSError):
            pass
    return {"hours": {}, "last": None}


def log_energy_sample(system: dict | None, now: datetime | None = None):
    """Integriert Momentanleistung (Verbrauch/Solar) zu Stunden-kWh auf und
    erfasst SOC (Min/Ø/Max) je Stunde. Nur echte Messwerte, keine Prognose.
    Wird bei jedem Regelzyklus aufgerufen."""
    if not system:
        return
    now = now or datetime.now()
    try:
        cons_w = float(system["loads"]["total"])
        solar_w = float(system["solar_total"])
        soc = float(system["battery"]["soc"])
    except (KeyError, TypeError, ValueError):
        return

    data = _load_history()
    hours = data["hours"]
    hour_key = now.strftime("%Y-%m-%dT%H")
    b = hours.setdefault(hour_key, {"verbrauch": 0.0, "solar": 0.0,
                                    "soc_min": soc, "soc_max": soc,
                                    "soc_sum": 0.0, "soc_n": 0})

    # Energie via Trapez zwischen letztem und aktuellem Sample
    last = data.get("last")
    if last:
        try:
            dt_s = (now - datetime.fromisoformat(last["ts"])).total_seconds()
        except (ValueError, KeyError):
            dt_s = 0.0
        if 0 < dt_s <= _MAX_SAMPLE_GAP_S:
            h = dt_s / 3600.0
            b["verbrauch"] += (last["cons_w"] + cons_w) / 2.0 / 1000.0 * h
            b["solar"] += (last["solar_w"] + solar_w) / 2.0 / 1000.0 * h

    # SOC-Statistik (jedes Sample zählt)
    b["soc_min"] = min(b["soc_min"], soc)
    b["soc_max"] = max(b["soc_max"], soc)
    b["soc_sum"] += soc
    b["soc_n"] += 1

    data["last"] = {"ts": now.isoformat(timespec="seconds"),
                    "cons_w": cons_w, "solar_w": solar_w}

    # Rollierend alte Stunden verwerfen
    keep_from = now.timestamp() - _HISTORY_KEEP_HOURS * 3600
    for k in list(hours.keys()):
        try:
            if datetime.fromisoformat(k + ":00:00").timestamp() < keep_from:
                del hours[k]
        except ValueError:
            del hours[k]

    with _lock, open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def energy_history_today(now: datetime | None = None) -> list:
    """Stundenwerte des heutigen Tages (0 bis aktuelle Stunde) für das Chart."""
    now = now or datetime.now()
    hours = _load_history().get("hours", {})
    today = now.strftime("%Y-%m-%d")
    out = []
    for h in range(0, now.hour + 1):
        key = f"{today}T{h:02d}"
        b = hours.get(key)
        if b and b.get("soc_n"):
            out.append({
                "hour": f"{h:02d}:00",
                "verbrauch": round(b["verbrauch"], 3),
                "solar": round(b["solar"], 3),
                "soc_avg": round(b["soc_sum"] / b["soc_n"], 1),
                "soc_min": round(b["soc_min"], 1),
                "soc_max": round(b["soc_max"], 1),
            })
        else:
            out.append({"hour": f"{h:02d}:00", "verbrauch": 0.0, "solar": 0.0,
                        "soc_avg": None, "soc_min": None, "soc_max": None})
    return out


def active_ev(now: datetime | None = None):
    """Gibt den aktuell laufenden E-Auto-Termin zurück (oder None)."""
    now = now or datetime.now()
    for i in _load_ev():
        if not i.get("enabled"):
            continue
        try:
            s = datetime.fromisoformat(i["start"])
            e = datetime.fromisoformat(i["end"])
        except (ValueError, KeyError):
            continue
        if s <= now < e:
            return i
    return None
