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
import logging
import threading
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, g, jsonify, redirect, render_template, request,
                   session, url_for)

import json
import os

import shelly
import store
import surplus
import tuya
import planner
import notify
import opslog
import price_cache
import report
import updater
import vrm
import vrm_import
import weather
from auth import UserError, UserStore, new_secret_key
from datasources import build_fixed_price_entries, fetch_tibber_prices
from logic import ESS_CHARGE, ESS_IDLE, Params, Slot, decide
from logic import _parse_iso as logic_parse_iso
from victron import Cerbo

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("webapp")
surplus_ctrl = surplus.SurplusController()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.secret_key = new_secret_key(os.path.join(BASE_DIR, "secret.key"))
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(store.load_config().get("cookie_secure", False)),
)

users = UserStore(os.path.join(BASE_DIR, "users.json"))


@app.context_processor
def inject_app_display_name():
    """Personalisierbarer Anzeigename (Kopfzeile/Titel) - fuer alle Templates
    verfuegbar, auch Login/Konto-Anlage (kein DB-Zugriff, nur die lokale Datei)."""
    return {"app_display_name": store.load_config().get("app_display_name") or "Victron Steuerung"}


PUBLIC_ENDPOINTS = {"login", "create_account", "static", "service_worker", "manifest"}

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
    if users.is_empty():
        if request.path.startswith("/api/"):
            return jsonify(error="Kein Konto eingerichtet.", setup_required=True), 401
        return redirect(url_for("create_account"))
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


@app.route("/create-account", methods=["GET", "POST"])
def create_account():
    """Einmaliger erster Schritt: legt den einzigen Admin-Zugang an, bevor
    irgendetwas anderes (auch /setup) erreichbar ist. Existiert bereits ein
    Konto, ist diese Route gesperrt - Aendern laeuft danach nur noch ueber
    die Einstellungen (mit aktuellem Passwort)."""
    if not users.is_empty():
        return redirect(url_for("login"))
    if request.method == "GET":
        return render_template("create_account.html", error=None)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    password2 = request.form.get("password2") or ""
    if password != password2:
        return render_template("create_account.html", error="Die Passwörter stimmen nicht überein."), 400
    try:
        users.create(username, password)
    except UserError as e:
        return render_template("create_account.html", error=str(e)), 400

    session.clear()
    session["user"] = username.strip().lower()
    session.permanent = True
    return redirect(url_for("setup"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if users.is_empty():
        return redirect(url_for("create_account"))
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
    """PWA-Manifest mit personalisiertem Namen, damit mehrere installierte
    Instanzen (eigene Anlage, Anlage der Mutter, ...) auf dem Homescreen
    unterscheidbar sind - statt bei allen "Victron Steuerung" zu zeigen."""
    path = os.path.join(app.static_folder, "manifest.webmanifest")
    with open(path, encoding="utf-8") as f:
        m = json.load(f)
    name = store.load_config().get("app_display_name") or "Victron Steuerung"
    m["name"] = name
    # Voller Name auch als Kurzname - eigenmaechtiges Abschneiden (z.B. auf
    # 12 Zeichen) reisst bei "Mamas Victron Steuerung" nur "Mamas" heraus.
    # Das Betriebssystem bricht/kuerzt lange Homescreen-Labels selbst sinnvoll.
    m["short_name"] = name
    resp = jsonify(m)
    resp.headers["Content-Type"] = "application/manifest+json"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.post("/api/account")
def api_update_own_account():
    """Eigenen Benutzernamen und/oder Passwort aendern. Beides optional, aber
    mindestens eines von beiden muss angegeben sein; das aktuelle Passwort
    wird immer verlangt."""
    body = request.json or {}
    if not users.verify(g.user, body.get("current") or ""):
        return jsonify(error="Aktuelles Passwort ist falsch."), 400
    new_username = (body.get("username") or "").strip()
    new_password = body.get("new") or ""
    if new_password and new_password != body.get("new2"):
        return jsonify(error="Die Passwörter stimmen nicht überein."), 400
    if not new_username and not new_password:
        return jsonify(error="Nichts zu ändern."), 400
    try:
        active = g.user
        if new_username and new_username.strip().lower() != active:
            users.rename(active, new_username)
            active = new_username.strip().lower()
        if new_password:
            users.update_password(active, new_password)
    except UserError as exc:
        return jsonify(error=str(exc)), 400
    session["user"] = active
    return jsonify(ok=True)


ESS_TEXT = {ESS_CHARGE: "Netzladen", ESS_IDLE: "Normal / Warten"}


class Controller:
    """Hintergrund-Regler: holt Daten, entscheidet, schreibt ESS-Mode.
    Hält den letzten Status im Speicher für die Web-UI."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = {"ok": False, "reason": "startet ..."}
        self.prices = []          # aufbereitete Slots für die Kurve
        self.plansim = {"available": False, "reason": "noch nicht berechnet"}   # Ladeplan-Simulation (nur Anzeige)
        self.last_tick = None
        self.last_error = None
        self.started_at = datetime.now().isoformat(timespec="seconds")
        self._tick_failed = False
        self._last_err_log = ("", 0.0)
        self._vrm_bad = False
        self._stop = threading.Event()
        self.last_system = None          # letzte Cerbo-Messung (vom Energie-Sampler)
        self.last_system_ts = 0.0
        self._surplus_dry_on: dict[str, bool] = {}   # Trockenlauf: gedachter Schaltzustand

    def tick(self):
        cfg = store.load_config()
        if not store.is_configured(cfg):
            with self.lock:
                self.status = {"ok": False, "reason": "nicht eingerichtet"}
            return
        cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
        soc = cerbo.read_soc()
        current_ess = cerbo.read_ess_mode()
        thr = int(cfg.get("notify_low_soc", notify.DEFAULT_LOW_SOC))
        notify.event("low_soc", soc < thr or (notify.is_active("low_soc") and soc < thr + 5),
                     f"🪫 Akku niedrig: {soc:.0f} % (Schwelle {thr} %).", f"🔋 Akku wieder bei {soc:.0f} %.", cfg=cfg)
        notify.daily_summary(cfg, datetime.now(), extra=self._report_line)
        try:
            system = cerbo.read_system(has_pv_inverter=cfg.get("has_pv_inverter", True),
                                        has_mppt=cfg.get("has_mppt", True))
        except Exception as e:                           # noqa: BLE001
            system = None
            log.warning("System-Werte nicht lesbar: %s", e)
        price_note = None
        if cfg.get("tariff_mode") == "fixed":
            prices = build_fixed_price_entries(cfg.get("fixed_price_ct", 32.0))
        else:
            prices, price_note = self._tibber_prices(cfg, cerbo, current_ess)
        pv_note = None

        now = datetime.now()
        ev = store.active_ev(now)
        forced = bool(cfg.get("manual_override")) or ev is not None
        reason = "Manueller Ladetermin" if ev else "MANUELL"

        # Prognose fuer die Regelung: Victron VRM kennt die reale Anlage (lernt aus dem Ertragsverlauf) und ist
        # die Quelle. Nur wenn es nicht eingerichtet/erreichbar/aktuell ist, rechnet die Regelung mit dem
        # Durchschnitt der echten Tageserträge der letzten Tage.
        measured_today = store.solar_measured_today(now)
        vrm_data = vrm_ctl = vrm_why = None
        try:
            vrm_data = vrm.forecast()
            vrm_ctl, vrm_why = vrm.control_forecast(vrm_data, now, measured_today)
        except Exception as e:                               # noqa: BLE001
            vrm_why = "PV-Prognose (VRM) nicht verfügbar – Rückfall auf Durchschnitt der letzten Tage"
            log.warning("VRM-Prognose fuer die Regelung fehlgeschlagen: %s", e)
        if bool(vrm_why) != self._vrm_bad:
            self._vrm_bad = bool(vrm_why)
            opslog.log("vrm", vrm_why if vrm_why else "VRM-Prognose wieder verfügbar")
        if vrm_why:
            notify.event("vrm", True, f"⚠️ {vrm_why}", "✅ Die VRM-Prognose ist wieder verfügbar.", after_min=30)
        elif vrm_ctl:
            notify.event("vrm", False, "", "✅ Die VRM-Prognose ist wieder verfügbar.")
        if vrm_ctl:
            pv_source = "VRM"
            solar_today_for_control = vrm_ctl["today_kwh"]
            solar_tom_ctl = vrm_ctl["tomorrow_kwh"]
            if solar_tom_ctl is None:                        # VRM hat (noch) nichts fuer morgen
                solar_tom_ctl = store.recent_solar_average(7, now) or 0.0
                pv_note = "VRM liefert noch keine Prognose für morgen – für morgen Ø der letzten Tage"
        else:
            avg = store.recent_solar_average(7, now)
            pv_source = "Ø letzte Tage"
            solar_today_for_control = round(max(measured_today, avg or 0.0), 2)
            solar_tom_ctl = avg or 0.0
            pv_note = vrm_why or "Kein VRM-Zugang eingerichtet (Einstellungen → VRM) – Prognose = Durchschnitt der letzten Tage"
            if vrm_why:
                log.warning(vrm_why)

        # Die VRM-Prognose ist schon anlagenkalibriert -> in decide() KEIN weiterer Korrekturfaktor.
        params = Params.from_config(cfg)
        params.pv_korrektur_faktor = 1.0
        state = store.load_state()
        d = decide(soc=soc, price_entries=prices, solar_today_raw=solar_today_for_control,
                   solar_tom_raw=solar_tom_ctl, state=state, now=now,
                   manual_override=forced, force_reason=reason,
                   params=params)
        store.save_state(state)
        store.log_charge_state(d.ess_mode == ESS_CHARGE, d.strategy, now)
        try:                                            # Simulation laeuft nur mit - sie steuert nichts und darf nie stoeren
            if cfg.get("tariff_mode") == "fixed":
                self.plansim = {"available": False, "reason": "Nur bei dynamischem Tarif."}
            else:
                self.plansim = self._run_plansim(now, soc, prices, d, vrm_data, params)
        except Exception as e:                          # noqa: BLE001
            log.warning("Ladeplan-Simulation fehlgeschlagen: %s", e)
            self.plansim = {"available": False, "reason": f"Simulation fehlgeschlagen: {e}"}
        # Solar-Logbuch: VRM-Tagesprognose einmal pro Tag einfrieren, vergangene Tage mit dem realen Ertrag abschließen.
        try:
            store.record_vrm_forecast(vrm_data["today_kwh"] if vrm_data and vrm_data.get("hours") else None, now)
        except Exception as e:                               # noqa: BLE001
            log.warning("Solar-Logbuch konnte nicht geschrieben werden: %s", e)
        try:
            store.record_forecast_hours(vrm_data, now)
        except Exception as e:                               # noqa: BLE001
            log.warning("VRM-Prognose (Stunden) konnte nicht gespeichert werden: %s", e)

        dry = bool(cfg.get("dry_run", True))
        wrote = False
        if d.ess_mode != current_ess:
            wrote = cerbo.write_ess_mode(d.ess_mode, dry_run=dry)
            opslog.count("ess_writes", now=now)
            opslog.log("ess", f"ESS-Modus {ESS_TEXT.get(current_ess, current_ess)} → {ESS_TEXT.get(d.ess_mode, d.ess_mode)} "
                       f"({d.strategy}; Preis {d.now_price} ct, Akku {soc:.0f} %)" + ("" if wrote else " [Trockenlauf: nicht geschrieben]" if dry else " [Schreiben fehlgeschlagen]"),
                       dry=dry, price=d.now_price, soc=round(soc, 1), strategy=d.strategy)

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
                "pv_source": pv_source,
                "dry_run": dry,
                "wrote": wrote,
                "ev_active": ev,
                "pv_note": " · ".join(x for x in (price_note, pv_note) if x) or None,
                "system": system,
                "override": bool(cfg.get("manual_override")),
                "tariff_mode": cfg.get("tariff_mode", "tibber"),
            }
        # Hinweis: Das Energie-Logging läuft in einem eigenen, feineren Takt
        # (run_energy / energy_sample_seconds), NICHT hier - sonst würde die
        # Trapez-Integration doppelt zählen.

        opslog.count("src_vrm" if pv_source == "VRM" else "src_avg", now=now)
        if cfg.get("tariff_mode") != "fixed":
            opslog.count("tibber_cache" if price_note else "tibber_live", now=now)
        if d.ess_mode == ESS_CHARGE:
            opslog.count("charge_ticks", now=now)

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

    # ------------------------------------------------------------ Ladeplan-Simulation (nur Anzeige)
    @staticmethod
    def _hour_map(hours, now):
        """VRM-Stundenwerte -> {(datum_iso, stunde): Wh}."""
        out = {}
        for h in hours or []:
            day = now.date() if h.get("day") == "today" else now.date() + timedelta(days=1)
            out[(day.isoformat(), int(h["hour"]))] = float(h["wh"])
        return out

    def _run_plansim(self, now, soc, prices, d, vrm_data, params):
        if not vrm_data or not vrm_data.get("hours"):
            return {"available": False, "reason": "Die Simulation braucht die VRM-Prognose (Einstellungen → VRM)."}
        solar = self._hour_map(vrm_data["hours"], now)
        cons = self._hour_map((vrm_data.get("cons") or {}).get("hours"), now)
        # eigene Slot-Liste: logic.build_slots dedupliziert nach Uhrzeit (nur 24 h) - der Planer braucht heute UND morgen
        now_q = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
        seen, slots = set(), []
        for item in prices:
            start = logic_parse_iso(item["startsAt"])
            if start >= now_q and start not in seen:
                seen.add(start)
                slots.append(Slot(name="", price=item["total"] * 100, start=start))
        slots.sort(key=lambda x: x.start)
        res = planner.run(now, soc, params, slots, solar, cons or None, {x.start for x in d.plan})
        if not res:
            return {"available": False, "reason": "Zu wenig Preis- oder Prognosedaten für eine Simulation."}
        try:
            store.record_plansim(now, res)
        except Exception as e:                              # noqa: BLE001
            log.warning("Simulations-Protokoll nicht schreibbar: %s", e)
        return {"available": True, "computed": now.isoformat(timespec="seconds"), "result": res,
                "cons_source": "VRM-Verbrauchsprognose" if cons else "Tagesverbrauch (Einstellung) / 24 h",
                "current_strategy": d.strategy}

    # ------------------------------------------------------------ Tibber-Preise mit Ausfallsicherung
    _price_fail_since = None
    PRICE_GRACE_MIN = 10          # so lange ohne brauchbare Preise, bevor ein laufendes Netzladen gestoppt wird

    def _tibber_prices(self, cfg, cerbo, current_ess):
        """Preise von Tibber. Faellt der Abruf aus, gelten die zuletzt geholten Preise weiter, solange sie die aktuelle
        Zeit abdecken. Gibt es keine brauchbaren mehr, wird ein laufendes Netzladen nach PRICE_GRACE_MIN gestoppt
        (sichere Rueckfallstufe) und der Durchlauf mit einer klaren Meldung beendet.
        Rueckgabe: (Preise, Hinweistext oder None)."""
        now = datetime.now()
        try:
            prices = fetch_tibber_prices(cfg["tibber_token"])
            if not prices:
                raise ValueError("Tibber lieferte keine Preise")
            if self._price_fail_since is not None:
                opslog.log("tibber", "Tibber-Preise wieder verfügbar")
            self._price_fail_since = None
            notify.event("tibber", False, "", "✅ Tibber-Preise sind wieder verfügbar.")
            try:
                store.record_prices(prices)                  # Preis-Historie (nur bei Aenderung wird geschrieben)
            except Exception as e:                           # noqa: BLE001
                log.warning("Preis-Historie nicht schreibbar: %s", e)
            try:
                price_cache.save(prices, now)
            except Exception as e:                           # noqa: BLE001
                log.warning("Preis-Zwischenspeicher nicht schreibbar: %s", e)
            return prices, None
        except Exception as e:                               # noqa: BLE001
            log.warning("Tibber-Preise nicht abrufbar: %s", e)
            if self._price_fail_since is None:
                opslog.log("tibber", f"Tibber-Abruf fehlgeschlagen: {e}")
            self._price_fail_since = self._price_fail_since or now
            cached = price_cache.load()
            if cached and price_cache.covers(cached["prices"], now):
                stand = str(cached.get("fetched", ""))[11:16]
                notify.event("tibber", True, f"⚠️ Tibber ist seit 20 Minuten nicht erreichbar – die Steuerung nutzt die gespeicherten Preise von {stand} Uhr.",
                             after_min=20)
                return cached["prices"], f"Tibber nicht erreichbar – nutze die Preise von {stand} Uhr"
            stopped = ""
            waited = (now - self._price_fail_since).total_seconds() / 60
            if current_ess == ESS_CHARGE and waited >= self.PRICE_GRACE_MIN:
                try:
                    cerbo.write_ess_mode(ESS_IDLE, dry_run=bool(cfg.get("dry_run", True)))
                    stopped = " – Netzladen wurde gestoppt"
                    opslog.log("tibber", f"Keine brauchbaren Preise seit {waited:.0f} min – Netzladen gestoppt (Ruhe-Modus)")
                    log.warning("Keine brauchbaren Strompreise seit %.0f min - Netzladen gestoppt (Ruhe-Modus)", waited)
                except Exception as e2:                      # noqa: BLE001
                    log.error("Netzladen konnte nicht gestoppt werden: %s", e2)
            notify.event("tibber", True, "⛔ Keine Strompreise verfügbar" + (" – das laufende Netzladen wurde gestoppt." if stopped else "."), after_min=0)
            raise RuntimeError(f"Keine Strompreise verfügbar ({e}){stopped}")

    def _report_line(self):
        """Kurzfassung des Betriebsberichts fuer die Tages-Zusammenfassung per Telegram."""
        try:
            r = report.build(days=1, ctrl=_ctrl_info())
        except Exception as e:                               # noqa: BLE001
            return f"🩺 Systemcheck nicht möglich: {e}"
        bad = [c for c in r["checks"] if c["status"] in ("fail", "warn")]
        if not bad:
            return "🩺 Systemcheck: ✅ läuft sauber"
        return "🩺 Systemcheck: " + ("❌ Probleme" if r["verdict"] == "fail" else "⚠️ Hinweise") + " – " + "; ".join(f"{c['title']}: {c['detail']}" for c in bad[:4])

    def safe_tick(self):
        """Tick mit Fehlerabfang - für Hintergrundschleife und On-Demand-Aufrufe."""
        try:
            self.tick()
            opslog.note_tick(True)
            if self._tick_failed:
                self._tick_failed = False
                opslog.log("tick_ok", "Steuerung läuft wieder normal")
            notify.event("tick_error", False, "", "✅ Die Steuerung läuft wieder normal.")
        except Exception as e:                           # noqa: BLE001
            self.last_error = str(e)
            with self.lock:
                self.status = {"ok": False, "reason": f"Fehler: {e}"}
            log.error("Tick fehlgeschlagen: %s", e)
            opslog.note_tick(False)
            self._tick_failed = True
            if str(e) != self._last_err_log[0] or time.time() - self._last_err_log[1] > 1800:      # nicht bei jedem Durchlauf wiederholen
                opslog.log("tick_error", str(e))
                self._last_err_log = (str(e), time.time())
            notify.event("tick_error", True, f"⚠️ Die Steuerung meldet seit 10 Minuten einen Fehler: {e}", after_min=10)

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
        if wd["just_warned"]:
            notify.push("watchdog", f"🔋 Batterie-Watchdog: Die Batterie reagiert seit 15 Minuten nicht (Netz {grid_w:.0f} W, Batteriestrom {batt_a:.2f} A, "
                                    f"Akku {soc:.0f} %). Möglicher Ladehänger am Multiplus – ggf. Anlage prüfen.")
            opslog.log("watchdog", f"Batterie reagiert nicht (Netz {grid_w:.0f} W, {batt_a:.2f} A, Akku {soc:.0f} %)")
        if wd["just_resolved"]:
            notify.push("watchdog", f"✅ Batterie-Watchdog: wieder normal nach {wd['just_resolved']['duration_min']:.0f} Minuten.")
            opslog.log("watchdog", f"wieder normal nach {wd['just_resolved']['duration_min']:.0f} Minuten")
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
                    system = cerbo.read_system(has_pv_inverter=cfg.get("has_pv_inverter", True),
                                                has_mppt=cfg.get("has_mppt", True))
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
        opslog.log("surplus", text, dry=dry)
        surplus_ctrl.log(text)
        notify.push("surplus", ("🧪 " if dry else "🔌 ") + text, cfg)      # auch im Trockenlauf (Text beginnt dann mit "(Trockenlauf)")

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
    return render_template("admin.html", cfg=store.load_config(), session_user_display=g.user_display)


@app.route("/solar-log")
def solar_log_page():
    return render_template("solar_log.html")


@app.route("/api/solar-log")
def api_solar_log():
    data = store.solar_log()
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
               # show_tibber_card wird bei festem Tarif zusaetzlich im Admin-UI
               # gesperrt (updateTariffFields), hier nur normal ausgelesen.
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
               "show_weather_card": bool(cfg.get("show_weather_card", True)),
               "show_plansim_card": bool(cfg.get("show_plansim_card", True)) and cfg.get("tariff_mode") != "fixed",
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
    cfg = store.load_config()
    return jsonify({
        **store.energy_week_summary(offset_weeks=offset),
        "tariff_mode": cfg.get("tariff_mode", "tibber"),
    })


@app.route("/api/month")
def api_month():
    """Monatsuebersicht: eine Zeile je Kalendermonat (Solar/Verbrauch/Netz/
    Autarkie/Kosten), aus dem dauerhaften Monats-Archiv (nicht auf die 35-Tage-
    Historie beschraenkt - siehe store.monthly_overview)."""
    cfg = store.load_config()
    return jsonify({
        **store.monthly_overview(),
        "tariff_mode": cfg.get("tariff_mode", "tibber"),
    })


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
            system = cerbo.read_system(has_pv_inverter=cfg.get("has_pv_inverter", True),
                                        has_mppt=cfg.get("has_mppt", True))
            pv_sources = []
            for src in cfg.get("pv_inverters") or []:
                try:
                    power = cerbo.read_pvinverter_power(int(src["unit"]))
                except Exception as e:                       # noqa: BLE001
                    power = None
                    log.warning("PV-Wechselrichter '%s' (Unit %s) nicht lesbar: %s",
                                src.get("name"), src.get("unit"), e)
                pv_sources.append({"name": src.get("name") or f"Unit {src.get('unit')}",
                                    "unit": src.get("unit"), "power": power})
            data = {"ok": True, "soc": round(cerbo.read_soc(), 1),
                    "ess_mode": cerbo.read_ess_mode(),
                    "system": system,
                    "pv_sources": pv_sources,
                    "has_pv_inverter": cfg.get("has_pv_inverter", True),
                    "has_mppt": cfg.get("has_mppt", True),
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
        cfg.setdefault("has_pv_inverter", True)
        cfg.setdefault("has_mppt", True)
        cfg.setdefault("tariff_mode", "tibber")
        cfg.setdefault("fixed_price_ct", 32.0)
        cfg.setdefault("pv_inverters", [])
        cfg.setdefault("app_display_name", "Victron Steuerung")
        return jsonify(cfg)
    body = request.get_json(silent=True) or {}
    cfg = store.load_config()
    allowed = ["app_display_name", "cerbo_host", "cerbo_port", "tibber_token", "dry_run", "poll_seconds",
               "energy_sample_seconds", "manual_override", "web_port",
               "chart_energy_hourly", "chart_flow_hourly",
               "show_live_values", "show_energy_chart", "show_flow_chart",
               "show_week_overview", "show_month_overview", "show_tibber_card",
               "show_override_card", "show_price_plan", "show_charge_log",
               "show_ev_card", "show_shelly_card", "show_weather_card", "show_plansim_card", "surplus_enabled", "surplus_dry_run",
               "surplus_min_soc", "tile_order", "scan_networks",
               "has_pv_inverter", "has_mppt", "tariff_mode",
               "fixed_price_ct", "pv_inverters"] + list(Params().__dict__.keys())
    allowed = allowed + ["surplus_" + k for k in surplus.DEFAULTS]     # einstellbare Automatik-Werte
    if "scan_networks" in body:
        try:
            body["scan_networks"] = ", ".join(tuya.parse_networks(body["scan_networks"]))
        except tuya.TuyaError as e:
            return jsonify(error=str(e)), 400
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


@app.route("/api/notify", methods=["GET"])
def api_notify_info():
    return jsonify({**notify.credentials_public(), **notify.settings_public(store.load_config())})


@app.route("/api/notify/credentials", methods=["POST"])
def api_notify_credentials():
    body = request.get_json(silent=True) or {}
    try:
        notify.save_credentials(body.get("token"), body.get("chat_id"))
    except notify.NotifyError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/notify/detect", methods=["POST"])
def api_notify_detect():
    """Chat-ID ermitteln: erst dem Bot in Telegram eine Nachricht schreiben, dann hier abfragen."""
    body = request.get_json(silent=True) or {}
    try:
        return jsonify(notify.detect_chats(body.get("token")))
    except notify.NotifyError as e:
        return jsonify(error=str(e)), 400


@app.route("/api/notify/settings", methods=["POST"])
def api_notify_settings():
    body = request.get_json(silent=True) or {}
    try:
        upd = notify.validate_settings(body)
    except notify.NotifyError as e:
        return jsonify(error=str(e)), 400
    cfg = store.load_config()
    if "notify_events" in upd:                               # Schalter zusammenfuehren, nicht ersetzen
        merged = dict(cfg.get("notify_events") or {})
        merged.update(upd.pop("notify_events"))
        upd["notify_events"] = merged
    cfg.update(upd)
    store.save_config(cfg)
    return jsonify(ok=True)


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    cfg = store.load_config()
    try:
        notify.send(f"[{cfg.get('app_display_name') or 'Victron Steuerung'}] ✅ Testnachricht – die Benachrichtigungen funktionieren.")
    except notify.NotifyError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/plan-sim", methods=["GET"])
def api_plan_sim():
    """Ladeplan-Simulation (nur Anzeige): letzter Lauf + Tages-Vergleich der letzten Tage."""
    with ctrl.lock:
        data = dict(ctrl.plansim)
    data["history"] = store.plansim_log()
    return jsonify(data)


@app.route("/api/prices/history", methods=["GET"])
def api_price_history():
    """Gespeicherte Tibber-Preise je Viertelstunde (?days=N, Standard 90) + Kurzinfo."""
    try:
        n = max(1, min(800, int(request.args.get("days", 90))))
    except ValueError:
        n = 90
    return jsonify({"info": store.price_history_info(), "days": store.price_history(n)})


def _ctrl_info():
    with ctrl.lock:
        st = dict(ctrl.status)
    return {"status": st, "last_tick": ctrl.last_tick, "last_system_ts": ctrl.last_system_ts, "started": ctrl.started_at,
            "plansim": getattr(ctrl, "plansim", None)}


@app.route("/report")
def report_page():
    return render_template("report.html")


@app.route("/api/report", methods=["GET"])
def api_report():
    """Betriebsbericht ("schlauer Zettel"). ?format=md liefert den kompakten Text zum Kopieren, sonst JSON."""
    try:
        days = max(1, min(30, int(request.args.get("days", 7))))
    except ValueError:
        days = 7
    rep_ = report.build(days=days, ctrl=_ctrl_info())
    if request.args.get("format") == "md":
        return app.response_class(report.to_markdown(rep_), mimetype="text/plain; charset=utf-8")
    return jsonify(rep_)


@app.route("/api/weather", methods=["GET"])
def api_weather():
    """Wettervorhersage fuer den Standort (nur Anzeige). ?refresh=1 umgeht den Zwischenspeicher."""
    return jsonify(weather.forecast(force=request.args.get("refresh") == "1"))


@app.route("/api/weather/location", methods=["GET", "POST"])
def api_weather_location():
    if request.method == "GET":
        return jsonify(weather.get_location())
    body = request.get_json(silent=True) or {}
    try:
        weather.save_location(body.get("lat"), body.get("lon"), body.get("name", ""))
    except weather.WeatherError as e:
        return jsonify(error=str(e)), 400
    return jsonify(ok=True)


@app.route("/api/weather/search", methods=["GET"])
def api_weather_search():
    try:
        return jsonify(weather.search_places(request.args.get("q", "")))
    except weather.WeatherError as e:
        return jsonify(error=str(e)), 400


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


@app.route("/api/vrm/forecast/history", methods=["GET"])
def api_vrm_forecast_history():
    """Gemerkte VRM-Stundenprognose eines vergangenen Tages (?day=YYYY-MM-DD) fuer die schraffierten Balken im Energieverlauf."""
    day = request.args.get("day", "")
    try:
        datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return jsonify(error="Ungültiger Tag."), 400
    return jsonify(store.forecast_hours_for_day(day))


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
        return jsonify(error=str(e)), 400      # nicht 502/504: Cloudflare ersetzt diese Antworten durch eine eigene Fehlerseite
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
    if body.get("tariff_mode") == "fixed":
        price = body.get("fixed_price_ct", 0)
        if price and float(price) > 0:
            result["tibber"] = {"ok": True, "slots": len(build_fixed_price_entries(price))}
        else:
            result["tibber"] = {"ok": False, "error": "Bitte einen Preis > 0 ct/kWh eintragen"}
        return jsonify(result)
    try:
        p = fetch_tibber_prices(body.get("tibber_token", ""))
        result["tibber"] = {"ok": True, "slots": len(p)}
    except Exception as e:                               # noqa: BLE001
        result["tibber"] = {"ok": False, "error": str(e)}
    return jsonify(result)


def main():
    ctrl.start()
    cfg = store.load_config()
    try:
        n = store.backfill_prices_from_history()
        if n:
            log.info("Preis-Historie: %d Slots aus dem Verlauf zurückgerechnet", n)
    except Exception as e:                               # noqa: BLE001
        log.warning("Preis-Historie-Nachtrag fehlgeschlagen: %s", e)
    notify.push("startup", "🔄 Die Steuerung wurde gestartet.", cfg)
    opslog.count("restarts")
    opslog.log("startup", "Steuerung gestartet")
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
