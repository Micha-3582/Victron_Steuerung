"""
Tuya-/Smart-Life-Geraete (z.B. Gosund-Steckdosen) - LOKAL im Heimnetz steuern.
Die Tuya-Cloud wird nur einmalig zum Abholen der Geraete-Schluessel ("Local Keys")
gebraucht; geschaltet und gelesen wird direkt im LAN (Bibliothek `tinytuya`).

Die Bibliothek wird erst bei Bedarf importiert: fehlt sie (z.B. nach einem Update
ohne `pip install -r requirements.txt`), laeuft der Rest der App normal weiter und
nur die Tuya-Funktionen melden einen verstaendlichen Fehler.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import threading
import time

log = logging.getLogger("tuya")

CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tuya_cloud.json")
REGIONS = {"eu": "Europa (Zentral)", "eu-w": "Europa (West)", "us": "USA (West)",
           "us-e": "USA (Ost)", "in": "Indien", "cn": "China"}
CALL_TIMEOUT = 3
STATUS_TTL_S = 3.0                # kurz zwischenspeichern: Tuya-Steckdosen vertragen nur 1 Verbindung
OFFLINE_TTL_S = 15.0              # nicht erreichbare Geraete seltener anfragen (Timeout dauert ~2 s)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_status_cache: dict[str, tuple[float, dict]] = {}     # dev_id -> (gueltig bis, Status)
_scan_cache: dict[str, dict] = {}          # dev_id -> Kandidat inkl. local_key (nie zum Browser)


class TuyaError(Exception):
    pass


def _tt():
    try:
        import tinytuya          # noqa: PLC0415
    except ImportError:
        raise TuyaError("Die Tuya-Bibliothek fehlt – auf dem Server einmal "
                        "`pip install -r requirements.txt` ausführen und neu starten")
    return tinytuya


def _lock_for(dev_id: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(dev_id, threading.Lock())


# ---------------------------------------------------------------- Zugangsdaten
def load_credentials() -> dict:
    try:
        with open(CREDENTIALS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def credentials_public() -> dict:
    c = load_credentials()
    return {"configured": bool(c.get("api_key") and c.get("api_secret")),
            "region": c.get("region") or "eu", "api_key": c.get("api_key", ""),
            "networks": ", ".join(c.get("networks") or []), "regions": REGIONS}


def parse_networks(text: str) -> list[str]:
    """Weitere Netze (andere VLANs) fuer die Suche, z.B. '192.168.178.0/24, 192.168.48.0/21'."""
    out = []
    for tok in (text or "").replace(";", ",").replace("\n", ",").replace(" ", ",").split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            net = ipaddress.ip_network(tok, strict=False)
        except ValueError:
            raise TuyaError(f"Ungültiges Netz: {tok} (Beispiel: 192.168.178.0/24)")
        if net.version != 4 or not net.is_private or net.prefixlen < 20:
            raise TuyaError(f"{tok}: nur private IPv4-Netze bis /20 (max. 4096 Adressen) erlaubt")
        out.append(str(net))
    return out


def save_credentials(region: str, api_key: str, api_secret: str = "", networks: str | None = None):
    region = (region or "eu").strip()
    if region not in REGIONS:
        raise TuyaError("Unbekannte Region")
    api_key = (api_key or "").strip()
    old = load_credentials()
    api_secret = (api_secret or "").strip() or old.get("api_secret", "")   # leer = unveraendert
    if not api_key or not api_secret:
        raise TuyaError("Access ID und Access Secret eintragen")
    nets = old.get("networks", []) if networks is None else parse_networks(networks)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        json.dump({"region": region, "api_key": api_key, "api_secret": api_secret, "networks": nets}, f, indent=2)


# ---------------------------------------------------------------- Suchen
def _cloud_devices() -> list[dict]:
    c = load_credentials()
    if not (c.get("api_key") and c.get("api_secret")):
        raise TuyaError("Erst den Tuya-Zugang (Access ID/Secret) eintragen")
    tt = _tt()
    cloud = tt.Cloud(apiRegion=c.get("region", "eu"), apiKey=c["api_key"], apiSecret=c["api_secret"])
    res = cloud.getdevices()
    if isinstance(res, dict):                       # Fehler (falsche Keys, Region, Konto nicht verknuepft ...)
        raise TuyaError("Tuya-Cloud: " + str(res.get("Error") or res.get("Payload") or res))
    return [d for d in res if isinstance(d, dict) and d.get("id") and d.get("key")]


def discover(known_ids: set[str]) -> list[dict]:
    """Holt die Geraete samt Keys aus der Cloud und sucht sie per Broadcast im LAN.
    Rueckgabe: Kandidaten ohne Schluessel; die Schluessel bleiben serverseitig im Zwischenspeicher."""
    tt = _tt()
    cloud = _cloud_devices()
    nets = load_credentials().get("networks") or []
    if nets:
        # UDP-Broadcast kommt nicht ueber VLAN-Grenzen: zusaetzlich die genannten Netze per TCP (Port 6668)
        # absuchen; die Geraete werden dort anhand ihrer Cloud-Schluessel erkannt.
        from tinytuya import scanner          # noqa: PLC0415
        tdevs = [{"id": d["id"], "key": d["key"], "name": d.get("name") or d["id"], "mac": d.get("mac") or ""}
                 for d in cloud]
        local = scanner.devices(verbose=False, color=False, poll=True, byID=True, forcescan=nets,
                                tuyadevices=tdevs, assume_yes=True) or {}
    else:
        local = tt.deviceScan(verbose=False, color=False, poll=False, byID=True) or {}
    out = []
    _scan_cache.clear()
    for d in cloud:
        if d.get("sub"):                            # Zigbee/BLE-Untergeraete haengen an einem Gateway
            continue
        lo = local.get(d["id"]) or {}
        cand = {"dev_id": d["id"], "name": d.get("name") or d["id"],
                "model": d.get("product_name") or d.get("category") or "Tuya",
                "ip": lo.get("ip") or "", "version": str(lo.get("version") or lo.get("ver") or ""),
                "key": d["key"]}
        _scan_cache[d["id"]] = cand
        out.append({**{k: v for k, v in cand.items() if k != "key"},
                    "found_local": bool(cand["ip"]), "known": d["id"] in known_ids})
    return out


def _detect_version(entry: dict) -> str:
    """Protokollversion durch Ausprobieren ermitteln (bei per Hand eingetragener IP)."""
    for v in ("3.3", "3.4", "3.5", "3.1"):
        entry["version"] = v
        try:
            _raw_status(entry)
            return v
        except TuyaError:
            continue
    raise TuyaError("Das Gerät antwortet unter dieser IP nicht (Adresse, Netzfreigabe und Schlüssel prüfen)")


def build_entry(dev_id: str, ip: str = "") -> dict:
    """Legt aus einem Suchergebnis einen Geraete-Eintrag an (Schalt-/Leistungs-Datenpunkt per Statusabfrage).
    `ip`: von Hand eingetragene Adresse fuer Geraete, die die Suche nicht gefunden hat (z.B. anderes VLAN)."""
    cand = _scan_cache.get(dev_id)
    if not cand:
        raise TuyaError("Suchergebnis abgelaufen – bitte erneut nach Tuya-Geräten suchen")
    manual = bool((ip or "").strip())
    if manual:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            raise TuyaError("Ungültige IP-Adresse")
        if addr.version != 4 or not addr.is_private or addr.is_loopback:
            raise TuyaError("Nur IP-Adressen aus dem lokalen Netz erlaubt")
        cand = {**cand, "ip": str(addr), "version": ""}
    if not cand["ip"]:
        raise TuyaError(f"{cand['name']}: im Heimnetz nicht gefunden – ist das Gerät online und im selben Netz? "
                        "Dann erneut suchen")
    entry = {"kind": "tuya", "dev_id": dev_id, "local_key": cand["key"], "ip": cand["ip"],
             "version": cand["version"] or "3.3", "dp": "1", "power_dp": None, "power_scale": 0.1,
             "model": "Tuya · " + cand["model"], "name": cand["name"], "gen": 0, "channel": 0}
    if manual:
        entry["version"] = _detect_version(entry)
    try:
        dps = _raw_status(entry).get("dps") or {}
    except TuyaError as e:
        log.info("Tuya %s: Status beim Anlegen nicht lesbar (%s)", dev_id, e)
        dps = {}
    sw = [k for k, v in dps.items() if isinstance(v, bool)]
    if not sw and dps:
        raise TuyaError(f"{cand['name']}: kein Schalt-Datenpunkt gefunden – vermutlich kein Schalter/Steckdose")
    if sw:
        entry["dp"] = "1" if "1" in sw else sorted(sw, key=lambda k: (len(k), k))[0]
    if "19" in dps and isinstance(dps["19"], (int, float)) and not isinstance(dps["19"], bool):
        entry["power_dp"] = "19"          # cur_power in 0,1 W (Standard bei Gosund/Tuya-Steckdosen mit Messung)
    return entry


# ---------------------------------------------------------------- Status/Schalten
def _device(d: dict):
    tt = _tt()
    if not d.get("ip"):
        raise TuyaError("IP unbekannt – Tuya-Suche ausführen")
    dev = tt.OutletDevice(d["dev_id"], d["ip"], d["local_key"], version=float(d.get("version") or 3.3),
                          connection_timeout=CALL_TIMEOUT, persist=False, connection_retry_limit=1,
                          connection_retry_delay=0)
    dev.set_socketPersistent(False)
    dev.set_socketTimeout(CALL_TIMEOUT)
    return dev


def _raw_status(d: dict) -> dict:
    try:
        with _lock_for(d["dev_id"]):
            st = _device(d).status()
    except TuyaError:
        raise
    except Exception as e:                            # noqa: BLE001  (Socket-/Protokollfehler der Bibliothek)
        raise TuyaError(str(e))
    if not isinstance(st, dict) or "dps" not in st:
        raise TuyaError(str((st or {}).get("Error") or "keine Antwort") if isinstance(st, dict) else "keine Antwort")
    return st


def status(d: dict) -> dict:
    """{'online': bool, 'on': bool|None, 'power': W|None}"""
    now = time.time()
    hit = _status_cache.get(d["dev_id"])
    if hit and now < hit[0]:
        return hit[1]
    try:
        dps = _raw_status(d)["dps"]
        on = dps.get(d.get("dp", "1"))
        if on is None:
            raise TuyaError("Schalt-Datenpunkt fehlt")
        power = None
        pdp = d.get("power_dp")
        if pdp and isinstance(dps.get(pdp), (int, float)):
            power = round(float(dps[pdp]) * float(d.get("power_scale") or 0.1), 1)
        res = {"online": True, "on": bool(on), "power": power}
    except TuyaError:
        res = {"online": False, "on": None, "power": None}
    _status_cache[d["dev_id"]] = (now + (STATUS_TTL_S if res["online"] else OFFLINE_TTL_S), res)
    return res


def set_state(d: dict, on: bool) -> dict:
    try:
        with _lock_for(d["dev_id"]):
            r = _device(d).set_value(d.get("dp", "1"), bool(on))
    except TuyaError:
        raise
    except Exception as e:                            # noqa: BLE001  (Socket-/Protokollfehler der Bibliothek)
        raise TuyaError(f"Schalten fehlgeschlagen: {e}")
    if isinstance(r, dict) and r.get("Error"):
        raise TuyaError(f"Schalten fehlgeschlagen: {r.get('Error')}")
    _status_cache.pop(d["dev_id"], None)
    return status(d)
