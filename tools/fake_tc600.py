#!/usr/bin/env python3
"""A simulated TC 600 on a pseudo-terminal, for testing without hardware.

Prints the pty path it is listening on, then answers telegrams until killed:

    ./tools/fake_tc600.py
    # -> listening on /dev/pts/7
    ./tc600ctl.py -p /dev/pts/7 info

It models a pump that spins up towards its setpoint, so `monitor` and `log`
show changing numbers.
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tc600 import params, protocol  # noqa: E402

ADDRESS = 1

# Mutable state, keyed by parameter number, holding decoded Python values.
STATE: dict[int, object] = {
    10: False,   # PumpgStatn
    23: False,   # MotorPump
    2: False,    # Standby
    26: 0,       # OpModeTMP: 0=final speed, 1=speed setting mode
    303: "000000",
    308: 1000,   # SetRotSpd, Hz
    309: 0,      # ActualSpd, Hz
    310: 0.0,    # DrvCurrent, A
    313: 48.0,   # DrvVoltage, V
    316: 0,      # DrvPower, W
    311: 1234,   # OpHrsPump, h
    312: "0110",
    314: 1300,
    315: 1000,
    349: "TC600",
    354: "0201",
    302: False, 306: False, 307: False, 300: False, 304: False, 305: False,
    707: 100.0, 717: 66, 797: 1,
    # Accessory connectors: HEAT/TMS, VENT, FV PUMP.
    1: False, 331: 0, 333: False, 334: 90, 335: 1, 704: 40, 32: 0,
    12: True, 30: 0, 720: 50, 721: 3600,
    25: 0, 710: 0, 711: 0,
}

# Parameters this fake firmware deliberately does not implement, so the
# NO_DEF path gets exercised by `verify`.
UNIMPLEMENTED = {326, 330, 336, 342, 346, 397, 398, 399}


#: Rotor speed carried as a float, since STATE[309] is rounded for the wire
#: and accumulating sub-Hz steps into an int would never leave zero.
_speed = 0.0


def advance(dt: float) -> None:
    """Move the simulated rotor towards its target speed."""
    global _speed
    # Measured on the real TC 600: the rotor turns only when the pumping
    # station is on AND the turbopump motor is enabled. Either one alone holds
    # it at 0 Hz -- which is exactly what makes "backing pump only" possible.
    running = STATE[10] and STATE[23]
    if running:
        target = STATE[308]
        if STATE[2]:
            target = int(target * STATE[717] / 100.0)
        if STATE[26]:
            target = int(STATE[308] * STATE[707] / 100.0)
    else:
        target = 0

    step = 25 * dt if _speed < target else 15 * dt
    if abs(target - _speed) < step:
        _speed = float(target)
    else:
        _speed += step if target > _speed else -step

    STATE[309] = int(_speed)
    fraction = _speed / max(STATE[315], 1)
    STATE[316] = int(120 * fraction)
    STATE[310] = round(2.5 * fraction, 2)
    STATE[307] = bool(running and _speed < target - 1)
    STATE[306] = bool(running and abs(_speed - target) <= 1 and target > 0)
    STATE[302] = _speed >= 0.8 * STATE[315]


def handle(frame: bytes) -> bytes | None:
    text = frame.decode("ascii", errors="replace").strip("\r\n")
    if len(text) < 13:
        return None

    body, received = text[:-3], text[-3:]
    if protocol.checksum(body) != received:
        return None  # A real pump stays silent on a corrupt telegram.

    address, action, number = int(body[0:3]), body[3:5], int(body[5:8])
    payload = body[10:]

    if address not in (ADDRESS, protocol.GROUP_ADDRESS):
        return None

    def reply(data: str) -> bytes:
        return protocol.build(ADDRESS, protocol.ACTION_WRITE, number, data)

    if number in UNIMPLEMENTED or number not in params.BY_NUMBER:
        return reply("NO_DEF")

    parameter = params.BY_NUMBER[number]

    if action == protocol.ACTION_READ:
        if number not in STATE:
            return reply("NO_DEF")
        return reply(parameter.encode(STATE[number]))

    if action == protocol.ACTION_WRITE:
        if not parameter.writable:
            return reply("_LOGIC")
        try:
            value = parameter.decode(payload)
        except protocol.ProtocolError:
            return reply("_RANGE")
        STATE[number] = value
        if number == 9 and value:  # ErrorAckn
            STATE[303] = "000000"
        return reply(parameter.encode(STATE[number]))

    return reply("_LOGIC")


def serve(master: int, should_stop=None) -> None:
    """Answer telegrams arriving on pty `master` until `should_stop()` is true.

    Runs happily in a background thread, which is how tools/selftest.py drives
    it without spawning a second process.
    """
    os.set_blocking(master, False)
    buffer = bytearray()
    last = time.monotonic()

    while should_stop is None or not should_stop():
        now = time.monotonic()
        advance(now - last)
        last = now

        try:
            chunk = os.read(master, 256)
        except BlockingIOError:
            chunk = b""
        except OSError:
            # Nobody currently has the slave side open.
            chunk = b""
        if not chunk:
            time.sleep(0.02)
            continue

        buffer.extend(chunk)
        while b"\r" in buffer:
            frame, _, rest = bytes(buffer).partition(b"\r")
            buffer = bytearray(rest)
            answer = handle(frame + b"\r")
            if answer:
                time.sleep(0.01)
                try:
                    os.write(master, answer)
                except OSError:
                    pass


def main() -> int:
    master, slave = os.openpty()  # keep `slave` open so the pty stays alive
    print(f"listening on {os.ttyname(slave)}", flush=True)
    try:
        serve(master)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
