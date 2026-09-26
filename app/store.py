"""
Persistenz: Config, interner State (PersistentState) und E-Auto-Ladetermine.
Alles als JSON neben der App.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
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
MONTHLY_PATH = os.path.join(_DIR, "monthly_summary.json")

_lock = threading.Lock()
log = logging.getLogger("store")

BACKUP_DIR = os.path.join(_DIR, "backups")
_BACKUP_KEEP_DAYS = 14


def _daily_backup(path: str):
    """Einmal pro Tag den Stand VOR dem ersten Schreiben sichern (nur wenn lesbar und nicht leer),
    14 Tage lang. Schutz gegen Datenverlust bei defekter/geleerter Statistikdatei."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 10:
            return
        name = os.path.basename(path)[:-5]
        os.makedirs(BACKUP_DIR, exist_ok=True)
        target = os.path.join(BACKUP_DIR, f"{name}-{datetime.now():%Y-%m-%d}.json")
        if os.path.exists(target):
            return
        with open(path, encoding="utf-8") as f:
            json.load(f)                                  # nur gueltige Dateien sichern
        shutil.copyfile(path, target)
        cutoff = datetime.now() - timedelta(days=_BACKUP_KEEP_DAYS)
        for fn in os.listdir(BACKUP_DIR):
            if fn.startswith(name + "-") and datetime.fromtimestamp(
                    os.path.getmtime(os.path.join(BACKUP_DIR, fn))) < cutoff:
                os.remove(os.path.join(BACKUP_DIR, fn))
    except (OSError, ValueError) as e:
        log.warning("Tagessicherung von %s fehlgeschlagen: %s", os.path.basename(path), e)


def _forced_backup(path: str, tag: str):
    """Sofortige Sicherung mit Zeitstempel (vor riskanten Aktionen)."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        name = os.path.basename(path)[:-5]
        shutil.copyfile(path, os.path.join(BACKUP_DIR, f"{name}-{tag}-{datetime.now():%Y%m%d-%H%M%S}.json"))
    except OSError as e:
        log.warning("Sicherung (%s) fehlgeschlagen: %s", tag, e)


def _dump_json(path: str, data, indent=2, backup: bool = False):
    """Atomar schreiben: erst in eine Zwischendatei, dann umbenennen. Ein Absturz/Neustart mitten
    im Schreiben hinterlaesst so nie eine halbe oder leere Datei."""
    with _lock:
        if backup:
            _daily_backup(path)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)


def _load_json_recovering(path: str, default):
    """Datei lesen. Ist sie unlesbar/leer: NICHT still durch Leerwerte ersetzen, sondern beiseitelegen,
    laut warnen und die juengste Tagessicherung zurueckholen (sonst default())."""
    if not os.path.exists(path):
        return default()
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        log.error("%s ist unlesbar (%s) - lege sie beiseite und versuche die Tagessicherung", os.path.basename(path), e)
    try:
        os.replace(path, f"{path}.corrupt-{datetime.now():%Y%m%d-%H%M%S-%f}")
    except OSError:
        pass
    name = os.path.basename(path)[:-5]
    try:
        cands = sorted(fn for fn in os.listdir(BACKUP_DIR) if fn.startswith(name + "-") and fn.endswith(".json"))
    except OSError:
        cands = []
    for fn in reversed(cands):
        try:
            with open(os.path.join(BACKUP_DIR, fn), encoding="utf-8") as f:
                data = json.load(f)
            log.warning("%s aus Sicherung %s wiederhergestellt", os.path.basename(path), fn)
            return data
        except (ValueError, OSError):
            continue
    return default()

# Pflichtfelder, damit der Wizard weiß, ob die App eingerichtet ist.
REQUIRED_KEYS = ("cerbo_host", "tibber_token")

CONFIG_DEFAULTS = {
    "cerbo_host": "",
    "cerbo_port": 502,
    "tibber_token": "",
    "dry_run": True,
    "poll_seconds": 300,
    "energy_sample_seconds": 10,   # eigener, feiner Takt für die Energie-Messung
    "manual_override": False,
    "web_port": 5005,
    "pv_inverters": [],            # [{"name": "...", "unit": 80}, ...] - einzeln benannte PV-Wechselrichter fuer die Live-Anzeige
}


# --- Config ---------------------------------------------------------------
def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    return cfg


def save_config(cfg: dict):
    _dump_json(CONFIG_PATH, cfg, indent=2)


def is_configured(cfg=None) -> bool:
    """Minimal nötig: Cerbo + eine gültige Strompreis-Quelle (Tibber-Token bei
    dynamischem Tarif, sonst ein fester Preis > 0). Die PV-Prognose kommt aus dem VRM
    (optional einzurichten) - ohne VRM rechnet die Steuerung mit dem Durchschnitt der letzten Tageserträge."""
    cfg = cfg or load_config()
    if not cfg.get("cerbo_host"):
        return False
    if cfg.get("tariff_mode") == "fixed":
        return bool(cfg.get("fixed_price_ct", 0) > 0)
    return bool(cfg.get("tibber_token"))


# --- Interner State -------------------------------------------------------
def load_state() -> PersistentState:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        base = PersistentState().__dict__
        return PersistentState(**{k: data[k] for k in data if k in base})
    return PersistentState()


def save_state(state: PersistentState):
    _dump_json(STATE_PATH, asdict(state), indent=2)


# --- E-Auto-Ladetermine ---------------------------------------------------
def _load_ev():
    if os.path.exists(EV_PATH):
        with open(EV_PATH, encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_ev(items):
    _dump_json(EV_PATH, items, indent=2)


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


def get_grid_correction(now: datetime | None = None) -> dict:
    """Manuelle Tages-Korrektur (kWh) fuer Netzbezug/-einspeisung, sofern
    heute eine gesetzt wurde (siehe set_grid_today)."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = {}
    if os.path.exists(ENERGY_PATH):
        try:
            with open(ENERGY_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except (ValueError, OSError):
            data = {}
    if data.get("stamp") != today:
        return {"import": 0.0, "export": 0.0}
    return {"import": data.get("import_adj", 0.0), "export": data.get("export_adj", 0.0)}


def set_grid_today(import_today, export_today, now=None):
    """Speichert eine Korrektur, damit die heutigen Netzwerte (Import/Export)
    den angegebenen Werten (z.B. aus der Victron-App) entsprechen. Die Korrektur
    wird als Aufschlag auf die selbst gemessene Tagessumme (energy_grid_today,
    aus den erfassten Leistungsfluessen) gespeichert - die Cerbo-Zaehlerregister
    zaehlen auf manchen Anlagen unzuverlaessig und werden dafuer nicht genutzt."""
    now = now or datetime.now()
    imp, exp = _raw_grid_sum(now)
    data = {
        "stamp": now.date().isoformat(),
        "import_adj": round(import_today - imp, 3),
        "export_adj": round(export_today - exp, 3),
    }
    _dump_json(ENERGY_PATH, data, indent=2)
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
    _dump_json(CHARGE_LOG_PATH, data, indent=2)


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
         "soc_sum": 0.0, "soc_n": 0, "grid_cost_ct": 0.0}
    for k in _FLOW_KEYS:
        b[k] = 0.0
    return b


def _load_history() -> dict:
    d = _load_json_recovering(HISTORY_PATH, lambda: {"hours": {}, "last": None})
    if not isinstance(d, dict):
        d = {"hours": {}, "last": None}
    d.setdefault("hours", {})
    d.setdefault("last", None)
    return d


_HISTORY_LOCK = threading.RLock()      # history.json wird gelesen-veraendert-geschrieben: Regeltakt und VRM-Import nie gleichzeitig


def log_energy_sample(system: dict | None, now: datetime | None = None,
                       price_ct: float | None = None):
    with _HISTORY_LOCK:
        _log_energy_sample(system, now, price_ct)


def _log_energy_sample(system: dict | None, now: datetime | None = None,
                        price_ct: float | None = None):
    """Integriert Momentanleistung zu Stunden-kWh auf: Verbrauch, Solar, die 7
    Energieflüsse (VRM-Stil), SOC (Min/Ø/Max) und - falls price_ct übergeben -
    die Netzbezugskosten (ct), fuer den Wochenrueckblick. Nur echte Messwerte.
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
    n_before = len(hours)
    prev_ts = (data.get("last") or {}).get("ts")
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
            if price_ct is not None:
                import_inc = ((flow_last["g_load"] + flow_now["g_load"]) / 2.0 / 1000.0 * h
                              + (flow_last["g_batt"] + flow_now["g_batt"]) / 2.0 / 1000.0 * h)
                b["grid_cost_ct"] = b.get("grid_cost_ct", 0.0) + import_inc * price_ct

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
            log.warning("history.json: unbekannter Schluessel %r bleibt erhalten", k)

    # Notbremse: schrumpft der Verlauf in EINEM Schritt auf weniger als die Haelfte, ohne dass eine
    # Zeitluecke (Ausfall/Uhrensprung) dahintersteckt, ist das ein Fehler - dann nicht speichern.
    try:
        gap_s = (now - datetime.fromisoformat(prev_ts)).total_seconds() if prev_ts else 0.0
    except ValueError:
        gap_s = 0.0
    if n_before >= 100 and len(hours) < n_before * 0.5:
        if abs(gap_s) <= 86400:
            log.error("history.json: Verlauf waere von %d auf %d Slots geschrumpft (ohne Zeitluecke) - "
                      "NICHT gespeichert, Sicherung bleibt erhalten", n_before, len(hours))
            return
        # Grosse Luecke (lange Ausfallzeit ODER falsche Systemuhr): pruefen wir nicht, sichern aber vorher
        log.warning("history.json: grosses Aufraeumen (%d -> %d Slots, Luecke %.0f h) - Sicherung vorab",
                    n_before, len(hours), gap_s / 3600)
        _forced_backup(HISTORY_PATH, "vorAufraeumen")
    _dump_json(HISTORY_PATH, data, indent=2, backup=True)


def history_keys() -> set:
    """Alle vorhandenen Viertelstunden-Slots (Schluessel) in history.json."""
    return set(_load_history().get("hours", {}).keys())


def import_history_slots(slots: dict) -> int:
    """Ergaenzt fehlende Viertelstunden (z. B. aus dem VRM). Vorhandene Slots werden NIE ueberschrieben;
    vorher wird eine Sicherung angelegt. Rueckgabe: Anzahl neu eingetragener Slots."""
    with _HISTORY_LOCK:
        data = _load_history()
        hours = data["hours"]
        new = {k: v for k, v in slots.items() if k not in hours}
        if not new:
            return 0
        if os.path.exists(HISTORY_PATH):
            _forced_backup(HISTORY_PATH, "vorVrmImport")
        hours.update(new)
        _dump_json(HISTORY_PATH, data, indent=2, backup=True)
        return len(new)


def _row_from_bucket(label: str, b: dict | None) -> dict:
    if b and (b.get("soc_n") or b.get("restored")):
        n = b.get("soc_n") or 0            # aus dem VRM nachgeholte Slots koennen ohne SOC sein
        row = {
            "hour": label,
            "verbrauch": round(b["verbrauch"], 3),
            "solar": round(b["solar"], 3),
            "soc_avg": round(b["soc_sum"] / n, 1) if n else None,
            "soc_min": round(b["soc_min"], 1) if b.get("soc_min") is not None else None,
            "soc_max": round(b["soc_max"], 1) if b.get("soc_max") is not None else None,
        }
        for k in _FLOW_KEYS:
            row[k] = round(b.get(k, 0.0), 3)
        row["grid_cost_ct"] = round(b.get("grid_cost_ct", 0.0), 2)
    else:
        row = {"hour": label, "verbrauch": 0.0, "solar": 0.0,
               "soc_avg": None, "soc_min": None, "soc_max": None, "grid_cost_ct": 0.0}
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
    # Manuelle Netz-Korrektur (falls fuer diesen Tag gesetzt) in den ersten
    # Slot einrechnen, damit sie in Kacheln und Chart tatsaechlich ankommt.
    if day == now.date().isoformat() and out:
        corr = get_grid_correction(now)
        if corr["import"] or corr["export"]:
            out[0] = dict(out[0])
            out[0]["g_load"] = out[0].get("g_load", 0.0) + corr["import"]
            out[0]["s_grid"] = out[0].get("s_grid", 0.0) + corr["export"]
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


def energy_week_summary(now: datetime | None = None, days: int = 7, offset_weeks: int = 0) -> dict:
    """Tagesweise Bilanz (Solar/Verbrauch/Netz/Kosten) einer 7-Tage-Woche fuer
    den Wochenrueckblick. offset_weeks=0 ist die aktuelle Woche (bis heute),
    1 die davor usw. - so bleibt die Statistik blaetterbar, statt beim naechsten
    Tag aus der Anzeige zu verschwinden (Rohdaten bleiben ohnehin
    _HISTORY_KEEP_DAYS Tage erhalten). Kosten sind so genau wie der Preis, der
    beim jeweiligen Sample gerade bekannt war (siehe log_energy_sample) - bei
    Tagen vor Einfuehrung dieser Auswertung fehlen sie und stehen als 0."""
    now = now or datetime.now()
    hours = _load_history().get("hours", {})
    end_day = now.date() - timedelta(days=days * offset_weeks)
    day_keys = [(end_day - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    per_day = {d: {"solar": 0.0, "verbrauch": 0.0, "import": 0.0, "export": 0.0, "cost_ct": 0.0}
               for d in day_keys}
    for k, b in hours.items():
        d = k[:10]
        if d not in per_day:
            continue
        row = per_day[d]
        row["solar"] += b.get("solar", 0.0)
        row["verbrauch"] += b.get("verbrauch", 0.0)
        row["import"] += b.get("g_load", 0.0) + b.get("g_batt", 0.0)
        row["export"] += b.get("s_grid", 0.0) + b.get("b_grid", 0.0)
        row["cost_ct"] += b.get("grid_cost_ct", 0.0)
    corr = get_grid_correction(now)
    today_iso = now.date().isoformat()
    if today_iso in per_day and (corr["import"] or corr["export"]):
        per_day[today_iso]["import"] += corr["import"]
        per_day[today_iso]["export"] += corr["export"]
    days_out = []
    totals = {"solar": 0.0, "verbrauch": 0.0, "import": 0.0, "export": 0.0, "cost_ct": 0.0}
    for d in day_keys:
        row = per_day[d]
        autarky = (round(max(0.0, min(100.0, (1 - row["import"] / row["verbrauch"]) * 100)), 0)
                   if row["verbrauch"] > 0 else None)
        days_out.append({
            "day": d,
            "solar": round(row["solar"], 2), "verbrauch": round(row["verbrauch"], 2),
            "import": round(row["import"], 2), "export": round(row["export"], 2),
            "cost_eur": round(row["cost_ct"] / 100.0, 2), "autarky": autarky,
        })
        for key in totals:
            totals[key] += row[key]
    total_autarky = (round(max(0.0, min(100.0, (1 - totals["import"] / totals["verbrauch"]) * 100)), 0)
                      if totals["verbrauch"] > 0 else None)
    min_day = energy_min_day()
    can_go_older = bool(min_day) and min_day < day_keys[0]
    return {
        "days": days_out,
        "totals": {"solar": round(totals["solar"], 2), "verbrauch": round(totals["verbrauch"], 2),
                   "import": round(totals["import"], 2), "export": round(totals["export"], 2),
                   "cost_eur": round(totals["cost_ct"] / 100.0, 2), "autarky": total_autarky},
        "offset_weeks": offset_weeks,
        "can_go_older": can_go_older,
    }


def _load_monthly() -> dict:
    d = _load_json_recovering(MONTHLY_PATH, lambda: {"months": {}, "days_archived": []})
    return d if isinstance(d, dict) else {"months": {}, "days_archived": []}


def _save_monthly(data: dict):
    _dump_json(MONTHLY_PATH, data, indent=2, backup=True)


def archive_finished_days(now: datetime | None = None):
    """Schreibt abgeschlossene Tage aus der (nur _HISTORY_KEEP_DAYS=35 Tage
    rollierenden) history.json dauerhaft in ein separates Monats-Archiv
    (monthly_summary.json) fort - damit der Monatsueberblick auch nach Jahren
    noch Daten hat, nicht nur die letzten 35 Tage. Jeder Tag wird GENAU EINMAL
    archiviert (days_archived-Liste als Schutz gegen Doppelzaehlung); wird bei
    jedem Regeltick UND beim Abruf des Monatsueberblicks aufgerufen (billig,
    kein Aufwand wenn nichts Neues zu archivieren ist)."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = _load_monthly()
    archived = set(data.get("days_archived", []))
    hours = _load_history().get("hours", {})
    per_day: dict[str, dict] = {}
    for k, b in hours.items():
        d = k[:10]
        if d >= today or d in archived:
            continue
        row = per_day.setdefault(d, {"solar": 0.0, "verbrauch": 0.0, "import": 0.0,
                                      "export": 0.0, "cost_ct": 0.0})
        row["solar"] += b.get("solar", 0.0)
        row["verbrauch"] += b.get("verbrauch", 0.0)
        row["import"] += b.get("g_load", 0.0) + b.get("g_batt", 0.0)
        row["export"] += b.get("s_grid", 0.0) + b.get("b_grid", 0.0)
        row["cost_ct"] += b.get("grid_cost_ct", 0.0)
    if not per_day:
        return
    months = data.setdefault("months", {})
    for d, row in per_day.items():
        m = months.setdefault(d[:7], {"solar": 0.0, "verbrauch": 0.0, "import": 0.0,
                                       "export": 0.0, "cost_ct": 0.0})
        for key in row:
            m[key] += row[key]
        archived.add(d)
    data["days_archived"] = sorted(archived)
    _save_monthly(data)


def monthly_overview(now: datetime | None = None, limit_months: int = 120) -> dict:
    """Monatsuebersicht - EINE Zeile pro Kalendermonat (Solar/Verbrauch/Netz/
    Autarkie/Kosten), wie die Summenzeile des Wochenrueckblicks, nur je Monat statt
    je Woche. Liest aus dem dauerhaften Archiv (monthly_summary.json, waechst nie
    ueber die 35-Tage-Grenze von history.json hinaus zurueck) plus dem laufenden,
    noch nicht archivierten aktuellen Monat live aus history.json dazugerechnet.
    limit_months=120 (10 Jahre) als grosszuegige Obergrenze - das Archiv selbst wird
    nie automatisch beschnitten."""
    now = now or datetime.now()
    archive_finished_days(now)   # sicherstellen, dass nichts Vergangenes fehlt
    data = _load_monthly()
    months = {k: dict(v) for k, v in data.get("months", {}).items()}
    cur_key = now.strftime("%Y-%m")
    hours = _load_history().get("hours", {})
    archived_days = set(data.get("days_archived", []))
    cur = dict(months.get(cur_key) or {"solar": 0.0, "verbrauch": 0.0, "import": 0.0,
                                       "export": 0.0, "cost_ct": 0.0})
    for k, b in hours.items():
        if k[:7] != cur_key or k[:10] in archived_days:      # archivierte Tage stecken schon in `cur`
            continue
        cur["solar"] += b.get("solar", 0.0)
        cur["verbrauch"] += b.get("verbrauch", 0.0)
        cur["import"] += b.get("g_load", 0.0) + b.get("g_batt", 0.0)
        cur["export"] += b.get("s_grid", 0.0) + b.get("b_grid", 0.0)
        cur["cost_ct"] += b.get("grid_cost_ct", 0.0)
    corr = get_grid_correction(now)
    if corr["import"] or corr["export"]:
        cur["import"] += corr["import"]
        cur["export"] += corr["export"]
    months[cur_key] = cur
    rows = []
    for key in sorted(months.keys(), reverse=True)[:limit_months]:
        m = months[key]
        autarky = (round(max(0.0, min(100.0, (1 - m["import"] / m["verbrauch"]) * 100)), 0)
                   if m["verbrauch"] > 0 else None)
        y, mo = key.split("-")
        rows.append({
            "month": key, "year": int(y), "month_num": int(mo),
            "solar": round(m["solar"], 2), "verbrauch": round(m["verbrauch"], 2),
            "import": round(m["import"], 2), "export": round(m["export"], 2),
            "cost_eur": round(m["cost_ct"] / 100.0, 2), "autarky": autarky,
        })
    return {"months": rows}


def energy_grid_today(now: datetime | None = None) -> dict:
    """Tages-Netzbezug/-Einspeisung aus der integrierten Netzleistung (nicht aus
    den kumulierten Zählerregistern, die unzuverlässig zählen). Aus/Zum Netz =
    Summe der heutigen Netz-Flüsse plus manuelle Korrektur (siehe set_grid_today).
    Reset um Mitternacht ergibt sich automatisch."""
    now = now or datetime.now()
    imp, exp = _raw_grid_sum(now)
    corr = get_grid_correction(now)
    return {"import": round(imp + corr["import"], 2), "export": round(exp + corr["export"], 2)}


def _raw_grid_sum(now: datetime) -> tuple:
    """Reine Tagessumme der gemessenen Netz-Fluesse, ohne manuelle Korrektur."""
    today = now.strftime("%Y-%m-%d")
    imp = exp = 0.0
    for k, b in _load_history().get("hours", {}).items():
        if k[:10] == today:
            imp += b.get("g_load", 0.0) + b.get("g_batt", 0.0)   # Netz→Verbrauch/Batterie
            exp += b.get("s_grid", 0.0) + b.get("b_grid", 0.0)   # Solar/Batterie→Netz
    return imp, exp


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


def solar_measured_today(now: datetime | None = None) -> float:
    """Oeffentlicher Zugriff auf den bisher heute real gemessenen Solarertrag
    (kWh) - fuer die Regelung (siehe webapp.tick(): loest die reine
    Tagesprognose ab, sobald ein Teil des Tages schon gemessen ist)."""
    now = now or datetime.now()
    return _solar_actual_for_day(now.date().isoformat())


# Akku gilt als "voll" (MPPT drosselt evtl. → Ertrag gedeckelt) ab diesem SOC.
_SOC_FULL_THRESHOLD = 99.0


def _solar_socmax_for_day(day: str):
    """Höchster erreichter Batterie-SOC des Tages (aus der History) oder None."""
    hours = _load_history().get("hours", {})
    vals = [b.get("soc_max") for k, b in hours.items()
            if k[:10] == day and b.get("soc_max") is not None]
    return max(vals) if vals else None


def _finalize_vrm(e: dict):
    """Abweichung der VRM-Prognose vom realen Ertrag: (real - Prognose) / Prognose."""
    vrm_kwh, actual = e.get("vrm_forecast"), e.get("actual")
    if vrm_kwh and actual is not None:
        e["vrm_deviation_pct"] = round((actual - vrm_kwh) / vrm_kwh * 100, 1)


def _restored_days() -> set:
    """Tage, fuer die history.json aus dem VRM nachgeholte Slots enthaelt."""
    return {k[:10] for k, b in _load_history().get("hours", {}).items() if b.get("restored")}


def _finalize_solar_days(days: dict, now: datetime):
    """Schließt vergangene Tage ab: realer Ertrag, Abweichung der VRM-Prognose, Akku-voll-Markierung
    (PV evtl. gedeckelt)."""
    today = now.date().isoformat()
    # Tage, deren Verlauf nachtraeglich aus dem VRM ergaenzt wurde (Datenverlust), EINMAL neu abschliessen -
    # sonst bleibt ein damals falsch (z. B. 0 kWh) festgehaltener Ertrag stehen.
    restored = _restored_days()
    for day, e in days.items():
        if day < today and day in restored and not e.get("vrm_refixed"):
            e["actual"] = None
            e["vrm_refixed"] = True
        if day < today and e.get("actual") is None:
            e["actual"] = _solar_actual_for_day(day)
            _finalize_vrm(e)
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
            if e.get("vrm_deviation_pct") is None:
                _finalize_vrm(e)


VRM_HISTORY_MIN_STEP = 0.1     # kWh: kleinere Schwankungen der VRM-Tagesprognose gelten nicht als Nachjustierung


def record_vrm_forecast(vrm_kwh: float | None, now: datetime | None = None):
    """Friert die VRM-Tagesprognose EINMAL pro Tag ein (erster Durchlauf des Tages; Grundlage der Abweichungs-
    Statistik), merkt sich daneben den laufenden Stand ("vrm_latest") und schließt vergangene Tage ab."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = _load_solar_log()
    days = data["days"]
    _finalize_solar_days(days, now)
    v = round(vrm_kwh, 2) if vrm_kwh and vrm_kwh > 0 else None
    if today not in days:
        if v is not None:
            days[today] = {"vrm_forecast": v, "vrm_deviation_pct": None, "actual": None}
    elif v is not None and days[today].get("vrm_forecast") is None:
        days[today]["vrm_forecast"] = v
    if v is not None and today in days:
        days[today]["vrm_latest"] = v            # laufender Stand (das VRM justiert nach) - nur fuer die Anzeige des heutigen Tages
        hist = days[today].setdefault("vrm_history", [])       # [[HH:MM, kWh], ...] - jede Aenderung ab 0,1 kWh
        if not hist or abs(hist[-1][1] - v) >= VRM_HISTORY_MIN_STEP:
            hist.append([now.strftime("%H:%M"), v])
            del hist[:-60]
    _dump_json(SOLAR_LOG_PATH, data, indent=2)


def solar_log(now: datetime | None = None) -> dict:
    """Logbuch-Einträge (neueste zuerst). Der heutige Tag erscheint mit dem bisherigen Ertrag
    als vorläufig."""
    now = now or datetime.now()
    today = now.date().isoformat()
    data = _load_solar_log()
    days = data["days"]
    _finalize_solar_days(days, now)
    rows = []
    for day in sorted(days.keys(), reverse=True):
        e = dict(days[day])
        e["date"] = day
        if day == today and e.get("actual") is None:
            e["actual"] = _solar_actual_for_day(day)
            e["provisional"] = True
        rows.append(e)
    return {"rows": rows}


def recent_solar_average(days: int = 7, now: datetime | None = None) -> float | None:
    """Mittlerer realer Tagesertrag (kWh) der letzten `days` vollständig aufgezeichneten Tage aus history.json.
    Rückfall der Steuerung, wenn das VRM keine Prognose liefert. None, wenn zu wenig Tage vorliegen."""
    now = now or datetime.now()
    today = now.date()
    want = {(today - timedelta(days=i)).isoformat() for i in range(1, days + 1)}
    slots: dict[str, int] = {}
    sums: dict[str, float] = {}
    for k, b in _load_history().get("hours", {}).items():
        d = k[:10]
        if d in want:
            slots[d] = slots.get(d, 0) + 1
            sums[d] = sums.get(d, 0.0) + b.get("solar", 0.0)
    vals = [sums[d] for d in want if slots.get(d, 0) >= 80]      # nur (fast) lückenlose Tage
    if len(vals) < 3:
        return None
    return round(sum(vals) / len(vals), 2)


# --- Preis-Historie (Tibber-Preise je Viertelstunde, lange aufbewahrt) --------------------------
# price_history.json: {"days": {"YYYY-MM-DD": {"p": [96 Werte in ct/kWh oder null], "src": "tibber" | "derived"}}}
# "tibber" = Originalpreise, "derived" = aus Bezugskosten/-menge im Verlauf zurueckgerechnet (nur Slots mit Netzbezug, lueckenhaft).
PRICE_HISTORY_PATH = os.path.join(_DIR, "price_history.json")
_PRICE_KEEP_DAYS = 800            # rund 2 Jahre; die Datei bleibt trotzdem klein (~0,5 MB)
_PRICE_LOCK = threading.Lock()
_price_cache: dict | None = None


def _price_days() -> dict:
    global _price_cache
    if _price_cache is None:
        d = _load_json_recovering(PRICE_HISTORY_PATH, lambda: {"days": {}})
        _price_cache = d.get("days", {}) if isinstance(d, dict) and isinstance(d.get("days"), dict) else {}
    return _price_cache


def _save_price_days(days: dict):
    for old in sorted(days)[:-_PRICE_KEEP_DAYS]:
        del days[old]
    _dump_json(PRICE_HISTORY_PATH, {"days": days}, indent=None, backup=True)


def record_prices(entries: list) -> int:
    """Haelt die Tibber-Preise fest (heute UND morgen, sobald sie da sind). Schreibt nur bei Aenderungen; ein Tag mit
    Originalpreisen wird nur ergaenzt bzw. korrigiert, nie durch weniger Daten ersetzt. Rueckgabe: Anzahl geaenderter Tage."""
    from logic import _parse_iso
    starts = []
    for e in entries or []:
        try:
            starts.append((_parse_iso(e["startsAt"]), round(float(e["total"]) * 100, 2)))
        except (KeyError, TypeError, ValueError):
            continue
    starts.sort()
    new: dict[str, list] = {}
    for i, (t, ct) in enumerate(starts):
        if i + 1 < len(starts):
            gap = (starts[i + 1][0] - t).total_seconds() / 60
        else:                                                       # letzter Eintrag: gleiche Dauer wie der davor
            gap = (t - starts[i - 1][0]).total_seconds() / 60 if i else 15
        n = 4 if gap >= 55 else 1                                  # Stundenpreis gilt fuer alle 4 Viertelstunden
        vals = new.setdefault(t.date().isoformat(), [None] * 96)
        base = t.hour * 4 + t.minute // 15
        for k in range(n):
            if base + k < 96:
                vals[base + k] = ct
    changed = 0
    with _PRICE_LOCK:
        days = _price_days()
        for day, vals in new.items():
            rec = days.get(day)
            if rec and rec.get("src") == "tibber":
                merged = [v if v is not None else o for v, o in zip(vals, rec["p"])]
                if merged == rec["p"]:
                    continue
                vals = merged
            days[day] = {"p": vals, "src": "tibber"}
            changed += 1
        if changed:
            _save_price_days(days)
    return changed


def backfill_prices_from_history() -> int:
    """Rechnet fuer Tage OHNE Tibber-Originalpreise aus dem Verlauf zurueck: Preis = Bezugskosten / Bezugsmenge (nur Slots mit
    Netzbezug). Idempotent; laeuft beim Start. Rueckgabe: Anzahl neu gefuellter Slots."""
    per_day: dict[str, dict[int, float]] = {}
    for key, b in _load_history().get("hours", {}).items():
        imp = b.get("g_load", 0.0) + b.get("g_batt", 0.0)
        cost = b.get("grid_cost_ct", 0.0)
        if imp > 0.005 and cost > 0 and len(key) >= 16:
            try:
                slot = int(key[11:13]) * 4 + int(key[14:16]) // 15
            except ValueError:
                continue
            per_day.setdefault(key[:10], {})[slot] = round(cost / imp, 2)
    filled = 0
    with _PRICE_LOCK:
        days = _price_days()
        for day, m in per_day.items():
            rec = days.get(day)
            if rec and rec.get("src") == "tibber":
                continue
            vals = list(rec["p"]) if rec else [None] * 96
            for slot, p in m.items():
                if vals[slot] is None:
                    vals[slot] = p
                    filled += 1
            days[day] = {"p": vals, "src": "derived"}
        if filled:
            _save_price_days(days)
    return filled


def price_history_info() -> dict:
    with _PRICE_LOCK:
        days = _price_days()
        real = sum(1 for r in days.values() if r.get("src") == "tibber")
        return {"days": len(days), "tibber_days": real, "derived_days": len(days) - real,
                "first": min(days) if days else None, "last": max(days) if days else None}


def price_history(days: int = 90) -> dict:
    with _PRICE_LOCK:
        d = _price_days()
        return {k: d[k] for k in sorted(d)[-days:]}


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
    _dump_json(WATCHDOG_PATH, data, indent=2)
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


# --- Ladeplan-Simulation: Tages-Schnappschuss fuers Vergleichen ueber mehrere Tage ---------------
PLANSIM_LOG_PATH = os.path.join(_DIR, "plansim_log.json")


def _win_text(ws: list) -> str:
    return ", ".join(f"{w['from']}-{w['to']}" for w in ws)


def record_plansim(now: datetime, res: dict):
    """Haelt pro Tag EINMAL (ab 14 Uhr, wenn die Preise von morgen bekannt sind) fest, was Simulation und bisherige
    Steuerung laut Modell gekostet haetten. Nach ein paar Tagen zeigt das, welcher Ansatz besser ist."""
    if now.hour < 14 or not res:
        return
    data = _load_json_recovering(PLANSIM_LOG_PATH, lambda: {"days": {}})
    days = data.setdefault("days", {})
    day = now.date().isoformat()
    if day in days:
        return
    days[day] = {"horizon_end": res["horizon_end"],
                 "sim_net": res["sim"]["net_ct"], "current_net": res["current"]["net_ct"], "none_net": res["none"]["net_ct"],
                 "sim_charge_kwh": res["sim"]["grid_charge_kwh"], "current_charge_kwh": res["current"]["grid_charge_kwh"],
                 "sim_windows": _win_text(res["sim"]["windows"]), "current_windows": _win_text(res["current"]["windows"])}
    for old in sorted(days)[:-60]:
        del days[old]
    _dump_json(PLANSIM_LOG_PATH, data, indent=2)


def plansim_log(limit: int = 30) -> list:
    data = _load_json_recovering(PLANSIM_LOG_PATH, lambda: {"days": {}})
    days = data.get("days", {}) if isinstance(data, dict) else {}
    return [{"date": d, **days[d]} for d in sorted(days, reverse=True)[:limit]]


# --- VRM-Stundenprognose je Tag aufheben (das VRM zeigt sie nach Tagesende nicht mehr) -----------------
FORECAST_HOURS_PATH = os.path.join(_DIR, "forecast_hours.json")
FORECAST_KEEP_DAYS = 800


def record_forecast_hours(vrm_data: dict | None, now: datetime | None = None):
    """Merkt sich je Tag die stuendliche VRM-Prognose (Solar + Verbrauch). Der Folgetag wird bei jedem Abruf aktualisiert;
    sobald der Tag laeuft, bleibt der zuletzt gemerkte Stand stehen (so sieht man spaeter, was vorhergesagt war)."""
    if not vrm_data or not vrm_data.get("hours"):
        return
    now = now or datetime.now()
    keys = {"today": now.date().isoformat(), "tomorrow": (now.date() + timedelta(days=1)).isoformat()}
    data = _load_json_recovering(FORECAST_HOURS_PATH, lambda: {"days": {}})
    days = data.setdefault("days", {})
    changed = False
    frozen = {(keys["today"], f) for f in ("solar", "cons") if (days.get(keys["today"]) or {}).get(f)}   # laufender Tag: schon gemerkt
    for src, field in ((vrm_data, "solar"), (vrm_data.get("cons") or {}, "cons")):
        for h in src.get("hours") or []:
            day = keys.get(h.get("day"))
            if day is None or (day, field) in frozen:
                continue
            e = days.setdefault(day, {})
            e.setdefault(field, {})[str(int(h["hour"]))] = round(float(h["wh"]), 1)
            changed = True
    if not changed:
        return
    for old in sorted(days)[:-FORECAST_KEEP_DAYS]:
        del days[old]
    _dump_json(FORECAST_HOURS_PATH, data, indent=None)


def forecast_hours_for_day(day: str) -> dict:
    """{'solar': {stunde: Wh}, 'cons': {stunde: Wh}} des gemerkten Tages (leer, wenn nichts gemerkt)."""
    data = _load_json_recovering(FORECAST_HOURS_PATH, lambda: {"days": {}})
    e = (data.get("days", {}) if isinstance(data, dict) else {}).get(day) or {}
    return {"solar": e.get("solar") or {}, "cons": e.get("cons") or {}}
