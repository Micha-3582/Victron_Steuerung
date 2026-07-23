# Victron Standalone Steuerung

Intelligente, webbasierte Ladesteuerung für Victron-ESS-Anlagen (MultiPlus-II / Cerbo GX)
auf Basis dynamischer **Tibber**-Strompreise und **PV-Prognose** – komplett eigenständig,
**ohne ioBroker, Node-RED oder Cloud**. Läuft auf einem Raspberry Pi (oder jedem
Linux-Rechner im Netz) und redet direkt per **Modbus TCP** mit dem Cerbo GX.

Ziel: **PV maximal nutzen, Netzstrom nur in den günstigsten Viertelstunden ziehen** und
die Morgen-/Abend-Preisspitzen niemals aus dem Netz decken – vollautomatisch, aber
jederzeit manuell übersteuerbar.

---

## Inhalt

- [Wie es funktioniert](#wie-es-funktioniert)
- [Die Lade-Strategie im Detail](#die-lade-strategie-im-detail)
- [Funktionen der Web-App](#funktionen-der-web-app)
- [Sicherheitsmechanismen](#sicherheitsmechanismen)
- [Datenquellen & Register](#datenquellen--register)
- [Installation](#installation)
- [Einrichtung](#einrichtung)
- [Konfigurierbare Parameter](#konfigurierbare-parameter)
- [Architektur](#architektur)
- [Aktualisieren](#aktualisieren)
- [Lizenz / Haftung](#lizenz--haftung)

---

## Wie es funktioniert

Ein Hintergrund-Regler läuft in einer Schleife und macht bei jedem Takt (Standard alle
5 Min, auf das Viertelstunden-Raster ausgerichtet) folgendes:

1. **Liest den Ist-Zustand** vom Cerbo per Modbus TCP: Batterie-SOC (aus dem BMS),
   aktuellen ESS-Mode und die kompletten System-Leistungen (Netz, Verbrauch, PV-AC,
   PV-DC, Batterie).
2. **Holt die Strompreise** von der Tibber-API – viertelstundengenau für heute und
   (sobald verfügbar) morgen.
3. **Holt die PV-Prognose** von forecast.solar für die konfigurierten Solarflächen.
4. **Entscheidet** anhand der Strategie unten, ob jetzt aus dem Netz geladen werden soll.
5. **Schreibt den ESS-Mode** zurück auf den Cerbo (`9` = Netzladen erlaubt, `10` =
   nicht laden) – sofern kein Dry-Run aktiv ist und sich der Wert geändert hat.

Ein **zweiter, feiner Takt** (Standard alle 10 s) tastet unabhängig davon die
Momentanleistungen ab und integriert sie zu kWh – deutlich genauer als der Regeltakt und
sehr nah an den VRM-Werten. Daraus entstehen Verlaufs-Charts und das Ladeprotokoll mit
Menge und Kosten.

---

## Die Lade-Strategie im Detail

Die Logik ist eine 1:1-Portierung des über viele Iterationen erprobten ioBroker-Skripts
(V39.4) nach Python. Sie arbeitet in drei Schritten und einem klaren Prioritätsbaum.

### Schritt 1 – Bedarf ermitteln (Prioritätsbaum)

Es wird die **erste zutreffende** Strategie gewählt:

1. **Peak-Schutz** (harte Bedingung, höchste Priorität)
   Rechnet aus, ob der Akku den nächsten Preis-Peak (morgens 7–9, abends 19–21 Uhr) mit
   einem Mindest-SOC von **40 %** übersteht. Die bis dahin erwartete PV-Erzeugung wird
   abgezogen, der Verbrauch bis zum Peak addiert. Reicht es nicht, wird sofort nachgeladen
   – bis zu einem Preislimit von **37 ct/kWh**.

2. **Nacht-Puffer** (22:00–06:00, mit Hysterese)
   Fällt der SOC nachts unter die Sicherheitsschwelle (30 %) *und* ist die Gesamtbilanz
   negativ, wird bis knapp über die Schwelle nachgeladen. Eine Hysterese (±1,5 %)
   verhindert Schaltflattern an der Grenze.

3. **Morgen-Brücke** (00:00–09:00)
   Sorgt dafür, dass am Morgen ein Ziel-SOC von **35 %** vorhanden ist, um bis zur
   Mittags-PV zu überbrücken.

4. **Tiefpreis-Sicherung** (jederzeit)
   Ist die **Gesamtbilanz bis morgen Mittag** negativ (aktueller Speicher + erwartete
   Rest-PV − Verbrauch), wird die Differenz in den günstigsten Viertelstunden nachgeladen.

### Schritt 2 – Planung

Der ermittelte Bedarf (kWh) wird auf die nötige Anzahl 15-Min-Slots umgerechnet
(anhand der Ladeleistung). Aus den erlaubten Slots im jeweiligen Zeitfenster werden die
**preisgünstigsten** ausgewählt. Der Ladebedarf wird zusätzlich durch die freie
Restkapazität begrenzt (abzüglich einer PV-Reserve für die Mittagssonne).

### Schritt 3 – Ausführung

- Geladen wird nur, wenn die **aktuelle Viertelstunde im Plan** liegt.
- **Preislimit** für die Standard-Strategien: nur in der günstigen Hälfte des Fensters
  laden (Nacht-Puffer: bis Ø-Preis ×1,1; sonst: unteres Viertel der Preisspanne).
- **Commitment:** Eine einmal begonnene Viertelstunde wird zu Ende geladen (kein
  Sekunden-Flattern beim Schalten).
- **Notbremse (mit Hysterese):** Fällt der SOC unter 30 % *und* liegt der Preis
  ≤ 37 ct, wird unabhängig von der Strategie geladen, bis 40 % wieder erreicht sind.

### PV-Berücksichtigung

Die Prognose wird mit einem **Korrekturfaktor** (Erfahrungswert real vs. Prognose,
Standard 0,68) gedämpft und je nach Tageszeit anteilig auf die verbleibende
Erzeugung umgerechnet. Viel Sonne ⇒ weniger geplante Netzladung.

---

## Funktionen der Web-App

Mobile-optimierte Oberfläche, die den kompletten Zustand zeigt und Eingriffe erlaubt:

- **Live-Dashboard** im Victron-Stil: SOC, ESS-Mode, Netz/Verbrauch/PV/Batterie als
  Kacheln, Tages-Netzbilanz (Bezug/Einspeisung).
- **Strompreis-Kurve** (heute/morgen) mit den geplanten Ladefenstern eingezeichnet.
- **Manuelle Ladetermine** per Klick/Ziehen in der Kurve – z. B. für gezieltes
  E-Auto-Laden; überschreibt die Automatik für den gewählten Zeitraum.
- **Manueller Override**: sofortiges Erzwingen von „jetzt laden" per Schalter.
- **Ladeprotokoll**: alle Ladevorgänge (geplant / läuft / geladen) mit Menge (kWh),
  Kosten (€) und dauergewichtetem Ø-Preis – korrekt auch bei Fenstern, die nicht auf dem
  15-Min-Raster beginnen.
- **Energie-Charts**: Verlauf (Verbrauch/Solar als Balken + SOC-Band) und Energieflüsse
  (7 Pfade wie in VRM, symmetrisch um 0), 15-Min-Buckets, 35 Tage Historie mit
  Tages-Navigation.
- **Einrichtungsassistent** beim ersten Start (Cerbo-IP, Tibber-Token, Standort,
  Solarflächen) mit Verbindungstest für Cerbo und Tibber.
- **Admin-Bereich**: alle Anlagen- und Strategie-Parameter frei konfigurierbar, mit
  Hilfe-Boxen.
- **In-App-Update** über GitHub (Versionsprüfung + Ein-Klick-Update, Daten bleiben
  erhalten).

---

## Sicherheitsmechanismen

- **Dry-Run-Modus** (Standard beim ersten Start): Die App rechnet und protokolliert alles,
  schreibt aber **nichts** auf den Cerbo. Ideal zum gefahrlosen Parallelbetrieb, bevor man
  scharf schaltet.
- **Harte Ladesperre:** Es wird **nie über 90 % SOC** aus dem Netz geladen – auch nicht
  bei manuellem Override oder aktivem Ladetermin.
- **Preislimits** verhindern Netzladung zu teuren Zeiten außerhalb echter Peak-Not.
- **Fehlerabfang:** Fällt eine Datenquelle aus (Tibber, PV, Cerbo), läuft der Regler
  weiter und rechnet defensiv (z. B. PV = 0 kWh statt Absturz).

---

## Datenquellen & Register

| Quelle | Was | Wie |
|---|---|---|
| Cerbo GX | SOC, ESS-Mode, System-Leistungen, Netz-Energiezähler | Modbus TCP (Unit 100 System, Unit 225 BMS) |
| Tibber | Strompreise heute/morgen (viertelstundengenau) | Tibber-API (Token) |
| forecast.solar | PV-Prognose je Solarfläche | HTTP (mit Backoff + Cache gegen Rate-Limit) |

**Genutzte Cerbo-Register** (verifiziert gegen die Victron-App):
SOC BMS `225/266` (×10), ESS-Mode `100/2900` (Holding, 9/10), System-Block ab `100/811`
(PV-WR, Last, Netz), Batterie `100/840–846`, PV-Ladegerät `100/850–851`,
Netz-Energiezähler `100/2622ff` (uint32, Wh).

> **Hinweis:** Die kumulierten Netz-Zählerregister (2622ff) sind bei manchen Anlagen
> unzuverlässig. Die Tages-Netzbilanz wird deshalb standardmäßig aus der **integrierten
> Netzleistung** berechnet und ist manuell korrigierbar (Abgleich mit der Victron-App).

---

## Installation

Raspberry Pi / Debian – ein Befehl:

```bash
curl -fsSL https://raw.githubusercontent.com/Micha-3582/Victron_Steuerung/main/app/deploy/install.sh | bash -s -- https://github.com/Micha-3582/Victron_Steuerung.git
```

Danach im Browser `http://<host-ip>:5005` öffnen.

**Voraussetzung am Cerbo:** Einstellungen → Dienste → **Modbus TCP aktivieren**.

**Manuell / eigene Umgebung:**

```bash
pip install -r app/requirements.txt
python app/webapp.py     # http://<host>:5005
```

Läuft mit Python 3.9+ (u. a. auf dem Cerbo-nahen LXC/Pi getestet).

---

## Einrichtung

Der Assistent (`/setup`) führt durch:

1. **Cerbo-IP** (und Port, Standard 502)
2. **Tibber-Token** (aus dem Tibber-Entwicklerportal)
3. **Standort** (Breiten-/Längengrad für die PV-Prognose)
4. **Solarflächen** (Ausrichtung, Neigung, kWp je Fläche)

Ein Verbindungstest prüft Cerbo und Tibber, bevor es losgeht. Danach zunächst im
**Dry-Run** beobachten – erst wenn die Entscheidungen plausibel sind, in den
Einstellungen scharf schalten.

---

## Konfigurierbare Parameter

Alle Strategie-Werte lassen sich pro Anlage im Admin-Bereich anpassen. Defaults:

| Parameter | Standard | Bedeutung |
|---|---|---|
| `battery_usable_kwh` | 24,0 | nutzbare Akkukapazität |
| `daily_usage_kwh` | 30,0 | angenommener Tagesverbrauch |
| `charge_power_w` | 3500 | Netz-Ladeleistung |
| `pv_reserve_kwh` | 5,0 | Kapazitätspuffer für Mittags-PV |
| `pv_korrektur_faktor` | 0,68 | Dämpfung Prognose → real |
| `pv_tom_morning_factor` | 0,15 | Anteil morgiger PV vor dem Morgen-Peak (Winter 0,05 / Sommer 0,25) |
| `min_peak_soc` | 40 % | Mindest-SOC vor jedem Peak |
| `peak_avoid_price` | 37 ct | Preislimit für Peak-Schutz / Notbremse |
| `night_safety_soc` | 30 % | Sicherheits-SOC nachts |
| `target_safe_soc` | 35 % | Ziel-SOC der Morgen-Brücke |
| `max_charge_soc` | 90 % | harte Obergrenze für Netzladung |
| `hysterese_soc` | 1,5 % | Schalthysterese |
| `poll_seconds` | 300 | Regeltakt (aufs 15-Min-Raster ausgerichtet) |
| `energy_sample_seconds` | 10 | Takt des Energie-Samplers |

Peak-Zeitfenster (morgens 7–9, abends 19–21 Uhr) sind ebenfalls einstellbar.

---

## Architektur

```
webapp.py       Flask-Web-App + zwei Hintergrund-Threads (Regler + Energie-Sampler)
logic.py        reine, testbare Entscheidungslogik (decide()) – keine Hardware/IO
victron.py      Cerbo-Anbindung über Modbus TCP (Lesen/Schreiben)
datasources.py  Tibber-Preise + forecast.solar-Prognose
store.py        Persistenz: Config, State, Ladeprotokoll, Energie-Historie, Netzbilanz
updater.py      In-App-Update über GitHub
templates/      index (Dashboard), setup (Wizard), admin, base
deploy/         install.sh / update.sh
```

Die Trennung von **Logik** (rein, deterministisch, testbar) und **I/O** (Modbus, HTTP,
Speicherung) macht die Steuerung ohne Hardware testbar – die V39.4-Portierung ist durch
Tests abgesichert.

---

## Aktualisieren

- **Einfach:** in der App unter *Einstellungen → App-Version & Update* auf „Nach Updates
  suchen" → „Jetzt aktualisieren".
- **Am Terminal:** `bash ~/victron-steuerung/app/deploy/update.sh`

Persönliche Daten (Konfiguration, Termine, Zählerstände, Historie) bleiben bei Updates
erhalten.

---

## Lizenz / Haftung

Nutzung auf **eigene Verantwortung**. Die Software steuert die Netzladung einer
Batterieanlage – vor dem Scharfschalten (Dry-Run aus) unbedingt im Parallelbetrieb prüfen.
Keine Gewähr für Preis-, Prognose- oder Messdaten Dritter (Tibber, forecast.solar,
Victron-Register).
