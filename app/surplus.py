"""
Ueberschuss-Automatik fuer Shelly-Geraete ("Opportunity Loads").

Ist der Akku (fast) voll und wird eingespeist, schaltet die Automatik die
freigegebenen Geraete nach Prioritaet (Reihenfolge der Geraeteliste) nacheinander
zu; bei Netzbezug oder Batterie-Entladung schaltet sie in umgekehrter
Reihenfolge wieder ab. Die Entscheidung (`SurplusController.step`) ist reine
Logik ohne Netzwerk und daher testbar - geschaltet wird vom Aufrufer.

Alle Zeiten/Schwellen sind in den Einstellungen aenderbar (Config-Schluessel
`surplus_<name>`, Standard + Grenzen siehe DEFAULTS/BOUNDS).

Sicherheits-Timer: Schaltet die Automatik ein Geraet ein, gibt sie dem Shelly einen
eingebauten Rueckschalt-Timer mit (`failsafe_min`) und verlaengert ihn laufend, solange
sie das Geraet weiter fuer richtig haelt. Stuerzt die App ab oder faellt die Messung
aus, schaltet sich das Geraet nach spaetestens dieser Zeit selbst wieder aus.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

# Standardwerte + erlaubte Grenzen (min, max) der einstellbaren Werte
DEFAULTS = {
    "on_delay_min": 3,        # Ueberschuss so lange am Stueck, bevor zugeschaltet wird
    "off_delay_min": 2,       # Bezug/Entladung so lange am Stueck, bevor abgeschaltet wird
    "off_import_w": 150,      # Netzbezug ueber diesem Wert zaehlt als "Ueberschuss weg"
    "off_discharge_w": 200,   # ebenso Batterie-Entladung
    "soc_hyst": 5,            # Abschalten erst unter (Mindest-SOC - Hysterese)
    "manual_hold_min": 60,    # nach Handschaltung so lange Automatik-Pause fuer das Geraet
    "failsafe_min": 10,       # Shelly-Eigentimer (0 = aus, sonst 2..120)
}
BOUNDS = {
    "on_delay_min": (0, 60), "off_delay_min": (0, 60), "off_import_w": (0, 5000),
    "off_discharge_w": (0, 5000), "soc_hyst": (0, 30), "manual_hold_min": (0, 1440),
    "failsafe_min": (0, 120),
}
DEFAULT_MIN_SOC = 95


def _cfg_num(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


def settings(cfg: dict) -> dict:
    """Alle einstellbaren Werte aus der Config, mit Standard und Grenzen (ungueltige Eingaben -> Standard)."""
    out = {}
    for k, default in DEFAULTS.items():
        lo, hi = BOUNDS[k]
        v = _cfg_num(cfg, "surplus_" + k, default)
        out[k] = min(hi, max(lo, v))
    if 0 < out["failsafe_min"] < 2:
        out["failsafe_min"] = 2                 # kuerzer als 2 min waere nicht sinnvoll nachzufuehren
    return out


class SurplusController:
    def __init__(self):
        self._lock = threading.Lock()
        self._on_since: datetime | None = None
        self._off_since: datetime | None = None
        self._last_change: dict[str, datetime] = {}
        self._hold_until: dict[str, datetime] = {}
        self._armed: dict[str, datetime] = {}     # dev_id -> Zeitpunkt, an dem der Shelly-Timer zuletzt gesetzt wurde
        self.events: deque = deque(maxlen=40)     # jüngste zuerst (appendleft)

    # ------------------------------------------------------------ Hilfen
    def note_manual(self, dev_id: str, now: datetime | None = None, hold_min: float = DEFAULTS["manual_hold_min"]):
        """Handschaltung: Automatik fasst dieses Geraet fuer eine Weile nicht an (und fuehrt keinen Timer mehr nach)."""
        now = now or datetime.now()
        with self._lock:
            self._hold_until[dev_id] = now.replace(microsecond=0) + timedelta(minutes=hold_min)
            self._armed.pop(dev_id, None)

    def log(self, text: str, now: datetime | None = None):
        now = now or datetime.now()
        with self._lock:
            self.events.appendleft({"ts": now.isoformat(timespec="seconds"), "text": text})

    def recent(self) -> list[dict]:
        with self._lock:
            return list(self.events)

    def reset_timers(self):
        with self._lock:
            self._on_since = self._off_since = None

    # ---- Sicherheits-Timer-Buchfuehrung
    def mark_armed(self, dev_id: str, now: datetime | None = None):
        with self._lock:
            self._armed[dev_id] = now or datetime.now()

    def disarm(self, dev_id: str):
        with self._lock:
            self._armed.pop(dev_id, None)

    def disarm_all(self):
        with self._lock:
            self._armed.clear()

    def due_rearm(self, now: datetime, cfg: dict, devices: list[dict]) -> list[dict]:
        """Geraete, deren Shelly-Timer verlaengert werden muss (nach der halben Timer-Zeit). Geraete, die
        nicht mehr an/erreichbar/freigegeben sind (oder in Handschaltungs-Pause), werden aus der Liste genommen."""
        fs = settings(cfg)["failsafe_min"]
        by_id = {d["id"]: d for d in devices}
        due = []
        with self._lock:
            for dev_id, ts in list(self._armed.items()):
                d = by_id.get(dev_id)
                if (fs <= 0 or not d or not d.get("online") or not d.get("on") or not d.get("auto")
                        or not d.get("switchable", True) or self._hold_until.get(dev_id, now) > now):
                    self._armed.pop(dev_id, None)
                    continue
                if (now - ts).total_seconds() >= fs * 60 / 2:
                    due.append(d)
        return due

    # ------------------------------------------------------------ Kern
    def step(self, now: datetime, system: dict, cfg: dict, devices: list[dict]):
        """Ein Regelschritt. `devices` = Geraete in Prioritaetsreihenfolge inkl. Live-Status
        (online, on) und Auto-Einstellungen (auto, power_w, min_on_min, min_off_min).
        Rueckgabe: None oder (aktion 'on'|'off', geraet, begruendung)."""
        if not cfg.get("surplus_enabled"):
            self.reset_timers()
            return None
        try:
            soc = float(system["battery"]["soc"])
            grid = float(system["grid"]["total"])          # > 0 = Netzbezug
            batt_w = float(system["battery"]["power"])     # > 0 = Laden
        except (KeyError, TypeError, ValueError):
            self.reset_timers()
            return None

        st = settings(cfg)
        on_delay_s, off_delay_s = st["on_delay_min"] * 60, st["off_delay_min"] * 60
        min_soc = _cfg_num(cfg, "surplus_min_soc", DEFAULT_MIN_SOC)
        feed = max(0.0, -grid)
        # Ladeleistung des vollen Akkus kann zugunsten der Geraete umgelenkt werden
        available = feed + (max(0.0, batt_w) if soc >= min_soc else 0.0)
        discharge = max(0.0, -batt_w)

        with self._lock:
            hold = dict(self._hold_until)
            last = dict(self._last_change)
        autos = [d for d in devices
                 if d.get("auto") and d.get("switchable", True) and float(d.get("power_w") or 0) > 0
                 and d.get("online") and hold.get(d["id"], now) <= now]

        def waited(d, key, default_min):
            t = last.get(d["id"])
            need = float(d.get(key) if d.get(key) is not None else default_min) * 60
            return t is None or (now - t).total_seconds() >= need

        # ---- Abschalten (hat Vorrang)
        on_list = [d for d in autos if d.get("on")]
        want_off = bool(on_list) and (grid > st["off_import_w"] or discharge > st["off_discharge_w"]
                                      or soc < min_soc - st["soc_hyst"])
        with self._lock:
            if want_off:
                self._off_since = self._off_since or now
                off_ready = (now - self._off_since).total_seconds() >= off_delay_s
            else:
                self._off_since = None
                off_ready = False
            self._on_since = None if want_off else self._on_since
        if want_off:
            if off_ready:
                for d in reversed(on_list):                      # niedrigste Prioritaet zuerst
                    if waited(d, "min_on_min", 5):
                        why = (f"Netzbezug {grid:.0f} W" if grid > st["off_import_w"] else
                               f"Batterie entlädt {discharge:.0f} W" if discharge > st["off_discharge_w"]
                               else f"Akku {soc:.0f} % unter Schwelle")
                        return self._done(now, "off", d, why)
            return None

        # ---- Zuschalten
        off_list = [d for d in autos if not d.get("on") and waited(d, "min_off_min", 5)]
        nxt = off_list[0] if off_list else None
        can_on = (nxt is not None and soc >= min_soc
                  and available >= float(nxt["power_w"]))
        with self._lock:
            if can_on:
                self._on_since = self._on_since or now
                on_ready = (now - self._on_since).total_seconds() >= on_delay_s
            else:
                self._on_since = None
                on_ready = False
        if can_on and on_ready:
            return self._done(now, "on", nxt,
                              f"Überschuss {available:.0f} W, Akku {soc:.0f} %")
        return None

    def _done(self, now, action, dev, why):
        with self._lock:
            self._last_change[dev["id"]] = now
            self._on_since = self._off_since = None
        return action, dev, why
