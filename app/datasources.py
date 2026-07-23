"""
Datenquellen: Tibber-Preise (API) und PV-Prognose (forecast.solar).
Ersetzt tibberlink + pvforecast-Adapter. Mit Caching für forecast.solar
(Rate-Limit ~12 Abrufe/Stunde/IP).
"""
import json
import logging
import os
import time
from datetime import date, timedelta

import requests

log = logging.getLogger("datasources")

_PV_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pv_cache.json")

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
FORECAST_BASE = "https://api.forecast.solar/estimate"


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


class PvForecast:
    """forecast.solar mit Caching. Gibt (today_kwh, tomorrow_kwh) roh zurück
    (ohne Korrekturfaktor - der steckt in logic.py)."""

    def __init__(self, lat, lon, planes, cache_seconds=10800):   # 3 h Cache
        self.lat, self.lon, self.planes = lat, lon, planes
        self.cache_seconds = cache_seconds
        self._cache = None        # (ts, today_kwh, tomorrow_kwh, day_iso)
        self._next_try = 0.0      # vor diesem Zeitpunkt KEIN neuer API-Versuch
        self._backoff = 0         # aktuelle Backoff-Dauer (s)
        self._load_disk()

    def _load_disk(self):
        try:
            with open(_PV_CACHE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self._cache = (d["ts"], d["today"], d["tomorrow"], d.get("day", ""))
        except Exception:                                     # noqa: BLE001
            pass

    def _save_disk(self):
        try:
            with open(_PV_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": self._cache[0], "today": self._cache[1],
                           "tomorrow": self._cache[2], "day": self._cache[3]}, f)
        except Exception:                                     # noqa: BLE001
            pass

    def _fresh(self, now, today):
        return (self._cache and self._cache[3] == today
                and (now - self._cache[0]) < self.cache_seconds)

    def get(self):
        """Gibt (today_kwh, tomorrow_kwh) zurück. Robust gegen das
        forecast.solar-Rate-Limit: 3 h Cache, Persistenz über Neustarts hinweg
        und Backoff (30 min → 1 h) nach Fehlern, damit NICHT bei jedem Regeltakt
        neu abgefragt wird (das hielt die 429-Störung selbst am Leben)."""
        now = time.time()
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        # 1) frischer Cache?
        if self._fresh(now, today):
            return self._cache[1], self._cache[2]
        # 2) noch in der Backoff-Sperre nach einem Fehler -> nicht abrufen
        if now < self._next_try:
            if self._cache:
                return self._cache[1], self._cache[2]
            raise RuntimeError("PV-Prognose im Backoff, noch kein Wert vorhanden")
        # 3) neuer Abruf (eine Anfrage je Fläche)
        sum_t = sum_m = 0.0
        try:
            for p in self.planes:
                url = f"{FORECAST_BASE}/{self.lat}/{self.lon}/{p['declination']}/{p['azimuth']}/{p['kwp']}"
                r = requests.get(url, timeout=20)
                if r.status_code == 429:
                    raise RuntimeError("forecast.solar Rate-Limit (429)")
                r.raise_for_status()
                whd = r.json()["result"]["watt_hours_day"]
                sum_t += whd.get(today, 0) / 1000.0
                sum_m += whd.get(tomorrow, 0) / 1000.0
        except Exception as e:                                # noqa: BLE001
            # Backoff hochzählen: 30 min, dann max 1 h - so bleiben wir weit unter
            # dem Limit, auch wenn der Regler jede Minute nach der Prognose fragt.
            self._backoff = min(3600, (self._backoff * 2) or 1800)
            self._next_try = now + self._backoff
            if self._cache:
                log.warning("PV-Abruf fehlgeschlagen (%s) - letzter Wert, nächster "
                            "Versuch in %d min", e, self._backoff // 60)
                return self._cache[1], self._cache[2]
            raise
        # Erfolg: Backoff zurücksetzen, Cache (RAM + Platte) aktualisieren
        self._backoff = 0
        self._next_try = 0.0
        self._cache = (now, sum_t, sum_m, today)
        self._save_disk()
        return sum_t, sum_m
