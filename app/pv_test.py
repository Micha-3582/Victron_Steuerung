#!/usr/bin/env python3
"""
PV-Prognose-Test (forecast.solar)
=================================
Holt die PV-Prognose direkt von forecast.solar (ersetzt den pvforecast-Adapter).
Fragt jede Dachfläche einzeln ab und summiert - wie der Adapter.
Liest Standort + Flächen aus config.json.

Nutzung:
  pip install requests
  python pv_test.py

Hinweis forecast.solar (kostenlos, ohne Key): max ~12 Abrufe/Stunde pro IP.
Bei 4 Flächen also sparsam testen.
"""
import json
import os
import sys
from datetime import date, timedelta

try:
    import requests
except ImportError:
    sys.exit("Bitte zuerst installieren:  pip install requests")

BASE = "https://api.forecast.solar/estimate"


def load_cfg():
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(cfg, encoding="utf-8") as f:
        return json.load(f)


def fetch_plane(lat, lon, plane):
    url = f"{BASE}/{lat}/{lon}/{plane['declination']}/{plane['azimuth']}/{plane['kwp']}"
    r = requests.get(url, timeout=20)
    if r.status_code == 429:
        raise RuntimeError("Rate-Limit (429) - forecast.solar erlaubt nur wenige Abrufe/Stunde.")
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    # watt_hours_day: { 'YYYY-MM-DD': Wh, ... }
    return data["result"]["watt_hours_day"]


def main():
    cfg = load_cfg()
    lat, lon = cfg["pv_latitude"], cfg["pv_longitude"]
    planes = cfg["pv_planes"]

    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    print(f"forecast.solar für {lat},{lon} - {len(planes)} Flächen ...")
    print("-" * 56)
    sum_today = sum_tom = 0.0
    for p in planes:
        try:
            whd = fetch_plane(lat, lon, p)
        except RuntimeError as e:
            sys.exit(f"FEHLER bei {p['name']}: {e}")
        t = whd.get(today, 0) / 1000.0
        m = whd.get(tomorrow, 0) / 1000.0
        sum_today += t
        sum_tom += m
        print(f"  {p['name']:16s} {p['kwp']:>5} kWp | heute {t:5.1f} | morgen {m:5.1f} kWh")
    print("-" * 56)
    print(f"  {'SUMME':16s} {'':>5}     | heute {sum_today:5.1f} | morgen {sum_tom:5.1f} kWh")
    print("-" * 56)
    print("PV-Prognose funktioniert - kein pvforecast-Adapter/ioBroker nötig.")
    print("(Zum Vergleich: V39.4 rechnet mit PV_KORREKTUR_FAKTOR 0.68 auf die Rohprognose.)")


if __name__ == "__main__":
    main()
