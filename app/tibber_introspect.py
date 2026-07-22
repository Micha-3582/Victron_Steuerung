#!/usr/bin/env python3
"""
Tibber-Schema-Introspektion
===========================
Liest die tatsächlich erlaubten Werte des PriceResolution-Enums und die
Argumente/Felder rund um priceInfo aus - so sehen wir schwarz auf weiß,
ob und wie Viertelstunden abrufbar sind.

  python tibber_introspect.py
"""
import json
import os

import requests

URL = "https://api.tibber.com/v1-beta/gql"


def token():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)["tibber_token"]


def post(query):
    r = requests.post(URL, json={"query": query},
                      headers={"Authorization": f"Bearer {token()}"}, timeout=15)
    return r.status_code, r.json()


def main():
    # 1) Enum-Werte von PriceResolution
    _, d = post('{ __type(name: "PriceResolution") { enumValues { name } } }')
    vals = [v["name"] for v in (((d.get("data") or {}).get("__type") or {}) or {}).get("enumValues", [])]
    print("PriceResolution erlaubt:", vals or "(nichts / Fehler)")

    # 2) Felder des PriceInfo-Typs (heute/morgen/range/...)
    _, d = post('{ __type(name: "PriceInfo") { fields { name args { name type { name kind ofType { name } } } } } }')
    fields = (((d.get("data") or {}).get("__type") or {}) or {}).get("fields", [])
    print("\nPriceInfo-Felder:")
    for f in fields:
        args = ", ".join(a["name"] for a in f.get("args", []))
        print(f"  {f['name']}({args})")

    # 3) Anzahl Slots in today/tomorrow (Realcheck)
    _, d = post("""{ viewer { homes { currentSubscription { priceInfo {
      today { startsAt } tomorrow { startsAt } } } } } }""")
    try:
        pi = d["data"]["viewer"]["homes"][0]["currentSubscription"]["priceInfo"]
        print(f"\ntoday: {len(pi.get('today') or [])} Slots, "
              f"tomorrow: {len(pi.get('tomorrow') or [])} Slots")
    except Exception as e:                       # noqa: BLE001
        print("Realcheck-Fehler:", e)


if __name__ == "__main__":
    main()
