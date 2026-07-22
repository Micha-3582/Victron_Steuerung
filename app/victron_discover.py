#!/usr/bin/env python3
"""
Victron System-Register Discovery
=================================
Liest den System-Service (Unit 100, Register 800-869) vom Cerbo GX aus und
zeigt jeden Wert roh - als unsigned und als signed (int16). Damit gleichen wir
die Register mit den Live-Zahlen aus der Victron-App ab und finden heraus, wo
Netz, AC-Lasten, PV-Wechselrichter, MPPT und Batterie stehen.

  python victron_discover.py            # nutzt cerbo_host aus config.json
  python victron_discover.py --host 192.168.2.241 --unit 100 --start 800 --count 70
"""
import argparse
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


def read1(client, address, unit, fn):
    try:
        return fn(address=address, count=1, device_id=unit)
    except TypeError:
        return fn(address=address, count=1, slave=unit)


def signed(v):
    return v - 65536 if v >= 32768 else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=cfg_host())
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=100)
    ap.add_argument("--start", type=int, default=800)
    ap.add_argument("--count", type=int, default=70)
    args = ap.parse_args()
    if not args.host:
        sys.exit("Kein Host. --host angeben oder config.json füllen.")

    c = ModbusTcpClient(host=args.host, port=args.port, timeout=5)
    if not c.connect():
        sys.exit(f"Cerbo {args.host}:{args.port} nicht erreichbar.")

    print(f"System-Service Unit {args.unit}, Register {args.start}..{args.start + args.count - 1}")
    print("(Einzelabfragen, ungültige Register werden übersprungen)")
    print(f"{'Reg':>5} | {'raw':>7} | {'signed':>7} | /10      /100")
    print("-" * 48)
    # Input- (FC04) und Holding-Register (FC03) getrennt probieren
    for label, fn in (("input", c.read_input_registers), ("holding", c.read_holding_registers)):
        found = False
        for reg in range(args.start, args.start + args.count):
            rr = read1(c, reg, args.unit, fn)
            if rr.isError():
                continue
            if not found:
                print(f"--- {label}-Register ---")
                found = True
            s = signed(rr.registers[0])
            print(f"{reg:>5} | {rr.registers[0]:>7} | {s:>7} | {s/10:>7.1f}  {s/100:>7.2f}")
    c.close()
    print("-" * 48)
    print("Schick mir die Ausgabe - ich gleiche sie mit deinen Victron-App-Werten ab.")


if __name__ == "__main__":
    main()
