"""
Tasmota-Geraete (Steckdosen/Relais mit Tasmota-Firmware) - lokal per HTTP-Befehlsschnittstelle
`http://<ip>/cm?cmnd=<Befehl>` (keine Zusatzpakete, nur `requests`).

Jeder Relaiskanal ist ein eigener Eintrag (id = "tasmota-<mac>-<kanal>", Kanal ab 1).
Passwortgeschuetzte Geraete (WebPassword) werden per HTTP-Basic-Auth (Benutzer "admin") angesprochen.
"""
from __future__ import annotations

import re

import requests

CALL_TIMEOUT = 3.0
PROBE_TIMEOUT = 0.8


class TasmotaError(Exception):
    pass


def _cmd(ip: str, cmnd: str, timeout: float, auth=None) -> dict:
    r = requests.get(f"http://{ip}/cm", params={"cmnd": cmnd}, timeout=timeout, auth=auth)
    if r.status_code == 401:
        raise TasmotaError("auth")
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("keine JSON-Antwort")
    return data


def _relay_keys(sts: dict) -> list[str]:
    """Schluessel wie POWER / POWER1 / POWER2 aus StatusSTS, nach Kanal sortiert."""
    keys = [k for k in sts if re.fullmatch(r"POWER\d*", k)]
    return sorted(keys, key=lambda k: int(k[5:] or 1))


def _energy_power(sns: dict):
    e = (sns or {}).get("ENERGY")
    if isinstance(e, dict):
        p = e.get("Power")
        if isinstance(p, list):          # mehrphasig: Summe
            p = sum(x for x in p if isinstance(x, (int, float)))
        if isinstance(p, (int, float)):
            return round(float(p), 1)
    return None


def probe(ip: str, timeout: float = PROBE_TIMEOUT, auth=None) -> dict | None:
    """Fragt `Status 0` ab. None = kein (erreichbarer) Tasmota; {'auth': True} = passwortgeschuetzt."""
    try:
        data = _cmd(ip, "Status 0", timeout, auth)
    except TasmotaError:
        return {"ip": ip, "auth": True, "name": "", "mac": "", "channels": []}
    except (requests.RequestException, ValueError):
        return None
    st, net, sts = data.get("Status"), data.get("StatusNET"), data.get("StatusSTS")
    if not isinstance(st, dict) or not isinstance(net, dict) or not isinstance(sts, dict):
        return None
    mac = str(net.get("Mac") or "").replace(":", "").upper()
    if not mac:
        return None
    fn = st.get("FriendlyName")
    name = st.get("DeviceName") or (fn[0] if isinstance(fn, list) and fn else "") or st.get("Topic") or f"Tasmota-{mac[-6:]}"
    fwr = data.get("StatusFWR") if isinstance(data.get("StatusFWR"), dict) else {}
    return {"ip": ip, "auth": False, "name": str(name), "mac": mac,
            "model": "Tasmota " + str(fwr.get("Version") or "").split("(")[0].strip(),
            "channels": [int(k[5:] or 1) for k in _relay_keys(sts)],
            "multi": len(_relay_keys(sts)) > 1}


def _auth(d: dict):
    return ("admin", d["password"]) if d.get("password") else None


def status(d: dict) -> dict:
    """{'online': bool, 'on': bool|None, 'power': W|None}"""
    try:
        data = _cmd(d["ip"], "Status 0", CALL_TIMEOUT, _auth(d))
        sts = data["StatusSTS"]
        key = "POWER" if f"POWER{d['channel']}" not in sts else f"POWER{d['channel']}"
        if key not in sts:
            raise KeyError(key)
        return {"online": True, "on": str(sts[key]).upper() == "ON",
                "power": _energy_power(data.get("StatusSNS"))}
    except (TasmotaError, requests.RequestException, ValueError, KeyError, TypeError):
        return {"online": False, "on": None, "power": None}


def _pulse_value(secs: int) -> int:
    """PulseTime-Wert fuer eine Dauer in Sekunden (Tasmota: 1-111 = 0,1-s-Schritte, ab 112 = Sekunden + 100)."""
    return 100 + max(12, min(int(secs), 64800))


def set_state(d: dict, on: bool, timer_s: int | None = None):
    """Schaltet einen Kanal. `timer_s` (nur Einschalten): Rueckschalt-Timer ueber PulseTime (Sekunden);
    0 hebt einen gesetzten PulseTime auf. Ohne Angabe bleibt eine vorhandene Einstellung unberuehrt."""
    ch = int(d["channel"])
    power = f"Power{ch} {'On' if on else 'Off'}"
    if on and timer_s is not None:
        pulse = 0 if int(timer_s) <= 0 else _pulse_value(int(timer_s))
        cmnd = f"Backlog PulseTime{ch} {pulse}; {power}"
    else:
        cmnd = power
    try:
        resp = _cmd(d["ip"], cmnd, CALL_TIMEOUT, _auth(d))
    except TasmotaError:
        raise TasmotaError("Passwort abgelehnt")
    except (requests.RequestException, ValueError) as e:
        raise TasmotaError(f"Schalten fehlgeschlagen: {e}")
    if "Command" in resp and str(resp.get("Command")).lower().startswith("unknown"):
        raise TasmotaError("Schalten fehlgeschlagen: Befehl unbekannt")
