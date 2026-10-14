"""How fresh the readings are, how soon a run reacts, and the clock set an
hour either way."""

from __future__ import annotations

import os
import tempfile
import time

from lab import db as dbmod
from lab import pump as pumpmod
from lab import registry, runs
from tools import fake_tc600, fake_tpg
from tools.checks.common import (
    wait_until, attempt, Rig, ClockStep)


def test_timing(pump_port: str, gauge_port: str) -> None:
    print("\nHow fresh the readings are, and how soon a run reacts")
    from lab import alarms

    directory = tempfile.mkdtemp(prefix="selftest-timing-")
    database = dbmod.Database(os.path.join(directory, "timing.db"))

    def wait_ended(engine, seconds):
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if not engine.status()["running"]:
                return time.monotonic() - started
            time.sleep(0.05)
        return None

    # --- the pump reading the safety checks act on ---------------------------
    def freshness():
        fake_tc600.reset()
        with Rig(pump_port, interval=None) as rig:
            oldest, seen = 0.0, set()
            end = time.monotonic() + 6
            while time.monotonic() < end:
                snap = rig.pump.snapshot()
                if snap.get("connected"):
                    oldest = max(oldest, snap.get("age") or 0.0)
                    seen.add(snap.get("timestamp"))
                time.sleep(0.05)
        return (oldest <= 1.5 and len(seen) >= 5,
                f"oldest {oldest:.1f} s, {len(seen)} readings in 6 s")
    attempt("the pump reading is never much more than a second old", freshness)

    # --- a condition is acted on within seconds, at any sample interval -----
    def overheat_slow_run():
        fake_tc600.reset()
        fake_tpg.reset()
        with Rig(pump_port, gauge_port, interval=None) as rig:
            engine = runs.RunEngine(database, rig.devices, roles=runs.ALL_ROLES)
            started = engine.start("base_calibration", {"interval_s": 60})
            if not started.get("ok"):
                return False, started.get("error")
            time.sleep(2.5)                      # its first sample is taken
            fake_tc600.FAULTS["value"] = {305: True}
            took = wait_ended(engine, 10)
            message = (engine.status().get("last") or {}).get("message", "")
            if took is None:
                engine.stop("selftest")
                wait_ended(engine, 10)
            wait_until(lambda: not (engine.status().get("secure") or {})
                       .get("active"), 15)
        return (took is not None and took <= 4.0 and "temperature" in message,
                f"ended {took:.1f} s after the flag: {message[:50]}"
                if took is not None else "still running 10 s after the flag")
    attempt("an over-temperature flag ends a run sampling once a minute within "
            "seconds", overheat_slow_run)

    def silent_pump():
        fake_tc600.reset()
        fake_tpg.reset()
        saved = getattr(runs.RunEngine, "MAX_SILENT_S", None)
        runs.RunEngine.MAX_SILENT_S = 3.0
        try:
            with Rig(pump_port, gauge_port, interval=None) as rig:
                engine = runs.RunEngine(database, rig.devices, roles=runs.ALL_ROLES)
                engine.start("hold", {"interval_s": 60, "duration_min": 10})
                time.sleep(2.0)
                fake_tc600.FAULTS["silent"] = True
                took = wait_ended(engine, 15)
                message = (engine.status().get("last") or {}).get("message", "")
                if took is None:
                    engine.stop("selftest")
                    wait_ended(engine, 10)
        finally:
            fake_tc600.FAULTS.clear()
            if saved is None:
                del runs.RunEngine.MAX_SILENT_S
            else:
                runs.RunEngine.MAX_SILENT_S = saved
        return (took is not None and "lost contact" in message,
                f"ended after {took:.1f} s: {message[:50]}" if took is not None
                else "still running 15 s into the silence")
    attempt("a silent pump ends a run after seconds, not after thirty samples",
            silent_pump)

    def quiet_mid_reading():
        fake_tc600.reset()
        with Rig(pump_port, interval=None) as rig:
            rig.fresh()                          # a reading has just begun
            fake_tc600.FAULTS["quiet_after"] = 3
            went = time.monotonic()
            gone = wait_until(lambda: not rig.pump.snapshot().get("connected"),
                              20, 0.05)
            took = time.monotonic() - went
        return (gone and took <= 6.0,
                f"reported out of contact after {took:.1f} s" if gone
                else "still reported in contact after 20 s")
    attempt("a pump that goes quiet half way through a reading is reported "
            "within seconds", quiet_mid_reading)

    def stalled_run():
        class Stuck(runs.Procedure):
            name = "selftest_stuck"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                if len(engine._samples) == 1:
                    time.sleep(7.0)              # a command that will not return
                return len(engine._samples) >= 3

        runs.CATALOGUE[Stuck.name] = Stuck
        try:
            engine = runs.RunEngine(database, registry.Registry(),
                                    roles=runs.ALL_ROLES)
            supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                           auto_recover=False)
            engine.start(Stuck.name, {})
            found = wait_until(lambda: (supervisor.check() or True) and any(
                a["key"] == "engine_stalled"
                for a in supervisor.describe()["recent"]), 9, 0.2)
            wait_ended(engine, 10)
        finally:
            del runs.CATALOGUE[Stuck.name]
        return found, ("alarm raised" if found else "no alarm while the run "
                       "checked nothing for 7 s")
    attempt("a run that stops ticking raises an alarm", stalled_run)

    # --- the wall clock moving under a run --------------------------------------
    def clock_jumps():
        class Recording(runs.Procedure):
            name = "selftest_clock"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

        runs.CATALOGUE[Recording.name] = Recording
        clock = ClockStep()
        try:
            engine = runs.RunEngine(database, registry.Registry(),
                                    roles=runs.ALL_ROLES)
            engine.start(Recording.name, {})
            time.sleep(1.0)
            clock.set(3600.0)                    # an hour forward
            time.sleep(1.0)
            clock.set(-3600.0)                   # an hour back
            time.sleep(1.0)
            clock.restore()
            time.sleep(0.5)
            engine.stop("selftest")
            wait_ended(engine, 5)
            times = [s["t"] for s in engine._samples]
        finally:
            clock.restore()
            del runs.CATALOGUE[Recording.name]
        steps = [b - a for a, b in zip(times, times[1:])]
        return (steps and min(steps) > 0 and max(steps) < 2.0,
                f"{len(times)} samples, steps {min(steps):.2f} to "
                f"{max(steps):.2f} s" if steps else "no samples")
    attempt("run time does not jump when the wall clock is set an hour either "
            "way", clock_jumps)

    def frozen_gauge_clock_back():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-3       # roughed: the start is allowed
        fake_tc600.STATE[10] = True
        clock = ClockStep()
        saved = pumpmod.PRESSURE_MAX_AGE_S
        with Rig(pump_port, gauge_port) as rig:
            ready = wait_until(
                lambda: rig.pump.interlocks()["motor_on"]["allowed"], 10, 0.1)
            rig.gauge._stop.set()                # the gauge's reading freezes
            rig.gauge._thread.join(timeout=5)
            try:
                pumpmod.PRESSURE_MAX_AGE_S = 1.0
                clock.set(-3600.0)
                time.sleep(1.6)
                rule = rig.pump.interlocks()["motor_on"]
            finally:
                clock.restore()
                pumpmod.PRESSURE_MAX_AGE_S = saved
        return (ready and not rule["allowed"],
                rule["reason"][:70] if not rule["allowed"]
                else "a frozen reading still allowed the start")
    attempt("a frozen gauge reading stops counting when the clock is set back",
            frozen_gauge_clock_back)

    fake_tc600.reset()
    fake_tpg.reset()
    database.close()
