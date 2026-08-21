#!/usr/bin/env python3
"""Unit tests for the log loader, on synthetic fixtures.

canlog is the foundation every other tool sits on, and its failure modes are
quiet ones: a mis-sniffed format, a dropped short frame, a bus index silently
defaulting to 0. These run in milliseconds and need no external captures.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre import canlog  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
        failures.append(label)


def write(tmp: str, name: str, content: str, newline: str = "\n") -> str:
    p = os.path.join(tmp, name)
    with open(p, "w", newline="") as fh:
        fh.write(content.replace("\n", newline))
    return p


DRIVOX_HDR = "timestamp_s,can_id_hex,dlc,b0,b1,b2,b3,b4,b5,b6,b7,data_hex,label,channel\n"


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        # --- format sniffing -------------------------------------------
        d = write(tmp, "d.csv", DRIVOX_HDR + "0.1,0x4E8,8,12,212,205,92,212,205,92,211,0cd4cd5cd4cd5cd3,,1\n")
        a = write(tmp, "a.csv", "timestamp,arbitration_id,dlc,data_hex,data_dec\n5.0,0x111,8,0000000000000000,0 0 0\n")
        c = write(tmp, "c.log", "(1699999999.123456) can1 4E8#0CD4CD5CD4CD5CD3\n")
        check(canlog.sniff(d) == canlog.Format.DRIVOX, "sniff drivox csv")
        check(canlog.sniff(a) == canlog.Format.CANANALYSIS, "sniff can-analysis csv")
        check(canlog.sniff(c) == canlog.Format.CANDUMP, "sniff candump log")

        # --- payload and bus fidelity ----------------------------------
        f = next(iter(canlog.load(d)))
        check(f.addr == 0x4E8 and f.src == 1 and f.data.hex() == "0cd4cd5cd4cd5cd3",
              "drivox: addr, channel and payload", str(f))
        f = next(iter(canlog.load(a)))
        check(f.src == 0 and f.addr == 0x111, "can-analysis defaults to bus 0")
        f = next(iter(canlog.load(c)))
        check(f.src == 1 and f.addr == 0x4E8 and f.t == 1699999999.123456,
              "candump: bus from interface digits", str(f))

        # --- short frames: b0..b7 columns are empty, data_hex is truth --
        s = write(tmp, "s.csv", DRIVOX_HDR + "0.2,0x504,2,23,111,,,,,,,176f,,1\n")
        f = next(iter(canlog.load(s)))
        check(f.data == bytes.fromhex("176f"), "short frame parsed from data_hex, not b0..b7", str(f))

        # --- CRLF (the existing drivox captures are CRLF-terminated) ----
        w = write(tmp, "crlf.csv", DRIVOX_HDR + "0.3,0x108,8,27,12,212,12,208,2,46,96,1b0cd40cd0022e60,,1\n",
                  newline="\r\n")
        f = next(iter(canlog.load(w)))
        check(f.src == 1 and f.addr == 0x108, "CRLF line endings handled", str(f))

        # --- packet batching -------------------------------------------
        rows = "".join(f"{i*0.001:.3f},0x4E8,1,1,,,,,,,,01,,{i % 2}\n" for i in range(50))
        b = write(tmp, "b.csv", DRIVOX_HDR + rows)
        pk = list(canlog.packets(b, bin_ms=10))
        check(sum(len(fr) for _, fr in pk) == 50, "batching preserves every frame",
              f"got {sum(len(fr) for _, fr in pk)}")
        check(all(isinstance(t, int) for t, _ in pk), "packet timestamps are integer nanoseconds")
        check([t for t, _ in pk] == sorted(t for t, _ in pk), "packets are time-ordered")
        check({s for _, fr in pk for _, _, s in fr} == {0, 1}, "both buses preserved in packets")

        # --- guards fail loudly ----------------------------------------
        bad = write(tmp, "bad.csv", DRIVOX_HDR + "5.0,0x1,1,1,,,,,,,,01,,0\n1.0,0x1,1,1,,,,,,,,01,,0\n")
        try:
            list(canlog.packets(bad))
            check(False, "backwards timestamps rejected", "no exception raised")
        except ValueError:
            check(True, "backwards timestamps rejected")

        odd = write(tmp, "odd.csv", DRIVOX_HDR + "0.1,0x1,1,1,,,,,,,,0cd,,0\n")
        try:
            list(canlog.load(odd))
            check(False, "odd-length hex payload rejected", "no exception raised")
        except ValueError:
            check(True, "odd-length hex payload rejected")

        junk = write(tmp, "junk.csv", "not,a,can,log\n1,2,3,4\n")
        try:
            canlog.sniff(junk)
            check(False, "unknown format rejected", "no exception raised")
        except ValueError:
            check(True, "unknown format rejected")

        # --- survey ------------------------------------------------------
        sv = canlog.survey(b)
        check(sv["frames"] == 50 and sv["buses"] == [0, 1], "survey reports frames and buses", str(sv["buses"]))

    print("\n" + "=" * 60)
    if failures:
        print(f"CANLOG TESTS FAILED: {len(failures)}")
        return 1
    print("CANLOG TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
