"""Benutzerverwaltung: Anmeldung, Passwort-Hashes.

- Passwoerter werden **nie** im Klartext gespeichert, nur als Hash
  (werkzeug/scrypt -- kommt mit Flask mit, keine Extra-Abhaengigkeit).
- Speicherung atomar mit Sperre.
- Beim allerersten Start wird ein Benutzer mit Zufallspasswort angelegt und
  dieses einmalig ins Log + nach initial-password.txt geschrieben.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time

from datetime import date

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LEN = 8
LEVELS = ("none", "read", "write")
_LEVEL_ORDER = {"none": 0, "read": 1, "write": 2}

# Rechte-Baukasten: pro Bereich einzeln "Kein Zugriff" / "Lesen" / "Schreiben".
# "Lesen" zeigt die Seite/Karte inkl. aller Werte (Zugangsdaten/Tokens ausgenommen -
# die bleiben ausschliesslich bei "Schreiben" sichtbar), "Schreiben" erlaubt Aendern.
AREAS = [
    ("dashboard", "Dashboard", "Live-Werte, Verlauf, Preis/Ladeplan. Schreiben = Geräte schalten, Sofort-laden-Override, Ladetermine anlegen."),
    ("rules", "Regeln", "Die Seite „Regeln“ (Wenn/Dann-Schaltungen)."),
    ("automation", "Automatik", "Überschuss-Automatik (Geräte bei PV-Überschuss zuschalten)."),
    ("settings_anlage", "Einstellungen: Anlage", "Cerbo-Zugang, Batteriekapazität, Inbetriebnahme-Datum. Die Cerbo-IP-Adresse ist nur bei „Schreiben“ im Klartext sichtbar."),
    ("settings_solar", "Einstellungen: Solar", "PV-Wechselrichter (einzeln erfasst)."),
    ("settings_tarif", "Einstellungen: Tarif & Laden", "Tibber/fester Tarif, Ladestrategie, Vertragskosten. Der Tibber-Zugangs-Token ist nur bei „Schreiben“ sichtbar."),
    ("settings_vrm", "Einstellungen: VRM", "Victron-VRM-Anbindung. Zugangsdaten nur bei „Schreiben“ sichtbar."),
    ("settings_wetter", "Einstellungen: Wetter", "Standort für die Wettervorhersage."),
    ("settings_meldungen", "Einstellungen: Meldungen", "Telegram-Benachrichtigungen. Bot-Token nur bei „Schreiben“ sichtbar."),
    ("settings_geraete", "Einstellungen: Geräte", "Shelly/Tuya/Tasmota-Geräteverwaltung. Zugangsdaten und die IP-Adressen der Geräte sind nur bei „Schreiben“ im Klartext sichtbar."),
    ("settings_anzeige", "Einstellungen: Dashboard-Kacheln", "Welche Kacheln das Dashboard zeigt und in welcher Reihenfolge."),
    ("settings_system", "Einstellungen: System", "Trockenlauf, Abfrage-Intervalle, App-Update."),
    ("account", "Eigenes Konto", "Eigenen Benutzernamen und eigenes Passwort ändern können. Bei „Kein Zugriff“ bleiben Name und Passwort fest (z.B. für einen geteilten Demo-Zugang)."),
    ("user_management", "Benutzerverwaltung", "Andere Zugänge anlegen/ändern/löschen. Mindestens ein Zugang muss „Schreiben“ behalten."),
]
AREA_IDS = [a[0] for a in AREAS]

PRESETS = {
    "admin": {a: "write" for a in AREA_IDS},
    "user": {a: ("write" if a in ("dashboard", "account") else "none") for a in AREA_IDS},
    "demo": {a: ("none" if a in ("user_management", "account") else "read") for a in AREA_IDS},
}


def normalize_permissions(perms: dict | None) -> dict:
    """Vervollstaendigt/bereinigt ein Rechte-Dict: unbekannte Bereiche raus, fehlende = 'none'."""
    perms = perms or {}
    return {a: (perms.get(a) if perms.get(a) in LEVELS else "none") for a in AREA_IDS}


# Dashboard-Kacheln, die sich zusaetzlich zur globalen Einstellung (Einstellungen ->
# Dashboard-Kacheln) PRO BENUTZER weiter einschraenken lassen - muss zu TILE_IDS in
# index.html bzw. TILE_DEFS in admin.html passen.
DASHBOARD_TILES = [
    ("show_live_values", "Live-Werte (Victron)", "Momentanwerte von Netz/Verbrauch/Solar/Batterie."),
    ("show_energy_chart", "Energie (Verlauf)", "Balken-Diagramm Verbrauch/Solar/Batterie-SOC."),
    ("show_flow_chart", "Energieflüsse", "Woher der Strom kam und wohin er ging."),
    ("show_week_overview", "Wochenrückblick", "Solar/Verbrauch/Netz/Autarkie/Kosten der letzten 7 Tage."),
    ("show_month_overview", "Monatsüberblick", "Gleiche Werte je Kalendermonat, dauerhaft archiviert."),
    ("show_tibber_card", "Tibber-Kachel", "Preis jetzt, ESS-Modus, Bilanz. Nur bei dynamischem Tarif verfügbar."),
    ("show_override_card", "Sofort laden (Override)", "Erzwingt Netzladen unabhängig vom Preis."),
    ("show_price_plan", "Strompreis & Ladeplan", "Preiskurve mit Ladeplan, manuelle Termine markieren."),
    ("show_charge_log", "Ladevorgänge (heute)", "Protokoll geplanter/durchgeführter Ladungen."),
    ("show_ev_card", "Manuelle Ladetermine", "Formular zum Anlegen eines Ladetermins."),
    ("show_shelly_card", "Geräte (Shelly/Tasmota/Tuya)", "Schalter für smarte Steckdosen auf dem Dashboard."),
    ("show_savings_card", "Ersparnis", "Was PV, Akku und Steuerung gegenüber „alles aus dem Netz“ sparen."),
    ("show_plansim_card", "Ladeplan-Simulation (Test)", "Vorschlag des EMS-Planers im Vergleich zur bisherigen Steuerung – steuert nichts. Nur bei dynamischem Tarif."),
    ("show_weather_card", "Wetter", "Wettervorhersage für den Standort (Standort im Reiter „Wetter“)."),
]
DASHBOARD_TILE_KEYS = [k for k, _, _ in DASHBOARD_TILES]


def normalize_tiles(tiles) -> list | None:
    """None = keine zusaetzliche Einschraenkung (zeigt, was die globale Einstellung erlaubt).
    Sonst eine Liste bekannter Kachel-Schluessel - unbekannte fallen raus."""
    if tiles is None:
        return None
    if not isinstance(tiles, list):
        return None
    keep = [k for k in tiles if k in DASHBOARD_TILE_KEYS]
    return keep if len(keep) < len(DASHBOARD_TILE_KEYS) else None    # alles angehakt = keine Einschraenkung


def normalize_tile_order(order) -> list | None:
    """None = Standardreihenfolge. Sonst eine vollstaendige Reihenfolge aller bekannten Kacheln -
    unbekannte Schluessel fallen raus, fehlende werden hinten angehaengt."""
    if not isinstance(order, list):
        return None
    known = [k for k in order if k in DASHBOARD_TILE_KEYS]
    result = known + [k for k in DASHBOARD_TILE_KEYS if k not in known]
    return None if result == DASHBOARD_TILE_KEYS else result


def has_level(perms: dict, area: str, need: str) -> bool:
    return _LEVEL_ORDER.get((perms or {}).get(area, "none"), 0) >= _LEVEL_ORDER[need]


def is_full_admin(perms: dict) -> bool:
    """Kann Benutzer verwalten - die einzige Faehigkeit, die nie ganz verschwinden darf
    (sonst kaeme niemand mehr an die Benutzerverwaltung heran)."""
    return (perms or {}).get("user_management") == "write"


class UserError(Exception):
    """Fachlicher Fehler, dessen Text direkt dem Nutzer gezeigt werden darf."""


def _norm(username: str) -> str:
    return (username or "").strip().lower()


def is_expired(user: dict) -> bool:
    """True, wenn der Zugang ein gesetztes Ablaufdatum hat und das heute schon vorbei ist.
    `expires` = "YYYY-MM-DD", gueltig bis einschliesslich diesem Tag (23:59)."""
    expires = user.get("expires")
    if not expires:
        return False
    try:
        return date.today().isoformat() > expires
    except Exception:                                    # noqa: BLE001
        return False


class UserStore:
    """Benutzerspeicher, der sich mit der Datei auf der Platte abgleicht."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._users: dict[str, dict] = {}
        self._stamp = None
        self._load()

    def _file_stamp(self):
        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _load(self) -> None:
        stamp = self._file_stamp()
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                self._users = data.get("users", {}) if isinstance(data, dict) else {}
            except Exception:
                self._users = {}
        else:
            self._users = {}
        for u in self._users.values():
            self._migrate(u)
        self._stamp = stamp

    @staticmethod
    def _migrate(user: dict) -> None:
        """Alte Datensaetze (nur `role`, oder noch aelter: gar nichts) auf das
        Rechte-Baukasten-Modell heben - einmalig beim Einlesen, ohne extra Migrationsschritt."""
        if "permissions" in user:
            user["permissions"] = normalize_permissions(user["permissions"])
            return
        role = user.pop("role", None) or "admin"
        user["permissions"] = dict(PRESETS.get(role, PRESETS["admin"]))

    def _sync(self) -> None:
        if self._file_stamp() != self._stamp:
            self._load()

    def _write(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"users": self._users}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)
        self._stamp = self._file_stamp()

    def get(self, username: str):
        with self._lock:
            self._sync()
            return self._users.get(_norm(username))

    def is_empty(self) -> bool:
        with self._lock:
            self._sync()
            return not self._users

    def usernames(self) -> list[str]:
        with self._lock:
            self._sync()
            return [u["username"] for u in self._users.values()]

    def list(self) -> list[dict]:
        """Alle Benutzer (ohne Passwort-Hash) fuer die Verwaltung im Adminbereich."""
        with self._lock:
            self._sync()
            out = []
            for u in self._users.values():
                d = dict(u)
                d.pop("pw_hash", None)
                d["permissions"] = normalize_permissions(d.get("permissions"))
                d["dashboard_tiles"] = normalize_tiles(d.get("dashboard_tiles"))
                out.append(d)
            return sorted(out, key=lambda d: d.get("created") or 0)

    def full_admin_count(self) -> int:
        with self._lock:
            self._sync()
            return self._full_admin_count_locked()

    def _full_admin_count_locked(self) -> int:
        """Wie full_admin_count(), aber ohne erneut den Lock zu holen (nur intern, Lock ist schon gehalten)."""
        return sum(1 for u in self._users.values() if is_full_admin(u.get("permissions")))

    def verify(self, username: str, password: str):
        """Gibt den Benutzer zurueck oder None. Aktualisiert last_login. Ein
        abgelaufener Zugang (siehe `expires`) wird wie ein falsches Passwort behandelt."""
        with self._lock:
            self._sync()
            user = self._users.get(_norm(username))
            if not user or not password:
                return None
            if not check_password_hash(user["pw_hash"], password):
                return None
            if is_expired(user):
                return None
            user["last_login"] = time.time()
            self._write()
            return dict(user)

    def create(self, username: str, password: str, permissions: dict | None = None,
               expires: str | None = None, dashboard_tiles=None) -> dict:
        key = _norm(username)
        if not key:
            raise UserError("Benutzername darf nicht leer sein.")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben.")
        perms = normalize_permissions(permissions if permissions is not None else PRESETS["admin"])
        if is_full_admin(perms):
            expires = None                            # Volladmins laufen nie ab
        with self._lock:
            self._sync()
            if key in self._users:
                raise UserError("Benutzername ist bereits vergeben.")
            user = {
                "username": username.strip(),
                "pw_hash": generate_password_hash(password),
                "created": time.time(),
                "last_login": None,
                "permissions": perms,
                "expires": expires or None,          # "YYYY-MM-DD" oder None (unbegrenzt)
                "dashboard_tiles": normalize_tiles(dashboard_tiles),
            }
            self._users[key] = user
            self._write()
            return dict(user)

    def set_dashboard_tiles(self, username: str, tiles) -> dict:
        """Admin-Obergrenze: was dieses Konto ueberhaupt sehen KANN (siehe Weitere Benutzer)."""
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            user["dashboard_tiles"] = normalize_tiles(tiles)
            self._write()
            return dict(user)

    def set_my_tiles(self, username: str, tiles) -> dict:
        """Eigene Wahl des Kontos selbst ("Meine Ansicht") - schraenkt innerhalb der Admin-
        Obergrenze (dashboard_tiles) weiter ein, kann sie aber nie ueberschreiben/erweitern."""
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            user["my_tiles"] = normalize_tiles(tiles)
            self._write()
            return dict(user)

    def set_my_order(self, username: str, order) -> dict:
        """Eigene Kachel-Reihenfolge des Kontos ("Meine Ansicht")."""
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            user["my_order"] = normalize_tile_order(order)
            self._write()
            return dict(user)

    def set_permissions(self, username: str, permissions: dict) -> dict:
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            new_perms = normalize_permissions(permissions)
            was_full_admin = is_full_admin(user.get("permissions"))
            if was_full_admin and not is_full_admin(new_perms) and self._full_admin_count_locked() <= 1:
                raise UserError("Mindestens ein Zugang muss die Benutzerverwaltung („Schreiben“) behalten.")
            user["permissions"] = new_perms
            if is_full_admin(new_perms):
                user["expires"] = None                # Volladmins laufen nie ab
            self._write()
            return dict(user)

    def set_expires(self, username: str, expires: str | None) -> dict:
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            if expires and is_full_admin(user.get("permissions")):
                raise UserError("Ein Zugang mit Benutzerverwaltung („Schreiben“) kann nicht ablaufen.")
            user["expires"] = expires or None
            self._write()
            return dict(user)

    def delete(self, username: str) -> None:
        key = _norm(username)
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            if is_full_admin(user.get("permissions")) and self._full_admin_count_locked() <= 1:
                raise UserError("Der letzte Zugang mit Benutzerverwaltung kann nicht gelöscht werden.")
            del self._users[key]
            self._write()

    def update_password(self, username: str, password: str) -> dict:
        key = _norm(username)
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben.")
        with self._lock:
            self._sync()
            user = self._users.get(key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            user["pw_hash"] = generate_password_hash(password)
            self._write()
            return dict(user)

    def rename(self, old_username: str, new_username: str) -> dict:
        """Aendert den Benutzernamen (Passwort-Hash bleibt erhalten)."""
        old_key = _norm(old_username)
        new_key = _norm(new_username)
        if not new_key:
            raise UserError("Benutzername darf nicht leer sein.")
        with self._lock:
            self._sync()
            user = self._users.get(old_key)
            if not user:
                raise UserError("Benutzer nicht gefunden.")
            if new_key != old_key and new_key in self._users:
                raise UserError("Benutzername ist bereits vergeben.")
            user["username"] = new_username.strip()
            if new_key != old_key:
                del self._users[old_key]
                self._users[new_key] = user
            self._write()
            return dict(user)



def new_secret_key(path: str) -> bytes:
    """Signaturschluessel fuer Sitzungs-Cookies -- muss Neustarts ueberleben,
    sonst wird bei jedem Restart jeder abgemeldet."""
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                key = fh.read().strip()
            if len(key) >= 32:
                return key
        except OSError:
            pass
    key = secrets.token_hex(32).encode()
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(key)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key
