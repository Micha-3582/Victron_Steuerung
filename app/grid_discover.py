#!/usr/bin/env python3
"""
Grid-Meter Discovery
====================
Sucht den Victron Grid-Meter-Service (com.victronenergy.grid) auf dem Cerbo
und liest die kumulierten Netz-Energiezähler (kWh seit Anlagenstart).
Damit bauen wir später „Zum Netz / Aus dem Netz" als Tageswerte.

Grid-Meter-Register (laut Victron, FC03/holding):
  2600/2601/2602  aktive Leistung L1/L2/L3 (W, signed)
  2616            Spannung L1 (V, ÷10)
  2622/2624/2626  Bezug (import) L1/L2/L3 (uint32, Wh)
  2628/2630/2632  Einspeisung (export) L1/L2/L3 (uint32, Wh)

  python grid_discover.py
"""
import json
import os
import sys

from pymodbus.client import ModbusTcpClient


def cfg_host():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return json.load(f).get("cerbo_host")
    return None


def rd(client, addr, count, unit):
    try:
        rr = client.read_holding_registers(address=addr, count=count, device_id=unit)
    except TypeError:
        rr = client.read_holding_registers(address=addr, count=count, slave=unit)
    return None if rr.isError() else rr.registers


def u32(regs):
    return regs[0] * 65536 + regs[1]


def sgn(v):
    return v - 65536 if v >= 32768 else v


def main():
    host = cfg_host()
    if not host:
        sys.exit("Kein Host in config.json.")
    c = ModbusTcpClient(host=host, port=502, timeout=5)
    if not c.connect():
        sys.exit(f"Cerbo {host} nicht erreichbar.")

    print("Suche Grid-Meter (Unit-ID mit gültigem Register 2600) ...")
    found = []
    for unit in range(0, 250):
        regs = rd(c, 2600, 1, unit)
        if regs is not None:
            found.append(unit)
            volt = rd(c, 2616, 1, unit)
            v = (volt[0] / 10.0) if volt else None
            print(f"  Unit {unit}: Reg 2600 = {sgn(regs[0])} W"
                  + (f", Spannung L1 = {v} V" if v else ""))
    if not found:
        c.close()
        sys.exit("Kein Grid-Meter gefunden (Register 2600 nirgends lesbar).")

    for unit in found:
        imp = rd(c, 2622, 6, unit)   # 2622..2627: import L1/L2/L3 (je uint32)
        exp = rd(c, 2628, 6, unit)   # 2628..2633: export L1/L2/L3
        if imp and exp:
            imp_total = (u32(imp[0:2]) + u32(imp[2:4]) + u32(imp[4:6])) / 1000.0
            exp_total = (u32(exp[0:2]) + u32(exp[2:4]) + u32(exp[4:6])) / 1000.0
            print("-" * 46)
            print(f"Unit {unit}:")
            print(f"  Aus dem Netz (Bezug gesamt):   {imp_total:.2f} kWh")
            print(f"  Zum Netz (Einspeisung gesamt): {exp_total:.2f} kWh")
    c.close()
    print("-" * 46)
    print("Schick mir die Ausgabe + die Unit-ID mit den plausiblen kWh-Werten.")


if __name__ == "__main__":
    main()
