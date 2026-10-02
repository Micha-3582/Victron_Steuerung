"""
Homematic / HomematicIP ueber die OpenCCU (auch RaspberryMatic / CCU3) - LOKAL per JSON-RPC
(`http://<ccu>/api/homematic.cgi`, gleiche Schnittstelle wie die CCU-Weboberflaeche selbst).
Keine Zusatzpakete (nur `requests`), keine Cloud.

Ablauf: Mit Benutzer/Passwort der CCU anmelden (`Session.login`), Geraete samt Namen/Raeumen holen
(`Device.listAllDetail`, `Room.getAll`), Schaltkanaele erkennen (Kanal mit schreibbarem `STATE` = Schalter,
mit schreibbarem `LEVEL` und Typ DIMMER = Dimmer) und lesen/schalten ueber `Interface.getValue` /
`Interface.setValue`. Leistung kommt - falls das Geraet misst - vom Messkanal desselben Geraets (`POWER`).

Jeder Schalt-/Dimmkanal ist ein eigener Eintrag (id = "hm-<adresse>", Adresse mit ":" -> "-").
Die Aufrufformen sind aus dem WebUI-Code der CCU abgelesen (webui.js): `valueKey`, `paramsetKey: "VALUES"`,
`type`/`value` bei setValue. Einschalten mit Sicherheits-Timer nutzt den Datenpunkt `ON_TIME` (Sekunden).
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import socketserver
import threading
import time
import xmlrpc.client
from urllib.parse import quote
from xmlrpc.server import SimpleXMLRPCRequestHandler, SimpleXMLRPCServer
from concurrent.futures import ThreadPoolExecutor

import requests

log = logging.getLogger("homematic")

CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homematic.json")
SENSORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "homematic_sensors.json")
CALL_TIMEOUT = 5.0
UNREACH_TTL_S = 30.0              # "Nicht erreichbar" je Geraet nur alle 30 s neu fragen (spart Aufrufe je Statusrunde)
SCAN_TTL_S = 900.0                # Suchergebnis so lange fuer "Hinzufuegen" merken
SKIP_INTERFACES = {"VirtualDevices"}     # CCU-Gruppen/Heizungsgruppen - keine echten Geraete

_http = requests.Session()
_lock = threading.Lock()
_session_id: dict[str, str] = {}                           # CCU-Basisadresse -> Sitzungs-ID
_unreach_cache: dict[tuple, tuple[float, bool]] = {}       # (interface, geraeteadresse) -> (gueltig bis, nicht erreichbar)
_scan_cache: dict[str, dict] = {}                          # adresse -> Kandidat
_scan_ts = 0.0
_sensor_scan: dict[str, dict] = {}                      # sensor-id -> Kandidat der letzten Sensor-Suche
_sensor_scan_ts = 0.0
_value_cache: dict[str, tuple[float, object]] = {}   # sensor-id -> (gueltig bis, Wert)
VALUE_TTL_S = 1.0
_pushed: dict[tuple, tuple[float, object]] = {}      # (schnittstelle, adresse, datenpunkt) -> (Zeit, Wert): Meldungen der CCU bzw. letzte Abfrage
PUSH_VALUE_TTL_S = 90.0           # ein gemerkter Wert gilt so lange; danach wird einmal nachgefragt (Sicherheitsnetz gegen verpasste Meldungen)


class HomematicError(Exception):
    pass


# ---------------------------------------------------------------- Zugangsdaten
def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def credentials_public() -> dict:
    """Fuer die Einstellungen: ohne Passwort."""
    c = load_credentials()
    return {"configured": bool(c.get("host")), "host": c.get("host", ""), "user": c.get("user", ""),
            "password_set": bool(c.get("password"))}


def _split_host(host: str) -> tuple[str, str]:
    """('http'|'https', ip) aus '192.168.2.22' bzw. 'https://192.168.2.22'. Nur private IPv4 (kein Web-Proxy-Missbrauch)."""
    raw = (host or "").strip().rstrip("/")
    scheme = "http"
    for s in ("https://", "http://"):
        if raw.lower().startswith(s):
            scheme, raw = s[:-3], raw[len(s):]
            break
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        raise HomematicError("Ungültige Adresse – bitte die IP der CCU eintragen, z. B. 192.168.2.22")
    if addr.version != 4 or not (addr.is_private and not addr.is_loopback):
        raise HomematicError("Nur IP-Adressen aus dem lokalen Netz erlaubt")
    return scheme, str(addr)


def save_credentials(host: str, user: str, password: str = ""):
    scheme, ip = _split_host(host)
    old = load_credentials()
    password = password or old.get("password", "")           # leer = unveraendert
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump({**old, "host": f"{scheme}://{ip}", "user": (user or "").strip(), "password": password}, f, indent=2)
    with _lock:
        _session_id.clear()                                   # neue Zugangsdaten -> neu anmelden
        _unreach_cache.clear()


# ---------------------------------------------------------------- JSON-RPC
def _base(c: dict) -> str:
    if not c.get("host"):
        raise HomematicError("Homematic ist noch nicht eingerichtet (Einstellungen → Geräte → Homematic)")
    scheme, ip = _split_host(c["host"])
    return f"{scheme}://{ip}"


def _post(base: str, method: str, params: dict) -> dict:
    try:
        r = _http.post(f"{base}/api/homematic.cgi", json={"jsonrpc": "1.1", "method": method, "params": params, "id": 1},
                       timeout=CALL_TIMEOUT, verify=not base.startswith("https"))
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        raise HomematicError(f"CCU nicht erreichbar: {e}")
    if not isinstance(data, dict):
        raise HomematicError("Unerwartete Antwort der CCU")
    return data


def _login(c: dict, base: str) -> str:
    data = _post(base, "Session.login", {"username": c.get("user", ""), "password": c.get("password", "")})
    sid = data.get("result")
    if not sid or data.get("error"):
        raise HomematicError("Anmeldung an der CCU fehlgeschlagen – Benutzer und Passwort prüfen")
    with _lock:
        _session_id[base] = sid
    return sid


def _call(method: str, params: dict | None = None):
    """JSON-RPC-Aufruf mit automatischer Anmeldung; bei abgelaufener Sitzung einmal neu anmelden."""
    c = load_credentials()
    base = _base(c)
    for attempt in (0, 1):
        sid = _session_id.get(base) or _login(c, base)
        data = _post(base, method, {**(params or {}), "_session_id_": sid})
        err = data.get("error")
        if not err:
            return data.get("result")
        msg = str(err.get("message") or err) if isinstance(err, dict) else str(err)
        expired = "access denied" in msg.lower() or "session" in msg.lower()
        if attempt == 0 and expired:
            with _lock:
                _session_id.pop(base, None)
            continue
        raise HomematicError(f"CCU: {msg}")
    raise HomematicError("CCU: Anmeldung nicht möglich")


def test_connection() -> dict:
    """Anmelden und Geraeteanzahl zaehlen - fuer 'Zugang speichern & testen'."""
    devs = _call("Device.listAllDetail") or []
    real = [d for d in devs if d and d.get("interface") not in SKIP_INTERFACES]
    return {"devices": len(real)}


# ---------------------------------------------------------------- Suchen
def _param_desc(interface: str, address: str) -> dict[str, dict]:
    """VALUES-Parameter eines Kanals als {NAME: Beschreibung} (die CCU liefert eine Liste)."""
    res = _call("Interface.getParamsetDescription", {"interface": interface, "address": address, "paramsetKey": "VALUES"})
    if isinstance(res, dict):
        return {k: v for k, v in res.items() if isinstance(v, dict)}
    return {p.get("NAME"): p for p in (res or []) if isinstance(p, dict) and p.get("NAME")}


def _writable(p: dict | None) -> bool:
    try:
        return bool(p) and (int(p.get("OPERATIONS", 0)) & 2) == 2
    except (TypeError, ValueError):
        return False


def _is_default_name(name: str, address: str, dev_type: str) -> bool:
    n = (name or "").strip()
    return (not n) or address in n or n.startswith(dev_type)


def _probe_channel(item: dict) -> dict | None:
    """Prueft einen Kandidaten-Kanal; liefert den fertigen Eintrag oder None (nicht schaltbar)."""
    try:
        desc = _param_desc(item["interface"], item["address"])
    except HomematicError:
        return None
    ctype = item["ctype"].upper()
    if "STATE" in desc and _writable(desc["STATE"]) and "BOOL" in str(desc["STATE"].get("TYPE", "")).upper():
        dp = "STATE"
    elif "DIMMER" in ctype and "LEVEL" in desc and _writable(desc["LEVEL"]):
        dp = "LEVEL"
    else:
        return None
    power_addr = ""
    for cand in item.get("power_candidates", []):
        try:
            if "POWER" in _param_desc(item["interface"], cand):
                power_addr = cand
                break
        except HomematicError:
            continue
    return {**{k: item[k] for k in ("address", "interface", "name", "model", "room", "channel")},
            "datapoint": dp, "power_addr": power_addr}


def discover(known_ids: set[str]) -> list[dict]:
    """Alle schaltbaren Kanaele der CCU (Schalter + Dimmer). Merkt sich das Ergebnis fuer `build_entries`."""
    global _scan_ts
    devices = [d for d in (_call("Device.listAllDetail") or [])
               if d and d.get("interface") not in SKIP_INTERFACES and d.get("channels")]
    rooms: dict[str, str] = {}                  # Kanal-ID -> Raumname (nicht kritisch)
    try:
        for room in _call("Room.getAll") or []:
            for cid in room.get("channelIds") or []:
                rooms[str(cid)] = room.get("name") or ""
    except (HomematicError, AttributeError, TypeError):
        pass                                    # Raumnamen sind nur Zierde - nie die Suche daran scheitern lassen
    items = []
    for dev in devices:
        chans = dev["channels"]
        dtype = str(dev.get("type") or "")
        power_candidates = [ch["address"] for ch in chans
                            if any(k in str(ch.get("channelType") or "").upper()
                                   for k in ("POWERMETER", "ENERGIE_METER", "ENERGY_METER"))]
        for ch in chans:
            ctype = str(ch.get("channelType") or "")
            up = ctype.upper()
            if not (("SWITCH" in up or "DIMMER" in up) and "TRANSMITTER" not in up and "SENSOR" not in up):
                continue
            addr = str(ch["address"])
            ch_name = str(ch.get("name") or "")
            dev_name = str(dev.get("name") or "")
            name = dev_name if _is_default_name(ch_name, addr, dtype) else ch_name
            items.append({"address": addr, "interface": dev["interface"], "ctype": ctype, "model": dtype,
                          "name": name or addr, "channel": ch.get("index"),
                          "room": rooms.get(str(ch.get("id")), "") or rooms.get(str(dev.get("id")), ""),
                          "power_candidates": power_candidates})
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [r for r in ex.map(_probe_channel, items) if r]
    found.sort(key=lambda f: (f["room"].lower(), f["name"].lower(), f["address"]))
    with _lock:
        _scan_cache.clear()
        _scan_cache.update({f["address"]: f for f in found})
        _scan_ts = time.time()
    for f in found:
        f["id"] = entry_id(f["address"])
        f["known"] = f["id"] in known_ids
    return found


# ---------------------------------------------------------------- Anlegen
def entry_id(address: str) -> str:
    return "hm-" + address.replace(":", "-")


def _icon_for(f: dict) -> str:
    n = f"{f.get('name', '')} {f.get('model', '')}".lower()
    if f["datapoint"] == "LEVEL" or any(w in n for w in ("licht", "lampe", "leuchte", "light", "led", "strahler")):
        return "💡"
    return "🔌"


def build_entries(addresses: list[str]) -> list[dict]:
    """Eintraege fuer die gewaehlten Adressen (aus dem letzten Suchergebnis; ist es alt, wird neu gesucht)."""
    if time.time() - _scan_ts > SCAN_TTL_S or any(a not in _scan_cache for a in addresses):
        discover(set())
    out = []
    for a in addresses:
        f = _scan_cache.get(a)
        if not f:
            raise HomematicError(f"Kanal {a} nicht gefunden (nicht schaltbar oder in der CCU entfernt)")
        out.append({"id": entry_id(a), "kind": "homematic", "address": a, "interface": f["interface"],
                    "datapoint": f["datapoint"], "power_addr": f["power_addr"], "mac": None, "gen": 0,
                    "channel": f["channel"], "model": f["model"], "name": f["name"], "room": f["room"],
                    "ip": a,                                   # Anzeige in der Geraeteliste: die CCU-Adresse
                    "icon": _icon_for(f)})
    return out


# ---------------------------------------------------------------- Werte lesen (Push-Wert, sonst CCU fragen)
def _get_value(interface: str, address: str, key: str):
    """Datenpunkt lesen. Ist der Push von der CCU aktiv und der Wert frisch (Meldung der CCU), kostet das keinen Aufruf;
    sonst wird die CCU gefragt (und der Wert gemerkt)."""
    if push_active():
        hit = _pushed.get((interface, address, key))
        if hit and time.time() - hit[0] < PUSH_VALUE_TTL_S:
            return hit[1]
    v = _call("Interface.getValue", {"interface": interface, "address": address, "valueKey": key})
    _pushed[(interface, address, key)] = (time.time(), v)
    return v


# ---------------------------------------------------------------- Status / Schalten
def _truthy(v) -> bool:
    """Wahrheitswert aus der CCU-Antwort - alte Funk-Geraete (BidCos) liefern teils Texte wie "false"/"0" statt echter Booleans."""
    if isinstance(v, str):
        return v.strip().lower() not in ("", "false", "0", "no", "off", "null", "none")
    return bool(v)


def _unreach(d: dict) -> bool:
    dev_addr = str(d["address"]).split(":")[0]
    key = (d["interface"], dev_addr)
    now = time.time()
    hit = _unreach_cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    try:
        v = _truthy(_get_value(d["interface"], dev_addr + ":0", "UNREACH"))
    except HomematicError:
        v = False                      # Kanal :0 nicht lesbar -> nicht als offline werten
    _unreach_cache[key] = (now + UNREACH_TTL_S, v)
    return v


def status(d: dict) -> dict:
    """{'online': bool, 'on': bool|None, 'power': W|None}"""
    try:
        dp = d.get("datapoint", "STATE")
        v = _get_value(d["interface"], d["address"], dp)
        if _unreach(d):
            return {"online": False, "on": None, "power": None,
                    "error": "Die CCU meldet das Gerät als nicht erreichbar (UNREACH) – Funkverbindung/Strom prüfen"}
        on = _truthy(v) if dp == "STATE" else float(v or 0) > 0
        power = None
        if d.get("power_addr"):
            try:
                pv = _get_value(d["interface"], d["power_addr"], "POWER")
                power = None if pv is None else round(float(pv), 1)
            except (HomematicError, TypeError, ValueError):
                power = None
        return {"online": True, "on": on, "power": power}
    except (HomematicError, KeyError, TypeError, ValueError) as e:
        return {"online": False, "on": None, "power": None, "error": str(e) or e.__class__.__name__}


def _set_value(d: dict, key: str, typ: str, value):
    params = {"interface": d["interface"], "address": d["address"], "valueKey": key, "type": typ, "value": value}
    _pushed.pop((d["interface"], d["address"], key), None)          # gemerkter Wert ist ab jetzt ueberholt
    try:
        _call("Interface.setValue", params)
    except HomematicError:
        # Manche CCU-Staende erwarten Zahlen/Texte statt JSON-true/false - einmal in der anderen Schreibweise versuchen
        if typ == "bool":
            params["value"] = "1" if value else "0"
            _call("Interface.setValue", params)
        else:
            raise


def set_state(d: dict, on: bool, timer_s: int | None = None):
    """Schaltet/dimmt einen Kanal (Dimmer: 100 % bzw. aus). `timer_s` (nur Einschalten): eingebauter Rueckschalt-Timer
    des Geraets (`ON_TIME`, Sekunden) - nach Ablauf schaltet es von selbst aus; 0 hebt einen laufenden Timer auf."""
    dp = d.get("datapoint", "STATE")
    value = bool(on) if dp == "STATE" else (1.0 if on else 0.0)
    typ = "bool" if dp == "STATE" else "double"
    try:
        if on and timer_s is not None:
            # ON_TIME gemeinsam mit dem Einschalten schreiben (putParamset = eine Sendung); klappt das nicht
            # (z. B. Konto ohne Schreibrecht dafuer), wird ohne Timer geschaltet
            try:
                _call("Interface.putParamset", {
                    "interface": d["interface"], "address": d["address"], "paramsetKey": "VALUES",
                    "set": [{"name": "ON_TIME", "type": "double", "value": float(max(0, int(timer_s)))},
                            {"name": dp, "type": typ, "value": value}]})
            except HomematicError:
                _set_value(d, dp, typ, value)
        else:
            _set_value(d, dp, typ, value)
    except HomematicError as e:
        raise HomematicError(f"Schalten fehlgeschlagen: {e}")
    return status(d)


# ================================================================ Sensoren (nur lesen)
# Art -> (Anzeigename, Einheit, binaer?, [Datenpunkte in Reihenfolge der Vorliebe]).
# Binaere Sensoren liefern wahr/falsch und werden in den Regeln mit "TRUE/FALSE" benutzt, Messwerte mit unter/ueber.
SENSOR_KINDS = {
    "temperature": ("Temperatur", "°C", False, ["ACTUAL_TEMPERATURE", "TEMPERATURE"]),
    "humidity": ("Luftfeuchte", "%", False, ["HUMIDITY", "ACTUAL_HUMIDITY"]),
    "brightness": ("Helligkeit", "", False, ["ILLUMINATION", "BRIGHTNESS", "CURRENT_ILLUMINATION"]),
    "power": ("Leistung", "W", False, ["POWER"]),
    "contact": ("Fenster/Tür", "", True, ["STATE"]),                       # wahr = offen
    "motion": ("Bewegung", "", True, ["MOTION"]),                          # wahr = Bewegung erkannt
    "presence": ("Anwesenheit", "", True, ["PRESENCE_DETECTION_STATE"]),   # wahr = jemand da
}
# Welche kanaltypen zu welchen Sensorarten gehoeren koennen (Teilstrings des channelType)
_SENSOR_HINTS = [
    (("WEATHER", "CLIMATECONTROL", "THERMALCONTROL", "TEMPERATURE"), ("temperature", "humidity")),
    (("SHUTTER_CONTACT", "ROTARY_HANDLE", "WINDOW", "DOOR_SENSOR"), ("contact",)),
    (("MOTION_DETECTOR",), ("motion", "brightness")),
    (("PRESENCE_DETECTION",), ("presence", "brightness")),
    (("POWERMETER", "ENERGIE_METER", "ENERGY_METER"), ("power",)),
]


def load_sensors() -> list[dict]:
    try:
        with open(SENSORS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_sensors(items: list[dict]):
    with _lock, open(SENSORS_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def sensor_id(address: str, datapoint: str) -> str:
    return "hmS-" + address.replace(":", "-") + "-" + datapoint


def _probe_sensor_channel(item: dict) -> list[dict]:
    """Prueft einen Kandidaten-Kanal; liefert je lesbarem Datenpunkt einen Sensor-Eintrag."""
    try:
        desc = _param_desc(item["interface"], item["address"])
    except HomematicError:
        return []
    out = []
    for kind in item["kinds"]:
        label, unit, binary, dps = SENSOR_KINDS[kind]
        dp = next((d for d in dps if d in desc and (int(desc[d].get("OPERATIONS", 0) or 0) & 1)), None)
        if not dp:
            continue
        if dp == "ILLUMINATION":
            unit = "lux"
        out.append({"id": sensor_id(item["address"], dp), "kind": kind, "address": item["address"],
                    "interface": item["interface"], "datapoint": dp, "unit": unit, "binary": binary,
                    "model": item["model"], "room": item["room"],
                    "name": f"{item['name']} – {label}"})
    return out


def discover_sensors(known_ids: set[str]) -> list[dict]:
    """Alle lesbaren Sensoren der CCU (Temperatur, Luftfeuchte, Fenster/Tuer, Bewegung, Anwesenheit, Helligkeit,
    Leistung). Merkt sich das Ergebnis fuer `build_sensors`."""
    global _sensor_scan_ts
    devices = [d for d in (_call("Device.listAllDetail") or [])
               if d and d.get("interface") not in SKIP_INTERFACES and d.get("channels")]
    rooms: dict[str, str] = {}
    try:
        for room in _call("Room.getAll") or []:
            for cid in room.get("channelIds") or []:
                rooms[str(cid)] = room.get("name") or ""
    except (HomematicError, AttributeError, TypeError):
        pass
    items = []
    for dev in devices:
        dtype, dev_name = str(dev.get("type") or ""), str(dev.get("name") or "")
        for ch in dev["channels"]:
            up = str(ch.get("channelType") or "").upper()
            kinds = []
            for hints, ks in _SENSOR_HINTS:
                if any(h in up for h in hints):
                    kinds.extend(k for k in ks if k not in kinds)
            if not kinds or "SWITCH_VIRTUAL" in up:
                continue
            addr, ch_name = str(ch["address"]), str(ch.get("name") or "")
            name = dev_name if _is_default_name(ch_name, addr, dtype) else ch_name
            items.append({"address": addr, "interface": dev["interface"], "kinds": kinds, "model": dtype,
                          "name": name or addr,
                          "room": rooms.get(str(ch.get("id")), "") or rooms.get(str(dev.get("id")), "")})
    with ThreadPoolExecutor(max_workers=8) as ex:
        found = [s for res in ex.map(_probe_sensor_channel, items) for s in res]
    found.sort(key=lambda f: (f["room"].lower(), f["name"].lower(), f["id"]))
    with _lock:
        _sensor_scan.clear()
        _sensor_scan.update({f["id"]: f for f in found})
        _sensor_scan_ts = time.time()
    for f in found:
        f["known"] = f["id"] in known_ids
    return found


def build_sensors(ids: list[str]) -> list[dict]:
    if time.time() - _sensor_scan_ts > SCAN_TTL_S or any(i not in _sensor_scan for i in ids):
        discover_sensors(set())
    out = []
    for i in ids:
        f = _sensor_scan.get(i)
        if not f:
            raise HomematicError("Sensor nicht gefunden (in der CCU entfernt?)")
        out.append({k: f[k] for k in ("id", "kind", "address", "interface", "datapoint", "unit", "binary",
                                      "model", "room", "name")})
    return out


def read_sensor(sen: dict):
    """Aktueller Wert: Zahl (Messwert), True/False (binaer) oder None (nicht lesbar/Geraet nicht erreichbar)."""
    now = time.time()
    hit = _value_cache.get(sen["id"])
    if hit and hit[0] > now:
        return hit[1]
    try:
        v = _get_value(sen["interface"], sen["address"], sen["datapoint"])
        if _unreach(sen) or v is None:
            val = None
        elif sen.get("binary"):
            val = _truthy(v) if isinstance(v, (bool, str)) and not str(v).strip().isdigit() else bool(float(v))          # Drehgriff: 0 zu, 1 gekippt, 2 offen -> offen = wahr
        else:
            val = round(float(v), 1)
    except (HomematicError, KeyError, TypeError, ValueError):
        val = None
    _value_cache[sen["id"]] = (now + VALUE_TTL_S, val)
    return val


def read_values(sensors: list[dict]) -> dict[str, object]:
    """{sensor-id: Wert} fuer mehrere Sensoren (parallel)."""
    if not sensors:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(sensors))) as ex:
        vals = list(ex.map(read_sensor, sensors))
    return {s["id"]: v for s, v in zip(sensors, vals)}


def sensors_scan() -> list[dict]:
    return discover_sensors({s["id"] for s in load_sensors()})


def add_sensors(ids: list[str]) -> list[dict]:
    """Legt die gewaehlten Sensoren an (schon vorhandene bleiben unveraendert)."""
    new = build_sensors(ids)
    items = load_sensors()
    have = {s["id"] for s in items}
    added = [s for s in new if s["id"] not in have]
    _save_sensors(items + added)
    return added


def update_sensor(sensor_id_: str, name: str | None = None) -> bool:
    items = load_sensors()
    for s in items:
        if s["id"] == sensor_id_:
            if name is not None:
                s["name"] = name.strip()[:60] or s["name"]
            _save_sensors(items)
            return True
    return False


def remove_sensor(sensor_id_: str) -> bool:
    items = load_sensors()
    keep = [s for s in items if s["id"] != sensor_id_]
    if len(keep) == len(items):
        return False
    _save_sensors(keep)
    return True


def list_sensors_with_values() -> list[dict]:
    """Angelegte Sensoren inkl. aktuellem Wert ('value': Zahl | True/False | None = nicht lesbar)."""
    items = load_sensors()
    vals = read_values(items)
    return [{**s, "value": vals.get(s["id"])} for s in items]


# ================================================================ Push: XML-RPC-Rueckkanal der CCU (wie ioBroker hm-rpc)
# Die App meldet sich per `init` bei den Funk-Schnittstellen der CCU an; die CCU schickt dann nur bei Aenderungen ein `event`.
# Faellt der Rueckkanal aus (Port gesperrt, CCU neu gestartet ...), fragt die App wie bisher ab - die Regeln laufen immer weiter.
PUSH_PORT_DEFAULT = 8703
PUSH_ID_PREFIX = "victron-"
PUSH_CYCLE_S = 30.0               # Anmeldung pruefen / ping
PUSH_INIT_EVERY_S = 300.0         # Anmeldung alle 5 Minuten auffrischen
PUSH_ALIVE_MAX_S = 100.0          # laenger keine Meldung (auch kein PONG) = Rueckkanal gilt als ausgefallen
RPC_PORTS = {"BidCos-RF": 2001, "HmIP-RF": 2010, "BidCos-Wired": 2000}
NOISY_KEYS = {"POWER", "ENERGY_COUNTER", "FREQUENCY", "VOLTAGE", "CURRENT", "RSSI_DEVICE", "RSSI_PEER", "PONG"}     # wecken die Regeln nicht
_push_on = False
_push_state: dict[str, dict] = {}
_push_server = None
_push_server_port = 0
_push_thread = None
_push_stop = threading.Event()
_push_kick = threading.Event()
_inited: dict[str, str] = {}                              # schnittstelle -> url, mit der sie angemeldet ist
_watch: set[str] = set()                                  # Geraeteadressen, die aktive Regeln brauchen
_listeners: list = []
_last_wake = 0.0
_events_seen = 0


def add_listener(fn):
    """fn() wird aufgerufen, wenn die CCU eine fuer Regeln relevante Aenderung meldet."""
    _listeners.append(fn)


def set_watch(addresses):
    global _watch
    _watch = {str(a).split(":")[0] for a in addresses}


def push_active() -> bool:
    if not _push_on:
        return False
    now = time.time()
    return any(st.get("ok") and now - st.get("ts", 0) < 3 * PUSH_CYCLE_S for st in _push_state.values())


def _on_event(interface_id, address, key, value):
    global _events_seen, _last_wake
    iface = str(interface_id)
    iface = iface[len(PUSH_ID_PREFIX):] if iface.startswith(PUSH_ID_PREFIX) else iface
    now = time.time()
    _push_state.setdefault(iface, {})["alive"] = now
    _events_seen += 1
    if key == "PONG":
        return ""
    address, key = str(address), str(key)
    _pushed[(iface, address, key)] = (now, value)
    base = address.split(":")[0]
    if key == "UNREACH":
        _unreach_cache[(iface, base)] = (now + PUSH_VALUE_TTL_S, _truthy(value))
    if base in _watch and key not in NOISY_KEYS:
        _value_cache.clear()                              # Sensorwerte neu lesen (aus dem Push-Speicher)
        if now - _last_wake >= 0.3:
            _last_wake = now
            for fn in list(_listeners):
                try:
                    fn()
                except Exception:                         # noqa: BLE001
                    pass
    return ""


class _Handler(SimpleXMLRPCRequestHandler):
    rpc_paths = ("/", "/RPC2")

    def do_POST(self):
        try:
            allowed = _split_host(load_credentials().get("host", ""))[1]
        except HomematicError:
            allowed = None
        if self.client_address[0] != allowed:             # nur die CCU selbst darf melden
            self.send_error(403)
            return
        super().do_POST()

    def log_message(self, *a):
        pass


class _Server(socketserver.ThreadingMixIn, SimpleXMLRPCServer):
    daemon_threads = True
    allow_reuse_address = True


def _make_server(port: int):
    srv = _Server(("0.0.0.0", port), requestHandler=_Handler, allow_none=True, logRequests=False)
    srv.register_introspection_functions()
    srv.register_multicall_functions()                    # die CCU schickt Meldungen gebuendelt (system.multicall)
    srv.register_function(_on_event, "event")
    srv.register_function(lambda *a: [], "listDevices")
    for name in ("newDevices", "deleteDevices", "updateDevice", "replaceDevice", "readdedDevice"):
        srv.register_function(lambda *a: "", name)
    return srv


def _ensure_listener(port: int):
    global _push_server, _push_server_port
    if _push_server and _push_server_port == port:
        return
    if _push_server:
        _push_server.shutdown()
        _push_server.server_close()
        _push_server = None
    srv = _make_server(port)                              # OSError (Port belegt) geht an den Aufrufer
    threading.Thread(target=srv.serve_forever, daemon=True, name="homematic-push").start()
    _push_server, _push_server_port = srv, port


class _TimeoutTransport(xmlrpc.client.Transport):
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = CALL_TIMEOUT
        return conn


def _proxy(c: dict, ip: str, rpc_port: int):
    auth = f"{quote(c.get('user', ''), safe='')}:{quote(c.get('password', ''), safe='')}@" if c.get("user") else ""
    return xmlrpc.client.ServerProxy(f"http://{auth}{ip}:{rpc_port}", transport=_TimeoutTransport(), allow_none=True)


def _local_ip(ccu_ip: str) -> str:
    """Eigene Adresse im Netz der CCU (die CCU ruft uns unter dieser Adresse zurueck)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ccu_ip, 80))
        return s.getsockname()[0]
    finally:
        s.close()


def _known_interfaces() -> set[str]:
    import shelly
    out = {d.get("interface") for d in shelly.load_devices() if d.get("kind") == "homematic"}
    out |= {s.get("interface") for s in load_sensors()}
    return {i for i in out if i}


def _push_port(c: dict) -> int:
    try:
        return int(c.get("push_port") or PUSH_PORT_DEFAULT)
    except (TypeError, ValueError):
        return PUSH_PORT_DEFAULT


def _deinit_all(c: dict):
    if not _inited:
        return
    try:
        ip = _split_host(c.get("host", ""))[1]
    except HomematicError:
        _inited.clear()
        return
    for iface, url in list(_inited.items()):
        try:
            _proxy(c, ip, RPC_PORTS[iface]).init(url, "")        # abmelden
        except Exception:                                 # noqa: BLE001
            pass
        _inited.pop(iface, None)
    _push_state.clear()


def _push_cycle():
    global _push_on
    c = load_credentials()
    _push_on = bool(c.get("push")) and bool(c.get("host"))
    if not _push_on:
        _deinit_all(c)
        return
    now = time.time()
    port = _push_port(c)
    try:
        scheme, ip = _split_host(c["host"])
        if scheme != "http":
            raise HomematicError("Push geht nur mit http-Adresse der CCU")
        _ensure_listener(port)
        url = f"http://{c.get('push_host') or _local_ip(ip)}:{port}"
    except (HomematicError, OSError) as e:
        for iface in _known_interfaces() or {"-"}:
            _push_state[iface] = {"ok": False, "error": str(e), "ts": now}
        return
    for iface in _known_interfaces():
        st = _push_state.setdefault(iface, {})
        rp = RPC_PORTS.get(iface)
        if not rp:
            st.update(ok=False, error="Schnittstelle wird für Push nicht unterstützt", ts=now)
            continue
        try:
            proxy = _proxy(c, ip, rp)
            if _inited.get(iface) != url or now - st.get("init_ts", 0) > PUSH_INIT_EVERY_S:
                proxy.init(url, PUSH_ID_PREFIX + iface)
                _inited[iface] = url
                st.update(init_ts=now, alive=now)
            else:
                proxy.ping(PUSH_ID_PREFIX + iface)
            if now - st.get("alive", 0) > PUSH_ALIVE_MAX_S:
                st["init_ts"] = 0                         # beim naechsten Durchlauf neu anmelden
                st.update(ok=False, error="Keine Meldungen von der CCU – Port/Firewall prüfen")
            else:
                st.update(ok=True, error="")
        except Exception as e:                            # noqa: BLE001
            st["init_ts"] = 0
            st.update(ok=False, error=f"CCU-Anmeldung fehlgeschlagen: {e}")
        st["ts"] = now


def _push_loop():
    while not _push_stop.is_set():
        try:
            _push_cycle()
        except Exception as e:                            # noqa: BLE001
            log.warning("Homematic-Push: %s", e)
        _push_kick.wait(PUSH_CYCLE_S)
        _push_kick.clear()


def push_start():
    global _push_thread
    if _push_thread and _push_thread.is_alive():
        return
    _push_thread = threading.Thread(target=_push_loop, daemon=True, name="homematic-push-manager")
    _push_thread.start()


def push_status() -> dict:
    c = load_credentials()
    return {"enabled": bool(c.get("push")), "port": _push_port(c), "active": push_active(), "events": _events_seen,
            "interfaces": {k: {"ok": bool(v.get("ok")), "error": v.get("error", "")} for k, v in _push_state.items()}}


def save_push(enabled: bool, port=None):
    c = load_credentials()
    if not c.get("host"):
        raise HomematicError("Erst den CCU-Zugang einrichten")
    if port not in (None, ""):
        try:
            p = int(port)
        except (TypeError, ValueError):
            raise HomematicError("Port: Zahl erwartet")
        if not 1024 <= p <= 65535:
            raise HomematicError("Port: zwischen 1024 und 65535")
        c["push_port"] = p
    c["push"] = bool(enabled)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump(c, f, indent=2)
    _push_kick.set()
