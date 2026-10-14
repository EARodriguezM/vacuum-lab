"""What every section of the selftest shares: the tally, the simulators'
threads, and the helpers for a rig that misbehaves on purpose."""

from __future__ import annotations

import os
import threading
import traceback
import time

from lab import gauge as gaugemod
from lab import pump as pumpmod
from lab import registry


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: The tally, read by tools/selftest.py after each section.
PASSED = 0
FAILED = 0
#: Every failed check's label, in order, for the summary at the end.
FAILURES: list[str] = []
#: The section running now, set by the runner; prefixes each failure.
SECTION = ""


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        FAILURES.append(f"{SECTION}: {label}" if SECTION else label)
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def spawn(target, master, stop, name):
    """Run a simulator in a thread that reports its own death.

    A bare thread swallows its traceback, and a dead simulator looks exactly
    like a timeout from the client side.
    """
    def serve_loudly():
        try:
            target(master, stop.is_set)
        except Exception:
            traceback.print_exc()
            print(f"!! {name} simulator thread died")

    thread = threading.Thread(target=serve_loudly, daemon=True, name=name)
    thread.start()
    return thread


class _QuietEngine:
    """Just enough engine for `begin_vacuum` to run with no rig behind it."""

    def log(self, *args, **kwargs) -> None:
        pass

    #: What `pump_status()` should report. Empty unless a test sets it.
    status: dict = {}

    def prompt(self):
        return None

    def require_argon_gas_mode(self) -> None:
        pass

    def pump_status(self) -> dict:
        return self.status

    def pump_param(self, name, value) -> dict:
        return {"ok": True}

    def nominal_speed(self):
        return 1000.0

    def last_base_pressure(self, exclude_run=None):
        return None

    def run_id(self):
        return None

    def require(self, actuator, value) -> bool:
        return True

    def stop_choice(self):
        return None

    def shutdown_pump(self, reason: str = "") -> dict:
        return {"ok": True}

    def shutdown_status(self):
        return None

    def enforce_full_speed(self, reason: str) -> None:
        pass

    def pump_command(self, action: str, value=None) -> dict:
        return {"ok": True}


def wait_until(predicate, timeout: float = 15.0, step: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


def attempt(label: str, test) -> None:
    """A check whose own code may fail: an exception counts as a FAIL rather
    than a crash that would skip every section after it."""
    try:
        ok, detail = test()
    except Exception as exc:                              # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    check(label, bool(ok), str(detail)[:150])


class Rig:
    """Pump and gauge services on the simulators, closed again afterwards."""

    def __init__(self, pump_port: str | None = None,
                 gauge_port: str | None = None, interval: float | None = 0.3):
        self.devices = registry.Registry()
        self.pump = self.gauge = None
        if pump_port:
            self.pump = self.devices.add(
                pumpmod.PumpService("pump", pump_port, address=1))
            if interval is not None:
                self.pump.interval = interval
        if gauge_port:
            self.gauge = self.devices.add(
                gaugemod.GaugeService("gauge", gauge_port, baudrate=9600))
            if interval is not None:
                self.gauge.interval = interval
        if self.pump and self.gauge:
            self.pump.attach_pressure(self.gauge)

    def __enter__(self) -> "Rig":
        wait_until(lambda: all(d.snapshot().get("connected")
                               for d in self.devices), 10, 0.1)
        return self

    def __exit__(self, *exc) -> bool:
        for service in self.devices:
            service.close()
            service._thread.join(timeout=5)
            service.drop()
        return False

    def fresh(self, accept=lambda snap: True, service=None,
              timeout: float = 10.0) -> dict | None:
        """The first reading published from now on that `accept` likes."""
        service = service or self.pump
        since = service.snapshot().get("timestamp", 0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = service.snapshot()
            if snap.get("timestamp", 0) > since and accept(snap):
                return snap
            time.sleep(0.05)
        return None


def watching(supervisor, seconds: float) -> None:
    """Let the supervisor look at the rig for a while, as its thread would."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        supervisor.check()
        time.sleep(0.1)


class ClockStep:
    """Move the wall clock -- what a network time correction does to a box
    with no clock battery -- for everything that reads time.time()."""

    def __init__(self):
        self._real = time.time

    def set(self, offset: float) -> None:
        real = self._real
        time.time = lambda: real() + offset

    def restore(self) -> None:
        time.time = self._real


class FailingDatabase:
    """A database whose writes fail on demand, as a full SD card's do.

    `fail(name, n)` is asked before the n-th call of each write method;
    True makes that call raise. Everything else goes to the real one.
    """

    WRITES = ("add_sample", "add_event", "finish_run", "set_phase")

    def __init__(self, real, fail):
        self._real = real
        self._fail = fail
        self.calls: dict[str, int] = {}

    def __getattr__(self, name):
        attribute = getattr(self._real, name)
        if name not in self.WRITES:
            return attribute

        def write(*args, **kwargs):
            count = self.calls[name] = self.calls.get(name, 0) + 1
            if self._fail(name, count):
                import sqlite3
                raise sqlite3.OperationalError("disk I/O error (selftest)")
            return attribute(*args, **kwargs)
        return write


def _try(read):
    """`read()`, or False if it raised -- for polling a server coming up."""
    try:
        return read()
    except Exception:                                    # noqa: BLE001
        return False
