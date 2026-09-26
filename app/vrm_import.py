"""
Fehlende Verlaufsdaten (history.json) aus dem Victron-VRM-Portal nachholen.

`run(apply=False)` ist die Vorschau (schreibt nichts), `run(apply=True)` ergaenzt NUR Viertelstunden,
die in history.json fehlen - vorhandene Messwerte werden nie ueberschrieben. Vor dem Schreiben wird
eine Sicherung angelegt (siehe store.import_history_slots).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import store
import vrm


def _bucket(flows: dict, soc: float | None) -> dict:
    f = {k: round(float(flows.get(k, 0.0)), 4) for k in store._FLOW_KEYS}
    b = dict(f)
    b["solar"] = round(f["s_load"] + f["s_batt"] + f["s_grid"], 4)
    b["verbrauch"] = round(f["s_load"] + f["b_load"] + f["g_load"], 4)
    b["grid_cost_ct"] = 0.0                      # Preis von damals ist nicht bekannt
    b["restored"] = True                         # stammt aus dem VRM (siehe store._row_from_bucket)
    if soc is not None:
        b.update(soc_min=soc, soc_max=soc, soc_sum=soc, soc_n=1)
    else:
        b.update(soc_min=None, soc_max=None, soc_sum=0.0, soc_n=0)
    return b


def run(apply: bool = False, now: datetime | None = None) -> dict:
    c = vrm.load_credentials()
    if not (c.get("token") and c.get("installation_id")):
        raise vrm.VrmError("VRM-Zugang ist noch nicht eingerichtet")
    now = now or datetime.now()
    start = (now - timedelta(days=store._HISTORY_KEEP_DAYS - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)     # laufender Slot bleibt bei der App
    flows, used = vrm.fetch_flow_slots(c, start, end)
    have = store.history_keys()
    end_key = f"{end:%Y-%m-%dT%H:%M}"
    new = {k: v for k, v in flows.items() if k not in have and k < end_key}
    soc = vrm.fetch_soc_slots(c, start, end) if new else {}
    slots = {k: _bucket(v, soc.get(k)) for k, v in new.items()}
    days: dict[str, dict] = {}
    for k, b in slots.items():
        d = days.setdefault(k[:10], {"day": k[:10], "slots": 0, "solar": 0.0, "verbrauch": 0.0,
                                     "import": 0.0, "export": 0.0})
        d["slots"] += 1
        d["solar"] += b["solar"]
        d["verbrauch"] += b["verbrauch"]
        d["import"] += b["g_load"] + b["g_batt"]
        d["export"] += b["s_grid"] + b["b_grid"]
    rows = [{**d, **{k: round(d[k], 1) for k in ("solar", "verbrauch", "import", "export")}}
            for d in sorted(days.values(), key=lambda x: x["day"])]
    res = {"applied": False, "interval": " / ".join(sorted(used)) or "keine", "days": rows,
           "slots": len(slots), "soc": bool(soc)}
    if apply and slots:
        res["added"] = store.import_history_slots(slots)
        res["applied"] = True
    return res
