"""
Sicherheits-PIN (4 Ziffern) fuer die Bedienung ueber das Dashboard - gemeinsam fuer Geraete (alle Smart-Home-Systeme) und eigene Schalter/Knoepfe.
Gespeichert wird nur ein Hash (PBKDF2 mit eigenem Salz). Reine Logik ohne Netzwerk.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re

PIN_RE = re.compile(r"^\d{4}$")
ITERATIONS = 60000


class PinError(ValueError):
    pass


def validate(pin) -> str:
    if not isinstance(pin, str) or not PIN_RE.match(pin):
        raise PinError("Die PIN muss aus genau 4 Ziffern bestehen")
    return pin


def _hash(pin: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, ITERATIONS).hex()


def make(pin: str) -> tuple[str, str]:
    """(salt_hex, hash_hex) fuer eine gueltige PIN."""
    salt = os.urandom(16)
    return salt.hex(), _hash(validate(pin), salt)


def verify(pin, salt_hex: str, hash_hex: str) -> bool:
    if not isinstance(pin, str) or not PIN_RE.match(pin):
        return False
    return hmac.compare_digest(_hash(pin, bytes.fromhex(salt_hex)), hash_hex)


def apply(item: dict, pin: str | None) -> None:
    """PIN am Eintrag setzen (4 Ziffern) bzw. mit leerem Wert entfernen."""
    if pin:
        item["pin_salt"], item["pin_hash"] = make(pin)
    else:
        item.pop("pin_salt", None)
        item.pop("pin_hash", None)


def check(item: dict, pin) -> bool:
    """True, wenn keine PIN eingerichtet ist oder die PIN stimmt."""
    if not item.get("pin_hash"):
        return True
    return verify(pin, item["pin_salt"], item["pin_hash"])


def strip(item: dict) -> dict:
    """Fuer die Anzeige: ohne Hash und Salz, mit pin_set."""
    out = {k: v for k, v in item.items() if k not in ("pin_hash", "pin_salt")}
    out["pin_set"] = bool(item.get("pin_hash"))
    return out
