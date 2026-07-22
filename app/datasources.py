"""
Datenquellen: Tibber-Preise (API) und PV-Prognose (forecast.solar).
Ersetzt tibberlink + pvforecast-Adapter. Mit Caching für forecast.solar
(Rate-Limit ~12 Abrufe/Stunde/IP).
"""
import logging
import time
from datetime import date, timedelta

import requests

log = logging.getLogger("datasources")

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

    def __init__(self, lat, lon, planes, cache_seconds=3600):
        self.lat, self.lon, self.planes = lat, lon, planes
        self.cache_seconds = cache_seconds
        self._cache = None       # (ts, today, tomorrow)

    def get(self):
        """Gibt (today_kwh, tomorrow_kwh) zurück. Bei frischem Cache sofort;
        bei Abruf-Fehler (z.B. 429 Rate-Limit) wird der letzte bekannte Wert
        weiterverwendet, damit der Regler weiterläuft. Erst wenn nie ein Wert
        vorlag, wird der Fehler durchgereicht."""
        if self._cache and (time.time() - self._cache[0]) < self.cache_seconds:
            return self._cache[1], self._cache[2]
        today = date.today().isoformat()
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
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
            if self._cache:
                log.warning("PV-Abruf fehlgeschlagen (%s) - nutze letzten Wert", e)
                return self._cache[1], self._cache[2]
            raise
        self._cache = (time.time(), sum_t, sum_m)
        return sum_t, sum_m
