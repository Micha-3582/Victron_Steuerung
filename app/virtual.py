"""
Eigene Schalter und Knoepfe (reine Software, kein Geraet dahinter).

  button   Knopf: wird gedrueckt (Dashboard oder Regel-Aktion). Fuer Regeln ist "wird gedrueckt" ein kurzer Impuls.
  switch   Schalter mit Zustand an/aus (bleibt, bis jemand oder eine Regel ihn umschaltet).

Sie haben ihr eigenes Register (virtual.json) und erscheinen auf dem Dashboard in einer eigenen Kachel - getrennt von den
Hardware-Geraeten. Regeln der Art "Ablauf" (flows.py) koennen sie als Ausloeser (WENN) und als Aktion (DANN) benutzen.
Reine Logik ohne Netzwerk.
"""
from __future__ import annotations

import os
import threading
import uuid

_DIR = os.path.dirname(os.path.abspath(__file__))
VIRTUAL_PATH = os.path.join(_DIR, "virtual.json")
KINDS = ("button", "switch")
DEFAULT_ICON = {"button": "🔘", "switch": "🎚️"}

_lock = threading.RLock()
_pressed: dict[str, float] = {}                  # knopf-id -> Zeitpunkt des Druecks (Impuls, bis die Regelschleife ihn gesehen hat)


class VirtualError(ValueError):
    pass


def _store():
    import store
    return store


def load() -> list[dict]:
    with _lock:
        d = _store()._load_json_recovering(VIRTUAL_PATH, lambda: [])
        return [x for x in d if isinstance(x, dict) and x.get("id")] if isinstance(d, list) else []


def _save(items: list[dict]):
    _store()._dump_json(VIRTUAL_PATH, items, indent=2)


def add(name: str, kind: str, icon: str | None = None) -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise VirtualError("Name eingeben")
    if kind not in KINDS:
        raise VirtualError("Art: Knopf oder Schalter")
    with _lock:
        items = load()
        item = {"id": "v-" + uuid.uuid4().hex[:8], "name": name, "kind": kind, "icon": (icon or DEFAULT_ICON[kind]).strip()[:12] or DEFAULT_ICON[kind],
                "show": True, "on": False}
        items.append(item)
        _save(items)
    return item


def update(vid: str, name: str | None = None, icon: str | None = None, show: bool | None = None) -> bool:
    with _lock:
        items = load()
        for it in items:
            if it["id"] == vid:
                if name is not None and name.strip():
                    it["name"] = name.strip()[:60]
                if icon is not None and 0 < len(icon.strip()) <= 12:
                    it["icon"] = icon.strip()
                if show is not None:
                    it["show"] = bool(show)
                _save(items)
                return True
    return False


def remove(vid: str) -> bool:
    with _lock:
        items = load()
        keep = [x for x in items if x["id"] != vid]
        if len(keep) == len(items):
            return False
        _save(keep)
        _pressed.pop(vid, None)
        return True


def reorder(ids: list[str]):
    with _lock:
        items = load()
        pos = {i: n for n, i in enumerate(ids)}
        items.sort(key=lambda x: pos.get(x["id"], len(ids)))
        _save(items)


def press(vid: str) -> bool:
    """Knopf druecken (Impuls fuer Regeln)."""
    import time
    with _lock:
        it = next((x for x in load() if x["id"] == vid), None)
        if not it or it["kind"] != "button":
            return False
        _pressed[vid] = time.time()
        return True


def toggle(vid: str) -> bool:
    """Schalter umschalten."""
    with _lock:
        it = next((x for x in load() if x["id"] == vid), None)
        return bool(it) and it["kind"] == "switch" and set_state(vid, not it.get("on"))


def set_state(vid: str, on: bool) -> bool:
    with _lock:
        items = load()
        for it in items:
            if it["id"] == vid and it["kind"] == "switch":
                if bool(it.get("on")) != bool(on):
                    it["on"] = bool(on)
                    _save(items)
                return True
    return False


def snapshot() -> dict[str, dict]:
    """Fuer die Regel-Bedingungen: {id: {name, kind, on, pressed}}."""
    with _lock:
        return {x["id"]: {"id": x["id"], "name": x["name"], "kind": x["kind"], "on": bool(x.get("on")), "pressed": x["id"] in _pressed}
                for x in load()}


def consume(ids) -> None:
    """Die Regelschleife hat diese Druecke gesehen - Impuls beenden."""
    with _lock:
        for i in ids:
            _pressed.pop(i, None)


def pressed_ids() -> list[str]:
    with _lock:
        return list(_pressed)
