"""
Zwischenspeicher fuer die Tibber-Preise (price_cache.json).

Die Preise des ganzen Tages (und ab dem Nachmittag auch des naechsten) sind bekannt. Faellt der Tibber-Abruf
aus, plant die Steuerung mit den zuletzt geholten Preisen weiter, solange sie die aktuelle Zeit noch abdecken.
Sonst greift die sichere Rueckfallstufe in webapp.Controller (laufendes Netzladen wird gestoppt).
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import store
from logic import _parse_iso

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_cache.json")
MAX_SLOT = timedelta(hours=1, minutes=5)       # ein Eintrag gilt hoechstens eine Stunde (Viertelstunden-Slots kuerzer)


def save(prices: list, now: datetime | None = None, path: str | None = None):
    now = now or datetime.now()
    store._dump_json(path or PATH, {"fetched": now.isoformat(timespec="seconds"), "prices": prices}, indent=None)


def load(path: str | None = None) -> dict | None:
    """{'fetched': iso, 'prices': [...]} oder None (keine/kaputte Datei)."""
    try:
        with open(path or PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("prices"), list) and d["prices"]:
            return d
    except (OSError, ValueError):
        pass
    return None


def covers(prices: list, now: datetime) -> bool:
    """True, wenn es einen Preis gibt, der JETZT gilt (juengster Start <= jetzt, nicht aelter als ein Slot)."""
    latest = None
    for p in prices:
        try:
            start = _parse_iso(p["startsAt"])
        except (KeyError, ValueError, TypeError):
            continue
        if start <= now and (latest is None or start > latest):
            latest = start
    return latest is not None and now - latest < MAX_SLOT
