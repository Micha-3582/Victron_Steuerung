# Victron Standalone Steuerung

Intelligente, webbasierte Ladesteuerung für Victron-ESS-Anlagen (MultiPlus-II / Cerbo GX)
auf Basis dynamischer **Tibber**-Strompreise und **PV-Prognose** – komplett eigenständig,
**ohne ioBroker**. Läuft auf einem Raspberry Pi und redet direkt per Modbus TCP mit dem Cerbo.

## Funktionen
- Automatische, preisoptimierte Netzladung (Peak-Schutz, Nacht-Puffer, Tiefpreis-Sicherung)
- Live-Ansicht aller Werte (Netz, Verbrauch, PV, Batterie, Tages-Netzbilanz)
- Strompreis-Kurve (heute/morgen) mit Ladeplan, manuelle Ladetermine per Klick/Ziehen
- Protokoll der Ladevorgänge mit Menge und Kosten
- Einrichtungsassistent + alle Anlagenparameter frei konfigurierbar
- In-App-Update über GitHub

## Installation (Raspberry Pi / Debian)

Ein Befehl – Repo-URL anpassen:

```bash
curl -fsSL https://raw.githubusercontent.com/Micha-3582/Victron_Steuerung/main/app/deploy/install.sh | bash -s -- https://github.com/Micha-3582/Victron_Steuerung.git
```

Danach im Browser `http://<pi-ip>:5005` öffnen – der Einrichtungsassistent führt durch
Cerbo-IP, Tibber-Token, Standort und Solarflächen.

**Voraussetzung am Cerbo:** Einstellungen → Dienste → **Modbus TCP** aktivieren.

## Aktualisieren
- **Einfach:** in der App unter *Einstellungen → App-Version & Update* auf „Nach Updates suchen" → „Jetzt aktualisieren".
- **Alternativ am Terminal:** `bash ~/victron-steuerung/app/deploy/update.sh`

Persönliche Daten (Konfiguration, Termine, Zählerstände) bleiben bei Updates erhalten.

## Lizenz / Haftung
Nutzung auf eigene Verantwortung. Die Software steuert Netzladung einer Batterieanlage –
vor dem Scharfschalten (Dry-Run aus) unbedingt im Parallelbetrieb prüfen.
