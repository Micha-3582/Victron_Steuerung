#!/usr/bin/env python3
"""
Tibber-Auflösungs-Probe
=======================
Findet heraus, mit welcher Syntax dein Tibber-Konto Viertelstunden-Preise
liefert. Probiert mehrere Enum-Namen und Pagination-Varianten und meldet,
welche funktioniert (HTTP 200 + Slots) und welches Zeitraster herauskommt.

Nutzung (im app-Ordner, config.json mit tibber_token vorhanden):
  python tibber_probe.py
"""
import json
import os
import sys
from datetime import datetime

import requests

URL = "https://api.tibber.com/v1-beta/gql"


def token():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)["tibber_token"]


def run(name, query):
    try:
        r = requests.post(URL, json={"query": query},
                          headers={"Authorization": f"Bearer {token()}"}, timeout=15)
    except Exception as e:                       # noqa: BLE001
        print(f"  {name:34s} -> Netzwerkfehler: {e}")
        return
    if r.status_code != 200:
        # Fehlermeldung von Tibber mit ausgeben, falls JSON
        msg = ""
        try:
            j = r.json()
            msg = j.get("errors", j)
        except Exception:                        # noqa: BLE001
            msg = r.text[:120]
        print(f"  {name:34s} -> HTTP {r.status_code}: {str(msg)[:110]}")
        return
    data = r.json()
    if "errors" in data:
        print(f"  {name:34s} -> GraphQL-Fehler: {str(data['errors'])[:110]}")
        return
    pi = (data["data"]["viewer"]["homes"][0].get("currentSubscription") or {}).get("priceInfo") or {}
    nodes = ((pi.get("range") or {}).get("nodes")) or []
    if not nodes:
        print(f"  {name:34s} -> OK, aber 0 Slots")
        return
    raster = "?"
    if len(nodes) >= 2:
        t0 = datetime.fromisoformat(nodes[0]["startsAt"])
        t1 = datetime.fromisoformat(nodes[1]["startsAt"])
        raster = f"{int((t1 - t0).total_seconds() / 60)} min"
    print(f"  {name:34s} -> OK · {len(nodes)} Slots · Raster {raster}  <== funktioniert")


def q(resolution, page):
    return f"""{{ viewer {{ homes {{ currentSubscription {{ priceInfo {{
      range(resolution: {resolution}, {page}) {{ nodes {{ total startsAt level }} }}
    }} }} }} }} }}"""


def main():
    print("Probiere Tibber-Range-Varianten ...")
    print("-" * 70)
    for res in ("QUARTER_HOURLY", "QUARTERLY", "QUARTER_HOUR", "FIFTEEN_MINUTES", "HOURLY"):
        for page in ("first: 192", "last: 192"):
            run(f"{res} / {page}", q(res, page))
    print("-" * 70)
    print("Schick mir die Zeile mit '<== funktioniert' (oder alle, falls keine).")


if __name__ == "__main__":
    main()
