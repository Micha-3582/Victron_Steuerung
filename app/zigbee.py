"""
Zigbee ueber ein Phoscon-/deCONZ-Gateway (ConBee, RaspBee) - LOKAL ueber die REST-API (`http://<gateway>/api/<schluessel>/...`).
Keine Zusatzpakete (nur `requests`), keine Cloud.

Schluessel holen: In Phoscon unter Einstellungen -> Gateway -> Erweitert "App autorisieren" druecken, dann in der App "Mit Gateway verbinden"
(`POST /api` mit `devicetype`, das Gateway antwortet mit dem Schluessel). Alternativ einen vorhandenen Schluessel eintragen.

Lichter/Steckdosen (`/lights`, schreibbar `state.on`) sind Schalt-Geraete wie die der anderen Systeme (kind "zigbee", id "zb-<uniqueid>"; die
Leistung kommt vom ZHAPower-Sensor derselben Steckdose). Sensoren (`/sensors`: Tuer/Fenster, Bewegung, Temperatur, Luftfeuchte, Helligkeit, Leistung)
und Thermostate (ZHAThermostat, Solltemperatur `config.heatsetpoint`) landen in denselben Registern wie die von Homematic (mit `source: "zigbee"`),
damit Dashboard, Regeln und Abläufe sie gleich behandeln. Werte werden 1 s zwischengespeichert (eine Abfrage fuer alles).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time

import requests

import homematic

log = logging.getLogger("zigbee")

CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zigbee.json")
CALL_TIMEOUT = 5.0
VALUE_TTL_S = 1.0
SCAN_TTL_S = 900.0
DEFAULT_SP_RANGE = (5.0, 30.0)

_http = requests.Session()
_lock = threading.Lock()
_cache: dict = {"ts": 0.0, "lights": {}, "sensors": {}, "groups": {}}
_scan: dict[str, dict] = {}
_scan_ts = 0.0


class ZigbeeError(Exception):
    pass


# ---------------------------------------------------------------- Zugangsdaten
def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _split_host(host: str) -> tuple[str, int]:
    """(ip, port) aus '192.168.2.66' bzw. 'http://192.168.2.66:8080'. Nur private IPv4 (kein Web-Proxy-Missbrauch)."""
    raw = (host or "").strip().rstrip("/")
    for s in ("https://", "http://"):
        if raw.lower().startswith(s):
            raw = raw[len(s):]
            break
    port = 80
    if ":" in raw:
        raw, p = raw.rsplit(":", 1)
        try:
            port = int(p)
        except ValueError:
            raise ZigbeeError("Ungültiger Port")
        if not 1 <= port <= 65535:
            raise ZigbeeError("Ungültiger Port")
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        raise ZigbeeError("Ungültige Adresse – bitte die IP des Gateways eintragen, z. B. 192.168.2.66")
    if addr.version != 4 or not (addr.is_private and not addr.is_loopback):
        raise ZigbeeError("Nur IP-Adressen aus dem lokalen Netz erlaubt")
    return str(addr), port


def credentials_public() -> dict:
    c = load_credentials()
    return {"configured": bool(c.get("host") and c.get("key")), "host": c.get("host", ""), "key_set": bool(c.get("key"))}


def save_credentials(host: str, key: str = ""):
    ip, port = _split_host(host)
    old = load_credentials()
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump({"host": ip if port == 80 else f"{ip}:{port}", "key": (key or "").strip() or old.get("key", "")}, f, indent=2)
    _invalidate()


def _invalidate():
    with _lock:
        _cache.update(ts=0.0, lights={}, sensors={}, groups={})


# ---------------------------------------------------------------- REST
def _url(c: dict, path: str, with_key: bool = True) -> str:
    if not c.get("host"):
        raise ZigbeeError("Zigbee ist noch nicht eingerichtet (Einstellungen → Smart Home → Zigbee)")
    ip, port = _split_host(c["host"])
    if with_key:
        if not c.get("key"):
            raise ZigbeeError("Kein API-Schlüssel – „Mit Gateway verbinden“ drücken")
        return f"http://{ip}:{port}/api/{c['key']}{path}"
    return f"http://{ip}:{port}/api{path}"


def _request(method: str, path: str, body=None, with_key: bool = True):
    c = load_credentials()
    try:
        r = _http.request(method, _url(c, path, with_key), json=body, timeout=CALL_TIMEOUT)
    except requests.RequestException as e:
        raise ZigbeeError(f"Gateway nicht erreichbar: {e}")
    if r.status_code == 403:
        raise ZigbeeError("Zugriff verweigert – API-Schlüssel ungültig oder abgelaufen (neu verbinden)")
    try:
        data = r.json()
    except ValueError:
        raise ZigbeeError(f"Unerwartete Antwort des Gateways (HTTP {r.status_code})")
    if isinstance(data, list):                      # Fehlerliste: [{"error": {"description": ...}}]
        for item in data:
            if isinstance(item, dict) and item.get("error"):
                raise ZigbeeError(str(item["error"].get("description") or item["error"]))
    if r.status_code >= 400:
        raise ZigbeeError(f"Gateway-Fehler (HTTP {r.status_code})")
    return data


def pair(host: str) -> str:
    """Schluessel vom Gateway holen (in Phoscon vorher "App autorisieren"). Speichert Adresse und Schluessel."""
    ip, port = _split_host(host)
    try:
        r = _http.post(f"http://{ip}:{port}/api", json={"devicetype": "victron-steuerung"}, timeout=CALL_TIMEOUT)
    except requests.RequestException as e:
        raise ZigbeeError(f"Gateway nicht erreichbar: {e}")
    try:
        data = r.json()
    except ValueError:
        raise ZigbeeError("Unerwartete Antwort des Gateways – ist das ein Phoscon/deCONZ-Gateway?")
    first = data[0] if isinstance(data, list) and data else {}
    key = ((first or {}).get("success") or {}).get("username")
    if not key:
        raise ZigbeeError("Das Gateway hat keinen Schlüssel herausgegeben – in Phoscon unter Einstellungen → Gateway → Erweitert "
                          "„App autorisieren“ drücken und dann innerhalb einer Minute hier erneut „Mit Gateway verbinden“")
    save_credentials(host, key)
    return key


def test_connection() -> dict:
    cfg = _request("GET", "/config")
    lights, sensors, _ = _fetch(force=True)
    return {"name": cfg.get("name", "") if isinstance(cfg, dict) else "", "devices": len(lights) + len(sensors)}


def _fetch(force: bool = False, with_groups: bool = False):
    """(lights, sensors, groups) als {id: objekt} - 1 s zwischengespeichert. Gruppen (Raeume) nur fuer die Suche - im Dauerbetrieb 2 Abfragen."""
    now = time.time()
    with _lock:
        if not force and now - _cache["ts"] < VALUE_TTL_S and _cache["lights"] is not None and _cache["ts"]:
            return _cache["lights"], _cache["sensors"], _cache["groups"]
    lights = _request("GET", "/lights")
    sensors = _request("GET", "/sensors")
    groups = _cache["groups"]
    if with_groups:
        try:
            groups = _request("GET", "/groups")
        except ZigbeeError:
            groups = {}
    for d in (lights, sensors, groups):
        if not isinstance(d, dict):
            raise ZigbeeError("Unerwartete Antwort des Gateways")
    with _lock:
        _cache.update(ts=time.time(), lights=lights, sensors=sensors, groups=groups)
    return lights, sensors, groups


def _by_uid(objs: dict, uid: str):
    for oid, o in objs.items():
        if o.get("uniqueid") == uid:
            return oid, o
    return None, None


def _mac(uid: str) -> str:
    return (uid or "")[:23]


def _room(groups: dict, light_id: str) -> str:
    for g in groups.values():
        if str(light_id) in [str(x) for x in (g.get("lights") or [])] and g.get("name"):
            return g["name"]
    return ""


# ---------------------------------------------------------------- Lichter / Steckdosen (Schalter)
def device_id(uid: str) -> str:
    return "zb-" + uid.replace(":", "-")


def discover(known_ids: set[str]) -> list[dict]:
    lights, _, groups = _fetch(force=True, with_groups=True)
    out = []
    for lid, l in lights.items():
        st = l.get("state") or {}
        if "on" not in st or not l.get("uniqueid"):
            continue                                  # Rolllaeden & Co. (kein on/off) vorerst nicht
        out.append({"id": device_id(l["uniqueid"]), "uniqueid": l["uniqueid"], "name": l.get("name") or l["uniqueid"],
                    "model": str(l.get("modelid") or l.get("type") or "Zigbee"), "type": l.get("type", ""),
                    "room": _room(groups, lid), "dimmable": "bri" in st, "known": device_id(l["uniqueid"]) in known_ids})
    out.sort(key=lambda f: (f["room"].lower(), f["name"].lower()))
    with _lock:
        _scan.clear()
        _scan.update({f["id"]: f for f in out})
        global _scan_ts
        _scan_ts = time.time()
    return out


def _icon_for(f: dict) -> str:
    t = (f.get("type") or "").lower()
    return "🔌" if ("plug" in t or "on/off" in t or "outlet" in t) else "💡"


def build_entries(ids: list[str]) -> list[dict]:
    if time.time() - _scan_ts > SCAN_TTL_S or any(i not in _scan for i in ids):
        discover(set())
    ip = _split_host(load_credentials().get("host", ""))[0]
    out = []
    for i in ids:
        f = _scan.get(i)
        if not f:
            raise ZigbeeError("Gerät nicht gefunden (im Gateway entfernt?)")
        out.append({"id": f["id"], "kind": "zigbee", "uniqueid": f["uniqueid"], "mac": None, "gen": 0, "channel": 0, "model": f["model"],
                    "name": f["name"], "room": f["room"], "ip": ip, "icon": _icon_for(f)})
    return out


def status(d: dict) -> dict:
    """{'online': bool, 'on': bool|None, 'power': W|None}"""
    try:
        lights, sensors, _ = _fetch()
        _, l = _by_uid(lights, d["uniqueid"])
        if not l:
            return {"online": False, "on": None, "power": None, "error": "Gerät im Gateway nicht mehr vorhanden"}
        st = l.get("state") or {}
        if st.get("reachable") is False:
            return {"online": False, "on": None, "power": None, "error": "Das Gateway meldet das Gerät als nicht erreichbar"}
        power = None
        for s in sensors.values():
            if s.get("type") == "ZHAPower" and _mac(s.get("uniqueid", "")) == _mac(d["uniqueid"]):
                p = (s.get("state") or {}).get("power")
                power = None if p is None else round(float(p), 1)
                break
        return {"online": True, "on": bool(st.get("on")), "power": power}
    except (ZigbeeError, KeyError, TypeError, ValueError) as e:
        return {"online": False, "on": None, "power": None, "error": str(e)}


def set_state(d: dict, on: bool, timer_s: int | None = None):
    """Schaltet ein Licht/eine Steckdose. Einen eingebauten Rueckschalt-Timer gibt es bei Zigbee-Geraeten nicht (timer_s wird ignoriert)."""
    lights, _, _ = _fetch(force=True)
    lid, _ = _by_uid(lights, d["uniqueid"])
    if lid is None:
        raise ZigbeeError("Gerät im Gateway nicht mehr vorhanden")
    try:
        _request("PUT", f"/lights/{lid}/state", {"on": bool(on)})
    except ZigbeeError as e:
        raise ZigbeeError(f"Schalten fehlgeschlagen: {e}")
    _invalidate()
    return status(d)


# ---------------------------------------------------------------- Sensoren (nur lesen)
# deCONZ-Typ -> (Art, Einheit, binaer?, Datenfeld, Teiler)
SENSOR_TYPES = {
    "ZHAOpenClose": ("contact", "", True, "open", 1),
    "ZHAPresence": ("motion", "", True, "presence", 1),
    "ZHATemperature": ("temperature", "°C", False, "temperature", 100.0),
    "ZHAHumidity": ("humidity", "%", False, "humidity", 100.0),
    "ZHALightLevel": ("brightness", "lux", False, "lux", 1),
    "ZHAPower": ("power", "W", False, "power", 1),
}
KIND_LABEL = {"contact": "Fenster/Tür", "motion": "Bewegung", "temperature": "Temperatur", "humidity": "Luftfeuchte", "brightness": "Helligkeit", "power": "Leistung"}


def sensor_id(uid: str) -> str:
    return "zbS-" + uid.replace(":", "-")


def thermostat_id(uid: str) -> str:
    return "zbT-" + uid.replace(":", "-")


def _sensor_scan_items(known_ids: set[str]) -> list[dict]:
    _, sensors, _ = _fetch(force=True)
    out = []
    for s in sensors.values():
        spec = SENSOR_TYPES.get(s.get("type"))
        if not spec or not s.get("uniqueid"):
            continue
        kind, unit, binary, _, _ = spec
        out.append({"id": sensor_id(s["uniqueid"]), "kind": kind, "address": s["uniqueid"], "interface": "Zigbee", "datapoint": s["type"],
                    "unit": unit, "binary": binary, "model": str(s.get("modelid") or s.get("type")), "room": "",
                    "name": f"{s.get('name') or s['uniqueid']} – {KIND_LABEL[kind]}", "source": "zigbee",
                    "known": sensor_id(s["uniqueid"]) in known_ids})
    out.sort(key=lambda f: f["name"].lower())
    return out


_sensor_scan: dict[str, dict] = {}
_sp_scan: dict[str, dict] = {}


def sensors_scan() -> list[dict]:
    items = _sensor_scan_items({s["id"] for s in homematic.load_sensors()})
    with _lock:
        _sensor_scan.clear()
        _sensor_scan.update({f["id"]: f for f in items})
    return items


def add_sensors(ids: list[str]) -> list[dict]:
    if any(i not in _sensor_scan for i in ids):
        sensors_scan()
    items = homematic.load_sensors()
    have = {s["id"] for s in items}
    added = []
    for i in ids:
        f = _sensor_scan.get(i)
        if not f:
            raise ZigbeeError("Sensor nicht gefunden (im Gateway entfernt?)")
        if i in have:
            continue
        item = {k: f[k] for k in ("id", "kind", "address", "interface", "datapoint", "unit", "binary", "model", "room", "name", "source")}
        if f["kind"] == "contact":
            item["invert"] = True                      # Standard fuer Fenster/Tueren: TRUE = geschlossen (gruen), FALSE = offen (rot)
        items.append(item)
        added.append(item)
    if added:
        homematic._save_sensors(items)
    return added


def read_value(sen: dict):
    """Rohwert eines Zigbee-Sensors: True/False (binaer), Zahl oder None (nicht lesbar/nicht erreichbar)."""
    try:
        _, sensors, _ = _fetch()
        _, s = _by_uid(sensors, sen["address"])
        spec = SENSOR_TYPES.get(sen.get("datapoint"))
        if not s or not spec or (s.get("config") or {}).get("reachable") is False:
            return None
        v = (s.get("state") or {}).get(spec[3])
        if v is None:
            return None
        return bool(v) if spec[2] else float(v) / spec[4]
    except (ZigbeeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Thermostate (Solltemperatur)
def setpoints_scan() -> list[dict]:
    _, sensors, groups = _fetch(force=True, with_groups=True)
    known = {s["id"] for s in homematic.load_setpoints()}
    out = []
    for s in sensors.values():
        if s.get("type") != "ZHAThermostat" or not s.get("uniqueid"):
            continue
        out.append({"id": thermostat_id(s["uniqueid"]), "address": s["uniqueid"], "interface": "Zigbee", "datapoint": "heatsetpoint",
                    "min": DEFAULT_SP_RANGE[0], "max": DEFAULT_SP_RANGE[1], "model": str(s.get("modelid") or "Thermostat"), "room": "",
                    "name": s.get("name") or s["uniqueid"], "source": "zigbee", "known": thermostat_id(s["uniqueid"]) in known})
    out.sort(key=lambda f: f["name"].lower())
    with _lock:
        _sp_scan.clear()
        _sp_scan.update({f["id"]: f for f in out})
    return out


def add_setpoints(ids: list[str]) -> list[dict]:
    if any(i not in _sp_scan for i in ids):
        setpoints_scan()
    items = homematic.load_setpoints()
    have = {s["id"] for s in items}
    added = []
    for i in ids:
        f = _sp_scan.get(i)
        if not f:
            raise ZigbeeError("Thermostat nicht gefunden (im Gateway entfernt?)")
        if i in have:
            continue
        item = {k: f[k] for k in ("id", "address", "interface", "datapoint", "min", "max", "model", "room", "name", "source")}
        items.append(item)
        added.append(item)
    if added:
        homematic._save_setpoints(items)
    return added


def read_setpoint(sp: dict):
    """Aktuelle Solltemperatur in °C oder None."""
    try:
        _, sensors, _ = _fetch()
        _, s = _by_uid(sensors, sp["address"])
        v = ((s or {}).get("config") or {}).get("heatsetpoint")
        return None if v is None else round(float(v) / 100.0, 1)
    except (ZigbeeError, TypeError, ValueError):
        return None


def set_setpoint(sp: dict, value: float) -> float:
    """Solltemperatur setzen (auf den Bereich des Geraets begrenzt). Rueckgabe: gesetzter Wert."""
    v = min(float(sp.get("max", DEFAULT_SP_RANGE[1])), max(float(sp.get("min", DEFAULT_SP_RANGE[0])), float(value)))
    _, sensors, _ = _fetch(force=True)
    sid, _ = _by_uid(sensors, sp["address"])
    if sid is None:
        raise ZigbeeError("Thermostat im Gateway nicht mehr vorhanden")
    _request("PUT", f"/sensors/{sid}/config", {"heatsetpoint": int(round(v * 100))})
    _invalidate()
    return v
