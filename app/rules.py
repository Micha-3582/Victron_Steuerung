"""
Regel-Engine fuer Geraete (Shelly, Tasmota, Tuya).

Eine Regel gehoert zu EINEM Geraet und hat zwei Teile:
  "Einschalten, wenn"   Bedingungen (UND): Sind alle erfuellt, wird das Geraet eingeschaltet.
  "Ausschalten, wenn"   Bedingungen (ODER, optional): Trifft eine zu, wird es ausgeschaltet.
                        Ohne Ausschalt-Bedingung schaltet das Geraet aus, sobald die Einschalt-Bedingungen nicht mehr stimmen.
Eine Regel ohne Einschalt-Bedingung ist ein reiner Ausschalt-Timer: sie schaltet das Geraet aus, wenn eine Ausschalt-Bedingung zutrifft
(auch wenn es von Hand eingeschaltet wurde) und schaltet nie von selbst ein.
Nach einem Ausschalten durch eine Ausschalt-Bedingung ist die Regel gesperrt, bis ihre Einschalt-Bedingungen einmal nicht mehr galten
(sonst wuerde sie sofort wieder einschalten). Mehrere Regeln pro Geraet sind moeglich (eine reicht zum Einschalten).

Bedingungen:
  time          Zwischen von und bis Uhr (optional nur an bestimmten Wochentagen)
  price         Strompreis unter/ueber X ct
  budget        Laufzeit pro Tag (frueher auch 'guenstigste Stunden'): X Minuten pro Tag. Einschalten: laeuft zu den guenstigsten Zeiten, bis das Ziel erreicht ist (dann aus).
                Ausschalten: trifft zu, sobald das Geraet heute X Minuten gelaufen ist.
  soc           Akku ueber/unter X %
  at            Um HH:MM Uhr, einmal pro Tag (loest innerhalb von 30 Min nach der Uhrzeit aus). Beim Einschalten bleibt das Geraet danach
                an, bis eine Ausschalt-Bedingung zutrifft.
  sun_tomorrow  Sonne morgen (VRM-Prognose) ueber/unter X kWh
  sensor        Homematic-Sensor (siehe homematic.py): Messwerte (Temperatur, Luftfeuchte, Helligkeit, Leistung) unter/ueber X,
                Ja/Nein-Sensoren (Fenster offen, Bewegung, Anwesenheit) TRUE oder FALSE. Ohne lesbaren Wert gilt die Bedingung als nicht erfuellt.

Regeln sind vollstaendig unabhaengig von der PV-Ueberschuss-Automatik (surplus.py): eigener Hauptschalter, eigener Trockenlauf, eigene Einstellungen,
eigenes Logbuch. Ein Geraet gehoert entweder zur Ueberschuss-Automatik oder zu Regeln (wird beim Speichern geprueft).

Sicherheit: ausgeschaltet wird nur, was die Engine selbst eingeschaltet oder uebernommen hat (Besitzer-Merkung, ueberlebt Neustarts). Laeuft ein Geraet,
waehrend die Einschalt-Bedingungen einer Regel stimmen, uebernimmt die Regel es (Timer-Verhalten); von Hand ueber die App geschaltete Geraete
bleiben fuer eine Weile in Ruhe; Shelly bekommen einen Rueckschalt-Timer (wie bisher).

Alles hier ist reine Logik ohne Netzwerk (testbar). Geschaltet wird vom Aufrufer (webapp.py).
"""
from __future__ import annotations

import json
import math
import os
import threading
import uuid
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(_DIR, "rules.json")
STATE_PATH = os.path.join(_DIR, "rules_state.json")

TYPES = ("time", "price", "cheapest", "budget", "soc", "sun_tomorrow", "at", "sensor", "device", "virtual")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
VERSION = 2


class RuleError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ Speicher
def _store():
    import store                       # spaet importieren (store importiert nichts von hier)
    return store


def _upgrade(d: dict) -> dict:
    """Aeltere Fassung (Version 1: 'conditions' inkl. 'surplus') auf Version 2 heben. Ueberschuss-Regeln werden zu einem
    'auto'-Flag am Geraet (siehe pending_auto), die uebrigen Bedingungen zu 'Einschalten, wenn'."""
    if d.get("version") == VERSION:
        return d
    rules, pending = [], list(d.get("pending_auto") or [])
    for r in d.get("rules", []):
        conds = r.pop("conditions", None)
        if conds is not None:
            if any(c.get("type") == "surplus" for c in conds):
                if r.get("device_id") and r["device_id"] not in pending:
                    pending.append(r["device_id"])
            r["on"] = [c for c in conds if c.get("type") != "surplus"]
            r["off"] = []
        if r.get("on"):
            rules.append(r)
    return {"version": VERSION, "rules": rules, "pending_auto": pending}


def load() -> dict:
    d = _store()._load_json_recovering(RULES_PATH, lambda: {"version": VERSION, "rules": []})
    if not isinstance(d, dict) or not isinstance(d.get("rules"), list):
        d = {"version": VERSION, "rules": []}
    if d.get("version") != VERSION:
        d = _upgrade(d)
        _save(d)
    if any(c.get("type") == "cheapest" for r in d["rules"] for c in (r.get("on") or []) + (r.get("off") or [])):
        for r in d["rules"]:                         # alte Bedingung "guenstigste Stunden" -> "Laufzeit pro Tag"
            for k in ("on", "off"):
                r[k] = [normalize_condition(c) if c.get("type") == "cheapest" else c for c in r.get(k) or []]
        _save(d)
    return d


def _save(d: dict):
    _store()._dump_json(RULES_PATH, d, indent=2)


def list_rules() -> list[dict]:
    return load()["rules"]


def pending_auto(clear: bool = True) -> list[str]:
    """Geraete, die bei der Umstellung von Version 1 in die Ueberschuss-Automatik gehoeren (auto=True setzen)."""
    d = load()
    ids = list(d.get("pending_auto") or [])
    if ids and clear:
        d["pending_auto"] = []
        _save(d)
    return ids


def enabled(cfg: dict) -> bool:
    """Hauptschalter (aus der frueheren Ueberschuss-Automatik uebernommen, solange der neue Schluessel fehlt)."""
    return bool(cfg.get("rules_enabled", False))


def dry_run(cfg: dict) -> bool:
    return bool(cfg.get("rules_dry_run", True))


# Einstellungen der Regeln (eigene Werte, unabhaengig von der Ueberschuss-Automatik)
DEFAULTS = {"manual_hold_min": 60, "failsafe_min": 10}
BOUNDS = {"manual_hold_min": (0, 1440), "failsafe_min": (0, 120)}


def settings(cfg: dict) -> dict:
    """Pause nach Handschaltung und Sicherheits-Timer (Shelly) der Regeln, mit Standard und Grenzen (ungueltig -> Standard)."""
    out = {}
    for k, default in DEFAULTS.items():
        lo, hi = BOUNDS[k]
        try:
            v = float(cfg.get("rules_" + k, default))
        except (TypeError, ValueError):
            v = default
        out[k] = min(hi, max(lo, v))
    if 0 < out["failsafe_min"] < 2:
        out["failsafe_min"] = 2
    return out


# ------------------------------------------------------------------------------------------ Pruefen
def _hhmm(v, what):
    try:
        h, m = str(v).split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m):
            raise ValueError
    except (ValueError, TypeError):
        raise RuleError(f"{what}: Uhrzeit im Format HH:MM erwartet")
    return f"{h:02d}:{m:02d}"


def _num(v, lo, hi, what):
    try:
        x = float(str(v).replace(",", "."))
    except (ValueError, TypeError):
        raise RuleError(f"{what}: Zahl erwartet")
    if not (lo <= x <= hi):
        raise RuleError(f"{what}: zwischen {lo:g} und {hi:g}")
    return x


def normalize_condition(c: dict) -> dict:
    t = c.get("type")
    if t not in TYPES:
        raise RuleError("Unbekannte Bedingung")
    if t == "cheapest":                              # entfallen: "N guenstigste Stunden" = "Laufzeit pro Tag" ueber den ganzen Tag
        return {"type": "budget", "minutes": int(_num(c.get("hours"), 1, 23, "Stunden")) * 60, "from": "00:00", "to": "24:00"}
    if t == "at":
        days = sorted({int(x) for x in (c.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        return {"type": t, "time": _hhmm(c.get("time", "23:30"), "Uhrzeit"), "days": days if len(days) < 7 else []}
    if t == "time":
        days = sorted({int(x) for x in (c.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        return {"type": t, "from": _hhmm(c.get("from", "00:00"), "Von"), "to": _hhmm(c.get("to", "24:00"), "Bis"), "days": days if len(days) < 7 else []}
    if t == "virtual":                               # eigener Schalter/Knopf (virtual.py)
        vid = str(c.get("id") or "").strip()
        if not vid:
            raise RuleError("Eigenen Schalter wählen")
        st = str(c.get("is", "on")).strip().lower()
        if st not in ("pressed", "on", "off"):
            raise RuleError("Eigener Schalter: gedrückt, an oder aus")
        return {"type": t, "id": vid, "is": st}
    if t == "device":                                # Zustand/Leistung eines anderen Geraets
        did = str(c.get("device_id") or "").strip()
        if not did:
            raise RuleError("Gerät wählen")
        if c.get("op") in ("below", "above"):
            return {"type": t, "device_id": did, "op": c["op"], "value": _num(c.get("value"), 0, 100000, "Leistung (W)")}
        return {"type": t, "device_id": did, "is": "on" if str(c.get("is", "on")).strip().lower() in ("on", "1", "true", "an") else "off"}
    if t == "sensor":
        sid = str(c.get("sensor_id") or "").strip()
        if not sid:
            raise RuleError("Sensor wählen")
        if c.get("is") is not None:                  # Ja/Nein-Sensor: TRUE oder FALSE
            val = c["is"]
            if isinstance(val, str):
                val = val.strip().lower() in ("1", "true", "wahr", "ja")
            return {"type": t, "sensor_id": sid, "is": bool(val)}
        op = c.get("op", "below")
        if op not in ("below", "above"):
            raise RuleError("Vergleich: unter oder über")
        return {"type": t, "sensor_id": sid, "op": op, "value": _num(c.get("value"), -1000, 100000, "Sensor-Wert")}
    if t in ("price", "soc", "sun_tomorrow"):
        op = c.get("op", "below")
        if op not in ("below", "above"):
            raise RuleError("Vergleich: unter oder über")
        if t == "price":
            return {"type": t, "op": op, "ct": _num(c.get("ct"), -100, 200, "Preis (ct)")}
        if t == "soc":
            return {"type": t, "op": op, "pct": _num(c.get("pct"), 0, 100, "Akku (%)")}
        return {"type": t, "op": op, "kwh": _num(c.get("kwh"), 0, 500, "Sonne morgen (kWh)")}
    if t == "cheapest":
        return {"type": t, "hours": int(_num(c.get("hours"), 1, 23, "Stunden"))}
    return {"type": "budget", "minutes": int(_num(c.get("minutes"), 15, 1440, "Minuten pro Tag")),
            "from": _hhmm(c.get("from", "00:00"), "Von"), "to": _hhmm(c.get("to", "24:00"), "Bis")}


STEP_TYPES = ("switch", "toggle", "wait", "setpoint", "notify", "virtual", "lock", "wol")
MAX_WAIT_S = 7 * 86400


def _normalize_action(a: dict, flow: bool = False) -> dict:
    """Ein Schritt von DANN/SONST. 'switch' (Standard, ohne type-Feld gespeichert) geht in beiden Regelarten, die uebrigen nur im Ablauf."""
    if not isinstance(a, dict):
        raise RuleError("Aktion ungültig")
    t = str(a.get("type") or "switch")
    if t not in STEP_TYPES:
        raise RuleError("Unbekannte Aktion")
    if t != "switch" and not flow:
        raise RuleError("Warten, Sollwert, Nachricht und eigene Schalter gehen nur bei Regeln der Art „Ablauf“")
    if t == "switch":
        if not a.get("device_id"):
            raise RuleError("Aktion: Gerät wählen")
        st = str(a.get("state", "on")).strip().lower()
        if st not in ("on", "off"):
            raise RuleError("Aktion: einschalten oder ausschalten")
        return {"device_id": str(a["device_id"]), "state": st}
    if t == "wol":                                   # Wake-on-LAN: Rechner aufwecken (wol.py)
        wid = str(a.get("id") or "").strip()
        if not wid:
            raise RuleError("Rechner zum Aufwecken wählen")
        return {"type": t, "id": wid}
    if t == "lock":                                  # Tuerschloss (Homematic IP): verriegeln / entriegeln / oeffnen - Freigabe wird beim Speichern geprueft
        lid = str(a.get("id") or "").strip()
        st = str(a.get("state", "lock")).strip().lower()
        if not lid or st not in ("lock", "unlock", "open"):
            raise RuleError("Türschloss: Schloss und Aktion wählen")
        return {"type": t, "id": lid, "state": st}
    if t == "toggle":                                # Geraet umschalten: laeuft es, wird es ausgeschaltet - sonst eingeschaltet
        if not a.get("device_id"):
            raise RuleError("Aktion: Gerät wählen")
        return {"type": t, "device_id": str(a["device_id"])}
    if t == "wait":
        return {"type": t, "seconds": int(_num(a.get("seconds"), 1, MAX_WAIT_S, "Wartezeit (Sekunden)"))}
    if t == "setpoint":
        ids = [str(i) for i in (a.get("device_ids") or []) if str(i).strip()]
        if not ids:
            raise RuleError("Thermostate wählen")
        return {"type": t, "device_ids": ids, "value": _num(a.get("value"), 0, 40, "Temperatur (°C)")}
    if t == "notify":
        text = str(a.get("text") or "").strip()
        if not text:
            raise RuleError("Nachricht: Text eingeben")
        return {"type": t, "text": text[:500]}
    vid = str(a.get("id") or "").strip()
    st = str(a.get("state", "on")).strip().lower()
    if not vid or st not in ("on", "off", "press", "toggle"):
        raise RuleError("Eigener Schalter: Schalter und Aktion wählen")
    return {"type": t, "id": vid, "state": st}


def _is_switch(a: dict) -> bool:
    return a.get("type", "switch") == "switch"


def _states(r: dict) -> dict[str, tuple]:
    """Geraet -> (Zustand bei DANN, Zustand bei SONST); None = in dem Zweig nichts."""
    out: dict[str, list] = {}
    for i, key in enumerate(("then", "else")):
        for a in r.get(key) or []:
            if _is_switch(a):
                out.setdefault(a["device_id"], [None, None])[i] = a["state"]
    return {k: tuple(v) for k, v in out.items()}


def _group(mode: str, conds: list, neg: bool = False) -> dict:
    return {"type": "group", "mode": mode, "not": neg, "conds": conds}


STATEFUL = ("at", "budget")          # merken sich Tageszustand je Regel - gehen nur als einfache Liste, nicht in verschachtelten Gruppen


def compile_rule(r: dict) -> list[dict]:
    """Editor-Regel (WENN / DANN / SONST) in die Regeln fuer die Engine uebersetzen (je Geraet eine). Alte Regeln (on/off) bleiben unveraendert.
    DANN an + SONST aus = 'Einschalten, wenn' mit Selbst-Ausschalten; nur DANN an = bleibt an (nie automatisch aus); nur DANN aus = reiner
    Ausschalt-Timer; SONST gilt, wenn die Bedingung NICHT zutrifft (fehlende Daten: dann passiert weder DANN noch SONST)."""
    if "when" not in r:
        return [r]
    if r.get("mode") == "flow":                          # Ablauf-Regeln fuehrt flows.py aus, nicht diese Engine
        return []
    mode, conds = r["when"]["mode"], r["when"]["conds"]
    pos_on = conds if mode == "all" else [_group("any", conds)]
    pos_off = conds if (mode == "any" or len(conds) == 1) else [_group("all", conds)]
    neg = [_group(mode, conds, True)]
    never = [{"type": "never"}]
    states = _states(r)
    out = []
    for dev_id, (t, e) in states.items():
        if t == "on" and e == "off":
            on, off = pos_on, []
        elif t == "off" and e == "on":
            on, off = neg, []
        elif t == "on":
            on, off = pos_on, never
        elif e == "on":
            on, off = neg, never
        elif t == "off":
            on, off = [], pos_off
        else:
            on, off = [], neg
        for lst in (on, off):
            for c in lst:
                if c["type"] == "group" and any(x["type"] in STATEFUL for x in c["conds"]):
                    raise RuleError("„Uhrzeit (einmal pro Tag)“ und „Laufzeit pro Tag“ gehen nur in einfachen Regeln: nur UND-Bedingungen, ohne SONST für andere Geräte und ohne Umkehrung")
        cid = r["id"] if len(states) == 1 else f"{r['id']}~{dev_id}"
        out.append({"id": cid, "name": r.get("name", ""), "device_id": dev_id, "enabled": r.get("enabled", True), "on": on, "off": off, "src": r["id"]})
    return out


def compile_all(items: list[dict]) -> list[dict]:
    out = []
    for r in items:
        try:
            out.extend(compile_rule(r))
        except RuleError:
            continue                                    # nicht uebersetzbare Regel nie ausfuehren (beim Speichern wird sie ohnehin abgelehnt)
    return out


def devices_of(r: dict) -> list[str]:
    """Geraete, die eine Regel schaltet."""
    if "when" not in r:
        return [r["device_id"]] if r.get("device_id") else []
    seen: list[str] = []
    for a in (r.get("then") or []) + (r.get("else") or []):
        if a.get("type", "switch") in ("switch", "toggle") and a["device_id"] not in seen:
            seen.append(a["device_id"])
    return seen


def all_conditions(r: dict) -> list[dict]:
    return list(r["when"]["conds"]) if "when" in r else (r.get("on") or []) + (r.get("off") or [])


def to_new(r: dict) -> list[dict]:
    """Alte Regel (Einschalten/Ausschalten) in der Editor-Form anzeigen (WENN / DANN / SONST). Mit beiden Teilen werden es zwei Regeln."""
    if "when" in r:
        return [r]
    dev, on, off = r["device_id"], r.get("on") or [], r.get("off") or []
    base = {"name": r.get("name", ""), "device_id": dev, "enabled": r.get("enabled", True)}
    act = lambda s: [{"device_id": dev, "state": s}]       # noqa: E731
    if on and not off:
        return [{**base, "id": r["id"], "when": {"mode": "all", "conds": on}, "then": act("on"), "else": act("off")}]
    if off and not on:
        return [{**base, "id": r["id"], "when": {"mode": "any", "conds": off}, "then": act("off"), "else": []}]
    return [{**base, "id": r["id"], "when": {"mode": "all", "conds": on}, "then": act("on"), "else": []},
            {**base, "id": r["id"] + "b", "when": {"mode": "any", "conds": off}, "then": act("off"), "else": []}]


def rollup_status(status: dict) -> dict:
    """Engine-Status (je Geraet einer Regel) auf die Regel zusammenfassen: 'laeuft' gewinnt vor Pause vor aus."""
    rank = {"on": 3, "paused": 2, "off": 1, "disabled": 0}
    out: dict[str, dict] = {}
    for cid, st in status.items():
        rid = cid.split("~")[0]
        if rid not in out or rank.get(st.get("state"), 0) > rank.get(out[rid].get("state"), 0):
            out[rid] = st
    return out


def normalize_rule(body: dict, rule_id: str | None = None) -> dict:
    if "when" in body:
        w = body.get("when") or {}
        mode = "any" if w.get("mode") == "any" else "all"
        conds = [normalize_condition(c) for c in (w.get("conds") or [])]
        if not conds:
            raise RuleError("Mindestens eine Bedingung (WENN) angeben")
        flow = body.get("mode") == "flow"
        then = [_normalize_action(a, flow) for a in (body.get("then") or [])]
        other = [_normalize_action(a, flow) for a in (body.get("else") or [])]
        if not then and not other:
            raise RuleError("Mindestens eine Aktion (DANN) angeben")
        if flow:
            if any(c["type"] == "budget" for c in conds):
                raise RuleError("„Laufzeit pro Tag“ gibt es nur bei Regeln der Art „Zustand halten“")
        else:
            if any(c["type"] == "virtual" and c.get("is") == "pressed" for c in conds):
                raise RuleError("„wird gedrückt“ geht nur bei Regeln der Art „Ablauf“")
            for key, lst in (("DANN", then), ("SONST", other)):
                ids = [a["device_id"] for a in lst]
                if len(ids) != len(set(ids)):
                    raise RuleError(f"{key}: dasselbe Gerät kommt mehrfach vor")
            for dev_id, (t, e) in _states({"then": then, "else": other}).items():
                if t and t == e:
                    raise RuleError("Dasselbe Gerät soll bei DANN und SONST gleich geschaltet werden – das ergibt keinen Sinn")
        first = next((a["device_id"] for a in then + other if a.get("type", "switch") in ("switch", "toggle")), "")
        r = {"id": rule_id or uuid.uuid4().hex[:8], "name": str(body.get("name") or "").strip()[:60],
             "device_id": first, "enabled": bool(body.get("enabled", True)),
             "when": {"mode": mode, "conds": conds}, "then": then, "else": other}
        if flow:
            r["mode"] = "flow"
            r["abort_on_fall"] = bool(body.get("abort_on_fall"))
            r["keep_trigger"] = bool(body.get("keep_trigger"))           # Standard: ausloesender Schalter geht am Ende des Ablaufs von selbst zurueck
        compile_rule(r)                                  # prueft die Uebersetzbarkeit (RuleError)
        return r
    on = [normalize_condition(c) for c in (body.get("on") if body.get("on") is not None else body.get("conditions") or [])]
    off = [normalize_condition(c) for c in (body.get("off") or [])]
    if not on and not off:
        raise RuleError("Mindestens eine Bedingung angeben")
    if not body.get("device_id"):
        raise RuleError("Gerät wählen")
    name = str(body.get("name") or "").strip()[:60]
    return {"id": rule_id or uuid.uuid4().hex[:8], "name": name, "device_id": str(body["device_id"]),
            "enabled": bool(body.get("enabled", True)), "on": on, "off": off}


def add_rule(body: dict) -> dict:
    d = load()
    r = normalize_rule(body)
    d["rules"].append(r)
    _save(d)
    return r


def update_rule(rule_id: str, body: dict) -> dict | None:
    d = load()
    for i, r in enumerate(d["rules"]):
        if r["id"] == rule_id:
            merged = {**r, **{k: v for k, v in body.items() if k in ("name", "device_id", "enabled", "on", "off", "when", "then", "else", "mode", "abort_on_fall", "keep_trigger")}}
            d["rules"][i] = normalize_rule(merged, rule_id)
            _save(d)
            return d["rules"][i]
    return None


def replace_all(items: list) -> list[dict]:
    """Ersetzt die komplette Regelliste (Speichern-Knopf der Automatik-Seite). Alle Regeln werden vorab geprueft (RuleError -> nichts
    geschrieben); Regeln mit bekannter ID behalten sie, neue bekommen eine."""
    d = load()
    known = {r["id"] for r in d["rules"]}
    out = [normalize_rule(b, b.get("id") if b.get("id") in known else None) for b in items]
    d["rules"] = out
    _save(d)
    return out


def delete_rule(rule_id: str) -> bool:
    d = load()
    n = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if r["id"] != rule_id]
    if len(d["rules"]) != n:
        _save(d)
        return True
    return False


def remove_device(dev_id: str):
    d = load()
    keep, changed = [], False
    for r in d["rules"]:
        if "when" in r and (dev_id in devices_of(r) or any(dev_id in (a.get("device_ids") or []) or (a.get("type") in ("lock", "wol") and a.get("id") == dev_id) for a in r["then"] + r["else"])):
            def strip(lst):
                out = []
                for a in lst:
                    if a.get("type") == "setpoint":
                        ids = [i for i in a["device_ids"] if i != dev_id]
                        if ids:
                            out.append({**a, "device_ids": ids})
                    elif a.get("type") in ("lock", "wol") and a.get("id") == dev_id:
                        continue
                    elif not (a.get("type", "switch") in ("switch", "toggle") and a["device_id"] == dev_id):
                        out.append(a)
                return out
            r = {**r, "then": strip(r["then"]), "else": strip(r["else"])}
            changed = True
            if not any(a.get("type") != "wait" for a in r["then"] + r["else"]):
                continue
            r["device_id"] = next((a["device_id"] for a in r["then"] + r["else"] if a.get("type", "switch") in ("switch", "toggle")), "")
        elif "when" not in r and r["device_id"] == dev_id:
            changed = True
            continue
        keep.append(r)
    if changed:
        d["rules"] = keep
        _save(d)


# ------------------------------------------------------------------------------------------ Bedingungen
def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now: datetime, frm: str, to: str) -> bool:
    cur = now.hour * 60 + now.minute
    a, b = _minutes(frm), _minutes(to)
    if a == b:
        return True
    return a <= cur < b if a < b else (cur >= a or cur < b)      # Fenster ueber Mitternacht


def _slot(now: datetime) -> int:
    return now.hour * 4 + now.minute // 15


def _cheapest_slots(prices: list, lo: int, hi: int, n: int, first: int) -> list[int]:
    """Indizes der n guenstigsten Viertelstunden (ab `first`, im Fenster lo..hi-1; ohne Preise: die fruehesten)."""
    idx = [i for i in range(lo, hi) if i >= first and (prices is None or (i < len(prices) and prices[i] is not None))]
    idx.sort(key=lambda i: (prices[i] if prices else 0, i))
    return idx[:n]


BUDGET_DONE = " – Tagesziel erreicht"
AT_GRACE_MIN = 30          # "Um HH:MM" loest bis zu 30 Minuten nach der Uhrzeit aus (App-Neustart, kurze Aussetzer)


SENSOR_WORDS = {"lock": ("verriegelt", "entriegelt"), "contact": ("offen", "geschlossen"), "motion": ("Bewegung erkannt", "keine Bewegung"),
                "presence": ("jemand anwesend", "niemand anwesend")}


def eval_condition(c: dict, ctx: dict, ran_min: float = 0.0, fired_today: bool = False, side: str = "on") -> tuple[bool | None, str]:
    """(erfuellt?, Klartext). None = fehlende Daten (zaehlt als nicht erfuellt)."""
    now = ctx["now"]
    t = c["type"]
    if t == "never":                                 # intern: 'Ausschalt-Bedingung', die nie zutrifft (Geraet bleibt nach DANN an)
        return False, "nie"
    if t == "group":                                 # intern: UND/ODER, optional umgekehrt (SONST); fehlende Daten bleiben 'unbekannt'
        res = [eval_condition(x, ctx, ran_min, fired_today, side) for x in c["conds"]]
        oks = [r[0] for r in res]
        if c["mode"] == "all":
            v = False if any(o is False for o in oks) else (None if any(o is None for o in oks) else True)
        else:
            v = True if any(o is True for o in oks) else (None if any(o is None for o in oks) else False)
        if c.get("not") and v is not None:
            v = not v
        txt = (" UND " if c["mode"] == "all" else " ODER ").join(r[1] for r in res)
        return v, ("nicht (" + txt + ")") if c.get("not") else txt
    if t == "virtual":
        info = (ctx.get("virtual") or {}).get(c.get("id"))
        if not info:
            return None, f"Eigener Schalter {c.get('id')} (nicht mehr vorhanden)"
        name = info.get("name") or c["id"]
        if c["is"] == "pressed":
            return bool(info.get("pressed")), f"{name} wird gedrückt"
        return bool(info.get("on")) == (c["is"] == "on"), f"{name} ist {'an' if c['is'] == 'on' else 'aus'}"
    if t == "device":
        info = (ctx.get("devices") or {}).get(c.get("device_id"))
        if not info:
            return None, f"Gerät {c.get('device_id')} (nicht mehr vorhanden)"
        name = info.get("name") or info["id"]
        if "is" in c:
            txt = f"{name} ist {'an' if c['is'] == 'on' else 'aus'}"
            return (None if not info.get("online") or info.get("on") is None else bool(info["on"]) == (c["is"] == "on")), txt
        p = info.get("power")
        txt = f"{name} Leistung {'unter' if c['op'] == 'below' else 'über'} {c['value']:g} W"
        return (None if p is None or not info.get("online") else (p < c["value"] if c["op"] == "below" else p >= c["value"])), txt
    if t == "at":
        days = c.get("days") or []
        since = now.hour * 60 + now.minute - _minutes(c["time"])
        txt = f"um {c['time']} Uhr" + (" (" + ", ".join(WEEKDAYS[i] for i in days) + ")" if days else "")
        return (0 <= since < AT_GRACE_MIN and not fired_today and (not days or now.weekday() in days)), txt
    if t == "time":
        days = c.get("days") or []
        txt = f"{c['from']}–{c['to']} Uhr" + (" (" + ", ".join(WEEKDAYS[i] for i in days) + ")" if days else "")
        return in_window(now, c["from"], c["to"]) and (not days or now.weekday() in days), txt
    if t == "soc":
        soc = ctx.get("soc")
        txt = f"Akku {'unter' if c['op'] == 'below' else 'über'} {c['pct']:g} %"
        return (None if soc is None else (soc < c["pct"] if c["op"] == "below" else soc >= c["pct"])), txt
    if t == "price":
        p = ctx.get("price_ct")
        txt = f"Preis {'unter' if c['op'] == 'below' else 'über'} {c['ct']:g} ct"
        return (None if p is None else (p < c["ct"] if c["op"] == "below" else p >= c["ct"])), txt
    if t == "sun_tomorrow":
        s = ctx.get("pv_tomorrow")
        txt = f"Sonne morgen {'unter' if c['op'] == 'below' else 'über'} {c['kwh']:g} kWh"
        return (None if s is None else (s < c["kwh"] if c["op"] == "below" else s >= c["kwh"])), txt
    if t == "sensor":
        info = (ctx.get("sensor_info") or {}).get(c.get("sensor_id"))
        if not info:
            return None, f"Sensor {c.get('sensor_id')} (nicht mehr vorhanden)"
        val = (ctx.get("sensors") or {}).get(info["id"])
        name = info.get("name") or info["id"]
        if "is" in c:
            words = SENSOR_WORDS.get(info.get("kind"), ("wahr", "falsch"))
            if info.get("invert") and info.get("kind") in SENSOR_WORDS:
                words = (words[1], words[0])                 # umgekehrter Sensor: TRUE bedeutet das Gegenteil (z. B. TRUE = geschlossen)
            txt = f"{name}: {words[0] if c['is'] else words[1]} ({'TRUE' if c['is'] else 'FALSE'})"
            return (None if val is None else bool(val) == c["is"]), txt
        unit = (" " + info["unit"]) if info.get("unit") else ""
        txt = f"{name} {'unter' if c['op'] == 'below' else 'über'} {c['value']:g}{unit}"
        return (None if val is None else (val < c["value"] if c["op"] == "below" else val >= c["value"])), txt
    if t == "cheapest":
        prices = ctx.get("prices_today")
        txt = f"in den {c['hours']} günstigsten Stunden des Tages"
        if not prices:
            return None, txt
        return _slot(now) in _cheapest_slots(prices, 0, 96, c["hours"] * 4, 0), txt
    if t == "budget" and side == "off":              # Ausschalten: Laufzeitziel des Tages erreicht
        return ran_min >= c["minutes"], f"Tagesziel erreicht ({c['minutes']} Min Laufzeit heute)"
    if t == "budget":
        txt = f"{c['minutes']} Min pro Tag zu den günstigsten Zeiten ({c['from']}–{c['to']})"
        need = math.ceil((c["minutes"] - ran_min) / 15)
        if need <= 0:
            return False, txt + BUDGET_DONE
        a, b = _minutes(c["from"]), _minutes(c["to"])
        lo, hi = a // 15, (b + 14) // 15 if b > a else 96
        if b <= a:                                       # Fenster ueber Mitternacht: nur der heutige Teil ab dem Start bzw. bis zum Ende
            lo, hi = (0, min(96, b // 15)) if _slot(now) < a // 15 else (a // 15, 96)
        return _slot(now) in _cheapest_slots(ctx.get("prices_today"), lo, hi, need, _slot(now)), txt
    return False, t


# ------------------------------------------------------------------------------------------ Engine
class RuleEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self.owner: dict[str, str] = {}                  # geraet -> 'rule' (die Engine hat es eingeschaltet oder uebernommen)
        self.owner_rule: dict[str, str] = {}             # geraet -> Regel-ID, die es eingeschaltet hat
        self.ran: dict[str, dict] = {}                   # regel -> {"day": iso, "min": Minuten (Tagesziel)}
        self.blocked: set[str] = set()                   # Regeln, die nach einem Ausschalten erst neu 'scharf' werden muessen
        self.block_reason: dict[str, str] = {}           # regel -> 'manual' (von Hand ausgeschaltet) | 'off' (Ausschalt-Bedingung)
        self.en: dict[str, bool] = {}                    # regel -> war beim letzten Schritt eingeschaltet? (Aus -> An = frischer Start)
        self.sigs: dict[str, str] = {}                   # regel -> Fingerabdruck (Geraet + Bedingungen): Aenderung = Regel beginnt von vorn
        self.fired: dict[str, str] = {}                  # regel -> Tag, an dem ein 'Um HH:MM'-Ausloeser schon gefeuert hat
        self._prev_on: dict[str, bool] = {}              # geraet -> Zustand beim letzten Schritt (Erkennung von Handschaltungen am Geraet)
        self._hold_until: dict[str, datetime] = {}
        self._skip_noted: dict[str, str] = {}            # regel -> Ende der Pause, fuer die das Logbuch schon Bescheid weiss
        self._armed: dict[str, datetime] = {}
        self._last_step: datetime | None = None
        self._loaded = False
        self.status: dict[str, dict] = {}                # regel -> {"state": "on|wait|off|paused|disabled", "text", "conds": [...]}

    # ---- Persistenz (Besitzer + Tagesziel-Minuten)
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            d = _store()._load_json_recovering(STATE_PATH, lambda: {})
            if isinstance(d, dict):
                self.owner = {k: v for k, v in (d.get("owner") or {}).items() if isinstance(v, str)}
                self.owner_rule = {k: v for k, v in (d.get("owner_rule") or {}).items() if isinstance(v, str)}
                self.ran = {k: v for k, v in (d.get("ran") or {}).items() if isinstance(v, dict)}
                self.block_reason = {k: v for k, v in (d.get("blocked") or {}).items() if isinstance(v, str)}
                self.blocked = set(self.block_reason)
                self.fired = {k: v for k, v in (d.get("fired") or {}).items() if isinstance(v, str)}
                self.sigs = {k: v for k, v in (d.get("sigs") or {}).items() if isinstance(v, str)}
                self.en = {k: bool(v) for k, v in (d.get("en") or {}).items()}
        except Exception:                                # noqa: BLE001
            pass

    def _save_state(self):
        try:
            _store()._dump_json(STATE_PATH, {"owner": self.owner, "owner_rule": self.owner_rule, "ran": self.ran,
                                             "blocked": {r: self.block_reason.get(r, "off") for r in self.blocked}, "fired": self.fired, "sigs": self.sigs, "en": self.en}, indent=None)
        except Exception:                                # noqa: BLE001
            pass

    # ---- Hilfen fuer den Aufrufer
    def note_manual(self, dev_id: str, now: datetime | None = None, hold_min: float = 60):
        now = now or datetime.now()
        with self._lock:
            self._hold_until[dev_id] = now.replace(microsecond=0) + timedelta(minutes=hold_min)
            rid = self.owner_rule.get(dev_id)
            if self.owner.get(dev_id) == "rule" and rid:
                self._block(rid, "manual")               # von Hand ausgeschaltet: gilt, bis die Einschalt-Bedingungen einmal nicht mehr stimmen
            self.owner.pop(dev_id, None)
            self.owner_rule.pop(dev_id, None)
            self._armed.pop(dev_id, None)
        self._save_state()

    def _note_paused(self, notes: list, rule_id: str, dev: dict, until: datetime, what: str, result: str):
        """Logbuch-Eintrag (einmal je Pause): die Regel waere dran, wartet aber wegen der Pause nach Handschaltung."""
        key = until.isoformat()
        if self._skip_noted.get(rule_id) == key:
            return
        self._skip_noted[rule_id] = key
        notes.append(("paused", dev, f"{what}, aber Pause nach Handschaltung bis {until:%H:%M} Uhr – die Regel {result}", rule_id))

    def _block(self, rule_id: str, reason: str):
        self.blocked.add(rule_id)
        self.block_reason[rule_id] = reason

    def mark_armed(self, dev_id: str, now: datetime | None = None):
        with self._lock:
            self._armed[dev_id] = now or datetime.now()

    def disarm(self, dev_id: str):
        with self._lock:
            self._armed.pop(dev_id, None)

    def disarm_all(self):
        with self._lock:
            self._armed.clear()

    def reset(self):
        with self._lock:
            self._last_step = None

    def due_rearm(self, now: datetime, devices: list[dict], failsafe_min: float) -> list[dict]:
        by_id = {d["id"]: d for d in devices}
        due = []
        with self._lock:
            for dev_id, ts in list(self._armed.items()):
                d = by_id.get(dev_id)
                if (failsafe_min <= 0 or not d or not d.get("online") or not d.get("on") or dev_id not in self.owner
                        or not d.get("switchable", True) or self._hold_until.get(dev_id, now) > now):
                    self._armed.pop(dev_id, None)
                    continue
                if (now - ts).total_seconds() >= failsafe_min * 60 / 2:
                    due.append(d)
        return due

    def set_owner(self, dev_id: str, who: str | None, rule_id: str | None = None):
        with self._lock:
            if who:
                self.owner[dev_id] = who
                if who == "rule" and rule_id:
                    self.owner_rule[dev_id] = rule_id
                elif who != "rule":
                    self.owner_rule.pop(dev_id, None)
            else:
                self.owner.pop(dev_id, None)
                self.owner_rule.pop(dev_id, None)
                self._armed.pop(dev_id, None)
        self._save_state()

    def _check_changed(self, r: dict):
        """Wurde die Regel geaendert (Geraet/Bedingungen), beginnt sie von vorn: alte Ausloeser-Tage, Sperren, Laufzeit und die
        Uebernahme eines Geraets werden vergessen (das Geraet selbst bleibt, wie es ist)."""
        sig = json.dumps([r["device_id"], r["on"], r.get("off", [])], sort_keys=True)      # (an/aus-Schalter zaehlt nicht: Ausschalten muss das Geraet noch abschalten koennen)
        rid = r["id"]
        was_on = self.en.get(rid)
        self.en[rid] = bool(r.get("enabled", True))
        if was_on is False and self.en[rid]:               # Regel wieder eingeschaltet: beginnt frisch (Ausloeser-Tag, Sperre, Laufzeit vergessen)
            for k in (rid + ":on", rid + ":off"):
                self.fired.pop(k, None)
            self.blocked.discard(rid)
            self.block_reason.pop(rid, None)
            self.ran.pop(rid, None)
        prev = self.sigs.get(rid)
        self.sigs[rid] = sig
        if prev is None or prev == sig:
            return
        for k in (rid + ":on", rid + ":off"):
            self.fired.pop(k, None)
        self.blocked.discard(rid)
        self.block_reason.pop(rid, None)
        self.ran.pop(rid, None)
        for dev_id, owned_rule in list(self.owner_rule.items()):
            if owned_rule == rid:
                self.owner.pop(dev_id, None)
                self.owner_rule.pop(dev_id, None)
                self._armed.pop(dev_id, None)
        self._save_state()

    # ---- Kern
    def step(self, now: datetime, ctx: dict, cfg: dict, devices: list[dict], rules: list[dict]) -> list[tuple]:
        """Ein Regelschritt. devices: Live-Status (online, on, switchable). 
        Rueckgabe: Liste von (aktion, geraet, begruendung, regel-id). aktion: 'on'|'off' = schalten; 'adopt'|'manual' = nur fuers Logbuch
        (Regel hat ein bereits laufendes Geraet uebernommen / Geraet wurde am Geraet selbst ausgeschaltet)."""
        self._load()
        ctx = {**ctx, "now": now}
        today = now.date().isoformat()
        dt = 0.0 if self._last_step is None else min(60.0, max(0.0, (now - self._last_step).total_seconds()))
        self._last_step = now
        by_dev = {d["id"]: d for d in devices}
        hold = dict(self._hold_until)
        status: dict[str, dict] = {}
        info: dict[str, dict] = {}                       # regel -> Auswertung
        wants: dict[str, tuple[str, str]] = {}           # geraet -> (regel-id, Begruendung): soll jetzt eingeschaltet werden
        pure_off_acts: list[tuple] = []
        off_active: set[str] = set()                     # Geraete, fuer die gerade eine reine Ausschalt-Regel zutrifft
        notes: list[tuple] = []                          # Ereignisse ohne Schalten (fuers Logbuch)
        rule_ids = {r["id"] for r in rules}
        rule_by_id = {r["id"]: r for r in rules}
        for gone in self.blocked - rule_ids:
            self.blocked.discard(gone)
            self.block_reason.pop(gone, None)
        # Am Geraet selbst (oder in einer anderen App) ausgeschaltet, obwohl es der Regel gehoert: gilt wie Handschaltung
        try:
            manual_hold = settings(cfg)["manual_hold_min"]
        except (TypeError, ValueError):
            manual_hold = 60.0
        for d in devices:
            if self._prev_on.get(d["id"]) is True and d.get("online") and not d.get("on") and self.owner.get(d["id"]) == "rule":
                notes.append(("manual", d, "am Gerät ausgeschaltet – die Regel wartet, bis ihre Einschalt-Bedingungen einmal nicht mehr gelten", self.owner_rule.get(d["id"])))
                self.note_manual(d["id"], now, manual_hold)
                hold = dict(self._hold_until)
            if d.get("online"):
                self._prev_on[d["id"]] = bool(d.get("on"))

        for gone in (set(self.sigs) | set(self.en)) - rule_ids:
            self.sigs.pop(gone, None)
            self.en.pop(gone, None)
        for r in rules:
            self._check_changed(r)
            dev = by_dev.get(r["device_id"])
            if not r.get("enabled", True):
                status[r["id"]] = {"state": "disabled", "text": "Regel ist ausgeschaltet", "conds": []}
                continue
            if not dev:
                status[r["id"]] = {"state": "off", "text": "Gerät nicht mehr vorhanden", "conds": []}
                continue
            ran = self.ran.get(r["id"], {})
            ran_min = float(ran.get("min", 0.0)) if ran.get("day") == today else 0.0
            has_on_at = any(c["type"] == "at" for c in r["on"])
            on_res = [(c, *eval_condition(c, ctx, ran_min, self.fired.get(r["id"] + ":on") == today, "on")) for c in r["on"]]
            off_res = [(c, *eval_condition(c, ctx, ran_min, self.fired.get(r["id"] + ":off") == today, "off")) for c in r.get("off", [])]
            if any(c["type"] == "at" and ok for c, ok, _ in off_res):
                self.fired[r["id"] + ":off"] = today            # einmal pro Tag
            pure_off = not r["on"]                              # reiner Ausschalt-Timer
            base_on = bool(r["on"]) and all(ok for _, ok, _ in on_res)
            off_hit = [txt for _, ok, txt in off_res if ok]
            budget_done = any(c["type"] == "budget" and txt.endswith(BUDGET_DONE) for c, _, txt in on_res)
            missing = [txt for _, ok, txt in on_res if ok is None]
            conds = [{"ok": ok, "text": txt} for _, ok, txt in on_res]
            info[r["id"]] = {"base_on": base_on, "off_hit": off_hit, "budget_done": budget_done, "has_off": bool(off_res) or has_on_at}       # Ausloeser ("um HH:MM") schaltet nicht von selbst wieder aus
            if pure_off:                                        # schaltet nie ein; schaltet jedes laufende Geraet aus, wenn eine Ausschalt-Bedingung zutrifft
                dvc = by_dev.get(r["device_id"])
                if off_hit:
                    off_active.add(r["device_id"])              # solange sie zutrifft, schaltet keine andere Regel das Geraet ein (Ausschalten gewinnt)
                if dvc and dvc.get("switchable", True) and dvc.get("online") and dvc.get("on") and off_hit and hold.get(dvc["id"], now) <= now:
                    pure_off_acts.append(("off", dvc, "Ausschalt-Bedingung: " + ", ".join(off_hit), r["id"]))
                elif dvc and dvc.get("on") and off_hit and hold.get(dvc["id"], now) > now:
                    self._note_paused(notes, r["id"], dvc, hold[dvc["id"]], "Ausschalt-Bedingung erfüllt (" + ", ".join(off_hit) + ")", "schaltet nicht aus")
                status[r["id"]] = {"state": "off", "text": ("schaltet aus: " + ", ".join(off_hit)) if off_hit else "schaltet nur aus (nie automatisch ein) – wartet auf: " + ", ".join(t for _, _, t in off_res),
                                   "conds": [{"ok": ok, "text": txt} for _, ok, txt in off_res]}
                continue
            if not base_on:
                self.blocked.discard(r["id"])            # Einschalt-Bedingungen galten nicht mehr -> Regel wieder scharf
                self.block_reason.pop(r["id"], None)
            if not dev.get("switchable", True):
                status[r["id"]] = {"state": "off", "text": "Gerät ist nur zur Überwachung (nicht schaltbar)", "conds": conds}
                continue
            if hold.get(dev["id"], now) > now:
                status[r["id"]] = {"state": "paused", "text": "Pause nach Handschaltung", "conds": conds}
                if base_on and r["id"] not in self.blocked and not (dev.get("on") and self.owner.get(dev["id"]) == "rule"):
                    self._note_paused(notes, r["id"], dev, hold[dev["id"]], "Einschalt-Bedingung erfüllt (" + ", ".join(t for _, _, t in on_res) + ")", "schaltet nicht ein")
                continue
            owned_here = self.owner.get(dev["id"]) == "rule" and self.owner_rule.get(dev["id"]) == r["id"]
            if owned_here and dev.get("on") and "budget" in {c["type"] for c in r["on"] + r.get("off", [])}:
                self.ran[r["id"]] = {"day": today, "min": ran_min + dt / 60.0}        # Tagesziel: Laufzeit mitzaehlen
            if owned_here and dev.get("on"):
                if off_hit:
                    status[r["id"]] = {"state": "off", "text": "wird ausgeschaltet: " + ", ".join(off_hit), "conds": conds}
                elif budget_done or (not off_res and not base_on):
                    status[r["id"]] = {"state": "off", "text": "wird ausgeschaltet: Einschalt-Bedingungen nicht mehr erfüllt" if not budget_done else "Tagesziel erreicht", "conds": conds}
                else:
                    status[r["id"]] = {"state": "on", "text": "läuft" + (" – bis eine Ausschalt-Bedingung zutrifft" if off_res else " – solange die Bedingungen stimmen"), "conds": conds}
            elif base_on and r["id"] not in self.blocked:
                if dev.get("on") and dev["id"] not in self.owner:
                    self.set_owner(dev["id"], "rule", r["id"])            # Geraet laeuft schon, waehrend die Bedingungen stimmen: Regel uebernimmt es
                    notes.append(("adopt", dev, "war schon eingeschaltet (Einschalt-Bedingung erfüllt: " + ", ".join(t for _, _, t in on_res) + ") – die Regel übernimmt es und schaltet es später aus", r["id"]))
                    if has_on_at:
                        self.fired[r["id"] + ":on"] = today
                    status[r["id"]] = {"state": "on", "text": "läuft – von der Regel übernommen", "conds": conds}
                else:
                    wants.setdefault(dev["id"], (r["id"], ", ".join(t for _, _, t in on_res)))
                    status[r["id"]] = {"state": "on", "text": "Bedingungen erfüllt", "conds": conds}
            elif base_on:
                by_hand = self.block_reason.get(r["id"]) == "manual"
                status[r["id"]] = {"state": "off", "text": ("von Hand ausgeschaltet" if by_hand else "Ausschalt-Bedingung war erfüllt") + " – wartet, bis die Einschalt-Bedingungen einmal nicht mehr stimmen", "conds": conds}
            else:
                why = ("keine Daten für: " + ", ".join(missing)) if missing else "nicht erfüllt: " + ", ".join(x["text"] for x in conds if x["ok"] is False)
                status[r["id"]] = {"state": "off", "text": why, "conds": conds}

        actions: list[tuple] = notes + list(pure_off_acts)
        # ---- 1. Einschalten
        off_now = off_active | {a[1]["id"] for a in pure_off_acts}      # Ausschalten gewinnt: ein Geraet, das eine Regel gerade ausschaltet, wird nicht von einer anderen eingeschaltet
        for dev_id, (rid, name) in wants.items():
            d = by_dev[dev_id]
            if dev_id not in off_now and d.get("online") and not d.get("on"):
                actions.append(("on", d, "Einschalt-Bedingung: " + name, rid))
                if any(c["type"] == "at" for c in rule_by_id[rid]["on"]):
                    self.fired[rid + ":on"] = today                  # 'Um HH:MM' hat ausgeloest (einmal pro Tag)
        # ---- 2. Ausschalten (nur, was die Engine selbst eingeschaltet hat)
        for dev_id, who in list(self.owner.items()):
            d = by_dev.get(dev_id)
            if not d or hold.get(dev_id, now) > now:
                continue
            if not d.get("on"):
                self.set_owner(dev_id, None)
                continue
            rid = self.owner_rule.get(dev_id)
            if who == "rule":
                r = next((x for x in rules if x["id"] == rid), None)
                inf = info.get(rid or "")
                if r is None or not r.get("enabled", True) or inf is None:
                    reason = "Regel nicht mehr aktiv"
                elif inf["off_hit"]:
                    reason = "Ausschalt-Bedingung: " + ", ".join(inf["off_hit"])
                    if inf["base_on"]:
                        self._block(rid, "off")
                elif inf["budget_done"]:
                    reason = "Tagesziel erreicht"
                elif not inf["has_off"] and not inf["base_on"]:
                    reason = "Bedingungen nicht mehr erfüllt"
                else:
                    continue
                if d.get("online") and d.get("switchable", True):
                    actions.append(("off", d, reason, rid))
        with self._lock:
            self.status = status
        return actions

    def flush(self):
        self._save_state()
