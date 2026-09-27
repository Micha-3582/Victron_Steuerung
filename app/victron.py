"""
Victron Cerbo GX Anbindung über Modbus TCP.
Lesen: SOC (BMS, Faktor 10) + ESS-Mode.  Schreiben: ESS-Mode (mit Dry-Run-Sperre).
"""
import logging
import time

from pymodbus.client import ModbusTcpClient

log = logging.getLogger("victron")

_last_grid_counter_warning = 0.0
_GRID_COUNTER_WARNING_INTERVAL = 600   # nur alle 10 Min erneut loggen (Live-/Sampler-Polling fragt sonst im Sekundentakt an)

# Register (verifiziert am Cerbo 192.168.2.241, 22.07.2026)
SOC_BMS_UNIT, SOC_BMS_REG = 225, 266      # Wert = %*10  -> /10
SOH_REG = 304                              # Alterungszustand (State of Health), Unit wie SOC_BMS_UNIT, Wert = %*10
SOC_SYS_UNIT, SOC_SYS_REG = 100, 843      # Wert = %     (Gegencheck)
ESS_MODE_UNIT, ESS_MODE_REG = 100, 2900   # Holding: 9=laden, 10=idle
GRID_SP_UNIT, GRID_SP_REG = 100, 2700     # Holding int16: ESS 'Sollwert Netz' (W); negativ = leicht einspeisen
MIN_SOC_UNIT, MIN_SOC_REG = 100, 2901     # Holding: ESS 'Minimaler SOC (es sei denn, Netz faellt aus)', Wert = %*10

# Alarme (aus der offiziellen Victron Modbus-TCP-Registerliste, github.com/victronenergy/dbus_modbustcp;
# Unit 227 am Cerbo 192.168.2.241 verifiziert 27.09.2026 ueber AC-Ausgang/Batteriespannung - siehe Git-Historie)
VEBUS_UNIT = 227                          # Cerbo GX VE.Bus-Port (Multiplus/Quattro)
ALARM_REGS = {                            # alle rein lesend (Input-Register), 0=Ok, 2=Alarm (teils 1=Warnung)
    "vebus_error": (VEBUS_UNIT, 32),          # 0=kein Fehler, sonst VE.Bus-Fehlercode 1-26
    "vebus_high_temp": (VEBUS_UNIT, 34),
    "vebus_low_battery": (VEBUS_UNIT, 35),
    "vebus_overload": (VEBUS_UNIT, 36),
    "vebus_grid_lost": (VEBUS_UNIT, 64),      # 0=Ok, 2=Netz weg (nicht 1)
    "battery_low_voltage": (SOC_BMS_UNIT, 268),
    "battery_high_voltage": (SOC_BMS_UNIT, 269),
    "battery_low_soc": (SOC_BMS_UNIT, 272),
    "battery_low_temp": (SOC_BMS_UNIT, 273),
    "battery_high_temp": (SOC_BMS_UNIT, 274),
    "battery_cell_imbalance": (SOC_BMS_UNIT, 322),
    "battery_internal_failure": (SOC_BMS_UNIT, 323),
}
ALARM_LABELS = {                          # Beschriftung fuer Telegram/Log, wenn der Wert != 0 ist (0=Ok)
    "vebus_error": "Multiplus/Quattro: VE.Bus-Fehler",
    "vebus_high_temp": "Multiplus/Quattro: Übertemperatur",
    "vebus_low_battery": "Multiplus/Quattro: Batterie zu niedrig",
    "vebus_overload": "Multiplus/Quattro: Überlast",
    "vebus_grid_lost": "Multiplus/Quattro: Netz weg",
    "battery_low_voltage": "Batterie: Spannung zu niedrig",
    "battery_high_voltage": "Batterie: Spannung zu hoch",
    "battery_low_soc": "Batterie: Ladestand zu niedrig (BMS-Alarm)",
    "battery_low_temp": "Batterie: Temperatur zu niedrig",
    "battery_high_temp": "Batterie: Temperatur zu hoch",
    "battery_cell_imbalance": "Batterie: Zellen-Ungleichgewicht",
    "battery_internal_failure": "Batterie: interner Fehler",
}


def _call(fn, address, unit):
    """Versionsrobust: neuere pymodbus nutzen device_id=, aeltere slave=."""
    try:
        return fn(address=address, count=1, device_id=unit)
    except TypeError:
        return fn(address=address, count=1, slave=unit)


def _write(fn, address, value, unit):
    try:
        return fn(address=address, value=value, device_id=unit)
    except TypeError:
        return fn(address=address, value=value, slave=unit)


def _read_block(client, address, count, unit):
    """Liest einen zusammenhängenden Registerblock (int16-Rohwerte)."""
    try:
        rr = client.read_input_registers(address=address, count=count, device_id=unit)
    except TypeError:
        rr = client.read_input_registers(address=address, count=count, slave=unit)
    if rr.isError():
        raise IOError(f"Block {address}+{count} nicht lesbar: {rr}")
    return rr.registers


class Cerbo:
    def __init__(self, host, port=502, timeout=5):
        self.host, self.port, self.timeout = host, port, timeout

    def _client(self):
        c = ModbusTcpClient(host=self.host, port=self.port, timeout=self.timeout)
        if not c.connect():
            raise ConnectionError(f"Cerbo {self.host}:{self.port} nicht erreichbar "
                                  f"(Modbus TCP aktiv?)")
        return c

    def read_soc(self):
        """SOC in % (float). Primär BMS (Faktor 10), Fallback System-SOC."""
        c = self._client()
        try:
            rr = _call(c.read_input_registers, SOC_BMS_REG, SOC_BMS_UNIT)
            if not rr.isError():
                return rr.registers[0] / 10.0
            log.warning("BMS-SOC nicht lesbar, nutze System-SOC")
            rr = _call(c.read_input_registers, SOC_SYS_REG, SOC_SYS_UNIT)
            if not rr.isError():
                return float(rr.registers[0])
            raise IOError(f"SOC nicht lesbar: {rr}")
        finally:
            c.close()

    def read_system(self, has_pv_inverter=True, has_mppt=True):
        """Liest die aggregierten System-Werte (Unit 100) für die Live-Ansicht.
        Register per Discovery gegen die Victron-App verifiziert (22.07.2026).
        `has_pv_inverter`/`has_mppt`: Anlagen ohne AC-PV-Wechselrichter bzw. ohne
        MPPT-Solarladeregler lassen den jeweiligen Block aus – sonst würde ein
        Registerblock gelesen, den es am Cerbo gar nicht gibt (Fehler/Fantasiewerte)."""
        def sgn(v):
            return v - 65536 if v >= 32768 else v

        c = self._client()
        try:
            # Blöcke gezielt lesen (Lücken im Registerraum vermeiden):
            blk1 = _read_block(c, 811, 12, SOC_SYS_UNIT)  # PV-WR 811-813, Last 817-819, Netz 820-822
            blk2 = _read_block(c, 840, 7, SOC_SYS_UNIT)   # Batterie 840-846
            blk3 = _read_block(c, 850, 2, SOC_SYS_UNIT) if has_mppt else None  # PV-Ladegerät 850-851
            try:
                # Nicht jede Cerbo-Konfiguration hat einen registrierten Netz-Zähler
                # (z.B. wenn kein separates Grid-Meter am Cerbo angemeldet ist) -
                # dann liefert das Gerät hier einen Modbus-Fehler (Exception Code 10).
                # Das darf nicht die kompletten Live-Werte (PV/Last/Batterie) mitreißen.
                blk4 = _read_block(c, 2622, 12, SOC_SYS_UNIT)  # Netz-Energiezähler (uint32, Wh)
            except Exception as e:                        # noqa: BLE001
                global _last_grid_counter_warning
                now_ts = time.monotonic()
                if now_ts - _last_grid_counter_warning > _GRID_COUNTER_WARNING_INTERVAL:
                    log.warning("Netz-Energiezähler (2622ff) nicht lesbar, setze auf 0: %s", e)
                    _last_grid_counter_warning = now_ts
                blk4 = [0] * 12
        finally:
            c.close()

        def u32(hi, lo):
            return blk4[hi] * 65536 + blk4[lo]
        grid_import = (u32(0, 1) + u32(2, 3) + u32(4, 5)) / 1000.0   # 2622/2624/2626
        grid_export = (u32(6, 7) + u32(8, 9) + u32(10, 11)) / 1000.0  # 2628/2630/2632

        pv_ac = [sgn(blk1[0]), sgn(blk1[1]), sgn(blk1[2])] if has_pv_inverter else [0, 0, 0]  # 811/812/813
        load = [sgn(blk1[6]), sgn(blk1[7]), sgn(blk1[8])]          # 817/818/819
        grid = [sgn(blk1[9]), sgn(blk1[10]), sgn(blk1[11])]        # 820/821/822
        pv_dc = sgn(blk3[0]) if blk3 is not None else 0             # 850
        return {
            "grid": {"l1": grid[0], "l2": grid[1], "l3": grid[2], "total": sum(grid)},
            "loads": {"l1": load[0], "l2": load[1], "l3": load[2], "total": sum(load)},
            "pv_inverter": {"l1": pv_ac[0], "l2": pv_ac[1], "l3": pv_ac[2], "total": sum(pv_ac)},
            "pv_charger": pv_dc,
            "pv_charger_current": (sgn(blk3[1]) / 10.0) if blk3 is not None else 0.0,
            "solar_total": sum(pv_ac) + pv_dc,
            "grid_energy_total": {"import": round(grid_import, 2), "export": round(grid_export, 2)},
            "battery": {
                "voltage": blk2[0] / 10.0,          # 840
                "current": sgn(blk2[1]) / 10.0,     # 841
                "power": sgn(blk2[2]),              # 842
                "soc": blk2[3],                     # 843
                "state": blk2[4],                   # 844 (0=idle,1=laden,2=entladen)
            },
        }

    def read_pvinverter_power(self, unit):
        """Liest die Gesamt-Wirkleistung (W) EINES einzelnen PV-Wechselrichter-Dienstes
        (com.victronenergy.pvinverter). Unit = die Geräte-Instanz (Deviceinstance) dieses
        Wechselrichters am Cerbo, NICHT die System-Unit 100 - jeder Wechselrichter (auch
        ein per Shelly/MQTT eingespeister) hat dort seine eigene Modbus-Unit-ID.
        Register 1052 lt. offizieller Victron Modbus-TCP-Registerliste
        (com.victronenergy.pvinverter, "Total Power", int32, 1 W, Pfad /Ac/Power)."""
        c = self._client()
        try:
            regs = _read_block(c, 1052, 2, unit)
        finally:
            c.close()
        raw = (regs[0] << 16) | regs[1]
        return raw - 2**32 if raw >= 2**31 else raw

    def read_ess_mode(self):
        c = self._client()
        try:
            rr = _call(c.read_holding_registers, ESS_MODE_REG, ESS_MODE_UNIT)
            if rr.isError():
                raise IOError(f"ESS-Mode nicht lesbar: {rr}")
            return rr.registers[0]
        finally:
            c.close()

    def read_soh(self):
        """Alterungszustand der Batterie (State of Health, %) - dieselbe Anzeige wie am Cerbo unter
        Batterie -> Alterungszustand. Nicht von jedem BMS unterstuetzt; dann None statt Fehler."""
        c = self._client()
        try:
            rr = _call(c.read_input_registers, SOH_REG, SOC_BMS_UNIT)
            if rr.isError():
                return None
            val = rr.registers[0]
            return None if val in (0, 65535) else val / 10.0     # 0/0xFFFF = 'nicht unterstuetzt' bei manchen BMS
        finally:
            c.close()

    def read_alarms(self):
        """Alarmregister von Multiplus/Quattro (VE.Bus) und Batterie (BMS), siehe ALARM_REGS.
        Liest jedes einzeln und gibt trotzdem alle uebrigen zurueck, wenn eins fehlschlaegt (z.B. falsche
        Unit-ID) - der Fehler steht dann als Text beim jeweiligen Schluessel unter 'errors'."""
        out, errors = {}, {}
        c = self._client()
        try:
            for name, (unit, reg) in ALARM_REGS.items():
                try:
                    rr = _call(c.read_input_registers, reg, unit)
                    if rr.isError():
                        raise IOError(str(rr))
                    out[name] = rr.registers[0]
                except Exception as e:                       # noqa: BLE001
                    errors[name] = str(e)
        finally:
            c.close()
        return {"values": out, "errors": errors}

    def read_min_soc(self):
        """Minimaler SOC (ESS, in %) - dieselbe Einstellung wie im VRM-Portal 'Minimaler SOC'."""
        c = self._client()
        try:
            rr = _call(c.read_holding_registers, MIN_SOC_REG, MIN_SOC_UNIT)
            if rr.isError():
                raise IOError(f"Minimaler SOC nicht lesbar: {rr}")
            return rr.registers[0] / 10.0
        finally:
            c.close()

    def write_min_soc(self, pct, dry_run=True):
        """Setzt den minimalen SOC (ganze %). Bei dry_run wird NICHT geschrieben."""
        if dry_run:
            log.info("[DRY-RUN] wuerde Minimalen SOC = %s %% schreiben (kein Schreibzugriff)", pct)
            return False
        c = self._client()
        try:
            rr = _write(c.write_register, MIN_SOC_REG, int(round(pct * 10)), MIN_SOC_UNIT)
            if rr.isError():
                raise IOError(f"Minimaler SOC schreiben fehlgeschlagen: {rr}")
            log.info("Minimaler SOC = %s %% geschrieben", pct)
            return True
        finally:
            c.close()

    def read_grid_setpoint(self):
        """ESS 'Sollwert Netz' in W (mit Vorzeichen)."""
        c = self._client()
        try:
            rr = _call(c.read_holding_registers, GRID_SP_REG, GRID_SP_UNIT)
            if rr.isError():
                raise IOError(f"Sollwert Netz nicht lesbar: {rr}")
            raw = rr.registers[0]
            return raw - 65536 if raw >= 32768 else raw
        finally:
            c.close()

    def write_grid_setpoint(self, watt, dry_run=True):
        """Setzt den Netz-Sollwert (ganze W, auch negativ). Bei dry_run wird NICHT geschrieben."""
        if dry_run:
            log.info("[DRY-RUN] wuerde Sollwert Netz = %s W schreiben (kein Schreibzugriff)", watt)
            return False
        c = self._client()
        try:
            rr = _write(c.write_register, GRID_SP_REG, int(watt) & 0xFFFF, GRID_SP_UNIT)
            if rr.isError():
                raise IOError(f"Sollwert Netz schreiben fehlgeschlagen: {rr}")
            log.info("Sollwert Netz = %s W geschrieben", watt)
            return True
        finally:
            c.close()

    def write_ess_mode(self, mode, dry_run=True):
        """Schreibt den ESS-Mode. Bei dry_run=True wird NICHT geschrieben,
        nur der beabsichtigte Wert protokolliert."""
        if dry_run:
            log.info("[DRY-RUN] wuerde ESS-Mode = %s schreiben (kein Schreibzugriff)", mode)
            return False
        c = self._client()
        try:
            rr = _write(c.write_register, ESS_MODE_REG, int(mode), ESS_MODE_UNIT)
            if rr.isError():
                raise IOError(f"ESS-Mode schreiben fehlgeschlagen: {rr}")
            log.info("ESS-Mode = %s geschrieben", mode)
            return True
        finally:
            c.close()
