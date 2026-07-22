"""
Victron Standalone Steuerung - Entscheidungslogik
=================================================
1:1-Portierung von victron_steuerung_v39.4.js (ioBroker) nach Python.

REIN & TESTBAR: keine Hardware, kein Netz. Die Funktion decide() bekommt
alle Eingaben als Argumente plus einen persistenten State-Dict und gibt
die Entscheidung + aktualisierten State zurück. I/O (Modbus, Tibber, PV,
Speicherung) liegt außerhalb.

ESS-Mode:  9 = laden erlaubt · 10 = nicht laden (wie in V39.4).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# --- PARAMETER (aus V39.4) ---
PV_KORREKTUR_FAKTOR = 0.68
PV_TOM_MORNING_FACTOR = 0.15   # Saisonal: Winter 0.05 / Sommer 0.25
DAILY_USAGE_KWH = 30.0
BATTERY_USABLE_KWH = 24.0
PV_RESERVE_KWH = 5.0
CHARGE_POWER_W = 3500
HYSTERESE_SOC = 1.5

MORNING_PEAK_START = 7
MORNING_PEAK_END = 9
EVENING_PEAK_START = 19
EVENING_PEAK_END = 21
MIN_PEAK_SOC = 40
PEAK_AVOID_PRICE = 37.0
NIGHT_SAFETY_SOC = 30.0
TARGET_SAFE_SOC = 35.0

ESS_CHARGE = 9
ESS_IDLE = 10


@dataclass
class Params:
    """Alle anlagenspezifischen Steuerungs-Parameter. Defaults = V39.4 (Michael).
    Über die Web-App pro Anlage anpassbar."""
    battery_usable_kwh: float = BATTERY_USABLE_KWH
    daily_usage_kwh: float = DAILY_USAGE_KWH
    charge_power_w: int = CHARGE_POWER_W
    pv_reserve_kwh: float = PV_RESERVE_KWH
    pv_korrektur_faktor: float = PV_KORREKTUR_FAKTOR
    pv_tom_morning_factor: float = PV_TOM_MORNING_FACTOR
    hysterese_soc: float = HYSTERESE_SOC
    morning_peak_start: int = MORNING_PEAK_START
    morning_peak_end: int = MORNING_PEAK_END
    evening_peak_start: int = EVENING_PEAK_START
    evening_peak_end: int = EVENING_PEAK_END
    min_peak_soc: float = MIN_PEAK_SOC
    peak_avoid_price: float = PEAK_AVOID_PRICE
    night_safety_soc: float = NIGHT_SAFETY_SOC
    target_safe_soc: float = TARGET_SAFE_SOC

    @classmethod
    def from_config(cls, cfg: dict) -> "Params":
        """Baut Params aus einem Config-Dict; unbekannte/fehlende Felder = Default."""
        fields = cls().__dict__
        return cls(**{k: cfg[k] for k in fields if k in cfg and cfg[k] is not None})


@dataclass
class Slot:
    name: str          # "HH:MM-HH:MM"
    price: float       # ct/kWh
    start: datetime


@dataclass
class PersistentState:
    """Ersetzt die internen ioBroker-States (0_userdata.0.Victron.Intern_*)."""
    day_stamp: str = ""
    slots_charged: int = 0
    last_counted_slot: str = ""
    commit_slot: str = ""
    morning_bridge: bool = False
    night_buffer: bool = False


@dataclass
class Decision:
    allow_now: bool
    ess_mode: int
    now_slot: str
    now_price: float
    reason: str
    strategy: str
    balance: float
    plan: list = field(default_factory=list)      # gewählte Slots
    plan_windows: str = ""
    target_slots: int = 0
    solar_today_korr: float = 0.0
    solar_tom_korr: float = 0.0


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def _pad2(n: int) -> str:
    return f"{n:02d}"


def fmt_hm(d: datetime) -> str:
    return f"{_pad2(d.hour)}:{_pad2(d.minute)}"


def slot_name_from_date(d: datetime) -> str:
    e = d + timedelta(minutes=15)
    return f"{_pad2(d.hour)}:{_pad2(d.minute)}-{_pad2(e.hour)}:{_pad2(e.minute)}"


def build_slots(price_entries: list, now: datetime) -> list:
    """price_entries: [{'startsAt': iso, 'total': EUR/kWh}, ...] (heute + morgen).
    Filtert auf ab-jetzt und dedupliziert - wie readAllPrices() in V39.4."""
    now_q = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    slots: list[Slot] = []
    seen = set()
    for item in price_entries:
        start = _parse_iso(item["startsAt"])
        if start >= now_q:
            name = slot_name_from_date(start)
            if name not in seen:
                seen.add(name)
                slots.append(Slot(name=name, price=item["total"] * 100, start=start))
    slots.sort(key=lambda s: s.start)
    return slots


def _parse_iso(s: str) -> datetime:
    # Tibber liefert z.B. 2026-07-22T10:00:00.000+02:00 -> naive lokale Zeit nutzen
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def merge_into_windows(picked: list) -> str:
    if not picked:
        return ""
    s = sorted(picked, key=lambda x: x.start)
    windows = []
    win_start = s[0].start
    win_end = win_start + timedelta(minutes=15)
    for slot in s[1:]:
        if slot.start == win_end:
            win_end = win_end + timedelta(minutes=15)
        else:
            windows.append(f"{fmt_hm(win_start)}-{fmt_hm(win_end)}")
            win_start = slot.start
            win_end = win_start + timedelta(minutes=15)
    windows.append(f"{fmt_hm(win_start)}-{fmt_hm(win_end)}")
    return ", ".join(windows)


def calc_peak_protection(soc, hour_now, solar_today, solar_tom, p: Params):
    current_kwh = (soc / 100) * p.battery_usable_kwh
    min_peak_kwh = (p.min_peak_soc / 100) * p.battery_usable_kwh
    hours_to_peak = None
    peak_label = ""
    pv_until_peak = 0.0

    if hour_now < p.morning_peak_start:
        hours_to_peak = p.morning_peak_start - hour_now
        peak_label = "Morgen-Peak"
        pv_until_peak = solar_today * (0.0 if hour_now < 6 else 0.05)
    elif p.morning_peak_end <= hour_now < p.evening_peak_start:
        hours_to_peak = p.evening_peak_start - hour_now
        peak_label = "Abend-Peak"
        pv_factor = 0.50 if hour_now < 13 else (0.25 if hour_now < 16 else 0.05)
        pv_until_peak = solar_today * pv_factor
    elif hour_now >= p.evening_peak_end:
        hours_to_peak = (24 - hour_now) + p.morning_peak_start
        peak_label = "Morgen-Peak (morgen)"
        pv_until_peak = solar_tom * p.pv_tom_morning_factor

    if hours_to_peak is None:
        return False, 0.0, ""

    usage_until_peak = (p.daily_usage_kwh / 24) * hours_to_peak
    projected = current_kwh + pv_until_peak - usage_until_peak
    if projected < min_peak_kwh:
        return True, (min_peak_kwh - projected), f"Schutz vor {peak_label}"
    return False, 0.0, ""


# ---------------------------------------------------------------------------
# Hauptentscheidung (Port von recalcPlanAndApply)
# ---------------------------------------------------------------------------
def decide(soc: float, price_entries: list, solar_today_raw: float,
           solar_tom_raw: float, state: PersistentState,
           now: datetime | None = None,
           manual_override: bool = False,
           force_reason: str = "MANUELL",
           params: Params | None = None) -> Decision:
    p = params or Params()
    now = now or datetime.now()
    stamp = f"{now.year}-{_pad2(now.month)}-{_pad2(now.day)}"

    # --- Day-Reset ---
    if state.day_stamp != stamp:
        state.day_stamp = stamp
        state.slots_charged = 0
        state.last_counted_slot = ""
        state.commit_slot = ""
        state.morning_bridge = False
        state.night_buffer = False

    # --- Manual Override / erzwungenes Laden (z.B. E-Auto-Termin) ---
    if manual_override:
        return Decision(allow_now=True, ess_mode=ESS_CHARGE,
                        now_slot="", now_price=0.0, reason=force_reason,
                        strategy=force_reason, balance=0.0)

    slots_all = build_slots(price_entries, now)
    if not slots_all:
        return Decision(allow_now=False, ess_mode=ESS_IDLE, now_slot="",
                        now_price=0.0, reason="Keine Preisdaten",
                        strategy="", balance=0.0)

    now_q = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    now_slot_name = slot_name_from_date(now_q)
    hour_now = now.hour

    prices = [s.price for s in slots_all]
    avg_price = sum(prices) / len(prices)
    min_price = min(prices)
    now_price = next((s.price for s in slots_all if s.name == now_slot_name), avg_price)

    solar_today = round(solar_today_raw * p.pv_korrektur_faktor, 2)
    solar_tom = round(solar_tom_raw * p.pv_korrektur_faktor, 2)

    # --- Gesamtbilanz bis morgen Mittag ---
    pv_rem_factor = 0.75 if hour_now < 10 else (0.50 if hour_now < 13 else (0.25 if hour_now < 16 else 0.0))
    expected_pv_rest = (solar_today * pv_rem_factor) + (solar_tom * p.pv_tom_morning_factor)
    tomorrow_noon = (now + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    hours_to_bridge = max(1, (tomorrow_noon - now).total_seconds() / 3600)
    current_kwh = (soc / 100) * p.battery_usable_kwh
    total_need_kwh = (p.daily_usage_kwh / 24) * hours_to_bridge
    balance = (current_kwh + expected_pv_rest) - total_need_kwh

    # === SCHRITT 1: BEDARF ===
    grid_need = 0.0
    strategy = ""
    slot_filter = None

    # Prio 1: Peak-Schutz
    needs, kwh, reason = calc_peak_protection(soc, hour_now, solar_today, solar_tom, p)
    if needs:
        grid_need = kwh
        strategy = reason
        if hour_now < p.morning_peak_start:
            slot_filter = lambda s: s.start.hour < p.morning_peak_start
        elif p.morning_peak_end <= hour_now < p.evening_peak_start:
            slot_filter = lambda s: s.start.hour < p.evening_peak_start
        else:
            slot_filter = lambda s: s.start.hour < p.morning_peak_start or s.start.hour >= p.evening_peak_end

    # Prio 2: Nacht-Puffer (22-06) mit Hysterese
    if strategy == "" and (hour_now >= 22 or hour_now < 6):
        night = state.night_buffer
        if soc <= (p.night_safety_soc - p.hysterese_soc) and balance < 0:
            night = True
        elif soc >= (p.night_safety_soc + p.hysterese_soc):
            night = False
        state.night_buffer = night
        if night:
            night_target = p.night_safety_soc + p.hysterese_soc
            grid_need = max(0.0, (night_target - soc) / 100 * p.battery_usable_kwh)
            strategy = "Nacht-Puffer"
            slot_filter = lambda s: s.start.hour >= 22 or s.start.hour < 6

    # Prio 3: Morgen-Brücke (00-09)
    if strategy == "" and hour_now < 9:
        bridge = state.morning_bridge
        if soc <= (p.target_safe_soc - p.hysterese_soc):
            bridge = True
        elif soc >= p.target_safe_soc:
            bridge = False
        state.morning_bridge = bridge
        if bridge:
            grid_need = max(0.0, (p.target_safe_soc - soc) / 100 * p.battery_usable_kwh)
            strategy = "Morgen-Brücke"
            slot_filter = lambda s: s.start.hour < 9

    # Prio 4: Tiefpreis-Sicherung
    if strategy == "" and balance < 0:
        grid_need = abs(balance)
        strategy = "Tiefpreis-Sicherung"
        slot_filter = None

    # === SCHRITT 2: PLANUNG ===
    max_charge = max(0.0, (p.battery_usable_kwh - p.pv_reserve_kwh) - current_kwh)
    final_grid_need = min(grid_need, max_charge)
    needed_slots = math.ceil((final_grid_need / (p.charge_power_w / 1000.0)) * 4)

    candidates = [s for s in slots_all if slot_filter(s)] if slot_filter else slots_all
    picked = sorted(candidates, key=lambda s: s.price)[:needed_slots]

    # === SCHRITT 3: AUSFÜHRUNG ===
    plan_says_yes = any(pk.name == now_slot_name for pk in picked)
    allow_now = plan_says_yes

    # Notbremse mit Hysterese (nutzt night_buffer als gemeinsamen Zustand)
    emerg_on = p.min_peak_soc - 10
    emerg_off = p.min_peak_soc
    emergency = state.night_buffer
    if soc <= emerg_on and now_price <= p.peak_avoid_price:
        emergency = True
    if soc >= emerg_off:
        emergency = False
    state.night_buffer = emergency

    if not allow_now and emergency:
        allow_now = True
        strategy = f"Notbremse ({int(soc)}% < {int(emerg_off)}%)"

    # Preislimit für Standard-Strategien
    if allow_now and "Peak" not in strategy and "Notbremse" not in strategy:
        if strategy == "Nacht-Puffer":
            exec_limit = avg_price * 1.1
        else:
            exec_limit = min_price + (avg_price - min_price) * 0.25
        if now_price > exec_limit:
            allow_now = False
            strategy = f"Warte auf günstig ({strategy})"

    # Commitment
    committed = state.commit_slot
    if committed != "" and committed != now_slot_name:
        committed = ""
        state.commit_slot = ""
    if committed == now_slot_name:
        allow_now = True

    # Slot-Zähler
    if plan_says_yes and state.last_counted_slot != now_slot_name:
        state.slots_charged += 1
        state.last_counted_slot = now_slot_name
        state.commit_slot = now_slot_name

    ess_mode = ESS_CHARGE if allow_now else ESS_IDLE
    reason_text = f"{strategy or 'Idle'} | Bal: {balance:.1f}kWh | SoC: {soc}%"

    return Decision(
        allow_now=allow_now, ess_mode=ess_mode, now_slot=now_slot_name,
        now_price=round(now_price, 3), reason=reason_text,
        strategy=strategy or "Idle", balance=round(balance, 2),
        plan=picked, plan_windows=merge_into_windows(picked),
        target_slots=needed_slots, solar_today_korr=solar_today,
        solar_tom_korr=solar_tom,
    )
