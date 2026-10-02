"""
Ablauf-Regeln ("Programme"): WENN-Bedingung tritt ein -> DANN-Schritte nacheinander (mit Warten), WENN-Bedingung faellt weg -> SONST-Schritte.

Anders als die Zustands-Regeln (rules.py, 'Zustand halten') wird hier nur bei einem WECHSEL der Bedingung gestartet (steigende Flanke = DANN,
fallende Flanke = SONST). Ein Ablauf besteht aus Schritten:
  switch    Geraet ein/aus          wait      Sekunden warten (nicht blockierend)
  setpoint  Solltemperatur setzen   notify    Telegram-Nachricht
  virtual   eigenen Schalter setzen/Knopf druecken
Laufende Ablaeufe ueberleben einen Neustart der App (flows_state.json); Schritte, die waehrend einer Pause faellig wurden, laufen danach nach.
Loest dieselbe Regel erneut aus, startet ihr Ablauf von vorn (der alte wird abgebrochen). Reine Logik ohne Netzwerk - ausgefuehrt wird in webapp.py.
"""
from __future__ import annotations

import threading
import os
import time
import uuid
from datetime import datetime

import rules

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_DIR, "flows_state.json")


def _store():
    import store
    return store


def is_flow(r: dict) -> bool:
    return r.get("mode") == "flow" and "when" in r


class FlowEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self.last: dict[str, bool] = {}              # regel -> letzter bekannter Wert der WENN-Bedingung
        self.runs: list[dict] = []                   # laufende Ablaeufe
        self.status: dict[str, dict] = {}
        self._loaded = False

    # ---- Persistenz
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        try:
            d = _store()._load_json_recovering(STATE_PATH, lambda: {})
            if isinstance(d, dict):
                self.last = {k: bool(v) for k, v in (d.get("last") or {}).items()}
                self.runs = [x for x in (d.get("runs") or []) if isinstance(x, dict) and isinstance(x.get("steps"), list)]
        except Exception:                            # noqa: BLE001
            pass

    def _save(self):
        try:
            _store()._dump_json(STATE_PATH, {"last": self.last, "runs": self.runs}, indent=None)
        except Exception:                            # noqa: BLE001
            pass

    # ---- Auswertung
    def step(self, now: datetime, ctx: dict, flows: list[dict]) -> list[tuple]:
        """Bedingungen auswerten, bei Wechsel Ablauf starten. Rueckgabe: Ereignisse fuers Logbuch [(art, regel, text)]."""
        self._load()
        ctx = {**ctx, "now": now}
        events: list[tuple] = []
        ids = {r["id"] for r in flows}
        status: dict[str, dict] = {}
        changed = False
        for gone in [k for k in self.last if k not in ids]:
            self.last.pop(gone, None)
            changed = True
        for run in [x for x in self.runs if x["rule_id"] not in ids]:
            self.runs.remove(run)
            events.append(("cancel", {"id": run["rule_id"], "name": run.get("rule", "")}, "Regel wurde gelöscht – Ablauf abgebrochen"))
            changed = True
        for r in flows:
            rid = r["id"]
            if not r.get("enabled", True):
                if self.last.pop(rid, None) is not None:
                    changed = True
                for run in [x for x in self.runs if x["rule_id"] == rid]:
                    self.runs.remove(run)
                    events.append(("cancel", r, "Regel ausgeschaltet – Ablauf abgebrochen"))
                    changed = True
                status[rid] = {"state": "disabled", "text": "Regel ist ausgeschaltet", "conds": []}
                continue
            group = rules._group(r["when"]["mode"], r["when"]["conds"])
            res, txt = rules.eval_condition(group, ctx)
            per = [rules.eval_condition(c, ctx) for c in r["when"]["conds"]]
            prev = self.last.get(rid)
            if res is not None:
                if prev is not None and res != prev:
                    branch = "then" if res else "else"
                    steps = r.get(branch) or []
                    if steps:
                        for old in [x for x in self.runs if x["rule_id"] == rid]:
                            self.runs.remove(old)
                            events.append(("cancel", r, "neu ausgelöst – der laufende Ablauf wird durch den neuen ersetzt"))
                        self.runs.append({"id": uuid.uuid4().hex[:8], "rule_id": rid, "rule": r.get("name", ""), "branch": branch,
                                          "steps": steps, "idx": 0, "due": None, "start": time.time()})
                        events.append(("start", r, ("Auslöser: " if res else "Bedingung nicht mehr erfüllt: ") + txt + f" – {'DANN' if res else 'SONST'}-Ablauf startet"))
                if prev != res:
                    changed = True
                self.last[rid] = res
            mine = [x for x in self.runs if x["rule_id"] == rid]
            if mine:
                run = mine[0]
                nxt = f" – nächster Schritt in {self._fmt(run['due'] - time.time())}" if run.get("due") else ""
                status[rid] = {"state": "on", "text": f"Ablauf läuft: Schritt {min(run['idx'] + 1, len(run['steps']))} von {len(run['steps'])}{nxt}",
                               "conds": [{"ok": ok, "text": t} for ok, t in per]}
            else:
                status[rid] = {"state": "off", "text": "wartet auf den Auslöser" if res is not None else "keine Daten für: " + ", ".join(t for ok, t in per if ok is None),
                               "conds": [{"ok": ok, "text": t} for ok, t in per]}
        with self._lock:
            self.status = status
        if changed or events:
            self._save()
        return events

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = max(0, int(sec))
        if sec >= 3600:
            return f"{sec // 3600} h {(sec % 3600) // 60} min"
        return f"{sec // 60} min" if sec >= 120 else f"{sec} s"

    # ---- Ausfuehren
    def advance(self, now_ts: float | None = None, skip_waits: bool = False) -> list[dict]:
        """Faellige Schritte holen: [{'rule_id','rule','branch','step','after_s'}]. Warten setzt nur die naechste Faelligkeit.
        skip_waits (Trockenlauf): Wartezeiten werden uebersprungen, 'after_s' sagt, wann der Schritt dran gewesen waere."""
        self._load()
        now_ts = time.time() if now_ts is None else now_ts
        out: list[dict] = []
        changed = False
        for run in list(self.runs):
            offset = float(run.get("offset", 0.0))
            while run["idx"] < len(run["steps"]):
                if run.get("due") and run["due"] > now_ts and not skip_waits:
                    break
                st = run["steps"][run["idx"]]
                run["idx"] += 1
                changed = True
                if st.get("type") == "wait":
                    if skip_waits:
                        offset += st["seconds"]
                        run["offset"] = offset
                    else:
                        run["due"] = now_ts + st["seconds"]
                    continue
                run["due"] = None
                out.append({"rule_id": run["rule_id"], "rule": run.get("rule", ""), "branch": run["branch"], "step": st, "after_s": offset})
            if run["idx"] >= len(run["steps"]):
                self.runs.remove(run)
                changed = True
        if changed:
            self._save()
        return out

    def next_due(self) -> float | None:
        dues = [x["due"] for x in self.runs if x.get("due")]
        return min(dues) if dues else None

    def cancel_all(self, why: str) -> list[tuple]:
        self._load()
        ev = [("cancel", {"id": x["rule_id"], "name": x.get("rule", "")}, why) for x in self.runs]
        if self.runs:
            self.runs.clear()
            self._save()
        return ev

    def forget(self):
        """Hauptschalter aus: Bedingungen neu einlesen, ohne beim Wiedereinschalten sofort auszuloesen."""
        if self.last:
            self.last.clear()
            self._save()
