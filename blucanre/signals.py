"""Single source of truth for which messages live on which bus.

A DBC file has no concept of a bus, but ``CANParser`` filters incoming frames on
``src``. The Tigor exposes two buses at the OBD port and they are *not*
interchangeable: ``0x103`` and ``0x501`` appear on both with different meanings
(``0x501`` even has a different DLC). So we keep one DBC and split the message
list per bus here, driving one ``CANParser`` per bus.

Only empirically proven messages appear below. Candidate leads live in
``docs/SIGNALS.md``, never here.
"""

from __future__ import annotations

import re

BUS_CH0 = 0
BUS_CH1 = 1

# --- BMS cell voltages: ch1 0x4E8..0x4FA, 19 messages x 5 slots, 94 real cells.
CELL_BASE_ADDR = 0x4E8
CELL_MSG_COUNT = 19
CELL_COUNT = 94
CELL_HZ = 3.35

CELL_MESSAGES: list[tuple[str, int]] = [
    (f"BMS_CELL_{i:02d}", 3) for i in range(1, CELL_MSG_COUNT + 1)
]
CELL_SIGNALS: list[str] = [f"cell_v_{i:03d}" for i in range(1, CELL_COUNT + 1)]

# Physically plausible envelope for a Li-ion cell. Values outside this are
# quarantined, never clamped -- an out-of-bounds cell usually means the DBC is
# wrong or gateway firmware changed.
CELL_MV_MIN = 2500
CELL_MV_MAX = 4250

# Pack is 94 cells in series.
PACK_V_MIN = 300.0
PACK_V_MAX = 320.0

MESSAGES_CH0: list[tuple[str, int]] = []
MESSAGES_CH1: list[tuple[str, int]] = list(CELL_MESSAGES)

MESSAGES_BY_BUS: dict[int, list[tuple[str, int]]] = {
    BUS_CH0: MESSAGES_CH0,
    BUS_CH1: MESSAGES_CH1,
}

# Addresses seen on both buses. We define only the ch1 variant in the DBC
# because both ch0 variants are fully static across the whole 1926 s capture.
KNOWN_CROSS_BUS_COLLISIONS = {0x103, 0x501}


_SG_RE = re.compile(
    r"^\s*SG_\s+(?P<name>\w+)\s*:\s*\d+\|\d+@\d[+-]\s*"
    r"\([^)]*\)\s*\[(?P<lo>[-\d.eE+]+)\|(?P<hi>[-\d.eE+]+)\]"
)


def signal_bounds(dbc_path: str) -> dict[str, tuple[float, float]]:
    """Read each signal's declared ``[min|max]`` straight from the DBC text.

    opendbc's parser keeps factor/offset but discards the declared range, so we
    parse it ourselves rather than duplicating bounds in Python. That keeps the
    DBC the single source of truth, which is what makes the bounds check
    meaningful.
    """
    bounds: dict[str, tuple[float, float]] = {}
    with open(dbc_path) as fh:
        for line in fh:
            m = _SG_RE.match(line)
            if m:
                bounds[m.group("name")] = (float(m.group("lo")), float(m.group("hi")))
    return bounds
