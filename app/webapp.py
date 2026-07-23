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

from flask import Flask, jsonify, redirect, render_template, request

import os

import store
import updater
from datasources import PvForecast, fetch_tibber_prices
from logic import ESS_CHARGE, ESS_IDLE, Params, decide
from logic import _parse_iso as logic_parse_iso
from victron import Cerbo

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("webapp")

app = Flask(__name__)

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
        self._stop = threading.Event()

    def _pv_source(self, cfg):
        key = (cfg["pv_latitude"], cfg["pv_longitude"], str(cfg["pv_planes"]))
        if self._pv is None or self._pv_key != key:
            self._pv = PvForecast(cfg["pv_latitude"], cfg["pv_longitude"], cfg["pv_planes"])
            self._pv_key = key
        return self._pv

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
        try:
            solar_today, solar_tom = self._pv_source(cfg).get()
        except Exception as e:                               # noqa: BLE001
            solar_today = solar_tom = 0.0
            pv_note = f"PV-Prognose nicht verfügbar ({e}) - rechne mit 0 kWh"
            log.warning(pv_note)

        now = datetime.now()
        ev = store.active_ev(now)
        forced = bool(cfg.get("manual_override")) or ev is not None
        reason = "Manueller Ladetermin" if ev else "MANUELL"

        state = store.load_state()
        d = decide(soc=soc, price_entries=prices, solar_today_raw=solar_today,
                   solar_tom_raw=solar_tom, state=state, now=now,
                   manual_override=forced, force_reason=reason,
                   params=Params.from_config(cfg))
        store.save_state(state)
        store.log_charge_state(d.ess_mode == ESS_CHARGE, d.strategy, now)
        # Solar-Logbuch: Tages-Prognose (roh + korrigiert) einfrieren und
        # vergangene Tage mit dem realen Ertrag abschließen.
        if pv_note is None and solar_today > 0:
            store.record_solar_forecast(raw=solar_today, corr=d.solar_today_korr,
                                        factor=Params.from_config(cfg).pv_korrektur_faktor,
                                        now=now)

        dry = bool(cfg.get("dry_run", True))
        wrote = False
        if d.ess_mode != current_ess:
            wrote = cerbo.write_ess_mode(d.ess_mode, dry_run=dry)

        with self.lock:
            self.prices = self._prep_prices(prices, d)
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

    def _prep_prices(self, entries, decision):
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
            out.append({
                "start": start.isoformat(timespec="seconds"),
                "label": f"{start:%H:%M}",
                "ct": round(item["total"] * 100, 2),
                "level": item.get("level", "NORMAL"),
                "planned": start.isoformat(timespec="minutes") in planned,
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
                    store.log_energy_sample(system, datetime.now())
            except Exception as e:                       # noqa: BLE001
                log.warning("Energie-Sampler: %s", e)
            try:
                iv = max(3, int(store.load_config().get("energy_sample_seconds", 10)))
            except Exception:                            # noqa: BLE001
                iv = 10
            self._stop.wait(iv)

    def start(self):
        cfg = store.load_config()
        interval = int(cfg.get("poll_seconds", 300))
        threading.Thread(target=self.run, args=(interval,), daemon=True).start()
        threading.Thread(target=self.run_energy, daemon=True).start()


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
    data["current_factor"] = Params.from_config(cfg).pv_korrektur_faktor
    return jsonify(data)


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


def build_charge_overview(status, charge, prices):
    """Kombiniert geplante Ladefenster (aus dem Plan) mit tatsächlich geladenen
    (aus dem Protokoll) inkl. Menge und Kosten."""
    power_kw = (status.get("charge_power_w") or 3500) / 1000.0
    price_at = {p["start"][:16]: p["ct"] for p in prices}
    now_iso = datetime.now().isoformat(timespec="minutes")
    items = []
    for s in charge.get("sessions", []):
        items.append({"start": s["start"], "end": s["end"], "status": "geladen",
                      "strategy": s.get("strategy", ""),
                      **_window_stats(s["start"], s["end"], price_at, power_kw)})
    op = charge.get("open")
    if op:
        items.append({"start": op["start"], "end": None, "status": "läuft",
                      "strategy": op.get("strategy", ""),
                      **_window_stats(op["start"], now_iso, price_at, power_kw)})
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
               "chart_flow_hourly": bool(cfg.get("chart_flow_hourly", False))},
        "prices": prices,
        "ev_schedules": store.list_ev(),
        "charge_log": charge,
        "charge_overview": build_charge_overview(status, charge, prices),
        "energy_history": store.energy_history_today(),
        "energy_min_day": store.energy_min_day(),
        "last_tick": ctrl.last_tick,
        "last_error": ctrl.last_error,
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
        return jsonify(cfg)
    body = request.get_json(silent=True) or {}
    cfg = store.load_config()
    allowed = ["cerbo_host", "cerbo_port", "tibber_token", "pv_latitude",
               "pv_longitude", "pv_planes", "dry_run", "poll_seconds",
               "energy_sample_seconds", "manual_override", "web_port",
               "chart_energy_hourly", "chart_flow_hourly"] + list(Params().__dict__.keys())
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
    """Setzt die heutigen Netzwerte manuell (z.B. aus der Victron-App).
    Liest die aktuellen Gesamtzähler vom Cerbo und rechnet die Tages-Basis um."""
    body = request.get_json(silent=True) or {}
    try:
        imp_today = float(body.get("import", 0))
        exp_today = float(body.get("export", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültige Zahlen"}), 400
    cfg = store.load_config()
    try:
        cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
        tot = cerbo.read_system().get("grid_energy_total") or {}
    except Exception as e:                               # noqa: BLE001
        return jsonify({"error": f"Cerbo nicht erreichbar: {e}"}), 502
    result = store.set_grid_today(imp_today, exp_today,
                                  tot.get("import", 0), tot.get("export", 0))
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
