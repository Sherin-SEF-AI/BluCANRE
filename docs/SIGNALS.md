# SIGNALS — reverse-engineering lab notebook

Every signal in `dbc/tata_tigor_ev.dbc` must appear here with the evidence that
put it there. Anything not proven lives in **Quarantined leads**, never in the DBC.

Vehicle: Tata Tigor EV / Xpres-T EV (Ziptron, ~26 kWh).
Bus access: OBD-II port, two channels, 500 kbit/s. **Not gateway-gated** — 110
free-running IDs are broadcast without any request being sent.

---

## Confirmed

### BMS cell voltages — `ch1 0x4E8–0x4FA` → `BMS_CELL_01..19`, `cell_v_001..094`

**Layout.** 19 consecutive IDs, DLC 8, ~3.35 Hz. Skip the leading 4-bit nibble,
then five 12-bit big-endian unsigned fields, each a cell voltage in millivolts.
DBC big-endian start bits are **3, 23, 27, 47, 51** — note this is *not* the
intuitive 3/15/27/39/51; the irregular spacing is a consequence of 12-bit fields
straddling byte boundaries and is exactly the kind of detail that silently
produces plausible-looking garbage. Verified by decoding
`0x4E8 = 0cd4cd5cd4cd5cd3` two independent ways (manual nibble extraction and
cantools) to the same `[3284, 3285, 3284, 3285, 3283]`.

**Evidence** (source: `drivox-canre/session_20260812_184203`, 4,064,420 frames,
1925.97 s):

| Test | Result |
|---|---|
| Leading nibble is zero | 121,329 / 121,329 frames, all 19 IDs, no exception |
| Slot 0 of `0x4FA` is zero | 6,385 / 6,385 frames — and in **no other ID** |
| ⇒ real cell count | 18×5 + 4 = **94** |
| Cell values in 2500–4250 mV envelope | 100% (observed 3275–3283 mV) |
| Pack sum (94 cells) | **308.12–308.49 V**, steady |
| Cell delta | 4–5 mV |
| Plausibility | 94s × 3.28 V ≈ 308 V; 26 kWh / 308 V ≈ 84 Ah — consistent with Ziptron |
| Full-log replay via `validate_dbc.py` | 165,108 complete cell vectors, zero violations |

**Why this is trustworthy.** The layout was established *structurally* — 19
uniform messages, a single dead slot, and a pack sum landing on the correct
voltage for a 26 kWh pack — not by curve-fitting. Three independent decoders
(hand nibble extraction, cantools, opendbc `CANParser`) agree byte-for-byte.

**Still unverified (Phase 0b).** No dashboard cross-check has been done. The
decode is self-consistent and physically plausible, but "cell 1 in the DBC is
physically cell 1 in the pack" is an assumption — cell *ordering* is not proven,
only the set of 94 values. Ordering does not affect min/max/delta/sum analytics,
which is everything Phase 3 needs.

### `cell_slot_unused` — `ch1 0x4FA`, slot 0

Declared deliberately rather than omitted, so `validate_dbc.py` can assert it
stays zero. If a future capture ever populates it, that is a real finding (pack
reconfiguration or firmware change) rather than a silent blind spot.

---

## Quarantined leads — NOT in the DBC

Carried over from `drivox-canre/data/xprest_seed.yaml`, `xprest.dbc` and
`Desktop/tigor-RE/tigorsteering.py`. Recorded so the work is not lost; **none
have been admitted to the DBC**. Each is annotated with its status against the
4M-frame capture.

| Claim | Prior confidence | Status now |
|---|---|---|
| `AliveCounter247` — `ch0 0x247` b7 alternates `0x8D↔0x8E` | `high`, *"test: None needed; pattern is unambiguous"* | **REFUTED.** b7 = `0x8D` in all 19,155 frames. A counter that never counts. |
| `SpeedOrRpmRaw_BE` — `ch0 0x247` b5–6 BE, range 3363–6066 | `medium` | **DOES NOT REPRODUCE.** Measures 1076–2430 here, and never approaches zero while the pack is demonstrably at rest. Does not behave like road speed. |
| Steering angle — `0x247`, centre 3477, 26 units/deg, *"channel 1, OBD pins 3+11"* | n/a | **CONFLICTS.** Same 16 bits as the speed claim (b5–6), so these are two incompatible labels for one field, not two fields. The channel claim is also wrong: all 19,155 `0x247` frames are on **ch0**, none on ch1. |
| `TorqueDemandRaw` — `0x111` b1, 0–178 | `medium` | **NOT EXERCISED.** `0x111` is all-zeros at 100 Hz throughout. Consistent with a parked vehicle — neither confirmed nor refuted. |
| `AccumulatorOrOdo` — `0x398` b0–1, monotonic | `low` | Untested. Needs `odo_delta`. |
| `PackTempCandidate` — `0x398` b3 | `low` | Untested, and ~35 byte-fields across the bus sit in plausible °C ranges. Not separable without a thermal reference. |
| `0x386` "stops at 80 s", `0x389` "late spike" | n/a | Untested anomalies. |

### `0x247` — the honest state

`ch0 0x247` has **exactly one** varying quantity: bytes 5–6 (b0–b4 and b7 are
constant). Over the capture the 16-bit BE word decays smoothly from ~2390 to
~2170, so it is neither road speed (the vehicle never moved) nor plausibly
steering angle (nobody turns a wheel monotonically for 28 minutes). It is one
unresolved 16-bit field with two refuted labels. **It must not enter the DBC
until `steady_30`/`steady_50` and `steering_*` ground truth settles it.**

### Why the prior `evidence.md` cannot be relied on

Its marker-correlation table reports 20 byte-fields that "stopped_changing" at
the `drive_on` marker. That marker is at **t = 1925.966 s** in a capture of
duration **1925.97 s** — a 4 ms window. Everything stopped changing because the
log ended. All 20 rows are an artifact, and every row carries `novelty 0.00`,
which the table presents as a finding. `tools/record.py` now refuses to emit a
marker within 30 s of log end, and the Phase 0b protocol ends with a deliberate
60 s `final_idle` tail.

---

## ECU map

**Not yet populated.** Requires transmitting on a stationary vehicle, and the
authorisation preconditions in `docs/COMPLIANCE.md` are recorded as NOT MET.

The tooling is built and rehearsed. `tools/discover_ecus.py` (chunked
TesterPresent scan) and `tools/read_vin.py` (escalating ISO-TP query) were
verified end to end against `tools/fake_ecu.py` on a virtual bus: the scan
recovered exactly the three addresses the fake ECU listened on (`0x726`,
`0x7E0`, `0x7E1`) and the VIN read returned the expected 17-character VIN from
tier 1 in 2 transmitted frames.

One correction found during that rehearsal, worth remembering: opendbc's
`get_ecu_addrs` returns the address each reply **came from** — the RX address —
not the address queried. An ECU listening on `0x7E0` is reported as `0x7E8`.
`discover_ecus.py` now infers `tx = rx - 8` and says so in its output.

When run on the vehicle, record the resulting `out/ecu_scan.json` here along
with FW versions (`0x22 0xF195`) and ECU serials (`0x22 0xF18C`) per responder.

---

## What existing data can never answer

The 4M-frame capture is a **stationary vehicle throughout**. Speed, gear,
odometer, regen, charge state and torque units are not derivable from it at any
level of analytical effort — the events simply never occurred. This is why
`tools/record.py` plus `protocols/phase0b_session.yaml` are the critical path,
and why every driving/charging step carries `ground_truth` keys.
