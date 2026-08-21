#!/usr/bin/env python3
"""Negative tests: prove validate_dbc actually fails when it should.

A validator that has only ever passed is untested. Each case below injects one
specific, realistic defect into a copy of the DBC and asserts validate_dbc
rejects it. The first case is the important one: opendbc drops SG_ lines with no
receiver token silently, so without C1 that DBC would look healthy.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DBC = os.path.join(ROOT, "dbc", "tata_tigor_ev.dbc")
LOG = "/home/joai/drivox-canre/session_20260812_191420/can_frames.csv"

CASES = [
    (
        "SG_ with receiver token stripped (opendbc drops it silently)",
        lambda s: s.replace(' SG_ cell_v_002 : 23|12@0+ (1,0) [2500|4250] "mV"  VCU\n',
                            ' SG_ cell_v_002 : 23|12@0+ (1,0) [2500|4250] "mV"\n', 1),
    ),
    (
        "signal running past the declared DLC",
        lambda s: s.replace(" SG_ cell_v_005 : 51|12@0+", " SG_ cell_v_005 : 51|32@0+", 1),
    ),
    (
        "message evidence comment removed",
        lambda s: s.replace('CM_ BO_ 1256 "', 'XX_ BO_ 1256 "', 1),
    ),
    (
        "fantasy bounds that real data violates",
        lambda s: s.replace(' SG_ cell_v_001 : 3|12@0+ (1,0) [2500|4250]',
                            ' SG_ cell_v_001 : 3|12@0+ (1,0) [4000|4250]', 1),
    ),
    (
        "wrong start bit (the 3/23/27/47/51 pattern is easy to get wrong)",
        lambda s: s.replace(" SG_ cell_v_002 : 23|12@0+", " SG_ cell_v_002 : 15|12@0+", 1),
    ),
]


def main() -> int:
    original = open(DBC).read()
    failures = []

    for label, mutate in CASES:
        mutated = mutate(original)
        if mutated == original:
            print(f"  ERROR  {label}\n           mutation did not apply -- test is vacuous")
            failures.append(label)
            continue

        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tata_tigor_ev.dbc")
            with open(path, "w") as fh:
                fh.write(mutated)
            proc = subprocess.run(
                [sys.executable, os.path.join(HERE, "validate_dbc.py"), "--dbc", path, LOG],
                capture_output=True, text=True,
            )
        if proc.returncode != 0:
            print(f"  PASS   rejected: {label}")
        else:
            print(f"  FAIL   ACCEPTED (should have been rejected): {label}")
            failures.append(label)

    print("\n" + "=" * 60)
    if failures:
        print(f"SELFTEST FAILED -- {len(failures)} defect(s) slipped through")
        return 1
    print(f"SELFTEST PASSED -- all {len(CASES)} injected defects rejected")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
