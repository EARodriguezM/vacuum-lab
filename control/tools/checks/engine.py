"""A whole recorded run on the simulators: the crossover interlock, phase
timeouts, the full-vacuum procedure, a guided power-down, and what
happens after a drive fault."""

from __future__ import annotations

import os
import tempfile
import threading
import time

from lab import db as dbmod
from lab import gauge as gaugemod
from lab import pump as pumpmod
from lab import export, registry, runs
from tools import fake_tc600, fake_tpg
from tools.checks.common import (
    check, wait_until, watching, _QuietEngine)


def wait_for_gauge(service, accept, timeout: float = 6.0) -> float | None:
    """Wait until the gauge sampler publishes a pressure `accept` likes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = service.best_pressure()
        if accept(value):
            return value
        time.sleep(0.2)
    return service.best_pressure()


def check_crossover_interlock(devices) -> None:
    """The turbopump start is gated on chamber pressure, not on a timer.

    A turbopump started against a foreline that has not been roughed out
    cannot develop compression: it draws its current limit and trips. How
    long the backing pump has been running does not answer that question, so
    the gate is the gauge reading and nothing else.
    """
    pump_service = devices.get("pump")
    gauge_service = devices.get("gauge")
    limit = pumpmod.TURBO_START_MAX_MBAR

    check("turbopump start is held while the backing pump is off",
          pump_service.interlocks()["motor_on"]["allowed"] is False,
          pump_service.interlocks()["motor_on"]["reason"][:44])

    # Everything below is about the *pressure* gate, so get past the backing
    # pump one first: start the station and wait for the sampler to see it.
    check("starting the station is always allowed",
          pump_service.interlocks()["on"]["allowed"] is True)
    pump_service.command("on")
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if pump_service.snapshot()["setpoints"].get("PumpgStatn") is True:
            break
        time.sleep(0.2)

    # With no gauge attached the pressure is unknown, and unknown is refused.
    rule = pump_service.interlocks()["motor_on"]
    check("turbopump start is refused with no gauge attached",
          rule["allowed"] is False and "no gauge" in rule["reason"],
          rule["reason"][:52])

    pump_service.attach_pressure(gauge_service)

    # At atmosphere the Pirani is over its range and the cold cathode is off,
    # so neither channel is a pressure. That is not permission to start.
    fake_tpg.CHAMBER.pressure = 1013.0
    wait_for_gauge(gauge_service, lambda p: p is None)
    rule = pump_service.interlocks()["motor_on"]
    check("turbopump start is refused with no valid reading",
          rule["allowed"] is False and "valid pressure" in rule["reason"],
          rule["reason"][:52])

    # Roughing: a real number, but well above the crossover.
    fake_tpg.CHAMBER.pressure = 5.0
    wait_for_gauge(gauge_service, lambda p: p is not None and p > limit)
    rule = pump_service.interlocks()["motor_on"]
    check("turbopump start is held above the crossover pressure",
          rule["allowed"] is False and rule["pressure_mbar"] is not None,
          rule["reason"][:52])
    check("the refusal reports the pressure it is measuring",
          rule["limit_mbar"] == limit and rule["pressure_mbar"] > limit,
          f"{rule['pressure_mbar']:.2E} mbar vs {limit:.0E} mbar")
    check("no start-delay countdown remains on the turbopump",
          rule["wait"] == 0)

    # Roughed out: the same command, now allowed, with no waiting at all.
    fake_tpg.CHAMBER.pressure = limit / 2
    wait_for_gauge(gauge_service, lambda p: p is not None and p <= limit)
    check("turbopump start is allowed once the chamber is roughed out",
          pump_service.interlocks()["motor_on"]["allowed"] is True,
          f"{gauge_service.best_pressure():.2E} mbar")

    # A stale reading is not evidence: an old number must not authorise it.
    original = pumpmod.PRESSURE_MAX_AGE_S
    try:
        pumpmod.PRESSURE_MAX_AGE_S = -1.0
        rule = pump_service.interlocks()["motor_on"]
        check("a stale gauge reading does not authorise the start",
              rule["allowed"] is False and "old" in rule["reason"],
              rule["reason"][:52])
    finally:
        pumpmod.PRESSURE_MAX_AGE_S = original

    # The run-13 accident, 2026-09-02: starting the backing pump makes gas
    # rush through the turbopump and windmill the rotor to a few Hz. An
    # earlier version read "speed > 0" as "already running" and waved the
    # start through while the gauge was overrange -- and the rotor spun up to
    # 1444 Hz against atmosphere. A turning rotor is not evidence about the
    # foreline.
    fake_tpg.CHAMBER.pressure = 1013.0
    wait_for_gauge(gauge_service, lambda p: p is None)
    windmilling = dict(pump_service.snapshot())
    windmilling["status"] = {**windmilling.get("status", {}), "ActualSpd": 4}
    windmilling["setpoints"] = {**windmilling.get("setpoints", {}),
                                "PumpgStatn": True, "MotorPump": False}
    rule = pump_service.interlocks(windmilling)["motor_on"]
    check("a windmilling rotor does not authorise the start",
          rule["allowed"] is False, rule["reason"][-60:])
    check("the refusal names the windmilling for what it is",
          "gas flow" in rule["reason"])

    # The one genuine no-op: the drive's own motor enable is already set.
    already = dict(windmilling)
    already["setpoints"] = {**already["setpoints"], "MotorPump": True}
    check("re-sending the start to a running motor is a no-op, not a breach",
          pump_service.interlocks(already)["motor_on"]["allowed"] is True)

    # A recipe may not cross over above the interlock: it would stall asking
    # for a start that is refused every cycle.
    recipe = runs.BaseCalibration({"crossover_mbar": limit * 10})
    recipe.begin_vacuum(_QuietEngine())
    check("a recipe crossover above the limit is lowered to it",
          recipe.crossover == limit, f"{recipe.crossover:.2E} mbar")
    recipe = runs.BaseCalibration({"crossover_mbar": 1e-2})
    recipe.begin_vacuum(_QuietEngine())
    check("a deeper crossover is left alone", recipe.crossover == 1e-2)

    fake_tpg.CHAMBER.pressure = 1013.0
    pump_service.command("off", override=["stop_dwell"])


def check_phase_timeouts() -> None:
    """Every waiting phase gives up eventually, and pauses for the operator."""
    engine = _QuietEngine()
    recipe = runs.Deposition({"stage_timeout_min": 1.0 / 60,
                              "working_mbar": 5e-3, "argon_sccm": 20,
                              "gate_percent": 30})
    recipe.begin(engine)
    check("a stage timeout is configured", recipe.stage_timeout == 1.0)

    recipe.enter("rough")
    recipe.phase_started -= 5.0
    try:
        recipe.phase_rough(engine, {"p_best": 8.0, "t": 5.0})
        check("roughing gives up eventually", False)
    except runs.Abort as abort:
        check("roughing gives up eventually", True, abort.reason[:46])

    recipe.enter("backfill")
    recipe.phase_started -= 5.0
    try:
        recipe.phase_backfill(engine, {"p_best": 2.0e-2, "t": 5.0})
        check("the argon backfill gives up eventually", False)
    except runs.Abort as abort:
        check("the argon backfill gives up eventually", True,
              abort.reason[:46])

    # A guided run waits on a person, and that must not spend the phase's
    # patience: the clock stops while a prompt is outstanding.
    class Prompting(_QuietEngine):
        def prompt(self):
            return {"instruction": "open the gate valve"}

    prompting = Prompting()
    recipe.enter("rough")
    recipe.phase_age(prompting)                     # start the pause
    time.sleep(0.3)
    check("the phase clock pauses while the operator is prompted",
          recipe.phase_age(prompting) < 0.1,
          f"{recipe.phase_age(prompting):.3f} s counted of 0.3 s waited")
    check("the clock runs again once the prompt is answered",
          recipe.phase_age(engine) < 0.1 and recipe._paused >= 0.3,
          f"{recipe._paused:.2f} s not counted")


#: The rules a test waives, by name, to spin the simulated rotor up with the
#: simulated chamber at atmosphere.
SPIN_ANYWAY = ("pressure_unknown", "pressure_high")


def station_on(pump_service) -> None:
    """Start the station and wait until the pump's reading says it is on."""
    pump_service.command("on")
    wait_until(lambda: pump_service.snapshot().get("setpoints", {})
               .get("PumpgStatn") is True, 10, 0.1)


def check_full_vacuum() -> None:
    """The unlimited procedure: no deadline, and stopping is how it ends."""
    engine = _QuietEngine()
    recipe = runs.FullVacuum({"interval_s": 5})
    recipe.begin(engine)

    check("full vacuum has no deadline on reaching base pressure",
          recipe.base_timeout == 0)
    check("full vacuum still bounds roughing and spin-up",
          recipe.stage_timeout > 0, f"{recipe.stage_timeout / 60:.0f} min")
    check("base pressure leads to holding, not to done",
          recipe.after_base_pressure() == "holding")
    check("stopping a full vacuum run completes it rather than aborting it",
          recipe.stop_is_completion is True)
    check("every other procedure still treats a stop as an abort",
          all(not p.stop_is_completion for p in runs.CATALOGUE.values()
              if p.name != "full_vacuum"))

    # The holding phase must never end the run on its own, however long it
    # runs or whatever the pressure does.
    recipe.enter("holding")
    ended = [recipe.phase_holding(engine, {"t": t, "p_best": 5e-6})
             for t in (0.0, 60.0, 3600.0, 86400.0)]
    check("holding never ends by itself", all(n is None for n in ended))

    # Over-temperature is watched in every phase, from the drive's own flags.
    hot = _QuietEngine()
    hot.status = {"OvTempPump": True}
    try:
        recipe.guard(hot, {"t": 10.0, "p_best": 5e-6, "phase": "holding"})
        check("an over-temperature flag aborts the run", False)
    except runs.Abort as abort:
        check("an over-temperature flag aborts the run", True,
              abort.reason[:44])

    warm = _QuietEngine()
    try:
        recipe.guard(warm, {"t": 10.0, "p_best": 5e-6, "tms_temp": 95.0})
        check("a temperature above the run's limit aborts it", False)
    except runs.Abort as abort:
        check("a temperature above the run's limit aborts it", True,
              abort.reason[:44])


def check_guided_shutdown(engine, devices) -> None:
    """The power-down must not stop the station under a spinning rotor.

    The order is the whole point: motor off, wait for the rotor to reach
    rest with the backing pump still running, let the dwell pass, and only
    then stop the station. The dwell is shortened here so the test costs
    seconds rather than half a minute.
    """
    pump_service = devices.get("pump")
    dwell = pumpmod.STOP_DWELL_SECONDS
    poll = runs.RunEngine.SHUTDOWN_POLL_S
    try:
        pumpmod.STOP_DWELL_SECONDS = 1.0
        runs.RunEngine.SHUTDOWN_POLL_S = 0.3

        # Get the rig running: station on, rotor up to speed.
        station_on(pump_service)
        pump_service.command("motor_on", override=SPIN_ANYWAY)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            speed = pump_service.snapshot().get("status", {}).get("ActualSpd") or 0
            if speed > 50:
                break
            time.sleep(0.3)
        spinning = pump_service.snapshot().get("status", {}).get("ActualSpd") or 0
        check("rotor is spinning before the power-down", spinning > 50,
              f"{spinning} Hz")
        check("the station may not stop while the rotor turns",
              pump_service.interlocks()["off"]["allowed"] is False)

        started = engine.shutdown_pump("selftest")
        check("power-down starts", started.get("ok") is True)
        check("a second power-down is refused while one runs",
              engine.shutdown_pump("again").get("ok") is False)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            state = engine.shutdown_status() or {}
            if not state.get("active"):
                break
            time.sleep(0.3)

        state = engine.shutdown_status() or {}
        check("power-down finishes", state.get("active") is False,
              f"{state.get('phase')}: {state.get('message')}")
        check("power-down reports success", state.get("ok") is True)

        # The pump service publishes a cached snapshot every couple of
        # seconds, so give it a cycle to catch up with the last command
        # rather than reading the state it held while the command was sent.
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            snap = pump_service.snapshot()
            if snap.get("setpoints", {}).get("PumpgStatn") is False:
                break
            time.sleep(0.3)
        check("rotor is at rest afterwards",
              (snap.get("status", {}).get("ActualSpd") or 0) == 0)
        check("the station is off afterwards",
              snap.get("setpoints", {}).get("PumpgStatn") is False,
              str(snap.get("setpoints", {}).get("PumpgStatn")))
    finally:
        pumpmod.STOP_DWELL_SECONDS = dwell
        runs.RunEngine.SHUTDOWN_POLL_S = poll


def check_fault_handling(engine, devices, database) -> None:
    """After a fault: motor off, fault cleared, forevacuum pump kept on.

    The rig's real problem on 2026-10-08 was not only the Err006 itself but
    what came after: a latched fault, and an automatic power-down that
    switched the station off -- which stops the forevacuum pump and, with the
    drive's factory venting setting, vents the chamber.
    """
    from lab import alarms

    pump_service = devices.get("pump")
    state = fake_tc600.STATE

    check("Err006 is described as the run-up time error",
          pumpmod.describe_error("Err006").get("title") == "Run-up time error")
    check("no fault describes itself as none",
          pumpmod.describe_error("000000").get("code") is None)

    # --- the safe-state sequence itself ------------------------------------
    station_on(pump_service)
    pump_service.command("motor_on", override=SPIN_ANYWAY)
    wait_until(lambda: (pump_service.snapshot().get("status", {})
                        .get("ActualSpd") or 0) > 30)
    state[303] = "Err006"                      # the drive faults
    wait_until(lambda: pump_service.snapshot().get("fault", {}).get("code"))
    check("the snapshot says what the fault means",
          pump_service.snapshot().get("fault", {}).get("title")
          == "Run-up time error")
    result = pump_service.secure("selftest fault")
    check("making safe switches the turbopump motor off",
          state[23] is False, str(result.get("actions")))
    check("making safe clears the fault", str(state[303]) == "000000")
    check("making safe keeps the forevacuum pump (station) on",
          state[10] is True)
    check("making safe confirms what it did", result.get("ok") is True,
          str(result.get("error")))

    # --- the supervisor does it by itself ------------------------------------
    supervisor = alarms.Supervisor(devices, engine, database, auto_recover=True)
    pump_service.command("motor_on", override=SPIN_ANYWAY)
    wait_until(lambda: (pump_service.snapshot().get("setpoints", {})
                        .get("MotorPump")) is True)
    watching(supervisor, 7.0)                  # learn the healthy state
    state[303] = "Err006"
    # A fault is believed on two readings in a row, not one.
    wait_until(lambda: (supervisor.check() or True) and any(
        a["key"] == "pump_fault" for a in supervisor.describe()["alarms"]), 15, 0.1)
    raised = [a for a in supervisor.describe()["alarms"] if a["key"] == "pump_fault"]
    check("a drive fault raises a critical alarm",
          bool(raised) and raised[0]["level"] == "critical",
          raised[0]["title"] if raised else "none")
    recovered = wait_until(lambda: str(state[303]) == "000000"
                           and state[23] is False and state[10] is True)
    check("the supervisor makes the pump safe on its own", recovered,
          f"motor {state[23]}, station {state[10]}, fault {state[303]}")
    wait_until(lambda: (supervisor.recovering or {}).get("active") is False)
    check("the recovery says what it did",
          bool((supervisor.recovering or {}).get("actions")))
    wait_until(lambda: not pump_service.snapshot().get("fault", {}).get("code"))
    wait_until(lambda: (supervisor.check() or True) and not any(
        a["key"] == "pump_fault" and a["active"]
        for a in supervisor.describe()["recent"]), 15, 0.1)
    alarm = [a for a in supervisor.describe()["recent"] if a["key"] == "pump_fault"][0]
    check("the alarm stays until acknowledged, though the fault has cleared",
          alarm["active"] is False and alarm["acknowledged"] is False)
    supervisor.acknowledge("all")
    check("acknowledging clears the alarm bar",
          not supervisor.describe()["alarms"])

    # --- no automatic path switches the station off ---------------------------
    class Watching(_QuietEngine):
        def __init__(self):
            self.powered_down = False

        def shutdown_pump(self, reason: str = "") -> dict:
            self.powered_down = True
            return {"ok": True}

    watcher = Watching()
    recipe = runs.FullVacuum({})
    recipe.begin(watcher)
    recipe.finish(watcher)
    check("a full-vacuum run ending on its own does not power the pump down",
          watcher.powered_down is False)

    # --- a pump-trouble abort makes the pump safe -----------------------------
    class Overheating(runs.Procedure):
        name = "selftest_overheat"
        phases = ("running", "done")
        interval = 0.1

        def step(self, engine, sample):
            raise runs.Abort("selftest over temperature", pump_trouble=True)

    pump_service.command("motor_on", override=SPIN_ANYWAY)
    wait_until(lambda: pump_service.snapshot().get("setpoints", {})
               .get("MotorPump") is True)
    runs.CATALOGUE[Overheating.name] = Overheating
    try:
        started = engine.start(Overheating.name, {})
        check("the pump-trouble test run starts", started.get("ok") is True,
              str(started.get("error")))
        wait_until(lambda: not engine.status()["running"])
        wait_until(lambda: (engine.status().get("secure") or {}).get("active")
                   is False)
        secured = engine.status().get("secure") or {}
        check("a pump-trouble abort makes the pump safe afterwards",
              secured.get("ok") is True and state[23] is False
              and state[10] is True,
              f"motor {state[23]}, station {state[10]}")
        last = engine.status().get("last") or {}
        check("the ended run is kept for the panel to show",
              last.get("status") == "aborted" and last.get("pump_trouble") is True)
    finally:
        del runs.CATALOGUE[Overheating.name]

    # --- no new run while a power-down is still bringing the rotor to rest ----
    blocker = threading.Thread(target=time.sleep, args=(1.0,), daemon=True)
    blocker.start()
    engine._shutdown_thread = blocker
    refused = engine.start("hold", {"duration_min": 1})
    check("no new run starts while a power-down is still running",
          refused.get("ok") is False and "powered down" in refused.get("error", ""),
          refused.get("error", "")[:50])
    blocker.join()

    # --- the drive's protective settings, on demand ----------------------------
    found = pump_service.diagnostics()
    check("diagnostics read the run-up time limit",
          found.get("values", {}).get("RUTimeSVal") == 15)
    check("diagnostics read the speed switchpoint",
          found.get("values", {}).get("SpdSwPt") == 80)

    # Leave the simulated pump at rest for the tests that follow.
    pump_service.command("motor_off")


def test_run_engine(pump_port: str, gauge_port: str) -> None:
    print("\nRun engine, both instruments attached")
    with tempfile.TemporaryDirectory() as directory:
        database = dbmod.Database(os.path.join(directory, "run.db"))
        devices = registry.Registry()
        devices.add(pumpmod.PumpService("pump", pump_port, address=1))
        devices.add(gaugemod.GaugeService("gauge", gauge_port, baudrate=9600))
        engine = runs.RunEngine(database, devices, roles=runs.ALL_ROLES)
        try:
            # Give both samplers a cycle to make contact.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if all(d.snapshot().get("connected") for d in devices):
                    break
                time.sleep(0.3)
            check("pump service connects",
                  devices.get("pump").snapshot()["connected"])
            check("gauge service connects",
                  devices.get("gauge").snapshot()["connected"])

            row = devices.sample_row()
            check("registry merges both devices into one row",
                  "speed" in row and "p1" in row, sorted(row))

            check_crossover_interlock(devices)
            check_phase_timeouts()
            check_full_vacuum()
            check_guided_shutdown(engine, devices)
            check_fault_handling(engine, devices, database)

            check("unknown procedure refused",
                  engine.start("nonsense", {})["ok"] is False)

            # A short hold run: no interlock waiting, but every moving part of
            # the engine gets exercised -- sampling, storage, summarising.
            started = engine.start("hold", {"duration_min": 0.1, "interval_s": 1},
                                   name="Selftest hold", operator="selftest")
            check("run starts", started["ok"] is True)
            check("second run refused while one is running",
                  engine.start("hold", {})["ok"] is False)

            time.sleep(2)
            live = engine.status()
            check("engine reports the run as live", live["running"] is True)
            check("live samples accumulate", live["samples"] >= 1,
                  f"{live['samples']} samples")

            deadline = time.monotonic() + 30
            while engine.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.5)
            check("run finishes on its own",
                  engine.status()["running"] is False)

            record = database.runs(limit=1)[0]
            check("run recorded as completed", record["status"] == "completed",
                  record["status"])
            check("result carries pressure statistics",
                  "pressure" in (record["result"] or {}))
            check("samples persisted",
                  database.sample_count(record["id"]) >= 3,
                  f"{database.sample_count(record['id'])} samples")
            check("run has a log", len(database.events(record["id"])) >= 1)

            name, text = export.index_csv(database)
            check("index CSV lists the run", "Selftest hold" in text)
        finally:
            devices.close()
            database.close()
