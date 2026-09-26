"""
Benachrichtigungen aufs Handy per Telegram-Bot.

Zugang (Bot-Token + Chat-ID) liegt in `telegram.json` (nicht im Repo, Token geht nie zum Browser). Ereignisse werden nur
bei einem WECHSEL gemeldet (Problem beginnt / Problem behoben) und nicht bei jedem Durchlauf; der Zustand steht in
`notify_state.json`, damit ein Neustart nicht dieselbe Meldung nochmal ausloest. Jede Meldung beginnt mit dem Anlagennamen,
damit man mehrere Anlagen (z. B. Original und Fork) im selben Chat auseinanderhalten kann.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime

import requests

import store

log = logging.getLogger("notify")

_DIR = os.path.dirname(os.path.abspath(__file__))
CRED_PATH = os.path.join(_DIR, "telegram.json")
STATE_PATH = os.path.join(_DIR, "notify_state.json")
API = "https://api.telegram.org"
TIMEOUT = 10

# key -> (Beschriftung, Standard an?)
EVENTS = {
    "tick_error": ("Steuerung meldet einen Fehler (z. B. Cerbo oder Tibber nicht erreichbar)", True),
    "tibber": ("Tibber-Preise fehlen (Notlauf mit gespeicherten Preisen / Netzladen gestoppt)", True),
    "vrm": ("VRM-Prognose nicht verfügbar", True),
    "watchdog": ("Batterie-Watchdog schlägt an (Batterie reagiert nicht)", True),
    "low_soc": ("Akkustand niedrig", True),
    "surplus": ("Überschuss-Automatik schaltet ein Gerät", False),
    "summary": ("Tages-Zusammenfassung am Abend", False),
    "startup": ("App wurde gestartet / neu gestartet", False),
}
DEFAULT_LOW_SOC = 15
DEFAULT_SUMMARY_HOUR = 21

_lock = threading.Lock()


class NotifyError(Exception):
    pass


# ---------------------------------------------------------------- Zugang
def load_credentials() -> dict:
    try:
        with open(CRED_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def configured() -> bool:
    c = load_credentials()
    return bool(c.get("token") and c.get("chat_id"))


def save_credentials(token: str | None, chat_id) -> None:
    """Leerer Token = vorhandenen behalten."""
    c = load_credentials()
    tok = str(token or "").strip() or c.get("token", "")
    cid = str(chat_id or "").strip()
    if not tok:
        raise NotifyError("Bitte den Bot-Token eintragen")
    if not cid.lstrip("-").isdigit():
        raise NotifyError("Die Chat-ID besteht nur aus Ziffern (ggf. mit Minus bei Gruppen) – „Chat-ID ermitteln“ hilft")
    store._dump_json(CRED_PATH, {"token": tok, "chat_id": cid}, indent=None)


def credentials_public() -> dict:
    c = load_credentials()
    return {"configured": bool(c.get("token") and c.get("chat_id")), "has_token": bool(c.get("token")),
            "chat_id": c.get("chat_id", "")}


# ---------------------------------------------------------------- Telegram-Aufrufe
def _api(token: str, method: str, payload: dict | None = None) -> dict:
    try:
        r = requests.post(f"{API}/bot{token}/{method}", json=payload or {}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise NotifyError(f"Telegram nicht erreichbar: {e}")
    try:
        j = r.json()
    except ValueError:
        raise NotifyError(f"Ungültige Antwort von Telegram (HTTP {r.status_code})")
    if not j.get("ok"):
        desc = j.get("description") or f"HTTP {r.status_code}"
        if r.status_code == 401:
            desc = "Bot-Token wird von Telegram abgelehnt"
        raise NotifyError(desc)
    return j


def send(text: str, token: str | None = None, chat_id=None) -> None:
    c = load_credentials()
    token, chat_id = token or c.get("token"), chat_id or c.get("chat_id")
    if not (token and chat_id):
        raise NotifyError("Telegram ist noch nicht eingerichtet")
    _api(token, "sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def detect_chats(token: str | None = None) -> list[dict]:
    """Chats, die dem Bot zuletzt geschrieben haben (nach einer Nachricht an den Bot)."""
    token = (token or load_credentials().get("token") or "").strip()
    if not token:
        raise NotifyError("Bitte zuerst den Bot-Token eintragen")
    j = _api(token, "getUpdates", {"limit": 50})
    seen: dict = {}
    for u in j.get("result", []):
        m = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
        chat = m.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) or chat.get("username") or str(chat["id"])
            seen[chat["id"]] = {"id": chat["id"], "name": name, "type": chat.get("type", "")}
    return list(seen.values())


# ---------------------------------------------------------------- Zustand
def _state() -> dict:
    d = store._load_json_recovering(STATE_PATH, lambda: {"events": {}})
    if not isinstance(d, dict):
        d = {"events": {}}
    d.setdefault("events", {})
    return d


def enabled(key: str, cfg: dict | None = None) -> bool:
    cfg = cfg if cfg is not None else store.load_config()
    flags = cfg.get("notify_events") or {}
    return bool(flags.get(key, EVENTS[key][1]))


def settings_public(cfg: dict) -> dict:
    return {"events": [{"key": k, "label": lab, "enabled": enabled(k, cfg), "default": dflt} for k, (lab, dflt) in EVENTS.items()],
            "low_soc": int(cfg.get("notify_low_soc", DEFAULT_LOW_SOC)),
            "summary_hour": int(cfg.get("notify_summary_hour", DEFAULT_SUMMARY_HOUR))}


def validate_settings(body: dict) -> dict:
    out = {}
    if "events" in body:
        if not isinstance(body["events"], dict):
            raise NotifyError("Ungültige Ereignisliste")
        out["notify_events"] = {k: bool(v) for k, v in body["events"].items() if k in EVENTS}
    for key, cfgkey, lo, hi in (("low_soc", "notify_low_soc", 3, 60), ("summary_hour", "notify_summary_hour", 0, 23)):
        if key in body:
            try:
                v = int(body[key])
            except (TypeError, ValueError):
                raise NotifyError(f"{key}: keine ganze Zahl")
            if not lo <= v <= hi:
                raise NotifyError(f"{key}: erlaubt sind {lo} bis {hi}")
            out[cfgkey] = v
    return out


def _prefix(cfg: dict) -> str:
    return f"[{cfg.get('app_display_name') or 'Victron Steuerung'}] "


def _send_async(text: str):
    def run():
        try:
            send(text)
        except NotifyError as e:
            log.warning("Telegram-Meldung nicht gesendet: %s", e)
        except Exception as e:                              # noqa: BLE001
            log.warning("Telegram-Meldung nicht gesendet: %s", e)
    threading.Thread(target=run, daemon=True).start()


def push(key: str, text: str, cfg: dict | None = None) -> bool:
    """Einmalige Meldung (kein Zustand). True, wenn sie abgeschickt wurde."""
    cfg = cfg if cfg is not None else store.load_config()
    if not (configured() and enabled(key, cfg)):
        return False
    _send_async(_prefix(cfg) + text)
    return True


def is_active(key: str) -> bool:
    with _lock:
        return key in _state()["events"]


def event(key: str, active: bool, text_on: str, text_off: str | None = None, *, cfg: dict | None = None,
          after_min: float = 0, flag: str | None = None, now: datetime | None = None) -> None:
    """Zustandsmeldung: sendet `text_on` einmal, sobald das Problem `after_min` Minuten am Stueck besteht, und `text_off`
    (falls angegeben) einmal, wenn es behoben ist. `flag` = welcher Schalter in den Einstellungen gilt (Standard: key)."""
    cfg = cfg if cfg is not None else store.load_config()
    flag = flag or key
    if flag not in EVENTS:
        return
    now = now or datetime.now()
    send_text = None
    can = configured() and enabled(flag, cfg)      # nur als "gemeldet" markieren, wenn die Meldung auch wirklich rausgeht
    with _lock:
        st = _state()
        ev = st["events"].get(key)
        changed = False
        if active:
            if ev is None:
                ev = st["events"][key] = {"since": now.isoformat(timespec="seconds"), "sent": False}
                changed = True
            if not ev["sent"]:
                since = datetime.fromisoformat(ev["since"])
                if can and (now - since).total_seconds() >= after_min * 60:
                    ev["sent"] = True
                    changed = True
                    send_text = text_on
        elif ev is not None:
            if ev.get("sent") and text_off:
                send_text = text_off
            del st["events"][key]
            changed = True
        if changed:
            try:
                store._dump_json(STATE_PATH, st, indent=None)
            except OSError as e:
                log.warning("Meldungs-Zustand nicht speicherbar: %s", e)
    if send_text and can:
        _send_async(_prefix(cfg) + send_text)


def daily_summary(cfg: dict, now: datetime) -> None:
    """Abends einmal die Tagesbilanz (ab notify_summary_hour)."""
    if not (configured() and enabled("summary", cfg)):
        return
    hour = int(cfg.get("notify_summary_hour", DEFAULT_SUMMARY_HOUR))
    if now.hour < hour:
        return
    with _lock:
        st = _state()
        if st.get("summary_date") == now.date().isoformat():
            return
        st["summary_date"] = now.date().isoformat()
        store._dump_json(STATE_PATH, st, indent=None)
    try:
        day = store.energy_week_summary(now)["days"][-1]
    except Exception as e:                                  # noqa: BLE001
        log.warning("Tages-Zusammenfassung nicht berechenbar: %s", e)
        return
    aut = f" · Autarkie {day['autarky']:.0f} %" if day.get("autarky") is not None else ""
    _send_async(_prefix(cfg) + f"📊 Tagesbilanz {now:%d.%m.}: Solar {day['solar']:.1f} kWh · Verbrauch {day['verbrauch']:.1f} kWh · "
                f"Netzbezug {day['import']:.1f} kWh · Einspeisung {day['export']:.1f} kWh{aut}"
                + (f" · Netzkosten {day['cost_eur']:.2f} €" if day.get("cost_eur") else ""))
