#!/usr/bin/env python3
"""A simulated Pfeiffer TPG 252 A on a pseudo-terminal.

Prints the pty path it is listening on, then answers mnemonics until killed:

    ./tools/fake_tpg.py
    # -> listening on /dev/pts/8
    ./tpgctl.py -p /dev/pts/8 info

It models a real chamber rather than returning a constant, so a pump-down or
rate-of-rise run against it produces a curve with the right shape:

    dp/dt = (Q_leak + Q_outgas(t)) / V  -  S_effective * p / V

Two gauges are fitted, as on a typical rig: a Pirani on channel 1 covering
atmosphere down to its 1e-4 mbar floor, and a cold cathode on channel 2 that
only reads below 1e-2 mbar. Each reports the underrange and sensor-off
statuses at the right pressures, which is what makes the status handling in
`tpg/device.py` and `lab/gauge.py` worth testing at all.

`set_pumping()` links it to the pump simulator: tools/selftest.py feeds the
fake TC 600's rotor speed in, so the two simulators model one rig.
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tpg import commands, protocol  # noqa: E402


class Chamber:
    """A pumped volume with a leak, outgassing, and two gauges on it."""

    #: Litres.
    VOLUME = 20.0
    #: Turbopump speed at full rotation, litres per second.
    FULL_SPEED = 60.0
    #: Backing pump alone, litres per second.
    ROUGHING_SPEED = 1.5
    #: Real leak, mbar litres per second. Sets the ultimate pressure.
    LEAK = 2.0e-7
    #: Outgassing at t=0, decaying with a 400 s time constant.
    OUTGAS_INITIAL = 5.0e-4
    OUTGAS_TAU = 400.0

    #: One standard cubic centimetre per minute, in mbar litres per second.
    #: 1 sccm = 1013.25 mbar x 0.001 L / 60 s.
    SCCM = 1013.25 * 0.001 / 60.0

    def __init__(self):
        self.pressure = 1013.0          # mbar, atmosphere
        self.rotor_fraction = 0.0       # 0..1 of nominal turbopump speed
        self.station_on = False
        self.isolated = False           # valve shut: rate-of-rise tests
        #: Process gas admitted, sccm. Argon during a deposition.
        self.gas_sccm = 0.0
        #: Gate valve between chamber and turbopump, percent open. Throttling
        #: is how a turbopump at full speed holds a working pressure four
        #: decades above its base instead of pumping the gas straight away.
        self.gate_percent = 100.0
        self.started = time.monotonic()

    def outgassing(self) -> float:
        age = time.monotonic() - self.started
        return self.OUTGAS_INITIAL * math.exp(-age / self.OUTGAS_TAU)

    def speed(self) -> float:
        """Effective pumping speed in L/s for the current machine state."""
        if self.isolated:
            return 0.0
        speed = self.ROUGHING_SPEED if self.station_on else 0.0
        # A turbopump only pumps usefully once the foreline is roughed out;
        # below ~1 mbar it takes over, above that it contributes nothing.
        # The gate valve throttles what reaches it.
        if self.rotor_fraction > 0 and self.pressure < 1.0:
            speed += (self.FULL_SPEED * self.rotor_fraction
                      * max(0.0, min(100.0, self.gate_percent)) / 100.0)
        return speed

    def advance(self, dt: float) -> None:
        if dt <= 0:
            return
        throughput = (self.LEAK + self.outgassing()
                      + self.gas_sccm * self.SCCM)
        speed = self.speed()
        # Solved over the step rather than Euler-stepped: at 60 L/s in 20 L
        # the time constant is a third of a second, so a 1 s Euler step would
        # overshoot straight through zero into negative pressure.
        if speed > 0:
            tau = self.VOLUME / speed
            equilibrium = throughput / speed
            self.pressure = equilibrium + \
                (self.pressure - equilibrium) * math.exp(-dt / tau)
        else:
            self.pressure += throughput * dt / self.VOLUME
        self.pressure = max(self.pressure, 1.0e-11)


CHAMBER = Chamber()


def set_pumping(rotor_fraction: float, station_on: bool) -> None:
    """Tell the simulated chamber what the simulated pump is doing."""
    CHAMBER.rotor_fraction = max(0.0, min(1.0, rotor_fraction))
    CHAMBER.station_on = station_on


# --- unit state -----------------------------------------------------------

#: Modelled on the real TPG 252 A (firmware BG509727-C) wherever its
#: behaviour is known, because the published family documentation turned out
#: to differ from it in three ways that all broke real code: switching
#: functions answer "lower,upper" with no channel field, only SP1 and SP2
#: exist, and `SEN` reports 3 for a live gauge rather than 2.
STATE = {
    "unit": 0,                      # 0 = mbar
    "firmware": "BG509727-C",
    "sensors": [2, 1],              # ch1 on, ch2 off (struck once it is safe)
    "filter": [1, 1],
    "keylock": 0,
    "error": "0000",
    "setpoints": {                  # (lower, upper) -- no channel field
        1: (1.0e-3, 5.0e-3),
        2: (1.0e-5, 5.0e-5),
    },
}

#: Gauge fitted to each channel, with the range it can actually measure.
GAUGES = [
    {"id": "TPR", "low": 5.0e-5, "high": 1000.0},   # Pirani
    {"id": "IKR9", "low": 1.0e-9, "high": 1.0e-2},  # cold cathode
]

#: Mnemonics this fake firmware does not implement, so the NAK path and the
#: capability probe both get exercised. Matches what the real TPG 252 A
#: refuses, which notably includes degas and the third and fourth switching
#: functions.
UNIMPLEMENTED = {"RSX", "NAD", "COR", "DGS", "SCX", "SP3", "SP4",
                 "DCB", "DCC", "DCD", "DCS", "HDL", "SP5", "SP6"}


def measure(channel: int) -> tuple[int, float]:
    """One channel's (status, value), in whatever unit the controller is set to."""
    gauge = GAUGES[channel]
    if STATE["sensors"][channel] != 2:
        return 4, 0.0                                   # sensor off
    pressure = CHAMBER.pressure
    if pressure < gauge["low"]:
        return 1, _convert(gauge["low"])
    if pressure > gauge["high"]:
        return 2, _convert(gauge["high"])
    return 0, _convert(pressure)


def _convert(mbar: float) -> float:
    factor = commands.TO_MBAR.get(STATE["unit"], 1.0)
    return mbar / factor if factor else mbar


def auto_strike() -> None:
    """Model the controller's own auto-on for the cold cathode gauge."""
    if CHAMBER.pressure < 5.0e-3 and STATE["sensors"][1] == 1:
        STATE["sensors"][1] = 2
    elif CHAMBER.pressure > 1.0e-2 and STATE["sensors"][1] == 2:
        STATE["sensors"][1] = 1


def handle(line: str) -> str | None:
    """Answer one command line, or return None to signal NAK."""
    parts = [p.strip() for p in line.strip().split(",")]
    mnemonic, args = parts[0].upper(), parts[1:]

    if mnemonic in UNIMPLEMENTED:
        return None

    if mnemonic == "PNR":
        return STATE["firmware"]

    if mnemonic == "TID":
        return ",".join(g["id"] for g in GAUGES)

    if mnemonic == "UNI":
        if args:
            code = int(args[0])
            if code not in commands.UNITS:
                return None
            STATE["unit"] = code
        return f"{STATE['unit']}"

    if mnemonic == "PRX":
        fields = []
        for channel in (0, 1):
            status, value = measure(channel)
            fields.extend([str(status), f"{value:.4E}"])
        return ",".join(fields)

    if mnemonic in ("PR1", "PR2"):
        status, value = measure(int(mnemonic[2]) - 1)
        return f"{status},{value:.4E}"

    if mnemonic == "SEN":
        if args:
            for index, raw in enumerate(args[:2]):
                code = int(raw)
                if code in (1, 2):
                    STATE["sensors"][index] = code
        # The real unit answers 3, not 2, for a gauge that is on and reading.
        return ",".join("3" if s == 2 else str(s) for s in STATE["sensors"])

    if mnemonic == "FIL":
        if args:
            for index, raw in enumerate(args[:2]):
                code = int(raw)
                if code not in commands.FILTER:
                    return None
                STATE["filter"][index] = code
        return ",".join(str(f) for f in STATE["filter"])

    if mnemonic == "CAL":
        return "1.000,1.000"

    if mnemonic in ("OFC", "OFD", "PUC"):
        return "0,0"

    if mnemonic == "FSR":
        return "3,3"

    if mnemonic == "ERR":
        return STATE["error"]

    if mnemonic == "LOC":
        if args:
            STATE["keylock"] = int(args[0])
        return str(STATE["keylock"])

    if mnemonic in ("TLC", "WDT"):
        return "0"

    if mnemonic == "BAU":
        # The real unit answers 4 while running at 9600, which is not the
        # family's documented 0/1/2 encoding.
        return "4"

    if mnemonic == "SPS":
        closed = []
        for number in sorted(STATE["setpoints"]):
            lower, _upper = STATE["setpoints"][number]
            # No channel association on this model; the switching functions
            # follow whichever channel is reading.
            status, value = measure(1 if STATE["sensors"][1] == 2 else 0)
            closed.append("1" if status == 0 and value < lower else "0")
        return ",".join(closed)

    if mnemonic in ("SP1", "SP2"):
        number = int(mnemonic[2])
        if len(args) >= 2:
            STATE["setpoints"][number] = (float(args[0]), float(args[1]))
        lower, upper = STATE["setpoints"][number]
        return f"{lower:.4E},{upper:.4E}"

    if mnemonic == "RES":
        STATE["error"] = "0000"
        return "0"

    if mnemonic == "SAV":
        return "0"

    return None


def serve(master: int, should_stop=None) -> None:
    """Answer commands arriving on pty `master` until `should_stop()` is true.

    Implements the two-leg handshake: the mnemonic gets an ACK, and the data
    only goes out once the ENQ arrives.
    """
    os.set_blocking(master, False)
    buffer = bytearray()
    pending: str | None = None
    last = time.monotonic()

    while should_stop is None or not should_stop():
        now = time.monotonic()
        CHAMBER.advance(now - last)
        last = now
        auto_strike()

        try:
            chunk = os.read(master, 256)
        except (BlockingIOError, OSError):
            chunk = b""
        if not chunk:
            time.sleep(0.01)
            continue

        for byte in chunk:
            single = bytes([byte])

            if single == protocol.ETX:
                buffer.clear()
                pending = None
                continue

            if single == protocol.ENQ:
                if pending is not None:
                    _write(master, pending.encode("ascii") + protocol.TERMINATOR)
                    pending = None
                continue

            if single == b"\n":
                continue
            if single == b"\r":
                line = buffer.decode("ascii", "replace")
                buffer.clear()
                if not line.strip():
                    continue
                answer = handle(line)
                if answer is None:
                    _write(master, protocol.NAK + protocol.TERMINATOR)
                    pending = None
                else:
                    _write(master, protocol.ACK + protocol.TERMINATOR)
                    pending = answer
                continue

            buffer.append(byte)


def _write(master: int, payload: bytes) -> None:
    try:
        os.write(master, payload)
    except OSError:
        pass


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pumping", type=float, default=None,
                        help="simulate a turbopump at this fraction of full "
                             "speed (0-1), with the station on")
    parser.add_argument("--pressure", type=float, default=None,
                        help="start at this pressure in mbar instead of "
                             "atmosphere")
    args = parser.parse_args()

    if args.pressure is not None:
        CHAMBER.pressure = args.pressure
    if args.pumping is not None:
        set_pumping(args.pumping, True)

    master, slave = os.openpty()  # keep `slave` open so the pty stays alive
    print(f"listening on {os.ttyname(slave)}", flush=True)
    if args.pumping:
        print(f"pumping at {args.pumping:.0%} of full speed", flush=True)
    try:
        serve(master)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
