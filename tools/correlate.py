#!/usr/bin/env python3
"""Hunt for unknown signals by correlating against the confirmed cell voltages.

The 94-cell map gives us something rare in reverse engineering: a physically
meaningful reference series derived entirely from proven signals. Pack sum,
cell min/max/mean and cell delta are all known at every instant. Any BMS that
broadcasts its own pack voltage or cell extremes must correlate with them.

HONEST LIMITATION, READ BEFORE TRUSTING OUTPUT
----------------------------------------------
Over the reference capture the pack is at rest: the sum spans only ~0.37 V and
individual cells ~8 mV. Correlation is therefore computed against something
barely above quantisation noise, and a high |r| on its own means little. This
tool requires BOTH a high |r| AND an implied scale factor from the set real
BMS designers actually use. Even then the result is a *candidate*, not a
confirmed signal, and it does not enter the DBC without a charge/discharge log
that swings pack voltage by tens of volts.

Usage:
    correlate.py LOG [--min-r 0.9] [--grid-hz 1.0] [--top 25]
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre import canlog, signals  # noqa: E402

# Factors a BMS plausibly uses for voltage/current/temperature telemetry.
PLAUSIBLE_FACTORS = [1.0, 0.5, 0.25, 0.1, 0.05, 0.01, 0.005, 0.001, 0.0625, 2.0, 10.0]
CELL_STARTS_NIBBLE = [1, 4, 7, 10, 13]   # nibble offsets of the 5 cell fields


def decode_cells(payload: np.ndarray) -> np.ndarray:
    """[N,8] uint8 -> [N,5] cell values (12-bit BE at nibble offset)."""
    nib = np.empty((payload.shape[0], 16), dtype=np.int64)
    nib[:, 0::2] = payload >> 4
    nib[:, 1::2] = payload & 0x0F
    return np.stack([(nib[:, s] << 8) | (nib[:, s + 1] << 4) | nib[:, s + 2]
                     for s in CELL_STARTS_NIBBLE], axis=1)


def zoh(t_src: np.ndarray, v_src: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Zero-order hold resample onto a common time grid."""
    idx = np.searchsorted(t_src, grid, side="right") - 1
    idx = np.clip(idx, 0, len(v_src) - 1)
    return v_src[idx]


def candidates(payload: np.ndarray):
    """Yield (label, values) for plausible field encodings."""
    n, dlc = payload.shape
    p = payload.astype(np.int64)
    for i in range(dlc):
        col = p[:, i]
        if col.max() != col.min():
            yield f"b{i} u8", col
    for i in range(dlc - 1):
        be = (p[:, i] << 8) | p[:, i + 1]
        le = (p[:, i + 1] << 8) | p[:, i]
        if be.max() != be.min():
            yield f"b{i}-{i+1} BE16", be
            yield f"b{i}-{i+1} LE16", le
    # 12-bit big-endian at every nibble offset
    nib = np.empty((n, dlc * 2), dtype=np.int64)
    nib[:, 0::2] = p >> 4
    nib[:, 1::2] = p & 0x0F
    for s in range(dlc * 2 - 2):
        v = (nib[:, s] << 8) | (nib[:, s + 1] << 4) | nib[:, s + 2]
        if v.max() != v.min():
            yield f"nib{s} BE12", v


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--min-r", type=float, default=0.90)
    ap.add_argument("--grid-hz", type=float, default=1.0)
    ap.add_argument("--top", type=int, default=25)
    args = ap.parse_args()

    cell_addrs = {signals.CELL_BASE_ADDR + i for i in range(signals.CELL_MSG_COUNT)}
    ts: dict = defaultdict(list)
    dat: dict = defaultdict(list)
    for f in canlog.load(args.log):
        ts[(f.src, f.addr)].append(f.t)
        dat[(f.src, f.addr)].append(f.data.ljust(8, b"\x00")[:8])

    arrays = {k: (np.array(ts[k]), np.frombuffer(b"".join(dat[k]), dtype=np.uint8).reshape(-1, 8))
              for k in ts}
    print(f"{len(arrays)} (bus, id) pairs loaded")

    have = [(1, a) for a in sorted(cell_addrs) if (1, a) in arrays]
    if len(have) != signals.CELL_MSG_COUNT:
        print(f"REFUSING: only {len(have)}/{signals.CELL_MSG_COUNT} cell messages present. "
              "The reference series cannot be built from this log.")
        return 1

    t_lo = max(arrays[k][0][0] for k in have)
    t_hi = min(arrays[k][0][-1] for k in have)
    grid = np.arange(t_lo, t_hi, 1.0 / args.grid_hz)
    print(f"reference grid: {len(grid)} points over {t_hi - t_lo:.0f}s at {args.grid_hz} Hz")

    cells = []
    for k in have:
        t, p = arrays[k]
        c = decode_cells(p)
        for j in range(5):
            if (k[1], j) == (0x4FA, 0):
                continue  # the dead slot
            cells.append(zoh(t, c[:, j], grid))
    cells = np.stack(cells, axis=1).astype(float)
    print(f"reference built from {cells.shape[1]} cells")

    refs = {
        "pack_sum_V": cells.sum(axis=1) / 1000.0,
        "cell_min_mV": cells.min(axis=1),
        "cell_max_mV": cells.max(axis=1),
        "cell_mean_mV": cells.mean(axis=1),
        "cell_delta_mV": cells.max(axis=1) - cells.min(axis=1),
    }
    print("\nreference series (span over the capture):")
    for name, v in refs.items():
        print(f"  {name:14s} {v.min():10.3f} .. {v.max():10.3f}   span={v.max()-v.min():.3f}")

    hits = []
    for k, (t, p) in arrays.items():
        if k in have or len(t) < 10:
            continue
        for label, v in candidates(p):
            vg = zoh(t, v.astype(float), grid)
            if vg.std() == 0:
                continue
            for rname, rv in refs.items():
                if rv.std() == 0:
                    continue
                r = float(np.corrcoef(vg, rv)[0, 1])
                if abs(r) < args.min_r:
                    continue
                # best-fit scale, then snap to a factor a designer would pick
                scale = float(np.polyfit(vg, rv, 1)[0])
                best = min(PLAUSIBLE_FACTORS, key=lambda f: abs(f - abs(scale)))
                plausible = abs(abs(scale) - best) <= 0.15 * best
                hits.append((abs(r), r, k, label, rname, scale, best, plausible,
                             float(vg.min()), float(vg.max())))

    hits.sort(reverse=True)
    print(f"\n{len(hits)} correlation(s) with |r| >= {args.min_r}")
    strong = [h for h in hits if h[7]]
    print(f"{len(strong)} also have a plausible scale factor\n")

    shown = strong if strong else hits
    for _, r, k, label, rname, scale, best, plausible, lo, hi in shown[: args.top]:
        flag = "PLAUSIBLE" if plausible else "scale-odd"
        print(f"  ch{k[0]} 0x{k[1]:03X} {label:12s} vs {rname:14s} r={r:+.4f} "
              f"raw={lo:.0f}..{hi:.0f} scale={scale:.5g} (~{best}) {flag}")

    print("\nVERDICT")
    if not strong:
        print("  No candidate passes both tests. Expected: the pack is at rest in this")
        print("  capture, so there is almost no signal to correlate against.")
        print("  Resolve with a dc_charge or soc_sweep log, not with more analysis.")
    else:
        print("  Candidates above are LEADS ONLY. Record them in docs/SIGNALS.md and")
        print("  confirm against a log with real pack-voltage swing before any enter")
        print("  the DBC. A high r over a 0.37 V span is not proof.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
