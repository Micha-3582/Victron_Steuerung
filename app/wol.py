"""
Wake-on-LAN: Rechner (PC, NAS, Server ...) per "Magic Packet" aus dem Standby/Ruhezustand wecken.

Ein Ziel hat Name, MAC-Adresse und optional eine IP-Adresse (fuer die Anzeige "erreichbar" per Ping) sowie eine Broadcast-Adresse
(Standard: 255.255.255.255, mit IP zusaetzlich die /24-Broadcast des Netzes). Das Paket geht per UDP (Standard-Port 9).
Voraussetzung am Rechner: Wake-on-LAN im BIOS/Treiber aktiviert; der Server muss im selben Netz (Broadcast) stehen.
Nutzbar am Dashboard (Knopf, optional mit PIN) und in Regeln der Art "Ablauf" als Schritt "Rechner aufwecken".
Eigenes Register (wol.json). Reine Logik + ein UDP-Socket.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import subprocess
import threading
import time
import uuid
import os

import pins

_DIR = os.path.dirname(os.path.abspath(__file__))
WOL_PATH = os.path.join(_DIR, "wol.json")
MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:\-.]?){5}[0-9A-Fa-f]{2}$")
DEFAULT_ICON = "💻"
DEFAULT_PORT = 9
PING_TTL_S = 10.0

_lock = threading.RLock()
_ping_cache: dict[str, tuple[float, bool | None]] = {}


class WolError(ValueError):
    pass


def _store():
    import store
    return store


def load() -> list[dict]:
    with _lock:
        d = _store()._load_json_recovering(WOL_PATH, lambda: [])
        return [x for x in d if isinstance(x, dict) and x.get("id")] if isinstance(d, list) else []


def _save(items: list[dict]):
    _store()._dump_json(WOL_PATH, items, indent=2)


def normalize_mac(mac: str) -> str:
    raw = str(mac or "").strip()
    if not MAC_RE.match(raw):
        raise WolError("MAC-Adresse ungültig – Beispiel: AA:BB:CC:DD:EE:FF")
    hexs = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    return ":".join(hexs[i:i + 2] for i in range(0, 12, 2))


def _ip(value: str, what: str, allow_broadcast: bool = False) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    try:
        a = ipaddress.ip_address(v)
    except ValueError:
        raise WolError(f"{what}: ungültige IP-Adresse")
    if a.version != 4:
        raise WolError(f"{what}: nur IPv4")
    if not (a.is_private or (allow_broadcast and str(a) == "255.255.255.255")) or a.is_loopback:
        raise WolError(f"{what}: nur Adressen aus dem lokalen Netz")
    return str(a)


def _port(value) -> int:
    try:
        p = int(value if value not in (None, "") else DEFAULT_PORT)
    except (TypeError, ValueError):
        raise WolError("Port: Zahl erwartet")
    if not 1 <= p <= 65535:
        raise WolError("Port: zwischen 1 und 65535")
    return p


def add(name: str, mac: str, ip: str = "", broadcast: str = "", port=None, icon: str | None = None) -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise WolError("Name eingeben")
    item = {"id": "wol-" + uuid.uuid4().hex[:8], "name": name, "mac": normalize_mac(mac), "ip": _ip(ip, "IP-Adresse"),
            "broadcast": _ip(broadcast, "Broadcast-Adresse", True), "port": _port(port), "icon": (icon or DEFAULT_ICON).strip()[:12] or DEFAULT_ICON, "show": True}
    with _lock:
        items = load()
        items.append(item)
        _save(items)
    return item


def update(wid: str, **f) -> bool:
    with _lock:
        items = load()
        for it in items:
            if it["id"] != wid:
                continue
            if isinstance(f.get("name"), str) and f["name"].strip():
                it["name"] = f["name"].strip()[:60]
            if f.get("mac") is not None:
                it["mac"] = normalize_mac(f["mac"])
            if f.get("ip") is not None:
                it["ip"] = _ip(f["ip"], "IP-Adresse")
            if f.get("broadcast") is not None:
                it["broadcast"] = _ip(f["broadcast"], "Broadcast-Adresse", True)
            if f.get("port") not in (None, ""):
                it["port"] = _port(f["port"])
            if isinstance(f.get("icon"), str) and 0 < len(f["icon"].strip()) <= 12:
                it["icon"] = f["icon"].strip()
            if isinstance(f.get("show"), bool):
                it["show"] = f["show"]
            _save(items)
            return True
    return False


def remove(wid: str) -> bool:
    with _lock:
        items = load()
        keep = [x for x in items if x["id"] != wid]
        if len(keep) == len(items):
            return False
        _save(keep)
        return True


def reorder(ids: list[str]):
    with _lock:
        items = load()
        pos = {i: n for n, i in enumerate(ids)}
        items.sort(key=lambda x: pos.get(x["id"], len(ids)))
        _save(items)


# ---- PIN (gemeinsam mit den Geraeten, siehe pins.py)
def set_pin(wid: str, pin: str | None) -> bool:
    if pin:
        try:
            pins.validate(pin)
        except pins.PinError as e:
            raise WolError(str(e))
    with _lock:
        items = load()
        for it in items:
            if it["id"] == wid:
                pins.apply(it, pin)
                _save(items)
                return True
    return False


def has_pin(wid: str) -> bool:
    return any(x["id"] == wid and x.get("pin_hash") for x in load())


def check_pin(wid: str, pin) -> bool:
    it = next((x for x in load() if x["id"] == wid), None)
    return True if not it else pins.check(it, pin)


# ---- Magic Packet
def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(normalize_mac(mac).replace(":", ""))
    return b"\xff" * 6 + raw * 16


def targets(item: dict) -> list[str]:
    out = [item.get("broadcast") or "255.255.255.255"]
    if not item.get("broadcast") and item.get("ip"):
        a = item["ip"].split(".")
        out.append(".".join(a[:3] + ["255"]))                 # /24-Broadcast des Netzes (Standardfall im Heimnetz)
    return list(dict.fromkeys(out))


def wake(wid: str) -> list[str]:
    """Magic Packet senden. Rueckgabe: die Ziel-Adressen, an die gesendet wurde. WolError bei unbekanntem Ziel/Netzfehler."""
    it = next((x for x in load() if x["id"] == wid), None)
    if not it:
        raise WolError("Ziel existiert nicht (mehr)")
    pkt = magic_packet(it["mac"])
    sent = []
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(2.0)
        for t in targets(it):
            try:
                s.sendto(pkt, (t, int(it.get("port") or DEFAULT_PORT)))
                sent.append(t)
            except OSError as e:
                last = e
        if not sent:
            raise WolError(f"Senden fehlgeschlagen: {last}")
    finally:
        s.close()
    return sent


# ---- Erreichbarkeit (optional, per Ping)
def is_up(ip: str) -> bool | None:
    """True/False per Ping (1 s), None wenn nicht pruefbar (keine IP, kein ping-Programm). Ergebnis 10 s zwischengespeichert."""
    if not ip:
        return None
    now = time.time()
    hit = _ping_cache.get(ip)
    if hit and now - hit[0] < PING_TTL_S:
        return hit[1]
    try:
        r = subprocess.run(["ping", "-c", "1", "-W", "1", ip] if os.name != "nt" else ["ping", "-n", "1", "-w", "1000", ip],
                           capture_output=True, timeout=3)
        up = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        up = None
    _ping_cache[ip] = (now, up)
    return up


def public(item: dict, with_status: bool = True) -> dict:
    out = pins.strip(item)
    if with_status:
        out["up"] = is_up(item.get("ip", ""))
    return out
