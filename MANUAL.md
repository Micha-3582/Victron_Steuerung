# Handbuch – Victron Steuerung

Dieses Handbuch erklärt die komplette Software: was sie kann, wo man was einstellt und wie man Geräte, Regeln und Abläufe baut.
Die technische Beschreibung der Lade-Strategie, Installation und Architektur steht in der [README](README.md).

> **Hinweis zum Stand:** Das Handbuch beschreibt den Funktionsumfang der aktuellen Version. Die Oberfläche ist deutsch und für das Handy optimiert.
> Fast jedes Eingabefeld hat ein kleines **?** – darauf tippen zeigt eine kurze Erklärung.

## Inhalt

1. [Überblick](#1-überblick)
2. [Anmelden, Benutzer und Rechte](#2-anmelden-benutzer-und-rechte)
3. [Das Dashboard](#3-das-dashboard)
4. [Einstellungen](#4-einstellungen)
5. [Smart Home: Geräte einrichten](#5-smart-home-geräte-einrichten)
6. [Eigene Schalter & Knöpfe (Software)](#6-eigene-schalter--knöpfe-software)
7. [Sensoren, Thermostate und Türschlösser](#7-sensoren-thermostate-und-türschlösser)
8. [PIN-Schutz](#8-pin-schutz)
9. [Automatik (Überschuss)](#9-automatik-überschuss)
10. [Regeln und Abläufe (Regel-Editor)](#10-regeln-und-abläufe-regel-editor)
11. [Wake-on-LAN](#11-wake-on-lan)
12. [Logbuch, Betriebsbericht, Solarlogbuch, Watchdog](#12-logbuch-betriebsbericht-solarlogbuch-watchdog)
13. [Benachrichtigungen (Telegram)](#13-benachrichtigungen-telegram)
14. [Kosten, Tarife und Tarifwechsel](#14-kosten-tarife-und-tarifwechsel)
15. [Update, Sicherung, Datenhaltung](#15-update-sicherung-datenhaltung)
16. [Beispiele](#16-beispiele)
17. [Fehlersuche (FAQ)](#17-fehlersuche-faq)

---

## 1. Überblick

Die Software besteht aus drei Teilen, die zusammenarbeiten:

| Teil | Aufgabe |
|---|---|
| **Ladesteuerung** | Liest Akku, Netz, PV und Verbrauch vom Victron Cerbo GX (Modbus TCP), holt Strompreise und PV-Prognose und entscheidet, wann aus dem Netz geladen wird. |
| **Smart Home** | Schaltet Steckdosen, Lichter usw. (Shelly, Tasmota, Tuya, Homematic, Zigbee), liest Sensoren, setzt Thermostate, steuert Türschlösser und weckt Rechner. |
| **Regeln & Automatik** | Verknüpft alles: „WENN … DANN … SONST …“, Zeitpläne, Überschuss-Schaltung, Abläufe mit Warten und Telegram-Nachricht. |

Die Seiten oben in der Kopfzeile: **Dashboard**, **Automatik**, **Regeln**, **Einstellungen**. Welche davon du siehst, hängt von deinen Rechten ab.

**Grundprinzipien**

- **Alles läuft lokal** im Heimnetz. Cloud wird nur für Tibber-Preise, die VRM-Prognose, Wetter (Open-Meteo) und Telegram gebraucht.
- **Dry-Run / Trockenlauf:** Die Ladesteuerung und die Regeln haben je einen Trockenlauf-Schalter. Dabei wird alles berechnet und protokolliert, aber nichts wirklich geschaltet. Ideal zum Ausprobieren.
- **Sicherheitsgrenzen:** Es wird nie über das Ladelimit aus dem Netz geladen (Standard 90 %), egal ob Automatik, Ladetermin oder Sofort-Override.
- **Die App ist installierbar** (PWA): Im Handy-Browser „Zum Startbildschirm hinzufügen“.

---

## 2. Anmelden, Benutzer und Rechte

Beim ersten Start legt man einen Zugang an (`/create-account`) bzw. meldet sich mit dem Admin-Konto an. Danach werden weitere Benutzer unter **Einstellungen → System → Weitere Benutzer** angelegt.

**Rechte pro Bereich.** Jeder Benutzer bekommt für jeden Bereich einzeln **Kein Zugriff**, **Lesen** oder **Schreiben**:

| Bereich | Bedeutung |
|---|---|
| Dashboard | Live-Werte, Verlauf, Preis/Ladeplan. Schreiben = Geräte schalten, Sofort-Laden, Ladetermine. |
| Regeln | Die Seite „Regeln“. |
| Automatik | Überschuss-Automatik. |
| Einstellungen: Anlage | Cerbo-Zugang, Akku-Daten. |
| Einstellungen: Solar | PV-Wechselrichter. |
| Einstellungen: Tarif & Laden | Tarif, Ladestrategie, Vertragskosten. |
| Einstellungen: VRM | VRM-Anbindung. |
| Einstellungen: Wetter | Standort für das Wetter. |
| Einstellungen: Meldungen | Telegram. |
| Einstellungen: Smart Home | Geräte, Sensoren, eigene Schalter. |
| Einstellungen: Dashboard-Kacheln | Welche Kacheln sichtbar sind. |
| Einstellungen: System | Trockenlauf, Intervalle, Update. |
| Konto | Eigenen Namen/Passwort ändern. |
| Benutzerverwaltung | Nur für Admins. |

- **Vorlagen:** „Admin“ (alles), „Benutzer“ (nur Dashboard und eigenes Konto), „Demo“ (alles nur lesen). Sie füllen nur vor – danach ist jedes Feld einzeln änderbar.
- **Lesen** zeigt Seite und Werte; Zugangsdaten (Tokens, Passwörter, Geräte-IPs) sind nur bei **Schreiben** im Klartext sichtbar.
- **Ablauf:** Ein Benutzer kann mit Ablaufdatum angelegt werden (z. B. Gastzugang).
- **Meine Ansicht** (Einstellungen): Jeder wählt für sich, welche freigegebenen Dashboard-Kacheln er sieht und in welcher Reihenfolge – wirkt sich auf niemand sonst aus.

---

## 3. Das Dashboard

Das Dashboard zeigt den Zustand der Anlage und erlaubt Eingriffe. Die Live-Kacheln aktualisieren sich alle 2 Sekunden. Welche Kacheln erscheinen, legen Admin (global) und jeder Benutzer (**Meine Ansicht**) fest.

| Kachel | Inhalt |
|---|---|
| **Live-Werte (Victron)** | Momentanwerte: Netz, Verbrauch, Solar, Batterie, Ladestand (SOC). |
| **Energie (Verlauf)** | Balkendiagramm Verbrauch/Solar mit SOC-Band, 35 Tage Historie mit Tagesnavigation. Auflösung 15 Min oder stündlich (Einstellungen → Diagramme). |
| **Energieflüsse** | Woher der Strom kam und wohin er ging (7 Pfade wie im VRM). |
| **Wochenrückblick** | Solar, Verbrauch, Netz, Autarkie und Kosten der letzten 7 Tage. |
| **Monatsüberblick** | Dieselben Werte je Kalendermonat, dauerhaft archiviert. |
| **Tibber-Kachel** | Aktueller Preis, ESS-Modus, Tagesbilanz (nur bei dynamischem Tarif). |
| **Sofort laden (Override)** | Erzwingt Netzladen unabhängig vom Preis (bis zum Ladelimit). |
| **Strompreis & Ladeplan** | Preiskurve heute/morgen mit eingezeichneten Ladefenstern. Manuelle Termine lassen sich per Klick/Ziehen markieren. |
| **Ladevorgänge (heute)** | Protokoll: geplant / läuft / geladen, mit kWh, Kosten und Ø-Preis. |
| **Manuelle Ladetermine** | Formular für einen festen Ladezeitraum (z. B. E-Auto), überschreibt die Automatik für diesen Zeitraum. |
| **Geräte (Smart Home)** | Schalter für Steckdosen, Lichter und andere Aktoren. |
| **Eigene Schalter & Aktionen (Software)** | Selbst angelegte Schalter und Knöpfe sowie „Rechner aufwecken“. |
| **Sensoren (Smart Home)** | Fenster/Tür, Bewegung, Temperatur usw. – grün/rot bzw. Messwert. |
| **Ersparnis** | Was PV, Akku und Steuerung gegenüber „alles aus dem Netz“ sparen. |
| **Ladeplan-Simulation (Test)** | Vorschlag des EMS-Planers im Vergleich – steuert nichts. |
| **Wetter** | Vorhersage für den Standort (nur Anzeige). |

**Kacheln bedienen**

- **Schalten:** Auf die Gerätekachel tippen. Ist ein **PIN** gesetzt, erscheint zuerst ein Ziffernblock (siehe [PIN-Schutz](#8-pin-schutz)).
- **Reihenfolge ändern:** Kachel **lang drücken** und verschieben (Geräte, Sensoren, Schalter & Aktionen).
- **Eigene Schalter/Knöpfe:** Tippen schaltet bzw. drückt. Läuft ein Ablauf mit Timer, zeigt die Kachel die **Restzeit**; nochmal tippen bricht ab (bei Schaltern).
- **Sensoren** sind nur Anzeige. Rot/Grün hängt davon ab, wie der Sensor eingestellt ist (siehe [Sensoren](#7-sensoren-thermostate-und-türschlösser)).

---

## 4. Einstellungen

Die Einstellungen sind in Reiter gegliedert (Anlage, Solar, Tarif & Laden, VRM, Wetter, Meldungen, Smart Home, Dashboard-Kacheln, System). Geänderte Werte gelten erst nach **Speichern**. Einige Werte (Mindest-SOC, Sollwert Netz) werden dagegen **direkt am Cerbo** gesetzt.

### 4.1 Betrieb und System

- **Anzeigename:** Steht in der Kopfzeile und im Browser-Tab (praktisch bei mehreren Anlagen).
- **Dry-Run:** An = die Ladesteuerung rechnet und protokolliert nur, am Cerbo wird nichts geschaltet. Für den echten Betrieb ausschalten. Läuft parallel noch eine andere Steuerung (z. B. ioBroker), die den ESS-Modus setzt, diese vorher stoppen.
- **Regel-Intervall:** Wie oft die Ladesteuerung neu rechnet (Standard 300 s, ausgerichtet aufs Viertelstunden-Raster). Strompreise ändern sich nur viertelstündlich, daher reicht das.
- **Energie-Messtakt:** Wie oft Solar/Verbrauch/Netz/Akku für Verlauf und Tagesbilanz gemessen werden (Standard 10 s).
- **Diagramme:** „Energie (Verlauf) stündlich“ und „Energieflüsse stündlich“ fassen vier Viertelstunden wie im VRM zusammen.

### 4.2 Anlage (Cerbo GX)

- **Cerbo-IP und Port** (Standard 502). Am Cerbo muss **Modbus TCP** aktiviert sein: *Einstellungen → Dienste → Modbus TCP*.
- Der Button **Verbindung testen** prüft den Zugang.

### 4.3 Anlage & Speicher

| Feld | Bedeutung |
|---|---|
| Nutzbare Akkukapazität (kWh) | Real nutzbare Größe, nicht Nennkapazität (ca. 95 % davon). |
| Akku in Betrieb seit | Für die Vollzyklen-Berechnung im Watchdog. |
| Erwartete Zyklenzahl | Herstellerangabe (z. B. 6000), für die Lebensdauer-Hochrechnung. |
| Tagesverbrauch (kWh) | Durchschnitt pro Tag – Basis für die Planung. |
| Ladestrom (A) / Systemspannung (V) | Daraus errechnet sich die Ladeleistung beim Netzladen. |
| PV-Reserve (kWh) | Platz, der beim Netzladen für die Mittagssonne frei bleibt. |
| Ladelimit / max. SOC (%) | Harte Obergrenze für Netzladen – gilt immer. |
| **Minimaler Akkustand am Cerbo (%)** | Wie „Minimaler SOC“ im VRM; darunter entlädt der Akku nicht. Wird **direkt am Cerbo** gesetzt. |
| **Sollwert Netz am Cerbo (W)** | ESS-Netz-Sollwert (−1000 … 1000 W in 10er-Schritten). 0 = möglichst kein Bezug/Einspeisung. Wird **direkt am Cerbo** gesetzt. |
| Immer laden unter (ct/kWh) | Fällt der Preis auf/unter diesen Wert, wird geladen (0 = aus). |

**Periodische Vollladung:** Alle X Tage wird das Ladelimit auf ein Ziel (meist 100 %) angehoben, damit das BMS die Zellen balancieren kann. *Wann* geladen wird, entscheidet weiterhin die Planung – nur bei günstigem Preis bzw. genug Sonne. 0 = aus.

### 4.4 Stromtarif und Ladestrategie

- **Dynamischer Tarif (Tibber):** Access-Token von developer.tibber.com eintragen. Die Steuerung lädt in den günstigsten Viertelstunden.
- **Fester Preis:** Preis pro kWh (brutto) eintragen. Es gibt dann **keine Preisplanung**, die App lädt nie aktiv aus dem Netz. PV-Vorrang, Ladelimit und Sofort-Override wirken weiter.
- **Intelligente Planung** (Standard bei dynamischem Tarif): rechnet bei jedem Durchlauf frisch über den ganzen bekannten Zeitraum, wann Netzladen am günstigsten ist. Sie ersetzt Peak-Schutz, SOC-Strategie und Günstig-Vorkauf. *Sicherheitspuffer beim Nachtladen* lässt sie vorsichtiger planen.
- **Klassische Strategien** (nur wenn die Intelligente Planung aus ist): Peak-Schutz (Morgen-/Abend-Peak, Mindest-SOC, Notbrems-Preislimit, optional Günstig-Vorkauf) und SOC-Strategie (Nacht-Sicherheits-SOC, Ziel-SOC Morgen-Brücke, Hysterese). Details siehe README.
- **PV-Prognose:** Kommt vom VRM; Morgen-Faktor (Winter ≈ 0,05, Sommer ≈ 0,25) und optional **automatische Anpassung** an die Wirklichkeit (benötigt ≥ 5 Tage Solarlogbuch, Faktor 0,6–1,1). Ohne VRM rechnet die App mit dem Durchschnitt der letzten Tageserträge.
- **Netz-Zähler heute korrigieren:** Nur nötig, wenn die App über Mitternacht aus war – dann die Tageswerte aus der Victron-App eintragen.

### 4.5 VRM

- **Installations-ID** und **Zugriffstoken** aus dem VRM-Portal (*Präferenzen → Integrationen → Zugangs-Token*). Der Token wird nur einmal angezeigt.
- **Verlauf nachholen:** Fehlen Zeiträume (nach Ausfall), holt die App bis zu 35 Tage (oder länger, in 7-Tage-Häppchen) aus dem VRM. Erst Vorschau, dann übernehmen; vorhandene Messwerte bleiben, vorher wird gesichert. Kosten der nachgeholten Zeiten bleiben 0, bei festem Tarif werden sie automatisch nachgerechnet.

### 4.6 Wetter

Standort per Suche, GPS oder Google-Maps-Koordinaten einstellen. Das Wetter ist nur Anzeige und beeinflusst die Steuerung nicht.

### 4.7 Dashboard-Kacheln

Admin legt fest, welche Kacheln global sichtbar sind und in welcher Reihenfolge. Jeder Benutzer kann es unter **Meine Ansicht** weiter einschränken.

---

## 5. Smart Home: Geräte einrichten

**Einstellungen → Smart Home.** Oben wählst du per Häkchen, **welche Systeme du hast**. Für jedes angekreuzte System erscheint ein eigenes Fenster zum Suchen und Einrichten. Bereits eingerichtete Geräte bleiben immer sichtbar.

Jedes Gerät kann **umbenannt** und mit einem **Symbol** versehen werden, lässt sich per Zieh-Griff **sortieren** und auf dem Dashboard ein-/ausblenden.

| System | Einrichtung |
|---|---|
| **Shelly** (Gen1–Gen3) | „Shellys suchen“ durchsucht das Heimnetz, oder IP einzeln eintragen. |
| **Tasmota** | „Tasmota suchen“ oder IP einzeln eintragen. |
| **Tuya / Smart Life** (z. B. Gosund) | Einmalig Zugang (Region, Access ID, Access Secret) von iot.tuya.com eintragen; die Cloud wird nur zum Abholen der Geräteschlüssel gebraucht, geschaltet wird lokal. |
| **Homematic / HomematicIP (OpenCCU)** | CCU-Adresse, Benutzer, Passwort eintragen (Benutzer mit Schreibrechten). Dann Geräte, Sensoren, Thermostate und Türschlösser suchen. Das Passwort bleibt auf dem Server und wird nie wieder angezeigt. |
| **Zigbee (Phoscon / deCONZ)** | In Phoscon *Einstellungen → Gateway → Erweitert → „App autorisieren“* drücken, dann hier innerhalb einer Minute „Mit Gateway verbinden“. Danach Geräte, Sensoren und Thermostate suchen. |
| **Wake-on-LAN** | Siehe [Kapitel 11](#11-wake-on-lan). |

**Weitere Netze / VLANs:** Standardmäßig durchsucht die App nur das Netz des Servers. Weitere Netze (z. B. `192.168.178.0/24`, mehrere mit Komma) kann man für Shelly, Tasmota und Tuya zusätzlich eintragen. Der Server muss dorthin routen dürfen.

**Homematic-Push:** Mit dem Schalter „Push von der CCU“ meldet die CCU Änderungen (Tür geht auf, Schalter betätigt) selbst an die App. Regeln mit Sensoren reagieren dann in unter einer Sekunde, und es wird nichts ständig abgefragt. Die CCU muss den Server unter dem eingestellten Port erreichen können (Standard 8703). Fällt der Push aus, fragt die App wie gewohnt ab.

---

## 6. Eigene Schalter & Knöpfe (Software)

Das sind Schalter und Knöpfe, hinter denen **kein Gerät** steckt. Sie erscheinen auf dem Dashboard in der Kachel „Schalter & Aktionen“ und dienen als **Auslöser oder Merker für Regeln**.

- **Schalter:** bleibt an/aus, wie ein Lichtschalter.
- **Knopf:** wird gedrückt (löst einmal aus).

Anlegen unter *Einstellungen → Smart Home → Eigene Schalter & Knöpfe*: Name eingeben, Art wählen, **Anlegen**. Jeder Schalter/Knopf lässt sich umbenennen, mit Symbol versehen, sortieren, ausblenden und mit **PIN** schützen.

Was bei Druck passieren soll, baust du auf der Seite **Regeln** (Art „Ablauf“), z. B. der **Warmwasser-Timer**: Knopf drücken → Pumpe an → 60 Minuten warten → Pumpe aus.

---

## 7. Sensoren, Thermostate und Türschlösser

*Einstellungen → Smart Home → Sensoren, Thermostate & Türschlösser.*

### Sensoren (nur lesen)

Von Homematic und Zigbee: Temperatur, Luftfeuchte, Fenster/Tür, Bewegung, Anwesenheit, Helligkeit, Leistung. Pro Sensor einstellbar:

- **Name** und **Symbol**
- **Auf dem Dashboard zeigen** (eigener Schalter pro Sensor)
- **Invertieren:** Dreht Wahr/Falsch um. Beispiel: „geschlossen = wahr = grün“ statt „offen = wahr“.
- Reihenfolge per Ziehen.

Sensoren sind in Regeln als Bedingung nutzbar („WENN Sensor wahr/falsch“ bzw. „unter/über“ bei Messwerten). Ist ein Sensor nicht erreichbar, **passiert in der Regel nichts** (kein Fehlschalten).

### Thermostate

Homematic und Zigbee. In Abläufen kann man die **Solltemperatur setzen**, z. B. „alle Thermostate auf 25 °C“. Der Wert wird auf den Bereich des Geräts begrenzt (0 °C = „aus“, meist 4,5 °C).

### Türschlösser (Homematic IP)

Der Zustand (verriegelt/entriegelt) erscheint als Sensor. Zum **Verriegeln, Entriegeln und Öffnen** per Ablauf wird das Schloss unter „Türschlösser“ angelegt.

> **Sicherheit:** *Entriegeln* und *Öffnen* sind erst erlaubt, wenn du das beim jeweiligen Schloss **ausdrücklich freigibst**. Sie funktionieren **nur in Abläufen** (nie in „Zustand halten“) und werden im **Trockenlauf nie ausgeführt**. *Verriegeln* ist immer erlaubt.

---

## 8. PIN-Schutz

Jedes schaltbare Ding kann mit einer **4-stelligen PIN** geschützt werden: Geräte (Shelly, Tasmota, Tuya, Homematic, Zigbee), eigene Schalter/Knöpfe und Wake-on-LAN-Ziele.

- **Setzen/Ändern/Entfernen:** nur durch **Admins** (Recht „Benutzerverwaltung“ = Schreiben), in der jeweiligen Geräteliste unter Einstellungen. Das Auge-Symbol zeigt die Eingabe.
- **Benutzen:** Beim Tippen auf die Kachel erscheint ein Ziffernblock. Auch Admins geben die PIN ein.
- **Schutz gegen Raten:** Nach 5 falschen Eingaben ist das Gerät 5 Minuten gesperrt.
- Die PIN wird nur als Hash gespeichert, nie im Klartext.
- **Regeln und Abläufe sind von der PIN nicht betroffen** – die PIN schützt nur das Schalten von Hand am Dashboard.

---

## 9. Automatik (Überschuss)

Seite **Automatik**. Ist der Akku voll und wird eingespeist, schaltet die Steuerung die Geräte der Liste **nacheinander zu** (oberstes zuerst). Bei Netzbezug oder Akku-Entladung werden sie in **umgekehrter Reihenfolge** wieder abgeschaltet.

- **Überschuss-Automatik aktiv:** Hauptschalter.
- **Trockenlauf:** Nur protokollieren/melden, nichts schalten.
- **Geräte im Überschuss:** Geräte aus der Liste hinzufügen und per Ziehen sortieren.
- **Akku mindestens (%):** Ab diesem Ladestand gilt Überschuss als vorhanden (bei Ladelimit 90 % z. B. 88).
- **Feineinstellungen** (aufklappbar): Zeiten, Pause nach Handschaltung, Sicherheits-Timer. „Alle auf Standard“ stellt die Voreinstellung wieder her.
- Änderungen gelten erst mit **Speichern** (Leiste unten).
- **Logbuch:** Zeigt, was die Automatik wann und warum geschaltet hat.

> **Wichtig:** Ein Gerät gehört **entweder** zur Überschuss-Automatik **oder** zu einer Regel, nicht zu beidem.

---

## 10. Regeln und Abläufe (Regel-Editor)

Seite **Regeln**. Hier verknüpfst du alles nach dem Schema

> **WENN** (Bedingung) → **DANN** (Aktionen) → **SONST** (Aktionen, optional)

Ähnlich wie Blockly in ioBroker, aber als Formular. Eine neue Regel braucht zuerst nur einen **Namen** und öffnet dann den Regel-Editor. Regeln lassen sich **benennen, kopieren** (als „Kopie <Name>“), ein-/ausschalten und löschen. Die Liste zeigt je Regel eine Zusammenfassung und den Status.

Oben auf der Seite gibt es zwei Hauptschalter:

- **Regeln aktiv:** Hauptschalter für alle Regeln. Beim Ausschalten werden laufende Abläufe beendet.
- **Trockenlauf:** Nur protokollieren (und melden), was geschaltet würde.

### 10.1 Zwei Arten von Regeln

| Art | Verhalten | Typisches Beispiel |
|---|---|---|
| **Zustand halten** | Das Gerät **folgt der Bedingung**: Bedingung wahr → DANN, unwahr → SONST. | „Licht an, solange die Tür offen ist.“ |
| **Ablauf** | Startet **einmal**, wenn die Bedingung eintritt (steigende Flanke). Die Schritte laufen nacheinander, mit Warten. SONST läuft, wenn die Bedingung wieder wegfällt. | „Warmwasser-Timer“, „Morgens PC wecken“, „Thermostate auf 25 °C, nach 2 h zurück“. |

Der Editor stellt die Art automatisch auf „Ablauf“, sobald du einen Schritt wählst, den es nur im Ablauf gibt (Warten, Nachricht, Thermostat usw.).

**Zustands-Regeln:** Laufende Geräte werden übernommen. Von Hand ausgeschaltete bleiben aus, bis die Bedingung einmal nicht mehr stimmt. Bei Konflikten gewinnt **Ausschalten**.

### 10.2 Bedingungen (WENN)

Mehrere Bedingungen lassen sich mit **alle (UND)** oder **eine davon (ODER)** verknüpfen.

| Bedingung | Bedeutung |
|---|---|
| **Uhrzeit von–bis** | Zeitfenster, optional nur an bestimmten Wochentagen. |
| **Uhrzeit (einmal pro Tag)** | Löst zu einer festen Uhrzeit aus. |
| **Strompreis** | Preis über/unter einem Wert (nur bei dynamischem Tarif). |
| **Laufzeit pro Tag** | Das Gerät soll pro Tag x Minuten laufen, und zwar **zu den günstigsten Zeiten** innerhalb eines Zeitfensters (von–bis). Nur bei „Zustand halten“ und dynamischem Tarif. |
| **Akkustand** | SOC über/unter einem Wert. |
| **Sonne morgen (Prognose)** | PV-Prognose für morgen über/unter einem Wert. |
| **Sensor** | Homematic-/Zigbee-Sensor wahr/falsch bzw. Messwert unter/über. |
| **Anderes Gerät** | Ein anderes Gerät ist an/aus, oder seine Leistung liegt über/unter einem Wert. |
| **Eigener Schalter / Knopf** | Schalter ist an/aus, oder Knopf wurde gedrückt. |

Fehlt ein Messwert (Sensor/Gerät nicht erreichbar), **passiert nichts** – die Regel schaltet nicht „ins Blaue“.

### 10.3 Aktionen (DANN / SONST)

| Schritt | Wirkung | Nur im Ablauf? |
|---|---|---|
| **Gerät ein-/ausschalten** | Schaltet ein Gerät. | nein |
| **Gerät umschalten (an ↔ aus)** | Dreht den aktuellen Zustand um. | ja |
| **Warten** | Wartet x Sekunden/Minuten/Stunden (blockiert nichts anderes). | ja |
| **Thermostate: Solltemperatur setzen** | Setzt ein oder mehrere Thermostate auf einen Wert. | ja |
| **Telegram-Nachricht senden** | Schickt einen freien Text aufs Handy (auch für Fehlersuche in Abläufen). | ja |
| **Eigenen Schalter setzen / Knopf drücken** | Verkettet Abläufe: ein Ablauf kann einen Schalter setzen, der einen anderen Ablauf startet. | ja |
| **Türschloss: öffnen / entriegeln / verriegeln** | Siehe Sicherheit in [Kapitel 7](#türschlösser-homematic-ip). | ja |
| **Rechner aufwecken (Wake-on-LAN)** | Sendet das Magic Packet. | ja |

### 10.4 Optionen bei Abläufen

- **Laufenden Ablauf sofort beenden, wenn die Bedingung wegfällt** (z. B. Schalter wird wieder ausgeschaltet): bricht einen laufenden Timer ab. Bei vorhandenen SONST-Schritten passiert das immer, damit sie aufräumen können.
- **Am Ende des Ablaufs den auslösenden Schalter wieder ausschalten** (Standard an): Wird ein eigener Schalter als Auslöser genutzt, springt er nach dem Durchlauf selbst zurück auf „aus“ und ist wieder bereit. Danach läuft das SONST.
- Läuft ein Ablauf, zeigt die Kachel auf dem Dashboard die **Restzeit**.
- Laufende Abläufe überstehen einen **Neustart** der App (sie werden fortgesetzt).
- Im **Trockenlauf** werden Wartezeiten übersprungen und nichts wirklich geschaltet; alles steht im Logbuch.

### 10.5 Reaktionszeit

Regeln werden zyklisch ausgewertet und bei Änderungen sofort angestoßen. Mit aktivem **Homematic-Push** reagieren Sensor-Regeln in unter einer Sekunde. Zigbee wird alle ~1 s abgefragt (Cache).

---

## 11. Wake-on-LAN

Weckt einen PC, ein NAS oder einen Server aus dem Standby/Ruhezustand per „Magic Packet“.

**Einrichten:** *Einstellungen → Smart Home →* Häkchen „Wake-on-LAN“ → Name, **MAC-Adresse** (Windows: `ipconfig /all`, „Physische Adresse“; Linux: `ip link`) und optional **IP-Adresse** (für die Anzeige „erreichbar“ per Ping) sowie **Broadcast-Adresse** (nur bei anderem Netz) eintragen → **Anlegen**.

**Voraussetzungen:**

- Am Rechner ist Wake-on-LAN im **BIOS** und im **Netzwerktreiber** aktiviert (unter Windows: „Magic Packet zum Aufwecken“, ggf. Schnellstart aus).
- Der Server steht im **selben Netz** (Broadcast).

**Benutzen:**

- Am Dashboard: Kachel mit **Aufwecken**-Knopf (optional mit PIN). Mit IP zeigt die Kachel, ob der Rechner gerade erreichbar ist.
- In Regeln (Art „Ablauf“): Schritt **Rechner aufwecken**, z. B. werktags um 07:00.
- Im Trockenlauf wird **kein Paket gesendet**, nur protokolliert.

---

## 12. Logbuch, Betriebsbericht, Solarlogbuch, Watchdog

| Seite | Wo | Inhalt |
|---|---|---|
| **Logbuch** (Automatik/Regeln) | Buttons „📒 Logbuch“ auf Automatik und Regeln | Was wann und warum geschaltet wurde, auch Trockenlauf-Einträge und Fehler. Ungelesene Einträge zeigen ein Abzeichen. |
| **Betriebsbericht** („schlauer Zettel“) | Einstellungen → Meldungen | Ein Blick genügt: läuft die Regelung, sind Prognose, Preise und Daten vollständig? Dazu Tagestabelle, Ereignisprotokoll und Betriebsstatistik (Ø/maximale Dauer eines Regeldurchlaufs, größte Lücke). Mit einem Klick als Text kopierbar, um die Anlage gemeinsam mit Claude auszuwerten. |
| **Solarlogbuch** | Einstellungen → PV-Prognose | VRM-Prognose gegen realen Ertrag der letzten Tage, Abweichung und der Korrekturfaktor, der den Tag getroffen hätte. |
| **Batterie-Watchdog** | Einstellungen | Erkennt und protokolliert, wenn die Batterie trotz Netzfluss über 15 Minuten nicht reagiert (Multiplus-Ladehänger). Zeigt auch Vollzyklen und Lebensdauer-Hochrechnung. |

---

## 13. Benachrichtigungen (Telegram)

Die App meldet sich aufs Handy, wenn etwas nicht stimmt – und wenn es wieder in Ordnung ist. Gemeldet wird nur bei einem **Wechsel**, nicht bei jedem Durchlauf. Jede Meldung beginnt mit dem Namen der Anlage.

**Einrichten (einmalig, ca. 2 Minuten):**

1. In Telegram **@BotFather** öffnen → `/newbot` → Namen vergeben → **Token** kopieren und eintragen.
2. Den neuen Bot öffnen und ihm irgendeine Nachricht schreiben (für eine Gruppe: Bot hinzufügen und dort schreiben).
3. Hier **„Chat-ID ermitteln“** klicken und den Treffer anklicken, dann **Speichern** und **Test senden**.

**Einstellbar:** Welche Ereignisse gemeldet werden, „Akku niedrig unter (%)“ und die Uhrzeit der **Tages-Zusammenfassung** (Tagesbilanz plus Systemcheck, mit Kurzfassung des Betriebsberichts).

Telegram kann zusätzlich in **Abläufen** als Schritt „Telegram-Nachricht senden“ genutzt werden.

---

## 14. Kosten, Tarife und Tarifwechsel

**Vertragskosten** (optional): Wer die festen Posten der Stromrechnung einträgt, bekommt in Telegram-Tagesbilanz und Monatsübersicht zusätzlich eine geschätzte **Gesamtsumme inkl. Gebühren**. Zwei Vorgehen, je nach Rechnung:

1. **Netto + eigene MwSt-Zeile (Tibber-Stil):** Nettobeträge eintragen (Grundgebühr, Netznutzung, Messstelle, §14a-Abzug) und den MwSt-Satz (meist 19 %).
2. **Ein Gesamtpreis, alles inklusive (fester Tarif):** Beträge **brutto** eintragen und die **MwSt auf 0** lassen, sonst wird die Steuer doppelt gerechnet.

**Tarifwechsel:** Ändert sich der Vertrag, den ersten Tag des neuen Tarifs unter **„Änderungen an Preis, Gebühren und MwSt gelten ab“** eintragen und speichern. Alles davor bleibt mit den alten Werten stehen, ab dem Datum gilt das Neue. Leer = ab heute. Das Datum darf zurückliegen (dann werden die Kosten ab diesem Tag neu berechnet), aber nicht in der Zukunft und nicht vor der letzten Änderung.

**Fester Tarif:** Der Preis wird je Vertragszeitraum gespeichert und rückwirkend korrekt auf die Tageskosten angewendet. Tage ohne Kosten (z. B. nach Datennachholen) werden automatisch repariert.

---

## 15. Update, Sicherung, Datenhaltung

- **Update:** *Einstellungen → System → App-Version & Update* → „Nach Updates suchen“ → „Jetzt aktualisieren“. Alternativ per Terminal `git pull` im App-Ordner und Neustart des Dienstes (pm2 bzw. systemd). Persönliche Daten bleiben erhalten.
- **Daten** liegen als JSON-Dateien im Ordner `app/` und sind **nicht in Git** (stehen in `.gitignore`). Dazu gehören u. a. die Konfiguration, die Geräteregister (Geräte, Sensoren, Thermostate, Türschlösser, eigene Schalter, WOL-Ziele, Zigbee-/Homematic-Zugang), Regeln, laufende Abläufe, Historie, Ladeprotokoll, Benutzer und Vertragszeiträume.
- **Backups:** Vor riskanten Operationen (z. B. VRM-Nachimport) legt die App selbst Sicherungen im Ordner `app/backups/` an. Für eine Komplettsicherung den Ordner `app/` (ohne `__pycache__`) kopieren.
- **Passwörter und Tokens** (CCU, Tibber, VRM, Telegram, Tuya, Zigbee-Schlüssel) liegen nur auf dem Server und werden nie wieder angezeigt.
- **Python:** Der Server benötigt mindestens Python 3.9.

---

## 16. Beispiele

### 16.1 Licht, solange die Tür offen ist (Zustand halten)

1. Regeln → neue Regel „Flurlicht bei Tür“.
2. WENN: **Sensor** → Türsensor ist *wahr* (bzw. *offen*).
3. DANN: **Gerät einschalten** → Flurlicht. SONST: **Gerät ausschalten** → Flurlicht.
4. Mit Push von der CCU schaltet das Licht in unter einer Sekunde.

### 16.2 Warmwasser-Timer (Ablauf)

1. Einstellungen → Eigene Schalter: **Schalter** „Warmwasser-Timer“ anlegen.
2. Regel „Warmwasser“, Art **Ablauf**. WENN: **Eigener Schalter** „Warmwasser-Timer“ ist *an*.
3. DANN: Pumpe **einschalten** → **Warten** 60 Minuten → Pumpe **ausschalten**.
4. Optionen: „Laufenden Ablauf sofort beenden, wenn die Bedingung wegfällt“ an. Dann bricht das Ausschalten des Schalters den Timer ab (Pumpe geht per SONST aus).
5. Auf dem Dashboard zeigt die Kachel die Restzeit.

### 16.3 PC morgens wecken (Wake-on-LAN)

1. WOL-Ziel anlegen (Kapitel 11).
2. Regel, Art **Ablauf**, WENN **Uhrzeit (einmal pro Tag)** 07:00, an Werktagen.
3. DANN: **Rechner aufwecken** → dein PC.

### 16.4 Heizung bei viel Sonne anheben (Ablauf)

1. WENN: **Sonne morgen** über Schwellwert *und* **Akkustand** über 80 %.
2. DANN: **Thermostate** auf 22 °C setzen → **Warten** 4 Stunden → Thermostate auf 20 °C.
3. SONST: Thermostate auf 20 °C (räumt auf, auch bei vorzeitigem Abbruch).

### 16.5 Waschmaschine/Boiler nur bei günstigem Strom (Zustand halten)

1. WENN: **Laufzeit pro Tag** 120 Minuten, Zeitfenster 22:00–06:00. Die Regel wählt dann selbst die günstigsten Zeiten im Fenster aus.
2. DANN: Boiler einschalten. SONST: Boiler ausschalten.
3. Alternativ: WENN **Strompreis** unter 20 ct → Boiler an (läuft dann immer, solange der Preis niedrig ist).

### 16.6 Tür öffnen per Knopf (mit Sicherheit)

1. Türschloss unter „Türschlösser“ anlegen und **Entriegeln/Öffnen freigeben**.
2. Eigenen **Knopf** „Tür öffnen“ anlegen und mit **PIN** schützen.
3. Regel (Ablauf): WENN **Knopf** „Tür öffnen“ gedrückt → DANN **Türschloss öffnen**.
4. Zum Ausprobieren erst **Trockenlauf** – das Schloss wird dabei nie bewegt.

---

## 17. Fehlersuche (FAQ)

**Eine Regel schaltet nicht.**
Prüfe nacheinander: Ist „Regeln aktiv“ an und der **Trockenlauf aus**? Zeigt das **Logbuch** einen Eintrag (auch „nicht erreichbar“)? Ist ein Sensor/Gerät der Bedingung erreichbar? Gehört das Gerät auch zur Überschuss-Automatik? Bei einem Ablauf mit eigenem Schalter: Wurde der Schalter nach dem letzten Durchlauf wieder auf „aus“ gestellt?

**„Nicht erreichbar (UNREACH)“ bei Homematic.**
Das Gerät meldet der CCU keine Funkverbindung. In der CCU-Oberfläche nachsehen (Batterie, Reichweite). Die App schaltet bei fehlendem Messwert nichts.

**Homematic reagiert langsam.**
„Push von der CCU“ einschalten. Die CCU muss den Server unter dem Push-Port erreichen können.

**Zigbee verbindet nicht.**
In Phoscon muss „App autorisieren“ aktiv sein, wenn man „Mit Gateway verbinden“ drückt (nur ca. 1 Minute lang). Gateway-Adresse inkl. Port prüfen.

**Rechner wacht nicht auf.**
Wake-on-LAN im BIOS und Netzwerktreiber aktiviert? Schnellstart in Windows aus? Richtige MAC-Adresse? Server im selben Netz (sonst Broadcast-Adresse angeben)? Der Rechner muss per Kabel angeschlossen sein; WLAN weckt meist nicht.

**PIN vergessen / gesperrt.**
Ein Admin kann die PIN unter Einstellungen → Smart Home neu setzen oder entfernen. Eine Sperre nach 5 Fehlversuchen endet nach 5 Minuten.

**Kosten stehen auf 0 € (fester Tarif).**
Die App repariert Tage ohne Kosten beim Start automatisch. Prüfe, ob unter Stromtarif ein Preis eingetragen ist.

**Es wird nicht geladen, obwohl der Preis niedrig ist.**
Dry-Run an? Ladelimit schon erreicht? Fester Tarif (lädt nie aktiv)? Mit der Intelligenten Planung entscheidet der Plan über das gesamte Zeitfenster, nicht nur der aktuelle Preis – der Ladeplan im Dashboard zeigt die geplanten Fenster.

**Browser zeigt alte Oberfläche nach einem Update.**
Seite hart neu laden (Strg+Shift+R) bzw. auf dem Handy den Browser-Cache der App leeren.

**Betriebsbericht zeigt keine Dauer-Werte.**
Die Statistik (Ø/max. Dauer eines Regeldurchlaufs) braucht einige Tage Betrieb, um aussagekräftig zu sein.

---

## Haftung

Nutzung auf **eigene Verantwortung**. Die Software schaltet Strom, Heizung und – wenn freigegeben – Türschlösser. Vor dem Scharfschalten alles im **Trockenlauf** prüfen, besonders Abläufe mit Türschloss, Thermostaten oder Netzladung.
