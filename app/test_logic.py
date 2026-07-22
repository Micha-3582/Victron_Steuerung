"""
Tests für die portierte V39.4-Logik.
Ausführen:  python test_logic.py
"""
from datetime import datetime, timedelta

from logic import decide, PersistentState, ESS_CHARGE, ESS_IDLE


def make_prices(start: datetime, ct_list):
    """Baut Tibber-artige Einträge (total in EUR/kWh) ab start, 15-Min-Slots.
    Tibber liefert eigentlich Stunden-Slots; wir testen mit 15-Min-Aufloesung."""
    out = []
    t = start
    for ct in ct_list:
        out.append({"startsAt": t.isoformat(), "total": ct / 100.0})
        t += timedelta(minutes=15)
    return out


def scenario(name, **kw):
    d = decide(**kw)
    print(f"\n[{name}]")
    print(f"  Strategie : {d.strategy}")
    print(f"  laden jetzt: {d.allow_now}  (ESS {d.ess_mode})")
    print(f"  Jetzt-Preis: {d.now_price} ct | Bilanz {d.balance} kWh")
    print(f"  Plan       : {d.plan_windows or '-'}  ({len(d.plan)} Slots)")
    return d


def main():
    fails = 0

    # 1) Nachts, SOC kritisch niedrig, günstig -> muss laden (Notbremse/Nacht)
    now = datetime(2026, 1, 15, 2, 0)  # 02:00, Winter
    prices = make_prices(now, [18, 20, 22, 25, 30, 35] * 8)  # billig jetzt
    d = scenario("Nacht, SOC 20%, billig", soc=20,
                 price_entries=prices, solar_today_raw=3, solar_tom_raw=3,
                 state=PersistentState(), now=now)
    if not d.allow_now or d.ess_mode != ESS_CHARGE:
        print("  FAIL: sollte laden"); fails += 1

    # 2) Mittags, SOC hoch, teuer, viel PV -> darf NICHT laden
    now = datetime(2026, 7, 15, 12, 0)
    prices = make_prices(now, [40, 42, 45, 44, 38, 30] * 8)
    d = scenario("Mittag, SOC 85%, teuer, viel PV", soc=85,
                 price_entries=prices, solar_today_raw=30, solar_tom_raw=30,
                 state=PersistentState(), now=now)
    if d.allow_now or d.ess_mode != ESS_IDLE:
        print("  FAIL: sollte NICHT laden"); fails += 1

    # 3) Manueller Override -> immer laden
    d = scenario("Manual Override", soc=50,
                 price_entries=make_prices(datetime(2026, 5, 1, 15, 0), [30] * 20),
                 solar_today_raw=10, solar_tom_raw=10, state=PersistentState(),
                 now=datetime(2026, 5, 1, 15, 0), manual_override=True)
    if not d.allow_now or d.strategy != "MANUELL":
        print("  FAIL: Override sollte laden"); fails += 1

    # 4) Vor Morgen-Peak, SOC knapp -> Peak-Schutz aktiv
    now = datetime(2026, 1, 15, 5, 0)  # 05:00 Winter, wenig PV
    prices = make_prices(now, [22, 24, 20, 26, 40, 45] * 8)
    d = scenario("05:00, SOC 35%, vor Morgen-Peak", soc=35,
                 price_entries=prices, solar_today_raw=2, solar_tom_raw=2,
                 state=PersistentState(), now=now)
    if "Peak" not in d.strategy and not d.allow_now:
        print("  FAIL: Peak-Schutz oder Laden erwartet"); fails += 1

    # 5) Keine Preisdaten -> idle
    d = scenario("Keine Preise", soc=50, price_entries=[],
                 solar_today_raw=5, solar_tom_raw=5, state=PersistentState(),
                 now=datetime(2026, 5, 1, 14, 0))
    if d.allow_now or d.reason != "Keine Preisdaten":
        print("  FAIL: sollte idle sein"); fails += 1

    print("\n" + "=" * 40)
    if fails:
        print(f"{fails} Test(s) FEHLGESCHLAGEN")
        raise SystemExit(1)
    print("Alle Tests bestanden.")


if __name__ == "__main__":
    main()
