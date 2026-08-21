# BluCANRE

CAN reverse engineering for the **Tata Tigor EV / Xpres-T EV** (Ziptron, ~26 kWh),
and the foundation for an EV fleet-telematics platform.

**Current phase: 0a — offline DBC derivation.** The deliverable is a DBC whose
every signal is empirically proven, plus the tooling to extend it. No edge,
backend or dashboard code exists yet, by design: the project spec gates all of
that behind a working DBC.

## What is proven so far

`dbc/tata_tigor_ev.dbc` contains the **94-cell BMS voltage map** (`ch1
0x4E8–0x4FA`) plus the BMS's **broadcast cell extremes** (`ch1 0x108`). Across 121,329 cell frames from a 32-minute
capture: every cell in 3275–3283 mV, pack sum steady at 308.12–308.49 V, cell
delta 4–5 mV. Three independent decoders agree.

That single message set is enough to build Phase 3's flagship analytic —
cell-delta trending, which predicts pack failure weeks ahead.

Everything else (SoC, speed, gear, odometer, charge state) is **not derivable
from existing data**: the only substantial capture is of a stationary vehicle.
See `docs/SIGNALS.md`.

## Setup

```bash
uv sync     # fetches CPython 3.12 (opendbc pins <3.13 for pycapnp)
```

opendbc is a read-only path dependency at `/home/joai/opendbc` (commit
`17286eb`). It is never forked or modified; the DBC lives outside its tree and is
loaded by absolute path.

## Tools

| Tool | Purpose |
|---|---|
| `tools/record.py` | Capture a session with markers and dashboard ground truth |
| `tools/validate_dbc.py` | Gate the DBC: dual-parser diff, bounds, invariants |
| `tools/selftest.py` | Prove the validator rejects injected defects |
| `tools/bit_activity.py` | Per-bit heatmap and field-boundary candidates |
| `tools/diff_logs.py` | Isolate bits that changed between logs or across a marker |
| `tools/discover_ecus.py` | TesterPresent scan for responding ECUs **(transmits)** |
| `tools/read_vin.py` | Read the VIN over ISO-TP **(transmits)** |
| `tools/fake_ecu.py` | Fake ISO-TP responder on vcan, to rehearse the above offline |
| `tools/correlate.py` | Hunt unknown signals against the confirmed cell series |
| `tools/verify_all.py` | Run the full Phase 0a acceptance checklist |

## Verify

One command runs the whole Phase 0a acceptance checklist:

```bash
uv run python tools/verify_all.py
```

13 criteria, ~2.5 min. Covers: opendbc imports on 3.12; the DBC validating
against all five available captures; the validator rejecting five injected
defects; the analysis tools completing over the full 251 MB capture; the
diff guards refusing unsupportable comparisons; and both transmit tools failing
closed without authorisation.

## Transmitting tools

`discover_ecus.py` and `read_vin.py` are the only tools that put frames on the
bus. They **fail closed**: both require `--i-am-authorised-to-transmit` and
`--vehicle-stationary`, and every transmitted frame is appended to
`out/tx_audit.jsonl`.

Neither uses opendbc's convenience wrapper. `get_all_ecu_addrs` hands 512 frames
to a single `can_send` call, and `get_vin` with functional addressing fans out to
~511 parallel ISO-TP sessions — both far too aggressive for an unknown vehicle.
Ours chunk, rate-limit, and escalate.

Rehearse offline before touching the car:

```bash
uv run python tools/fake_ecu.py --channel vcan1 --addrs 0x7E0 &
uv run python tools/read_vin.py --backend socketcan --channel vcan1 \
    --i-am-authorised-to-transmit --vehicle-stationary
```

**The authorisation preconditions in `docs/COMPLIANCE.md` are recorded as NOT
MET.** Passing the flags asserts otherwise; that is your call to make, not the
tool's.

## Next: capture a session

The critical path. Prior sessions recorded 4.1M frames and still could not
resolve speed, SoC or odometer — because nothing recorded what the dashboard
said. The protocol fixes that.

```bash
uv run python tools/record.py --out sessions/s3 \
    --protocol protocols/phase0b_session.yaml
```

Highest-value steps: `dc_charge` (the only event that swings pack voltage far
enough to identify it), `odo_delta`, and the GPS-locked `steady_30`/`steady_50`
holds that settle whether `0x247` is speed, RPM or something else.

**Read `docs/COMPLIANCE.md` first.** The CANalyst-II cannot be put in listen-only
mode by python-can, so it ACKs on the bus even though this tool never transmits.
Vehicle-authorisation preconditions are recorded there as **not yet met**.

## Rules

1. Never fabricate a signal definition. A guessed signal is worse than a missing
   one — it silently corrupts every downstream analytic.
2. Every signal in the DBC carries a `CM_` comment stating its evidence.
3. Unproven ideas go in `docs/SIGNALS.md` as quarantined leads, never in the DBC.
4. Uncertainties go in `docs/ASSUMPTIONS.md` rather than blocking progress.
