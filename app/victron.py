"""
Victron Cerbo GX Anbindung über Modbus TCP.
Lesen: SOC (BMS, Faktor 10) + ESS-Mode.  Schreiben: ESS-Mode (mit Dry-Run-Sperre).
"""
import logging

from pymodbus.client import ModbusTcpClient

log = logging.getLogger("victron")

# Register (verifiziert am Cerbo 192.168.2.241, 22.07.2026)
SOC_BMS_UNIT, SOC_BMS_REG = 225, 266      # Wert = %*10  -> /10
SOC_SYS_UNIT, SOC_SYS_REG = 100, 843      # Wert = %     (Gegencheck)
ESS_MODE_UNIT, ESS_MODE_REG = 100, 2900   # Holding: 9=laden, 10=idle


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

    def read_system(self):
        """Liest die aggregierten System-Werte (Unit 100) für die Live-Ansicht.
        Register per Discovery gegen die Victron-App verifiziert (22.07.2026)."""
        def sgn(v):
            return v - 65536 if v >= 32768 else v

        c = self._client()
        try:
            # Blöcke gezielt lesen (Lücken im Registerraum vermeiden):
            blk1 = _read_block(c, 811, 12, SOC_SYS_UNIT)  # PV-WR 811-813, Last 817-819, Netz 820-822
            blk2 = _read_block(c, 840, 7, SOC_SYS_UNIT)   # Batterie 840-846
            blk3 = _read_block(c, 850, 2, SOC_SYS_UNIT)   # PV-Ladegerät 850-851
            blk4 = _read_block(c, 2622, 12, SOC_SYS_UNIT)  # Netz-Energiezähler (uint32, Wh)
        finally:
            c.close()

        def u32(hi, lo):
            return blk4[hi] * 65536 + blk4[lo]
        grid_import = (u32(0, 1) + u32(2, 3) + u32(4, 5)) / 1000.0   # 2622/2624/2626
        grid_export = (u32(6, 7) + u32(8, 9) + u32(10, 11)) / 1000.0  # 2628/2630/2632

        pv_ac = [sgn(blk1[0]), sgn(blk1[1]), sgn(blk1[2])]          # 811/812/813
        load = [sgn(blk1[6]), sgn(blk1[7]), sgn(blk1[8])]          # 817/818/819
        grid = [sgn(blk1[9]), sgn(blk1[10]), sgn(blk1[11])]        # 820/821/822
        pv_dc = sgn(blk3[0])                                        # 850
        return {
            "grid": {"l1": grid[0], "l2": grid[1], "l3": grid[2], "total": sum(grid)},
            "loads": {"l1": load[0], "l2": load[1], "l3": load[2], "total": sum(load)},
            "pv_inverter": {"l1": pv_ac[0], "l2": pv_ac[1], "l3": pv_ac[2], "total": sum(pv_ac)},
            "pv_charger": pv_dc,
            "pv_charger_current": sgn(blk3[1]) / 10.0,
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

    def read_ess_mode(self):
        c = self._client()
        try:
            rr = _call(c.read_holding_registers, ESS_MODE_REG, ESS_MODE_UNIT)
            if rr.isError():
                raise IOError(f"ESS-Mode nicht lesbar: {rr}")
            return rr.registers[0]
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
