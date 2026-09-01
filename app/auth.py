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

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LEN = 8


class UserError(Exception):
    """Fachlicher Fehler, dessen Text direkt dem Nutzer gezeigt werden darf."""


def _norm(username: str) -> str:
    return (username or "").strip().lower()


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
        self._stamp = stamp

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

    def verify(self, username: str, password: str):
        """Gibt den Benutzer zurueck oder None. Aktualisiert last_login."""
        with self._lock:
            self._sync()
            user = self._users.get(_norm(username))
            if not user or not password:
                return None
            if not check_password_hash(user["pw_hash"], password):
                return None
            user["last_login"] = time.time()
            self._write()
            return dict(user)

    def create(self, username: str, password: str) -> dict:
        key = _norm(username)
        if not key:
            raise UserError("Benutzername darf nicht leer sein.")
        if len(password or "") < MIN_PASSWORD_LEN:
            raise UserError(f"Passwort muss mindestens {MIN_PASSWORD_LEN} Zeichen haben.")
        with self._lock:
            self._sync()
            if key in self._users:
                raise UserError("Benutzername ist bereits vergeben.")
            user = {
                "username": username.strip(),
                "pw_hash": generate_password_hash(password),
                "created": time.time(),
                "last_login": None,
            }
            self._users[key] = user
            self._write()
            return dict(user)

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

    def ensure_initial_user(self, username: str, data_dir: str) -> str | None:
        """Legt beim allerersten Start den einzigen Benutzer mit Zufallspasswort an.

        Gibt das Klartext-Passwort zurueck (nur dieses eine Mal), sonst None.
        """
        if not self.is_empty():
            return None
        password = secrets.token_urlsafe(12)
        self.create(username, password)
        note = os.path.join(data_dir, "initial-password.txt")
        try:
            with open(note, "w", encoding="utf-8") as fh:
                fh.write(
                    "Victron Steuerung -- Zugangsdaten beim Erststart\n"
                    f"Benutzer: {username}\n"
                    f"Passwort: {password}\n\n"
                    "Bitte nach der ersten Anmeldung unter Einstellungen das Passwort\n"
                    "aendern und diese Datei loeschen.\n"
                )
        except OSError:
            pass
        return password


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
