#!/usr/bin/env python3
"""Read the VIN over ISO-TP, escalating from the least intrusive query.

THIS TRANSMITS ON THE VEHICLE BUS. Stationary vehicle only.

Deliberately does NOT use opendbc's get_vin(). When functional_addrs is set,
get_vin replaces the address list with every address in 0x700-0x7FF (minus
0x7DF) plus 256 extended addresses -- roughly 511 parallel ISO-TP sessions
opened at once on an unknown vehicle. This targets specific addresses and
escalates only if the previous tier found nothing.

A VIN is a strong identifier and becomes personal data once linked to a person,
so it is redacted unless --show-full is passed. See docs/COMPLIANCE.md.

Usage:
    read_vin.py --backend socketcan --channel vcan0 \
        --i-am-authorised-to-transmit --vehicle-stationary
"""

from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre.vehicle import CanIO, require_authorisation  # noqa: E402

OBD_REQ, OBD_RESP = b"\x09\x02", b"\x49\x02\x01"
UDS_REQ, UDS_RESP = b"\x22\xf1\x90", b"\x62\xf1\x90"


def redact(vin: str) -> str:
    return vin if len(vin) < 8 else f"{vin[:3]}{'*' * (len(vin) - 7)}{vin[-4:]}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backend", default="canalystii", choices=["canalystii", "socketcan"])
    ap.add_argument("--channel", default="0")
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--addrs", default="0x7E0", help="physical tx addresses to try")
    ap.add_argument("--timeout", type=float, default=0.5)
    ap.add_argument("--audit", default="out/tx_audit.jsonl")
    ap.add_argument("--show-full", action="store_true", help="print the VIN unredacted")
    ap.add_argument("--i-am-authorised-to-transmit", action="store_true")
    ap.add_argument("--vehicle-stationary", action="store_true")
    args = ap.parse_args()

    require_authorisation(args, "VIN read")

    from opendbc.car.isotp_parallel_query import IsoTpParallelQuery
    from opendbc.car.vin import is_valid_vin

    addrs = [int(a, 16) for a in args.addrs.split(",")]
    os.makedirs(os.path.dirname(args.audit) or ".", exist_ok=True)

    # Least intrusive first. Functional broadcast is last because it makes every
    # listening ECU open a session.
    tiers = [
        ("OBD 09 02, physical", addrs, OBD_REQ, OBD_RESP, None),
        ("UDS 22 F1 90, physical", addrs, UDS_REQ, UDS_RESP, None),
        ("OBD 09 02, functional 0x7DF", addrs, OBD_REQ, OBD_RESP, [0x7DF]),
    ]

    with CanIO(args.backend, {args.bus: args.channel}, args.bitrate,
               allow_transmit=True, audit_path=args.audit) as io:
        for label, tx, req, resp, func in tiers:
            print(f"trying {label} on {[hex(a) for a in tx]} ...")
            try:
                q = IsoTpParallelQuery(io.send, io.recv, args.bus, tx, [req], [resp],
                                       functional_addrs=func)
                results = q.get_data(args.timeout)
            except Exception as exc:
                print(f"  query failed: {exc}")
                continue

            for addr in tx:
                raw = results.get((addr, None))
                if raw is None:
                    continue
                raw = re.sub(b"\x00*$", b"", raw)
                vin = raw.decode("latin-1", errors="replace").strip()
                if not is_valid_vin(vin):
                    print(f"  0x{addr:X} replied but VIN failed validation: {vin!r}")
                    continue
                shown = vin if args.show_full else redact(vin)
                print(f"\nVIN {shown}  (from 0x{addr:X}, {label})")
                wmi = vin[:3]
                if wmi == "MAT":
                    print(f"  WMI {wmi} -- Tata Motors, India. Consistent with the target vehicle.")
                else:
                    print(f"  WMI {wmi} -- NOT a Tata WMI (expected MAT). Verify the vehicle.")
                if not args.show_full:
                    print("  redacted; pass --show-full to reveal (see docs/COMPLIANCE.md)")
                print(f"{io.tx_count} frames transmitted (audit: {args.audit})")
                return 0

        print(f"\nno VIN obtained. {io.tx_count} frames transmitted (audit: {args.audit})")
        print("  The OBD port may be diagnostic-gated, or the VIN may live on another "
              "address -- run discover_ecus.py and retry with --addrs.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
