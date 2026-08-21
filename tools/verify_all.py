#!/usr/bin/env python3
"""Run the full Phase 0a acceptance checklist and report pass/fail per criterion.

This is the gate the plan defines. Exits non-zero if any criterion fails.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

DRIVOX_BIG = "/home/joai/drivox-canre/session_20260812_184203/can_frames.csv"
DRIVOX_SMALL = "/home/joai/drivox-canre/session_20260812_191420/can_frames.csv"
CANALYSIS = [
    "/home/joai/can-analysis/tigor_30s.csv",
    "/home/joai/can-analysis/live_engine_on.csv",
    "/home/joai/can-analysis/test_capture.csv",
]

results: list[tuple[str, bool, str]] = []


def run(label: str, cmd: list[str], expect_rc: int = 0, timeout: int = 3600) -> str:
    t = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, timeout=timeout)
    dt = time.monotonic() - t
    ok = p.returncode == expect_rc
    results.append((label, ok, f"rc={p.returncode} in {dt:.0f}s"))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}  ({dt:.0f}s)")
    if not ok:
        print((p.stdout + p.stderr)[-800:])
    return p.stdout


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the Phase 0a acceptance checklist (13 criteria, ~2.5 min).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quick", action="store_true",
                    help="skip the checks that stream the full 251 MB capture")
    args = ap.parse_args()

    print("A1  environment")
    run("opendbc imports on 3.12", [PY, "-c", "from opendbc.can import CANParser; import cantools, can, numpy"])

    print("\nA2  DBC validates against every available capture")
    if not args.quick:
        run("validate: drivox 4.06M-frame session", [PY, "tools/validate_dbc.py", DRIVOX_BIG])
    run("validate: drivox 18s session", [PY, "tools/validate_dbc.py", DRIVOX_SMALL])
    run("validate: all three can-analysis captures", [PY, "tools/validate_dbc.py", *CANALYSIS])

    print("\nA3  validator rejects injected defects")
    run("selftest: injected defects rejected", [PY, "tools/selftest.py"])

    print("\nA4  analysis tools run over the full 251 MB capture")
    if not args.quick:
        out = run("bit_activity: whole capture", [PY, "tools/bit_activity.py", DRIVOX_BIG, "--top", "2"])
        static = "fully static across log" in out
        results.append(("bit_activity reports static IDs", static, ""))
        print(f"  {'PASS' if static else 'FAIL'}  bit_activity reports static IDs")
        run("bit_activity selftest", [PY, "tools/bit_activity.py", "--selftest", DRIVOX_BIG])
        run("diff_logs: two drivox sessions", [PY, "tools/diff_logs.py", "--a", DRIVOX_SMALL, "--b", DRIVOX_BIG])
        run("correlate: cell-anchored signal hunt", [PY, "tools/correlate.py", DRIVOX_BIG])

    print("\nA5  guards refuse unsupportable analysis")
    if not args.quick:
        run("diff_logs refuses ambiguous marker", [PY, "tools/diff_logs.py", "--log", DRIVOX_BIG,
                                                   "--marker", "drive_on"], expect_rc=1)
        run("diff_logs refuses end-of-log marker", [PY, "tools/diff_logs.py", "--log", DRIVOX_BIG,
                                                    "--marker", "drive_on", "--occurrence", "1"], expect_rc=1)

    print("\nA6  transmit tools fail closed")
    run("discover_ecus refuses without authorisation",
        [PY, "tools/discover_ecus.py", "--backend", "socketcan", "--channel", "vcan0"], expect_rc=1)
    run("read_vin refuses without authorisation",
        [PY, "tools/read_vin.py", "--backend", "socketcan", "--channel", "vcan0"], expect_rc=1)

    print("\n" + "=" * 64)
    failed = [r for r in results if not r[1]]
    for label, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {label}  {note}")
    print("=" * 64)
    if failed:
        print(f"PHASE 0A: {len(failed)} of {len(results)} criteria FAILED")
        return 1
    print(f"PHASE 0A ACCEPTANCE: all {len(results)} criteria passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
