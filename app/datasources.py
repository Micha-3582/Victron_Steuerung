"""
Datenquelle: Tibber-Preise (API). Die PV-Prognose kommt aus dem Victron-VRM-Portal (vrm.py).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta

import requests

log = logging.getLogger("datasources")

# Der Server-LXC hat eine kaputte IPv6-Route: manche APIs (u.a. api.open-meteo.com)
# lösen per IPv6 auf, die Verbindung hängt dann bis zum Timeout (curl -4 antwortet
# dagegen in ms). Wir zwingen die gesamte Namensauflösung des Prozesses auf IPv4,
# indem getaddrinfo nur noch AF_INET-Adressen zurückgibt. Das wirkt garantiert für
# alle HTTP-Aufrufe (requests/urllib3, egal welche Variante) und behebt nebenbei die
# gelegentlichen forecast.solar-/Tibber-Timeouts.
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _getaddrinfo_ipv4_only(host, port, family=0, *args, **kwargs):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, *args, **kwargs)


_socket.getaddrinfo = _getaddrinfo_ipv4_only

TIBBER_URL = "https://api.tibber.com/v1-beta/gql"

# Viertelstunden-Auflösung (Tibber seit Okt 2025): resolution ist ein Argument
# an priceInfo selbst - today/tomorrow liefern dann je 96 statt 24 Slots.
# (Verifiziert per tibberlink-Community, Diskussion #768.)
TIBBER_QUERY_QUARTER = """
{ viewer { homes { currentSubscription { priceInfo(resolution: QUARTER_HOURLY) {
  today { total startsAt level } tomorrow { total startsAt level }
} } } } }
"""
# Rückfall: klassische Stundenwerte ohne resolution-Argument.
TIBBER_QUERY_HOURLY = """
{ viewer { homes { currentSubscription { priceInfo {
  today { total startsAt level } tomorrow { total startsAt level }
} } } } }
"""


def _post(token, query, timeout):
    r = requests.post(TIBBER_URL, json={"query": query},
                      headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Tibber API: {data['errors']}")
    homes = data["data"]["viewer"]["homes"]
    if not homes:
        raise RuntimeError("Keine Tibber-Homes gefunden")
    return (homes[0].get("currentSubscription") or {}).get("priceInfo") or {}


def fetch_tibber_prices(token, timeout=15):
    """Liefert [{'startsAt','total'(EUR/kWh),'level'}, ...].
    Versucht Viertelstunden (falls Tibber sie irgendwann im PriceResolution-Enum
    freischaltet) und fällt sonst sauber auf Stundenwerte zurück.
    Stand 2026-07: die öffentliche API kennt nur HOURLY/DAILY -> Stundenwerte."""
    try:
        pi = _post(token, TIBBER_QUERY_QUARTER, timeout)
        combined = list(pi.get("today") or []) + list(pi.get("tomorrow") or [])
        if combined:
            return combined
    except Exception:                        # noqa: BLE001  (400 falls Enum fehlt)
        pass
    pi = _post(token, TIBBER_QUERY_HOURLY, timeout)
    return list(pi.get("today") or []) + list(pi.get("tomorrow") or [])


def build_fixed_price_entries(price_ct_per_kwh, now=None):
    """Für Anlagen mit normalem (nicht-dynamischem) Stromvertrag: liefert das gleiche
    Format wie fetch_tibber_prices() (['startsAt','total'(EUR/kWh),'level']), aber mit
    konstantem Preis über alle Viertelstunden von heute 00:00 bis morgen 23:45. So kann
    logic.decide() unverändert weiterlaufen (Peak-Schutz/Nacht-Puffer/Morgen-Brücke
    arbeiten weiter zeitfensterbasiert) - es findet nur keine Preis-Optimierung mehr statt,
    weil jeder Slot denselben Preis hat."""
    from datetime import datetime as _dt
    now = now or _dt.now()
    price_eur = float(price_ct_per_kwh) / 100.0
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    entries = []
    for i in range(4 * 24 * 2):   # heute + morgen, 15-Min-Raster
        ts = start + timedelta(minutes=15 * i)
        entries.append({"startsAt": ts.isoformat(), "total": price_eur, "level": "NORMAL"})
    return entries
