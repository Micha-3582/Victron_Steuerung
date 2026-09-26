#!/usr/bin/env python3
"""
Victron Standalone Steuerung - Web-App
======================================
Mobile Web-Oberfläche + integrierter Regler (Scheduler-Thread).
- Dashboard: Status, Preis-Kurve, Plan, manueller Override, E-Auto-Termine
- Einrichtungsassistent (/setup) beim ersten Start
- Admin-Bereich (/admin) zum Anpassen aller Einstellungen

Start:
  pip install -r requirements.txt
  python webapp.py            # http://<host>:5005
"""
import json
import logging
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for)

import os

import shelly
import store
import surplus
import tuya
import updater
import vrm
import vrm_import
from auth import UserError, UserStore, new_secret_key
from datasources import (OPENMETEO_PR, PvForecast, PvForecastOpenMeteo,
                         fetch_tibber_prices)
from logic import ESS_CHARGE, ESS_IDLE, Params, decide
from logic import _parse_iso as logic_parse_iso
from victron import Cerbo

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("webapp")
surplus_ctrl = surplus.SurplusController()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_USERNAME = "Micha3582"

app = Flask(__name__)
app.secret_key = new_secret_key(os.path.join(BASE_DIR, "secret.key"))
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(store.load_config().get("cookie_secure", False)),
)

users = UserStore(os.path.join(BASE_DIR, "users.json"))
_initial_pw = users.ensure_initial_user(DEFAULT_USERNAME, BASE_DIR)
if _initial_pw:
    print("=" * 68, flush=True)
    print("  ERSTSTART: Zugang wurde angelegt", flush=True)
    print(f"    Benutzer: {DEFAULT_USERNAME}", flush=True)
    print(f"    Passwort: {_initial_pw}", flush=True)
    print("  Steht auch in initial-password.txt", flush=True)
    print("  Bitte nach der ersten Anmeldung unter Einstellungen aendern!", flush=True)
    print("=" * 68, flush=True)

@app.context_processor
def inject_app_display_name():
    """Personalisierbarer Anzeigename (Kopfzeile/Titel) - fuer alle Templates
    verfuegbar, auch die Login-Seite (kein DB-Zugriff, nur die lokale Datei)."""
    return {"app_display_name": store.load_config().get("app_display_name") or "Victron Steuerung"}


PUBLIC_ENDPOINTS = {"login", "static", "service_worker", "manifest"}

_attempts: dict[str, list] = {}
_attempts_lock = threading.Lock()


def client_ip() -> str:
    # Hinter Cloudflare Tunnel steht die echte IP im Header.
    return (
        request.headers.get("CF-Connecting-IP")
        or (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        or request.remote_addr
        or "?"
    )


def too_many_attempts(ip: str) -> bool:
    """Einfache Bremse gegen Passwort-Raten (max. 10 Versuche / 5 Min pro IP)."""
    window, limit = 300, 10
    now = time.time()
    with _attempts_lock:
        tries = [t for t in _attempts.get(ip, []) if now - t < window]
        _attempts[ip] = tries
        return len(tries) >= limit


def note_failed_attempt(ip: str) -> None:
    with _attempts_lock:
        _attempts.setdefault(ip, []).append(time.time())


@app.before_request
def _require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    username = session.get("user")
    user = users.get(username) if username else None
    if not user:
        session.clear()
        if request.path.startswith("/api/"):
            return jsonify(error="Nicht angemeldet.", login_required=True), 401
        return redirect(url_for("login", next=request.path))
    g.user = username
    g.user_display = user["username"]
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if session.get("user") and users.get(session["user"]):
            return redirect(url_for("index"))
        return render_template("login.html", error=None)

    ip = client_ip()
    if too_many_attempts(ip):
        return render_template(
            "login.html", error="Zu viele Fehlversuche. Bitte einige Minuten warten."
        ), 429

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    remember = bool(request.form.get("remember"))

    user = users.verify(username, password)
    if not user:
        note_failed_attempt(ip)
        return render_template("login.html", error="Benutzername oder Passwort falsch."), 401

    session.clear()
    session["user"] = username.strip().lower()
    session.permanent = remember
    target = request.args.get("next") or url_for("index")
    if not target.startswith("/"):
        target = url_for("index")
    return redirect(target)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/sw.js")
def service_worker():
    """Service Worker MUSS vom Wurzelpfad kommen, sonst gilt er nur fuer /static/."""
    resp = app.send_static_file("sw.js")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/manifest.webmanifest")
def manifest():
    """PWA-Manifest mit personalisiertem Namen - nuetzlich, wenn mehrere
    installierte Instanzen unterscheidbar sein sollen."""
    path = os.path.join(app.static_folder, "manifest.webmanifest")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    name = store.load_config().get("app_display_name") or "Victron Steuerung"
    m["name"] = name
    m["short_name"] = name
    resp = jsonify(m)
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/api/password")
def api_change_own_password():
    """Eigenes Passwort aendern."""
    body = request.json or {}
    if not users.verify(g.user, body.get("current") or ""):
        return jsonify(error="Aktuelles Passwort ist falsch."), 400
    if body.get("new") != body.get("new2"):
        return jsonify(error="Die Passwörter stimmen nicht überein."), 400
    try:
        users.update_password(g.user, body.get("new") or "")
    except UserError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True)


ESS_TEXT = {ESS_CHARGE: "Netzladen", ESS_IDLE: "Normal / Warten"}


class Controller:
    """Hintergrund-Regler: holt Daten, entscheidet, schreibt ESS-Mode.
    Hält den letzten Status im Speicher für die Web-UI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"ok": False, "reason": "startet ..."}
        self.prices = []          # aufbereitete Slots für die Kurve
        self.last_tick = None
        self.last_error = None
        self._pv = None
        self._pv_key = None
        self._om = None
        self._om_key = None
        self._stop = threading.Event()
        self.last_system = None          # letzte Cerbo-Messung (vom Energie-Sampler)
        self.last_system_ts = 0.0
        self._surplus_dry_on: dict[str, bool] = {}   # Trockenlauf: gedachter Schaltzustand

    def _pv_source(self, cfg):
        key = (cfg["pv_latitude"], cfg["pv_longitude"], str(cfg["pv_planes"]))
        if self._pv is None or self._pv_key != key:
            self._pv = PvForecast(cfg["pv_latitude"], cfg["pv_longitude"], cfg["pv_planes"])
            self._pv_key = key
        return self._pv

    def _om_source(self, cfg):
        """Primärquelle Open-Meteo für die Regelung. Performance Ratio aus der
        Config (Standard 0,68), wird bei Änderung neu aufgebaut."""
        pr = float(cfg.get("openmeteo_pr", OPENMETEO_PR))
        key = (cfg["pv_latitude"], cfg["pv_longitude"], str(cfg["pv_planes"]), pr)
        if self._om is None or self._om_key != key:
            self._om = PvForecastOpenMeteo(cfg["pv_latitude"], cfg["pv_longitude"],
                                           cfg["pv_planes"], pr=pr)
            self._om_key = key
        return self._om

    def tick(self):
        cfg = store.load_config()
        if not store.is_configured(cfg):
            with self.lock:
                self.status = {"ok": False, "reason": "nicht eingerichtet"}
            return
        cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
        soc = cerbo.read_soc()
        current_ess = cerbo.read_ess_mode()
        try:
            system = cerbo.read_system()
        except Exception as e:                           # noqa: BLE001
            system = None
            log.warning("System-Werte nicht lesbar: %s", e)
        prices = fetch_tibber_prices(cfg["tibber_token"])
        pv_note = None
        # Primärquelle für die Regelung: Open-Meteo (bringt die Performance Ratio
        # schon mit -> kein zusätzlicher Korrekturfaktor in decide()).
        try:
            solar_today, solar_tom = self._om_source(cfg).get()
        except Exception as e:                               # noqa: BLE001
            solar_today = solar_tom = 0.0
            pv_note = f"PV-Prognose (Open-Meteo) nicht verfügbar ({e}) - rechne mit 0 kWh"
            log.warning(pv_note)
        # forecast.solar nur noch als Vergleich fürs Logbuch (steuert nichts).
        try:
            fs_today, _fs_tom = self._pv_source(cfg).get()
        except Exception as e:                               # noqa: BLE001
            fs_today = 0.0
            log.warning("forecast.solar (Vergleich) nicht verfügbar: %s", e)

        now = datetime.now()
        ev = store.active_ev(now)
        forced = bool(cfg.get("manual_override")) or ev is not None
        reason = "Manueller Ladetermin" if ev else "MANUELL"

        # Fuer die REGELUNG den Tagesrest um das bereits real Gemessene ersetzen -
        # sonst plant decide() nachmittags noch mit der ungenauen Morgen-Tagesprognose
        # weiter, obwohl laengst klar ist, wie viel Sonne heute tatsaechlich kam.
        # Gleiches Prinzip wie bei der Anzeige-Spanne (store.pv_forecast_range):
        # gemessen + laut Stundenkurve noch zu erwartender Rest von JETZT bis
        # Tagesende (nachts automatisch 0). Betrifft nur decide() - der WEITER
        # UNTEN ans Solarlogbuch gemeldete Wert bleibt die reine, unkorrigierte
        # Tagesprognose (sonst wuerde sich die Kalibrierungsbasis selbst verfaelschen,
        # da record_solar_forecast() den Wert vom ERSTEN Tick des Tages einfriert).
        # Zusaetzlich zur globalen Performance Ratio (die den ganzen Tag gleich
        # behandelt) eine gelernte Tageszeit-Korrektur anwenden - faengt z.B. eine
        # nur morgens verschattete Flaeche ab, die ein einzelner Tages-Faktor
        # verschmieren wuerde (siehe store.auto_adjust_bucket_factors).
        bucket_factors = cfg.get("pv_bucket_factors")
        solar_today_for_control = solar_today
        try:
            om_remaining = self._om_source(cfg).get_remaining_today(
                now, bucket_factors=bucket_factors)
            solar_today_for_control = round(
                store.solar_measured_today(now) + om_remaining, 2)
        except Exception as e:                               # noqa: BLE001
            log.warning("PV-Rest-Korrektur fuer die Regelung fehlgeschlagen (%s) - "
                        "nutze die reine Tagesprognose", e)

        # Steuerquelle: Victron VRM (kennt die reale Anlage) - ohne Zugang, bei Ausfall oder
        # veralteten Werten faellt die Regelung automatisch auf Open-Meteo zurueck.
        pv_source, vrm_ctl, solar_tom_ctl = "Open-Meteo", None, solar_tom
        if cfg.get("solar_source", "auto") != "openmeteo":
            try:
                vrm_ctl, vrm_why = vrm.control_forecast(vrm.forecast(), now, store.solar_measured_today(now))
                if vrm_why:
                    pv_note = vrm_why
                    log.warning(vrm_why)
            except Exception as e:                           # noqa: BLE001
                vrm_ctl = None
                log.warning("VRM-Prognose fuer die Regelung fehlgeschlagen (%s) - nutze Open-Meteo", e)
        if vrm_ctl:
            pv_source = "VRM"
            solar_today_for_control = vrm_ctl["today_kwh"]
            if vrm_ctl["tomorrow_kwh"] is not None:
                solar_tom_ctl = vrm_ctl["tomorrow_kwh"]
            else:
                pv_note = "VRM liefert noch keine Prognose für morgen – für morgen Open-Meteo"

        # Open-Meteo bringt die Performance Ratio schon mit -> in decide() KEINEN
        # weiteren Korrekturfaktor anwenden (sonst doppelte Skalierung). Das gilt auch fuer VRM.
        params = Params.from_config(cfg)
        params.pv_korrektur_faktor = 1.0
        state = store.load_state()
        d = decide(soc=soc, price_entries=prices, solar_today_raw=solar_today_for_control,
                   solar_tom_raw=solar_tom_ctl, state=state, now=now,
                   manual_override=forced, force_reason=reason,
                   params=params)
        store.save_state(state)
        store.log_charge_state(d.ess_mode == ESS_CHARGE, d.strategy, now)
        # Solar-Logbuch: Open-Meteo (Steuerquelle) einfrieren, forecast.solar als
        # Vergleich mitloggen, vergangene Tage mit dem realen Ertrag abschließen.
        vrm_today = None
        try:                                     # VRM-Prognose: nur Vergleich fuers Logbuch, Ausfall egal
            vf = vrm.forecast()
            if vf.get("hours"):
                vrm_today = vf["today_kwh"]
        except Exception as e:                               # noqa: BLE001
            log.warning("VRM-Prognose (Vergleich) nicht verfügbar: %s", e)
        if solar_today > 0 or fs_today > 0 or vrm_today:
            fs_factor = Params.from_config(cfg).pv_korrektur_faktor
            fs_corr = round(fs_today * fs_factor, 2) if fs_today else None
            store.record_solar_forecast(
                om_kwh=solar_today, pr=float(cfg.get("openmeteo_pr", OPENMETEO_PR)),
                now=now, fs_raw=(fs_today or None), fs_corr=fs_corr,
                fs_factor=fs_factor,
                hourly_today=self._om_source(cfg).get_hourly_today(), vrm_kwh=vrm_today)
            # Einmal pro Tag die PR leise Richtung Logbuch-Empfehlung nachziehen -
            # ab hier laeuft die Kalibrierung von selbst, kein manuelles Nachtragen
            # mehr noetig (siehe store.auto_adjust_pr).
            new_pr = store.auto_adjust_pr(now)
            if new_pr is not None:
                cfg["openmeteo_pr"] = new_pr
                log.info("PV-Prognose: Performance Ratio automatisch auf %.3f angepasst", new_pr)
            # Dieselbe taegliche Nachjustierung fuer die Tageszeit-Buckets (siehe
            # store.auto_adjust_bucket_factors) - laeuft unabhaengig von der PR-
            # Anpassung, beide schreiben nur unterschiedliche Config-Felder.
            new_buckets = store.auto_adjust_bucket_factors(now)
            if new_buckets is not None:
                cfg["pv_bucket_factors"] = new_buckets
                log.info("PV-Prognose: Tageszeit-Faktoren automatisch angepasst auf %s",
                          new_buckets)

        dry = bool(cfg.get("dry_run", True))
        wrote = False
        if d.ess_mode != current_ess:
            wrote = cerbo.write_ess_mode(d.ess_mode, dry_run=dry)

        with self.lock:
            self.prices = self._prep_prices(prices, d, Params.from_config(cfg).absolute_cheap_price)
            self.status = {
                "ok": True,
                "soc": round(soc, 1),
                "ess_mode": d.ess_mode,
                "ess_current": current_ess,
                "ess_text": ESS_TEXT.get(d.ess_mode, str(d.ess_mode)),
                "allow_now": d.allow_now,
                "now_price": d.now_price,
                "now_slot": d.now_slot,
                "strategy": d.strategy,
                "reason": d.reason,
                "balance": d.balance,
                "plan_windows": d.plan_windows,
                "plan_count": len(d.plan),
                "plan_slots": [{"start": s.start.isoformat(timespec="minutes"),
                                "price": round(s.price, 2)} for s in d.plan],
                "charge_power_w": Params.from_config(cfg).charge_power_w,
                "pv_today": d.solar_today_korr,
                "pv_tom": d.solar_tom_korr,
                # Unsicherheits-Spannen stammen aus den Open-Meteo-Abweichungen - fuer VRM nicht uebertragbar
                "pv_source": pv_source,
                "pv_today_range": None if vrm_ctl else store.pv_forecast_range(
                    d.solar_today_korr, now.date().isoformat(), now,
                    remaining_forecast_kwh=self._om_source(cfg).get_remaining_today(
                        now, bucket_factors=bucket_factors)),
                "pv_tom_range": None if vrm_ctl else store.pv_forecast_range(
                    d.solar_tom_korr, (now.date() + timedelta(days=1)).isoformat(), now),
                "dry_run": dry,
                "wrote": wrote,
                "ev_active": ev,
                "pv_note": pv_note,
                "system": system,
                "override": bool(cfg.get("manual_override")),
            }
        # Hinweis: Das Energie-Logging läuft in einem eigenen, feineren Takt
        # (run_energy / energy_sample_seconds), NICHT hier - sonst würde die
        # Trapez-Integration doppelt zählen.

        self.last_tick = now.isoformat(timespec="seconds")
        self.last_error = None

    def _prep_prices(self, entries, decision, absolute_cheap_price=0.0):
        # Datums-genauer Abgleich: geplante Slots über den vollen Zeitstempel
        # markieren, NICHT nur über die Uhrzeit - sonst würde z.B. 13:45 an
        # heute UND morgen als geplant erscheinen.
        planned = {p.start.isoformat(timespec="minutes") for p in decision.plan}
        out = []
        for item in entries:
            try:
                start = logic_parse_iso(item["startsAt"])
            except (KeyError, ValueError):
                continue
            ct = round(item["total"] * 100, 2)
            out.append({
                "start": start.isoformat(timespec="seconds"),
                "label": f"{start:%H:%M}",
                "ct": ct,
                "level": item.get("level", "NORMAL"),
                "planned": start.isoformat(timespec="minutes") in planned,
                # Rein optische Vorschau der "Immer laden unter"-Schwelle (siehe
                # logic.decide()) - im Unterschied zu "planned" KEIN Ergebnis der
                # eigentlichen Planung, sondern nur "dieser Slot WUERDE die Regel
                # ausloesen, sobald er dran ist". Reagiert die Steuerung ja ohnehin
                # erst live pro Tick, aber so sieht man vorab, wo es greifen wird.
                "cheap_lock": bool(absolute_cheap_price) and ct <= absolute_cheap_price,
            })
        return out

    def safe_tick(self):
        """Tick mit Fehlerabfang - für Hintergrundschleife und On-Demand-Aufrufe."""
        try:
            self.tick()
        except Exception as e:                           # noqa: BLE001
            self.last_error = str(e)
            with self.lock:
                self.status = {"ok": False, "reason": f"Fehler: {e}"}
            log.error("Tick fehlgeschlagen: %s", e)

    def run(self, interval):
        while not self._stop.is_set():
            self.safe_tick()
            # Intervall bei jedem Durchlauf frisch lesen -> Änderung in den
            # Einstellungen greift ohne Neustart.
            try:
                interval = max(10, int(store.load_config().get("poll_seconds", 300)))
            except Exception:                            # noqa: BLE001
                interval = 300
            # Auf das Zeitraster ausrichten: der nächste Tick fällt genau auf ein
            # Vielfaches des Intervalls seit voller Stunde. Bei Teilern von 900 s
            # (z.B. 60, 300, 900) trifft das exakt die Viertelstunden :00/:15/:30/:45,
            # sodass Tibber-Slots punktgenau geschaltet werden.
            nowt = time.time()
            sleep_s = interval - (nowt % interval)
            if sleep_s < 1:                              # schon auf dem Raster
                sleep_s += interval
            self._stop.wait(sleep_s)

    def _check_battery_watchdog(self, system: dict, now: datetime):
        """Erkennt den Bulk/Absorption-Haenger vom 01.09.2026: Batterie bewegt
        sich trotz nennenswertem Netzfluss nicht. Nur Erkennung + Log-Warnung,
        kein automatischer Eingriff (siehe Projekt-Notiz - ein Modbus-Befehl
        half beim echten Vorfall nachweislich nicht, nur ein physischer Reset)."""
        try:
            grid_w = system["grid"]["total"]
            batt_a = system["battery"]["current"]
            soc = system["battery"]["soc"]
        except (KeyError, TypeError):
            return
        is_frozen = abs(grid_w) > 150 and abs(batt_a) < 0.5
        detail = {"grid_w": round(grid_w, 1), "battery_a": round(batt_a, 2), "soc": soc}
        try:
            wd = store.battery_watchdog_update(is_frozen, now, detail)
        except Exception as e:                               # noqa: BLE001
            log.warning("Batterie-Watchdog: %s", e)
            return
        if wd["just_warned"]:
            log.warning(
                "Batterie-Watchdog: Batterie reagiert seit >=15 Min nicht (Netz %.0f W, "
                "Batteriestrom %.2f A, SOC %.1f%%) - moeglicher Ladealgorithmus-Haenger "
                "am Multiplus, siehe Projekt-Notiz (Vorfall 01.09.2026)",
                grid_w, batt_a, soc,
            )
        if wd["just_resolved"]:
            ev = wd["just_resolved"]
            log.info("Batterie-Watchdog: wieder normal nach %.1f Min (seit %s)",
                     ev["duration_min"], ev["start"])

    def run_energy(self):
        """Eigener, feiner Takt nur für die Energie-Messung. Tastet die
        Momentanleistung häufig ab (Standard 10 s) und integriert sie zu kWh -
        deutlich genauer als der 60-s-Regeltakt, näher an VRM. Läuft unabhängig
        vom Dashboard. Einziger Aufrufer von log_energy_sample (kein Doppelzählen)."""
        while not self._stop.is_set():
            try:
                cfg = store.load_config()
                if store.is_configured(cfg):
                    cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
                    system = cerbo.read_system()
                    now = datetime.now()
                    with self.lock:
                        price_ct = self.status.get("now_price") if self.status.get("ok") else None
                    store.log_energy_sample(system, now, price_ct=price_ct)
                    self.last_system, self.last_system_ts = system, time.time()
                    self._check_battery_watchdog(system, now)
            except Exception as e:                       # noqa: BLE001
                log.warning("Energie-Sampler: %s", e)
            try:
                iv = max(3, int(store.load_config().get("energy_sample_seconds", 10)))
            except Exception:                            # noqa: BLE001
                iv = 10
            self._stop.wait(iv)

    def run_surplus(self):
        """Ueberschuss-Automatik fuer Shelly-Geraete (alle 10 s). Nutzt die Messwerte
        des Energie-Samplers; ohne frische Werte wird nichts geschaltet."""
        while not self._stop.is_set():
            try:
                cfg = store.load_config()
                if cfg.get("surplus_enabled"):
                    system = self.last_system if time.time() - self.last_system_ts < 45 else None
                    if system:
                        dry = bool(cfg.get("surplus_dry_run"))
                        devs = sorted(shelly.list_with_status(),
                                      key=lambda d: d.get("prio") or 10 ** 6)   # Prioritaet
                        if dry:      # Trockenlauf: mit gedachtem statt echtem Zustand rechnen
                            for d in devs:
                                if d["id"] in self._surplus_dry_on:
                                    d["on"] = self._surplus_dry_on[d["id"]]
                        now = datetime.now()
                        if dry:
                            surplus_ctrl.disarm_all()
                        else:        # Sicherheits-Timer der zugeschalteten Geraete verlaengern (nur bei frischen Messwerten)
                            fs = surplus.settings(cfg)["failsafe_min"]
                            for d in surplus_ctrl.due_rearm(now, cfg, devs):
                                try:
                                    shelly.set_state(d["id"], True, timer_s=int(fs * 60))
                                    surplus_ctrl.mark_armed(d["id"], now)
                                except shelly.ShellyError as e:
                                    log.warning("Sicherheits-Timer %s: %s", d["name"], e)
                        act = surplus_ctrl.step(now, system, cfg, devs)
                        if act:
                            self._apply_surplus(act, dry, cfg)
                else:
                    surplus_ctrl.reset_timers()
                    surplus_ctrl.disarm_all()   # Timer laufen aus -> Geraete schalten sich selbst ab
                    self._surplus_dry_on.clear()
            except Exception as e:                       # noqa: BLE001
                log.warning("Ueberschuss-Automatik: %s", e)
            self._stop.wait(10)

    def _apply_surplus(self, act, dry: bool, cfg: dict):
        action, dev, why = act
        on = action == "on"
        verb = "eingeschaltet" if on else "ausgeschaltet"
        if dry:
            self._surplus_dry_on[dev["id"]] = on
            text = f"(Trockenlauf) {dev['name']} würde {verb} – {why}"
        else:
            try:
                fs = surplus.settings(cfg)["failsafe_min"]
                timer_s = int(fs * 60) if on and fs > 0 and dev.get("kind") != "tuya" else None
                shelly.set_state(dev["id"], on, timer_s=timer_s)
                if timer_s:
                    surplus_ctrl.mark_armed(dev["id"])
                elif not on:
                    surplus_ctrl.disarm(dev["id"])
                text = f"{dev['name']} {verb} – {why}"
            except shelly.ShellyError as e:
                text = f"{dev['name']}: Schalten fehlgeschlagen ({e})"
        log.info("Ueberschuss-Automatik: %s", text)
        surplus_ctrl.log(text)

    def start(self):
        cfg = store.load_config()
        interval = int(cfg.get("poll_seconds", 300))
        threading.Thread(target=self.run, args=(interval,), daemon=True).start()
        threading.Thread(target=self.run_energy, daemon=True).start()
        threading.Thread(target=self.run_surplus, daemon=True).start()


ctrl = Controller()


# --- Routen ---------------------------------------------------------------
@app.route("/")
def index():
    if not store.is_configured():
        return redirect("/setup")
    return render_template("index.html")


@app.route("/setup")
def setup():
    return render_template("setup.html", cfg=store.load_config())


@app.route("/admin")
def admin():
    return render_template("admin.html", cfg=store.load_config())


@app.route("/solar-log")
def solar_log_page():
    return render_template("solar_log.html")


@app.route("/api/solar-log")
def api_solar_log():
    cfg = store.load_config()
    data = store.solar_log()
    data["current_pr"] = float(cfg.get("openmeteo_pr", OPENMETEO_PR))
    try:                                          # vom VRM gemessener Tagesertrag zum Vergleich mit unserer Messung
        vm = vrm.daily_solar()
    except Exception as e:                        # noqa: BLE001
        log.warning("VRM-Tagesertrag nicht verfügbar: %s", e)
        vm = {}
    for row in data.get("rows", []):
        if row["date"] in vm:
            row["vrm_measured"] = vm[row["date"]]
    return jsonify(data)


@app.route("/watchdog")
def watchdog_page():
    return render_template("watchdog.html")


@app.route("/api/watchdog")
def api_watchdog():
    return jsonify(store.battery_watchdog_state())


def _next15(iso):
    return (datetime.fromisoformat(iso) + timedelta(minutes=15)).isoformat(timespec="minutes")


def _window_stats(start_iso, end_iso, price_at, power_kw):
    """Kennzahlen für ein Ladefenster start–end: kWh, Kosten (€) und
    (dauer­gewichteter) Ø-Preis. Rechnet über die tatsächliche Überlappung mit
    den Viertelstunden-Preis-Slots – auch wenn start/end nicht aufs 15-Min-Raster
    fallen (z.B. echter Ladebeginn 00:47)."""
    s = datetime.fromisoformat(start_iso)
    e = datetime.fromisoformat(end_iso)
    kwh = cost = wsum = wdur = 0.0
    t = s
    while t < e:
        slot_start = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        seg_end = min(e, slot_start + timedelta(minutes=15))
        dur_h = (seg_end - t).total_seconds() / 3600.0
        kwh_seg = dur_h * power_kw
        kwh += kwh_seg
        p = price_at.get(slot_start.strftime("%Y-%m-%dT%H:%M"))
        if p is not None:
            cost += p / 100.0 * kwh_seg
            wsum += p * dur_h
            wdur += dur_h
        t = seg_end
    return {"avg_price": round(wsum / wdur, 1) if wdur else None,
            "kwh": round(kwh, 2), "cost": round(cost, 2)}


def _window_stats_measured(start_iso, end_iso, price_at, gbatt_at):
    """Wie _window_stats, aber mit der TATSÄCHLICH gemessenen Netz→Batterie-Energie
    (aus dem Energie-Sampler) statt der geschätzten Ladeleistung. Für bereits
    geladene/laufende Fenster – so stimmt die Menge mit dem Flüsse-Chart überein."""
    s = datetime.fromisoformat(start_iso)
    e = datetime.fromisoformat(end_iso)
    kwh = cost = wsum = wdur = 0.0
    t = s
    while t < e:
        slot_start = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        key = slot_start.strftime("%Y-%m-%dT%H:%M")
        seg_end = min(e, slot_start + timedelta(minutes=15))
        frac = (seg_end - t).total_seconds() / 900.0          # Anteil am 15-Min-Bucket
        kwh_seg = gbatt_at.get(key, 0.0) * frac
        kwh += kwh_seg
        dur_h = (seg_end - t).total_seconds() / 3600.0
        p = price_at.get(key)
        if p is not None:
            cost += p / 100.0 * kwh_seg
            wsum += p * dur_h
            wdur += dur_h
        t = seg_end
    return {"avg_price": round(wsum / wdur, 1) if wdur else None,
            "kwh": round(kwh, 2), "cost": round(cost, 2)}


def build_charge_overview(status, charge, prices):
    """Kombiniert geplante Ladefenster (aus dem Plan) mit tatsächlich geladenen
    (aus dem Protokoll) inkl. Menge und Kosten. Geladene/laufende Fenster nutzen die
    gemessene Netz→Batterie-Energie, geplante die geschätzte Ladeleistung."""
    power_kw = (status.get("charge_power_w") or 3500) / 1000.0
    price_at = {p["start"][:16]: p["ct"] for p in prices}
    now_iso = datetime.now().isoformat(timespec="minutes")
    gbatt_at = store.energy_grid_charge_buckets(now_iso[:10])
    items = []
    for s in charge.get("sessions", []):
        items.append({"start": s["start"], "end": s["end"], "status": "geladen",
                      "strategy": s.get("strategy", ""),
                      **_window_stats_measured(s["start"], s["end"], price_at, gbatt_at)})
    op = charge.get("open")
    if op:
        items.append({"start": op["start"], "end": None, "status": "läuft",
                      "strategy": op.get("strategy", ""),
                      **_window_stats_measured(op["start"], now_iso, price_at, gbatt_at)})
    # geplante (zukünftige) Fenster: zusammenhängende Plan-Slots mergen.
    # Nur HEUTE (die Karte zeigt nur den heutigen Tag) und bereits laufende
    # Slots (vom offenen Vorgang abgedeckt) ausblenden. Sonst würden morgige
    # Plan-Slots hier ohne Datum erscheinen und wie heute aussehen.
    today = now_iso[:10]
    ps = [x for x in status.get("plan_slots", [])
          if x["start"][:10] == today and not (op and x["start"] <= now_iso)]
    ps = sorted(ps, key=lambda x: x["start"])
    i = 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and _next15(ps[j]["start"]) == ps[j + 1]["start"]:
            j += 1
        start, end = ps[i]["start"], _next15(ps[j]["start"])
        items.append({"start": start, "end": end, "status": "geplant", "strategy": "",
                      **_window_stats(start, end, price_at, power_kw)})
        i = j + 1
    items.sort(key=lambda x: x["start"])
    return items


@app.route("/api/status")
def api_status():
    with ctrl.lock:
        status = dict(ctrl.status)
        prices = list(ctrl.prices)
    charge = store.list_charge_sessions()
    cfg = store.load_config()
    return jsonify({
        "status": status,
        "ui": {"chart_energy_hourly": bool(cfg.get("chart_energy_hourly", False)),
               "chart_flow_hourly": bool(cfg.get("chart_flow_hourly", False)),
               # Alle Dashboard-Kacheln einzeln ein-/ausblendbar (Einstellungen ->
               # Kacheln), Default ueberall an - siehe TILE_IDS in index.html.
               "show_live_values": bool(cfg.get("show_live_values", True)),
               "show_energy_chart": bool(cfg.get("show_energy_chart", True)),
               "show_flow_chart": bool(cfg.get("show_flow_chart", True)),
               "show_week_overview": bool(cfg.get("show_week_overview", True)),
               "show_month_overview": bool(cfg.get("show_month_overview", True)),
               "show_tibber_card": bool(cfg.get("show_tibber_card", True)),
               "show_override_card": bool(cfg.get("show_override_card", True)),
               "show_price_plan": bool(cfg.get("show_price_plan", True)),
               "show_charge_log": bool(cfg.get("show_charge_log", True)),
               "show_ev_card": bool(cfg.get("show_ev_card", True)),
               "show_shelly_card": bool(cfg.get("show_shelly_card", True)),
               "tile_order": [k for k in (cfg.get("tile_order") or []) if isinstance(k, str)]},
        "prices": prices,
        "ev_schedules": store.list_ev(),
        "charge_log": charge,
        "charge_overview": build_charge_overview(status, charge, prices),
        "energy_history": store.energy_history_today(),
        "energy_min_day": store.energy_min_day(),
        "last_tick": ctrl.last_tick,
        "last_error": ctrl.last_error,
        "battery_watchdog": store.battery_watchdog_state(),
        "now": datetime.now().isoformat(timespec="seconds"),
    })


@app.route("/api/history")
def api_history():
    now = datetime.now()
    day = request.args.get("day") or now.strftime("%Y-%m-%d")
    return jsonify({
        "day": day,
        "history": store.energy_history_for_day(day, now),
        "min_day": store.energy_min_day(),
        "max_day": now.strftime("%Y-%m-%d"),
    })


@app.route("/api/week")
def api_week():
    """Wochenrueckblick: Solar/Verbrauch/Netz/Kosten einer 7-Tage-Woche.
    ?offset=0 aktuelle Woche (Default), 1 die davor, usw. - so bleibt
    Aelteres ueber die Pfeile erreichbar statt aus der Anzeige zu fallen."""
    try:
        offset = max(0, int(request.args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    return jsonify(store.energy_week_summary(offset_weeks=offset))


@app.route("/api/month")
def api_month():
    """Monatsuebersicht: eine Zeile je Kalendermonat (Solar/Verbrauch/Netz/
    Autarkie/Kosten), aus dem dauerhaften Monats-Archiv (nicht auf die 35-Tage-
    Historie beschraenkt - siehe store.monthly_overview)."""
    return jsonify(store.monthly_overview())


_live_cache = {"ts": 0.0, "data": None}
_live_lock = threading.Lock()


@app.route("/api/live")
def api_live():
    """Schnelle Live-Werte direkt vom Cerbo (SOC, ESS, System-Kacheln).
    Unabhängig vom 5-Min-Regelzyklus. Kurzer Cache (2 s) gegen Überlastung."""
    now = time.time()
    with _live_lock:
        if _live_cache["data"] and (now - _live_cache["ts"]) < 1:
            return jsonify(_live_cache["data"])
        cfg = store.load_config()
        if not store.is_configured(cfg):
            return jsonify({"ok": False, "reason": "nicht eingerichtet"})
        try:
            cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
            system = cerbo.read_system()
            data = {"ok": True, "soc": round(cerbo.read_soc(), 1),
                    "ess_mode": cerbo.read_ess_mode(),
                    "system": system,
                    "battery_usable_kwh": cfg.get("battery_usable_kwh"),
                    "grid_today": store.energy_grid_today(),
                    "now": datetime.now().isoformat(timespec="seconds")}
        except Exception as e:                           # noqa: BLE001
            data = {"ok": False, "reason": str(e)}
        _live_cache["ts"] = now
        _live_cache["data"] = data
    return jsonify(data)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        cfg = store.load_config()
        # Steuerungs-Parameter mit Defaults auffüllen, damit die UI Werte zeigt
        defaults = Params().__dict__
        for k, v in defaults.items():
            cfg.setdefault(k, v)
        cfg.setdefault("app_display_name", "Victron Steuerung")
        return jsonify(cfg)
    body = request.get_json(silent=True) or {}
    cfg = store.load_config()
    allowed = ["app_display_name", "cerbo_host", "cerbo_port", "tibber_token", "pv_latitude",
               "pv_longitude", "pv_planes", "dry_run", "poll_seconds",
               "energy_sample_seconds", "manual_override", "web_port",
               "chart_energy_hourly", "chart_flow_hourly", "openmeteo_pr",
               "show_live_values", "show_energy_chart", "show_flow_chart",
               "show_week_overview", "show_month_overview", "show_tibber_card",
               "show_override_card", "show_price_plan", "show_charge_log",
               "show_ev_card", "show_shelly_card", "surplus_enabled", "surplus_dry_run",
               "surplus_min_soc", "tile_order", "scan_networks", "solar_source"] + list(Params().__dict__.keys())
    allowed = allowed + ["surplus_" + k for k in surplus.DEFAULTS]     # einstellbare Automatik-Werte
    if "scan_networks" in body:
        try:
            body["scan_networks"] = ", ".join(tuya.parse_networks(body["scan_networks"]))
        except tuya.TuyaError as e:
            return jsonify(error=str(e)), 400
    if body.get("solar_source") not in (None, "auto", "openmeteo"):
        return jsonify(error="Ungültige Prognose-Quelle"), 400
    if not (isinstance(body.get("tile_order", []), list)
            and all(isinstance(k, str) for k in body.get("tile_order", []))):
        body.pop("tile_order", None)          # Kachelreihenfolge: nur Liste von Textschluesseln
    for key in allowed:
        if key in body:
            cfg[key] = body[key]
    store.save_config(cfg)
    # Sofort einen Regel-Durchlauf anstoßen, damit Preise/Status gleich erscheinen
    # (der periodische Thread schläft sonst bis zu poll_seconds).
    threading.Thread(target=ctrl.safe_tick, daemon=True).start()
    return jsonify({"ok": True, "configured": store.is_configured(cfg)})


@app.route("/api/grid-adjust", methods=["POST"])
def api_grid_adjust():
    """Setzt die heutigen Netzwerte manuell (z.B. aus der Victron-App), falls
    sie z.B. wegen einer Pause der App nicht vollstaendig erfasst wurden."""
    body = request.get_json(silent=True) or {}
    try:
        imp_today = float(body.get("import", 0))
        exp_today = float(body.get("export", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültige Zahlen"}), 400
    result = store.set_grid_today(imp_today, exp_today)
    with _live_lock:                                     # Live-Cache invalidieren
        _live_cache["ts"] = 0
    return jsonify({"ok": True, "grid_today": result})


def _under_process_manager():
    """True, wenn ein Prozessmanager die App bei Beenden neu startet
    (systemd Restart=always ODER pm2 autorestart). Dann kann sich die App
    zum Update selbst beenden und wird automatisch neu gestartet."""
    return bool(os.environ.get("INVOCATION_ID")      # systemd
                or os.environ.get("pm_id")            # pm2
                or os.environ.get("PM2_HOME"))


@app.route("/api/version")
def api_version():
    return jsonify({"version": updater.current_version(),
                    "under_systemd": _under_process_manager()})


@app.route("/api/check-update")
def api_check_update():
    return jsonify(updater.check_update())


@app.route("/api/update", methods=["POST"])
def api_update():
    result = updater.do_update()
    if result.get("ok"):
        # Unter systemd/pm2: sauber beenden -> Prozessmanager startet neu.
        if _under_process_manager():
            result["restarting"] = True

            def _restart():
                time.sleep(1.5)
                os._exit(0)
            threading.Thread(target=_restart, daemon=True).start()
        else:
            result["restarting"] = False   # manuell neu starten
    return jsonify(result)


@app.route("/api/override", methods=["POST"])
def api_override():
    body = request.get_json(silent=True) or {}
    cfg = store.load_config()
    cfg["manual_override"] = bool(body.get("value"))
    store.save_config(cfg)
    threading.Thread(target=ctrl.safe_tick, daemon=True).start()
    return jsonify({"manual_override": cfg["manual_override"]})


@app.route("/api/ev", methods=["POST"])
def api_ev_add():
    body = request.get_json(silent=True) or {}
    start, end = body.get("start"), body.get("end")
    if not start or not end:
        return jsonify({"error": "Start und Ende erforderlich"}), 400
    try:
        s, e = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except ValueError:
        return jsonify({"error": "Ungültiges Zeitformat"}), 400
    if e <= s:
        return jsonify({"error": "Ende muss nach Start liegen"}), 400
    entry = store.add_ev(s.isoformat(timespec="minutes"),
                         e.isoformat(timespec="minutes"), body.get("note", ""))
    threading.Thread(target=ctrl.safe_tick, daemon=True).start()
    return jsonify(entry), 201


@app.route("/api/ev/<eid>", methods=["DELETE", "PATCH"])
def api_ev_modify(eid):
    if request.method == "DELETE":
        # Nur noch nicht gestartete Termine dürfen gelöscht werden. Laufende
        # sind nur stoppbar (das Geladene bleibt in den Ladevorgängen erhalten).
        entry = next((i for i in store.list_ev() if i["id"] == eid), None)
        if not entry:
            return jsonify({"error": "nicht gefunden"}), 404
        try:
            started = datetime.fromisoformat(entry["start"]) <= datetime.now()
        except (ValueError, KeyError):
            started = False
        if started:
            return jsonify({"error": "bereits gestartet – nur stoppbar"}), 409
        store.delete_ev(eid)
        threading.Thread(target=ctrl.safe_tick, daemon=True).start()
        return jsonify({"ok": True})
    body = request.get_json(silent=True) or {}
    if body.get("action") == "stop":
        entry = store.stop_ev(eid)
        if not entry:
            return jsonify({"error": "nicht laufend"}), 400
        threading.Thread(target=ctrl.safe_tick, daemon=True).start()
        return jsonify(entry)
    entry = store.toggle_ev(eid, body.get("enabled", True))
    if not entry:
        return jsonify({"error": "nicht gefunden"}), 404
    return jsonify(entry)


def _scan_networks() -> list[str]:
    """Weitere Netze/VLANs fuer alle Geraetesuchen (Einstellung `scan_networks`; frueher am Tuya-Zugang gespeichert)."""
    cfg = store.load_config()
    text = cfg.get("scan_networks") or ", ".join(tuya.load_credentials().get("networks") or [])
    try:
        return tuya.parse_networks(text)
    except tuya.TuyaError:
        return []


@app.route("/api/shelly", methods=["GET"])
def api_shelly_list():
    """?dashboard=1: nur die in den Einstellungen fuers Dashboard freigegebenen."""
    return jsonify(shelly.list_with_status(only_shown=request.args.get("dashboard") == "1"))


@app.route("/api/shelly/auto", methods=["GET"])
def api_shelly_auto():
    cfg = store.load_config()
    return jsonify({"enabled": bool(cfg.get("surplus_enabled")),
                    "dry_run": bool(cfg.get("surplus_dry_run")),
                    "min_soc": cfg.get("surplus_min_soc", surplus.DEFAULT_MIN_SOC),
                    "settings": surplus.settings(cfg), "defaults": surplus.DEFAULTS,
                    "bounds": surplus.BOUNDS,
                    "events": surplus_ctrl.recent()})


@app.route("/api/shelly/auto-order", methods=["POST"])
def api_shelly_auto_order():
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return jsonify(error="ids fehlt"), 400
    shelly.reorder_auto(ids)
    return jsonify(ok=True)


@app.route("/api/vrm", methods=["GET"])
def api_vrm_info():
    return jsonify(vrm.credentials_public())           # ohne Token


@app.route("/api/vrm/credentials", methods=["POST"])
def api_vrm_credentials():
    body = request.get_json(silent=True) or {}
    try:
        vrm.save_credentials(body.get("installation_id"), body.get("token"))
    except vrm.VrmError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/vrm/restore", methods=["POST"])
def api_vrm_restore():
    """Fehlende Verlaufsdaten aus dem VRM nachholen. Ohne {"apply": true} nur Vorschau."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(vrm_import.run(apply=bool(body.get("apply"))))
    except vrm.VrmError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:                               # noqa: BLE001
        log.warning("VRM-Import fehlgeschlagen: %s", e)
        return jsonify(error=f"Import fehlgeschlagen: {e}"), 500


@app.route("/api/vrm/forecast", methods=["GET"])
def api_vrm_forecast():
    """Solar-Prognose aus dem VRM-Portal (?refresh=1 = Zwischenspeicher umgehen, z. B. beim Testen)."""
    return jsonify(vrm.forecast(force=request.args.get("refresh") == "1"))


@app.route("/api/tuya", methods=["GET"])
def api_tuya_info():
    return jsonify(tuya.credentials_public())          # ohne Secret


@app.route("/api/tuya/credentials", methods=["POST"])
def api_tuya_credentials():
    body = request.get_json(silent=True) or {}
    try:
        tuya.save_credentials(body.get("region"), body.get("api_key"), body.get("api_secret"),
                              body.get("networks"))
    except tuya.TuyaError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/tuya/scan", methods=["POST"])
def api_tuya_scan():
    """Tuya-Geraete: Schluessel aus der Cloud + Suche im LAN (dauert ca. 20-30 s)."""
    try:
        return jsonify(shelly.tuya_scan(_scan_networks()))
    except shelly.ShellyError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:                               # noqa: BLE001
        log.warning("Tuya-Suche fehlgeschlagen: %s", e)
        return jsonify(error=f"Suche fehlgeschlagen: {e}"), 500


@app.route("/api/tuya/add", methods=["POST"])
def api_tuya_add():
    dev_id = str((request.get_json(silent=True) or {}).get("dev_id") or "")
    try:
        entry = shelly.add_tuya(dev_id, str((request.get_json(silent=True) or {}).get("ip") or ""))
    except shelly.ShellyError as e:
        return jsonify(error=str(e)), 400
    return jsonify({"added": entry["id"]}), 201


@app.route("/api/shelly/icons", methods=["GET"])
def api_shelly_icons():
    return jsonify(shelly.ICONS)


@app.route("/api/shelly/order", methods=["POST"])
def api_shelly_order():
    ids = (request.get_json(silent=True) or {}).get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        return jsonify(error="ids fehlt"), 400
    shelly.reorder(ids)
    return jsonify(ok=True)


@app.route("/api/shelly/scan", methods=["POST"])
def api_shelly_scan():
    """Durchsucht das lokale /24-Netz nach Shelly-Geraeten (dauert ca. 2-5 s)."""
    try:
        return jsonify(shelly.discover(extra_networks=_scan_networks()))
    except Exception as e:                               # noqa: BLE001
        log.warning("Shelly-Scan fehlgeschlagen: %s", e)
        return jsonify(error=f"Suche fehlgeschlagen: {e}"), 500


@app.route("/api/tasmota/scan", methods=["POST"])
def api_tasmota_scan():
    """Durchsucht das lokale /24-Netz und die eingestellten weiteren Netze nach Tasmota-Geraeten."""
    try:
        return jsonify(shelly.tasmota_discover(extra_networks=_scan_networks()))
    except Exception as e:                               # noqa: BLE001
        log.warning("Tasmota-Scan fehlgeschlagen: %s", e)
        return jsonify(error=f"Suche fehlgeschlagen: {e}"), 500


@app.route("/api/shelly", methods=["POST"])
def api_shelly_add():
    body = request.get_json(silent=True) or {}
    try:
        added = shelly.add_by_ip(body.get("ip", ""), body.get("password", ""))
    except shelly.ShellyError as e:
        return jsonify(error=str(e)), 400
    return jsonify({"added": len(added)}), 201


@app.route("/api/shelly/<dev_id>", methods=["PATCH", "DELETE"])
def api_shelly_modify(dev_id):
    if request.method == "DELETE":
        ok = shelly.remove(dev_id)
    else:
        body = request.get_json(silent=True) or {}
        try:
            ok = shelly.update(dev_id, name=body.get("name"), icon=body.get("icon"),
                               show=body.get("show"), auto=body.get("auto"),
                               power_w=body.get("power_w"), min_on_min=body.get("min_on_min"),
                               min_off_min=body.get("min_off_min"),
                               switchable=body.get("switchable"))
        except shelly.ShellyError as e:
            return jsonify(error=str(e)), 400
    return jsonify(ok=True) if ok else (jsonify(error="nicht gefunden"), 404)


@app.route("/api/shelly/<dev_id>/switch", methods=["POST"])
def api_shelly_switch(dev_id):
    on = bool((request.get_json(silent=True) or {}).get("on"))
    try:
        result = shelly.set_state(dev_id, on, timer_s=0 if on else None)     # 0 = evtl. laufenden Auto-Timer aufheben
    except shelly.ShellyError as e:
        return jsonify(error=str(e)), 502
    surplus_ctrl.note_manual(dev_id, hold_min=surplus.settings(store.load_config())["manual_hold_min"])   # Automatik pausiert fuer dieses Geraet
    return jsonify(result)


@app.route("/api/test-connection", methods=["POST"])
def api_test():
    """Wizard: prüft Cerbo + Tibber mit den übergebenen Werten."""
    body = request.get_json(silent=True) or {}
    result = {"cerbo": None, "tibber": None}
    try:
        c = Cerbo(body.get("cerbo_host", ""), int(body.get("cerbo_port", 502)))
        result["cerbo"] = {"ok": True, "soc": round(c.read_soc(), 1),
                           "ess_mode": c.read_ess_mode()}
    except Exception as e:                               # noqa: BLE001
        result["cerbo"] = {"ok": False, "error": str(e)}
    try:
        p = fetch_tibber_prices(body.get("tibber_token", ""))
        result["tibber"] = {"ok": True, "slots": len(p)}
    except Exception as e:                               # noqa: BLE001
        result["tibber"] = {"ok": False, "error": str(e)}
    return jsonify(result)


def main():
    ctrl.start()
    cfg = store.load_config()
    # PORT-Umgebungsvariable hat Vorrang (pm2/systemd), sonst web_port aus Config
    port = int(os.environ.get("PORT", cfg.get("web_port", 5005)))
    log.info("Web-App startet auf Port %s (dry_run=%s)", port, cfg.get("dry_run"))
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=port, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
