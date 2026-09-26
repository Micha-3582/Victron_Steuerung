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
  cheapest      In den N guenstigsten Stunden des Tages
  budget        Tagesziel: X Minuten pro Tag. Einschalten: laeuft zu den guenstigsten Zeiten, bis das Ziel erreicht ist (dann aus).
                Ausschalten: trifft zu, sobald das Geraet heute X Minuten gelaufen ist.
  soc           Akku ueber/unter X %
  at            Um HH:MM Uhr, einmal pro Tag (loest innerhalb von 30 Min nach der Uhrzeit aus). Beim Einschalten bleibt das Geraet danach
                an, bis eine Ausschalt-Bedingung zutrifft.
  sun_tomorrow  Sonne morgen (VRM-Prognose) ueber/unter X kWh

Regeln sind vollstaendig unabhaengig von der PV-Ueberschuss-Automatik (surplus.py): eigener Hauptschalter, eigener Trockenlauf, eigene Einstellungen,
eigenes Logbuch. Ein Geraet gehoert entweder zur Ueberschuss-Automatik oder zu Regeln (wird beim Speichern geprueft).

Sicherheit: ausgeschaltet wird nur, was die Engine selbst eingeschaltet oder uebernommen hat (Besitzer-Merkung, ueberlebt Neustarts). Laeuft ein Geraet,
waehrend die Einschalt-Bedingungen einer Regel stimmen, uebernimmt die Regel es (Timer-Verhalten); von Hand ueber die App geschaltete Geraete
bleiben fuer eine Weile in Ruhe; Shelly bekommen einen Rueckschalt-Timer (wie bisher).

Alles hier ist reine Logik ohne Netzwerk (testbar). Geschaltet wird vom Aufrufer (webapp.py).
"""
from __future__ import annotations

import math
import os
import threading
import uuid
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
RULES_PATH = os.path.join(_DIR, "rules.json")
STATE_PATH = os.path.join(_DIR, "rules_state.json")

TYPES = ("time", "price", "cheapest", "budget", "soc", "sun_tomorrow", "at")
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
    if t == "at":
        days = sorted({int(x) for x in (c.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        return {"type": t, "time": _hhmm(c.get("time", "23:30"), "Uhrzeit"), "days": days if len(days) < 7 else []}
    if t == "time":
        days = sorted({int(x) for x in (c.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        return {"type": t, "from": _hhmm(c.get("from", "00:00"), "Von"), "to": _hhmm(c.get("to", "24:00"), "Bis"), "days": days if len(days) < 7 else []}
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


def normalize_rule(body: dict, rule_id: str | None = None) -> dict:
    on = [normalize_condition(c) for c in (body.get("on") if body.get("on") is not None else body.get("conditions") or [])]
    off = [normalize_condition(c) for c in (body.get("off") or [])]
    if not on and not off:
        raise RuleError("Mindestens eine Bedingung angeben")
    if not body.get("device_id"):
        raise RuleError("Gerät wählen")
    name = str(body.get("name") or "").strip()[:60]
    return {"id": rule_id or uuid.uuid4().hex[:8], "name": name, "device_id": str(body["device_id"]),
            "enabled": bool(body.get("enabled", True)), "on": on, "off": off,
            "min_on_min": _num(body.get("min_on_min", 5) if body.get("min_on_min") not in (None, "") else 5, 0, 1440, "Mind. an (min)"),
            "min_off_min": _num(body.get("min_off_min", 5) if body.get("min_off_min") not in (None, "") else 5, 0, 1440, "Mind. aus (min)")}


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
            merged = {**r, **{k: v for k, v in body.items() if k in ("name", "device_id", "enabled", "on", "off", "min_on_min", "min_off_min")}}
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
    keep = [r for r in d["rules"] if r["device_id"] != dev_id]
    if len(keep) != len(d["rules"]):
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


def eval_condition(c: dict, ctx: dict, ran_min: float = 0.0, fired_today: bool = False, side: str = "on") -> tuple[bool | None, str]:
    """(erfuellt?, Klartext). None = fehlende Daten (zaehlt als nicht erfuellt)."""
    now = ctx["now"]
    t = c["type"]
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
        self.fired: dict[str, str] = {}                  # regel -> Tag, an dem ein 'Um HH:MM'-Ausloeser schon gefeuert hat
        self._prev_on: dict[str, bool] = {}              # geraet -> Zustand beim letzten Schritt (Erkennung von Handschaltungen am Geraet)
        self._last_change: dict[str, datetime] = {}
        self._hold_until: dict[str, datetime] = {}
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
        except Exception:                                # noqa: BLE001
            pass

    def _save_state(self):
        try:
            _store()._dump_json(STATE_PATH, {"owner": self.owner, "owner_rule": self.owner_rule, "ran": self.ran,
                                             "blocked": {r: self.block_reason.get(r, "off") for r in self.blocked}, "fired": self.fired}, indent=None)
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
            self._last_change[dev_id] = datetime.now()
        self._save_state()

    # ---- Kern
    def step(self, now: datetime, ctx: dict, cfg: dict, devices: list[dict], rules: list[dict]) -> list[tuple]:
        """Ein Regelschritt. devices: Live-Status (online, on, switchable). Mind. an/aus stehen an der Regel.
        Rueckgabe: Liste von (aktion 'on'|'off', geraet, begruendung, regel-id)."""
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
                self.note_manual(d["id"], now, manual_hold)
                hold = dict(self._hold_until)
            if d.get("online"):
                self._prev_on[d["id"]] = bool(d.get("on"))

        for r in rules:
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
                if dvc and dvc.get("switchable", True) and dvc.get("online") and dvc.get("on") and off_hit and hold.get(dvc["id"], now) <= now:
                    pure_off_acts.append(("off", dvc, "Ausschalt-Regel: " + ", ".join(off_hit), r["id"]))
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
                    if has_on_at:
                        self.fired[r["id"] + ":on"] = today
                    status[r["id"]] = {"state": "on", "text": "läuft – von der Regel übernommen", "conds": conds}
                else:
                    wants.setdefault(dev["id"], (r["id"], r["name"] or "Regel"))
                    status[r["id"]] = {"state": "on", "text": "Bedingungen erfüllt", "conds": conds}
            elif base_on:
                by_hand = self.block_reason.get(r["id"]) == "manual"
                status[r["id"]] = {"state": "off", "text": ("von Hand ausgeschaltet" if by_hand else "Ausschalt-Bedingung war erfüllt") + " – wartet, bis die Einschalt-Bedingungen einmal nicht mehr stimmen", "conds": conds}
            else:
                why = ("keine Daten für: " + ", ".join(missing)) if missing else "nicht erfüllt: " + ", ".join(x["text"] for x in conds if x["ok"] is False)
                status[r["id"]] = {"state": "off", "text": why, "conds": conds}

        actions: list[tuple] = list(pure_off_acts)
        # ---- 1. Einschalten
        for dev_id, (rid, name) in wants.items():
            d = by_dev[dev_id]
            if d.get("online") and not d.get("on") and self._waited(rule_by_id.get(rid), dev_id, now, "min_off_min"):
                actions.append(("on", d, "Regel: " + name, rid))
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
                if d.get("online") and d.get("switchable", True) and self._waited(r, dev_id, now, "min_on_min"):
                    actions.append(("off", d, reason, rid))
        with self._lock:
            self.status = status
        return actions

    def _waited(self, rule: dict | None, dev_id: str, now: datetime, key: str) -> bool:
        """Mindest-Ein-/Ausschaltdauer (an der Regel) seit der letzten Aenderung eingehalten?"""
        t = self._last_change.get(dev_id)
        need = float((rule or {}).get(key, 5)) * 60
        return t is None or (now - t).total_seconds() >= need

    def flush(self):
        self._save_state()
