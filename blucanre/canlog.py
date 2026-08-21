"""Log loading for BluCANRE reverse-engineering tools.

The canonical internal form is exactly what ``opendbc``'s ``CANParser.update``
expects, so tools never have to reshape frames themselves.

Three input formats are supported and sniffed from the header/first line:

* **drivox CSV** ``timestamp_s,can_id_hex,dlc,b0..b7,data_hex,label,channel``
  Dual-channel. We parse ``data_hex`` rather than the ``b0..b7`` columns
  because short frames leave those columns empty.
* **can-analysis CSV** ``timestamp,arbitration_id,dlc,data_hex,data_dec``
  Single bus; ``src`` is forced to 0.
* **candump -L** ``(1699999999.123456) can0 1A2#DEADBEEF``
  ``src`` comes from the trailing digits of the interface name.
"""

from __future__ import annotations

import csv
import re
from typing import Iterator, NamedTuple

__all__ = ["Frame", "Format", "sniff", "load", "packets", "survey"]


class Frame(NamedTuple):
    t: float
    """Timestamp in seconds. May be relative to the start of capture."""
    addr: int
    data: bytes
    src: int
    """Bus/channel index. Must match the ``bus`` a CANParser was built with."""


class Format:
    DRIVOX = "drivox-csv"
    CANANALYSIS = "cananalysis-csv"
    CANDUMP = "candump-log"


# (1699999999.123456) can0 1A2#DEADBEEF   — also tolerates CAN FD's '##'
_CANDUMP_RE = re.compile(
    r"^\(\s*(?P<t>\d+(?:\.\d+)?)\s*\)\s+(?P<if>\S+)\s+(?P<id>[0-9A-Fa-f]+)#{1,2}(?P<data>[0-9A-Fa-f]*)"
)
_IFACE_DIGITS = re.compile(r"(\d+)\s*$")


def sniff(path: str) -> str:
    """Identify the log format from its first line."""
    with open(path, "r", errors="replace") as fh:
        first = fh.readline().strip()
    if not first:
        raise ValueError(f"{path}: empty file")

    lowered = first.lower()
    if lowered.startswith("timestamp_s,can_id_hex"):
        return Format.DRIVOX
    if lowered.startswith("timestamp,arbitration_id"):
        return Format.CANANALYSIS
    if _CANDUMP_RE.match(first):
        return Format.CANDUMP
    raise ValueError(f"{path}: unrecognised CAN log format (first line: {first[:80]!r})")


def _hex_to_bytes(h: str) -> bytes:
    h = h.strip()
    if len(h) % 2:
        # Never seen in practice; refuse rather than silently truncate.
        raise ValueError(f"odd-length hex payload {h!r}")
    return bytes.fromhex(h)


def _load_drivox(path: str) -> Iterator[Frame]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield Frame(
                float(row["timestamp_s"]),
                int(row["can_id_hex"], 16),
                _hex_to_bytes(row["data_hex"]),
                int(row["channel"]),
            )


def _load_cananalysis(path: str) -> Iterator[Frame]:
    with open(path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            yield Frame(
                float(row["timestamp"]),
                int(row["arbitration_id"], 16),
                _hex_to_bytes(row["data_hex"]),
                0,
            )


def _load_candump(path: str) -> Iterator[Frame]:
    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            m = _CANDUMP_RE.match(line)
            if not m:
                raise ValueError(f"{path}:{lineno}: unparsable candump line {line[:80]!r}")
            digits = _IFACE_DIGITS.search(m.group("if"))
            yield Frame(
                float(m.group("t")),
                int(m.group("id"), 16),
                _hex_to_bytes(m.group("data")),
                int(digits.group(1)) if digits else 0,
            )


_LOADERS = {
    Format.DRIVOX: _load_drivox,
    Format.CANANALYSIS: _load_cananalysis,
    Format.CANDUMP: _load_candump,
}


def load(path: str, fmt: str | None = None) -> Iterator[Frame]:
    """Yield every frame in ``path`` in file order."""
    return _LOADERS[fmt or sniff(path)](path)


def packets(path: str, bin_ms: float = 10.0, strict: bool = True) -> Iterator[tuple[int, list]]:
    """Yield ``(t_nanos, [(addr, data, src), ...])`` batched into time bins.

    ``CANParser.update`` takes a list of such packets. Batching is not an
    optimisation detail: feeding 4M frames one update() at a time is
    unworkably slow now that opendbc's parser is pure Python.

    Timestamps must be non-decreasing. Real captures are (the 4M-frame drivox
    session is strictly monotonic), but a corrupted or merged log would silently
    produce garbage timeouts, so ``strict`` fails loudly instead.
    """
    bin_s = bin_ms / 1000.0
    batch: list = []
    bin_start: float | None = None
    last_t = float("-inf")

    for f in load(path):
        if f.t < last_t:
            if strict:
                raise ValueError(
                    f"{path}: timestamps went backwards ({f.t} after {last_t}). "
                    "Refusing to guess an order; sort the log or pass strict=False."
                )
        else:
            last_t = f.t

        if bin_start is None:
            bin_start = f.t
        elif f.t - bin_start >= bin_s and batch:
            yield int(batch_t * 1e9), batch
            batch = []
            bin_start = f.t

        batch.append((f.addr, f.data, f.src))
        batch_t = f.t

    if batch:
        yield int(batch_t * 1e9), batch


def survey(path: str) -> dict:
    """One pass returning basic shape: frame count, duration, and (src, addr) census."""
    census: dict[tuple[int, int], int] = {}
    n = 0
    t_first = t_last = None
    for f in load(path):
        n += 1
        if t_first is None:
            t_first = f.t
        t_last = f.t
        key = (f.src, f.addr)
        census[key] = census.get(key, 0) + 1
    return {
        "path": path,
        "format": sniff(path),
        "frames": n,
        "duration_s": (t_last - t_first) if n else 0.0,
        "buses": sorted({src for src, _ in census}),
        "census": census,
    }
