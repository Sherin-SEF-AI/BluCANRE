"""python-can <-> opendbc bridge, with a hard gate on transmission.

opendbc's diagnostic helpers (ecu_addrs, IsoTpParallelQuery, uds.CanClient) are
panda-free: they take a ``CanSendCallable`` and a ``CanRecvCallable`` from
``opendbc.car.can_definitions``. This module supplies both over python-can.

TRANSMISSION IS OFF BY DEFAULT AND FAILS CLOSED.
Constructing CanIO without ``allow_transmit=True`` makes send() raise. Nothing
reaches the bus by accident, and every frame that does is written to an audit
log for docs/COMPLIANCE.md.

Note the adapter still ACKs at the CAN controller level regardless -- python-can's
CANalyst-II backend has no listen_only parameter. See docs/COMPLIANCE.md.
"""

from __future__ import annotations

import json
import time

from opendbc.car.can_definitions import CanData


class TransmitDenied(RuntimeError):
    pass


class CanIO:
    """Implements CanRecvCallable / CanSendCallable over python-can."""

    def __init__(self, interface: str, channels: dict[int, str], bitrate: int = 500000,
                 allow_transmit: bool = False, audit_path: str | None = None):
        import can
        self.allow_transmit = allow_transmit
        self.audit_path = audit_path
        self._tx_count = 0
        self.buses: dict[int, object] = {}
        for bus_idx, chan in channels.items():
            if interface == "canalystii":
                self.buses[bus_idx] = can.Bus(interface=interface, channel=int(chan), bitrate=bitrate)
            else:
                self.buses[bus_idx] = can.Bus(interface=interface, channel=chan)

    # --- CanRecvCallable -------------------------------------------------
    def recv(self, wait_for_one: bool = False) -> list[list[CanData]]:
        """Drain every bus. Returns a list of packets, each a list of frames."""
        deadline = time.monotonic() + (0.1 if wait_for_one else 0.0)
        frames: list[CanData] = []
        while True:
            for bus_idx, bus in self.buses.items():
                while True:
                    msg = bus.recv(timeout=0.0)
                    if msg is None:
                        break
                    frames.append(CanData(msg.arbitration_id, bytes(msg.data), bus_idx))
            if frames or not wait_for_one or time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        return [frames]

    # --- CanSendCallable -------------------------------------------------
    def send(self, msgs: list[CanData]) -> None:
        if not self.allow_transmit:
            raise TransmitDenied(
                f"send() blocked: {len(msgs)} frame(s) refused. This process was not "
                "constructed with allow_transmit=True."
            )
        import can
        for m in msgs:
            bus = self.buses.get(m.src)
            if bus is None:
                continue
            bus.send(can.Message(arbitration_id=m.address, data=bytes(m.dat),
                                 is_extended_id=m.address > 0x7FF))
            self._tx_count += 1
            self._audit(m)

    def _audit(self, m: CanData) -> None:
        if not self.audit_path:
            return
        with open(self.audit_path, "a") as fh:
            fh.write(json.dumps({
                "t": round(time.time(), 6), "bus": m.src,
                "addr": f"0x{m.address:X}", "data": bytes(m.dat).hex(),
            }) + "\n")

    @property
    def tx_count(self) -> int:
        return self._tx_count

    def shutdown(self) -> None:
        for bus in self.buses.values():
            try:
                bus.shutdown()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()


def require_authorisation(args, what: str) -> None:
    """Fail closed unless the operator asserted both conditions explicitly."""
    if not getattr(args, "i_am_authorised_to_transmit", False):
        raise SystemExit(
            f"REFUSING: {what} transmits on the vehicle bus.\n"
            "  Pass --i-am-authorised-to-transmit to confirm you own the vehicle or\n"
            "  hold written authorisation from the fleet operator.\n"
            "  See docs/COMPLIANCE.md -- these preconditions are recorded as NOT MET."
        )
    if not getattr(args, "vehicle_stationary", False):
        raise SystemExit(
            "REFUSING: pass --vehicle-stationary to confirm the vehicle is stopped,\n"
            "  wheels chocked, handbrake engaged."
        )
