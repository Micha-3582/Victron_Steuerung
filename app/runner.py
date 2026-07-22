#!/usr/bin/env python3
"""
Runner - verbindet alles: liest SOC vom Cerbo, holt Tibber-Preise und
PV-Prognose, ruft die V39.4-Logik und schreibt den ESS-Mode.

DRY-RUN (Standard): schreibt NICHTS an den Cerbo, protokolliert nur die
Entscheidung. So kann die App parallel zum ioBroker laufen und verglichen
werden, bevor sie die Steuerung übernimmt.

Nutzung:
  pip install pymodbus requests
  python runner.py --once        # ein Durchlauf, dann Ende
  python runner.py --loop        # alle poll_seconds (Standard 300s)

Scharfschalten: in config.json  "dry_run": false  setzen
(oder --live). Erst nach erfolgreichem Parallelvergleich!
"""
import argparse
import logging
import time
from datetime import datetime

from datasources import PvForecast, fetch_tibber_prices
from logic import Params, decide
from store import load_config, load_state, save_state
from victron import Cerbo

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("runner")


def run_once(cfg, cerbo, pv, dry_run):
    state = load_state()
    # 1) Daten sammeln
    soc = cerbo.read_soc()
    current_ess = cerbo.read_ess_mode()
    prices = fetch_tibber_prices(cfg["tibber_token"])
    solar_today, solar_tom = pv.get()

    # 2) Entscheiden
    d = decide(
        soc=soc, price_entries=prices,
        solar_today_raw=solar_today, solar_tom_raw=solar_tom,
        state=state, now=datetime.now(),
        manual_override=bool(cfg.get("manual_override", False)),
        params=Params.from_config(cfg),
    )
    save_state(state)

    # 3) Anwenden (oder Dry-Run)
    mode_tag = "DRY-RUN" if dry_run else "LIVE"
    log.info("[%s] SoC %.1f%% | ESS ist %s -> Ziel %s | %s | Plan: %s",
             mode_tag, soc, current_ess, d.ess_mode, d.reason, d.plan_windows or "-")
    if d.ess_mode != current_ess:
        cerbo.write_ess_mode(d.ess_mode, dry_run=dry_run)
    else:
        log.info("ESS-Mode unveraendert (%s), kein Schreiben noetig", current_ess)
    return d


def main():
    ap = argparse.ArgumentParser(description="Victron Standalone Runner")
    ap.add_argument("--once", action="store_true", help="ein Durchlauf")
    ap.add_argument("--loop", action="store_true", help="Dauerbetrieb")
    ap.add_argument("--live", action="store_true", help="Schreiben aktivieren (ueberschreibt dry_run)")
    args = ap.parse_args()

    cfg = load_config()
    dry_run = not (args.live or cfg.get("dry_run") is False)
    cerbo = Cerbo(cfg["cerbo_host"], cfg.get("cerbo_port", 502))
    pv = PvForecast(cfg["pv_latitude"], cfg["pv_longitude"], cfg["pv_planes"])
    interval = int(cfg.get("poll_seconds", 300))

    if dry_run:
        log.info("=== DRY-RUN: es wird NICHTS an den Cerbo geschrieben ===")
    else:
        log.warning("=== LIVE: der ESS-Mode wird aktiv gesteuert ===")

    if args.loop:
        while True:
            try:
                run_once(cfg, cerbo, pv, dry_run)
            except Exception as e:                     # noqa: BLE001
                log.error("Durchlauf fehlgeschlagen: %s", e)
            time.sleep(interval)
    else:
        run_once(cfg, cerbo, pv, dry_run)


if __name__ == "__main__":
    main()
