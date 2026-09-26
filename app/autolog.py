"""
Logbuch der Automatik: Ueberschuss-Automatik ("surplus") und Regeln ("rules") schreiben in je eine EIGENE Datei.

Jede Zeile: {"ts": ISO-Zeit, "dev": Geraetename, "action": "on"|"off"|"fail", "text": Klartext, "dry": Trockenlauf?, "rule": Regel-ID|None}
Die Dateien werden bei ca. MAX_BYTES gekuerzt (aelteste 40 % fallen weg), damit sie nie unbegrenzt wachsen.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta

_DIR = os.path.dirname(os.path.abspath(__file__))
MODULES = ("surplus", "rules")
PATHS = {m: os.path.join(_DIR, f"{m}_log.jsonl") for m in MODULES}
MAX_BYTES = 1_500_000
_lock = threading.Lock()


def log(module: str, text: str, dev: str = "", action: str = "", dry: bool = False, rule: str | None = None, now: datetime | None = None):
    if module not in PATHS:
        raise ValueError(module)
    row = {"ts": (now or datetime.now()).isoformat(timespec="seconds"), "dev": dev, "action": action, "text": text, "dry": bool(dry), "rule": rule}
    with _lock:
        path = PATHS[module]
        try:
            if os.path.exists(path) and os.path.getsize(path) > MAX_BYTES:
                with open(path, encoding="utf-8") as f:
                    lines = f.readlines()
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(lines[int(len(lines) * 0.4):])
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass                                   # das Logbuch darf die Steuerung nie stoeren


def recent(module: str, limit: int = 200, days: int | None = None, now: datetime | None = None) -> list[dict]:
    """Neueste zuerst. days: nur die letzten N Tage (None = alles)."""
    path = PATHS[module]
    if not os.path.exists(path):
        return []
    since = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds") if days else None
    out = []
    with _lock:
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if since and e.get("ts", "") < since:
            break
        out.append(e)
        if len(out) >= limit:
            break
    return out


def counts(module: str, days: int = 7, now: datetime | None = None) -> dict:
    """Kurzstatistik fuer den Betriebsbericht: Aktionen gesamt und davon echt (nicht Trockenlauf)."""
    rows = recent(module, 5000, days, now)
    return {"total": len(rows), "real": sum(1 for r in rows if not r.get("dry")), "failed": sum(1 for r in rows if r.get("action") == "fail")}
