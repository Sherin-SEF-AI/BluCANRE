#!/usr/bin/env python3
"""A minimal ISO-TP responder on a virtual bus, for rehearsing the transmit tools.

Debugging ISO-TP framing and address chunking while sitting in a car, on a bus
you are actively transmitting onto, is the worst possible place to discover a
bug. This fakes just enough of an ECU to exercise discover_ecus.py and
read_vin.py end to end on vcan0.

Responds to:
  * TesterPresent  (02 3E 00)        at each --addrs entry, replying at addr+8
  * OBD VIN        (02 09 02)        multi-frame
  * UDS VIN        (03 22 F1 90)     multi-frame

Usage:
    fake_ecu.py --channel vcan0 --addrs 0x7E0,0x7E1 --vin MAT625012NPE12345
"""

from __future__ import annotations

import argparse
import time

TESTER_PRESENT = bytes([0x3E, 0x00])
OBD_VIN_REQ = bytes([0x09, 0x02])
UDS_VIN_REQ = bytes([0x22, 0xF1, 0x90])


def isotp_frames(payload: bytes) -> list[bytes]:
    """Split a payload into ISO-TP single or first+consecutive frames."""
    if len(payload) <= 7:
        return [bytes([len(payload)]) + payload + b"\x00" * (7 - len(payload))]
    out = [bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]]
    rest, sn = payload[6:], 1
    while rest:
        chunk, rest = rest[:7], rest[7:]
        out.append(bytes([0x20 | (sn & 0x0F)]) + chunk + b"\x00" * (7 - len(chunk)))
        sn += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="vcan0")
    ap.add_argument("--addrs", default="0x7E0,0x7E1")
    ap.add_argument("--vin", default="MAT625012NPE12345")
    ap.add_argument("--duration", type=float, default=30.0)
    args = ap.parse_args()

    import can
    addrs = {int(a, 16) for a in args.addrs.split(",")}
    # 0x7DF is the OBD functional (broadcast) address; a real ECU answers it too.
    listen = addrs | {0x7DF}
    bus = can.Bus(interface="socketcan", channel=args.channel)
    print(f"fake ECU on {args.channel}, addrs={[hex(a) for a in sorted(addrs)]}, vin={args.vin}")

    vin = args.vin.encode()
    end = time.monotonic() + args.duration
    pending: dict[int, list[bytes]] = {}
    try:
        while time.monotonic() < end:
            msg = bus.recv(timeout=0.2)
            if msg is None:
                continue
            if msg.arbitration_id not in listen:
                continue
            d = bytes(msg.data)
            if len(d) < 2:
                continue

            targets = sorted(addrs) if msg.arbitration_id == 0x7DF else [msg.arbitration_id]

            # Flow control from the tester -> release queued consecutive frames.
            if d[0] == 0x30:
                for tx in targets:
                    for f in pending.pop(tx, []):
                        bus.send(can.Message(arbitration_id=tx + 8, data=f, is_extended_id=False))
                        time.sleep(0.001)
                continue

            payload = d[1 : 1 + d[0]]
            for tx in targets:
                rx = tx + 8
                if payload[:2] == TESTER_PRESENT:
                    bus.send(can.Message(arbitration_id=rx, data=bytes([0x02, 0x7E, 0x00, 0, 0, 0, 0, 0]),
                                         is_extended_id=False))
                elif payload[:2] == OBD_VIN_REQ:
                    frames = isotp_frames(bytes([0x49, 0x02, 0x01]) + vin)
                    bus.send(can.Message(arbitration_id=rx, data=frames[0], is_extended_id=False))
                    pending[tx] = frames[1:]
                elif payload[:3] == UDS_VIN_REQ:
                    frames = isotp_frames(bytes([0x62, 0xF1, 0x90]) + vin)
                    bus.send(can.Message(arbitration_id=rx, data=frames[0], is_extended_id=False))
                    pending[tx] = frames[1:]
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
