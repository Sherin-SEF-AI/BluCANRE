# COMPLIANCE

Phase 0a scope. Nothing here is legal advice; it records the position taken and
what still needs sign-off.

## Bus safety — read this before connecting

**The recording tool never transmits.** `tools/record.py` has no `send()` code
path.

**But listen-only is a software policy, not a hardware guarantee.** python-can's
CANalyst-II backend exposes no `listen_only` parameter — verified:

```
CANalystIIBus.__init__(self, channel, device, bitrate, timing,
                       can_filters, rx_queue_size, **kwargs)
```

There is no way to request listen-only mode, so **the adapter still ACKs frames
at the CAN controller level**. It is electrically participating in the bus even
while our software stays silent. Do not describe this setup as "listen-only" to
a fleet operator.

For genuine isolation use an interface that supports it:

```bash
sudo ip link set can0 type can bitrate 500000 listen-only on restart-ms 100
sudo ip link set up can0
```

**Transmitting tools** (`discover_ecus.py`, `read_vin.py`) exist and are gated.
Both refuse to run without `--i-am-authorised-to-transmit` **and**
`--vehicle-stationary`; `blucanre/vehicle.py` also fails closed at the library
level, so `CanIO.send()` raises unless constructed with `allow_transmit=True`.
Every transmitted frame is appended to `out/tx_audit.jsonl` (timestamp, bus,
address, payload). Verified against a fake ECU on a virtual bus — see
`tools/fake_ecu.py`, which lets the whole flow be rehearsed without a vehicle.
Do not use `opendbc`'s `get_all_ecu_addrs` or `vin.get_vin` unmodified: the
former sends 512 tester-present frames in a single call, and the latter fans out
to ~511 parallel ISO-TP sessions across `0x700–0x7FF` plus extended addresses.
Both are far too aggressive for an unknown vehicle. Chunk and rate-limit.

## Preconditions — NOT yet confirmed

These are stated as unmet. They gate Phase 0b and any vehicle install.

- [ ] Vehicle ownership, or **written authorisation** from the fleet operator to
      connect diagnostic hardware.
- [ ] Fleet operator sign-off on aftermarket fitment (can affect warranty), on
      file before first install.
- [ ] Acknowledgement that probing happens on a stationary vehicle.

## AIS-140

Commercial passenger vehicles in India already carry a certified VLTD, so pilot
vehicles most likely have one fitted. ARAI/ICAT certification is slow and
expensive.

**Position BluCANRE as a complementary CAN-data device, never a tracker
replacement.** This keeps the project entirely out of the certification path
while selling precisely the data the certified box cannot see. Attempting to
replace the AIS-140 unit would make certification the critical path and probably
consume the whole budget.

## DPDP Act 2023

Driver location is personal data. Required before any location data is collected
or stored:

- notice to the data principal;
- lawful basis contracted through the fleet operator as employer;
- defined retention period;
- deletion on request;
- India-resident storage.

Cheap to design in now, very expensive to retrofit.

**Phase 0a status:** no location data is collected. The recorded CAN captures
contain no GPS. The `ground_truth.csv` produced by `record.py` may contain
odometer readings — vehicle data, not personal data, provided it is not linked to
an identified driver.

**VIN handling.** A VIN is a strong vehicle identifier and becomes personal data
once linked to an individual. `read_vin.py` must redact by default and require an
explicit flag to print in full.

## Data provenance

Existing captures under `/home/joai/drivox-canre/` and `/home/joai/can-analysis/`
are read-only inputs. They are not redistributed by this repo, and `logs/` and
`out/` are gitignored so recorded sessions are never committed accidentally.
