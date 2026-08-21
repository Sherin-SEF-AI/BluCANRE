#!/usr/bin/env python3
"""Validate dbc/tata_tigor_ev.dbc against recorded logs.

Exits non-zero on any failure so this can gate CI.

The dual-parser check (C1) exists because opendbc's DBC parser is a set of
regexes that ``continue`` on any line they fail to match -- no exception, no
warning. An ``SG_`` line missing its trailing receiver token is silently
dropped, so a DBC that cantools decodes perfectly can be invisible to opendbc.
Diffing two independent parsers is the only way to make that failure loud.

Usage:
    validate_dbc.py [--dbc PATH] LOG [LOG ...]
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blucanre import canlog, signals  # noqa: E402


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> bool:
        if ok:
            print(f"  PASS  {label}")
        else:
            print(f"  FAIL  {label}" + (f"\n          {detail}" if detail else ""))
            self.failures.append(label)
        return ok

    def note(self, msg: str) -> None:
        print(f"  ....  {msg}")
        self.notes.append(msg)


def c1_dual_parser(dbc_path: str, rep: Report) -> None:
    """Both parsers must see structurally identical content."""
    print("\nC1  dual-parser agreement (opendbc vs cantools)")
    from opendbc.can.dbc import DBC
    import cantools

    DBC.cache_clear()  # DBC is @cache-memoised; drop any stale parse of this path
    od = DBC(dbc_path)
    ct = cantools.database.load_file(dbc_path, strict=True)

    od_msgs = {m.name: m for m in od.name_to_msg.values()}
    ct_msgs = {m.name: m for m in ct.messages}
    rep.check(
        set(od_msgs) == set(ct_msgs),
        "message sets identical",
        f"opendbc-only={sorted(set(od_msgs) - set(ct_msgs))} cantools-only={sorted(set(ct_msgs) - set(od_msgs))}",
    )

    bad = []
    for name in sorted(set(od_msgs) & set(ct_msgs)):
        o, c = od_msgs[name], ct_msgs[name]
        if set(o.sigs) != {s.name for s in c.signals}:
            missing = {s.name for s in c.signals} - set(o.sigs)
            bad.append(f"{name}: opendbc silently dropped {sorted(missing)}")
            continue
        for cs in c.signals:
            os_ = o.sigs[cs.name]
            if (os_.start_bit, os_.size, os_.is_little_endian, os_.is_signed) != (
                cs.start, cs.length, cs.byte_order == "little_endian", cs.is_signed
            ):
                bad.append(f"{name}.{cs.name}: bit layout differs between parsers")
    rep.check(not bad, "every signal identical in both parsers", "\n          ".join(bad[:5]))


def c2_structural(dbc_path: str, rep: Report) -> None:
    print("\nC2  structural sanity")
    import cantools

    db = cantools.database.load_file(dbc_path, strict=True)
    overruns = [
        f"{m.name}.{s.name} ends at bit {s.start + s.length} > {m.length * 8}"
        for m in db.messages
        for s in m.signals
        if s.start + s.length > m.length * 8
    ]
    rep.check(not overruns, "all signals fit within declared DLC", "; ".join(overruns[:5]))

    dupes = [m.name for m in db.messages if len({s.name for s in m.signals}) != len(m.signals)]
    rep.check(not dupes, "no duplicate signal names within a message", "; ".join(dupes))

    addrs = [m.frame_id for m in db.messages]
    rep.check(len(set(addrs)) == len(addrs), "no duplicate addresses")


def c3_evidence(dbc_path: str, rep: Report) -> None:
    """Spec rule: no signal in the DBC without stated evidence."""
    print("\nC3  evidence gate")
    import cantools

    db = cantools.database.load_file(dbc_path, strict=True)
    missing = [m.name for m in db.messages if not (m.comment or "").strip()]
    rep.check(not missing, "every message has a CM_ evidence comment", f"missing: {missing[:5]}")


def c4_bus_map(dbc_path: str, rep: Report) -> None:
    print("\nC4  bus map")
    import cantools

    db = cantools.database.load_file(dbc_path, strict=True)
    mapped = {n for msgs in signals.MESSAGES_BY_BUS.values() for n, _ in msgs}
    declared = {m.name for m in db.messages}
    rep.check(declared == mapped, "every DBC message assigned to exactly one bus",
              f"unmapped={sorted(declared - mapped)} stale={sorted(mapped - declared)}")

    collide = {m.frame_id for m in db.messages} & signals.KNOWN_CROSS_BUS_COLLISIONS
    if collide:
        rep.note(f"DBC defines known cross-bus collision addr(s) {[hex(a) for a in collide]}; "
                 "only the ch1 variant is representable in a single DBC.")


def c5_replay(dbc_path: str, log: str, rep: Report) -> None:
    """Replay a log and bounds-check every decoded value."""
    print(f"\nC5  replay conformance -- {os.path.basename(log)}")
    from opendbc.can.dbc import DBC
    from opendbc.can import CANParser

    DBC.cache_clear()
    parsers = {
        bus: CANParser(dbc_path, msgs, bus)
        for bus, msgs in signals.MESSAGES_BY_BUS.items()
        if msgs
    }
    bounds = signals.signal_bounds(dbc_path)
    name_to_addr = {m.name: m.address for m in DBC(dbc_path).name_to_msg.values()}

    seen_buses = set()
    violations: list[str] = []
    cell_vectors = 0
    pack_lo, pack_hi = float("inf"), float("-inf")
    spread_max = 0.0
    unused_nonzero = 0
    last: dict[str, float] = {}

    for t_ns, frames in canlog.packets(log):
        seen_buses.update(f[2] for f in frames)
        for bus, cp in parsers.items():
            # update() returns the addresses that actually carried data. vl is
            # pre-populated with 0.0 for every signal, so reading a message that
            # has not arrived yet would bounds-check a phantom zero.
            updated = cp.update([(t_ns, frames)])
            for name, _ in signals.MESSAGES_BY_BUS[bus]:
                if name_to_addr[name] not in updated:
                    continue
                for sig, v in cp.vl[name].items():
                    last[sig] = v
                    lo, hi = bounds.get(sig, (float("-inf"), float("inf")))
                    if not (lo <= v <= hi) and len(violations) < 20:
                        violations.append(f"{sig}={v} outside [{lo}|{hi}] at t={t_ns/1e9:.3f}s")

        # Invariants need a complete vector: every cell must have been seen at
        # least once, otherwise we would average real cells against defaults.
        if len(last) >= len(signals.CELL_SIGNALS):
            cells = [last[s] for s in signals.CELL_SIGNALS]
            cell_vectors += 1
            packv = sum(cells) / 1000.0
            pack_lo, pack_hi = min(pack_lo, packv), max(pack_hi, packv)
            spread_max = max(spread_max, max(cells) - min(cells))
            if last.get("cell_slot_unused", 0) != 0:
                unused_nonzero += 1

    if not (seen_buses & set(parsers)):
        rep.note(f"log has buses {sorted(seen_buses)}; no message defined for them -- "
                 "cell invariants NOT EXERCISED by this log (expected for single-bus ch0 captures)")
        return

    rep.check(not violations, "all decoded values within declared bounds", "; ".join(violations[:5]))
    if cell_vectors:
        rep.check(pack_lo >= signals.PACK_V_MIN and pack_hi <= signals.PACK_V_MAX,
                  f"pack sum in [{signals.PACK_V_MIN},{signals.PACK_V_MAX}] V "
                  f"(observed {pack_lo:.2f}-{pack_hi:.2f} V over {cell_vectors} vectors)")
        rep.check(spread_max <= 150, f"cell spread plausible (max {spread_max:.0f} mV)")
        rep.check(unused_nonzero == 0,
                  f"0x4FA unused slot stays zero ({unused_nonzero} non-zero of {cell_vectors})")
    else:
        rep.note("no complete 94-cell vector assembled -- cell invariants NOT EXERCISED")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--dbc", default=os.path.join(here, "dbc", "tata_tigor_ev.dbc"))
    ap.add_argument("logs", nargs="*")
    args = ap.parse_args()

    dbc_path = os.path.abspath(args.dbc)  # absolute: DBC() checks os.path.exists() first
    print(f"DBC: {dbc_path}")

    rep = Report()
    c1_dual_parser(dbc_path, rep)
    c2_structural(dbc_path, rep)
    c3_evidence(dbc_path, rep)
    c4_bus_map(dbc_path, rep)
    for log in args.logs:
        c5_replay(dbc_path, log, rep)

    print("\n" + "=" * 60)
    if rep.failures:
        print(f"FAILED ({len(rep.failures)}): " + "; ".join(rep.failures))
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
