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


def _bucket(flows: dict, soc: float | None, fixed_price_ct: float = 0.0) -> dict:             # fixed_price_ct: Preis des jeweiligen Tages
    f = {k: round(float(flows.get(k, 0.0)), 4) for k in store._FLOW_KEYS}
    b = dict(f)
    b["solar"] = round(f["s_load"] + f["s_batt"] + f["s_grid"], 4)
    b["verbrauch"] = round(f["s_load"] + f["b_load"] + f["g_load"], 4)
    # Bei dynamischem Tarif (Tibber) ist der Preis von damals nicht bekannt -> 0. Bei Festpreis ist er bekannt: Netzbezug x Preis.
    b["grid_cost_ct"] = round((f["g_load"] + f["g_batt"]) * fixed_price_ct, 4) if fixed_price_ct > 0 else 0.0
    b["restored"] = True                         # stammt aus dem VRM (siehe store._row_from_bucket)
    if soc is not None:
        b.update(soc_min=soc, soc_max=soc, soc_sum=soc, soc_n=1)
    else:
        b.update(soc_min=None, soc_max=None, soc_sum=0.0, soc_n=0)
    return b


def run(apply: bool = False, now: datetime | None = None, days: int | None = None) -> dict:
    """`days`: Anzahl Tage rueckwirkend (Default: store.VRM_RESTORE_LOOKBACK_DAYS = 35 fuer die
    normale Luecken-Kontrolle). Fuer einen einmaligen historischen Nachimport (z.B. seit
    Installationsdatum) kann hier ein deutlich groesserer Wert uebergeben werden - vrm.fetch_flow_slots/
    fetch_soc_slots zerlegen den Zeitraum ohnehin in CHUNK_DAYS=7-Tage-Haeppchen, das ist also
    unabhaengig von der Laenge sicher (anders als die fruehere 365-Tage-Falle bei _LIFETIME_CHUNK_DAYS)."""
    c = vrm.load_credentials()
    if not (c.get("token") and c.get("installation_id")):
        raise vrm.VrmError("VRM-Zugang ist noch nicht eingerichtet")
    now = now or datetime.now()
    lookback = days if days is not None else store.VRM_RESTORE_LOOKBACK_DAYS
    start = (now - timedelta(days=lookback - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)     # laufender Slot bleibt bei der App
    flows, used = vrm.fetch_flow_slots(c, start, end)
    have = store.history_keys()
    end_key = f"{end:%Y-%m-%dT%H:%M}"
    new = {k: v for k, v in flows.items() if k not in have and k < end_key}
    soc = vrm.fetch_soc_slots(c, start, end) if new else {}
    cfg = store.load_config()
    fixed_mode = cfg.get("tariff_mode") == "fixed" and float(cfg.get("fixed_price_ct") or 0.0) > 0
    slots = {k: _bucket(v, soc.get(k), store.fixed_price_for_day(k[:10]) if fixed_mode else 0.0) for k, v in new.items()}       # Preis je Tag (Tarifwechsel)
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
