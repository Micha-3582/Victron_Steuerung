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

    # 6) Minimaler SOC am Cerbo (Untergrenze): entnehmbar ist nur, was darueber liegt -> Bilanz sinkt um Untergrenze x Kapazitaet
    from logic import Params
    now = datetime(2026, 5, 1, 20, 0)
    prices = make_prices(now, [30] * 40)
    kw = dict(soc=50, price_entries=prices, solar_today_raw=5, solar_tom_raw=5, now=now)
    d0 = decide(state=PersistentState(), params=Params(battery_usable_kwh=20.0, soc_floor_pct=0.0), **kw)
    d15 = decide(state=PersistentState(), params=Params(battery_usable_kwh=20.0, soc_floor_pct=15.0), **kw)
    print(f"Untergrenze: Bilanz ohne {d0.balance} kWh, mit 15 % Untergrenze {d15.balance} kWh")
    if abs((d0.balance - d15.balance) - 3.0) > 0.02:
        print("  FAIL: Untergrenze 15 % x 20 kWh = 3 kWh weniger Bilanz erwartet"); fails += 1
    dn = decide(state=PersistentState(), params=Params(battery_usable_kwh=20.0, soc_floor_pct=60.0), **kw)
    if dn.balance > d0.balance:
        print("  FAIL: Bilanz darf mit Untergrenze nie steigen"); fails += 1

    # 7) Guenstig-Vorkauf vor Abend-Peak: JETZT sehr guenstig, Abend-Peak sehr teuer -> Ziel steigt von
    # min_peak_soc auf das hoehere Komfort-Ziel, obwohl die blosse Sicherheit (min_peak_soc) schon reicht.
    now = datetime(2026, 9, 27, 18, 0)                                     # 1 h vor dem Abend-Peak (19-21 Uhr)
    prices7 = [21.0] * 4 + [50.0] * 8 + [30.0] * 12                        # 18-19 Uhr 21ct, 19-21 Uhr 50ct (Peak), danach 30ct
    entries7 = make_prices(now, prices7)
    p7 = Params(battery_usable_kwh=20.0, daily_usage_kwh=24.0, min_peak_soc=40.0,
                evening_comfort_soc=65.0, valley_min_saving_ct=15.0,
                morning_peak_end=9, evening_peak_start=19, evening_peak_end=21)
    d7 = scenario("Guenstig-Vorkauf, SOC 46% (Sicherheit erfuellt, Komfort nicht)", soc=46,
                  price_entries=entries7, solar_today_raw=0, solar_tom_raw=90, state=PersistentState(), now=now, params=p7)
    if not d7.allow_now or "Peak" not in d7.strategy or "günstig" not in d7.strategy:
        print("  FAIL: sollte wegen Komfort-Vorkauf laden"); fails += 1
    p7b = Params(battery_usable_kwh=20.0, daily_usage_kwh=24.0, min_peak_soc=40.0,
                 morning_peak_end=9, evening_peak_start=19, evening_peak_end=21)          # kein Komfort-Ziel (Standard 0 = aus)
    d7b = scenario("Gleiche Lage OHNE Komfort-Ziel (Standard)", soc=46,
                   price_entries=entries7, solar_today_raw=0, solar_tom_raw=90, state=PersistentState(), now=now, params=p7b)
    if d7b.allow_now:
        print("  FAIL: ohne Komfort-Ziel sollte hier nichts laden (min_peak_soc schon erreicht)"); fails += 1
    prices7c = [30.0] * 4 + [33.0] * 8 + [30.0] * 12                       # Abend nur wenig teurer (33 statt 50 ct)
    d7c = scenario("Komfort-Ziel, aber Abend kaum teurer -> kein Vorkauf", soc=46,
                   price_entries=make_prices(now, prices7c), solar_today_raw=0, solar_tom_raw=90,
                   state=PersistentState(), now=now, params=p7)
    if d7c.allow_now:
        print("  FAIL: bei kleinem Preisunterschied sollte der Komfort-Vorkauf nicht greifen"); fails += 1
    d7d = scenario("Komfort-Ziel schon erreicht (SOC 70%)", soc=70,
                   price_entries=entries7, solar_today_raw=0, solar_tom_raw=90, state=PersistentState(), now=now, params=p7)
    if d7d.allow_now:
        print("  FAIL: Komfort-Ziel schon erreicht, sollte nicht laden"); fails += 1

    print("\n" + "=" * 40)
    if fails:
        print(f"{fails} Test(s) FEHLGESCHLAGEN")
        raise SystemExit(1)
    print("Alle Tests bestanden.")


if __name__ == "__main__":
    main()
