#!/usr/bin/env python3
"""Passwort zuruecksetzen, wenn der Zugang zur Web-App vergessen wurde.

Ueber die Web-Oberflaeche geht das absichtlich nicht (kein Login = kein
Zugriff auf die Einstellungen) - daher dieses Skript direkt auf dem
Geraet ausfuehren, auf dem die App laeuft (SSH oder direkt am Pi/Server).
Wer Zugriff auf dieses Skript hat, hat ohnehin schon Zugriff auf
config.json und alle Anlagendaten - das ist die gleiche Vertrauensebene.

Nutzung:
  python reset_password.py                          # fragt interaktiv ab
  python reset_password.py --password neuesPasswort1234
  python reset_password.py --user admin --password neuesPasswort1234
"""
import argparse
import getpass
import os
import sys

from auth import UserError, UserStore

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser(description="Passwort fuer die Victron-Steuerung-App zuruecksetzen")
    ap.add_argument("--user", help="Benutzername (bei nur einem Konto nicht noetig)")
    ap.add_argument("--password", help="Neues Passwort (sonst interaktive, versteckte Eingabe)")
    args = ap.parse_args()

    store = UserStore(os.path.join(BASE_DIR, "users.json"))

    if store.is_empty():
        print("Es existiert noch kein Konto. Einfach die Web-App oeffnen -")
        print("dort erscheint automatisch die Seite zum Anlegen eines Kontos.")
        return

    names = store.usernames()
    username = args.user
    if not username:
        if len(names) > 1:
            print("Mehrere Konten vorhanden, bitte mit --user angeben:", ", ".join(names))
            sys.exit(1)
        username = names[0]
    elif username not in names:
        print(f"Kein Konto mit dem Namen '{username}' gefunden. Vorhanden: {', '.join(names)}")
        sys.exit(1)

    password = args.password
    if not password:
        password = getpass.getpass(f"Neues Passwort fuer '{username}': ")
        password2 = getpass.getpass("Nochmal eingeben: ")
        if password != password2:
            print("Die Passwoerter stimmen nicht ueberein.")
            sys.exit(1)

    try:
        store.update_password(username, password)
    except UserError as e:
        print(f"Fehler: {e}")
        sys.exit(1)

    print(f"Passwort fuer '{username}' wurde geaendert.")


if __name__ == "__main__":
    main()
