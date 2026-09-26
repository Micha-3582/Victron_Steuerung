"""
Betriebsprotokoll - Grundlage fuer den "schlauen Zettel" (Betriebsbericht).

Zwei Dinge, beide dauerhaft gespeichert und klein gehalten:
* Ereignisse (`events.jsonl`, eine JSON-Zeile je Ereignis): Start, Fehler, ESS-Umschaltungen, Ausfaelle/Rueckfaelle von VRM und Tibber,
  Watchdog, Ueberschuss-Aktionen, Benachrichtigungen. Aelteste Zeilen werden bei Bedarf verworfen (max. ~3 MB).
* Tageszaehler (`ops_stats.json`): Durchlaeufe ok/Fehler, groesste Luecke zwischen zwei Durchlaeufen, welche Prognosequelle
  gesteuert hat, ob Tibber live oder aus dem Zwischenspeicher kam, Lade-Durchlaeufe, ESS-Schreibvorgaenge, Neustarts.

Nichts hier darf die Steuerung stoeren: alle Funktionen fangen ihre Fehler selbst ab.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta

log_ = logging.getLogger("opslog")

_DIR = os.path.dirname(os.path.abspath(__file__))
EVENTS_PATH = os.path.join(_DIR, "events.jsonl")
STATS_PATH = os.path.join(_DIR, "ops_stats.json")
MAX_EVENT_BYTES = 3_000_000
KEEP_STAT_DAYS = 400
FLUSH_EVERY_S = 60

_lock = threading.Lock()
_stats: dict | None = None
_dirty = False
_last_flush = 0.0
_last_tick_ts: float | None = None


# ---------------------------------------------------------------- Ereignisse
def log(kind: str, text: str, **data) -> None:
    """Haengt ein Ereignis an. kind: startup, tick_error, tick_ok, ess, tibber, vrm, notify, notify_fail, watchdog, surplus ..."""
    try:
        line = json.dumps({"ts": datetime.now().isoformat(timespec="seconds"), "kind": kind, "text": text, **data},
                          ensure_ascii=False, default=str)
        with _lock:
            with open(EVENTS_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            if os.path.getsize(EVENTS_PATH) > MAX_EVENT_BYTES:
                _trim_locked()
    except Exception as e:                                   # noqa: BLE001
        log_.warning("Ereignis nicht protokolliert: %s", e)


def _trim_locked() -> None:
    with open(EVENTS_PATH, encoding="utf-8") as f:
        lines = f.readlines()
    keep = lines[int(len(lines) * 0.4):]                     # aelteste 40 % verwerfen
    tmp = EVENTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(keep)
    os.replace(tmp, EVENTS_PATH)


def recent(limit: int = 100, kinds: set | None = None, since: datetime | None = None) -> list[dict]:
    """Neueste zuerst."""
    out: list[dict] = []
    try:
        with _lock:
            with open(EVENTS_PATH, encoding="utf-8") as f:
                lines = f.readlines()
    except OSError:
        return out
    since_iso = since.isoformat(timespec="seconds") if since else None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if since_iso and e.get("ts", "") < since_iso:
            break
        if kinds and e.get("kind") not in kinds:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- Tageszaehler
def _load() -> dict:
    global _stats
    if _stats is None:
        try:
            with open(STATS_PATH, encoding="utf-8") as f:
                d = json.load(f)
            _stats = d if isinstance(d, dict) and isinstance(d.get("days"), dict) else {"days": {}}
        except (OSError, ValueError):
            _stats = {"days": {}}
    return _stats


def _flush_locked(force: bool = False) -> None:
    global _dirty, _last_flush
    if not _dirty or (not force and time.time() - _last_flush < FLUSH_EVERY_S):
        return
    st = _load()
    for old in sorted(st["days"])[:-KEEP_STAT_DAYS]:
        del st["days"][old]
    tmp = STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)
    os.replace(tmp, STATS_PATH)
    _dirty, _last_flush = False, time.time()


def flush() -> None:
    try:
        with _lock:
            _flush_locked(force=True)
    except Exception as e:                                   # noqa: BLE001
        log_.warning("Zaehler nicht speicherbar: %s", e)


def count(key: str, n: int = 1, now: datetime | None = None) -> None:
    global _dirty
    try:
        day = (now or datetime.now()).date().isoformat()
        with _lock:
            d = _load()["days"].setdefault(day, {})
            d[key] = d.get(key, 0) + n
            _dirty = True
            _flush_locked()
    except Exception as e:                                   # noqa: BLE001
        log_.warning("Zaehler nicht erhoeht: %s", e)


def note_tick(ok: bool, now: datetime | None = None) -> None:
    """Ein Regel-Durchlauf ist fertig (ok oder mit Fehler). Merkt sich auch die groesste Luecke zwischen zwei Durchlaeufen."""
    global _dirty, _last_tick_ts
    try:
        now = now or datetime.now()
        with _lock:
            d = _load()["days"].setdefault(now.date().isoformat(), {})
            d["ticks_ok" if ok else "ticks_err"] = d.get("ticks_ok" if ok else "ticks_err", 0) + 1
            ts = now.timestamp()
            if _last_tick_ts is not None:
                gap = ts - _last_tick_ts
                if 0 < gap < 86400 * 3 and gap > d.get("max_gap_s", 0):
                    d["max_gap_s"] = round(gap)
            _last_tick_ts = ts
            _dirty = True
            _flush_locked()
    except Exception as e:                                   # noqa: BLE001
        log_.warning("Durchlauf nicht gezaehlt: %s", e)


def stats_days(limit: int = 60) -> dict:
    with _lock:
        d = _load()["days"]
        return {k: dict(d[k]) for k in sorted(d)[-limit:]}
