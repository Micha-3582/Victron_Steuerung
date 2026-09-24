"""
Shelly-Anbindung: Geraete im LAN finden, anlegen, Status lesen, schalten.
Nur HTTP im lokalen Netz (kein Cloud-Zugriff, keine Zusatz-Pakete).
- Gen1 (z.B. Shelly 1, 1PM, Plug S, 2.5):  /relay/<n>?turn=on|off, /status
- Gen2/3 (Plus/Pro/Gen3):                  /rpc/Switch.Set, /rpc/Shelly.GetStatus
Jeder Schaltkanal ist ein eigener Eintrag (id = "<mac>-<kanal>").
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("shelly")

DEVICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shelly_devices.json")
_lock = threading.Lock()

# Auswahl fuer die Geraete-Symbole (Einstellungen); Standard = Steckdose
ICONS = ["🔌", "💡", "🔥", "♨️", "🌡️", "❄️", "🌀", "💧", "🚗", "🧺", "🫧", "🍳",
         "🏊", "🖥️", "📺", "🎧", "🔔", "🌿", "🛋️", "🔒"]
DEFAULT_ICON = ICONS[0]

PROBE_TIMEOUT = 0.8      # Subnetz-Scan (LAN-Antwort < 100 ms)
CALL_TIMEOUT = 3.0       # Status/Schalten


class ShellyError(Exception):
    pass


# ---------------------------------------------------------------- Speicher
def load_devices() -> list[dict]:
    if not os.path.exists(DEVICES_PATH):
        return []
    try:
        with open(DEVICES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save(items: list[dict]):
    with _lock, open(DEVICES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def _find(dev_id: str) -> dict | None:
    return next((d for d in load_devices() if d["id"] == dev_id), None)


# ---------------------------------------------------------------- Netz
def check_ip(ip: str) -> str:
    """Nur private IPv4-Adressen zulassen (kein Missbrauch als Web-Proxy)."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        raise ShellyError("Ungültige IP-Adresse")
    if addr.version != 4 or not (addr.is_private and not addr.is_loopback):
        raise ShellyError("Nur IP-Adressen aus dem lokalen Netz erlaubt")
    return str(addr)


def local_subnet() -> ipaddress.IPv4Network:
    """/24 des Netzwerks, in dem dieser Rechner haengt."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))    # sendet nichts, ermittelt nur die Quell-IP
        ip = s.getsockname()[0]
    except OSError:
        ip = "192.168.2.1"
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def _get(ip: str, path: str, timeout: float, auth=None):
    r = requests.get(f"http://{ip}{path}", timeout=timeout, auth=auth)
    r.raise_for_status()
    return r.json()


def probe(ip: str, timeout: float = PROBE_TIMEOUT) -> dict | None:
    """Fragt /shelly ab (ohne Login moeglich). None = kein Shelly."""
    try:
        info = _get(ip, "/shelly", timeout)
    except (requests.RequestException, ValueError):
        return None
    if not isinstance(info, dict) or not (info.get("mac") or info.get("id")):
        return None
    gen = int(info.get("gen") or 1)
    mac = str(info.get("mac") or info.get("id")).replace(":", "").upper()
    if gen >= 2:
        model = info.get("app") or info.get("model") or "Shelly"
        name = info.get("name") or info.get("id") or model
        auth = bool(info.get("auth_en"))
        channels = None       # per Status ermitteln
    else:
        model = info.get("type") or "Shelly"
        name = f"{model}-{mac[-6:]}"
        auth = bool(info.get("auth"))
        channels = int(info.get("num_outputs") or 0)
    return {"ip": ip, "gen": gen, "mac": mac, "model": model, "name": name,
            "auth": auth, "channels": channels}


def _channels_gen2(ip: str) -> list[int]:
    st = _get(ip, "/rpc/Shelly.GetStatus", CALL_TIMEOUT)
    return sorted(int(k.split(":")[1]) for k in st if k.startswith("switch:"))


def discover(subnet: ipaddress.IPv4Network | None = None) -> list[dict]:
    """Scannt das Subnetz parallel. Liefert gefundene Shellys (auch bereits angelegte)."""
    subnet = subnet or local_subnet()
    hosts = [str(h) for h in subnet.hosts()]
    with ThreadPoolExecutor(max_workers=64) as ex:
        found = [r for r in ex.map(probe, hosts) if r]
    known = {d["mac"] for d in load_devices()}
    for f in found:
        f["known"] = f["mac"] in known
    found.sort(key=lambda f: tuple(int(x) for x in f["ip"].split(".")))
    return found


# ---------------------------------------------------------------- Anlegen
def add_by_ip(ip: str, password: str = "") -> list[dict]:
    """Legt alle Schaltkanaele des Geraets an (schon vorhandene bleiben unveraendert)."""
    ip = check_ip(ip)
    info = probe(ip, CALL_TIMEOUT)
    if not info:
        raise ShellyError("Unter dieser Adresse antwortet kein Shelly")
    if info["auth"] and info["gen"] >= 2:
        raise ShellyError("Passwortgeschützte Gen2/3-Geräte werden noch nicht unterstützt "
                          "(Login in der Shelly-Weboberfläche vorübergehend abschalten)")
    if info["auth"] and not password:
        raise ShellyError("Gerät ist passwortgeschützt – Passwort angeben")
    if info["gen"] >= 2:
        try:
            channels = _channels_gen2(ip)
        except (requests.RequestException, ValueError) as e:
            raise ShellyError(f"Status nicht lesbar: {e}")
    else:
        channels = list(range(info["channels"]))
    if not channels:
        raise ShellyError(f"{info['model']} hat keinen Schaltausgang (Relais)")
    items = load_devices()
    have = {d["id"] for d in items}
    added = []
    for ch in channels:
        dev_id = f"{info['mac']}-{ch}"
        if dev_id in have:
            continue
        name = info["name"] if len(channels) == 1 else f"{info['name']} K{ch + 1}"
        entry = {"id": dev_id, "mac": info["mac"], "ip": ip, "gen": info["gen"],
                 "channel": ch, "model": info["model"], "name": name,
                 "icon": DEFAULT_ICON, "show": False,
                 "user": "admin" if password else "", "password": password}
        items.append(entry)
        added.append(entry)
    _save(items)
    return added


def update(dev_id: str, name: str | None = None, icon: str | None = None,
           show: bool | None = None) -> bool:
    """Name, Symbol und Dashboard-Sichtbarkeit eines Geraets aendern."""
    if icon is not None and icon not in ICONS:
        raise ShellyError("Unbekanntes Symbol")
    items = load_devices()
    for d in items:
        if d["id"] == dev_id:
            if name is not None:
                d["name"] = name.strip()[:60] or d["name"]
            if icon is not None:
                d["icon"] = icon
            if show is not None:
                d["show"] = bool(show)
            _save(items)
            return True
    return False


def reorder(ids: list[str]) -> None:
    """Neue Reihenfolge (Liste der IDs); nicht genannte Geraete rutschen ans Ende."""
    items = load_devices()
    pos = {i: n for n, i in enumerate(ids)}
    items.sort(key=lambda d: pos.get(d["id"], len(pos)))    # stabil
    _save(items)


def remove(dev_id: str) -> bool:
    items = load_devices()
    keep = [d for d in items if d["id"] != dev_id]
    if len(keep) == len(items):
        return False
    _save(keep)
    return True


# ---------------------------------------------------------------- Status/Schalten
def _auth(d: dict):
    return (d.get("user"), d.get("password")) if d.get("password") else None


def status(d: dict) -> dict:
    """{'online': bool, 'on': bool|None, 'power': W|None}"""
    try:
        if d["gen"] >= 2:
            st = _get(d["ip"], f"/rpc/Switch.GetStatus?id={d['channel']}", CALL_TIMEOUT)
            power = st.get("apower")
            return {"online": True, "on": bool(st.get("output")),
                    "power": None if power is None else round(float(power), 1)}
        st = _get(d["ip"], "/status", CALL_TIMEOUT, _auth(d))
        relay = st["relays"][d["channel"]]
        meters = st.get("meters") or []
        power = meters[d["channel"]].get("power") if d["channel"] < len(meters) else None
        return {"online": True, "on": bool(relay.get("ison")),
                "power": None if power is None else round(float(power), 1)}
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return {"online": False, "on": None, "power": None}


def set_state(dev_id: str, on: bool) -> dict:
    d = _find(dev_id)
    if not d:
        raise ShellyError("Gerät nicht gefunden")
    try:
        if d["gen"] >= 2:
            _get(d["ip"], f"/rpc/Switch.Set?id={d['channel']}&on={'true' if on else 'false'}",
                 CALL_TIMEOUT)
        else:
            _get(d["ip"], f"/relay/{d['channel']}?turn={'on' if on else 'off'}",
                 CALL_TIMEOUT, _auth(d))
    except (requests.RequestException, ValueError) as e:
        raise ShellyError(f"Schalten fehlgeschlagen: {e}")
    return status(d)


def list_with_status(only_shown: bool = False) -> list[dict]:
    """Angelegte Geraete inkl. Live-Status (parallel abgefragt). Ohne Passwort.
    only_shown: nur die fuers Dashboard freigegebenen (Reihenfolge wie gespeichert)."""
    items = [d for d in load_devices() if d.get("show")] if only_shown else load_devices()
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(items))) as ex:
        states = list(ex.map(status, items))
    out = []
    for d, st in zip(items, states):
        pub = {k: v for k, v in d.items() if k not in ("password", "user")}
        pub.setdefault("icon", DEFAULT_ICON)
        pub.setdefault("show", False)
        pub.update(st)
        out.append(pub)
    return out
