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
from datetime import datetime, timedelta

from logic import PersistentState

_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_DIR, "config.json")
STATE_PATH = os.path.join(_DIR, "state.json")
EV_PATH = os.path.join(_DIR, "ev_schedules.json")
ENERGY_PATH = os.path.join(_DIR, "energy.json")
CHARGE_LOG_PATH = os.path.join(_DIR, "charge_log.json")
HISTORY_PATH = os.path.join(_DIR, "history.json")
SOLAR_LOG_PATH = os.path.join(_DIR, "solar_log.json")
WATCHDOG_PATH = os.path.join(_DIR, "battery_watchdog.json")

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
    "energy_sample_seconds": 10,   # eigener, feiner Takt für die Energie-Messung
    "manual_override": False,
    "web_port": 5005,
    "openmeteo_pr": 0.68,          # Performance Ratio der Open-Meteo-Steuerprognose
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


def stop_ev(eid, now: datetime | None = None):
    """Beendet einen bereits laufenden Termin sofort (setzt Ende = jetzt).
    Das bis dahin Geladene bleibt über das Lade-Protokoll in den Ladevorgängen
    erhalten. Gibt None zurück, wenn der Termin nicht existiert oder noch nicht
    gestartet ist (dann wäre Löschen der richtige Weg)."""
    now = now or datetime.now()
    ts = now.isoformat(timespec="minutes")
    items = _load_ev()
    for i in items:
        if i["id"] == eid:
            try:
                start = datetime.fromisoformat(i["start"])
                end = datetime.fromisoformat(i["end"])
            except (ValueError, KeyError):
                return None
            if start > now or end <= now:
                return None  # nicht laufend
            i["end"] = ts
            _save_ev(items)
            return i
    return None


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


# --- Energie-Verlauf (15-Min-Slots, nur echte Messwerte) ------------------
# history.json: {"hours": {"YYYY-MM-DDTHH:MM": {verbrauch, solar, 7 Flüsse,
#   soc_min, soc_max, soc_sum, soc_n}}, "last": {ts, pv, load, grid, bc, bd}}
# (Schlüssel "hours" historisch beibehalten, enthält jetzt Viertelstunden-Slots.)
_HISTORY_KEEP_DAYS = 35           # rollierend, ältere Tage werden verworfen
_MAX_SAMPLE_GAP_S = 900           # Lücken (z.B. nach Downtime) auf 15 min kappen


def _slot_key(dt: datetime) -> str:
    """Viertelstunden-Slot-Schlüssel, z.B. 2026-07-22T13:15."""
    m = (dt.minute // 15) * 15
    return f"{dt:%Y-%m-%dT%H}:{m:02d}"

# Die 7 Energieflüsse (wie Victron VRM). Werte in W bzw. aufintegriert in kWh.
_FLOW_KEYS = ("s_load", "s_batt", "s_grid", "b_load", "b_grid", "g_load", "g_batt")


def decompose_flows(pv, load, grid_import, grid_export, batt_charge, batt_discharge):
    """Zerlegt die Momentanleistungen in die 7 Pfade (greedy, feste Priorität):
    PV deckt zuerst Verbrauch, dann Batterie, dann Netz-Einspeisung.
    Rest-Verbrauch aus Batterie, dann Netz. Batterie-Ladung aus PV, dann Netz.
    Alle Rückgaben >= 0. Einheit = Einheit der Eingaben."""
    pv = max(0.0, pv); load = max(0.0, load)
    gi = max(0.0, grid_import); ge = max(0.0, grid_export)
    bc = max(0.0, batt_charge); bd = max(0.0, batt_discharge)

    s_load = min(pv, load);          pv -= s_load;  load -= s_load
    s_batt = min(pv, bc);            pv -= s_batt;  bc -= s_batt
    s_grid = min(pv, ge);            pv -= s_grid;  ge -= s_grid

    b_load = min(bd, load);          bd -= b_load;  load -= b_load
    b_grid = min(bd, ge);            bd -= b_grid;  ge -= b_grid

    g_load = min(gi, load);          gi -= g_load;  load -= g_load
    g_batt = min(gi, bc);            gi -= g_batt;  bc -= g_batt

    return {"s_load": s_load, "s_batt": s_batt, "s_grid": s_grid,
            "b_load": b_load, "b_grid": b_grid, "g_load": g_load, "g_batt": g_batt}


def _powers_from_system(system: dict) -> dict:
    """Momentanleistungen (W) aus read_system(), konsistent zur VRM-Darstellung.

    - Verbrauch = loads.total (AC-Verbrauch, = VRM „Gesamtverbrauch").
    - Batterie-Fluss wird als REST der AC-Energiebilanz abgeleitet, nicht aus dem
      DC-Register 842. So landen AC↔DC-Wandlungsverluste (beim Netzladen) korrekt
      bei „Netz zur Batterie" und werden nicht dem Verbrauch zugeschlagen.
      net = PV + Netz − Verbrauch  →  >0 laden, <0 entladen."""
    pv = float(system["solar_total"])
    grid = float(system["grid"]["total"])           # + Bezug / − Einspeisung
    load = max(0.0, float(system["loads"]["total"]))
    net = pv + grid - load
    bc = max(0.0, net)
    bd = max(0.0, -net)
    return {"pv": pv, "load": load, "grid": grid, "bc": bc, "bd": bd}


def _flows_from_system(system: dict) -> dict:
    """Momentane Flüsse (W) aus einem read_system()-Dict."""
    p = _powers_from_system(system)
    return decompose_flows(p["pv"], p["load"], max(0.0, p["grid"]),
                           max(0.0, -p["grid"]), p["bc"], p["bd"])


def _new_bucket(soc: float) -> dict:
    b = {"verbrauch": 0.0, "solar": 0.0, "soc_min": soc, "soc_max": soc,
         "soc_sum": 0.0, "soc_n": 0}
    for k in _FLOW_KEYS:
        b[k] = 0.0
    return b


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
    """Integriert Momentanleistung zu Stunden-kWh auf: Verbrauch, Solar, die 7
    Energieflüsse (VRM-Stil) und SOC (Min/Ø/Max). Nur echte Messwerte.
    Wird bei jedem Regelzyklus aufgerufen."""
    if not system:
        return
    now = now or datetime.now()
    try:
        p = _powers_from_system(system)
        pv_w, load_w = p["pv"], p["load"]
        soc = float(system["battery"]["soc"])
        flow_now = decompose_flows(p["pv"], p["load"], max(0.0, p["grid"]),
                                   max(0.0, -p["grid"]), p["bc"], p["bd"])
    except (KeyError, TypeError, ValueError):
        return

    data = _load_history()
    hours = data["hours"]
    slot_key = _slot_key(now)
    b = hours.get(slot_key) or _new_bucket(soc)
    hours[slot_key] = b

    # Energie via Trapez zwischen letztem und aktuellem Sample
    last = data.get("last")
    if last:
        try:
            dt_s = (now - datetime.fromisoformat(last["ts"])).total_seconds()
        except (ValueError, KeyError):
            dt_s = 0.0
        if 0 < dt_s <= _MAX_SAMPLE_GAP_S:
            h = dt_s / 3600.0
            b["verbrauch"] += (last["load"] + load_w) / 2.0 / 1000.0 * h
            b["solar"] += (last["pv"] + pv_w) / 2.0 / 1000.0 * h
            # Flüsse: Momentanzerlegung an beiden Stützstellen, trapezförmig
            flow_last = decompose_flows(last["pv"], last["load"],
                                        max(0.0, last["grid"]), max(0.0, -last["grid"]),
                                        last["bc"], last["bd"])
            for k in _FLOW_KEYS:
                b[k] += (flow_last[k] + flow_now[k]) / 2.0 / 1000.0 * h

    # SOC-Statistik (jedes Sample zählt)
    b["soc_min"] = min(b["soc_min"], soc)
    b["soc_max"] = max(b["soc_max"], soc)
    b["soc_sum"] += soc
    b["soc_n"] += 1

    data["last"] = {"ts": now.isoformat(timespec="seconds"),
                    "pv": p["pv"], "load": p["load"], "grid": p["grid"],
                    "bc": p["bc"], "bd": p["bd"]}

    # Rollierend alte Slots verwerfen
    keep_from = now.timestamp() - _HISTORY_KEEP_DAYS * 86400
    for k in list(hours.keys()):
        try:
            if datetime.fromisoformat(k).timestamp() < keep_from:
                del hours[k]
        except ValueError:
            del hours[k]

    with _lock, open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _row_from_bucket(label: str, b: dict | None) -> dict:
    if b and b.get("soc_n"):
        row = {
            "hour": label,
            "verbrauch": round(b["verbrauch"], 3),
            "solar": round(b["solar"], 3),
            "soc_avg": round(b["soc_sum"] / b["soc_n"], 1),
            "soc_min": round(b["soc_min"], 1),
            "soc_max": round(b["soc_max"], 1),
        }
        for k in _FLOW_KEYS:
            row[k] = round(b.get(k, 0.0), 3)
    else:
        row = {"hour": label, "verbrauch": 0.0, "solar": 0.0,
               "soc_avg": None, "soc_min": None, "soc_max": None}
        for k in _FLOW_KEYS:
            row[k] = 0.0
    return row


def energy_history_for_day(day: str, now: datetime | None = None) -> list:
    """Alle 96 15-Min-Slots eines Tages (00:00–23:45, YYYY-MM-DD). Feste
    Zeitachse – noch nicht erfasste Slots kommen als Leerwerte zurück."""
    now = now or datetime.now()
    try:
        d0 = datetime.strptime(day, "%Y-%m-%d")
    except (ValueError, TypeError):
        return []
    hours = _load_history().get("hours", {})
    end = d0.replace(hour=23, minute=45)
    t = d0.replace(hour=0, minute=0)
    out = []
    while t <= end:
        out.append(_row_from_bucket(f"{t:%H:%M}", hours.get(_slot_key(t))))
        t += timedelta(minutes=15)
    return out


def energy_history_today(now: datetime | None = None) -> list:
    """15-Min-Werte des heutigen Tages (00:00 bis aktueller Slot) für die Charts."""
    now = now or datetime.now()
    return energy_history_for_day(now.strftime("%Y-%m-%d"), now)


def energy_min_day() -> str | None:
    """Frühester Tag, für den Verlaufsdaten vorliegen (YYYY-MM-DD) oder None."""
    hours = _load_history().get("hours", {})
    days = {k[:10] for k in hours.keys() if len(k) >= 10}
    return min(days) if days else None


def energy_grid_today(now: datetime | None = None) -> dict:
    """Tages-Netzbezug/-Einspeisung aus der integrierten Netzleistung (nicht aus
    den kumulierten Zählerregistern, die unzuverlässig zählen). Aus/Zum Netz =
    Summe der heutigen Netz-Flüsse. Reset um Mitternacht ergibt sich automatisch."""
    now = now or datetime.now()
    today = now.strftime("%Y-%m-%d")
    imp = exp = 0.0
    for k, b in _load_history().get("hours", {}).items():
        if k[:10] == today:
            imp += b.get("g_load", 0.0) + b.get("g_batt", 0.0)   # Netz→Verbrauch/Batterie
            exp += b.get("s_grid", 0.0) + b.get("b_grid", 0.0)   # Solar/Batterie→Netz
    return {"import": round(imp, 2), "export": round(exp, 2)}


# --- Solar-Logbuch (Prognose vs. reale Erzeugung) -------------------------
def _load_solar_log() -> dict:
    if os.path.exists(SOLAR_LOG_PATH):
        try:
            with open(SOLAR_LOG_PATH, encoding="utf-8") as f:
                d = json.load(f)
            d.setdefault("days", {})
            return d
        except (ValueError, OSError):
            pass
    return {"days": {}}


def _solar_actual_for_day(day: str) -> float:
    """Realer Tagesertrag (kWh) aus der integrierten PV-Leistung."""
    hours = _load_history().get("hours", {})
    return round(sum(b.get("solar", 0.0) for k, b in hours.items() if k[:10] == day), 2)


# Akku gilt als "voll" (MPPT drosselt evtl. → Ertrag gedeckelt) ab diesem SOC.
_SOC_FULL_THRESHOLD = 99.0


def _solar_socmax_for_day(day: str):
    """Höchster erreichter Batterie-SOC des Tages (aus der History) oder None."""
    hours = _load_history().get("hours", {})
    vals = [b.get("soc_max") for k, b in hours.items()
            if k[:10] == day and b.get("soc_max") is not None]
    return max(vals) if vals else None


# PR alter Tage (vor der PR-Kalibrierung lief Open-Meteo mit 0,85).
_OLD_OM_PR = 0.85


def _finalize_om_pr(e: dict):
    """Vorschlags-PR (Open-Meteo, Steuerquelle) = genutzte PR · real/Prognose."""
    om = e.get("om_forecast")
    actual = e.get("actual")
    pr_used = e.get("pr", _OLD_OM_PR)
    if om and actual is not None:
        e["om_deviation_pct"] = round((actual - om) / om * 100, 1)
        e["om_suggested_pr"] = round(pr_used * actual / om, 2)


def _finalize_solar_days(days: dict, now: datetime):
    """Schließt vergangene Tage ab: realer Ertrag, Abweichung und Vorschlagswerte
    für beide Quellen. Markiert Tage mit vollem Akku (PV evtl. gedeckelt)."""
    today = now.date().isoformat()
    for day, e in days.items():
        if day < today and e.get("actual") is None:
            actual = _solar_actual_for_day(day)
            e["actual"] = actual
            # forecast.solar (nur Vergleich)
            raw = e.get("forecast_raw") or 0.0
            corr = e.get("forecast_corr") or 0.0
            e["deviation_pct"] = round((actual - corr) / corr * 100, 1) if corr else None
            e["suggested_factor"] = round(actual / raw, 2) if raw else None
            # Open-Meteo (Steuerquelle): Abweichung + Vorschlags-PR
            _finalize_om_pr(e)
            smax = _solar_socmax_for_day(day)
            e["soc_max"] = round(smax, 1) if smax is not None else None
            e["curtailed"] = bool(smax is not None and smax >= _SOC_FULL_THRESHOLD)
        elif day < today:
            # Nachrüstung für Tage, die vor neuen Features finalisiert wurden.
            if "curtailed" not in e:
                smax = _solar_socmax_for_day(day)
                if smax is not None:
                    e["soc_max"] = round(smax, 1)
                    e["curtailed"] = bool(smax >= _SOC_FULL_THRESHOLD)
            if e.get("om_suggested_pr") is None:
                _finalize_om_pr(e)


def record_solar_forecast(om_kwh: float | None, pr: float,
                          now: datetime | None = None,
                          fs_raw: float | None = None, fs_corr: float | None = None,
                          fs_factor: float | None = None):
    """Friert die Tages-Prognose EINMAL pro Tag ein und finalisiert vergangene Tage.
    Primärquelle = Open-Meteo (om_kwh mit Performance Ratio pr, steuert die Anlage);
    forecast.solar (fs_*) läuft nur als Vergleich mit. Überschreibt einen bereits
    eingefrorenen Tag nicht, trägt aber eine anfangs fehlende Quelle einmal nach."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = _load_solar_log()
    days = data["days"]
    _finalize_solar_days(days, now)
    om = round(om_kwh, 2) if om_kwh and om_kwh > 0 else None
    if today not in days:
        if om is None and not fs_raw:
            return                      # noch keine einzige Quelle -> nicht einfrieren
        days[today] = {
            "om_forecast": om, "pr": round(pr, 3),
            "om_deviation_pct": None, "om_suggested_pr": None,
            "forecast_raw": round(fs_raw, 2) if fs_raw else None,
            "forecast_corr": round(fs_corr, 2) if fs_corr else None,
            "factor": round(fs_factor, 2) if fs_factor else None,
            "actual": None, "deviation_pct": None, "suggested_factor": None,
        }
    else:
        e = days[today]
        if om is not None and e.get("om_forecast") is None:
            e["om_forecast"] = om
            e["pr"] = round(pr, 3)
        if fs_raw and e.get("forecast_raw") is None:
            e["forecast_raw"] = round(fs_raw, 2)
            e["forecast_corr"] = round(fs_corr, 2) if fs_corr else None
            e["factor"] = round(fs_factor, 2) if fs_factor else None
    with _lock, open(SOLAR_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def solar_log(now: datetime | None = None) -> dict:
    """Logbuch-Einträge (neueste zuerst) + eine gerollte Faktor-Empfehlung aus
    den letzten abgeschlossenen Tagen. Der heutige Tag erscheint mit dem
    bisherigen Ertrag als vorläufig."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = _load_solar_log()
    days = data["days"]
    _finalize_solar_days(days, now)
    rows = []
    for day in sorted(days.keys(), reverse=True):
        e = dict(days[day])
        e["date"] = day
        # Heute ist vorläufig, SOLANGE kein Ist-Wert manuell gesetzt wurde. Ein
        # eingetragener actual (z.B. Handeintrag am Tagesende) bleibt stehen.
        if day == today and e.get("actual") is None:
            e["actual"] = _solar_actual_for_day(day)
            e["provisional"] = True
        rows.append(e)
    # Empfehlung: Median der Vorschlags-PR (Open-Meteo) der letzten 14 fertigen Tage.
    # Tage mit vollem Akku (PV evtl. gedeckelt) fließen NICHT ein – ihr Ertrag liegt
    # unter dem Potenzial und würde die Empfehlung verzerren.
    recent = [days[d] for d in sorted(days.keys(), reverse=True)
              if d < today and days[d].get("om_suggested_pr")][:14]
    curtailed_days = sum(1 for e in recent if e.get("curtailed"))
    finals = [e["om_suggested_pr"] for e in recent if not e.get("curtailed")]
    suggestion = None
    if finals:
        s = sorted(finals)
        n = len(s)
        suggestion = round((s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2), 2)
    return {"rows": rows, "suggestion": suggestion, "days_used": len(finals),
            "curtailed_days": curtailed_days}


def energy_grid_charge_buckets(day: str) -> dict:
    """{slot_key 'YYYY-MM-DDTHH:MM': gemessene Netz→Batterie-kWh} eines Tages.
    Basis für die tatsächliche (statt geschätzte) Lademenge in den Ladevorgängen."""
    hours = _load_history().get("hours", {})
    return {k: round(b.get("g_batt", 0.0), 4) for k, b in hours.items() if k[:10] == day}


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


# --- Batterie-Watchdog ------------------------------------------------------
# Erkennt den Multiplus-Ladealgorithmus-Haenger vom 01.09.2026 (siehe Projekt-
# Notiz): Batterie laedt/entlaedt praktisch nicht (|Strom| < 0.5 A), obwohl
# gleichzeitig ein nennenswerter Netzfluss da ist (>150 W) - normalerweise
# wuerde die Batterie mithelfen. Reine Erkennung + Protokollierung, KEIN
# automatischer Eingriff (ein manueller ESS-Mode-Befehl blieb beim echten
# Vorfall wirkungslos, nur ein physischer Reset half).
def _load_watchdog() -> dict:
    if os.path.exists(WATCHDOG_PATH):
        with open(WATCHDOG_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"active": False, "since": None, "notified": False,
            "last_detail": None, "events": []}


def battery_watchdog_update(is_frozen: bool, now: datetime, detail: dict,
                            threshold_min: float = 15.0) -> dict:
    """Fuehrt die Zustandsverfolgung fort und persistiert sie. Gibt zurueck,
    ob der Aufrufer gerade jetzt warnen ('just_warned') bzw. Entwarnung geben
    soll ('just_resolved', mit Episoden-Details oder None)."""
    data = _load_watchdog()
    result = {"just_warned": False, "just_resolved": None}
    if is_frozen:
        if not data.get("active"):
            data["active"] = True
            data["since"] = now.isoformat(timespec="seconds")
            data["notified"] = False
        data["last_detail"] = detail
        since = datetime.fromisoformat(data["since"])
        duration_min = (now - since).total_seconds() / 60.0
        if not data.get("notified") and duration_min >= threshold_min:
            data["notified"] = True
            result["just_warned"] = True
        result["duration_min"] = round(duration_min, 1)
    else:
        if data.get("active"):
            since = datetime.fromisoformat(data["since"])
            duration_min = (now - since).total_seconds() / 60.0
            if data.get("notified"):
                event = {"start": data["since"], "end": now.isoformat(timespec="seconds"),
                         "duration_min": round(duration_min, 1), "detail": data.get("last_detail")}
                events = [event] + data.get("events", [])
                data["events"] = events[:50]
                result["just_resolved"] = event
        data["active"] = False
        data["since"] = None
        data["notified"] = False
        data["last_detail"] = None
    with _lock, open(WATCHDOG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return result


def battery_watchdog_state() -> dict:
    """Fuer die UI/API: aktueller Zustand + juengste abgeschlossene Episoden."""
    data = _load_watchdog()
    status = {"active": bool(data.get("active")), "since": data.get("since"),
              "detail": data.get("last_detail")}
    if status["active"] and status["since"]:
        try:
            since = datetime.fromisoformat(status["since"])
            status["duration_min"] = round((datetime.now() - since).total_seconds() / 60.0, 1)
        except ValueError:
            status["duration_min"] = None
    return {"status": status, "events": data.get("events", [])[:20]}
