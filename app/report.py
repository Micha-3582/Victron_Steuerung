"""
Betriebsbericht ("schlauer Zettel"): fasst auf einen Blick zusammen, ob die Anlage sauber laeuft, und liefert dieselben Daten
kompakt als Text zum Kopieren (fuer die gemeinsame Auswertung).

`build()` sammelt Pruefungen (ok / Warnung / Fehler / Info), eine Tagestabelle, die jüngsten Ereignisse und die wichtigsten
Einstellungen. Jeder Abschnitt faengt seine Fehler selbst ab - ein kaputter Abschnitt darf den Rest nicht verhindern.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta

import notify
import opslog
import store
import vrm

ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
ROUTINE: set = set()                                         # Ereignisarten, die im Bericht ausgeblendet werden sollen


def _check(key: str, title: str, status: str, detail: str) -> dict:
    return {"key": key, "title": title, "status": status, "detail": detail}


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:                                   # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def _version() -> str:
    for p in (os.path.join(store._DIR, "..", "VERSION"), os.path.join(store._DIR, "VERSION")):
        try:
            with open(p, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "?"


def _age_min(iso: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 60.0
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- Pruefungen
def _checks(now: datetime, cfg: dict, ctrl: dict, stats: dict, hist_days: dict) -> list[dict]:
    out: list[dict] = []
    poll = int(cfg.get("poll_seconds", 300) or 300)
    today, yday = now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()
    st = ctrl.get("status") or {}

    # 1) laeuft die Regelung?
    age = _age_min(ctrl.get("last_tick"), now)
    if age is None:
        out.append(_check("tick", "Regelung läuft", "fail", "noch kein erfolgreicher Durchlauf seit dem Start"))
    elif age > (2 * poll + 60) / 60:
        out.append(_check("tick", "Regelung läuft", "fail", f"letzter Durchlauf vor {age:.0f} min (Intervall {poll // 60} min)"))
    else:
        out.append(_check("tick", "Regelung läuft", "ok", f"letzter Durchlauf vor {age:.0f} min"))
    if st and st.get("ok") is False:
        out.append(_check("status", "Steuerungs-Status", "fail", str(st.get("reason") or "Fehler")))

    # 2) Fehler / Luecken
    e_today = stats.get(today, {}).get("ticks_err", 0)
    e_yday = stats.get(yday, {}).get("ticks_err", 0)
    ok_today = stats.get(today, {}).get("ticks_ok", 0)
    lvl = "ok" if e_today == 0 and e_yday == 0 else ("warn" if e_today + e_yday <= 3 else "fail")
    out.append(_check("errors", "Durchlauf-Fehler", lvl, f"heute {e_today} (von {ok_today + e_today}), gestern {e_yday}"))
    gaps = [(d, s.get("max_gap_s", 0)) for d, s in stats.items() if d >= (now.date() - timedelta(days=7)).isoformat()]
    big = [(d, g) for d, g in gaps if g > 3 * poll]
    out.append(_check("gaps", "Unterbrechungen (7 Tage)", "ok" if not big else "warn",
                      "keine" if not big else "; ".join(f"{d[5:]}: {g / 60:.0f} min ohne Durchlauf" for d, g in big[-4:])))
    restarts = sum(s.get("restarts", 0) for d, s in stats.items() if d >= (now.date() - timedelta(days=7)).isoformat())
    out.append(_check("restarts", "Neustarts (7 Tage)", "ok" if restarts <= 3 else "warn", str(restarts)))

    # 3) Betriebsart
    dry = bool(cfg.get("dry_run", True))
    out.append(_check("dry_run", "Cerbo wird geschaltet", "warn" if dry else "ok",
                      "TROCKENLAUF: die Steuerung rechnet nur, geschrieben wird nichts" if dry else "ja (dry_run aus)"))

    # 4) Prognose (VRM)
    v = _safe(vrm.credentials_public)
    if not v.get("configured"):
        out.append(_check("vrm", "VRM-Prognose", "warn", "VRM nicht eingerichtet – Steuerung nutzt den Ø der letzten Tage"))
    else:
        week = [stats[d] for d in stats if d >= (now.date() - timedelta(days=7)).isoformat()]
        n_v = sum(s.get("src_vrm", 0) for s in week)
        n_a = sum(s.get("src_avg", 0) for s in week)
        share = n_v / (n_v + n_a) * 100 if n_v + n_a else None
        now_src = st.get("pv_source")
        lvl = "ok" if now_src == "VRM" and (share is None or share >= 95) else "warn"
        out.append(_check("vrm", "VRM-Prognose", lvl, f"aktuell: {now_src or '?'}; Anteil VRM an den Durchläufen (7 Tage): "
                          + (f"{share:.0f} %" if share is not None else "–")))

    # 5) Tibber
    week = [stats[d] for d in stats if d >= (now.date() - timedelta(days=7)).isoformat()]
    n_live, n_cache = sum(s.get("tibber_live", 0) for s in week), sum(s.get("tibber_cache", 0) for s in week)
    if n_live + n_cache:
        share = n_cache / (n_live + n_cache) * 100
        out.append(_check("tibber", "Tibber-Preise", "ok" if share < 2 else "warn",
                          f"live {n_live}, aus Zwischenspeicher {n_cache} Durchläufe (7 Tage)"))
    cur = hist_days.get(today) or {}
    n_today = sum(1 for x in (cur.get("p") or []) if x is not None)
    tom = hist_days.get((now.date() + timedelta(days=1)).isoformat()) or {}
    n_tom = sum(1 for x in (tom.get("p") or []) if x is not None)
    if n_today >= 90 and (now.hour < 14 or n_tom >= 90):
        out.append(_check("prices", "Preis-Historie", "ok", f"heute {n_today}/96" + (f", morgen {n_tom}/96" if n_tom else "")))
    else:
        out.append(_check("prices", "Preis-Historie", "warn", f"heute {n_today}/96, morgen {n_tom}/96"))

    # 6) Vollstaendigkeit der Messdaten
    try:
        hours = store._load_history().get("hours", {})
        n_y = sum(1 for k in hours if k[:10] == yday)
        lvl = "ok" if n_y >= 90 else ("warn" if n_y >= 70 else "fail")
        out.append(_check("history", "Messdaten gestern", lvl, f"{n_y}/96 Viertelstunden"))
        n_days = len({k[:10] for k in hours})
        out.append(_check("history_span", "Verlauf vorhanden", "info", f"{n_days} Tage in history.json"))
    except Exception as e:                                   # noqa: BLE001
        out.append(_check("history", "Messdaten gestern", "fail", str(e)))
    ls = ctrl.get("last_system_ts") or 0
    if ls:
        a = (now.timestamp() - ls) / 60
        out.append(_check("sampler", "Energie-Messung", "ok" if a < 2 else "fail", f"letzte Messung vor {a:.1f} min"))

    # 7) Dateien + Sicherungen + Platz
    files = {}
    for name in ("history.json", "monthly_summary.json", "solar_log.json", "config.json", "state.json", "price_history.json"):
        p = os.path.join(store._DIR, name)
        try:
            with open(p, encoding="utf-8") as f:
                json.load(f)
            files[name] = "ok"
        except FileNotFoundError:
            files[name] = "fehlt"
        except (OSError, ValueError):
            files[name] = "DEFEKT"
    bad = {k: v for k, v in files.items() if v == "DEFEKT"}
    missing = [k for k, v in files.items() if v == "fehlt"]
    out.append(_check("files", "Datendateien", "fail" if bad else "ok",
                      "defekt: " + ", ".join(bad) if bad else "alle lesbar" + (f" (nicht vorhanden: {', '.join(missing)})" if missing else "")))
    try:
        newest = max(os.path.getmtime(os.path.join(store.BACKUP_DIR, f)) for f in os.listdir(store.BACKUP_DIR))
        a = (now.timestamp() - newest) / 3600
        out.append(_check("backups", "Tagessicherung (App)", "ok" if a < 30 else "warn", f"jüngste Kopie vor {a:.0f} h"))
    except (OSError, ValueError):
        out.append(_check("backups", "Tagessicherung (App)", "warn", "noch keine Kopie im Ordner backups"))
    try:
        free = shutil.disk_usage(store._DIR).free / 1e9
        out.append(_check("disk", "Speicherplatz", "ok" if free > 2 else ("warn" if free > 0.5 else "fail"), f"{free:.1f} GB frei"))
    except OSError:
        pass

    # 8) Batterie-Watchdog
    try:
        wd = store.battery_watchdog_state()
        evs = [e for e in (wd.get("events") or []) if (e.get("start") or "") >= (now - timedelta(days=7)).isoformat()]
        act = bool(wd.get("active"))
        out.append(_check("watchdog", "Batterie-Watchdog", "fail" if act else ("warn" if evs else "ok"),
                          "läuft gerade!" if act else (f"{len(evs)} Ereignis(se) in 7 Tagen" if evs else "keine Ereignisse")))
    except Exception:                                        # noqa: BLE001
        pass

    # 9) Benachrichtigungen
    if notify.configured():
        failed = sum(1 for e in opslog.recent(500, {"notify_fail"}, now - timedelta(days=7)))
        out.append(_check("notify", "Telegram", "ok" if not failed else "warn", "eingerichtet" + (f", {failed} Sendefehler (7 Tage)" if failed else "")))
    else:
        out.append(_check("notify", "Telegram", "info", "nicht eingerichtet"))

    # 10) Prognosegenauigkeit (VRM-Prognose vs. Ertrag) fertiger Tage
    try:
        rows = [r for r in store.solar_log(now)["rows"] if r.get("date", "") < today and r.get("vrm_forecast") and r.get("actual") is not None][:7]
        if rows:
            dev = [abs(r["actual"] - r["vrm_forecast"]) / r["vrm_forecast"] * 100 for r in rows]
            avg = sum(dev) / len(dev)
            out.append(_check("forecast", "Solar-Prognose (VRM) vs. Ertrag", "ok" if avg <= 30 else "warn", f"Ø Abweichung {avg:.0f} % über {len(rows)} Tage"))
    except Exception:                                        # noqa: BLE001
        pass

    # 11) Ueberschuss-Automatik
    import rules
    if rules.enabled(cfg):
        acts = opslog.recent(300, {"surplus"}, now - timedelta(days=7))
        real = sum(1 for e in acts if not e.get("dry"))
        out.append(_check("surplus", "Geräte-Regeln", "info",
                          f"aktiv, {'TROCKENLAUF' if rules.dry_run(cfg) else 'scharf'}; {len([x for x in rules.list_rules() if x.get('enabled', True)])} Regel(n); Aktionen 7 Tage: {len(acts)} (davon echt {real})"))
    return out


# ---------------------------------------------------------------- Tagestabelle
def _day_rows(now: datetime, days: int, stats: dict, hist_days: dict) -> list[dict]:
    summ = {d["day"]: d for d in store.energy_week_summary(now, days=days)["days"]}
    sl = {r["date"]: r for r in store.solar_log(now)["rows"]}
    ps = {}
    try:
        ps = {r["date"]: r for r in store.plansim_log(60)}
    except Exception:                                        # noqa: BLE001
        pass
    hours = store._load_history().get("hours", {})
    charged: dict[str, float] = {}
    for k, b in hours.items():
        charged[k[:10]] = charged.get(k[:10], 0.0) + b.get("g_batt", 0.0)
    rows = []
    for day in sorted(summ, reverse=True):
        s, o = summ[day], stats.get(day, {})
        p = [x for x in (hist_days.get(day, {}).get("p") or []) if x is not None]
        imp = s["import"]
        rows.append({
            "date": day, "solar": s["solar"], "verbrauch": s["verbrauch"], "import": imp, "export": s["export"], "autarky": s["autarky"],
            "cost_eur": s["cost_eur"], "avg_import_ct": round(s["cost_eur"] * 100 / imp, 1) if imp > 0.5 and s["cost_eur"] else None,
            "charged_kwh": round(charged.get(day, 0.0), 1),
            "price_min": round(min(p), 1) if p else None, "price_avg": round(sum(p) / len(p), 1) if p else None, "price_max": round(max(p), 1) if p else None,
            "vrm_forecast": (sl.get(day) or {}).get("vrm_forecast"), "vrm_dev_pct": (sl.get(day) or {}).get("vrm_deviation_pct"),
            "vrm_history": (sl.get(day) or {}).get("vrm_history") or [],
            "ticks_ok": o.get("ticks_ok"), "ticks_err": o.get("ticks_err"), "charge_ticks": o.get("charge_ticks"), "ess_writes": o.get("ess_writes"),
            "src_vrm": o.get("src_vrm"), "src_avg": o.get("src_avg"),
            "plan_diff_ct": round(ps[day]["current_net"] - ps[day]["sim_net"], 1) if day in ps else None,
        })
    return rows


# ---------------------------------------------------------------- Gesamtbericht
def build(days: int = 7, ctrl: dict | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    ctrl = ctrl or {}
    cfg = store.load_config()
    stats = opslog.stats_days(60)
    hist_days = _safe(store.price_history, 60)
    if "_error" in hist_days:
        hist_days = {}
    checks = _safe(_checks, now, cfg, ctrl, stats, hist_days)
    if isinstance(checks, dict):
        checks = [_check("report", "Prüfungen", "fail", checks["_error"])]
    rows = _safe(_day_rows, now, max(1, min(days, 30)), stats, hist_days)
    if isinstance(rows, dict):
        rows = []
    events = [e for e in opslog.recent(80, None, now - timedelta(days=days)) if e.get("kind") not in ROUTINE][:40]
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    verdict = "fail" if fails else ("warn" if warns else "ok")
    plan = ctrl.get("plansim") or {}
    sv = _safe(store.savings, cfg, now)
    cal = _safe(store.pv_calibration, now)
    settings = {k: cfg.get(k) for k in ("battery_usable_kwh", "daily_usage_kwh", "charge_power_w", "pv_reserve_kwh", "max_charge_soc", "absolute_cheap_price",
                                        "pv_tom_morning_factor", "min_peak_soc", "night_safety_soc", "target_safe_soc", "hysterese_soc",
                                        "peak_avoid_price", "poll_seconds", "dry_run", "rules_enabled", "rules_dry_run", "surplus_enabled", "surplus_dry_run", "surplus_min_soc", "tariff_mode")
                if cfg.get(k) is not None}
    return {"generated": now.isoformat(timespec="seconds"), "app": cfg.get("app_display_name") or "Victron Steuerung", "version": _version(),
            "started": ctrl.get("started"), "verdict": verdict, "checks": checks, "days": rows, "events": events, "settings": settings,
            "current": {"soc": (ctrl.get("status") or {}).get("soc"), "strategy": (ctrl.get("status") or {}).get("strategy"),
                        "ess": (ctrl.get("status") or {}).get("ess_text"), "pv_source": (ctrl.get("status") or {}).get("pv_source"),
                        "pv_today": (ctrl.get("status") or {}).get("pv_today"), "pv_tom": (ctrl.get("status") or {}).get("pv_tom"),
                        "plan_windows": (ctrl.get("status") or {}).get("plan_windows"), "now_price": (ctrl.get("status") or {}).get("now_price")},
            "savings": sv if "_error" not in sv else None,
            "pv_cal": ({**cal, "enabled": bool(cfg.get("pv_auto_calibration"))} if "_error" not in cal else None),
            "planner": ({"available": True, "sim_ct": plan["result"]["sim"]["cost_ct"], "current_ct": plan["result"]["current"]["cost_ct"],
                         "sim_net": plan["result"]["sim"]["net_ct"], "current_net": plan["result"]["current"]["net_ct"],
                         "sim_end_soc": plan["result"]["sim"]["end_soc"], "current_end_soc": plan["result"]["current"]["end_soc"],
                         "horizon_end": plan["result"]["horizon_end"],
                         "sim_windows": plan["result"]["sim"]["windows"], "current_windows": plan["result"]["current"]["windows"]}
                        if plan.get("available") else None)}


def _f(v, fmt="{:.1f}", dash="–"):
    return dash if v is None else fmt.format(v)


def to_markdown(r: dict) -> str:
    """Kompakter Text: passt in eine Chat-Nachricht und enthaelt alles fuer die Auswertung."""
    verdict = {"ok": "✅ LÄUFT SAUBER", "warn": "⚠️ LÄUFT, ABER MIT HINWEISEN", "fail": "❌ PROBLEME"}[r["verdict"]]
    L = [f"# Betriebsbericht – {r['app']}", f"Stand {r['generated']} · Version {r['version']} · gestartet {r.get('started') or '?'}", "", f"## Ergebnis: {verdict}", ""]
    for c in r["checks"]:
        L.append(f"- {ICON[c['status']]} **{c['title']}**: {c['detail']}")
    cur = r.get("current") or {}
    L += ["", "## Jetzt", f"SOC {_f(cur.get('soc'), '{:.0f}')} % · ESS {cur.get('ess') or '–'} · Strategie {cur.get('strategy') or '–'} · Preis {_f(cur.get('now_price'), '{:.1f}')} ct · "
          f"Sonne heute/morgen {_f(cur.get('pv_today'))}/{_f(cur.get('pv_tom'))} kWh ({cur.get('pv_source') or '?'}) · Plan: {cur.get('plan_windows') or 'kein Netzladen'}"]
    if r.get("planner"):
        p = r["planner"]
        w = lambda ws: ", ".join(f"{x['from']}-{x['to']}" for x in ws) or "kein Netzladen"
        diff = p["current_net"] - p["sim_net"]
        L.append(f"Planer (Test), Horizont bis {p['horizon_end'][5:16].replace('T', ' ')}: Simulation lädt [{w(p['sim_windows'])}] → Netzbezug {p['sim_ct'] / 100:.2f} €, Akku-Endstand {p['sim_end_soc']:.0f} % · "
                 f"bisherige Steuerung [{w(p['current_windows'])}] → {p['current_ct'] / 100:.2f} €, Endstand {p['current_end_soc']:.0f} % · "
                 f"Unterschied inkl. Akku-Restwert: {'Simulation' if diff >= 0 else 'bisherige Steuerung'} {abs(diff) / 100:.2f} € günstiger")
    sv = r.get("savings")
    if sv and sv.get("available"):
        t = sv["totals"]
        part = lambda k: f"{t[k]['saving_eur']:.2f} € (Eigenversorgung {t[k]['self_eur']:.2f}, Preis-Timing {t[k]['timing_eur']:.2f})" if t.get(k) else "–"
        L.append(f"Ersparnis ggü. „alles aus dem Netz“: heute {part('today')} · 7 Tage {part('d7')} [{t['d7']['days']} Tage] · gesamt {t['all']['saving_eur']:.2f} € über {t['all']['days']} Tage")
    pc = r.get("pv_cal")
    if pc:
        L.append(("PV-Kalibrierung: Faktor %.2f (Ø %+d %%) aus %d Tagen – %s" % (pc["factor"], pc["avg_dev_pct"], pc["days"], "ANGEWENDET" if pc["enabled"] else "nicht aktiv"))
                 if pc.get("ready") else f"PV-Kalibrierung: sammelt Tage ({pc['days']}/{pc['min_days']}), {'aktiviert' if pc['enabled'] else 'nicht aktiviert'}")
    L += ["", "## Tage (neueste zuerst)", "| Tag | Solar | Verbr. | Bezug | Einsp. | Autark | Kosten € | Ø Bezugspreis | Geladen kWh | Preis min/Ø/max | VRM-Prog. (Abw.) | Durchl. ok/Fehler | Lade-Durchl. | ESS-Wechsel | Plan-Vorteil ct |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for d in r["days"]:
        L.append(f"| {d['date'][5:]} | {_f(d['solar'])} | {_f(d['verbrauch'])} | {_f(d['import'])} | {_f(d['export'])} | {_f(d['autarky'], '{:.0f}')} % | {_f(d['cost_eur'], '{:.2f}')} | "
                 f"{_f(d['avg_import_ct'])} ct | {_f(d['charged_kwh'])} | {_f(d['price_min'], '{:.0f}')}/{_f(d['price_avg'], '{:.0f}')}/{_f(d['price_max'], '{:.0f}')} | "
                 f"{_f(d['vrm_forecast'])} ({_f(d['vrm_dev_pct'], '{:+.0f}')} %) | {d['ticks_ok'] if d['ticks_ok'] is not None else '–'}/{d['ticks_err'] if d['ticks_err'] is not None else '–'} | "
                 f"{d['charge_ticks'] if d['charge_ticks'] is not None else '–'} | {d['ess_writes'] if d['ess_writes'] is not None else '–'} | {_f(d['plan_diff_ct'], '{:+.0f}')} |")
    vh = [d for d in r["days"] if len(d.get("vrm_history") or []) > 1]
    if vh:
        L += ["", "## VRM-Tagesprognose: Nachjustierungen (Uhrzeit Wert kWh)"]
        for d in vh:
            L.append(f"- {d['date'][5:]}: " + " → ".join(f"{t} {v:.1f}" for t, v in d["vrm_history"]) + f" (Ertrag {_f(d['solar'])})")
    L += ["", "## Ereignisse (neueste zuerst)"]
    if r["events"]:
        for e in r["events"][:30]:
            L.append(f"- {e['ts'][5:16].replace('T', ' ')} [{e['kind']}] {e['text']}")
    else:
        L.append("- keine bemerkenswerten Ereignisse")
    L += ["", "## Einstellungen", ", ".join(f"{k}={v}" for k, v in r["settings"].items())]
    return "\n".join(L) + "\n"
