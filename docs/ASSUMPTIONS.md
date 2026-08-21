# ASSUMPTIONS

Unresolved uncertainties, recorded rather than stalled on. Each states what we
assumed, why, and what would falsify it.

## Environment

**A1 — opendbc is a path dependency pinned to `17286eb`, not a fork.**
`/home/joai/opendbc` is treated read-only. The project spec cited commit
`b4ef5e1c`; the local clone is shallow so that commit is not fetchable and its
claims could not be diffed. All API facts here were re-verified against
`17286eb`.

**A2 — `opendbc/can` is pure Python at this commit.** No Cython, no scons, no
`.so`. The spec's premise that opendbc provides *fast* DBC decode **no longer
holds**. Falsifier: a future opendbc restores the C++ parser.

**A3 — Python is pinned to 3.12.** opendbc requires `>=3.11,<3.13` because
pycapnp does not build on 3.13+. System Python is 3.14.4, so `uv` fetches
3.12.13. Falsifier: opendbc drops the capnp dependency.

**A4 — pycapnp is accepted as a transitive dependency.** `opendbc.can.__init__`
→ `packer` → `opendbc/car/__init__` → `structs` → `capnp`, so even pure DBC
parsing drags it in. Vendoring is not a clean escape: `dbc.py` imports eleven
brand checksum modules at module scope.
**Phase 1 risk:** pycapnp plus pure-Python parsing on a Pi Zero 2W against a
~2100 frame/s bus is unvalidated. Benchmark (`opendbc/can/tests/benchmark.py`)
*before* committing to the edge architecture. If it fails, the edge agent should
use cantools or a hand-rolled decoder; nothing in Phase 0a depends on that choice.

## opendbc behaviour we rely on

**A5 — `DBC.__init__` checks `os.path.exists(name)` before the packaged lookup**,
so an absolute path to our private DBC works. Verified.

**A6 — The DBC filename must stay `tata_tigor_ev`.** `_parse_file` sets
`self.name` from the basename and that drives `get_checksum_state()` brand
special-casing (`honda_`, `toyota_`, `vw_*`, …). `tata_*` matches none, so no
checksum/counter handling is applied. Renaming with a brand prefix would silently
change parsing.

**A7 — `DBC` is `@cache`-memoised, but `DBC.cache_clear()` exists and works.**
Tools call it before constructing parsers so an edited DBC is actually re-read.
Parsers must also be rebuilt — clearing the cache does not refresh `DBC` objects
already held by live `CANParser` instances.

**A8 — opendbc silently drops malformed `SG_` lines.** Its parser is regexes that
`continue` on no match; an `SG_` line missing its trailing receiver token is
dropped with no exception and no warning, so a DBC can decode perfectly under
cantools and be invisible to opendbc. This is why `validate_dbc.py` C1 diffs two
independent parsers. Verified by injection in `tools/selftest.py`.

**A9 — `CANParser.vl` is pre-populated with 0.0 for every signal** before its
message has ever been received. Bounds-checking must gate on the address set
returned by `update()`, not read `vl` unconditionally.

## Vehicle / bus topology

**A10 — The OBD-II port is NOT gateway-gated.** 110 free-running IDs across two
channels are broadcast without any request. This is the single fact the spec says
determines the project's shape, and it resolves favourably. Falsifier: a
different vehicle/firmware where only query responses appear.

**A11 — Channel numbering follows the recording adapter, not the OBD pinout.**
`0x247` appears on channel 0 in both canmcpd and drivox; `tigorsteering.py`
claims "channel 1 (OBD pins 3+11)". Which physical pin pair maps to which
channel index is **unverified** and needs a pinout check. All analysis here uses
adapter channel indices as recorded.

**A12 — `0x103` and `0x501` are genuinely different messages on the two buses.**
`0x501` has DLC 8 on ch0 and 7 on ch1, which proves distinctness. A single DBC
keys messages by address alone and would silently overwrite one with the other,
so **only the ch1 variants may be defined**. Both ch0 variants are fully static
across the whole capture, so nothing is currently lost. `validate_dbc.py` C4
flags this if it ever becomes load-bearing; the escalation is to split into
per-bus DBC files.

**A13 — The 4M-frame reference capture is a stationary vehicle.** `0x111` torque
demand is all-zeros at 100 Hz and cell voltages merely rest and recover. Any
signal requiring motion, gear change, or charging is therefore out of reach of
existing data.

**A14 — Cell *ordering* is unproven.** We have 94 correct cell values; that the
DBC's `cell_v_001` is physically the first cell in the pack is an assumption.
Ordering does not affect min/max/delta/sum, which is what the analytics need.

## Recording hardware (learned from the first live capture, 2026-08-21)

**A18 — The CANalyst-II is one USB device and cannot be opened twice.** Opening
a `can.Bus` per channel fails the second with `[Errno 16] Resource busy`. Its
python-can backend takes a *sequence* of channels on a single Bus
(`channel=(0, 1)`, its default) and tags each message with `msg.channel`.
`record.py` opens one Bus for canalystii and one per channel for socketcan.

**A19 — canalystii timestamps are device-relative, not epoch.** The first live
capture produced `timestamp_s = -1787301005` because the recorder subtracted a
wall-clock origin. `record.py` now anchors on the first frame's own timestamp
regardless of backend, so `timestamp_s` always starts at 0.

**A20 — The two channels arrive globally out of order.** Each channel's
timestamps are strictly monotonic, but the device buffers them separately and
python-can drains them in bursts, so ch0 and ch1 frames interleave incorrectly —
2,445 inversions in a 63k-frame capture, max skew 22.6 ms. `record.py` holds a
250 ms reorder buffer and emits in timestamp order. Downstream tools assert
monotonicity, so an unsorted log fails validation rather than silently
corrupting timeout logic.

**A21 — python-can's canalystii shutdown emits a libusb assertion** on process
exit (`usbi_mutex_destroy`). It happens after all data is written and flushed
and does not affect the capture, but it makes the exit status unreliable — check
the frame count in `session_meta.json`, not the shell exit code.

## Tooling limitations

**A15 — Automatic field-boundary inference cannot resolve narrow-range values.**
The cells span 5 mV of a 12-bit range, so their top nibble is constant and a
12-bit field at nibble offset 4 is numerically indistinguishable from an 8-bit
field on the low byte. No smoothness metric separates them. `bit_activity.py`
therefore *proposes* candidates and its selftest asserts the true fields are
among them — not that they rank first. Claiming exact recovery would be false.

**A16 — `canmcpd` is a monitoring convenience, not a data source.** It keeps a
rolling 300 s in-memory window and persists only `anomalies.jsonl`. Its running
process bound its python-can handle on 2026-08-12 and will not see a re-plugged
adapter without a restart (which also resets its counters).

**A17 — Timestamps in drivox CSVs are relative to capture start**, and were
verified strictly monotonic (0 of 4,064,420 frames out of order). `canlog.packets`
asserts this rather than assuming it for future logs. The `can-analysis` CSVs
start mid-run (585.6 s, 501.3 s) and are relative to something else; they also
have no channel column and are assumed single-bus ch0.
