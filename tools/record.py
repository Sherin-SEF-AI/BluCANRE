#!/usr/bin/env python3
"""Record a CAN session with event markers and dashboard ground truth.

SAFETY
------
This tool never transmits. There is no send() code path.

But note carefully: python-can's CANalyst-II backend exposes no ``listen_only``
parameter (verified: CANalystIIBus.__init__ takes only channel, device, bitrate,
timing, can_filters, rx_queue_size). The adapter therefore **still ACKs frames at
the hardware level**. "Listen-only" here is a software policy, not a hardware
guarantee. See docs/COMPLIANCE.md. If true bus isolation is required, use an
interface that supports listen-only mode (e.g. SocketCAN `ip link set can0 type
can listen-only on`).

GROUND TRUTH IS THE POINT
-------------------------
Neither prior session recorded any dashboard reading, which is exactly why speed,
SoC, odometer and torque units remain unresolved after 4 million frames. A log
without ground truth cannot answer those questions no matter how long it is.
Every protocol step may request readings; they land in ground_truth.csv.

Usage:
    record.py --out sessions/s1 --protocol protocols/phase0b.yaml
    record.py --out sessions/s1 --duration 120        # free-run, markers on stdin
    record.py --out /tmp/t --backend socketcan --channels vcan0,vcan1 --duration 5
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import sys
import threading
import time

# A marker placed at the very end of a log is worthless: there is no "after" to
# compare against. The prior evidence.md was invalidated exactly this way -- its
# drive_on marker sat at t=1925.966 s in a 1925.97 s capture, so every
# "stopped_changing" row was just the log ending.
MARKER_TAIL_S = 30.0

# A multi-channel adapter buffers each channel separately, so python-can drains
# them in bursts and frames arrive globally out of order even though each
# channel's own timestamps are monotonic. Measured skew on the CANalyst-II is up
# to ~23 ms. We hold frames briefly and emit in timestamp order; downstream tools
# assert monotonicity, and an unsorted log would fail them.
REORDER_WINDOW_S = 0.25

CSV_HEADER = ["timestamp_s", "can_id_hex", "dlc", "b0", "b1", "b2", "b3", "b4",
              "b5", "b6", "b7", "data_hex", "label", "channel"]


class Recorder:
    def __init__(self, backend: str, channels: list[str], bitrate: int):
        self.backend, self.channels, self.bitrate = backend, channels, bitrate
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._buses: list = []
        self.t0: float | None = None
        self._t0_mono: float = 0.0
        self._t_ref: float | None = None
        self._pending: list = []
        self._t_max: float = 0.0
        self._t_origin: float | None = None
        self.gaps: list[tuple[float, float]] = []

    def _open_all(self) -> list[tuple[object, int | None]]:
        """Return [(bus, fixed_idx or None)]. None means read idx from msg.channel.

        The CANalyst-II is a single USB device: python-can claims the whole
        device per Bus, so opening one Bus per channel fails the second with
        "[Errno 16] Resource busy". Its backend instead takes a sequence of
        channels on one Bus and tags each message with msg.channel.
        """
        import can
        if self.backend == "canalystii":
            chans = tuple(int(c) for c in self.channels)
            bus = can.Bus(interface="canalystii", channel=chans, bitrate=self.bitrate)
            return [(bus, None)]
        return [(can.Bus(interface="socketcan", channel=c), i)
                for i, c in enumerate(self.channels)]

    def _pump(self, bus, fixed_idx: int | None):
        last_ok = time.monotonic()
        seen_any = False
        while not self._stop.is_set():
            try:
                msg = bus.recv(timeout=0.5)
            except Exception as exc:                     # adapter dropped out
                self._q.put(("error", fixed_idx if fixed_idx is not None else -1, str(exc)))
                time.sleep(0.5)
                continue
            now = time.monotonic()
            if msg is None:
                continue
            idx = fixed_idx if fixed_idx is not None else int(getattr(msg, "channel", 0) or 0)
            # A gap is silence *between* frames. Leading silence (waiting for the
            # bus to wake) and trailing silence are not dropouts, and reporting
            # them as such would poison gap-exclusion in downstream analysis.
            if seen_any and now - last_ok > 2.0:
                self._q.put(("gap", idx, (last_ok - self._t0_mono, now - self._t0_mono)))
            seen_any = True
            last_ok = now
            self._q.put(("frame", idx, msg))

    def start(self):
        self.t0 = time.time()
        self._t0_mono = time.monotonic()
        for bus, fixed_idx in self._open_all():
            self._buses.append(bus)
            th = threading.Thread(target=self._pump, args=(bus, fixed_idx), daemon=True)
            th.start()
            self._threads.append(th)

    def stop(self):
        self._stop.set()
        for b in self._buses:
            try:
                b.shutdown()
            except Exception:
                pass

    def _flush(self, writer, up_to: float) -> int:
        """Emit buffered frames with t <= up_to, in timestamp order."""
        if not self._pending:
            return 0
        self._pending.sort(key=lambda r: r[0])
        # Frames from two channels interleave, so the true earliest timestamp is
        # only known once the first reorder window has filled. Fix the origin
        # then, so timestamp_s always starts at exactly 0 and never goes negative.
        if self._t_origin is None:
            self._t_origin = self._pending[0][0]
        i = 0
        while i < len(self._pending) and self._pending[i][0] <= up_to:
            t, row = self._pending[i]
            writer.writerow([f"{t - self._t_origin:.6f}"] + row)
            i += 1
        if i:
            del self._pending[:i]
        return i

    def flush_all(self, writer) -> int:
        """Emit everything still buffered. Call once after the last drain."""
        return self._flush(writer, float("inf"))

    def drain(self, writer, deadline: float | None = None) -> int:
        """Move queued frames to CSV. Returns frames written."""
        n = 0
        while True:
            try:
                kind, idx, payload = self._q.get(timeout=0.05)
            except queue.Empty:
                if deadline is None or time.monotonic() >= deadline:
                    return n
                continue
            if kind == "gap":
                self.gaps.append(payload)
                continue
            if kind == "error":
                print(f"  [ch{idx}] bus error: {payload}", file=sys.stderr)
                continue
            msg = payload
            if msg.timestamp:
                # Do not assume an epoch base: the canalystii backend reports
                # device-relative time, socketcan reports wall clock. Anchor on
                # the first frame so t is always relative to capture start.
                if self._t_ref is None:
                    self._t_ref = msg.timestamp
                t = msg.timestamp - self._t_ref
            else:
                t = time.time() - self.t0
            data = bytes(msg.data)
            row = [f"0x{msg.arbitration_id:03X}", len(data)]
            row += [data[i] if i < len(data) else "" for i in range(8)]
            row += [data.hex(), "", idx]
            self._pending.append((t, row))
            self._t_max = max(self._t_max, t)
            n += self._flush(writer, self._t_max - REORDER_WINDOW_S)
            if deadline is not None and time.monotonic() >= deadline:
                return n


def _ask(prompt: str) -> str:
    """input() that treats a closed stdin as 'stop', not as a crash.

    Without this a piped or nohup'd run dies with a bare EOFError mid-protocol
    and the markers and ground truth collected so far are never written.
    """
    try:
        return input(prompt)
    except EOFError:
        raise KeyboardInterrupt("stdin closed")


def run_protocol(path: str, rec: Recorder, writer, markers: list, truth: list):
    import yaml
    steps = yaml.safe_load(open(path))["steps"]
    total = len(steps)
    for i, step in enumerate(steps, 1):
        for rep in range(step.get("repeat", 1)):
            tag = step["id"] + (f"_r{rep+1}" if step.get("repeat", 1) > 1 else "")
            print(f"\n[{i}/{total}] {tag}\n    {step['prompt']}")
            _ask("    press ENTER when in position...")
            t_start = time.time() - rec.t0
            markers.append((t_start, f"{tag}_start", ""))
            dur = float(step.get("duration_s", 10))
            print(f"    recording {dur:.0f}s ...", end="", flush=True)
            rec.drain(writer, deadline=time.monotonic() + dur)
            markers.append((time.time() - rec.t0, f"{tag}_end", ""))
            print(" done")
            for key in step.get("ground_truth", []):
                val = _ask(f"    dashboard {key} = ").strip()
                if val:
                    truth.append((time.time() - rec.t0, key, val, "", tag))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="canalystii", choices=["canalystii", "socketcan"])
    ap.add_argument("--channels", default="0,1")
    ap.add_argument("--bitrate", type=int, default=500000)
    ap.add_argument("--duration", type=float, help="free-run seconds (ignored with --protocol)")
    ap.add_argument("--protocol")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    channels = args.channels.split(",")
    rec = Recorder(args.backend, channels, args.bitrate)

    markers: list = []
    truth: list = []
    csv_path = os.path.join(args.out, "can_frames.csv")
    n = 0
    # newline="" keeps the writer from emitting CRLF; prior CSVs are CRLF and
    # break naive line splitting downstream.
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_HEADER)
        print(f"recording {args.backend} channels={channels} -> {csv_path}")
        rec.start()
        try:
            if args.protocol:
                run_protocol(args.protocol, rec, w, markers, truth)
            else:
                end = time.monotonic() + (args.duration or 60.0)
                n += rec.drain(w, deadline=end)
        except KeyboardInterrupt:
            print("\ninterrupted")
        finally:
            rec.stop()
            n += rec.drain(w)
            n += rec.flush_all(w)

    duration = time.time() - rec.t0

    # Refuse to ship a marker with no usable "after" window.
    bad = [m for m in markers if m[1].endswith("_start") and duration - m[0] < MARKER_TAIL_S]
    with open(os.path.join(args.out, "markers.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "label", "note"])
        for t, label, note in markers:
            w.writerow([f"{t:.6f}", label, note])
    with open(os.path.join(args.out, "ground_truth.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["t_s", "key", "value", "unit", "step"])
        for row in truth:
            w.writerow([f"{row[0]:.6f}"] + list(row[1:]))

    meta = {
        "backend": args.backend, "channels": channels, "bitrate": args.bitrate,
        "start_wall_time": rec.t0, "duration_s": round(duration, 3),
        "total_frames": n, "marker_count": len(markers),
        "ground_truth_count": len(truth),
        "gaps": [[round(a, 3), round(b, 3)] for a, b in rec.gaps],
        "listen_only": False,
        "listen_only_note": "CANalyst-II via python-can cannot be put in listen-only "
                            "mode; the adapter ACKs at hardware level. No frames were "
                            "transmitted by this tool.",
    }
    with open(os.path.join(args.out, "session_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)

    print(f"\n{n} frames, {duration:.1f}s, {len(markers)} markers, {len(truth)} ground-truth readings")
    if rec.gaps:
        print(f"WARNING: {len(rec.gaps)} bus gap(s) recorded -- exclude these windows from analysis")
    if bad:
        print(f"WARNING: {len(bad)} marker(s) within {MARKER_TAIL_S:.0f}s of log end "
              f"({[m[1] for m in bad]}). There is no usable 'after' window; "
              "re-record these events or they cannot be diffed.")
        return 2
    if n == 0:
        print("WARNING: no frames captured -- check adapter, bitrate and vehicle power")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
