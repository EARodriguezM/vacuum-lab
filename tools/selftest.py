#!/usr/bin/env python3
"""End-to-end test of the whole stack against the simulators, no hardware needed.

Runs the fake pump and the fake gauge in background threads on pseudo-terminals
and drives the real client code against them: `./tools/selftest.py`

Five sections, in dependency order: the TC 600 driver, the TPG driver, the
analysis maths, the SQLite store, and a whole recorded run through the engine
with both instruments attached.
"""

from __future__ import annotations

import ast
import math
import os
import sys
import tempfile
import threading
import traceback
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

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
        check("NominalSpd is 1500 Hz", identity.get("NominalSpd") == 1500)
        check("DrvVoltage decodes as u_real",
              pump.read("DrvVoltage") == 23.25, f"{pump.read('DrvVoltage')} V")
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


def test_cold_cathode_interlock() -> None:
    """The gauge on the rig answers "CO9", not "PKR": the rule must still bite."""
    print("\nCold-cathode interlock")
    service = gaugemod.GaugeService("gauge", "/dev/null-gauge")

    def snap(mbar):
        return {"readings": [{"channel": 1, "valid": True, "mbar": mbar}]}

    def refused(identifier, mbar):
        service._identity = {"gauges": [{"channel": 1, "identifier": identifier}]}
        try:
            service._check_sensor_on(1, snap(mbar))
        except gaugemod.GaugeInterlock:
            return True
        return False

    check("the rig's CO9 (a PKR 251) is not struck at 4e2 mbar",
          refused("CO9", 412.0))
    check("nor just above the 1e-2 mbar limit", refused("CO9", 2.0e-2))
    check("it may be switched on below the limit", not refused("CO9", 1.0e-3))
    check("the published names still count", refused("PKR", 412.0)
          and refused("IKR9", 1.0))
    check("a Pirani is never held back", not refused("TPR", 412.0))
    check("CO9 is described as what it is",
          "PKR 251" in tpg.commands.describe_gauge("CO9"))


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


def field_default_mismatches() -> dict[str, list[str]]:
    """Every published field default against the fallback the code uses.

    Done by *running* each procedure's `begin()` against a stub engine with
    an empty config and recording what `number()` and `flag()` were asked
    for. An earlier version read the syntax tree of each procedure class,
    which silently skipped every fallback living on a base class -- and
    `begin_vacuum`, shared by four procedures, is where the mismatch was.

    The fallback only shows itself when the key is *missing* from the
    config, which is the one case a form-driven run never produces and an
    API caller or an old stored config produces easily.
    """
    out: dict[str, list[str]] = {}

    for procedure in runs.CATALOGUE.values():
        published = {f["key"]: f.get("default") for f in procedure.fields}
        seen: dict[str, object] = {}

        class Recording(procedure):                  # noqa: B903
            def number(self, key, default=0.0):
                seen[key] = default
                return super().number(key, default)

            def flag(self, key, default=False):
                seen[key] = default
                return super().flag(key, default)

        recipe = Recording({})
        try:
            recipe.begin(_QuietEngine())
        except Exception:
            pass          # only the reads matter, not whether it could start

        out[procedure.name] = [
            f"{key}: form says {published[key]!r}, code falls back to "
            f"{fallback!r}"
            for key, fallback in sorted(seen.items())
            if key in published and published[key] != fallback
        ]
    return out


def test_equipment() -> None:
    """A procedure whose hardware is not on the rig is switched off."""
    print("\nRig equipment")
    from lab import process, registry as reg

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "equipment.json")
        hardware = process.ProcessHardware()
        equipment = process.Equipment(path, default=False, hardware=hardware)
        check("nothing counts as installed until somebody says so",
              equipment.missing(["gas", "heater"]) ==
              [process.EQUIPMENT["gas"], process.EQUIPMENT["heater"]])

        database = dbmod.Database(os.path.join(directory, "eq.db"))
        engine = runs.RunEngine(database, reg.Registry(), hardware=hardware,
                                equipment=equipment)
        refused = engine.start("deposition", {})
        check("a deposition is refused while its hardware is not installed",
              refused["ok"] is False and "not installed" in refused["error"]
              and "gate valve" in refused["error"], refused.get("error", "")[:60])
        check("so are the leak test and the bake-out",
              engine.start("rate_of_rise", {})["ok"] is False
              and engine.start("bakeout", {})["ok"] is False)
        check("a refused procedure creates no run", database.runs(limit=5) == [])

        listed = {entry["name"]: entry for entry in engine.catalogue()}
        check("the catalogue marks what cannot run, and why",
              listed["deposition"]["available"] is False
              and len(listed["deposition"]["missing"]) == 4
              and listed["full_vacuum"]["available"] is True)
        check("a pump-only procedure may run unattended with manual actuators",
              listed["full_vacuum"]["automatic_ready"] is True
              and listed["base_calibration"]["automatic_ready"] is True)
        check("the panel's state lists only the procedures that cannot run",
              sorted(engine.unavailable()) == ["bakeout", "deposition",
                                               "rate_of_rise"])

        result = engine.set_equipment("gate", True)
        check("marking the gate valve installed enables the leak test",
              result["ok"] and "rate_of_rise" not in engine.unavailable())
        check("the change is in the rig's log",
              any("Gate valve marked installed" in e["message"]
                  for e in database.events(limit=5)))
        check("and survives a restart",
              process.Equipment(path).installed("gate") is True
              and process.Equipment(path).installed("magnetron") is False)

        from tools import fake_actuators, fake_tpg
        fake_actuators.attach(hardware, fake_tpg.Chamber())
        check("a driven actuator is installed by definition",
              equipment.installed("magnetron")
              and equipment.set("magnetron", False)["ok"] is False)
        database.close()


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

        # --- published defaults must match the code's own fallbacks --------
        # A form default of 100 % with a fallback of 30 % is how a deposition
        # came to throttle a chamber that the operator was shown as open, and
        # then sit in backfill for ever because the flow could not reach the
        # working pressure through it.
        for name, disagreements in field_default_mismatches().items():
            check(f"{name}: published defaults match the code's fallbacks",
                  not disagreements, "; ".join(disagreements))

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
                "p_best": 1.0e-3 / (index + 1),
                "speed": index * 100, "station": 1, "motor": 1,
            })
        check("samples stored", database.sample_count(run["id"]) == 10)

        # p_best is the pressure the engine actually judged on -- which
        # channel was trustworthy is decided at sampling time and cannot be
        # reconstructed by a reader. It was computed but never stored until
        # 2026-09-02, so every finished run read back with no pressure at all.
        stored = database.samples(run["id"])
        check("p_best is stored, not just computed",
              stored[0].get("p_best") == 1.0e-3,
              f"{stored[0].get('p_best')}")
        check("p_best survives decimation",
              all(s.get("p_best") is not None
                  for s in database.samples(run["id"], every=3)))
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
        check("CSV header names p_best", "p_best_mbar" in text)
        check("CSV header and rows have the same width",
              len(text.strip().splitlines()[-1].split(",")) ==
              len(export.SAMPLE_HEADER),
              f"{len(text.strip().splitlines()[-1].split(','))} vs "
              f"{len(export.SAMPLE_HEADER)}")
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


def test_history_and_charts() -> None:
    """The chart recorder, the decimation behind every chart, and the data
    the new charts depend on."""
    print("\nHistory, decimation and chart data")
    from lab import history, process, trends

    # --- decimation must keep the spike that tripped the pump --------------
    # Run 25 (2026-10-08): two samples at 5.9e-2 and 1.6e-1 mbar in an hour
    # of 1e-4. A stride of every Nth row steps straight over them.
    count = 10_000
    pressures = [1.0e-4] * count
    pressures[5000], pressures[5001] = 5.9e-2, 1.6e-1
    speeds = [1500.0] * count
    speeds[5002] = 1171.0
    keep = history.decimate(count, 300, [pressures, speeds])
    check("decimation keeps a two-sample pressure spike",
          5000 in keep and 5001 in keep)
    check("decimation keeps a one-sample dip in rotor speed", 5002 in keep)
    check("decimation keeps the first and the newest row",
          keep[0] == 0 and keep[-1] == count - 1)
    check("decimation actually thins the data", len(keep) < 1000,
          f"{len(keep)} of {count}")
    check("decimated indices stay in order", keep == sorted(set(keep)))
    check("short series pass through untouched",
          history.decimate(5, 300, [[1, 2, 3, 4, 5]]) == [0, 1, 2, 3, 4])

    # --- the recorder -------------------------------------------------------
    class Gauge:
        def best_pressure(self):
            return 2.0e-5

    class Devices:
        def __init__(self):
            self.n = 0

        def sample_row(self):
            self.n += 1
            return {"p1": 2.0e-5, "p1_status": 0, "speed": 1500.0 + self.n,
                    "power": 12.0, "current": 0.52}

        def __contains__(self, key):
            return key == "gauge"

        def get(self, key):
            return Gauge()

    recorder = history.History(Devices(), process.ProcessHardware(),
                               interval=2.0, span_s=20.0)
    base = 1_000_000.0
    for step in range(30):
        recorder.record(now=base + step * 2.0)
    check("the recorder is bounded by its span", len(recorder) == 11,
          f"{len(recorder)} rows kept of 30 recorded")
    window = recorder.window(seconds=None, points=1500)
    fields = window["fields"]
    check("history rows carry the run-sample columns",
          {"p_best", "speed", "power", "gas_sccm", "shutter"} <= set(fields))
    row = dict(zip(fields, window["rows"][-1]))
    check("history rows carry the best pressure", row["p_best"] == 2.0e-5)
    newer = recorder.window(since=base + 54.0)
    check("asking with `since` returns only newer rows",
          [r[0] for r in newer["rows"]] == [base + 56.0, base + 58.0])

    # --- process hardware as sample columns ---------------------------------
    hardware = process.ProcessHardware()
    check("nothing asked for, nothing recorded",
          hardware.sample_fields() == {"gas_sccm": None, "gate_pct": None,
                                       "magnetron_w": None, "shutter": None})
    try:
        hardware.gas.set(20)
    except process.OperatorRequired:
        pass
    check("a manual step is not recorded until it is confirmed",
          hardware.sample_fields()["gas_sccm"] is None)
    hardware.gas.confirm()
    check("a confirmed manual step is recorded",
          hardware.sample_fields()["gas_sccm"] == 20.0)
    try:
        hardware.shutter.set(True)
    except process.OperatorRequired:
        pass
    hardware.shutter.confirm()
    check("the shutter records as 1 when open",
          hardware.sample_fields()["shutter"] == 1)

    # --- the base-pressure judgement must not be fooled by a dragged rotor -
    # Run 26: the pressure sat flat at 3.4e-2 mbar with the rotor held at
    # 1160 Hz of 1500 by the gas load, and the run called that "base
    # pressure" twelve minutes before the pump tripped on over-temperature.
    class Nominal(_QuietEngine):
        def nominal_speed(self):
            return 1500.0

    engine = Nominal()
    recipe = runs.BaseCalibration({"base_rate_pct": 2, "base_settle_min": 0.5})
    recipe.begin(engine)
    recipe.enter("basepressure")
    outcomes = [recipe.phase_basepressure(engine, {
        "t": 1000.0 + i * 2.0, "p_best": 3.36e-2, "speed": 1160.0})
        for i in range(120)]
    check("a flat pressure with the rotor dragged down is not base pressure",
          all(o is None for o in outcomes))
    outcomes = [recipe.phase_basepressure(engine, {
        "t": 2000.0 + i * 2.0, "p_best": 4.0e-6, "speed": 1500.0})
        for i in range(120)]
    check("the same flat curve at full speed is base pressure",
          "done" in outcomes)

    # --- a rotor held down by gas ends the run before the drive trips -------
    # Runs 25 and 26 spent minutes at the power limit, rotor sinking below
    # the 1200 Hz switchpoint, until Err006 or over-temperature stopped them.
    def dragged(recipe, start, seconds, speed=1160.0, motor=1):
        for i in range(int(seconds / 2) + 1):
            recipe.check_rotor(engine, {"t": start + i * 2.0, "speed": speed,
                                        "motor": motor, "p_best": 3.3e-2})

    recipe = runs.FullVacuum({})
    recipe.begin(engine)
    try:
        dragged(recipe, 0.0, 300.0)
        check("no rotor check before the rotor has reached full speed", True)
    except runs.Abort:
        check("no rotor check before the rotor has reached full speed", False)
    recipe.full_speed_at = 100.0
    try:
        dragged(recipe, 200.0, 100.0, speed=1350.0)
        check("a short dip below full speed is tolerated", True)
    except runs.Abort:
        check("a short dip below full speed is tolerated", False)
    recipe.check_rotor(engine, {"t": 302.0, "speed": 1500.0, "motor": 1})
    try:
        dragged(recipe, 304.0, 300.0, motor=0)
        check("a rotor coasting with its motor off is not 'held down'", True)
    except runs.Abort:
        check("a rotor coasting with its motor off is not 'held down'", False)
    recipe.check_rotor(engine, {"t": 690.0, "speed": 1500.0, "motor": 1})
    try:
        # Run 25: Err006 came five seconds after 1200 Hz. Nearing the
        # switchpoint must end the run on the spot, not after a dwell.
        recipe.check_rotor(engine, {"t": 692.0, "speed": 1260.0, "motor": 1})
        recipe.check_rotor(engine, {"t": 694.0, "speed": 1260.0, "motor": 1})
        check("nearing the switchpoint ends the run at once", False)
    except runs.Abort as abort:
        check("nearing the switchpoint ends the run at once",
              "switchpoint" in abort.reason, abort.reason[:60])
    recipe.check_rotor(engine, {"t": 696.0, "speed": 1500.0, "motor": 1})
    try:
        dragged(recipe, 700.0, runs.VacuumSequence.DRAGGED_LIMIT_S + 10,
                speed=1350.0)
        check("two minutes held below full speed by gas ends the run", False)
    except runs.Abort as abort:
        check("two minutes held below full speed by gas ends the run",
              "more gas than this pump can carry" in abort.reason,
              abort.reason[:60])

    # --- what the engine concluded is stored with the sample ----------------
    class Marking(runs.Procedure):
        name = "selftest_marking"
        phases = ("running", "done")
        interval = 0.05

        def step(self, engine, sample):
            sample["rate_pct_min"] = 1.25
            return len(engine._samples) >= 3

    with tempfile.TemporaryDirectory() as directory:
        database = dbmod.Database(os.path.join(directory, "order.db"))
        runs.CATALOGUE[Marking.name] = Marking
        try:
            engine = runs.RunEngine(database, registry.Registry())
            started = engine.start(Marking.name, {})
            deadline = time.monotonic() + 10
            while engine.status()["running"] and time.monotonic() < deadline:
                time.sleep(0.05)
            stored = database.samples(started["run"]["id"])
            check("a sample is stored after the procedure has seen it",
                  stored and all(s.get("rate_pct_min") == 1.25 for s in stored),
                  f"{[s.get('rate_pct_min') for s in stored]}")
            check("a run's samples carry the process-hardware columns",
                  stored and "gas_sccm" in stored[0] and "shutter" in stored[0])
        finally:
            del runs.CATALOGUE[Marking.name]
            database.close()

        # An existing database gains the new columns rather than failing the
        # first insert on a column the code believes is there.
        import sqlite3
        old = os.path.join(directory, "old.db")
        connection = sqlite3.connect(old)
        connection.executescript(
            "CREATE TABLE samples (id INTEGER PRIMARY KEY, run_id INTEGER, "
            "t REAL, at REAL, phase TEXT, p1 REAL, p1_status INTEGER, p2 REAL, "
            "p2_status INTEGER, speed REAL, set_speed REAL, current REAL, "
            "voltage REAL, power REAL, tms_temp REAL, error_code TEXT, "
            "station INTEGER, motor INTEGER);")
        connection.close()
        migrated = dbmod.Database(old)
        columns = {row["name"] for row in
                   migrated._db.execute("PRAGMA table_info(samples)")}
        missing = set(dbmod.SAMPLE_COLUMNS) - columns
        check("an older database gains every new sample column", not missing,
              f"missing {sorted(missing)}" if missing else "")
        migrated.close()

    # --- health indicators measured from a run's curve ----------------------
    curve = []
    t = 0.0
    for _ in range(10):                       # station on, motor off
        curve.append({"t": t, "motor": 0, "speed": 0.0, "power": 0.0,
                      "voltage": 23.25, "p_best": 0.5})
        t += 2.0
    speed = 0.0
    while speed < 1500:                       # run-up at the power limit
        speed = min(1500.0, speed + 15.0)
        curve.append({"t": t, "motor": 1, "speed": speed, "power": 82.0,
                      "voltage": 21.5, "p_best": 1e-3})
        t += 2.0
    for _ in range(20):                       # full speed, nothing to pump
        curve.append({"t": t, "motor": 1, "speed": 1500.0, "power": 11.0,
                      "voltage": 23.0, "p_best": 3e-5})
        t += 2.0
    for drag in range(30):                    # argon drags the rotor down
        curve.append({"t": t, "motor": 1, "speed": 1450.0 - drag * 10,
                      "power": 80.0, "voltage": 21.75, "p_best": 3.3e-2})
        t += 2.0
    found = trends.metrics(curve, 1500.0)
    # Motor on at 15 Hz, +15 Hz per 2 s sample, 95 % of 1500 at 1425 Hz:
    # 94 steps, 188 s.
    check("run-up is timed from rest to full speed",
          found["runup_s"] == 188.0, f"{found['runup_s']} s")
    check("friction is the full-speed power with nothing to pump",
          found["friction_w"] == 11.0, f"{found['friction_w']} W")
    check("supply sag is idle voltage minus the lowest under load",
          found["sag_v"] == 1.75, f"{found['sag_v']} V")
    # The first three drag samples (1450, 1440, 1430 Hz) are still above
    # 95 % of nominal; the other 27 are held down, 2 s each.
    check("time held below full speed is counted",
          found["held_s"] == 54.0, f"{found['held_s']} s")
    check("a base pressure taken with the rotor dragged down is flagged",
          trends.base_valid(curve, {"time_to_base_s": curve[-1]["t"]}, 1500.0)
          is False)

    # --- the pump simulator's power line ------------------------------------
    check("simulated power line is 60 W at nominal speed (gas mode 0)",
          fake_tc600.power_limit(1500, 1500) == 60.0)
    check("simulated power line reaches 80 W at 1300 Hz",
          abs(fake_tc600.power_limit(1300, 1500) - 80.0) < 1e-9)

    check("the pump's ratings name their source",
          pumpmod.RATINGS.get("source", "").startswith("PM 0504"))


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
    pump_service.command("off", override=True)


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
        recipe.step(hot, {"t": 10.0, "p_best": 5e-6, "phase": "holding"})
        check("an over-temperature flag aborts the run", False)
    except runs.Abort as abort:
        check("an over-temperature flag aborts the run", True,
              abort.reason[:44])

    warm = _QuietEngine()
    try:
        recipe.step(warm, {"t": 10.0, "p_best": 5e-6, "tms_temp": 95.0})
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
        pump_service.command("on", override=True)
        pump_service.command("motor_on", override=True)
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


def wait_until(predicate, timeout: float = 15.0, step: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


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
    pump_service.command("on", override=True)
    pump_service.command("motor_on", override=True)
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
    supervisor.check()                         # learn the healthy state
    pump_service.command("motor_on", override=True)
    wait_until(lambda: (pump_service.snapshot().get("setpoints", {})
                        .get("MotorPump")) is True)
    supervisor.check()
    state[303] = "Err006"
    wait_until(lambda: pump_service.snapshot().get("fault", {}).get("code"))
    supervisor.check()
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
    supervisor.check()
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

    pump_service.command("motor_on", override=True)
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
    refused = engine.start("hold", {"duration_min": 0.05})
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
    pump_service.command("motor_off", override=True)


def test_accounts() -> None:
    """Accounts, roles and control, over real HTTP against the real handler.

    The point of lab/auth.py is what it refuses, so most of these are
    refusals: a viewer commanding the pump, an operator overriding an
    interlock, a second operator while the first holds control, an idle
    session, a paired tablet, a reused pairing code, the fifth wrong guess.
    The registry is empty on purpose: a 404 "no device" means the request
    got past every permission check, which is what is being asked.
    """
    import json as jsonmod
    import sqlite3
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from lab import api as apimod
    from lab import auth as authmod

    print("\nAccounts, roles and control")
    check("passwords hash and verify",
          authmod.verify_password("correct horse", authmod.hash_password("correct horse"))
          and not authmod.verify_password("wrong", authmod.hash_password("correct horse")))
    check("a username is checked", authmod.check_username("Ab") is not None
          and authmod.check_username("ana.p") is None)
    check("a short password is refused", authmod.check_password("short") is not None)
    check("a pairing code survives being typed loosely",
          authmod.normalise_code("ab3-k9m 2xp") == "AB3K9M2XP")
    check("loopback always reaches the panel, other networks only if allowed",
          apimod.address_allowed("127.0.0.1", apimod.parse_networks("10.0.0.0/8"))
          and apimod.address_allowed("10.1.2.3", apimod.parse_networks("10.0.0.0/8"))
          and not apimod.address_allowed("172.16.2.9", apimod.parse_networks("10.0.0.0/8"))
          and apimod.address_allowed("172.16.2.9", []))

    with tempfile.TemporaryDirectory() as directory:
        # An events table from before accounts must gain its `user` column.
        old = os.path.join(directory, "old.db")
        with sqlite3.connect(old) as connection:
            connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, run_id "
                               "INTEGER, at REAL NOT NULL, level TEXT NOT NULL, "
                               "message TEXT NOT NULL, detail TEXT)")
        migrated = dbmod.Database(old)
        migrated.add_event(None, "action", "after the migration", user="someone")
        check("an old database gains the events.user column",
              migrated.events(limit=1)[0].get("user") == "someone")
        migrated.close()

        database = dbmod.Database(os.path.join(directory, "accounts.db"))
        accounts = authmod.Auth(database, "chamber-1")
        engine = runs.RunEngine(database, registry.Registry(), chamber="chamber-1")
        application = apimod.Application(registry.Registry(), database, engine,
                                         auth=accounts,
                                         chamber={"id": "chamber-1", "name": "Chamber 1"})
        handler = type("QuietHandler", (apimod.Handler,),
                       {"app": application, "log_message": lambda *a: None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(path, body=None, token=None):
            request = urllib.request.Request(
                base + path, method="POST" if body is not None else "GET",
                data=jsonmod.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            if token:
                request.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, jsonmod.loads(response.read())
            except urllib.error.HTTPError as error:
                raw = error.read()
                return error.code, (jsonmod.loads(raw) if raw[:1] == b"{" else {})

        def login(name, password):
            return call("/api/login", {"username": name, "password": password})[1].get("token")

        command = {"device": "pump", "action": "on", "confirm": True}
        try:
            code, ping = call("/api/ping")
            check("the ping stays open and says what it is (the kiosk needs it)",
                  code == 200 and ping.get("service") == "vacuum-lab"
                  and ping.get("auth") == {"enabled": True, "owner": False})
            check("no session, no state", call("/api/state")[0] == 401)
            check("no session, no command", call("/api/command", command)[0] == 401)

            authmod.bootstrap_owner(database, "chamber-1", "owner", "owner-pass-1", "Olga")
            check("the owner is made on the machine",
                  call("/api/ping")[1]["auth"]["owner"] is True)
            owner = login("owner", "owner-pass-1")
            check("the owner signs in", bool(owner))

            check("a sign-up is accepted",
                  call("/api/signup", {"username": "ana", "name": "Ana",
                                       "password": "ana-pass-1"})[0] == 200)
            code, body = call("/api/login", {"username": "ana", "password": "ana-pass-1"})
            check("a pending account cannot sign in", code == 403, body.get("error", ""))
            people = call("/api/users", token=owner)[1]["people"]
            ana_id = next(p["id"] for p in people if p["username"] == "ana")
            call("/api/signup", {"username": "vic", "name": "Vic", "password": "vic-pass-1"})
            people = call("/api/users", token=owner)[1]["people"]
            vic_id = next(p["id"] for p in people if p["username"] == "vic")
            call("/api/users/approve", {"id": ana_id, "role": "operator"}, owner)
            call("/api/users/approve", {"id": vic_id, "role": "viewer"}, owner)
            ana, vic = login("ana", "ana-pass-1"), login("vic", "vic-pass-1")
            check("approved accounts sign in", bool(ana) and bool(vic))

            check("a viewer sees the state", call("/api/state", token=vic)[0] == 200)
            check("a viewer cannot command the pump",
                  call("/api/command", command, vic)[0] == 403)
            check("a viewer cannot make the pump safe",
                  call("/api/pump/secure", {"confirm": True}, vic)[0] == 403)
            check("a viewer cannot list the accounts", call("/api/users", token=vic)[0] == 403)

            code, body = call("/api/command", command, ana)
            check("an operator's command gets past every check (and takes control)",
                  code == 404 and "no device" in body.get("error", ""), f"{code}")
            state = call("/api/state", token=owner)[1]
            check("control is reported as Ana's",
                  state["control"].get("holder") == "Ana" and not state["control"]["mine"])
            code, body = call("/api/command", command, owner)
            check("nobody else commands while she holds control, not even the owner",
                  code == 409, body.get("error", ""))
            check("an operator may not override an interlock",
                  call("/api/command", {**command, "override": True}, ana)[0] == 403)
            check("an admin may (and gets past the check)",
                  call("/api/command", {**command, "override": True}, owner)[0] in (404, 409))
            check("making the pump safe needs no control",
                  call("/api/pump/secure", {"confirm": True}, owner)[0] == 200)
            check("stopping a run and keeping the vacuum needs no control",
                  call("/api/run/stop", {"shutdown": False}, owner)[0] == 200)
            check("powering down needs control",
                  call("/api/run/stop", {"shutdown": True}, owner)[0] == 409)
            check("an unlisted action is refused to an operator",
                  call("/api/not-a-route", {}, ana)[0] == 403)
            check("an admin takes control over by force",
                  call("/api/control/take", {"force": True}, owner)[0] == 200)
            check("Ana has lost it", call("/api/command", command, ana)[0] == 409)
            call("/api/control/release", {}, owner)

            # Control lapses once the holder's polling stops.
            saved = authmod.LEASE_S
            authmod.LEASE_S = 0.3
            call("/api/control/take", {}, ana)
            time.sleep(0.6)
            check("control lapses when the holder's panel goes quiet",
                  call("/api/command", command, owner)[0] == 404)
            authmod.LEASE_S = saved
            call("/api/control/release", {}, owner)

            with database._lock:
                database._db.execute(
                    "UPDATE sessions SET active_at = active_at - 3600 WHERE user_id=?",
                    (ana_id,))
                database._db.commit()
            code, body = call("/api/command", command, ana)
            check("an idle session must give its password again", code == 401
                  and body.get("reauth") is True)
            call("/api/session/touch", {}, ana)
            check("a touch does not wake an idle session",
                  call("/api/command", command, ana)[0] == 401)
            check("the password does", call("/api/session/reauth",
                                            {"password": "ana-pass-1"}, ana)[0] == 200
                  and call("/api/command", command, ana)[0] == 404)
            call("/api/control/release", {}, ana)

            pairing = call("/api/devices/add", {"label": "Wall"}, owner)[1]
            code, paired = call("/api/pair", {"code": pairing["code"]})
            tablet = paired.get("token")
            check("a tablet pairs with its code", code == 200 and bool(tablet))
            check("a pairing code works once",
                  call("/api/pair", {"code": pairing["code"]})[0] == 401)
            check("a paired tablet watches", call("/api/state", token=tablet)[0] == 200)
            check("a paired tablet cannot act",
                  call("/api/alarms/ack", {"id": "all"}, tablet)[0] == 403)

            temporary = call("/api/users/reset-password", {"id": ana_id}, owner)[1]["temporary"]
            check("a reset ends the old sessions", call("/api/state", token=ana)[0] == 401)
            fresh = login("ana", temporary)
            code, body = call("/api/command", command, fresh)
            check("a temporary password must be changed before acting",
                  code == 403 and body.get("must_change") is True)
            call("/api/users/status", {"id": vic_id, "enabled": False}, owner)
            check("a disabled account is out at once", call("/api/state", token=vic)[0] == 401)
            check("an admin cannot change the owner",
                  call("/api/users/role", {"id": 1, "role": "viewer"}, owner)[0] == 403)

            for attempt in range(6):
                code, _ = call("/api/login", {"username": "owner", "password": f"guess{attempt}x"})
            check("guessing passwords is slowed down", code == 429, f"{code}")

            snapshot = os.path.join(directory, "download.db")
            database.snapshot(snapshot)
            with sqlite3.connect(snapshot) as connection:
                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
            check("the downloadable database carries no accounts",
                  not tables & set(dbmod.ACCOUNT_TABLES) and "runs" in tables,
                  ", ".join(sorted(tables)))
            check("actions are recorded under the name of who took them",
                  any(e.get("user") == "owner" and "took control from" in e["message"]
                      for e in database.events(limit=200)))
        finally:
            server.shutdown()
            server.server_close()

        # With accounts off (--no-auth) everything is the local owner.
        open_app = apimod.Application(registry.Registry(), database, engine)
        handler = type("OpenHandler", (apimod.Handler,),
                       {"app": open_app, "log_message": lambda *a: None})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            code, state = call("/api/state")
            check("with accounts off the state needs no session",
                  code == 200 and state["me"]["kind"] == "local")
            check("and a command gets through", call("/api/command", command)[0] == 404)
        finally:
            server.shutdown()
            server.server_close()
            engine.registry.close()
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


def test_discovery(pump_port: str, gauge_port: str) -> None:
    """Finding the instruments by asking: no port is assumed.

    Runs after every service has let go of the simulators' ports -- a probe
    is a second client on a port, and one owner per port is the rule.
    """
    print("\nFinding the instruments")
    from lab import discovery

    found = discovery.probe_tc600(pump_port)
    check("the TC 600 probe recognises the pump",
          found is not None and found["identity"].get("ElecName") == "TC600",
          found["says"] if found else "nothing answered")
    check("the TC 600 probe does not mistake the gauge for a pump",
          discovery.probe_tc600(gauge_port) is None)
    found = discovery.probe_tpg(gauge_port)
    check("the TPG probe recognises the gauge and its baud rate",
          found is not None and found["baudrate"] == 9600,
          found["says"] if found else "nothing answered")
    check("the TPG probe does not mistake the pump for a gauge",
          discovery.probe_tpg(pump_port) is None)

    tried: list = []
    found = discovery.find("tc600", ["/dev/does-not-exist", gauge_port, pump_port],
                           tried=tried)
    check("a search finds the pump on whichever port it is",
          found is not None and found["device"] == os.path.realpath(pump_port))
    check("the search reports what each port said",
          [t["answered"] for t in tried] == [False, True], str(tried))

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "ports.json")
        discovery.PortMemory(path).remember("tc600", pump_port, "BG03FP5F")
        memory = discovery.PortMemory(path)
        check("the adapter that carried an instrument is remembered",
              memory.preferred("tc600", [{"device": "/dev/ttyUSB7",
                                          "serial_number": "BG03FP5F"}])[0]
              == "/dev/ttyUSB7")

        # A service started on a dead port finds its instrument by itself.
        devices = registry.Registry()
        service = devices.add(pumpmod.PumpService("pump", "/dev/no-such-port"))
        service.SEARCH_AFTER_S = 0.5
        service.SEARCH_EVERY_S = 1.0
        # Only the simulators' ports: on the lab PC the default search
        # reaches /dev/tc600, and the real pump answers there.
        simulated = [{"device": gauge_port, "serial_number": ""},
                     {"device": pump_port, "serial_number": ""}]
        service.finder = discovery.Finder(
            devices, memory, enabled=True, udev_names={},
            ports=lambda exclude=(): [p for p in simulated
                                      if discovery.real(p["device"])
                                      not in {discovery.real(x) for x in exclude}])
        try:
            moved = wait_until(lambda: service.snapshot().get("connected"), 30)
            check("a silent instrument is found on another port and reconnected",
                  moved and os.path.realpath(service.port)
                  == os.path.realpath(pump_port),
                  f"now on {service.port}")
            check("the move is recorded for the alarm",
                  (service.moved or {}).get("from") == "/dev/no-such-port")
        finally:
            devices.close()


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
        test_cold_cathode_interlock()
        test_analysis()
        test_process()
        test_equipment()
        test_deposition_engine()
        test_database()
        test_accounts()
        test_history_and_charts()
        test_run_engine(pump_port, gauge_port)
        test_discovery(pump_port, gauge_port)
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
