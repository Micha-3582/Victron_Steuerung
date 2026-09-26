"""
Ladeplan-Simulation ("EMS-Planer") - reine Rechnung, keine Hardware, kein Netz.

Fragt: Wann soll ich in den naechsten Stunden aus dem Netz laden, damit die Stromkosten bis zum Ende des
Prognosezeitraums minimal sind? Grundlage sind Stundenwerte fuer Solar und Verbrauch (VRM), die Tibber-Preise je
Viertelstunde und der Akku (Kapazitaet, Ladeleistung, Grenzen, Wirkungsgrad).

Verfahren: dynamische Programmierung ueber den Ladezustand (rueckwaerts ueber alle Slots). Je Slot gibt es zwei
Aktionen - "laden" (mit der festen Ladeleistung, wie es die Steuerung ueber den ESS-Modus tut) oder "nicht laden".
Verglichen wird der Plan mit (a) dem Plan der bisherigen Steuerung und (b) gar keinem Netzladen. Alle drei laufen
durch dasselbe Akkumodell, damit die Kosten vergleichbar sind.

Es steuert NICHTS - Ergebnis nur zur Anzeige und zum Vergleich.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

EFF_CHARGE = 0.95        # Wirkungsgrad Laden (AC -> Akku)
EFF_DISCHARGE = 0.95     # Wirkungsgrad Entladen (Akku -> Verbrauch)
LEVEL_KWH = 0.25         # Rasterung des Ladezustands fuer die Optimierung
MIN_GAIN_CT = 1.0        # Laden nur, wenn es in diesem Slot mindestens so viel spart (verhindert Zick-Zack-Plaene fuer Cent-Betraege)


@dataclass
class Step:
    start: datetime
    dur_h: float          # Dauer in Stunden (meist 0,25)
    price: float          # ct/kWh
    pv_kwh: float         # erwartete Solarenergie in diesem Slot
    cons_kwh: float       # erwarteter Verbrauch in diesem Slot


@dataclass
class Battery:
    cap: float            # nutzbare Kapazitaet kWh
    grid_cap: float       # bis hierhin darf aus dem Netz geladen werden (kWh)
    floor: float          # darunter gilt der Akku als leer (kWh)
    charge_kw: float
    eff_c: float = EFF_CHARGE
    eff_d: float = EFF_DISCHARGE


def battery_from_params(p, floor_soc: float | None = None) -> Battery:
    cap = float(p.battery_usable_kwh)
    grid_cap = min(cap * float(p.max_charge_soc) / 100.0, cap - float(p.pv_reserve_kwh))
    floor = cap * float(p.night_safety_soc if floor_soc is None else floor_soc) / 100.0
    return Battery(cap=cap, grid_cap=max(0.0, grid_cap), floor=min(max(0.0, floor), cap), charge_kw=float(p.charge_power_w) / 1000.0)


def _advance(b: Battery, e: float, s: Step, charge: bool) -> tuple[float, float, float, float]:
    """Ein Slot. Rueckgabe: (neuer Ladezustand, Netz-Ladeenergie, Netzbezug fuer Verbrauch, Kosten in ct)."""
    used = min(s.pv_kwh, s.cons_kwh)
    surplus, deficit = s.pv_kwh - used, s.cons_kwh - used
    e1 = e + min(surplus * b.eff_c, max(0.0, b.cap - e))                 # Solaruebeschuss laedt den Akku bis voll
    g = 0.0
    if charge and e1 < b.grid_cap:
        room = b.grid_cap - e1
        g = min(b.charge_kw * s.dur_h, room / b.eff_c)                    # Netzladen bis zur Ladegrenze
        e1 += g * b.eff_c
    served = min(deficit, max(0.0, e1 - b.floor) * b.eff_d)               # Verbrauch aus dem Akku (nicht unter die Reserve)
    e2 = e1 - served / b.eff_d
    grid_def = deficit - served
    return e2, g, grid_def, s.price * (g + grid_def)


def _terminal_value(steps: list[Step]) -> float:
    """Was ein kWh im Akku am Ende wert ist: das, was das Wiederaufladen zu den guenstigsten Zeiten kosten wuerde."""
    prices = sorted(s.price for s in steps)
    if not prices:
        return 0.0
    q = prices[: max(1, len(prices) // 4)]
    return sum(q) / len(q)


def optimize(steps: list[Step], e0: float, b: Battery) -> list[bool]:
    """Kostenoptimale Ladeentscheidung je Slot (True = aus dem Netz laden)."""
    n = int(b.cap / LEVEL_KWH) + 1
    val = _terminal_value(steps) * b.eff_d
    nxt = [-val * (i * LEVEL_KWH) for i in range(n)]                       # Kosten-bis-Ende am Horizont (negativer Restwert)

    def interp(v: list[float], e: float) -> float:
        x = min(max(e, 0.0), (n - 1) * LEVEL_KWH) / LEVEL_KWH
        i = int(x)
        if i >= n - 1:
            return v[n - 1]
        f = x - i
        return v[i] * (1 - f) + v[i + 1] * f

    decisions: list[list[bool]] = [None] * len(steps)                      # type: ignore[list-item]
    for t in range(len(steps) - 1, -1, -1):
        cur = [0.0] * n
        dec = [False] * n
        s = steps[t]
        for i in range(n):
            e = i * LEVEL_KWH
            e_a, _, _, c_a = _advance(b, e, s, False)
            best, choose = c_a + interp(nxt, e_a), False
            e_b, g_b, _, c_b = _advance(b, e, s, True)
            if g_b > 1e-9:
                cost_b = c_b + interp(nxt, e_b)
                if cost_b < best - MIN_GAIN_CT:
                    best, choose = cost_b, True
            cur[i], dec[i] = best, choose
        decisions[t] = dec
        nxt = cur
    # Vorwaerts: konkrete Entscheidungen fuer den tatsaechlichen Anfangszustand
    out, e = [], e0
    for t, s in enumerate(steps):
        x = min(max(e, 0.0), (n - 1) * LEVEL_KWH) / LEVEL_KWH
        i = int(round(x))
        charge = decisions[t][min(i, n - 1)]
        out.append(charge)
        e, _, _, _ = _advance(b, e, s, charge)
    return out


def simulate(steps: list[Step], e0: float, b: Battery, charges: list[bool]) -> dict:
    """Laeuft einen Plan durch das Akkumodell."""
    e, rows = e0, []
    tot_cost = grid_charge = grid_def = 0.0
    for s, ch in zip(steps, charges):
        e, g, gd, c = _advance(b, e, s, ch)
        tot_cost += c
        grid_charge += g
        grid_def += gd
        rows.append({"start": s.start, "soc": e / b.cap * 100.0, "charge_kwh": g, "grid_deficit_kwh": gd, "price": s.price, "cost_ct": c})
    return {"rows": rows, "cost_ct": tot_cost, "grid_charge_kwh": grid_charge, "grid_deficit_kwh": grid_def, "end_kwh": e}


def windows(steps: list[Step], charges: list[bool]) -> list[dict]:
    """Zusammenhaengende Ladefenster [{'from','to','kwh'?}]."""
    out, cur = [], None
    for s, ch in zip(steps, charges):
        end = s.start + timedelta(hours=s.dur_h)
        if ch:
            if cur and cur["_end"] == s.start:
                cur["_end"] = end
                cur["slots"] += 1
            else:
                cur = {"_start": s.start, "_end": end, "slots": 1}
                out.append(cur)
        else:
            cur = None
    return [{"from": f"{w['_start']:%H:%M}", "to": f"{w['_end']:%H:%M}", "day": w["_start"].date().isoformat(), "slots": w["slots"]} for w in out]


def build_steps(now: datetime, slots: list, solar_wh: dict, cons_wh: dict | None, daily_usage_kwh: float) -> list[Step]:
    """slots: logic.Slot-Liste (start, price ct/kWh), ab jetzt. solar_wh/cons_wh: {(datum_iso, stunde): Wh je Stunde}."""
    steps = []
    for i, sl in enumerate(slots):
        if i + 1 < len(slots):
            dur = (slots[i + 1].start - sl.start).total_seconds() / 3600.0
        else:
            dur = (sl.start - slots[i - 1].start).total_seconds() / 3600.0 if i else 0.25
        dur = min(max(dur, 0.25), 1.0)
        key = (sl.start.date().isoformat(), sl.start.hour)
        if key not in solar_wh:
            break                                            # ab hier fehlt die Prognose -> Horizont endet
        pv = solar_wh[key] / 1000.0 * dur
        cons = (cons_wh[key] / 1000.0 * dur) if cons_wh and key in cons_wh else daily_usage_kwh / 24.0 * dur
        steps.append(Step(start=sl.start, dur_h=dur, price=sl.price, pv_kwh=pv, cons_kwh=cons))
    return steps


def run(now: datetime, soc: float, params, slots: list, solar_wh: dict, cons_wh: dict | None,
        existing_plan_starts: set, floor_soc: float | None = None) -> dict | None:
    """Kompletter Lauf. existing_plan_starts: Startzeiten (datetime) der Slots, die die bisherige Steuerung eingeplant hat."""
    steps = build_steps(now, slots, solar_wh, cons_wh, float(params.daily_usage_kwh))
    if len(steps) < 4:
        return None
    b = battery_from_params(params, floor_soc)
    if b.cap <= 0:
        return None
    e0 = min(max(soc, 0.0), 100.0) / 100.0 * b.cap
    sim_ch = optimize(steps, e0, b)
    ex_ch = [s.start in existing_plan_starts for s in steps]
    none_ch = [False] * len(steps)
    sim, ex, none = simulate(steps, e0, b, sim_ch), simulate(steps, e0, b, ex_ch), simulate(steps, e0, b, none_ch)

    tv = _terminal_value(steps)

    def slim(res, charges):
        # net_ct: Kosten abzueglich Restwert des Akkuinhalts am Ende - nur so sind Plaene mit unterschiedlichem Endstand vergleichbar
        return {"cost_ct": round(res["cost_ct"], 1), "net_ct": round(res["cost_ct"] - tv * b.eff_d * res["end_kwh"], 1),
                "end_soc": round(res["end_kwh"] / b.cap * 100, 1), "grid_charge_kwh": round(res["grid_charge_kwh"], 2),
                "grid_deficit_kwh": round(res["grid_deficit_kwh"], 2), "windows": windows(steps, charges),
                "soc": [round(r["soc"], 1) for r in res["rows"]]}
    return {
        "times": [s.start.isoformat(timespec="minutes") for s in steps],
        "prices": [round(s.price, 2) for s in steps],
        "pv_kwh": [round(s.pv_kwh, 3) for s in steps],
        "cons_kwh": [round(s.cons_kwh, 3) for s in steps],
        "sim": {**slim(sim, sim_ch), "charge_kwh": [round(r["charge_kwh"], 3) for r in sim["rows"]]},
        "current": {**slim(ex, ex_ch), "charge_kwh": [round(r["charge_kwh"], 3) for r in ex["rows"]]},
        "none": slim(none, none_ch),
        "horizon_end": (steps[-1].start + timedelta(hours=steps[-1].dur_h)).isoformat(timespec="minutes"),
        "assumptions": {"floor_soc": round(b.floor / b.cap * 100, 1), "grid_cap_soc": round(b.grid_cap / b.cap * 100, 1),
                        "charge_kw": b.charge_kw, "eff_charge": b.eff_c, "eff_discharge": b.eff_d,
                        "terminal_value_ct": round(tv, 1)},
    }
