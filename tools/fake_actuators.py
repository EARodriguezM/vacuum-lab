#!/usr/bin/env python3
"""Simulated drivers for the process hardware, wired to the fake chamber.

Nothing is connected to the real rig yet, so a deposition run today would be
one manual prompt after another. These stand in: they accept the same `set`
and `read` calls a real driver will, and they move the simulated chamber, so
the whole recipe -- crossover, base pressure, argon backfill, pre-sputter,
deposition, recovery -- runs end to end on a laptop.

That makes them useful twice. Today they let the recipe be written and
watched without hardware. Later they stay as the reference for what a real
driver has to implement, and as the thing the selftest runs against so the
recipe keeps working when the real ones arrive.

    ./webserver.py --simulate-actuators ...

They deliberately model the awkward parts rather than being ideal:

  * the magnetron refuses to strike outside the pressure window a real
    discharge needs, which is what catches a recipe that opens the shutter
    before the argon is in
  * the gate valve takes a few seconds to travel
  * argon flow ramps rather than stepping, so the pressure curve has the
    shape the operator will actually see
"""

from __future__ import annotations

import time

from tools import fake_tpg

#: A Penning/magnetron discharge will not sustain outside roughly this band.
#: Too thin and there is nothing to ionise; too dense and it arcs.
PLASMA_MIN_MBAR = 5.0e-4
PLASMA_MAX_MBAR = 1.0e-1


class SimGasFlow:
    """Argon admission. Ramps rather than stepping, like a real MFC."""

    RAMP_SCCM_PER_S = 20.0

    def __init__(self, chamber):
        self.chamber = chamber
        self.target = 0.0
        self._changed = time.monotonic()

    def set(self, value):
        self.target = max(0.0, float(value or 0.0))
        self._changed = time.monotonic()
        return self.target

    def read(self):
        # Move the chamber towards the setpoint each time it is read; the
        # sampler polls often enough for this to look like a ramp.
        elapsed = time.monotonic() - self._changed
        span = self.RAMP_SCCM_PER_S * elapsed
        current = self.chamber.gas_sccm
        if abs(self.target - current) <= span:
            current = self.target
        else:
            current += span if self.target > current else -span
        self.chamber.gas_sccm = current
        self._changed = time.monotonic()
        return round(current, 2)


class SimGateValve:
    """Gate valve between chamber and turbopump, in percent open."""

    TRAVEL_PERCENT_PER_S = 25.0

    def __init__(self, chamber):
        self.chamber = chamber
        self.target = 100.0

    def set(self, value):
        self.target = max(0.0, min(100.0, float(value)))
        self._changed = time.monotonic()
        return self.target

    def read(self):
        current = self.chamber.gate_percent
        if not hasattr(self, "_changed"):
            return round(current, 1)
        span = self.TRAVEL_PERCENT_PER_S * (time.monotonic() - self._changed)
        if abs(self.target - current) <= span:
            current = self.target
        else:
            current += span if self.target > current else -span
        self.chamber.gate_percent = current
        self._changed = time.monotonic()
        return round(current, 1)


class SimMagnetron:
    """Sputter source. Refuses to strike outside a plausible pressure window.

    This is the part worth simulating honestly: a recipe that opens the
    shutter before argon is admitted, or that tries to run the plasma at base
    pressure, should fail here rather than quietly "work" and produce a run
    record showing a deposition that never happened.
    """

    def __init__(self, chamber):
        self.chamber = chamber
        self.power = 0.0
        self.struck = False

    def set(self, value):
        power = float(value or 0.0)
        if power <= 0:
            self.power, self.struck = 0.0, False
            return 0.0
        pressure = self.chamber.pressure
        if not PLASMA_MIN_MBAR <= pressure <= PLASMA_MAX_MBAR:
            raise RuntimeError(
                f"the plasma will not strike at {pressure:.2E} mbar — a "
                f"magnetron discharge needs roughly {PLASMA_MIN_MBAR:.0E} to "
                f"{PLASMA_MAX_MBAR:.0E} mbar. Admit the argon first.")
        self.power, self.struck = power, True
        return power

    def read(self):
        # A discharge goes out if the pressure leaves the window under it.
        if self.struck and not (PLASMA_MIN_MBAR <= self.chamber.pressure
                                <= PLASMA_MAX_MBAR):
            self.power, self.struck = 0.0, False
        return self.power


class SimShutter:
    """The flap over the substrate. True is open."""

    def __init__(self, chamber):
        self.chamber = chamber
        self.open = False

    def set(self, value):
        self.open = bool(value)
        return self.open

    def read(self):
        return self.open


def attach(hardware, chamber=None) -> dict:
    """Give every actuator a simulated driver. Returns what was attached."""
    chamber = chamber or fake_tpg.CHAMBER
    drivers = {
        "gas": SimGasFlow(chamber),
        "gate": SimGateValve(chamber),
        "magnetron": SimMagnetron(chamber),
        "shutter": SimShutter(chamber),
    }
    for key, driver in drivers.items():
        hardware.attach(key, driver)
    return drivers
