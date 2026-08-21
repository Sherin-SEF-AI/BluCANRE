#!/usr/bin/env python3
"""Isolate the bits that changed between two logs, or across a marker.

Signals are found by diffing captures that differ in exactly one variable. The
strongest evidence is a bit that is *constant within* each group but *different
between* them -- that is a state bit (gear, door, charge flag).

GUARDS
------
This tool refuses to produce a comparison it cannot support. The prior
evidence.md reported 20 "findings" that were purely an artifact of a marker
placed 4 ms before the end of the log, every one carrying novelty 0.00. The
checks below make that class of result impossible rather than merely unlikely.

Usage:
    diff_logs.py --a baseline.csv --b gear_d.csv
    diff_logs.py --log session.csv --marker gear_d --pre 5 --post 5
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre import canlog  # noqa: E402

MIN_FRAMES = 20


def profile(frames) -> dict:
    """(bus, addr, byte) -> set of observed values, plus per-id frame counts."""
    vals: dict[tuple, set] = defaultdict(set)
    counts: dict[tuple, int] = defaultdict(int)
    for f in frames:
        counts[(f.src, f.addr)] += 1
        for i, b in enumerate(f.data):
            vals[(f.src, f.addr, i)].add(b)
    return {"vals": vals, "counts": counts}


def window(log: str, t0: float, t1: float):
    return [f for f in canlog.load(log) if t0 <= f.t <= t1]


def load_markers(log: str) -> list[tuple[float, str]]:
    path = os.path.join(os.path.dirname(os.path.abspath(log)), "markers.csv")
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="") as fh:
        for row in csv.DictReader(fh):
            out.append((float(row["t_s"]), row["label"]))
    return out


def compare(pa: dict, pb: dict, label_a: str, label_b: str, top: int) -> None:
    keys = set(pa["vals"]) | set(pb["vals"])
    state_bits, appeared, widened = [], [], []

    for k in sorted(keys):
        va, vb = pa["vals"].get(k, set()), pb["vals"].get(k, set())
        if not va or not vb:
            appeared.append((k, va, vb))
            continue
        if len(va) == 1 and len(vb) == 1 and va != vb:
            state_bits.append((k, va, vb))       # strongest evidence
        elif va != vb and (len(va) == 1 or len(vb) == 1):
            widened.append((k, va, vb))

    def fmt(k, va, vb):
        bus, addr, byte = k
        sa = " ".join(f"{v:02X}" for v in sorted(va)[:6]) or "-"
        sb = " ".join(f"{v:02X}" for v in sorted(vb)[:6]) or "-"
        return f"  ch{bus} 0x{addr:03X} b{byte}:  {label_a}=[{sa}]  {label_b}=[{sb}]"

    print(f"\nSTATE BITS -- constant in both groups, different between ({len(state_bits)})")
    print("  strongest evidence: a discrete vehicle state changed")
    for k, va, vb in state_bits[:top]:
        print(fmt(k, va, vb))
    if not state_bits:
        print("  (none)")

    print(f"\nACTIVITY-GATED -- constant in one group, varying in the other ({len(widened)})")
    for k, va, vb in widened[:top]:
        print(fmt(k, va, vb))
    if not widened:
        print("  (none)")

    only_a = sorted({(k[0], k[1]) for k, va, vb in appeared if va and not vb})
    only_b = sorted({(k[0], k[1]) for k, va, vb in appeared if vb and not va})
    if only_a:
        print(f"\nIDs only in {label_a} ({len(only_a)}): " +
              ", ".join(f"ch{b} 0x{a:03X}" for b, a in only_a[:12]))
    if only_b:
        print(f"\nIDs only in {label_b} ({len(only_b)}): " +
              ", ".join(f"ch{b} 0x{a:03X}" for b, a in only_b[:12]))


def guard_counts(p: dict, label: str) -> bool:
    thin = [k for k, c in p["counts"].items() if c < MIN_FRAMES]
    total = sum(p["counts"].values())
    if total == 0:
        print(f"REFUSING: window '{label}' contains no frames.")
        return False
    if len(thin) == len(p["counts"]):
        print(f"REFUSING: every ID in '{label}' has < {MIN_FRAMES} frames "
              f"({total} total). Too thin to distinguish signal from coincidence.")
        return False
    if thin:
        print(f"  note: {len(thin)} ID(s) in '{label}' have < {MIN_FRAMES} frames; "
              "treat those rows as unsupported")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--a"); ap.add_argument("--b")
    ap.add_argument("--log"); ap.add_argument("--marker")
    ap.add_argument("--pre", type=float, default=5.0)
    ap.add_argument("--post", type=float, default=5.0)
    ap.add_argument("--occurrence", type=int, default=None,
                    help="0-based index when a marker label repeats")
    ap.add_argument("--top", type=int, default=20)
    args = ap.parse_args()

    if args.a and args.b:
        fa, fb = list(canlog.load(args.a)), list(canlog.load(args.b))
        la, lb = os.path.basename(args.a), os.path.basename(args.b)
    elif args.log and args.marker:
        frames = list(canlog.load(args.log))
        t_end = max(f.t for f in frames)
        marks = [m for m in load_markers(args.log) if m[1] == args.marker]
        if not marks:
            print(f"REFUSING: marker '{args.marker}' not found in markers.csv")
            return 1
        if len(marks) > 1 and args.occurrence is None:
            print(f"REFUSING: marker '{args.marker}' occurs {len(marks)} times at "
                  f"{[round(m[0], 3) for m in marks]}. Silently picking one would hide "
                  "which event was analysed -- pass --occurrence N (0-based).")
            return 1
        t = marks[args.occurrence or 0][0]
        # The exact failure that invalidated the prior evidence.md.
        if t + args.post > t_end:
            print(f"REFUSING: marker '{args.marker}' at t={t:.3f}s leaves only "
                  f"{t_end - t:.3f}s before the log ends at {t_end:.3f}s, but a "
                  f"{args.post:.1f}s post-window was requested.\n"
                  "         There is no 'after' to compare against; any result "
                  "would be an end-of-log artifact, not an event.")
            return 1
        if t - args.pre < 0:
            print(f"REFUSING: marker at t={t:.3f}s has no room for a {args.pre:.1f}s pre-window.")
            return 1
        fa = [f for f in frames if t - args.pre <= f.t < t]
        fb = [f for f in frames if t <= f.t <= t + args.post]
        la, lb = f"pre({args.pre}s)", f"post({args.post}s)"
    else:
        ap.error("need either --a/--b or --log/--marker")

    pa, pb = profile(fa), profile(fb)
    print(f"{la}: {len(fa)} frames, {len(pa['counts'])} ids")
    print(f"{lb}: {len(fb)} frames, {len(pb['counts'])} ids")
    if not guard_counts(pa, la) or not guard_counts(pb, lb):
        return 1

    da = max((f.t for f in fa), default=0) - min((f.t for f in fa), default=0)
    db = max((f.t for f in fb), default=0) - min((f.t for f in fb), default=0)
    if da and db and max(da, db) / min(da, db) > 4:
        print(f"  WARNING: window durations differ by {max(da,db)/min(da,db):.1f}x "
              "-- duty-cycle comparison is unreliable")

    compare(pa, pb, la, lb, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
