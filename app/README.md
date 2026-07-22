# Victron Standalone Steuerung — App

Standalone-Ladesteuerung (kein ioBroker). Konzept: [[Victron Standalone Steuerung]]

## Schritt 1: Modbus-Verbindung zum Cerbo testen (jetzt)

Der Test läuft **nicht** hier in der Cloud, sondern bei dir im LAN — auf dem
Raspberry Pi oder einem PC, der den Cerbo erreicht.

1. Am Cerbo GX: **Einstellungen → Dienste → Modbus TCP → AN**.
2. Cerbo-IP herausfinden (Einstellungen → Ethernet/WLAN), z.B. `192.168.1.50`.
3. Auf dem Pi/PC:

   ```bash
   pip install pymodbus
   python3 cerbo_test.py --host 192.168.1.50
   ```

Erwartete Ausgabe: SOC-Wert(e) und der ESS-Mode werden gelesen. Der Test
schreibt **nichts** — reines Auslesen.

Wenn das klappt, ist die Grundlage bewiesen: die App kann den Cerbo direkt
ansprechen. Danach bauen wir Tibber-API, PV-Forecast, Logik-Portierung und
Web-UI drauf.

### Wenn ein Register nicht gelesen wird
Die Unit-IDs (225 für BMS, 100 für VE.Bus) können je nach Anlage abweichen.
Am Cerbo unter Modbus-TCP gibt es eine Geräteliste mit den echten Unit-IDs —
die tragen wir dann ein.

## Schritt 2: Runner im Dry-Run (Parallelvergleich)

Der Runner holt SOC (Cerbo), Preise (Tibber) und PV (forecast.solar), rechnet
mit der portierten V39.4-Logik und protokolliert die Entscheidung. Im
**Dry-Run schreibt er NICHTS** an den Cerbo — dein ioBroker steuert weiter.

```bash
pip install -r requirements.txt
python runner.py --once     # ein Durchlauf
python runner.py --loop     # alle 5 Min (poll_seconds)
```

Lass das ein paar Tage neben dem ioBroker laufen und vergleiche die
`Ziel`-ESS-Modes im Log mit dem, was das ioBroker-Skript tatsächlich schaltet.
Stimmen sie überein, ist die Portierung bestätigt.

## Scharfschalten (erst nach Vergleich!)

In `config.json` `"dry_run": false` setzen — oder einmalig `python runner.py --once --live`.
**Wichtig:** vorher im ioBroker das alte `victron_steuerung_v39.4.js` stoppen,
sonst schreiben zwei Regler gleichzeitig den ESS-Mode.

## Web-App (Dashboard, Wizard, Admin)

Statt des CLI-Runners gibt es jetzt die Web-App mit integriertem Regler:

```bash
pip install -r requirements.txt
python webapp.py
```

Dann im Browser `http://<host>:5005`:
- Beim ersten Start führt der **Einrichtungsassistent** (`/setup`) durch Cerbo-IP,
  Tibber-Token, Standort und Solarflächen — mit „Verbindung testen".
- **Dashboard**: SOC, Preis jetzt, ESS-Modus, Strategie, Preis-Kurve mit
  markierten Ladefenstern, Sofort-Override, E-Auto-Ladetermine.
- **Einstellungen** (`/admin`): alle Werte ändern, Dry-Run an/aus, Intervall.

Der Regler läuft als Hintergrund-Thread und schreibt im Dry-Run nichts an den Cerbo.

## Deployment (später)
- Als Webapp auf dem Proxmox-Server (LXC) für den internen Betrieb.
- Für die Weitergabe: GitHub + Installationsskript für den Raspberry Pi
  (systemd-Service, Autostart) — geplant, sobald der Code final ist.

## Dateien
- `config.json` – deine Zugangsdaten/Anlagenwerte (nicht im Git)
- `logic.py` – portierte V39.4-Entscheidungslogik (rein, testbar)
- `victron.py` – Cerbo Modbus (lesen/schreiben)
- `datasources.py` – Tibber + forecast.solar
- `store.py` – Config + State-Persistenz
- `runner.py` – der Scheduler/Regler
- `*_test.py` / `test_logic.py` – Einzeltests
