#!/usr/bin/env python3
"""End-to-end test of the whole stack against the simulators, no hardware needed.

Runs the fake pump and the fake gauge in background threads on pseudo-terminals
and drives the real client code against them: `./tools/selftest.py`

Five sections, in dependency order: the TC 600 driver, the TPG driver, the
analysis maths, the SQLite store, and a whole recorded run through the engine
with both instruments attached.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import traceback
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tpg  # noqa: E402
from tc600 import TC600, protocol, scan  # noqa: E402
from lab import db as dbmod  # noqa: E402
from lab import gauge as gaugemod  # noqa: E402
from lab import pump as pumpmod  # noqa: E402
from lab import export, registry, runs  # noqa: E402
from tools import fake_tc600, fake_tpg  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
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


# --- sections -------------------------------------------------------------


def test_pump(port: str) -> None:
    print("TC 600 driver")
    with TC600(port, address=1, timeout=1.0) as pump:
        identity = pump.identify()
        check("identify() returns fields", bool(identity), str(identity))
        check("ElecName is TC600", identity.get("ElecName") == "TC600")
        check("NominalSpd is 1000 Hz", identity.get("NominalSpd") == 1000)
        check("DrvVoltage decodes as u_real",
              pump.read("DrvVoltage") == 48.0, f"{pump.read('DrvVoltage')} V")
        check("lookup by number works", pump.read(311) == pump.read("OpHrsPump"))

        status = pump.status()
        check("status() populated", len(status) >= 10, f"{len(status)} fields")

        try:
            pump.read_raw(326)  # TempElec: NO_DEF on real firmware 017500
            check("NO_DEF raises DeviceError", False)
        except protocol.DeviceError as exc:
            check("NO_DEF raises DeviceError", True, str(exc)[:40])

        try:
            pump.write_raw(309, "000100")  # ActualSpd is read-only
            check("_LOGIC raises DeviceError", False)
        except protocol.DeviceError as exc:
            check("_LOGIC raises DeviceError", True, str(exc)[:40])

        try:
            pump.write("ActualSpd", 100)
            check("client blocks read-only writes", False)
        except ValueError:
            check("client blocks read-only writes", True)

        check("PumpgStatn write echoes True", pump.pumping_station(True) is True)
        check("MotorPump write echoes True", pump.motor(True) is True)
        time.sleep(1.5)
        speed = pump.read("ActualSpd")
        check("rotor spins up", speed > 0, f"{speed} Hz")

        pump.set_speed_percent(50.0)
        check("OpModeTMP set to speed-setting", pump.read("OpModeTMP") == 1)
        check("SpdSVal is 50 %", pump.read("SpdSVal") == 50.0)

        before = pump.read("ActualSpd")
        pump.pumping_station(False)
        pump.motor(False)
        time.sleep(0.8)
        check("rotor slows down", pump.read("ActualSpd") < before)

    found = scan(port, range(1, 4), timeout=0.4)
    check("scan finds address 1", found == [1], str(found))


def test_gauge(port: str) -> None:
    print("\nTPG driver")
    with tpg.TPG(port, baudrate=9600, timeout=1.0) as unit:
        check("firmware reads back", unit.firmware() == "BG509727-C",
              unit.firmware())

        gauges = unit.gauge_types()
        check("two gauges identified", len(gauges) == 2,
              ", ".join(g["identifier"] for g in gauges))
        check("gauge 1 recognised as Pirani", "Pirani" in gauges[0]["description"])

        code, name = unit.unit()
        check("unit reads as mbar", code == 0 and name == "mbar")

        readings = unit.pressures()
        check("PRX returns both channels", len(readings) == 2)
        check("status decodes to a name",
              all(r["status_name"] for r in readings),
              ", ".join(r["status_name"] for r in readings))
        # The chamber starts at atmosphere, so the cold cathode is off and the
        # Pirani is over its range: both must report invalid, not a number.
        check("invalid readings carry no mbar value",
              all(r["mbar"] is None for r in readings if not r["valid"]))

        errors = unit.error_status()
        check("error register is clear", errors["ok"], errors["word"])

        # The real TPG 252 A answers "lower,upper" with no channel field, and
        # assuming otherwise made int("1.00E-11") raise and the whole
        # switching-function panel vanish. Both shapes must parse.
        first = unit.setpoint(1)
        check("setpoint reads back", first["setpoint"] == 1)
        check("channel-less setpoint parses", first["channel"] is None,
              f"lower {first['lower']:.2E} upper {first['upper']:.2E}")
        check("setpoint count matches the model", unit.setpoint_count() == 2,
              f"{unit.setpoint_count()} switching functions")

        updated = unit.set_setpoint(2, 1.0e-4, 5.0e-4)
        check("setpoint writes back", abs(updated["lower"] - 1.0e-4) < 1e-9,
              f"{updated['lower']:.2E}")

        try:
            unit.set_setpoint(1, 5.0e-4, 1.0e-4)   # lower above upper
            check("setpoint rejects inverted thresholds", False)
        except ValueError:
            check("setpoint rejects inverted thresholds", True)

        # SEN reports 3, not 2, for a live gauge on the real instrument.
        sensors = unit.sensor_state()
        check("live gauge reads as on", sensors[0]["on"] is True,
              f"code {sensors[0]['code']} -> {sensors[0]['state']}")
        check("empty channel identifier recognised",
              not tpg.commands.gauge_present("noSe")
              and tpg.commands.gauge_present("CO9"))

        try:
            unit.exchange("SCX")     # in UNIMPLEMENTED on the fake firmware
            check("unimplemented mnemonic raises NakError", False)
        except tpg.NakError:
            check("unimplemented mnemonic raises NakError", True)

        # An exchange abandoned after its ACK must not desynchronise the next
        # one -- that is what the ETX at the start of every exchange is for.
        unit.serial.write(b"PNR\r\n")
        unit.serial.flush()
        time.sleep(0.1)
        check("resynchronises after an abandoned exchange",
              unit.firmware() == "BG509727-C")

        result = unit.probe()
        check("probe finds the implemented set", len(result["supported"]) > 15,
              f"{len(result['supported'])} implemented, "
              f"{len(result['refused'])} refused")
        check("probe reports refusals", len(result["refused"]) > 0)

    detected = tpg.autodetect(port, timeout=0.8)
    check("autodetect finds the baud rate", detected is not None
          and detected.baudrate == 9600)
    if detected:
        detected.close()


def test_analysis() -> None:
    print("\nAnalysis")
    # A perfect straight line: slope must come back exactly, R² must be 1.
    points = [(t, 1.0e-6 + 2.0e-8 * t) for t in range(0, 100, 5)]
    fit = runs.linear_fit(points)
    check("linear_fit recovers the slope",
          abs(fit["slope"] - 2.0e-8) < 1e-14, f"{fit['slope']:.3E}")
    check("linear_fit reports R2 = 1", abs(fit["r_squared"] - 1.0) < 1e-9)
    check("linear_fit refuses too few points", runs.linear_fit([(0, 1)]) is None)

    # Fractional rate: the base-pressure detector's whole basis. An
    # exponential decay with time constant tau has a constant relative rate
    # of -100/tau percent per second, whatever the absolute pressure.
    decay = [(t, 1.0e-6 * math.exp(-t / 400.0)) for t in range(0, 300, 10)]
    rate = runs.relative_rate(decay)
    check("relative_rate reads a decade-independent rate",
          abs(rate - (-60.0 * 100.0 / 400.0)) < 1e-6, f"{rate:.3f} %/min")
    deep = [(t, 1.0e-9 * math.exp(-t / 400.0)) for t in range(0, 300, 10)]
    check("the same shape three decades lower gives the same rate",
          abs(runs.relative_rate(deep) - rate) < 1e-6)
    check("a flat curve reads as zero rate",
          abs(runs.relative_rate([(t, 1e-6) for t in range(0, 300, 10)])) < 1e-9)

    stats = runs.statistics([3.0, 1.0, 2.0])
    check("statistics min/max/mean",
          stats["min"] == 1.0 and stats["max"] == 3.0 and stats["mean"] == 2.0)
    check("statistics keeps order for first/last",
          stats["first"] == 3.0 and stats["last"] == 2.0)


def test_process() -> None:
    print("\nProcess hardware")
    from lab import process

    hardware = process.ProcessHardware()
    check("nothing is wired by default", not hardware.automatic_ready,
          f"missing: {len(hardware.missing())}")
    check("every actuator reports itself manual",
          all(not a.available for a in hardware))

    try:
        hardware.shutter.set(True)
        check("an unwired actuator refuses to act", False)
    except process.OperatorRequired as prompt:
        check("an unwired actuator refuses to act", True,
              prompt.instruction[:44])

    check("the request is recorded even when nobody carried it out",
          hardware.shutter.requested is True)
    check("instructions name the direction",
          "OPEN" in hardware.shutter.instruction(True)
          and "CLOSE" in hardware.shutter.instruction(False))

    # Attaching a driver is the whole upgrade path from manual to automatic.
    from tools import fake_actuators, fake_tpg

    chamber = fake_tpg.Chamber()
    fake_actuators.attach(hardware, chamber)
    check("attaching drivers makes it automatic-ready",
          hardware.automatic_ready and not hardware.missing())
    hardware.gas.set(15)
    time.sleep(1.0)          # the simulated MFC ramps rather than stepping
    hardware.gas.read()
    check("a driven actuator moves the chamber", chamber.gas_sccm > 0,
          f"{chamber.gas_sccm:.1f} sccm on the way to 15")

    # The magnetron must refuse to strike where no discharge could exist.
    chamber.pressure = 1.0e-8
    try:
        hardware.magnetron.set(100)
        check("magnetron refuses to strike at base pressure", False)
    except process.ActuatorError as exc:
        check("magnetron refuses to strike at base pressure", True,
              str(exc)[:44])

    chamber.pressure = 5.0e-3
    hardware.magnetron.set(100)
    check("magnetron strikes in the working window",
          hardware.magnetron.read() == 100)

    hardware.safe_shutdown()
    check("safe shutdown kills the plasma, the gas and the shutter",
          hardware.magnetron.read() == 0
          and hardware.gas.requested == 0
          and hardware.shutter.read() is False)


def test_deposition_engine() -> None:
    print("\nDeposition engine")
    from lab import process, registry as reg

    with tempfile.TemporaryDirectory() as directory:
        database = dbmod.Database(os.path.join(directory, "dep.db"))
        hardware = process.ProcessHardware()
        engine = runs.RunEngine(database, reg.Registry(), hardware=hardware)
        engine._run = database.create_run("deposition", "prompt test", {})

        # --- guided mode: a manual step raises a prompt, not an abort ------
        engine.mode = runs.GUIDED
        check("guided require() returns False on a manual step",
              engine.require(hardware.shutter, False) is False)
        check("a prompt is raised", engine.prompt() is not None,
              (engine.prompt() or {}).get("instruction", "")[:40])
        check("the same step does not re-prompt",
              engine.require(hardware.shutter, False) is False
              and engine.prompt() is not None)

        engine.acknowledge()
        check("acknowledging clears the prompt", engine.prompt() is None)
        check("the step counts as done once acknowledged",
              engine.require(hardware.shutter, False) is True)

        # The bug the first simulated deposition found: after shutter False,
        # then True, asking for False again must act rather than be treated
        # as already satisfied -- otherwise the shutter is left open over the
        # sample for the whole recovery.
        engine.require(hardware.shutter, True)
        engine.acknowledge()
        check("re-requesting an earlier value asks again",
              engine.require(hardware.shutter, False) is False,
              "shutter close after open is not skipped")

        # --- automatic mode: refuses rather than waits ---------------------
        engine.mode = runs.AUTOMATIC
        engine._satisfied = {}
        try:
            engine.require(hardware.gas, 20)
            check("automatic mode refuses an unwired actuator", False)
        except runs.Abort as abort:
            check("automatic mode refuses an unwired actuator", True,
                  abort.reason[:44])

        check("automatic start is refused while anything is manual",
              engine.start("deposition", {"mode": "auto"})["ok"] is False)

        # --- the ceiling must not fire while roughing ----------------------
        recipe = runs.Deposition({"max_inlet_mbar": 5e-2})
        recipe.pressure_ceiling = 5e-2
        recipe.phase = "rough"
        check("no pressure ceiling while roughing at atmosphere",
              recipe.current_pressure_ceiling({"speed": 0}) is None)
        recipe.phase = "deposit"
        check("the ceiling applies once the rotor is turning",
              recipe.current_pressure_ceiling({"speed": 1000}) == 5e-2)

        database.close()


def test_database() -> None:
    print("\nDatabase")
    with tempfile.TemporaryDirectory() as directory:
        database = dbmod.Database(os.path.join(directory, "test.db"))

        run = database.create_run("pumpdown", "Test run", {"target_mbar": 1e-5},
                                  operator="selftest")
        check("run created with an id", run["id"] > 0)
        check("config round-trips as JSON",
              run["config"]["target_mbar"] == 1e-5)
        check("new run is active", database.active_run()["id"] == run["id"])

        for index in range(10):
            database.add_sample(run["id"], {
                "t": index * 2.0, "at": time.time(), "phase": "pumping",
                "p1": 1.0e-3 / (index + 1), "p1_status": 0,
                "speed": index * 100, "station": 1, "motor": 1,
            })
        check("samples stored", database.sample_count(run["id"]) == 10)
        check("samples come back in order",
              [s["t"] for s in database.samples(run["id"])] ==
              [i * 2.0 for i in range(10)])
        check("decimation thins the curve",
              len(database.samples(run["id"], every=3)) == 4,
              f"{len(database.samples(run['id'], every=3))} of 10")

        database.add_event(run["id"], "info", "phase change")
        check("events stored", len(database.events(run["id"])) == 1)

        database.finish_run(run["id"], dbmod.COMPLETED, {"ultimate_mbar": 1e-4})
        finished = database.run(run["id"])
        check("run finishes as completed", finished["status"] == "completed")
        check("result round-trips", finished["result"]["ultimate_mbar"] == 1e-4)
        check("duration computed", finished["duration"] is not None)
        check("no active run once finished", database.active_run() is None)

        check("search matches the operator",
              len(database.runs(search="selftest")) == 1)
        check("filter excludes other procedures",
              len(database.runs(procedure="bakeout")) == 0)

        name, text = export.run_csv(database, run["id"])
        check("CSV export names itself usefully",
              name.endswith(".csv") and "pumpdown" in name, name)
        rows = [ln for ln in text.splitlines()
                if ln and not ln.startswith("#")]
        check("CSV carries a row per sample", len(rows) == 11,   # header + 10
              f"{len(rows)} data rows, {len(text.splitlines())} lines total")

        name, payload = export.run_json(database, run["id"])
        check("JSON export includes samples and events",
              '"samples"' in payload and '"events"' in payload)

        # Orphan recovery: a run left open by a crash must be closed out.
        database.create_run("hold", "Crashed run", {})
        check("orphan detected", len(database.orphaned_runs()) == 1)
        # Constructing the engine is the whole test: closing out runs left
        # open by a dead process is a startup side effect.
        runs.RunEngine(database, registry.Registry())
        check("orphan closed out on startup",
              len(database.orphaned_runs()) == 0)
        check("orphan marked failed",
              database.runs(status="failed")[0]["name"] == "Crashed run")

        snapshot = os.path.join(directory, "copy.db")
        database.snapshot(snapshot)
        check("snapshot is a usable database",
              dbmod.Database(snapshot).stats()["runs"] == 2)

        check("delete removes the run", database.delete_run(run["id"]))
        check("samples cascade with the run",
              database.sample_count(run["id"]) == 0)
        database.close()


def test_run_engine(pump_port: str, gauge_port: str) -> None:
    print("\nRun engine, both instruments attached")
    with tempfile.TemporaryDirectory() as directory:
        database = dbmod.Database(os.path.join(directory, "run.db"))
        devices = registry.Registry()
        devices.add(pumpmod.PumpService("pump", pump_port, address=1))
        devices.add(gaugemod.GaugeService("gauge", gauge_port, baudrate=9600))
        engine = runs.RunEngine(database, devices)
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

            interlocks = devices.get("pump").interlocks()
            check("turbopump start is held without backing",
                  interlocks["motor_on"]["allowed"] is False,
                  interlocks["motor_on"]["reason"][:44])
            check("starting the station is always allowed",
                  interlocks["on"]["allowed"] is True)

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


def main() -> int:
    pump_master, pump_slave = os.openpty()
    gauge_master, gauge_slave = os.openpty()
    pump_port, gauge_port = os.ttyname(pump_slave), os.ttyname(gauge_slave)
    stop = threading.Event()

    threads = [
        spawn(fake_tc600.serve, pump_master, stop, "pump"),
        spawn(fake_tpg.serve, gauge_master, stop, "gauge"),
    ]
    time.sleep(0.3)
    print(f"Simulated TC 600 on {pump_port}")
    print(f"Simulated TPG    on {gauge_port}\n")

    try:
        test_pump(pump_port)
        test_gauge(gauge_port)
        test_analysis()
        test_process()
        test_deposition_engine()
        test_database()
        test_run_engine(pump_port, gauge_port)
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        os.close(pump_slave)
        os.close(gauge_slave)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
