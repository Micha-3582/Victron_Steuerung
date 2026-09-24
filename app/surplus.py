"""
Ueberschuss-Automatik fuer Shelly-Geraete ("Opportunity Loads").

Ist der Akku (fast) voll und wird eingespeist, schaltet die Automatik die
freigegebenen Geraete nach Prioritaet (Reihenfolge der Geraeteliste) nacheinander
zu; bei Netzbezug oder Batterie-Entladung schaltet sie in umgekehrter
Reihenfolge wieder ab. Die Entscheidung (`SurplusController.step`) ist reine
Logik ohne Netzwerk und daher testbar - geschaltet wird vom Aufrufer.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timedelta

# Feste Feinabstimmung (bewusst nicht in der Oberflaeche)
ON_DELAY_S = 180          # Ueberschuss so lange am Stueck, bevor zugeschaltet wird
OFF_DELAY_S = 120         # Bezug/Entladung so lange am Stueck, bevor abgeschaltet wird
OFF_IMPORT_W = 150        # Netzbezug ueber diesem Wert zaehlt als "Ueberschuss weg"
OFF_DISCHARGE_W = 200     # ebenso Batterie-Entladung
SOC_HYST = 5              # Abschalten erst unter (Mindest-SOC - 5 %)
MANUAL_HOLD_MIN = 60      # nach Handschaltung so lange Automatik-Pause fuer das Geraet

DEFAULT_MIN_SOC = 95


def _cfg_num(cfg: dict, key: str, default: float) -> float:
    try:
        return float(cfg.get(key, default))
    except (TypeError, ValueError):
        return default


class SurplusController:
    def __init__(self):
        self._lock = threading.Lock()
        self._on_since: datetime | None = None
        self._off_since: datetime | None = None
        self._last_change: dict[str, datetime] = {}
        self._hold_until: dict[str, datetime] = {}
        self.events: deque = deque(maxlen=40)     # jüngste zuerst (appendleft)

    # ------------------------------------------------------------ Hilfen
    def note_manual(self, dev_id: str, now: datetime | None = None):
        """Handschaltung: Automatik fasst dieses Geraet fuer eine Weile nicht an."""
        now = now or datetime.now()
        with self._lock:
            self._hold_until[dev_id] = now.replace(microsecond=0) + timedelta(minutes=MANUAL_HOLD_MIN)

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
                 and d.get("online")
                 and hold.get(d["id"], now) <= now]

        def waited(d, key, default_min):
            t = last.get(d["id"])
            need = float(d.get(key) if d.get(key) is not None else default_min) * 60
            return t is None or (now - t).total_seconds() >= need

        # ---- Abschalten (hat Vorrang)
        on_list = [d for d in autos if d.get("on")]
        want_off = bool(on_list) and (grid > OFF_IMPORT_W or discharge > OFF_DISCHARGE_W
                                      or soc < min_soc - SOC_HYST)
        with self._lock:
            if want_off:
                self._off_since = self._off_since or now
                off_ready = (now - self._off_since).total_seconds() >= OFF_DELAY_S
            else:
                self._off_since = None
                off_ready = False
            self._on_since = None if want_off else self._on_since
        if want_off:
            if off_ready:
                for d in reversed(on_list):                      # niedrigste Prioritaet zuerst
                    if waited(d, "min_on_min", 5):
                        why = (f"Netzbezug {grid:.0f} W" if grid > OFF_IMPORT_W else
                               f"Batterie entlädt {discharge:.0f} W" if discharge > OFF_DISCHARGE_W
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
                on_ready = (now - self._on_since).total_seconds() >= ON_DELAY_S
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

