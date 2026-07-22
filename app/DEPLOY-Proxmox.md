# Victron Standalone Steuerung – Betrieb auf dem Webserver-LXC

Gleiches Muster wie **Tibbertimer** und **Chogan** auf demselben Server:
Code unter `/root`, eigenes venv, Prozessverwaltung über **pm2**.

Erreichbar danach unter `http://<container-ip>:5005`.

## Umgebung
- Host: `Webserver` (Debian 11, Python 3.9) – Code ist 3.9-kompatibel.
- Belegte Ports: 5000 (chogan), 5002 (tibbertimer), 3000 (bauhof-tank), 80/443/22/21/25.
- **Frei für uns: 5005.**

## 1. Code per Git holen (ermöglicht In-App-Updates)

Statt SFTP wird hier **geklont** – so funktioniert später der Update-Knopf in der App.

```bash
cd /root
git clone https://github.com/Micha-3582/Victron_Steuerung.git
```

Ergebnis: `/root/Victron_Steuerung/app/...`

## 2. Python-Umgebung

```bash
cd /root/Victron_Steuerung/app
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt
```

## 3. Vorprüfung

```bash
# Erreicht der Container den Cerbo? (Modbus TCP am Cerbo aktiv?)
./venv/bin/python cerbo_test.py --host 192.168.2.241
# -> SOC / ESS-Mode werden gelesen

# Entscheidungslogik gegen Testdaten (fasst nichts an):
./venv/bin/python test_logic.py
# -> Alle Tests bestanden.
```

## 4. Unter pm2 starten

```bash
cd /root/Victron_Steuerung/app
pm2 start ecosystem.config.js
pm2 save
pm2 list
```

`pm2 save` nicht vergessen – sonst ist die App nach einem Reboot weg.

## 5. Einrichten

Browser: **http://<container-ip>:5005** → der Einrichtungsassistent führt durch
Cerbo-IP, Tibber-Token, Standort und Solarflächen. Danach steht die Config in
`app/config.json` (bleibt bei Updates erhalten, ist nicht im Git).

**Wichtig:** Zum Start bleibt **Dry-Run AN** (Einstellungen → Betrieb). Erst nach
Parallelvergleich mit dem ioBroker-Skript scharfschalten – und dann das alte
`victron_steuerung_v39.4.js` im ioBroker stoppen, sonst schreiben zwei Regler
gleichzeitig den ESS-Mode.

## Update

- **Einfach:** in der App unter *Einstellungen → App-Version & Update* → „Nach Updates
  suchen" → „Jetzt aktualisieren". Die App beendet sich, **pm2 startet sie neu**.
- **Am Terminal:**
  ```bash
  cd /root/Victron_Steuerung && git pull --ff-only
  ./app/venv/bin/pip install -r app/requirements.txt   # nur bei neuen Abhängigkeiten
  pm2 restart victron-steuerung
  ```

Nutzerdaten (`config.json`, `state.json`, `energy.json`, `charge_log.json`,
`ev_schedules.json`) bleiben erhalten – sie sind nicht im Git.

## Zeitzone
`ecosystem.config.js` setzt `TZ: 'Europe/Berlin'` nur für diesen Prozess. Die
Logik selbst rechnet zeitzonen-robust (vergleicht immer gegen die eigene
lokale Zeit), die TZ sorgt nur für korrekte Anzeige der Uhrzeiten.

## Fehlersuche
| Symptom | Prüfung |
|---|---|
| Seite nicht erreichbar | `pm2 logs victron-steuerung`, Port 5005 frei? |
| „Cerbo nicht erreichbar" | Modbus TCP am Cerbo an? `cerbo_test.py --host …` aus dem Container |
| Update-Knopf sagt „kein Git" | Code wurde per SFTP statt `git clone` geholt |
| App nach Reboot weg | `pm2 save` vergessen |
| Zeiten verschoben | `TZ` in ecosystem.config.js gesetzt? danach `pm2 restart` |
