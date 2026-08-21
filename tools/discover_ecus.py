#!/usr/bin/env python3
"""Enumerate responding ECU addresses by TesterPresent scan.

THIS TRANSMITS ON THE VEHICLE BUS. Stationary vehicle only.

Deliberately does NOT use opendbc's get_all_ecu_addrs: that builds 512 addresses
(0x700-0x7FF plus 256 extended) and hands them to can_send in a *single* call,
dumping 512 frames onto an unknown bus at once. This chunks and rate-limits.

opendbc's get_ecu_addrs also wraps its whole loop in `except Exception` and
returns partial results, so a dead adapter looks identical to a bus with no
ECUs. We pre-flight a single frame first so that failure is loud.

Usage:
    discover_ecus.py --backend socketcan --channel vcan0 \
        --i-am-authorised-to-transmit --vehicle-stationary
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre.vehicle import CanIO, require_authorisation  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="canalystii", choices=["canalystii", "socketcan"])
    ap.add_argument("--channel", default="0")
    ap.add_argument("--bus", type=int, default=0, help="bus index reported in results")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--chunk", type=int, default=16, help="addresses per batch")
    ap.add_argument("--gap", type=float, default=0.3, help="seconds between batches")
    ap.add_argument("--timeout", type=float, default=0.4, help="listen window per batch")
    ap.add_argument("--extended", action="store_true", help="also scan 29-bit 0x18DAxxF1")
    ap.add_argument("--out", default="out/ecu_scan.json")
    ap.add_argument("--audit", default="out/tx_audit.jsonl")
    ap.add_argument("--i-am-authorised-to-transmit", action="store_true")
    ap.add_argument("--vehicle-stationary", action="store_true")
    args = ap.parse_args()

    require_authorisation(args, "ECU discovery")

    from opendbc.car.ecu_addrs import get_ecu_addrs
    from opendbc.car import make_tester_present_msg

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(args.audit) or ".", exist_ok=True)

    addrs = [0x700 + i for i in range(256)]
    if args.extended:
        addrs += [0x18DA00F1 + (i << 8) for i in range(256)]

    found: set = set()
    with CanIO(args.backend, {args.bus: args.channel}, args.bitrate,
               allow_transmit=True, audit_path=args.audit) as io:

        # Pre-flight: prove we can actually put a frame on the wire, because
        # get_ecu_addrs would swallow the failure and report "no ECUs".
        try:
            io.send([make_tester_present_msg(0x7DF, args.bus)])
        except Exception as exc:
            print(f"ABORT: cannot transmit on {args.backend}:{args.channel} -- {exc}")
            return 1

        batches = [addrs[i : i + args.chunk] for i in range(0, len(addrs), args.chunk)]
        print(f"scanning {len(addrs)} addresses in {len(batches)} batches of {args.chunk} "
              f"({args.gap}s apart, ~{len(batches) * (args.gap + args.timeout):.0f}s total)")

        for n, batch in enumerate(batches, 1):
            queries = {(a, None, args.bus) for a in batch}
            hits = get_ecu_addrs(io.recv, io.send, queries, queries, timeout=args.timeout)
            found |= hits
            if hits:
                print(f"  batch {n}/{len(batches)}: " +
                      ", ".join(f"0x{a:X}" for a, _, _ in sorted(hits)))
            time.sleep(args.gap)

    # get_ecu_addrs returns the address each reply CAME FROM -- i.e. the RX
    # address. The request address is RX minus the standard 11-bit offset of 8.
    # (Verified against a fake ECU: listening on 0x7E0, it is reported as 0x7E8.)
    result = [{"rx_addr": f"0x{a:X}",
               "tx_addr": f"0x{a - 8:X}" if a > 8 else None,
               "subaddr": s, "bus": b}
              for a, s, b in sorted(found)]
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)

    print(f"\n{len(found)} ECU(s) responded; {io.tx_count} frames transmitted "
          f"(audit: {args.audit})")
    for r in result:
        print(f"  tx={r['tx_addr']}  rx={r['rx_addr']}  bus={r['bus']}")
    if found:
        print("  (tx inferred as rx-8, the standard 11-bit offset; verify before "
              "relying on it for non-OBD access)")
    if not found:
        print("  none. Either the bus is diagnostic-gated, the vehicle is asleep, "
              "or the wrong channel was selected.")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
