#!/usr/bin/env python3
"""A simulated TC 600 on a pseudo-terminal, for testing without hardware.

Prints the pty path it is listening on, then answers telegrams until killed:

    ./tools/fake_tc600.py
    # -> listening on /dev/pts/7
    ./tc600ctl.py -p /dev/pts/7 info

It models the pump on the real rig -- a TMH/TMU 071 P, 1500 Hz, on a 24 V
supply -- closely enough that the panel's charts show what the real one does:

  * run-up at the drive's power limit, ~80 W and ~3.8 A, the whole way up
  * at speed, bearing friction plus gas friction that grows with the gas
    throughput the turbopump is handling (fed in by tools/fake_rig.py)
  * the gas-mode-0 power line from the manual: 60 W at 1500 Hz rising to
    80 W at 1300 Hz. A gas load that needs more drags the rotor down to
    where the two balance -- exactly what tripped the real pump on
    2026-10-08 -- and a rotor held below the 80 % switchpoint trips with
    Err006, after raising the over-temperature flag
  * the supply sagging under load, as the real one does (23.25 V idle,
    ~21.5 V at 80 W)

The speed dynamics stay simple linear ramps, deliberately: a real coast-down
takes many minutes, and the selftest's power-down check cannot wait that long.

It also fails on demand. `FAULTS` makes it drop, corrupt, delay or refuse
chosen replies, go silent, or echo like a looping converter, so the tests can
show what the panel does with a link that is not perfect -- the real one is
not -- and `reset()` puts everything back between tests.
"""

from __future__ import annotations

import copy
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from drivers.tc600 import params, protocol  # noqa: E402

ADDRESS = 1

# Mutable state, keyed by parameter number, holding decoded Python values.
STATE: dict[int, object] = {
    10: False,   # PumpgStatn
    23: False,   # MotorPump
    2: False,    # Standby
    26: 0,       # OpModeTMP: 0=final speed, 1=speed setting mode
    303: "000000",
    308: 1500,   # SetRotSpd, Hz
    309: 0,      # ActualSpd, Hz
    310: 0.0,    # DrvCurrent, A
    313: 23.25,  # DrvVoltage, V -- the real supply idles here, not at 24.0
    316: 0,      # DrvPower, W
    311: 33802,  # OpHrsPump, h -- the real pump's count on 2026-10-08
    312: "017500",
    314: 1300,
    315: 1500,
    27: 0,       # GasMode: 0 heavy gases (argon)
    # Protective settings and the fault history, at the factory values the
    # manual gives (run-up time 15 min, switchpoint 80 %), so the panel's
    # diagnostics have something real-shaped to show.
    4: True,     # RUTimeCtrl: run-up time monitoring on
    700: 15,     # RUTimeSVal, min
    701: 80,     # SpdSwPt, %
    28: 0, 60: 2, 708: 100, 319: 1520,
    360: "000000", 361: "000000", 362: "000000", 363: "000000",
    364: "000000", 365: "000000", 366: "000000", 367: "000000",
    368: "000000", 369: "000000",
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

#: Faults to inject. Empty is a healthy pump. The counted kinds map a
#: parameter number to how many requests for it they spoil, -1 meaning every
#: one:
#:
#:   silent    True: answer nothing at all, as a drive switched off would
#:   quiet_after  n: answer n more requests, then nothing -- a drive that
#:             goes quiet half way through a reading
#:   drop      {param: n}  no reply -- the commonest fault on a real bus. A
#:                         write still takes effect: the reply is what is lost
#:   corrupt   {param: n}  a reply with a wrong checksum
#:   nodef     {param: n}  NO_DEF, as if the firmware lacked the parameter
#:   late      {param: n}  hold the reply back and send it just before the
#:                         reply to the next request: one that arrives after
#:                         its own exchange has given up
#:   value     {param: v}  report v instead of the simulated value
#:   echo      True: loop every request back before answering, as some
#:             RS-485 converters do
#:   delay_s   seconds before every reply: the real bus at 9600 baud
#:   drop_write    {param: n}  lose only the replies to writes; the writes
#:                 still happen
#:   refuse_write  {param: payload}  answer writes with an error payload
#:                 ("_LOGIC") and do nothing, as a drive refusing them would
#:   station_acks  True: writing P010 = 1 also acknowledges a fault, as
#:                 P010 does on Pfeiffer's newer drives (unconfirmed here)
FAULTS: dict = {}

#: Replies held back by a "late" fault, sent ahead of the next reply.
_held: list[bytes] = []

#: The state as the drive starts, for `reset()`.
_INITIAL = copy.deepcopy(STATE)


def reset() -> None:
    """A healthy pump at rest again: state, simulation and faults."""
    global _speed, _tms, _attained, _limited_for, _below_switchpoint_for
    global GAS_LOAD
    STATE.clear()
    STATE.update(copy.deepcopy(_INITIAL))
    FAULTS.clear()
    _held.clear()
    _speed, _tms, _attained = 0.0, 0.0, False
    _limited_for = _below_switchpoint_for = 0.0
    GAS_LOAD = 0.0


def _take(kind: str, number: int) -> bool:
    """Whether fault `kind` spoils this request for `number`, using it up."""
    counts = FAULTS.get(kind) or {}
    left = counts.get(number)
    if not left:
        return False
    if left > 0:
        counts[number] = left - 1
    return True


#: Rotor speed carried as a float, since STATE[309] is rounded for the wire
#: and accumulating sub-Hz steps into an int would never leave zero.
_speed = 0.0

#: Gas throughput the turbopump would carry at nominal speed, mbar l/s.
#: tools/fake_rig.py sets it from the simulated chamber; standalone it is 0.
GAS_LOAD = 0.0

# --- the drive's electrical model -----------------------------------------
#: Supply at no load, and its effective source resistance. Fitted to the
#: real rig: 23.25 V idle, 21.5-21.75 V at 3.7-3.9 A.
SUPPLY_V = 23.25
SUPPLY_OHMS = 0.42
#: Power the drive applies while running up ("maximum power is applied when
#: the pump starts", PM 0504 BE 4.4) -- 80-83 W measured.
RUNUP_W = 82.0
#: Bearing and windage loss at nominal speed with no gas: 9-17 W measured.
FRICTION_W = 12.0
#: Gas friction per mbar l/s of throughput, at nominal speed. Fitted so
#: that run 26's 3.3e-2 mbar settles near 1160 Hz at the 80 W cap.
GAS_W_PER_THROUGHPUT = 55.0
#: Linear ramps, Hz per second: run-up, being dragged down, coasting.
ACCEL_HZ_S = 37.5
DRAG_HZ_S = 6.0
COAST_HZ_S = 15.0
#: Seconds held at the power limit below full speed before the pump raises
#: over-temperature, and below the switchpoint before it trips with Err006.
#: Much shorter than the real pump's, so a simulated overload shows up in
#: minutes rather than a quarter of an hour.
OVERTEMP_AFTER_S = 90.0
TRIP_AFTER_S = 150.0

_tms = 0.0                # TMS temperature, carried as a float like _speed
_attained = False         # the set speed has been reached since motor-on
_limited_for = 0.0        # seconds at the power limit below full speed
_below_switchpoint_for = 0.0


def power_limit(speed: float, nominal: float) -> float:
    """The drive's permitted power at this speed, gas mode 0 (C to D).

    60 W at nominal rising linearly to 80 W at 1300/1500 of nominal, and 80 W
    below that. Gas mode 1 is a flat 80 W.
    """
    if STATE.get(27, 0):
        return 80.0
    knee = nominal * 1300.0 / 1500.0
    if speed <= knee:
        return 80.0
    return 80.0 - 20.0 * (speed - knee) / max(nominal - knee, 1.0)


def loss(speed: float, nominal: float) -> float:
    """Power the rotor dissipates at this speed: friction plus gas friction.

    Both scale with the square of speed here -- gas friction because the
    throughput the pump carries scales with its pumping speed, which scales
    with rotor speed, and the friction power is that throughput times speed.
    """
    fraction = speed / max(nominal, 1.0)
    return (FRICTION_W + GAS_W_PER_THROUGHPUT * GAS_LOAD) * fraction * fraction


def advance(dt: float) -> None:
    """Move the simulated rotor and recompute what the drive reports."""
    global _speed, _tms, _attained, _limited_for, _below_switchpoint_for
    nominal = float(max(STATE[315], 1))
    faulted = str(STATE[303]).strip() not in ("000000", "")
    # Measured on the real TC 600: the rotor turns only when the pumping
    # station is on AND the turbopump motor is enabled. Either one alone holds
    # it at 0 Hz -- which is exactly what makes "backing pump only" possible.
    driven = STATE[10] and STATE[23] and not faulted
    if driven:
        target = float(STATE[308])
        if STATE[2]:
            target = target * STATE[717] / 100.0
        if STATE[26]:
            target = STATE[308] * STATE[707] / 100.0
    else:
        target = 0.0
        _attained = False

    power = 0.0
    accelerating = False
    if driven:
        limit = RUNUP_W if not _attained else power_limit(_speed, nominal)
        needed = loss(_speed, nominal)
        if _speed < target - 0.5 and needed < limit:
            # Accelerate at full power: the whole limit goes into the rotor.
            _speed = min(target, _speed + ACCEL_HZ_S * dt)
            power = limit
            accelerating = True
        elif needed > limit:
            # More gas than the permitted power can carry: the drive gives
            # what it may and the rotor slows until loss and limit balance.
            _speed = max(0.0, _speed - DRAG_HZ_S * dt)
            power = limit
        else:
            power = needed
        if _speed >= target - 0.5:
            _attained = True
    else:
        _speed = max(0.0, _speed - COAST_HZ_S * dt)

    # Over-temperature and the run-up trip both come from riding the power
    # limit below full speed for too long.
    dragged = driven and _attained and _speed < 0.95 * nominal and power > 0
    _limited_for = _limited_for + dt if dragged else max(0.0, _limited_for - dt)
    below = driven and _attained and _speed < 0.8 * nominal
    _below_switchpoint_for = _below_switchpoint_for + dt if below else 0.0
    STATE[305] = _limited_for >= OVERTEMP_AFTER_S
    if _below_switchpoint_for >= TRIP_AFTER_S:
        STATE[303] = "Err006"          # run-up time error: below switchpoint
        _below_switchpoint_for = 0.0
        for number in range(369, 360, -1):     # the drive's error history
            STATE[number] = STATE[number - 1]
        STATE[360] = "Err006"

    current = power / SUPPLY_V if power else 0.0
    # The supply sags under the drive's current; the drive reports its input
    # voltage in 0.25 V steps, as the real one does.
    voltage = SUPPLY_V - SUPPLY_OHMS * current
    if power:
        current = power / voltage
    STATE[313] = round(voltage * 4) / 4.0
    STATE[310] = round(current, 2)
    STATE[316] = int(round(power))
    STATE[309] = int(_speed)
    # P307 is "accelerating", not "below target": a rotor dragged down by gas
    # is below its target and decelerating, and the real drive says so.
    STATE[307] = bool(accelerating or (driven and not _attained and _speed < target - 1))
    STATE[306] = bool(driven and abs(_speed - target) <= 1 and target > 0)
    STATE[302] = _speed >= 0.8 * nominal

    # TMS: approaches its setpoint while heating; reads 0 with the heater
    # off, which is what the real drive reports.
    if STATE.get(1):
        _tms = _tms or 22.0
        setpoint = float(STATE.get(704, 40))
        _tms += (setpoint - _tms) * min(1.0, dt / 60.0)
        STATE[331] = int(round(_tms))
    else:
        _tms = 0.0
        STATE[331] = 0


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
    if FAULTS.get("silent"):
        return None
    left = FAULTS.get("quiet_after")
    if left is not None:
        if left <= 0:
            return None
        FAULTS["quiet_after"] = left - 1

    # A dropped reply is lost on the way back: the drive has already done
    # what a write asked, though the host never hears that it did.
    answer = _answer(action, number, payload)
    if _take("drop", number):
        return None
    if action == protocol.ACTION_WRITE and _take("drop_write", number):
        return None
    if _take("corrupt", number):
        text = answer.decode("ascii").rstrip("\r")
        wrong = f"{(int(text[-3:]) + 1) % 256:03d}"
        answer = (text[:-3] + wrong + "\r").encode("ascii")
    if _take("late", number):
        _held.append(answer)
        return None
    return answer


def _answer(action: str, number: int, payload: str) -> bytes:
    """What a healthy drive answers to one request."""

    def reply(data: str) -> bytes:
        return protocol.build(ADDRESS, protocol.ACTION_WRITE, number, data)

    if number in UNIMPLEMENTED or number not in params.BY_NUMBER \
            or _take("nodef", number):
        return reply("NO_DEF")

    parameter = params.BY_NUMBER[number]

    if action == protocol.ACTION_READ:
        if number not in STATE:
            return reply("NO_DEF")
        forced = FAULTS.get("value") or {}
        return reply(parameter.encode(forced.get(number, STATE[number])))

    if action == protocol.ACTION_WRITE:
        if not parameter.writable:
            return reply("_LOGIC")
        refusal = (FAULTS.get("refuse_write") or {}).get(number)
        if refusal:
            return reply(refusal)
        try:
            value = parameter.decode(payload)
        except protocol.ProtocolError:
            return reply("_RANGE")
        STATE[number] = value
        if number == 9 and value:  # ErrorAckn
            STATE[303] = "000000"
        if number == 10 and value and FAULTS.get("station_acks"):
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
            if FAULTS.get("echo"):
                _send(master, frame + b"\r")
            # Replies held back from earlier requests go out first, ahead of
            # this one's -- which is what a reply arriving late looks like.
            late, _held[:] = list(_held), []
            answer = handle(frame + b"\r")
            out = b"".join(late) + (answer or b"")
            if out:
                time.sleep(0.01 + float(FAULTS.get("delay_s") or 0))
                _send(master, out)


def _send(master: int, data: bytes) -> None:
    try:
        os.write(master, data)
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
