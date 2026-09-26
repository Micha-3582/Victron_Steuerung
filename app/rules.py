"""
Regel-Engine fuer Geraete (Shelly, Tasmota, Tuya).

Eine Regel gehoert zu EINEM Geraet und besteht aus Bedingungen. Sind ALLE Bedingungen erfuellt, soll das Geraet an sein,
sonst (wenn die Regel es eingeschaltet hat) wird es wieder ausgeschaltet. Mehrere Regeln pro Geraet gelten als ODER.

Bedingungen (Reihenfolge = Reihenfolge in der Oberflaeche):
  time        Zwischen von und bis Uhr (optional nur an bestimmten Wochentagen)
  price       Strompreis unter/ueber X ct
  cheapest    In den N guenstigsten Stunden des Tages
  budget      Tagesziel: X Minuten pro Tag, zu den guenstigsten Zeiten (im Zeitfenster)
  soc         Akku ueber/unter X %
  surplus     PV-Ueberschuss vorhanden (Akku voll, es wird eingespeist) - nutzt die bewaehrte Ueberschuss-Logik (surplus.py)
  sun_tomorrow  Sonne morgen (VRM-Prognose) ueber/unter X kWh

Die Ueberschuss-Logik bleibt unveraendert (Prioritaet = Reihenfolge der Regeln, Ein-/Aus-Verzoegerung, Hysterese): Geraete, deren
uebrige Bedingungen erfuellt sind und die eine Ueberschuss-Bedingung haben, werden ihr als Kandidaten uebergeben.

Sicherheit: geschaltet wird nur, was die Regel selbst eingeschaltet hat (Besitzer-Merkung, ueberlebt Neustarts); von Hand geschaltete Geraete
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

TYPES = ("time", "price", "cheapest", "budget", "soc", "surplus", "sun_tomorrow")
WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


class RuleError(ValueError):
    pass


# ------------------------------------------------------------------------------------------ Speicher
def _store():
    import store                       # spaet importieren (store importiert nichts von hier)
    return store


def load() -> dict:
    d = _store()._load_json_recovering(RULES_PATH, lambda: {"rules": []})
    if not isinstance(d, dict) or not isinstance(d.get("rules"), list):
        d = {"rules": []}
    return d


def _save(d: dict):
    _store()._dump_json(RULES_PATH, d, indent=2)


def list_rules() -> list[dict]:
    return load()["rules"]


def enabled(cfg: dict) -> bool:
    """Hauptschalter (aus der frueheren Ueberschuss-Automatik uebernommen, solange der neue Schluessel fehlt)."""
    return bool(cfg.get("rules_enabled", cfg.get("surplus_enabled", False)))


def dry_run(cfg: dict) -> bool:
    return bool(cfg.get("rules_dry_run", cfg.get("surplus_dry_run", True)))


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
    if t == "budget":
        return {"type": t, "minutes": int(_num(c.get("minutes"), 15, 1440, "Minuten pro Tag")),
                "from": _hhmm(c.get("from", "00:00"), "Von"), "to": _hhmm(c.get("to", "24:00"), "Bis")}
    return {"type": "surplus"}


def normalize_rule(body: dict, rule_id: str | None = None) -> dict:
    conds = [normalize_condition(c) for c in (body.get("conditions") or [])]
    if not conds:
        raise RuleError("Mindestens eine Bedingung angeben")
    if sum(1 for c in conds if c["type"] == "surplus") > 1:
        raise RuleError("Die Überschuss-Bedingung nur einmal verwenden")
    if not body.get("device_id"):
        raise RuleError("Gerät wählen")
    name = str(body.get("name") or "").strip()[:60]
    return {"id": rule_id or uuid.uuid4().hex[:8], "name": name, "device_id": str(body["device_id"]),
            "enabled": bool(body.get("enabled", True)), "conditions": conds}


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
            merged = {**r, **{k: v for k, v in body.items() if k in ("name", "device_id", "enabled", "conditions")}}
            d["rules"][i] = normalize_rule(merged, rule_id)
            _save(d)
            return d["rules"][i]
    return None


def delete_rule(rule_id: str) -> bool:
    d = load()
    n = len(d["rules"])
    d["rules"] = [r for r in d["rules"] if r["id"] != rule_id]
    if len(d["rules"]) != n:
        _save(d)
        return True
    return False


def reorder(ids: list[str]):
    d = load()
    by = {r["id"]: r for r in d["rules"]}
    d["rules"] = [by[i] for i in ids if i in by] + [r for r in d["rules"] if r["id"] not in ids]
    _save(d)


def remove_device(dev_id: str):
    d = load()
    keep = [r for r in d["rules"] if r["device_id"] != dev_id]
    if len(keep) != len(d["rules"]):
        d["rules"] = keep
        _save(d)


def migrate_from_devices(devices: list[dict]) -> int:
    """Einmalig: Geraete, die bisher in der Ueberschuss-Automatik waren (auto=True), bekommen eine Regel 'Ueberschuss'."""
    d = load()
    if d.get("migrated"):
        return 0
    made = 0
    for dev in sorted((x for x in devices if x.get("auto")), key=lambda x: x.get("prio") or 10 ** 6):
        if not any(r["device_id"] == dev["id"] and any(c["type"] == "surplus" for c in r["conditions"]) for r in d["rules"]):
            d["rules"].append({"id": uuid.uuid4().hex[:8], "name": "Überschuss", "device_id": dev["id"], "enabled": True,
                               "conditions": [{"type": "surplus"}]})
            made += 1
    d["migrated"] = True
    _save(d)
    return made


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


def eval_condition(c: dict, ctx: dict, ran_min: float = 0.0) -> tuple[bool | None, str]:
    """(erfuellt?, Klartext). None = fehlende Daten (zaehlt als nicht erfuellt)."""
    now = ctx["now"]
    t = c["type"]
    if t == "time":
        days = c.get("days") or []
        txt = f"{c['from']}–{c['to']} Uhr" + (" (" + ", ".join(WEEKDAYS[i] for i in days) + ")" if days else "")
        ok = in_window(now, c["from"], c["to"]) and (not days or now.weekday() in days)
        return ok, txt
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
        chosen = _cheapest_slots(prices, 0, 96, c["hours"] * 4, 0)
        return _slot(now) in chosen, txt
    if t == "budget":
        txt = f"{c['minutes']} Min pro Tag zu den günstigsten Zeiten ({c['from']}–{c['to']})"
        need = math.ceil((c["minutes"] - ran_min) / 15)
        if need <= 0:
            return False, txt + " – Tagesziel erreicht"
        a, b = _minutes(c["from"]), _minutes(c["to"])
        lo, hi = a // 15, (b + 14) // 15 if b > a else 96
        if b <= a:                                       # Fenster ueber Mitternacht: nur der heutige Teil ab dem Start bzw. bis zum Ende
            lo, hi = (0, min(96, b // 15)) if _slot(now) < a // 15 else (a // 15, 96)
        chosen = _cheapest_slots(ctx.get("prices_today"), lo, hi, need, _slot(now))
        return _slot(now) in chosen, txt
    if t == "surplus":
        return None, "PV-Überschuss"                       # wird vom Ueberschuss-Controller entschieden (siehe RuleEngine.step)
    return False, t


# ------------------------------------------------------------------------------------------ Engine
class RuleEngine:
    def __init__(self, surplus_ctrl):
        self.surplus = surplus_ctrl                      # SurplusController (Verzoegerungen/Hysterese der Ueberschuss-Logik)
        self._lock = threading.RLock()
        self.owner: dict[str, str] = {}                  # geraet -> 'rule' | 'surplus' (wer es eingeschaltet hat)
        self.ran: dict[str, dict] = {}                   # regel -> {"day": iso, "min": Minuten (Tagesziel)}
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
                self.ran = {k: v for k, v in (d.get("ran") or {}).items() if isinstance(v, dict)}
        except Exception:                                # noqa: BLE001
            pass

    def _save_state(self):
        try:
            _store()._dump_json(STATE_PATH, {"owner": self.owner, "ran": self.ran}, indent=None)
        except Exception:                                # noqa: BLE001
            pass

    # ---- Hilfen fuer den Aufrufer
    def note_manual(self, dev_id: str, now: datetime | None = None, hold_min: float = 60):
        now = now or datetime.now()
        with self._lock:
            self._hold_until[dev_id] = now.replace(microsecond=0) + timedelta(minutes=hold_min)
            self.owner.pop(dev_id, None)
            self._armed.pop(dev_id, None)
        self._save_state()

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
        self.surplus.reset_timers()

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

    def owned(self, dev_id: str) -> bool:
        return dev_id in self.owner

    def set_owner(self, dev_id: str, who: str | None):
        with self._lock:
            if who:
                self.owner[dev_id] = who
            else:
                self.owner.pop(dev_id, None)
                self._armed.pop(dev_id, None)
            self._last_change[dev_id] = datetime.now()
        self._save_state()

    # ---- Kern
    def step(self, now: datetime, ctx: dict, system: dict, cfg: dict, devices: list[dict], rules: list[dict]) -> list[tuple]:
        """Ein Regelschritt. devices: Live-Status (online, on, switchable, power_w, min_on_min, min_off_min).
        Rueckgabe: Liste von (aktion 'on'|'off', geraet, begruendung, quelle 'rule'|'surplus')."""
        self._load()
        ctx = {**ctx, "now": now}
        today = now.date().isoformat()
        dt = 0.0 if self._last_step is None else min(60.0, max(0.0, (now - self._last_step).total_seconds()))
        self._last_step = now
        by_dev = {d["id"]: d for d in devices}
        hold = dict(self._hold_until)

        forced: dict[str, list[str]] = {}                # geraet -> Gruende (Regeln ohne Ueberschuss, alles erfuellt)
        candidates: dict[str, str] = {}                  # geraet -> Regelname (Ueberschuss-Kandidat, uebrige Bedingungen erfuellt)
        status: dict[str, dict] = {}
        for idx, r in enumerate(rules):
            dev = by_dev.get(r["device_id"])
            if not r.get("enabled", True):
                status[r["id"]] = {"state": "disabled", "text": "Regel ist ausgeschaltet", "conds": []}
                continue
            if not dev:
                status[r["id"]] = {"state": "off", "text": "Gerät nicht mehr vorhanden", "conds": []}
                continue
            ran = self.ran.get(r["id"], {})
            ran_min = float(ran.get("min", 0.0)) if ran.get("day") == today else 0.0
            results = [(c, *eval_condition(c, ctx, ran_min)) for c in r["conditions"]]
            conds = [{"ok": ok, "text": txt} for c, ok, txt in results if c["type"] != "surplus"]
            has_surplus = any(c["type"] == "surplus" for c in r["conditions"])
            base_ok = all(ok for c, ok, txt in results if c["type"] != "surplus")
            missing = [txt for c, ok, txt in results if ok is None and c["type"] != "surplus"]
            if has_surplus:
                conds.append({"ok": None, "text": "PV-Überschuss"})
            if not dev.get("switchable", True):
                status[r["id"]] = {"state": "off", "text": "Gerät ist nur zur Überwachung (nicht schaltbar)", "conds": conds}
                continue
            if hold.get(dev["id"], now) > now:
                status[r["id"]] = {"state": "paused", "text": "Pause nach Handschaltung", "conds": conds}
                continue
            if base_ok and not has_surplus:
                forced.setdefault(dev["id"], []).append(r["name"] or ", ".join(txt for _, _, txt in results))
                status[r["id"]] = {"state": "on", "text": "Bedingungen erfüllt", "conds": conds, "rule": r["id"]}
                if dev.get("on") and "budget" in {c["type"] for c in r["conditions"]}:
                    self.ran[r["id"]] = {"day": today, "min": ran_min + dt / 60.0}
            elif base_ok and has_surplus:
                candidates.setdefault(dev["id"], r["name"] or "Überschuss")
                status[r["id"]] = {"state": "wait", "text": "wartet auf PV-Überschuss", "conds": conds, "rule": r["id"]}
            else:
                why = ("keine Daten für: " + ", ".join(missing)) if missing else "nicht erfüllt: " + ", ".join(x["text"] for x in conds if x["ok"] is False)
                status[r["id"]] = {"state": "off", "text": why, "conds": conds}
        actions: list[tuple] = []
        # ---- 1. Regeln ohne Ueberschuss: einschalten
        for dev_id, names in forced.items():
            d = by_dev[dev_id]
            if d.get("online") and not d.get("on") and self._waited(d, now, "min_off_min"):
                actions.append(("on", d, "Regel: " + " / ".join(names), "rule"))
        # ---- 2. Was die Engine eingeschaltet hat, aber nicht mehr gebraucht wird: ausschalten
        for dev_id, who in list(self.owner.items()):
            d = by_dev.get(dev_id)
            if not d or hold.get(dev_id, now) > now:
                continue
            if not d.get("on"):
                self.set_owner(dev_id, None)
                continue
            if dev_id in forced:
                if who != "rule":
                    self.set_owner(dev_id, "rule")           # eine feste Regel haelt das Geraet jetzt an
                continue
            if who == "surplus" and dev_id in candidates:
                continue                                      # die Ueberschuss-Logik entscheidet ueber das Abschalten
            if d.get("online") and d.get("switchable", True) and self._waited(d, now, "min_on_min"):
                actions.append(("off", d, "Bedingungen nicht mehr erfüllt", "rule"))
        # ---- 3. Ueberschuss: nur Kandidaten (uebrige Bedingungen erfuellt), nach Regel-Reihenfolge
        order = {r["device_id"]: i for i, r in reversed(list(enumerate(rules))) if any(c["type"] == "surplus" for c in r["conditions"])}
        sdevs = []
        for dev_id in sorted((i for i in candidates if i not in forced and self.owner.get(i, "surplus") == "surplus"), key=lambda i: order.get(i, 10 ** 6)):
            d = dict(by_dev[dev_id])
            d["auto"] = True
            d["prio"] = order.get(dev_id, 10 ** 6)
            sdevs.append(d)
        if sdevs and system:
            act = self.surplus.step(now, system, {**cfg, "surplus_enabled": True}, sdevs)
            if act:
                a, d, why = act
                actions.append((a, by_dev[d["id"]], why if a == "off" else "PV-Überschuss: " + why, "surplus"))
        elif not sdevs:
            self.surplus.reset_timers()
        with self._lock:
            self.status = status
        return actions

    def _waited(self, d: dict, now: datetime, key: str) -> bool:
        t = self._last_change.get(d["id"])
        need = float(d.get(key) if d.get(key) is not None else 5) * 60
        return t is None or (now - t).total_seconds() >= need

    def flush(self):
        self._save_state()
