#!/usr/bin/env python3
"""
Tibber-API Preisabruf-Test
==========================
Holt die Strompreise direkt von der Tibber-API (ersetzt tibberlink).
Liest den Token aus config.json (oder --token).

Nutzung (im selben Ordner wie config.json):
  pip install requests
  python tibber_test.py

Ausgabe: Anzahl Preis-Slots heute/morgen, aktueller Preis, Min/Max.
Preise werden in ct/kWh angezeigt (wie in V39.4).
"""
import argparse
import json
import os
import sys
from datetime import datetime

try:
    import requests
except ImportError:
    sys.exit("Bitte zuerst installieren:  pip install requests")

TIBBER_URL = "https://api.tibber.com/v1-beta/gql"
QUERY = """
{ viewer { homes {
  currentSubscription { priceInfo {
    current { total startsAt }
    today { total startsAt }
    tomorrow { total startsAt }
  } }
} } }
"""


def load_token(args):
    if args.token:
        return args.token
    cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg):
        with open(cfg, encoding="utf-8") as f:
            return json.load(f).get("tibber_token")
    return None


def summarize(label, slots):
    if not slots:
        print(f"  {label:8s}: (leer)")
        return
    prices_ct = [s["total"] * 100 for s in slots]
    print(f"  {label:8s}: {len(slots)} Slots | "
          f"min {min(prices_ct):.1f} | max {max(prices_ct):.1f} ct/kWh")


def main():
    ap = argparse.ArgumentParser(description="Tibber-API Preisabruf-Test")
    ap.add_argument("--token", help="Tibber Access Token (sonst aus config.json)")
    args = ap.parse_args()

    token = load_token(args)
    if not token:
        sys.exit("Kein Token. Trage tibber_token in config.json ein oder nutze --token.")

    print("Frage Tibber-API ab ...")
    try:
        r = requests.post(
            TIBBER_URL,
            json={"query": QUERY},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except requests.RequestException as e:
        sys.exit(f"Verbindungsfehler: {e}")

    if r.status_code == 401:
        sys.exit("FEHLER 401: Token ungültig oder abgelaufen.")
    if r.status_code != 200:
        sys.exit(f"FEHLER HTTP {r.status_code}: {r.text[:300]}")

    data = r.json()
    if "errors" in data:
        sys.exit(f"API-Fehler: {data['errors']}")

    homes = data["data"]["viewer"]["homes"]
    if not homes:
        sys.exit("Keine Homes im Tibber-Konto gefunden.")

    print("-" * 52)
    for i, home in enumerate(homes):
        pi = (home.get("currentSubscription") or {}).get("priceInfo") or {}
        print(f"Home {i + 1}:")
        cur = pi.get("current")
        if cur:
            print(f"  Jetzt   : {cur['total'] * 100:.1f} ct/kWh  ({cur['startsAt'][:16]})")
        summarize("Heute", pi.get("today"))
        summarize("Morgen", pi.get("tomorrow"))
    print("-" * 52)
    print("Tibber-Abruf funktioniert - kein tibberlink/ioBroker nötig.")
    if not homes[0].get("currentSubscription", {}).get("priceInfo", {}).get("tomorrow"):
        print("Hinweis: Morgen-Preise noch leer (werden i.d.R. ab ~13 Uhr veröffentlicht).")

    # Auflösungs-Check über die App-Funktion (Viertelstunde vs. Stunde)
    try:
        from datetime import datetime as _dt
        from datasources import fetch_tibber_prices
        entries = fetch_tibber_prices(token)
        if len(entries) >= 2:
            t0 = _dt.fromisoformat(entries[0]["startsAt"])
            t1 = _dt.fromisoformat(entries[1]["startsAt"])
            mins = int((t1 - t0).total_seconds() / 60)
            print("-" * 52)
            print(f"App-Abruf: {len(entries)} Slots, Raster {mins} Minuten "
                  f"({'Viertelstunde ✓' if mins == 15 else 'Stunde (Fallback)'})")
    except Exception as e:            # noqa: BLE001
        print(f"App-Abruf-Check übersprungen: {e}")


if __name__ == "__main__":
    main()
