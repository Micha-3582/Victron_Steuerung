#!/usr/bin/env python3
"""
Cerbo GX Modbus-TCP Lesetest (READ-ONLY)
=========================================
Testet, ob die Standalone-App den Cerbo GX direkt erreichen kann -
OHNE ioBroker. Liest nur, schreibt NICHTS.

Voraussetzung am Cerbo:
  Einstellungen -> Dienste -> Modbus TCP  ->  AN

Nutzung (auf dem Raspberry Pi oder einem PC im selben LAN):
  pip install pymodbus
  python3 cerbo_test.py --host 192.168.X.X

Die Register/Unit-IDs entsprechen den ioBroker-Datenpunkten aus V39.4:
  SOC (BMS)  : Unit 225, InputRegister  266   (uint16, %)
  ESS-Mode   : Unit 100, HoldingRegister 2900  (uint16)  -> wird NUR gelesen
  Batt.-SOC  : Unit 100, InputRegister  843    (system SOC, Gegencheck)
"""
import argparse
import sys

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    sys.exit("Bitte zuerst installieren:  pip install pymodbus")


def _call(fn, address, unit):
    """Ruft eine pymodbus-Lesefunktion versionsrobust auf.
    Aeltere Versionen erwarten slave=, neuere device_id=."""
    try:
        return fn(address=address, count=1, device_id=unit)
    except TypeError:
        return fn(address=address, count=1, slave=unit)


def read_reg(client, address, unit, kind="input"):
    """Liest ein einzelnes 16-bit-Register. kind = 'input' | 'holding'."""
    fn = client.read_input_registers if kind == "input" else client.read_holding_registers
    rr = _call(fn, address, unit)
    if rr.isError():
        return None, str(rr)
    return rr.registers[0], None


def main():
    ap = argparse.ArgumentParser(description="Cerbo GX Modbus-TCP Lesetest (read-only)")
    ap.add_argument("--host", required=True, help="IP des Cerbo GX, z.B. 192.168.1.50")
    ap.add_argument("--port", type=int, default=502)
    args = ap.parse_args()

    print(f"Verbinde mit Cerbo GX unter {args.host}:{args.port} ...")
    client = ModbusTcpClient(host=args.host, port=args.port, timeout=5)
    if not client.connect():
        sys.exit(f"FEHLER: Keine Verbindung. Ist Modbus TCP am Cerbo aktiviert? "
                 f"Ist {args.host} erreichbar (ping)?")

    checks = [
        ("SOC (BMS)",        266,  225, "input",   "%"),
        ("System-SOC",       843,  100, "input",   "%"),
        ("ESS-Mode",        2900,  100, "holding", ""),
    ]

    print("-" * 52)
    ok = 0
    for name, addr, unit, kind, unitlabel in checks:
        val, err = read_reg(client, addr, unit, kind)
        if err:
            print(f"  [FEHLER] {name:14s} (Unit {unit}, Reg {addr}): {err}")
        else:
            print(f"  [OK]     {name:14s} (Unit {unit}, Reg {addr}) = {val} {unitlabel}")
            ok += 1
    print("-" * 52)
    client.close()

    if ok == 0:
        print("Nichts gelesen. Prüfe Modbus-TCP-Aktivierung und Unit-IDs "
              "(Victron -> Einstellungen -> Modbus TCP -> Geräteliste).")
        sys.exit(1)
    print(f"{ok}/{len(checks)} Register gelesen. Modbus-Verbindung steht. "
          f"Die App kann den Cerbo direkt ansprechen - kein ioBroker nötig.")


if __name__ == "__main__":
    main()
