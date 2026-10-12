#!/usr/bin/env python3
"""End-to-end test of the whole stack against the simulators, no hardware needed.

Runs the fake pump and the fake gauge in background threads on pseudo-terminals
and drives the real client code against them: `./tools/selftest.py`

Sections in dependency order -- the drivers, the analysis maths, the process
hardware, the store, accounts, the chart recorder, a whole recorded run --
then five that make things go wrong on purpose: `faults` (lost, late and
refused replies; the interlocks over every combination of readings),
`timing` (how fresh the readings are, how soon a run reacts, the clock set
an hour either way), `lifecycle` (a run's start, end and clean-up when the
database, the service or the process fails), `authority` (overrides and
who may change the drive) and `security` (idle sessions, bursts of
sign-ins and connections, wrong clocks, stray hosts and origins, downloads
that must not hold up the rig). `--only NAME` runs one; see SECTIONS.
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

        engine._flush()                  # its log, written before the close
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
        # switchpoint must end the run on the spot -- on the first reading
        # below full speed, if that one is already there.
        recipe.check_rotor(engine, {"t": 692.0, "speed": 1260.0, "motor": 1})
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


def test_accounts() -> None:
    """Accounts, roles and control, over real HTTP against the real handler.

    The point of lab/auth.py is what it refuses, so most of these are
    refusals: a viewer commanding the pump, an operator overriding an
    interlock, a second operator while the first holds control, an idle
    session, a paired tablet, a reused pairing code, the fifth wrong guess.
    The registry is empty on purpose: a 404 "no device" means the request
    got past every permission check, which is what is being asked.
    """
    import http.client
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
            check("the ping does not list the serial ports",
                  "devices" not in ping and bool(ping.get("build")))
            check("no session, no state", call("/api/state")[0] == 401)

            # Bodies are refused before they are read: no size the client
            # claims is allocated, and a negative length never blocks.
            def raw_post(length_header, data=b"{}"):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1],
                                                        timeout=10)
                connection.putrequest("POST", "/api/login")
                connection.putheader("Content-Type", "application/json")
                connection.putheader("Content-Length", length_header)
                connection.endheaders()
                connection.send(data)
                status = connection.getresponse().status
                connection.close()
                return status
            check("a body claiming more than 64 KB is refused unread",
                  raw_post(str(10 ** 9)) == 413)
            check("a negative Content-Length is refused, not waited on",
                  raw_post("-1") == 400)
            check("a JSON body that is not an object reads as empty, not a crash",
                  call("/api/signup", [1, 2, 3])[0] == 400)
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
            waiver = {"rule": "pressure_unknown", "reason": "selftest",
                      "checked": True}
            check("an operator may not override an interlock",
                  call("/api/command", {**command, "override": waiver},
                       ana)[0] == 403)
            check("an admin may (and gets past the check)",
                  call("/api/command", {**command, "override": waiver}, owner)[0]
                  in (404, 409))
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
            check("an override must name the one rule it waives",
                  call("/api/command", {**command, "override": True},
                       owner)[0] == 400)
            check("and must say the rig was checked in person",
                  call("/api/command", {**command, "override": {
                      **waiver, "checked": False}}, owner)[0] == 400)
            call("/api/control/release", {}, owner)

            # One account on two screens is still one controller: acting on
            # the second never moves control; pressing Take control does.
            second = login("ana", "ana-pass-1")
            call("/api/command", command, ana)
            code, body = call("/api/command", command, second)
            check("the same account on another screen cannot act implicitly",
                  code == 409 and body.get("same_user") is True, f"{code}")
            state = call("/api/state", token=second)[1]
            check("and is told it is its own account holding control",
                  state["control"].get("same_user") is True)
            check("an explicit Take control moves it without an admin",
                  call("/api/control/take", {}, second)[0] == 200
                  and call("/api/command", command, second)[0] == 404
                  and call("/api/command", command, ana)[0] == 409)
            call("/api/control/release", {}, second)
            call("/api/logout", {}, second)

            # The person whose run is going may watch longer before the
            # password is asked again.
            accounts.runner = lambda: "ana"
            check("whoever's run is going gets the longer idle limit",
                  call("/api/me", token=ana)[1]["me"]["idle_after_s"]
                  == round(authmod.RUN_IDLE_S)
                  and call("/api/me", token=owner)[1]["me"]["idle_after_s"]
                  == round(authmod.IDLE_S))
            accounts.runner = None

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
            # The server times touches on the monotonic clock while it runs;
            # age those too, as an hour away from the tablet would.
            for row in database.sessions(ana_id):
                if row["token_hash"] in accounts._touched:
                    accounts._touched[row["token_hash"]] -= 3600
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


# --- fault injection ---------------------------------------------------------
#
# The simulators answer perfectly unless told otherwise, and the real bus does
# not: replies are lost, arrive late, or come back for the wrong question.
# Each check below makes one such thing happen and asserts a rule that must
# hold anyway. Each one that covers a fix failed against the code before it --
# which is the point of writing them first; the rest (the rig's gauge layout,
# a fault that returns) keep behaviour that was already right from breaking.


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


def test_faults(pump_port: str, gauge_port: str) -> None:
    print("\nFaults on the bus, and the rules that must hold anyway")
    from lab import alarms
    from lab import api as apimod

    directory = tempfile.mkdtemp(prefix="selftest-faults-")
    database = dbmod.Database(os.path.join(directory, "faults.db"))
    limit = pumpmod.TURBO_START_MAX_MBAR

    # --- the driver: a late reply is not the next question's answer --------
    def late_reply():
        fake_tc600.reset()
        fake_tc600.FAULTS.update({"value": {309: 1500}, "late": {309: 1}})
        with TC600(pump_port, address=1, timeout=0.3, retries=0) as pump:
            try:
                pump.read_raw(309)          # its reply comes after it gave up
            except protocol.ProtocolError:
                pass
            code = pump.read("ErrorCode")
            speed = pump.read("ActualSpd")
        return (code == "000000" and speed == 1500,
                f"error code read as {code!r}, then speed {speed}")
    attempt("a reply that arrives late is never taken for the next parameter's",
            late_reply)

    def echoing_converter():
        fake_tc600.reset()
        fake_tc600.FAULTS["echo"] = True         # every request looped back
        with TC600(pump_port, address=1, timeout=0.5, retries=0) as pump:
            speed = pump.read("ActualSpd")
            learned = pump.echoes
            written = pump.write("Standby", True)
            standby = fake_tc600.STATE[2]
        fake_tc600.reset()
        return (speed == 0 and learned is True and written is True
                and standby is True,
                f"speed {speed}, echo learned {learned}, write read back "
                f"{written}")
    attempt("an RS-485 converter that echoes is learned, and its echo never "
            "taken for the reply", echoing_converter)

    # --- every combination of what the pump reading can say ------------------
    service = pumpmod.PumpService("table", "auto")
    service.close()
    service._thread.join(timeout=5)

    class Gauge:
        """Stands in for the gauge: a pressure, or none."""
        mbar = None

        def snapshot(self):
            return {"connected": True, "age": 0.5}

        def best_pressure(self, snap=None):
            return self.mbar

    gauge = Gauge()
    service.attach_pressure(gauge)

    def table():
        import itertools

        max_age = getattr(pumpmod, "SNAPSHOT_MAX_AGE_S", 5.0)
        breaches, cases = [], 0
        for (connected, station, motor, speed, fault, mbar, age,
             dwell) in itertools.product(
                (True, False),                   # contact
                (True, False, None),             # station: on, off, not read
                (True, False, None),             # motor flag
                (None, 0, 4, 1500),              # speed: unread, rest, windmill, top
                ("000000", "Err006", None),      # error code
                (None, 1e-3, 5.0),               # chamber: unknown, roughed, not
                (0.5, 30.0),                     # age of the reading, s
                (None, 5.0, 60.0)):              # seconds since 0 Hz was first read
            cases += 1
            now = time.monotonic()
            gauge.mbar = mbar
            service._rotor_zero_since = None if dwell is None else now - dwell
            service._speed_unread = 0
            status, setpoints = {}, {}
            for target, name, value in ((status, "ActualSpd", speed),
                                        (status, "ErrorCode", fault),
                                        (setpoints, "PumpgStatn", station),
                                        (setpoints, "MotorPump", motor)):
                if value is not None:
                    target[name] = value
            rules = service.interlocks({
                "connected": connected, "age": age, "status": status,
                "setpoints": setpoints, "identity": {"NominalSpd": 1500}})
            fresh = connected and age <= max_age
            settled = dwell is not None and dwell >= pumpmod.STOP_DWELL_SECONDS
            may_stop = (fresh and speed == 0 and settled
                        and not (station is True and motor is True))
            may_start = fresh and (motor is True or (
                station is True and mbar is not None and mbar <= limit))
            case = (f"station {station}, motor {motor}, speed {speed}, fault "
                    f"{fault}, {mbar} mbar, {age} s old, dwell {dwell}")
            if rules.get("off", {}).get("allowed") and not may_stop:
                breaches.append(f"station off with {case}")
            if rules.get("motor_on", {}).get("allowed") and not may_start:
                breaches.append(f"motor start with {case}")
        return (not breaches, f"{len(breaches)} unsafe of {cases} combinations"
                + (f", e.g. {breaches[0]}" if breaches else ""))
    attempt("no combination of readings lets the station stop or the turbopump "
            "start unsafely", table)

    def vent(snap):
        try:
            service._check_param("EnableVent", 1, snap)
        except pumpmod.Interlock as stop:
            return True, stop.reason
        return False, "venting was allowed"
    attempt("venting is refused while the rotor speed is unknown",
            lambda: vent({"connected": True, "age": 0.5, "status": {},
                          "setpoints": {}, "identity": {"NominalSpd": 1500}}))
    attempt("venting at full speed is refused before the nominal speed is known",
            lambda: vent({"connected": True, "age": 0.5,
                          "status": {"ActualSpd": 1500}, "setpoints": {},
                          "identity": {}}))

    # --- the run's dragged-rotor clock survives a lost reading -----------------
    class Nominal(_QuietEngine):
        def nominal_speed(self):
            return 1500.0

    def dragged_through(gap):
        engine = Nominal()
        recipe = runs.FullVacuum({})
        recipe.begin(engine)
        recipe.full_speed_at = 0.0
        t = 10.0
        try:
            for _ in range(50):                       # 100 s held at 90 %
                recipe.check_rotor(engine, {"t": t, "speed": 1350.0, "motor": 1})
                t += 2.0
            recipe.check_rotor(engine, {"t": t, **gap})
            t += 2.0
            for _ in range(20):                       # and 40 s more
                recipe.check_rotor(engine, {"t": t, "speed": 1350.0, "motor": 1})
                t += 2.0
        except runs.Abort as abort:
            return True, abort.reason[:60]
        return False, "two and a half minutes held down, and the run went on"
    attempt("a lost speed reading does not restart the two-minute clock",
            lambda: dragged_through({"speed": None, "motor": 1}))
    attempt("nor does a lost motor-flag reading",
            lambda: dragged_through({"speed": 1350.0, "motor": None}))

    # --- three ways to start the rotor, only one of them was checked -----------
    def station_on_with_motor_set():
        fake_tc600.reset()
        fake_tc600.STATE[23] = True              # the motor flag left set
        with Rig(pump_port) as rig:
            result = rig.pump.command("on")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (result.get("ok") and fake_tc600.STATE[10] is True and spun == 0
                and fake_tc600.STATE[23] is False,
                f"rotor at {spun:.0f} Hz, motor flag {fake_tc600.STATE[23]}: "
                f"{result.get('error') or 'sent'}")
    attempt("starting the station with the motor flag set does not start the "
            "rotor", station_on_with_motor_set)

    def ack_with_motor_set():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (spun == 0 and str(fake_tc600.STATE[303]) == "000000"
                and fake_tc600.STATE[23] is False,
                f"rotor at {spun:.0f} Hz, fault {fake_tc600.STATE[303]}: "
                f"{result.get('error') or 'sent'}")
    attempt("acknowledging a fault with the motor on does not restart the rotor",
            ack_with_motor_set)

    def secure_motor_unread():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["drop"] = {23: -1}     # the motor flag never answers
        with Rig(pump_port) as rig:
            unread = rig.fresh(lambda s: s.get("connected")
                               and "MotorPump" not in s.get("setpoints", {}))
            rig.pump.secure("selftest", settle_s=3.0)
            time.sleep(2.0)
            spun = fake_tc600._speed
        # The motor-off was sent (the drive did it), but with no reply and no
        # reading to confirm it the fault is left latched: acknowledging it
        # unconfirmed is what restarted the rotor.
        return (unread is not None and spun == 0
                and fake_tc600.STATE[23] is False
                and fake_tc600.STATE[303] == "Err006",
                f"rotor at {spun:.0f} Hz, motor flag {fake_tc600.STATE[23]}, "
                f"fault {fake_tc600.STATE[303]}")
    attempt("making safe switches a motor it cannot read off before clearing "
            "the fault", secure_motor_unread)

    attempt("acknowledging a fault needs the operator's confirmation",
            lambda: ("ack" in apimod.CONFIRM_REQUIRED, "ack is guarded"
                     if "ack" in apimod.CONFIRM_REQUIRED else "ack is not"))

    def station_on_acknowledges():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["station_acks"] = True  # P010 = 1 also acknowledges
        with Rig(pump_port) as rig:
            result = rig.pump.command("on")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (result.get("ok") and spun == 0
                and fake_tc600.STATE[303] == "Err006",
                f"rotor at {spun:.0f} Hz, fault {fake_tc600.STATE[303]}")
    attempt("\"station on\" with the station already on sends nothing that "
            "could clear a fault", station_on_acknowledges)

    def echo_lost_motor_off():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["drop_write"] = {23: -1}   # the write works, unheard
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (result.get("ok") and fake_tc600.STATE[303] == "000000"
                and fake_tc600.STATE[23] is False and spun == 0,
                f"{result.get('error') or 'acknowledged'}; rotor {spun:.0f} Hz")
    attempt("a motor-off whose reply is lost, but which the drive confirms, "
            "lets the fault be cleared", echo_lost_motor_off)

    def motor_write_refused():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["refuse_write"] = {23: "_LOGIC"}
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (not result.get("ok") and result.get("rule") == "motor_off_first"
                and fake_tc600.STATE[303] == "Err006" and spun == 0,
                f"{(result.get('error') or 'acknowledged')[:60]}; rule "
                f"{result.get('rule')}; rotor {spun:.0f} Hz")
    attempt("a drive that refuses the motor-off keeps its fault until a person "
            "decides, and says which rule", motor_write_refused)

    def waived_motor_off_is_a_start():
        fake_tc600.reset()
        fake_tc600.STATE[23] = True              # motor flag set, station off
        with Rig(pump_port) as rig:          # no gauge: the pressure unknown
            result = rig.pump.command("on", override=["motor_off_first"])
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (not result.get("ok") and result.get("rule") == "pressure_unknown"
                and fake_tc600.STATE[10] is False and spun == 0,
                f"refused by {result.get('rule')}; station "
                f"{fake_tc600.STATE[10]}, rotor {spun:.0f} Hz")
    attempt("waiving \"motor off first\" does not waive the start's pressure "
            "rule", waived_motor_off_is_a_start)

    def waived_ack():
        fake_tc600.reset()
        fake_tc600.STATE.update({23: True, 303: "Err006"})   # station off
        with Rig(pump_port) as rig:
            off = rig.pump.command("ack", override=["motor_off_first"])
            cleared = fake_tc600.STATE[303] == "000000"
            fake_tc600.STATE.update({10: True, 303: "Err006"})
            rig.fresh(lambda s: s.get("setpoints", {}).get("PumpgStatn") is True)
            on = rig.pump.command("ack", override=["motor_off_first"])
            time.sleep(1.5)
            spun = fake_tc600._speed
        return (off.get("ok") and cleared and not on.get("ok")
                and on.get("rule") == "pressure_unknown" and spun == 0,
                f"station off: {off.get('error') or 'acknowledged'}; station on: "
                f"refused by {on.get('rule')}, rotor {spun:.0f} Hz")
    attempt("a waived \"motor off first\" on acknowledge is judged as a start "
            "only when the station is on", waived_ack)

    def found_running_at_start():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0               # running before the panel
        saved = getattr(alarms.Supervisor, "FOUND_DRIVEN_WAIT_S", None)
        alarms.Supervisor.FOUND_DRIVEN_WAIT_S = 2.0
        try:
            with Rig(pump_port) as rig:          # no gauge: pressure unknown
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 6.0)
                found = [a for a in supervisor.describe()["recent"]
                         if a["key"] == "motor_at_start"]
        finally:
            if saved is None:
                del alarms.Supervisor.FOUND_DRIVEN_WAIT_S
            else:
                alarms.Supervisor.FOUND_DRIVEN_WAIT_S = saved
        return bool(found), found[0]["title"] if found else "nothing said"
    attempt("a turbopump found running at startup with the pressure unknown "
            "raises an alarm", found_running_at_start)

    def deploy_under_vacuum():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-6        # pumped down
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        with Rig(pump_port, gauge_port) as rig:
            wait_until(lambda: rig.gauge.best_pressure() is not None, 10, 0.1)
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 6.0)
            keys = [a["key"] for a in supervisor.describe()["recent"]]
        return ("motor_at_start" not in keys and "motor_started" not in keys
                and "motor_after_outage" not in keys,
                f"alarms: {keys or 'none'}")
    attempt("restarting the panel with the turbopump at speed under vacuum "
            "raises nothing", deploy_under_vacuum)

    def late_drive_under_vacuum():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-6
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        fake_tc600.FAULTS["silent"] = True       # the drive answers late
        saved = alarms.Supervisor.OFFLINE_AFTER_S
        alarms.Supervisor.OFFLINE_AFTER_S = 2.0
        try:
            with Rig(pump_port, gauge_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 5.0)
                fake_tc600.FAULTS.clear()
                watching(supervisor, 6.0)
                keys = [a["key"] for a in supervisor.describe()["recent"]]
        finally:
            alarms.Supervisor.OFFLINE_AFTER_S = saved
        return ("motor_after_outage" not in keys and "motor_at_start" not in keys,
                f"alarms: {keys}")
    attempt("a drive that answers late, running under vacuum, is not taken for "
            "one restarted after an outage", late_drive_under_vacuum)

    # --- the reading the decisions are made on, at the real bus's speed ---------
    def slow_bus():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["delay_s"] = 0.08      # ~ the real 9600 baud bus
        stale, judged, dwell_restarts = 0, 0, 0
        lose = ("ErrorCode", "MotorPump", "PumpgStatn", "DrvPower")
        numbers = {"ErrorCode": 303, "MotorPump": 23, "PumpgStatn": 10,
                   "DrvPower": 316}
        with Rig(pump_port, interval=None) as rig:
            rig.fresh()
            since = rig.pump._rotor_zero_since
            seen, turn = 0, 0
            end = time.monotonic() + 15
            while time.monotonic() < end:
                snap = rig.pump.snapshot()
                if snap.get("timestamp", 0) != seen:     # one lost reply a cycle
                    seen = snap.get("timestamp", 0)
                    name = lose[turn % len(lose)]
                    fake_tc600.FAULTS["drop"] = {numbers[name]: 1}
                    turn += 1
                rules = rig.pump.interlocks(snap)
                judged += 1
                if "stale_reading" in (rules["off"].get("rule"),
                                       rules["motor_on"].get("rule")):
                    stale += 1
                time.sleep(0.05)
            # and one lost speed reply does not restart the stop dwell
            fake_tc600.FAULTS["drop"] = {309: 1}
            rig.fresh(lambda s: "ActualSpd" not in s.get("status", {}))
            rig.fresh(lambda s: "ActualSpd" in s.get("status", {}))
            if rig.pump._rotor_zero_since != since:
                dwell_restarts += 1
        return (stale == 0 and dwell_restarts == 0,
                f"{stale} of {judged} judgements refused as stale; the stop "
                f"dwell {'restarted' if dwell_restarts else 'kept running'}")
    attempt("at the real bus's speed, with a reply lost every cycle, nothing is "
            "refused as stale", slow_bus)

    # --- a coasting rotor with its speed unread ---------------------------------
    def coast_unread():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600._speed = 400.0                # coasting, motor off
        fake_tc600.FAULTS["drop"] = {309: -1}    # the speed never answers
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            snap = rig.fresh(lambda s: s.get("connected")
                             and "ActualSpd" not in s.get("status", {}))
            rule = rig.pump.interlocks(snap)["off"] if snap else {}
            dwell, poll = pumpmod.STOP_DWELL_SECONDS, runs.RunEngine.SHUTDOWN_POLL_S
            pumpmod.STOP_DWELL_SECONDS, runs.RunEngine.SHUTDOWN_POLL_S = 1.0, 0.2
            stopped_at = None
            try:
                engine.shutdown_pump("selftest")
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and fake_tc600._speed > 0:
                    if fake_tc600.STATE[10] is False:
                        stopped_at = fake_tc600._speed
                        break
                    time.sleep(0.05)
                engine.cancel_shutdown()
            finally:
                pumpmod.STOP_DWELL_SECONDS = dwell
                runs.RunEngine.SHUTDOWN_POLL_S = poll
        return (rule.get("allowed") is False and stopped_at is None,
                "station stopped under a rotor at "
                f"{stopped_at:.0f} Hz" if stopped_at is not None
                else rule.get("reason", "no reading without the speed"))
    attempt("an unknown speed never lets the station stop, nor the power-down",
            coast_unread)

    # --- the supervisor, on a noisy bus ------------------------------------------
    def start_outside():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 4.0)            # station on, motor off
            fake_tc600.STATE[23] = True          # switched on at the drive
            found = wait_until(
                lambda: (supervisor.check() or True) and any(
                    a["key"] == "motor_started"
                    for a in supervisor.describe()["recent"]), 12, 0.1)
            fake_tc600.STATE[23] = False
            watching(supervisor, 4.0)
            asked = time.time()
            rig.pump.command("motor_on", override=["pressure_unknown"])
            watching(supervisor, 5.0)
            ours = [a for a in supervisor.describe()["recent"]
                    if a["key"] == "motor_started" and a["raised_at"] >= asked]
        return (found and not ours,
                ("no alarm for the start nobody asked for" if not found else
                 "an alarm for the panel's own start" if ours else
                 "alarm for the start at the drive, none for the panel's own"))
    attempt("a rotor started without the panel asking raises an alarm",
            start_outside)

    def start_unheard():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 4.0)
            fake_tc600.FAULTS["drop_write"] = {23: -1}   # it starts, unheard
            asked = time.time()
            result = rig.pump.command("motor_on",
                                      override=["pressure_unknown"])
            fake_tc600.FAULTS.pop("drop_write", None)
            watching(supervisor, 5.0)
            blamed = [a for a in supervisor.describe()["recent"]
                      if a["key"] == "motor_started" and a["raised_at"] >= asked]
        return (fake_tc600.STATE[23] is True and not blamed,
                f"command said {result.get('ok')}; "
                + ("blamed on somebody else" if blamed else "no false alarm"))
    attempt("a start whose replies were lost is not blamed on the front panel",
            start_unheard)

    class Snapshots:
        """A pump that says exactly what it is told to, one reading each."""

        def __init__(self):
            self.commanded = {}
            self.snap = {}
            self.seq = 0

        def say(self, station, motor, speed, code="000000"):
            self.seq += 1
            status = {"ErrorCode": code}
            if speed is not None:
                status["ActualSpd"] = speed
            self.snap = {"connected": True, "seq": self.seq, "age": 0.2,
                         "status": status, "identity": {"NominalSpd": 1500},
                         "setpoints": {"PumpgStatn": station,
                                       "MotorPump": motor}}

        def snapshot(self):
            return dict(self.snap)

        def nominal_speed(self, snap=None):
            return 1500.0

        def chamber_pressure(self):
            return {"mbar": None}

    def station_off_unread_speed():
        fake = Snapshots()
        devices = registry.Registry()
        devices._devices["pump"] = fake
        engine = runs.RunEngine(database, registry.Registry())
        supervisor = alarms.Supervisor(devices, engine, database,
                                       auto_recover=False)
        for _ in range(3):
            fake.say(True, False, 0)
            supervisor.check()
        for _ in range(2):
            fake.say(False, False, None)        # off, and no speed in it
            supervisor.check()
        found = [a for a in supervisor.describe()["recent"]
                 if a["key"] == "station_off"]
        return (bool(found) and found[0]["level"] == "critical",
                found[0]["level"] if found else "no alarm")
    attempt("a station-off read without the rotor speed is critical",
            station_off_unread_speed)

    def back_after_outage():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        saved = alarms.Supervisor.OFFLINE_AFTER_S
        alarms.Supervisor.OFFLINE_AFTER_S = 3.0
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 4.0)        # running, before the cut
                # Its power is cut -- for well over the 3 s threshold: the
                # sampler needs a cycle or two to say "no contact" at all.
                fake_tc600.FAULTS["silent"] = True
                watching(supervisor, 10.0)
                fake_tc600.FAULTS.pop("silent")      # ... and comes back on
                watching(supervisor, 5.0)
                found = [a for a in supervisor.describe()["recent"]
                         if a["key"] == "motor_after_outage"]
        finally:
            alarms.Supervisor.OFFLINE_AFTER_S = saved
        return bool(found), found[0]["title"] if found else "nothing said"
    attempt("a rotor found running after a silence nobody watched raises an "
            "alarm", back_after_outage)

    def noise():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            watching(supervisor, 4.0)
            lose, seen, turn = (10, 303, 23), 0, 0
            end = time.monotonic() + 14
            while time.monotonic() < end:
                stamp = rig.pump.snapshot().get("timestamp", 0)
                if stamp != seen:                # one lost reply per reading
                    seen = stamp
                    fake_tc600.FAULTS["drop"] = {lose[turn % 3]: 1}
                    turn += 1
                supervisor.check()
                time.sleep(0.05)
            keys = [a["key"] for a in supervisor.describe()["recent"]]
        bad = [k for k in keys if k in ("station_off", "pump_fault",
                                        "motor_started")]
        return (not bad and supervisor.recovering is None,
                f"{turn} readings each missing one reply; alarms: {keys or 'none'}")
    attempt("line noise alone raises no alarm and starts no recovery", noise)

    def one_recovery():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["value"] = {303: "Err006"}   # a fault that stays
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            wait_until(lambda: (supervisor.check() or True)
                       and supervisor.recovering is not None, 12, 0.1)
            for _ in range(3):
                fake_tc600.FAULTS["drop"] = {303: 1}
                rig.fresh(lambda s: "ErrorCode" not in s.get("status", {}))
                watching(supervisor, 2.0)
            count = len(supervisor._recoveries)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
        return count == 1, f"{count} recoveries for one fault"
    attempt("a lost error-code reply does not spend another recovery",
            one_recovery)

    def fault_returns():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["value"] = {303: "Err006"}
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            wait_until(lambda: (supervisor.check() or True)
                       and supervisor.recovering is not None, 12, 0.1)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
            fake_tc600.FAULTS.pop("value")       # it clears ...
            fake_tc600.STATE[303] = "000000"
            watching(supervisor, 4.0)
            fake_tc600.STATE[303] = "Err006"     # ... and comes back
            wait_until(lambda: (supervisor.check() or True)
                       and len(supervisor._recoveries) == 2, 12, 0.1)
            count = len(supervisor._recoveries)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
        return count == 2, f"{count} recoveries for two faults"
    attempt("a fault that clears and comes back gets its own recovery",
            fault_returns)

    # --- an identity that did not all answer at connect --------------------------
    def nominal_after_loss():
        fake_tc600.reset()
        fake_tc600.FAULTS["drop"] = {315: -1}
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            missing = "NominalSpd" not in rig.pump.snapshot().get("identity", {})
            during = engine.nominal_speed()
            fake_tc600.FAULTS["drop"] = {}
            read = wait_until(lambda: rig.pump.snapshot().get("identity", {})
                              .get("NominalSpd") == 1500, 10, 0.1)
        return (missing and during == 1500 and read,
                f"nominal speed {during} while unread; "
                + ("read once it answered" if read else "never read again"))
    attempt("a lost nominal-speed reply is asked for again, and full speed is "
            "never unknown meanwhile", nominal_after_loss)

    def gauge_unidentified():
        fake_tpg.reset()
        fake_tpg.FAULTS["drop"] = {"TID": -1}
        with Rig(gauge_port=gauge_port) as rig:
            missing = not rig.gauge.snapshot().get("identity", {}).get("gauges")
            result = rig.gauge.command("sensor_on", {"channel": 2})
            fake_tpg.FAULTS["drop"] = {}
            read = wait_until(lambda: bool(rig.gauge.snapshot()
                                           .get("identity", {}).get("gauges")),
                              12, 0.1)
        return (missing and not result.get("ok") and read,
                (result.get("error") or "switched on at atmosphere")[:70]
                + ("; identified later" if read else "; never identified"))
    attempt("an unidentified gauge is treated as a cold cathode, and identified "
            "later", gauge_unidentified)

    def rig_layout():
        fake_tpg.reset()
        fake_tpg.FAULTS["layout"] = "rig"
        with Rig(gauge_port=gauge_port) as rig:
            wait_until(lambda: bool(rig.gauge.snapshot().get("identity", {})
                                    .get("gauges")), 10, 0.1)
            snap = rig.gauge.snapshot()
            fitted = [g["identifier"] for g in snap["identity"].get("gauges", [])]
            empty = [r for r in snap.get("readings", []) if r["channel"] == 2]
            refused = rig.gauge.command("sensor_on", {"channel": 1})
        return (fitted == ["CO9", "noSe"] and empty and not empty[0]["valid"]
                and not refused.get("ok"),
                f"{fitted}, channel 2 status {empty[0]['status'] if empty else '?'}")
    attempt("the simulator can be the rig's own gauge, and the rules hold on it",
            rig_layout)

    fake_tc600.reset()
    fake_tpg.reset()
    database.close()


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
            engine = runs.RunEngine(database, rig.devices)
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
                engine = runs.RunEngine(database, rig.devices)
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
            engine = runs.RunEngine(database, registry.Registry())
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
            engine = runs.RunEngine(database, registry.Registry())
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


def test_lifecycle(pump_port: str, gauge_port: str) -> None:
    print("\nHow a run starts, ends, and survives what goes wrong around it")
    from lab import alarms, process

    directory = tempfile.mkdtemp(prefix="selftest-lifecycle-")
    database = dbmod.Database(os.path.join(directory, "lifecycle.db"))

    def wait_ended(engine, seconds):
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if not engine.status()["running"]:
                return time.monotonic() - started
            time.sleep(0.05)
        return None

    # --- the pump taken away from a run ------------------------------------------
    def no_power_down_under_a_run():
        fake_tc600.reset()
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            engine.start("hold", {"duration_min": 5, "interval_s": 1})
            time.sleep(1.0)
            refused = engine.shutdown_pump("selftest")
            stopping = engine.stop("selftest", shutdown=True)
            wait_ended(engine, 10)
            began = wait_until(lambda: (engine.shutdown_status() or {})
                               .get("phase") not in (None, "starting"), 10, 0.1)
            engine.cancel_shutdown()
        return (refused.get("ok") is False and stopping.get("ok") and began,
                refused.get("error") or "a power-down started under the run")
    attempt("a power-down is refused while a run is going, except through Stop",
            no_power_down_under_a_run)

    def motor_off_under_a_run():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-3
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0                 # already at full speed
        with Rig(pump_port, gauge_port, interval=None) as rig:
            engine = runs.RunEngine(database, rig.devices)
            engine.start("full_vacuum", {"interval_s": 1})
            reached = wait_until(lambda: engine.status().get("phase")
                                 in ("basepressure", "holding"), 15, 0.1)
            fake_tc600.STATE[23] = False           # switched off at the drive
            took = wait_ended(engine, 8)
            message = (engine.status().get("last") or {}).get("message", "")
            if took is None:
                engine.stop("selftest")
                wait_ended(engine, 10)
        return (reached and took is not None and "motor" in message,
                f"ended {took:.1f} s later: {message[:60]}" if took is not None
                else "the run carried on with its motor off")
    attempt("switching the turbopump off under a run ends the run",
            motor_off_under_a_run)

    def kept_unit():
        recipe = runs.FullVacuum({})
        recipe.begin(_QuietEngine())
        engine = _QuietEngine()
        calm = True
        try:
            recipe.check_pump_kept(engine, {"motor": 0, "station": 1})
            recipe.check_pump_kept(engine, {"motor": None, "station": 1})
        except runs.Abort:
            calm = False
        try:
            recipe.check_pump_kept(engine, {"motor": 1, "station": 1})
            recipe.check_pump_kept(engine, {"motor": None, "station": None})
            recipe.check_pump_kept(engine, {"motor": 0, "station": 1})
        except runs.Abort as abort:
            return calm, abort.reason[:60]
        return False, "the motor went off and nothing was said"
    attempt("only a motor seen on and then read off ends it; a lost reading does "
            "not", lambda: _try(kept_unit) or (False, "check_pump_kept missing"))

    def not_on_a_faulted_drive():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 303: "Err006"})
        with Rig(pump_port) as rig:
            rig.fresh(lambda s: s.get("status", {}).get("ErrorCode") == "Err006")
            engine = runs.RunEngine(database, rig.devices)
            faulted = engine.start("hold", {"duration_min": 1})
            if faulted.get("ok"):
                engine.stop("selftest")
                wait_ended(engine, 10)
            fake_tc600.reset()
            fake_tc600.FAULTS["silent"] = True
            wait_until(lambda: not rig.pump.snapshot().get("connected"), 10, 0.1)
            silent = engine.start("hold", {"duration_min": 1})
            if silent.get("ok"):
                engine.stop("selftest")
                wait_ended(engine, 10)
            fake_tc600.reset()
        return (not faulted.get("ok") and not silent.get("ok"),
                f"{faulted.get('error', 'started')[:45]} / "
                f"{silent.get('error', 'started')[:45]}")
    attempt("no run starts on a drive with a fault latched, or one it cannot read",
            not_on_a_faulted_drive)

    # --- the recipe's numbers, checked where the run starts ------------------------
    def every_field_bounded():
        loose = [f"{p.name}.{f['key']}" for p in runs.CATALOGUE.values()
                 for f in p.fields if f.get("type") in ("number", "exponent")
                 and (f.get("min") is None or f.get("max") is None)]
        return not loose, ", ".join(loose) or "all bounded"
    attempt("every numeric field of every procedure declares its range",
            every_field_bounded)

    def refused(procedure, config):
        engine = runs.RunEngine(database, registry.Registry())
        result = engine.start(procedure, config)
        if result.get("ok"):
            engine.stop("selftest")
            wait_ended(engine, 10)
        return result

    attempt("a stage timeout cleared on the form (sent as 0) is refused, not "
            "\"no limit\"",
            lambda: (lambda r: (r.get("ok") is False and "stalls after"
                                in r.get("error", ""), r.get("error", "it ran")))(
                refused("full_vacuum", {"stage_timeout_min": 0})))
    attempt("a deposition's pressure ceiling cannot be removed with a 0",
            lambda: (lambda r: (r.get("ok") is False and "Abort if pressure"
                                in r.get("error", ""), r.get("error", "it ran")))(
                refused("deposition", {"max_inlet_mbar": 0})))
    attempt("an empty box or a value out of range is refused, with its name",
            lambda: (lambda r: (r.get("ok") is False and "left empty"
                                in r.get("error", "") and "Sample every"
                                in r.get("error", ""), r.get("error", "it ran")))(
                refused("hold", {"duration_min": "", "interval_s": 0})))

    def stored_config():
        engine = runs.RunEngine(database, registry.Registry())
        result = engine.start("hold", {"interval_s": 2, "extra": "ignored"})
        engine.stop("selftest")
        wait_ended(engine, 10)
        config = database.run(result["run"]["id"])["config"] if result.get("ok") \
            else {}
        return (config == {"duration_min": 60, "interval_s": 2.0},
                f"stored {config}")
    attempt("the stored configuration is the one that ran: defaults filled in, "
            "unknown keys dropped", stored_config)

    # --- a database that fails ------------------------------------------------
    class Trouble(runs.Procedure):
        name = "selftest_trouble"
        phases = ("running", "done")
        interval = 0.1
        finished = False

        def step(self, engine, sample):
            raise runs.Abort("selftest pump trouble", pump_trouble=True)

        def finish(self, engine):
            type(self).finished = True

    runs.CATALOGUE[Trouble.name] = Trouble
    try:
        def write_fails_once():
            failing = FailingDatabase(database, lambda name, n:
                                      name == "add_sample" and n == 1)
            engine = runs.RunEngine(failing, registry.Registry())
            engine.start(Trouble.name, {})
            wait_ended(engine, 10)
            last = engine.status().get("last") or {}
            return (last.get("status") == "aborted" and last.get("pump_trouble")
                    and last.get("message") == "selftest pump trouble",
                    f"{last.get('status')}: {last.get('message')}")
        attempt("a failed write does not replace an abort, nor skip making the "
                "pump safe", write_fails_once)

        def writes_all_fail():
            Trouble.finished = False
            failing = FailingDatabase(database, lambda name, n: True)
            engine = runs.RunEngine(failing, registry.Registry())
            engine.start(Trouble.name, {})
            wait_ended(engine, 10)
            supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                           auto_recover=False)
            supervisor.check()
            alarm = any(a["key"] == "storage"
                        for a in supervisor.describe()["recent"])
            # The run stays open in the real database -- every write to it
            # failed. Closed here, or the next engine takes it for a crash.
            for left in database.orphaned_runs():
                database.finish_run(left["id"], dbmod.FAILED,
                                    {"error": "selftest: left open on purpose"})
            return (Trouble.finished and alarm,
                    ("clean-up ran" if Trouble.finished else "clean-up skipped")
                    + ("; alarm raised" if alarm else "; no alarm"))
        attempt("with every write failing, the run's clean-up still runs and an "
                "alarm says so", writes_all_fail)
    finally:
        del runs.CATALOGUE[Trouble.name]

    # --- the panel stopped under a run ------------------------------------------
    def sigterm_during_bakeout():
        import json as jsonmod
        import signal
        import socket
        import sqlite3
        import subprocess
        import urllib.request

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        path = os.path.join(directory, "sigterm.db")
        server = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, "webserver.py"), "--simulate",
             "--no-auth", "--db", path, "--port", str(port), "--bind",
             "127.0.0.1"], cwd=ROOT, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"

        def call(route, body=None):
            request = urllib.request.Request(
                base + route, method="POST" if body is not None else "GET",
                data=jsonmod.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=5) as response:
                return jsonmod.loads(response.read())

        code = None
        try:
            up = wait_until(lambda: _try(lambda: call("/api/state")["devices"]
                                         ["pump"]["connected"]), 30, 0.3)
            started = _try(lambda: call("/api/run/start",
                                        {"procedure": "bakeout", "config": {}}))
            heating = wait_until(lambda: _try(lambda: call("/api/state")
                                              ["devices"]["pump"]["status"]
                                              .get("Heating") is True), 20, 0.3)
            server.send_signal(signal.SIGTERM)
            code = server.wait(timeout=30)
        finally:
            if server.poll() is None:
                server.kill()
                server.wait()
        with sqlite3.connect(path) as connection:
            row = connection.execute(
                "SELECT id, status FROM runs ORDER BY id DESC LIMIT 1").fetchone()
            events = [r[0] for r in connection.execute(
                "SELECT message FROM events WHERE run_id=?", (row[0],))] \
                if row else []
        off = any("Heating switched off" in m for m in events)
        with sqlite3.connect(path) as connection:
            alarmed = any(f"Run {row[0]} " in r[0] for r in connection.execute(
                "SELECT message FROM events WHERE message LIKE 'ALARM:%'")) \
                if row else False
        later = dbmod.Database(path)
        try:
            reported = any(o.get("kind") == "restart" and row
                           and o["id"] == row[0]
                           for o in runs.RunEngine(later,
                                                   registry.Registry()).orphans)
        finally:
            later.close()
        return (bool(up and started and started.get("ok") and heating)
                and code == 0 and row and row[1] == "aborted" and off
                and reported and alarmed,
                f"exit {code}, run {row[1] if row else '?'}, heater "
                + ("switched off" if off else "never switched off")
                + ("; reported at the next start" if reported
                   else "; not reported at the next start")
                + ("" if alarmed else "; the supervisor's record of it lost"))
    attempt("stopping the panel during a bake-out ends the run with its heater "
            "off", sigterm_during_bakeout)

    def slow_finish():
        class Lingering(runs.Procedure):
            name = "selftest_lingering"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return len(engine._samples) >= 2

            def finish(self, engine):
                time.sleep(7.0)                  # a clean-up against a slow drive

        runs.CATALOGUE[Lingering.name] = Lingering
        try:
            engine = runs.RunEngine(database, registry.Registry())
            supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                           auto_recover=False)
            started = engine.start(Lingering.name, {})
            end = time.monotonic() + 10
            while time.monotonic() < end and engine.status()["running"]:
                supervisor.check()
                time.sleep(0.2)
            stalled = any(a["key"] == "engine_stalled"
                          for a in supervisor.describe()["recent"])
        finally:
            del runs.CATALOGUE[Lingering.name]
        if not started.get("ok"):
            return False, f"the run did not start: {started.get('error')}"
        return not stalled, ("a false stall alarm during the clean-up" if stalled
                             else "no alarm while the run cleaned up")
    attempt("a run's slow clean-up is not reported as the run stalling",
            slow_finish)

    def download_during_a_run():
        fake_tc600.reset()
        fake_tpg.reset()
        with Rig(pump_port, gauge_port, interval=None) as rig:
            engine = runs.RunEngine(database, rig.devices)
            engine.start("hold", {"duration_min": 1, "interval_s": 1})
            time.sleep(2.0)

            def holding():                       # what a download's VACUUM does
                with database._lock:
                    time.sleep(5.0)

            threading.Thread(target=holding, daemon=True).start()
            worst = 0.0
            end = time.monotonic() + 5.0
            while time.monotonic() < end:
                worst = max(worst, engine.tick_age() or 0.0)
                time.sleep(0.1)
            engine.stop("selftest")
            wait_ended(engine, 15)
        return worst < 2.0, f"the safety tick was {worst:.1f} s late at worst"
    attempt("a database download during a run does not hold up the safety tick",
            download_during_a_run)

    def frozen_reading_is_silence():
        fake_tc600.reset()
        fake_tpg.reset()
        saved = runs.RunEngine.MAX_SILENT_S
        runs.RunEngine.MAX_SILENT_S = 3.0
        try:
            with Rig(pump_port, gauge_port, interval=None) as rig:
                engine = runs.RunEngine(database, rig.devices)
                engine.start("hold", {"duration_min": 5, "interval_s": 1})
                time.sleep(2.0)
                rig.pump._stop.set()             # held up: still "connected"
                rig.pump._thread.join(timeout=5)
                took = wait_ended(engine, 20)
                message = (engine.status().get("last") or {}).get("message", "")
                if took is None:
                    engine.stop("selftest")
                    wait_ended(engine, 10)
        finally:
            runs.RunEngine.MAX_SILENT_S = saved
        return (took is not None and "lost contact" in message,
                f"ended after {took:.1f} s: {message[:40]}" if took is not None
                else "still running on a reading 20 s old")
    attempt("a reading that has stopped being renewed counts as silence",
            frozen_reading_is_silence)

    def ceiling_without_speed():
        recipe = runs.Deposition({})
        recipe.begin(_QuietEngine())
        recipe.phase = "deposit"
        return (recipe.current_pressure_ceiling({"speed": None}) == 5e-2,
                f"ceiling {recipe.current_pressure_ceiling({'speed': None})}")
    attempt("a lost speed reading does not lift the deposition's pressure "
            "ceiling", ceiling_without_speed)

    def owed_cleanup_survives():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True                # the heater, left on
        cut_off = database.create_run("bakeout", "Cut off twice", {})
        saved = getattr(runs.RunEngine, "ORPHAN_WAIT_S", None)
        runs.RunEngine.ORPHAN_WAIT_S = 2.0       # (a deadline only older code had)
        try:
            fake_tc600.FAULTS["silent"] = True    # the pump not answering yet
            with Rig(pump_port) as rig:
                runs.RunEngine(database, rig.devices)
                time.sleep(4.0)                   # ... and the panel stops again
            fake_tc600.FAULTS.clear()             # restarted again, pump back
            with Rig(pump_port) as rig:
                second = runs.RunEngine(database, rig.devices)
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
                again = any(o["id"] == cut_off["id"] for o in second.orphans)
        finally:
            if saved is None:
                del runs.RunEngine.ORPHAN_WAIT_S
            else:
                runs.RunEngine.ORPHAN_WAIT_S = saved
            fake_tc600.FAULTS.clear()
        return (again and off, ("found again" if again else "forgotten")
                + (", heater switched off" if off else ", heater left on"))
    attempt("a crashed run's clean-up is still owed after a second restart",
            owed_cleanup_survives)

    def owed(run_id):
        return bool((database.run(run_id).get("result") or {})
                    .get("cleanup_pending"))

    def orphans_retried(**faults):
        """Run the crash clean-up with ORPHAN_TRIES 2 and short waits."""
        names = ("ORPHAN_TRIES", "ORPHAN_RETRY_S", "ORPHAN_POLL_S",
                 "ORPHAN_WAIT_S")
        saved = {n: getattr(runs.RunEngine, n, None) for n in names}
        runs.RunEngine.ORPHAN_TRIES, runs.RunEngine.ORPHAN_RETRY_S = 2, 0.5
        runs.RunEngine.ORPHAN_POLL_S = 0.2
        runs.RunEngine.ORPHAN_WAIT_S = 2.0       # (a deadline only older code had)
        return saved

    def restore(saved):
        for name, value in saved.items():
            if value is None:
                if hasattr(runs.RunEngine, name):
                    delattr(runs.RunEngine, name)
            else:
                setattr(runs.RunEngine, name, value)

    def two_orphans():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True
        first = database.create_run("bakeout", "Cut off, A", {})
        second = database.create_run("bakeout", "Cut off, B", {})
        saved = orphans_retried()
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 15)
                engine._flush()
        finally:
            restore(saved)
        still = [run_id for run_id in (first["id"], second["id"]) if owed(run_id)]
        logged = {run_id: [e["message"] for e in database.events(run_id)
                           if e["message"].startswith("After the restart")]
                  for run_id in (first["id"], second["id"])}
        good = all(len(messages) == 1 and "was run" in messages[0]
                   for messages in logged.values())
        return (good and not still,
                f"owed still: {still}; records: {logged}")
    attempt("two runs cut off at once are each cleaned up and recorded under "
            "their own id", two_orphans)

    def failed_cleanup_owed():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True
        fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}  # heater-off refused
        cut_off = database.create_run("bakeout", "Heater stuck on", {})
        saved = orphans_retried()
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 15)
                engine._flush()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
        kept = owed(cut_off["id"])
        database.set_result(cut_off["id"], {"cleanup_pending": False})
        return kept, ("still owed" if kept else "marked done though the heater "
                      "never went off")
    attempt("a clean-up that fails leaves the run owed", failed_cleanup_owed)

    def failure_alerts_again():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True
        fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}
        cut_off = database.create_run("bakeout", "Heater stuck, alarmed", {})
        saved = orphans_retried()
        runs.RunEngine.ORPHAN_RETRY_S = 2.0
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                supervisor.check()
                supervisor.acknowledge("all")    # somebody saw the cut-off one
                wait_until(lambda: (supervisor.check() or True) and not (
                    engine._orphan_thread and engine._orphan_thread.is_alive()),
                    20, 0.1)
                supervisor.check()
                fresh = [a for a in supervisor.describe()["alarms"]
                         if a["key"] == f"run_{cut_off['id']}_cleanup_failed"]
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            database.set_result(cut_off["id"], {"cleanup_pending": False})
        return (bool(fresh), fresh[0]["title"] if fresh else
                "the failure only edited an alarm already acknowledged")
    attempt("a clean-up that fails later is heard again, as a new alarm",
            failure_alerts_again)

    def stalled_cleanup():
        class Hanging(runs.Procedure):
            name = "selftest_hanging"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return True

            def finish(self, engine):
                time.sleep(4.0)                  # the heater-off that hangs

        saved = getattr(alarms.Supervisor, "CLEANUP_STALL_S", None)
        alarms.Supervisor.CLEANUP_STALL_S = 2.0
        runs.CATALOGUE[Hanging.name] = Hanging
        try:
            engine = runs.RunEngine(database, registry.Registry())
            supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                           auto_recover=False)
            engine.start(Hanging.name, {})
            raised = wait_until(lambda: (supervisor.check() or True) and any(
                a["key"] == "cleanup_stalled" and a["active"]
                for a in supervisor.describe()["recent"]), 8, 0.2)
            wait_ended(engine, 10)
            supervisor.check()
            cleared = not any(a["key"] == "cleanup_stalled" and a["active"]
                              for a in supervisor.describe()["recent"])
        finally:
            del runs.CATALOGUE[Hanging.name]
            if saved is None:
                del alarms.Supervisor.CLEANUP_STALL_S
            else:
                alarms.Supervisor.CLEANUP_STALL_S = saved
        return raised and cleared, (("raised" if raised else "never raised")
                                    + (", then cleared" if cleared
                                       else ", never cleared"))
    attempt("a clean-up that hangs raises an alarm, cleared when it ends",
            stalled_cleanup)

    def slow_summary():
        class Slow(runs.Procedure):
            name = "selftest_slow_summary"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return True

            def summarise(self, engine, samples):
                time.sleep(4.0)                  # only the record is slow
                return {}

        saved = getattr(alarms.Supervisor, "CLEANUP_STALL_S", None)
        alarms.Supervisor.CLEANUP_STALL_S = 2.0
        runs.CATALOGUE[Slow.name] = Slow
        try:
            engine = runs.RunEngine(database, registry.Registry())
            supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                           auto_recover=False)
            engine.start(Slow.name, {})
            end = time.monotonic() + 8
            while time.monotonic() < end and engine.status()["running"]:
                supervisor.check()
                time.sleep(0.2)
            false_alarm = any(a["key"] == "cleanup_stalled"
                              for a in supervisor.describe()["recent"])
        finally:
            del runs.CATALOGUE[Slow.name]
            if saved is None:
                del alarms.Supervisor.CLEANUP_STALL_S
            else:
                alarms.Supervisor.CLEANUP_STALL_S = saved
        return not false_alarm, ("a false alarm while only the record was slow"
                                 if false_alarm else "no alarm")
    attempt("a slow record of the run is not taken for a stalled clean-up",
            slow_summary)

    def settle(run_id):
        """Mark a run's clean-up done, so the next engine does not redo it."""
        record = database.run(run_id) if run_id is not None else None
        if record:
            database.set_result(run_id, {**(record.get("result") or {}),
                                         "cleanup_pending": False})

    def live_cleanup_failed():
        fake_tc600.reset()
        run_id = None
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}  # off refused
                engine.stop("selftest")
                wait_ended(engine, 15)
                engine._flush()
                supervisor.check()
                found = [a for a in supervisor.describe()["alarms"]
                         if a["key"].endswith("_cleanup_failed")]
                engine.close()                    # and its retry with it
        finally:
            fake_tc600.FAULTS.clear()
            settle(run_id)
        return (heating and bool(found),
                found[0]["title"] if found else "the heater stayed on and "
                "nothing said so")
    attempt("a run stopped by the operator whose heater will not go off raises "
            "an alarm", live_cleanup_failed)

    def owed_after_close():
        """A bake-out whose heater-off is refused as the panel closes, and then
        the next start."""
        fake_tc600.reset()
        saved = orphans_retried()
        run_id, found, said, off = None, [], [], False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}  # off refused
                engine.close(timeout=15)
                engine._flush()
            left_on = fake_tc600.STATE[1] is True
            fake_tc600.FAULTS.clear()            # the next start, pump fine
            with Rig(pump_port) as rig:
                later = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, later, database,
                                               auto_recover=False)
                found = [o for o in later.orphans if o["id"] == run_id]
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
                wait_until(lambda: not (later._orphan_thread
                                        and later._orphan_thread.is_alive()), 15)
                later._flush()
                supervisor.check()
                said = [a for a in supervisor.describe()["recent"]
                        if a["key"] == f"run_{run_id}_cut_off"]
            settled = run_id is not None and not owed(run_id)
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(run_id)
        kind = found[0]["kind"] if found else None
        title = said[-1]["title"] if said else ""
        return (heating and left_on and kind == "unfinished" and off and settled
                and "not made safe" in title,
                f"heater {'left on' if left_on else 'off'} as it ended; next "
                f"start: {kind or 'not owed'}"
                + (", heater switched off" if off else ", heater left on")
                + ("" if settled else ", still owed")
                + (f"; alarm: {title}" if said else "; no alarm"))
    attempt("a run whose heater would not go off as the panel closed is made "
            "safe at the next start, not reported as cleaned up",
            owed_after_close)

    def retried_once_pump_answers():
        """The likeliest real case: the pump's link drops during a bake-out,
        and the heater-off cannot be sent as the run ends."""
        fake_tc600.reset()
        saved = orphans_retried()
        run_id = None
        heating = quiet = raised = still_on = off = settled = cleared = False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                fake_tc600.FAULTS["silent"] = True        # the link drops
                quiet = wait_until(lambda: not rig.pump.snapshot()
                                   .get("connected"), 10, 0.1)
                engine.stop("selftest")
                wait_ended(engine, 25)
                engine._flush()
                supervisor.check()
                key = f"run_{run_id}_cleanup_failed"
                raised = any(a["key"] == key and a["active"]
                             for a in supervisor.describe()["recent"])
                time.sleep(1.0)
                still_on = fake_tc600.STATE[1] is True
                fake_tc600.FAULTS.clear()                 # it answers again
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 20, 0.1)
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 20)
                engine._flush()
                supervisor.check()
                cleared = not any(a["key"] == key and a["active"]
                                  for a in supervisor.describe()["recent"])
                settled = not owed(run_id)
                engine.close()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(run_id)
        return (heating and quiet and raised and still_on and off and settled
                and cleared,
                ("alarm raised" if raised else "no alarm")
                + ("; heater on while the pump was quiet" if still_on
                   else "; heater state changed while the pump was quiet")
                + ("; switched off once it answered" if off
                   else "; left on after it answered")
                + ("" if settled else "; still owed")
                + ("" if cleared else "; alarm not cleared"))
    attempt("a heater left on because the pump went quiet as a bake-out ended is "
            "switched off once it answers, with the panel running",
            retried_once_pump_answers)

    def no_run_while_retrying():
        fake_tc600.reset()
        saved = orphans_retried()
        runs.RunEngine.ORPHAN_RETRY_S = 1.5
        run_id, held, after, off = None, {}, {}, False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}  # off refused
                engine.stop("selftest")
                wait_ended(engine, 15)
                held = engine.start("hold", {"duration_min": 1, "interval_s": 1})
                if held.get("ok"):
                    engine.stop("selftest")
                    wait_ended(engine, 15)
                fake_tc600.FAULTS.clear()                 # the drive takes it
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 15)
                after = engine.start("hold", {"duration_min": 1, "interval_s": 1})
                if after.get("ok"):
                    engine.stop("selftest")
                    wait_ended(engine, 15)
                engine.close()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(run_id)
        refused = not held.get("ok") and "clean-up" in (held.get("error") or "")
        return (heating and refused and off and bool(after.get("ok")),
                ("refused meanwhile" if refused else
                 f"meanwhile: {held.get('error') or 'a run started'}")
                + ("; heater off on a later try" if off else "; heater left on")
                + ("; a run starts after" if after.get("ok") else
                   f"; after: {after.get('error')}"))
    attempt("no run starts while a failed clean-up is tried again, and one can "
            "once it has worked", no_run_while_retrying)

    def owing_listed():
        store = dbmod.Database(os.path.join(directory, "owing.db"))
        try:
            expected = {}
            for status, result in (
                    (dbmod.COMPLETED, {"cleanup_pending": True}),
                    (dbmod.ABORTED, {"cleanup_pending": True}),
                    (dbmod.FAILED, {"cleanup_pending": True, "cut_off": True}),
                    (dbmod.ABORTED, {"cleanup_pending": False}),
                    (dbmod.COMPLETED, {"message": 'said "cleanup_pending": true'})):
                made = store.create_run("bakeout", f"{status}", {})
                store.finish_run(made["id"], status, result)
                expected[made["id"]] = result.get("cleanup_pending") is True
            going = store.create_run("bakeout", "still going", {})
            store.set_result(going["id"], {"cleanup_pending": True})
            expected[going["id"]] = False
            found = {run["id"] for run in store.runs_owing_cleanup()}
        finally:
            store.close()
        want = {run_id for run_id, owes in expected.items() if owes}
        return found == want, f"listed {sorted(found)}, owed {sorted(want)}"
    attempt("every ended run that owes its clean-up is listed, whatever its "
            "status; nothing else is", owing_listed)

    def owed_retry_gives_up():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True
        fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}   # it stays on
        ended = database.create_run("bakeout", "Ended, heater stuck", {})
        database.finish_run(ended["id"], dbmod.ABORTED, {"cleanup_pending": True})
        saved = orphans_retried()
        started = {}
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                gave_up = wait_until(lambda: (supervisor.check() or True) and not (
                    engine._orphan_thread and engine._orphan_thread.is_alive()),
                    20, 0.1)
                supervisor.check()
                alarmed = any(a["key"] == f"run_{ended['id']}_cleanup_failed"
                              for a in supervisor.describe()["recent"])
                kept = owed(ended["id"])
                started = engine.start("hold", {"duration_min": 1,
                                                "interval_s": 1})
                if started.get("ok"):
                    engine.stop("selftest")
                    wait_ended(engine, 15)
                engine.close()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(ended["id"])
        return (gave_up and kept and alarmed and bool(started.get("ok")),
                ("still owed" if kept else "no longer owed")
                + ("; alarm raised" if alarmed else "; no alarm")
                + ("; runs can start" if started.get("ok") else
                   f"; start refused: {started.get('error')}"))
    attempt("an ended run whose clean-up keeps failing stays owed, with an "
            "alarm, and does not block runs for ever", owed_retry_gives_up)

    class FailsOnce:
        """A flow controller that does not answer the first time."""
        def __init__(self):
            self.left, self.value = 1, 20.0

        def set(self, value):
            if self.left:
                self.left -= 1
                raise RuntimeError("no answer from the flow controller")
            self.value = value

        def read(self):
            return self.value

    def stuck_gas():
        """Simulated process hardware whose gas fails to close once."""
        from tools import fake_actuators
        hardware = process.ProcessHardware()
        fake_actuators.attach(hardware, fake_tpg.Chamber())
        gas = FailsOnce()
        hardware.attach("gas", gas)
        return hardware, gas

    def deposition_replayed():
        hardware, gas = stuck_gas()
        ended = database.create_run("deposition", "Gas would not close", {})
        database.finish_run(ended["id"], dbmod.ABORTED, {"cleanup_pending": True})
        saved = orphans_retried()
        kind, settled, said = None, False, []
        try:
            engine = runs.RunEngine(database, registry.Registry(),
                                    hardware=hardware)
            wait_until(lambda: not (engine._orphan_thread
                                    and engine._orphan_thread.is_alive()), 15)
            engine._flush()
            kind = next((o["kind"] for o in engine.orphans
                         if o["id"] == ended["id"]), None)
            settled = not owed(ended["id"])
            said = [e["message"] for e in database.events(ended["id"])
                    if e["message"].startswith("After the restart")]
            engine.close()
        finally:
            restore(saved)
            settle(ended["id"])
        return (kind == "unfinished" and gas.value == 0 and settled
                and any("was run" in message for message in said),
                f"{kind or 'not owed'}; gas at {gas.value}; "
                + ("settled" if settled else "still owed") + f"; {said}")
    attempt("a deposition whose gas would not close is closed on a later try "
            "and settled", deposition_replayed)

    def deposition_retried_live():
        hardware, gas = stuck_gas()
        saved = orphans_retried()
        settled, said = False, []
        ended = None
        try:
            engine = runs.RunEngine(database, registry.Registry(),
                                    hardware=hardware)
            # As a run's ending does when its clean-up fails, the panel running.
            ended = database.create_run("deposition", "Gas would not close, "
                                        "panel running", {})
            database.finish_run(ended["id"], dbmod.ABORTED,
                                {"cleanup_pending": True})
            engine._remember(ended, "trying again", None, "retry")
            engine._start_cleanups()
            wait_until(lambda: not (engine._orphan_thread
                                    and engine._orphan_thread.is_alive()), 15)
            engine._flush()
            settled = not owed(ended["id"])
            said = [e["message"] for e in database.events(ended["id"])
                    if e["message"].startswith("Clean-up tried again")]
            engine.close()
        finally:
            restore(saved)
            if ended:
                settle(ended["id"])
        return (gas.value == 0 and settled
                and any("was run" in message for message in said),
                f"gas at {gas.value}; " + ("settled" if settled else "still owed")
                + f"; {said}")
    attempt("so is one retried while the panel runs", deposition_retried_live)

    def retry_gives_up_live():
        fake_tc600.reset()
        saved = orphans_retried()
        run_id, started = None, {}
        heating = gave_up = alarmed = kept = False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}  # for good
                engine.stop("selftest")
                wait_ended(engine, 15)
                gave_up = wait_until(lambda: (supervisor.check() or True) and not (
                    engine._orphan_thread and engine._orphan_thread.is_alive()),
                    20, 0.1)
                supervisor.check()
                alarmed = any(a["key"] == f"run_{run_id}_retry_failed"
                              and a["active"]
                              for a in supervisor.describe()["recent"])
                kept = owed(run_id)
                started = engine.start("hold", {"duration_min": 1,
                                                "interval_s": 1})
                if started.get("ok"):
                    engine.stop("selftest")
                    wait_ended(engine, 15)
                engine.close()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(run_id)
        return (heating and gave_up and alarmed and kept
                and bool(started.get("ok")),
                ("a new alarm once the tries ran out" if alarmed
                 else "no alarm when the tries ran out")
                + ("; still owed to the next start" if kept else "; not owed")
                + ("; runs can start again" if started.get("ok") else
                   f"; start refused: {started.get('error')}"))
    attempt("a retry that keeps failing while the panel runs gives up with a "
            "new alarm, still owed, and runs can start again",
            retry_gives_up_live)

    def retry_cleared_after_a_later_run():
        fake_tc600.reset()
        saved = orphans_retried()
        runs.RunEngine.ORPHAN_RETRY_S = 1.0
        run_id, quick, cleared = None, {}, False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                wait_until(lambda: fake_tc600.STATE[1] is True, 10, 0.1)
                fake_tc600.FAULTS["refuse_write"] = {1: "_LOGIC"}
                engine.stop("selftest")
                wait_ended(engine, 15)
                supervisor.check()                   # its alarm is raised
                fake_tc600.FAULTS.clear()            # the next try works ...
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 15)
                # ... and another run starts and ends before the supervisor's
                # next look.
                quick = engine.start("hold", {"duration_min": 1, "interval_s": 1})
                if quick.get("ok"):
                    engine.stop("selftest")
                    wait_ended(engine, 15)
                supervisor.check()
                cleared = not any(a["key"] == f"run_{run_id}_cleanup_failed"
                                  and a["active"]
                                  for a in supervisor.describe()["recent"])
                engine.close()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
            settle(run_id)
        return (bool(quick.get("ok")) and cleared,
                "cleared" if cleared else "left open after a later run")
    attempt("a clean-up that worked on a later try clears its alarm, even after "
            "another run has ended", retry_cleared_after_a_later_run)

    def run_ids_never_reused():
        path = os.path.join(directory, "ids.db")
        store = dbmod.Database(path)

        def ended(name):
            made = store.create_run("hold", name, {})
            store.finish_run(made["id"], dbmod.COMPLETED, {})
            return made

        try:
            first = ended("first")
            store.delete_run(first["id"])        # the newest, deleted
            second = ended("second")
            store.delete_run(second["id"])
        finally:
            store.close()
        store = dbmod.Database(path)             # and after a restart
        try:
            third = store.create_run("hold", "third", {})
        finally:
            store.close()
        given = [first["id"], second["id"], third["id"]]
        return given == sorted(set(given)), f"ids given: {given}"
    attempt("a deleted run's id is never given to another run", run_ids_never_reused)

    def meta_added_to_old_store():
        import sqlite3
        path = os.path.join(directory, "old-store.db")
        store = dbmod.Database(path)
        made = [store.create_run("hold", f"old {n}", {})["id"] for n in range(3)]
        for run_id in made:
            store.finish_run(run_id, dbmod.COMPLETED, {})
        store.close()
        with sqlite3.connect(path) as raw:       # as a store from before it
            raw.execute("DROP TABLE IF EXISTS meta")
        store = dbmod.Database(path)             # upgraded on open
        try:
            store.delete_run(made[-1])           # then its newest run deleted
            fresh = store.create_run("hold", "after the upgrade", {})["id"]
        finally:
            store.close()
        return fresh > made[-1], f"runs {made}, then {fresh}"
    attempt("an older store keeps its ids unique from its first open on",
            meta_added_to_old_store)

    def alarms_after_newest_deleted():
        class StaysOn(runs.Procedure):
            name = "selftest_stays_on"
            phases = ("running", "done")
            interval = 0.1

            def step(self, engine, sample):
                return True

            def finish(self, engine):
                return False                     # a heater that will not go off

        runs.CATALOGUE[StaysOn.name] = StaysOn
        saved = orphans_retried()
        store = dbmod.Database(os.path.join(directory, "newest-deleted.db"))
        first = second = None
        fresh = []
        try:
            engine = runs.RunEngine(store, registry.Registry())
            supervisor = alarms.Supervisor(registry.Registry(), engine, store,
                                           auto_recover=False)

            def run_and_retry():
                run_id = engine.start(StaysOn.name, {})["run"]["id"]
                wait_until(lambda: not engine.status()["running"], 10, 0.02)
                wait_until(lambda: (supervisor.check() or True) and not (
                    engine._orphan_thread and engine._orphan_thread.is_alive()),
                    15, 0.05)
                supervisor.check()
                return run_id

            first = run_and_retry()
            supervisor.acknowledge("all")        # somebody checked by hand ...
            engine._flush()
            engine.delete_run(first)             # ... and an admin deleted it
            second = run_and_retry()             # the next one fails the same way
            fresh = [a["key"] for a in supervisor.describe()["alarms"]
                     if not a.get("acknowledged")]
            engine.close()
        finally:
            restore(saved)
            del runs.CATALOGUE[StaysOn.name]
            store.close()
        heard = f"run_{second}_cleanup_failed" in fresh
        return (second != first and heard,
                f"runs {first} and {second}; unacknowledged: {fresh}")
    attempt("after the newest run is deleted, the next run's failure is heard as "
            "a new alarm", alarms_after_newest_deleted)

    def forget_run_vs_start():
        class Plain(runs.Procedure):
            name = "selftest_plain_named"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

        runs.CATALOGUE[Plain.name] = Plain
        store = dbmod.Database(os.path.join(directory, "forget.db"))
        engine = runs.RunEngine(store, registry.Registry())
        named, second = False, {}
        try:
            first = engine.start(Plain.name, {})["run"]
            time.sleep(0.3)
            engine.stop("selftest")
            wait_ended(engine, 10)
            engine._flush()
            checked, go_on = threading.Event(), threading.Event()

            class Paused(dict):
                """Pauses forget_run inside its check, where a thread switch
                could put it."""
                def get(self, key, default=None):
                    value = super().get(key, default)
                    if key == "id" and not go_on.is_set():
                        checked.set()
                        go_on.wait(5)
                    return value

            engine._run = Paused(engine._run)
            deleter = threading.Thread(target=engine.delete_run,
                                       args=(first["id"],), daemon=True)
            deleter.start()
            checked.wait(5)
            answers = []
            starter = threading.Thread(target=lambda: answers.append(
                engine.start(Plain.name, {})), daemon=True)
            starter.start()                      # another screen starts a run
            time.sleep(0.4)
            go_on.set()
            deleter.join(5)
            starter.join(5)
            time.sleep(0.3)
            second = (answers[0] if answers else {}).get("run") or {}
            named = (checked.is_set() and engine.status()["running"]
                     and engine.run_id() == second.get("id"))
        finally:
            if engine._thread and engine._thread.is_alive():
                engine.stop("selftest")
                engine._thread.join(10)
            del runs.CATALOGUE[Plain.name]
            store.close()
        return named, ("the run started meanwhile keeps its name" if named
                       else "the run started meanwhile lost its name")
    attempt("forgetting a deleted run never takes the name of one starting",
            forget_run_vs_start)

    def make_safe_outlives_its_run():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        before = after = None
        kept = []
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            engine.start("hold", {"duration_min": 5, "interval_s": 1})
            run_id = engine.run_id()
            wait_until(lambda: engine.status()["running"], 5, 0.05)
            held = rig.pump._secure_lock.acquire(timeout=5)   # so it waits
            try:
                fake_tc600.STATE[303] = "Err006"     # the run ends on a fault
                wait_until(lambda: not engine.status()["running"], 15, 0.1)
                engine._flush()
                before = (engine.storage_error or {}).get("count", 0)
                engine.delete_run(run_id)            # deleted as it is made safe
            finally:
                if held:
                    rig.pump._secure_lock.release()
            securing = engine._secure_thread
            wait_until(lambda: not (securing and securing.is_alive()), 40)
            engine._flush()
            after = (engine.storage_error or {}).get("count", 0)
            kept = [e for e in database.events(limit=50)
                    if e["message"].startswith("Pump made safe after the run")
                    and e.get("run_id") is None
                    and (e.get("detail") or {}).get("deleted_run") == run_id]
            engine.close()
        fake_tc600.reset()
        return (after == before and bool(kept),
                ("its record kept in the rig's log" if kept else "its record lost")
                + ("" if after == before else "; database writes failed"))
    attempt("a make-safe that finishes after its run was deleted keeps its record",
            make_safe_outlives_its_run)

    def finish_replays_are_safe():
        asked = []

        class Driver:
            def __init__(self, key):
                self.key = key

            def set(self, value):
                asked.append((self.key, value))

            def read(self):
                return None

        class Recorder:
            """An engine that only notes what a clean-up asks of it."""
            def __init__(self):
                self.hardware = process.ProcessHardware()
                for key in ("gas", "gate", "magnetron", "shutter"):
                    self.hardware.attach(key, Driver(key))

            def pump_param(self, name, value):
                asked.append(("pump " + name, value))
                return {"ok": True, "result": value}

            def pump_command(self, action, value=None):
                asked.append(("pump command " + action, value))
                return {"ok": True}

            def log(self, *args, **kwargs):
                pass

            def stop_choice(self):
                return None

            def __getattr__(self, name):
                raise AttributeError(f"the clean-up used engine.{name}")

        #: All a clean-up may ask for. Not "anything 0 or off": to the drive
        #: VentMode 0 is controlled venting and RUTimeCtrl off disables the
        #: run-up monitoring -- and the station and the motor are never its.
        SAFE = {("pump Heating", False), ("gas", 0), ("magnetron", 0),
                ("shutter", False)}

        def judge(procedures):
            unsafe, broken = [], []
            for name, procedure in sorted(procedures.items()):
                del asked[:]
                try:
                    procedure({}).finish(Recorder())
                except Exception as exc:                 # noqa: BLE001
                    broken.append(f"{name}: {exc}")
                unsafe += [f"{name}: {what}={value}" for what, value in asked
                           if (what, value) not in SAFE]
            return unsafe, broken

        class Careless(runs.Bakeout):
            """What the check must catch: protections switched 'off'."""
            def finish(self, engine):
                engine.pump_param("RUTimeCtrl", False)
                engine.pump_param("VentMode", 0)
                return super().finish(engine)

        caught, _ = judge({"careless": Careless})
        unsafe, broken = judge(runs.CATALOGUE)
        return (bool(caught) and not unsafe and not broken,
                "; ".join(unsafe + broken)
                or ("every replay only switches off or closes" if caught
                    else "the check let a clean-up disable the drive's "
                         "protections"))
    attempt("every procedure's clean-up, run again on a fresh instance, only "
            "switches things off or closes them", finish_replays_are_safe)

    def clean_stop_is_quiet():
        fake_tc600.reset()
        run_id = None
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                engine.start("bakeout", {"setpoint_c": 70})
                run_id = engine.run_id()
                heating = wait_until(lambda: fake_tc600.STATE[1] is True, 10,
                                     0.1)
                engine.stop("selftest")
                wait_ended(engine, 15)
                engine._flush()
                supervisor.check()
                noisy = [a["key"] for a in supervisor.describe()["recent"]
                         if a["key"].endswith("_cleanup_failed")]
            off = fake_tc600.STATE[1] is False
            kept = owed(run_id)
        finally:
            settle(run_id)
        return (heating and off and not noisy and not kept,
                ("heater off" if off else "heater left on")
                + (f"; alarms {noisy}" if noisy else "")
                + ("; owed to the next start" if kept else ""))
    attempt("a bake-out stopped normally raises no clean-up alarm and owes "
            "nothing", clean_stop_is_quiet)

    def one_make_safe():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            first = engine.secure_pump("selftest, first press")
            thread = getattr(engine, "_secure_thread", None)
            second = engine.secure_pump("selftest, second press")
            same = getattr(engine, "_secure_thread", None) is thread
            reason = (engine._secure or {}).get("reason")
            wait_until(lambda: not (thread and thread.is_alive()), 15)
            last = getattr(engine, "_secure_thread", None)
            wait_until(lambda: not (last and last.is_alive()), 15)
        return (bool(first.get("ok")) and not second.get("ok") and same
                and reason == "selftest, first press",
                f"second press: {second.get('error') or 'accepted'}"
                + ("" if same else "; the sequence waited on was replaced"))
    attempt("a second Make safe while the first is going is refused, not "
            "started over it", one_make_safe)

    def make_safe_race():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            real = engine.run_id

            def slow_run_id():              # a thread switch at the worst moment
                time.sleep(0.3)
                return real()
            engine.run_id = slow_run_id
            answers = []
            presses = [threading.Thread(target=lambda: answers.append(
                engine.secure_pump("selftest, two presses at once")))
                for _ in range(2)]
            for press in presses:
                press.start()
            for press in presses:
                press.join(5)
            started = sum(1 for answer in answers if answer.get("ok"))
            last = getattr(engine, "_secure_thread", None)
            wait_until(lambda: not (last and last.is_alive()), 15)
        return started == 1, f"{started} sequences started for two presses"
    attempt("two presses of Make safe at the same moment start one sequence",
            make_safe_race)

    def fault_while_securing():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        since = time.time()
        ended = asked = done = False
        engine_ok = supervisor_ok = None
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            engine.start("hold", {"duration_min": 5, "interval_s": 1})
            watching(supervisor, 1.0)
            held = rig.pump._secure_lock.acquire(timeout=5)  # one under way
            try:
                fake_tc600.STATE[303] = "Err006"       # the drive faults mid-run
                ended = wait_until(lambda: not engine.status()["running"], 15,
                                   0.1)
                asked = wait_until(lambda: (supervisor.check() or True)
                                   and supervisor.recovering is not None, 15, 0.1)
                time.sleep(1.0)
            finally:
                if held:
                    rig.pump._secure_lock.release()
            done = wait_until(lambda: (supervisor.check() or True)
                              and not (supervisor.recovering or {}).get("active")
                              and not (engine._secure or {}).get("active"), 60,
                              0.2)
            engine._flush()
            engine_ok = (engine._secure or {}).get("ok")
            supervisor_ok = (supervisor.recovering or {}).get("ok")
            engine.close()
        busy = [e["message"] for e in database.events(limit=300)
                if (e.get("at") or 0) >= since
                and "already securing" in e["message"]]
        return (ended and asked and done and engine_ok is True
                and supervisor_ok is True and not busy,
                f"the run's make-safe ok={engine_ok}, the recovery "
                f"ok={supervisor_ok}" + (f"; {busy[0][:70]}" if busy else ""))
    attempt("a fault during a run is made safe once, each record true -- "
            "neither reports the other's sequence as a failure",
            fault_while_securing)

    def recoveries_in_turn():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 303: "Err006"})
        since = time.time()
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            fake_tc600.FAULTS["delay_s"] = 0.05     # each sequence takes a while
            supervisor._recover(rig.pump, "Err006", True)
            first = supervisor._recovery_thread
            supervisor._recover(rig.pump, "Err006", True)   # while it runs
            second = supervisor._recovery_thread
            supervisor.close()
            finished = not first.is_alive() and not second.is_alive()
            wait_until(lambda: not first.is_alive() and not second.is_alive(),
                       40)
            engine._flush()
            fake_tc600.FAULTS.clear()
            engine.close()
        busy = [e["message"] for e in database.events(limit=300)
                if (e.get("at") or 0) >= since
                and "already securing" in e["message"]]
        return (finished and not busy,
                ("both finished before close returned" if finished
                 else "close returned with a recovery still going")
                + (f"; {busy[0][:70]}" if busy else ""))
    attempt("recoveries run one after another, and shutdown waits for both",
            recoveries_in_turn)

    def last_look_recovery():
        import sqlite3
        from lab import api as apimod

        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        path = os.path.join(directory, "last-look.db")
        store = dbmod.Database(path)
        supervisor = None
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(store, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, store,
                                               auto_recover=True)
                application = apimod.Application(rig.devices, store, engine,
                                                  supervisor=supervisor)
                watching(supervisor, 1.0)        # all well so far
                fake_tc600.STATE[303] = "Err006"  # the drive faults ...

                def faulted(snap):
                    return snap.get("status", {}).get("ErrorCode") == "Err006"
                rig.fresh(faulted)
                supervisor.check()               # ... seen once: not yet sure
                early = supervisor.recovering is not None
                rig.fresh(faulted)               # its second reading is in
                application.close()              # the last look confirms it
                started = supervisor.recovering is not None
                waited = started and not supervisor.recovering.get("active")
        finally:
            if supervisor is not None:
                wait_until(lambda: not (supervisor.recovering or {})
                           .get("active"), 15)
            fake_tc600.reset()
        with sqlite3.connect(path) as connection:
            recorded = [row[0] for row in connection.execute(
                "SELECT message FROM events WHERE message LIKE "
                "'Automatic recovery after fault%'")]
        return (not early and started and waited and bool(recorded),
                ("confirmed too early; " if early else "")
                + ("recovery started by the last look" if started
                   else "no recovery started")
                + ("" if waited else ", not waited for")
                + (f"; recorded: {recorded[0][:60]}" if recorded
                   else "; not recorded"))
    attempt("a fault confirmed by the last look at shutdown is made safe and "
            "recorded before the panel exits", last_look_recovery)

    def close_on_time():
        import contextlib
        import io
        from lab import api as apimod

        release = threading.Event()

        class Stuck(runs.Procedure):
            name = "selftest_stuck"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

            def finish(self, engine):
                release.wait(60)                 # a clean-up that never ends

        saved = apimod.Application.__dict__.get("CLOSE_BUDGET_S")
        apimod.Application.CLOSE_BUDGET_S = 3.0
        runs.CATALOGUE[Stuck.name] = Stuck
        path = os.path.join(directory, "budget.db")
        store = dbmod.Database(path)
        engine, took, run_id, owed_later = None, None, None, False
        try:
            engine = runs.RunEngine(store, registry.Registry())
            supervisor = alarms.Supervisor(registry.Registry(), engine, store,
                                           auto_recover=False)
            application = apimod.Application(registry.Registry(), store, engine,
                                              supervisor=supervisor)
            engine.start(Stuck.name, {})
            run_id = engine.run_id()
            wait_until(lambda: engine.status()["running"], 5, 0.05)
            started = time.monotonic()
            application.close()
            took = time.monotonic() - started
        finally:
            # The stuck clean-up is let go; its record finds the store closed.
            with contextlib.redirect_stderr(io.StringIO()):
                release.set()
                if engine is not None and engine._thread is not None:
                    engine._thread.join(20)
            # The next start finds the run it could not finish, and owes it.
            later = dbmod.Database(path)
            try:
                after = runs.RunEngine(later, registry.Registry())
                owed_later = any(o["id"] == run_id for o in after.orphans)
                wait_until(lambda: not (after._orphan_thread
                                        and after._orphan_thread.is_alive()), 10)
                after._flush()
            finally:
                later.close()
            del runs.CATALOGUE[Stuck.name]
            if saved is None:
                del apimod.Application.CLOSE_BUDGET_S
            else:
                apimod.Application.CLOSE_BUDGET_S = saved
        return (took is not None and took < 4.5 and owed_later,
                (f"closed in {took:.1f} s on a 3 s budget" if took is not None
                 else "did not close")
                + ("; the run is owed at the next start" if owed_later
                   else "; the next start did not find the run"))
    attempt("the panel's shutdown keeps to its time limit, even with a clean-up "
            "that hangs", close_on_time)

    def budget_cuts_make_safe():
        import contextlib
        import io
        from lab import api as apimod
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        saved = apimod.Application.__dict__.get("CLOSE_BUDGET_S")
        apimod.Application.CLOSE_BUDGET_S = 2.0
        store = dbmod.Database(os.path.join(directory, "budget-secure.db"))
        took = None
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(store, rig.devices)
                application = apimod.Application(rig.devices, store, engine)
                fake_tc600.FAULTS["delay_s"] = 0.2   # a sequence that takes long
                engine.secure_pump("selftest")
                started = time.monotonic()
                application.close()
                took = time.monotonic() - started
                securing = engine._secure_thread
                # Let go, it finishes against a closed store: its record fails.
                with contextlib.redirect_stderr(io.StringIO()):
                    wait_until(lambda: not (securing and securing.is_alive()),
                               40)
        finally:
            fake_tc600.reset()
            if saved is None:
                del apimod.Application.CLOSE_BUDGET_S
            else:
                apimod.Application.CLOSE_BUDGET_S = saved
        return (took is not None and took < 3.5,
                f"closed in {took:.1f} s on a 2 s budget" if took is not None
                else "did not close")
    attempt("a make-safe still going when the time runs out does not hold the "
            "shutdown past its limit", budget_cuts_make_safe)

    def late_make_safe_waited():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        trouble = threading.Event()

        class SlowTrouble(runs.Procedure):
            name = "selftest_slow_trouble"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                if trouble.is_set():
                    raise runs.Abort("selftest pump trouble", pump_trouble=True)
                return False

            def finish(self, engine):
                time.sleep(2.0)                  # outlasts close()'s timeout

        runs.CATALOGUE[SlowTrouble.name] = SlowTrouble
        waited = False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                engine.start(SlowTrouble.name, {})
                wait_until(lambda: engine.status()["running"], 5, 0.05)
                trouble.set()
                wait_until(lambda: engine._cleanup_since is not None, 10, 0.05)
                engine.close(timeout=1.0, deadline=time.monotonic() + 20)
                securing = engine._secure_thread
                waited = securing is not None and not securing.is_alive()
                if securing is not None:
                    wait_until(lambda: not securing.is_alive(), 20)
        finally:
            del runs.CATALOGUE[SlowTrouble.name]
            fake_tc600.reset()
        return waited, ("the make-safe it started as it ended was waited for"
                        if waited else "close() returned before that make-safe "
                        "had run")
    attempt("shutdown waits for the make-safe of a run that ends after its time "
            "is up", late_make_safe_waited)

    def no_start_while_closing():
        engine = runs.RunEngine(database, registry.Registry())
        engine.close()
        refused = engine.start("hold", {"duration_min": 1, "interval_s": 1})
        if refused.get("ok"):
            engine.stop("selftest")
            wait_ended(engine, 15)
        return (not refused.get("ok")
                and "shutting down" in (refused.get("error") or ""),
                refused.get("error") or "a run started on a closing panel")
    attempt("no run starts once the panel is closing", no_start_while_closing)

    def start_refused_by_second_look():
        class Plain(runs.Procedure):
            name = "selftest_plain_late"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

        runs.CATALOGUE[Plain.name] = Plain
        store = dbmod.Database(os.path.join(directory, "second-look.db"))
        engine = runs.RunEngine(store, registry.Registry())
        holding = threading.Event()

        def download():                          # longer than close() waits
            with store._lock:
                holding.set()
                time.sleep(2.0)

        answers, began, record = [], True, {}
        try:
            threading.Thread(target=download, daemon=True).start()
            holding.wait(5)
            starter = threading.Thread(target=lambda: answers.append(
                engine.start(Plain.name, {})), daemon=True)
            starter.start()
            wait_until(lambda: engine._lock.locked(), 5, 0.01)
            time.sleep(0.1)                      # start() waits on the database
            engine.close(deadline=time.monotonic() + 1.0)   # and gives up
            starter.join(10)
            time.sleep(0.3)
            began = bool(engine._thread and engine._thread.is_alive())
            record = (store.runs(limit=1) or [{}])[0]
        finally:
            if engine._thread and engine._thread.is_alive():
                engine.stop("selftest")
                engine._thread.join(10)
            del runs.CATALOGUE[Plain.name]
            store.close()
        answer = answers[0] if answers else {}
        return (not answer.get("ok") and not began
                and record.get("status") == dbmod.ABORTED,
                f"start: {answer.get('error') or 'a run began'}; its record "
                f"{record.get('status')}")
    attempt("a start that outlasts the wait at shutdown still does not begin",
            start_refused_by_second_look)

    def close_waits_past_its_first_try():
        class Plain(runs.Procedure):
            name = "selftest_plain_held"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

        runs.CATALOGUE[Plain.name] = Plain
        store = dbmod.Database(os.path.join(directory, "held-start.db"))
        engine = runs.RunEngine(store, registry.Registry())
        holding = threading.Event()

        def download():                          # past close()'s first 5 s try
            with store._lock:
                holding.set()
                time.sleep(6.0)

        answers, answered, record = [], [], {}
        try:
            threading.Thread(target=download, daemon=True).start()
            holding.wait(5)
            starter = threading.Thread(target=lambda: answers.append(
                engine.start(Plain.name, {})), daemon=True)
            starter.start()
            wait_until(lambda: engine._lock.locked(), 5, 0.01)
            time.sleep(0.1)
            engine.close(deadline=time.monotonic() + 15)
            answered = list(answers)             # what start() had said by then
            starter.join(10)
            record = (store.runs(limit=1) or [{}])[0]
        finally:
            if engine._thread and engine._thread.is_alive():
                engine.stop("selftest")
                engine._thread.join(10)
            del runs.CATALOGUE[Plain.name]
            store.close()
        refused = bool(answered) and not answered[0].get("ok")
        return (refused and record.get("status") == dbmod.ABORTED,
                ("the start was refused before close() returned" if refused else
                 "close() returned with the start still waiting")
                + f"; its record {record.get('status')}")
    attempt("shutdown waits for a start held up past its first try",
            close_waits_past_its_first_try)

    def records_outlive_their_run():
        said = threading.Event()

        class Talks(runs.Procedure):
            name = "selftest_talks_kept"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                if not said.is_set():
                    engine.log("info", "selftest: the run's own log")
                    said.set()
                return False

        runs.CATALOGUE[Talks.name] = Talks
        store = dbmod.Database(os.path.join(directory, "kept.db"))
        left, made = {}, {}
        try:
            engine = runs.RunEngine(store, registry.Registry())
            made = engine.start(Talks.name, {})["run"]
            said.wait(5)
            # During the run, the rig's records filed under it: an admin's
            # override, an alarm the supervisor raised.
            store.add_event(made["id"], "warning", "Olga: interlock overridden "
                            "for pump.motor_on: pressure_unknown",
                            {"reason": "checked the gauge by hand"},
                            user="olga")
            store.add_event(made["id"], "error", "ALARM: The turbopump was "
                            "started without this panel asking")
            engine.stop("selftest")
            wait_ended(engine, 10)
            engine._flush()
            ended = store.run(made["id"])["ended_at"]
            # And after it, filed under it between runs.
            store.add_event(made["id"], "error", "ALARM: Drive fault Err006",
                            at=ended + 9)
            engine.delete_run(made["id"])
            left = {e["message"]: e for e in store.events(limit=50)}
        finally:
            del runs.CATALOGUE[Talks.name]
            store.close()
        override = left.get("Olga: interlock overridden for pump.motor_on: "
                            "pressure_unknown") or {}
        during = left.get("ALARM: The turbopump was started without this "
                          "panel asking")
        after = left.get("ALARM: Drive fault Err006")
        own = left.get("selftest: the run's own log")
        detail = override.get("detail") or {}
        kept = bool(override) and during is not None and after is not None
        return (kept and own is None
                and detail.get("deleted_run") == made["id"]
                and detail.get("reason") == "checked the gauge by hand",
                ("the override and both alarms kept" if kept else
                 f"kept: override {bool(override)}, alarm during "
                 f"{during is not None}, alarm after {after is not None}")
                + ("; the run's own log deleted" if own is None
                   else "; the run's own log kept"))
    attempt("deleting a run keeps the overrides and alarms filed under it",
            records_outlive_their_run)

    def legacy_records_by_the_old_rule():
        import sqlite3
        path = os.path.join(directory, "legacy-events.db")
        store = dbmod.Database(path)
        made = store.create_run("hold", "from before", {})
        store.finish_run(made["id"], dbmod.COMPLETED, {})
        ended = store.run(made["id"])["ended_at"]
        store.close()
        with sqlite3.connect(path) as raw:       # a store from before `origin`
            raw.execute("ALTER TABLE events DROP COLUMN origin")
            raw.executemany(
                "INSERT INTO events (run_id, at, level, message, user) "
                "VALUES (?,?,?,?,?)",
                [(made["id"], ended - 5, "info", "old: the run's own log", None),
                 (made["id"], ended - 3, "warning", "old: an override", "olga"),
                 (made["id"], ended + 5, "error", "old: an alarm after", None)])
        store = dbmod.Database(path)             # upgraded on open
        try:
            store.delete_run(made["id"])
            left = {e["message"] for e in store.events(limit=20)}
        finally:
            store.close()
        want = {"old: an override", "old: an alarm after"}
        return (want <= left and "old: the run's own log" not in left,
                f"kept {sorted(m for m in left if m.startswith('old:'))}")
    attempt("records from before `origin` are judged by the older rule",
            legacy_records_by_the_old_rule)

    def between_runs_kept():
        class Talks(runs.Procedure):
            name = "selftest_talks"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                engine.log("info", "selftest: said during the run")
                return True

        runs.CATALOGUE[Talks.name] = Talks
        store = dbmod.Database(os.path.join(directory, "between-runs.db"))
        left = {}
        try:
            engine = runs.RunEngine(store, registry.Registry())
            run_id = engine.start(Talks.name, {})["run"]["id"]
            wait_until(lambda: not engine.status()["running"], 10, 0.05)
            # Between runs the engine files under the run that just ended --
            # a power-down asked for from the Pump page, a clean-up retried.
            engine.log("warning", "selftest: the power-down gave up; backing "
                                  "pump left running")
            engine._flush()
            outcome = engine.delete_run(run_id)
            left = {e["message"]: e for e in store.events(limit=50)}
        finally:
            del runs.CATALOGUE[Talks.name]
            store.close()
        later = left.get("selftest: the power-down gave up; backing pump left "
                         "running") or {}
        own = "selftest: said during the run" in left
        return (outcome.get("ok") and bool(later) and not own
                and (later.get("detail") or {}).get("deleted_run") == run_id,
                ("the power-down's line kept" if later else
                 "the power-down's line deleted with the run")
                + ("; the run's own line kept" if own
                   else "; the run's own line deleted"))
    attempt("what the engine logs between runs is the rig's, kept when the last "
            "run is deleted", between_runs_kept)

    def power_down_beside_a_stuck_ending():
        release = threading.Event()

        class SlowEnd(runs.Procedure):
            name = "selftest_slow_end"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

            def finish(self, engine):
                release.wait(10)                 # stuck behind a download

        runs.CATALOGUE[SlowEnd.name] = SlowEnd
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        kept, outcome, went_ahead, beside = [], {}, False, False
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                run_id = engine.start(SlowEnd.name, {})["run"]["id"]
                wait_until(lambda: engine.status()["running"], 5, 0.05)
                thread = engine._thread
                # The power-down waits a minute for the run's ending, then
                # goes ahead: that minute, shortened.
                thread.join = lambda timeout=None: threading.Thread.join(
                    thread, 0.3)
                engine.stop("selftest", shutdown=True)
                went_ahead = wait_until(lambda: (engine._shutdown or {}).get(
                    "phase") not in (None, "starting"), 10, 0.05)
                beside = thread.is_alive()       # the ending still stuck
                time.sleep(0.5)
                engine.cancel_shutdown()
                release.set()
                wait_ended(engine, 15)
                wait_until(lambda: not (engine._shutdown_thread and
                                        engine._shutdown_thread.is_alive()), 15)
                engine._flush()
                outcome = engine.delete_run(run_id)
                kept = [e["message"] for e in database.events(limit=80)
                        if e["message"].startswith("Power-down")
                        and (e.get("detail") or {}).get("deleted_run") == run_id]
        finally:
            release.set()
            del runs.CATALOGUE[SlowEnd.name]
            fake_tc600.reset()
        started = any(m.startswith("Power-down started") for m in kept)
        if not (went_ahead and beside):
            return False, ("the power-down never went ahead beside the stuck "
                           "ending, so nothing was tested")
        return (bool(outcome.get("ok")) and started,
                f"{len(kept)} power-down lines kept" if kept else
                "the power-down's lines went with the run")
    attempt("a power-down that goes ahead beside a stuck run ending is the "
            "rig's record, not the run's", power_down_beside_a_stuck_ending)

    def retry_lines_kept():
        tries = []

        class Stubborn(runs.Procedure):
            name = "selftest_stubborn"
            phases = ("running", "done")
            interval = 0.1

            def step(self, engine, sample):
                return True

            def finish(self, engine):
                tries.append(1)
                engine.log("info", f"selftest: clean-up, try {len(tries)}")
                return len(tries) > 1            # the first try fails

        runs.CATALOGUE[Stubborn.name] = Stubborn
        saved = orphans_retried()
        store = dbmod.Database(os.path.join(directory, "retry-lines.db"))
        left = set()
        try:
            engine = runs.RunEngine(store, registry.Registry())
            real_start = engine._start_cleanups

            def lingering():
                real_start()
                if threading.current_thread() is engine._thread:
                    time.sleep(0.5)              # the run thread, still alive
            engine._start_cleanups = lingering
            run_id = engine.start(Stubborn.name, {})["run"]["id"]
            wait_until(lambda: not engine.status()["running"], 10, 0.05)
            wait_until(lambda: not (engine._orphan_thread
                                    and engine._orphan_thread.is_alive()), 15)
            engine._flush()
            engine.delete_run(run_id)
            left = {e["message"] for e in store.events(limit=50)}
            engine.close()
        finally:
            restore(saved)
            del runs.CATALOGUE[Stubborn.name]
            store.close()
        retried = "selftest: clean-up, try 2" in left
        own = "selftest: clean-up, try 1" in left
        return (retried and not own,
                ("the retry's line kept" if retried
                 else "the retry's line deleted")
                + ("; the run's own try kept" if own
                   else "; the run's own try deleted"))
    attempt("a clean-up retried by its own thread is the rig's record",
            retry_lines_kept)

    def run_read_once():
        store = dbmod.Database(os.path.join(directory, "read-once.db"))
        raised = []
        try:
            engine = runs.RunEngine(store, registry.Registry())
            made = store.create_run("hold", "read once", {})
            store.finish_run(made["id"], dbmod.COMPLETED, {})

            class Vanishing(dict):
                """A delete's _forget, landing between two reads."""
                def __bool__(self):
                    engine._run = None
                    return True

            for name, call in (("log", lambda: engine.log(
                                    "info", "selftest: power-down, motor off")),
                               ("run_id", engine.run_id)):
                engine._run = Vanishing(id=made["id"])
                try:
                    call()
                except Exception as exc:                 # noqa: BLE001
                    raised.append(f"{name}: {type(exc).__name__}")
            engine._flush()
            engine.close(timeout=5)
        finally:
            store.close()
        return not raised, "; ".join(raised) or "neither raised"
    attempt("a run deleted between two reads of it breaks neither the log nor "
            "the run id", run_read_once)

    def confirmation_kept():
        class Asks(runs.Procedure):
            name = "selftest_asks"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                engine.require(engine.hardware.shutter, False)
                return False

        runs.CATALOGUE[Asks.name] = Asks
        store = dbmod.Database(os.path.join(directory, "confirmed.db"))
        left = {}
        try:
            engine = runs.RunEngine(store, registry.Registry(),
                                    hardware=process.ProcessHardware())
            engine.mode = runs.GUIDED
            run_id = engine.start(Asks.name, {})["run"]["id"]
            wait_until(lambda: engine.prompt() is not None, 10, 0.05)
            engine.acknowledge()                 # an operator closed it by hand
            engine.stop("selftest")
            wait_ended(engine, 10)
            engine._flush()
            engine.delete_run(run_id)
            left = {e["message"] for e in store.events(limit=50)}
            engine.close(timeout=5)
        finally:
            del runs.CATALOGUE[Asks.name]
            store.close()
        confirmed = any(m.startswith("Operator confirmed:") for m in left)
        asked = any(m.startswith("MANUAL STEP") for m in left)
        return (confirmed and not asked,
                ("the operator's confirmation kept" if confirmed
                 else "the operator's confirmation deleted")
                + ("; the prompt kept" if asked
                   else "; the run's own prompt deleted"))
    attempt("an operator's confirmation of a manual step outlives the run's "
            "deletion; the run's prompt does not", confirmation_kept)

    def stale_running_row():
        store = dbmod.Database(os.path.join(directory, "stale-running.db"))
        try:
            engine = runs.RunEngine(store, registry.Registry())
            # A run whose ending was never written -- a failing card.
            stale = store.create_run("hold", "never ended", {})
            outcome = engine.delete_run(stale["id"])
        finally:
            store.close()
        return (outcome.get("code") == 409
                and "never recorded" in (outcome.get("error") or ""),
                outcome.get("error") or "deleted")
    attempt("a run never recorded as ended is refused with the reason, not as "
            "'in progress'", stale_running_row)

    def migration_in_one_go():
        import sqlite3
        import subprocess
        path = os.path.join(directory, "migration-cut.db")
        store = dbmod.Database(path)
        made = store.create_run("hold", "from before", {})
        store.finish_run(made["id"], dbmod.COMPLETED, {})
        store.close()
        with sqlite3.connect(path) as raw:       # a store from before `origin`
            raw.execute("ALTER TABLE events DROP COLUMN origin")
            raw.execute("INSERT INTO events (run_id, at, level, message) "
                        "VALUES (?, 1.0, 'info', 'old: its own log')",
                        (made["id"],))
        # The panel opens it and the power goes between the column and the
        # marking: the process dies there, nothing gets to roll back.
        cut = subprocess.run([sys.executable, "-c", f"""
import os, sqlite3, sys
sys.path.insert(0, {ROOT!r})
from lab import db as dbmod
real = sqlite3.connect
class Cut:
    def __init__(self, connection):
        object.__setattr__(self, "real", connection)
    def __setattr__(self, name, value):
        setattr(self.real, name, value)
    def execute(self, sql, *args):
        if sql.startswith("UPDATE events SET origin='legacy'"):
            os._exit(3)
        return self.real.execute(sql, *args)
    def __getattr__(self, name):
        return getattr(self.real, name)
dbmod.sqlite3.connect = lambda *a, **k: Cut(real(*a, **k))
dbmod.Database({path!r})
"""], capture_output=True, timeout=30)
        store = dbmod.Database(path)             # the next start
        try:
            origins = [row[0] for row in store._db.execute(
                "SELECT origin FROM events WHERE message='old: its own log'")]
        finally:
            store.close()
        return (cut.returncode == 3 and origins == ["legacy"],
                f"died with {cut.returncode}; the old record's origin after "
                f"the cut: {origins}")
    attempt("a power cut in the middle of the migration leaves no record "
            "unmarked", migration_in_one_go)

    def migrated_store_left_alone():
        path = os.path.join(directory, "migrated.db")
        store = dbmod.Database(path)
        store.add_event(None, "info", "selftest: a rig record")
        store.close()
        store = dbmod.Database(path)             # opened again
        try:
            origins = [row[0] for row in store._db.execute(
                "SELECT origin FROM events WHERE message='selftest: a rig "
                "record'")]
        finally:
            store.close()
        return origins == [None], f"origin after reopening: {origins}"
    attempt("opening a store again does not mark new records as old",
            migrated_store_left_alone)

    def running_row_not_deleted():
        store = dbmod.Database(os.path.join(directory, "running-row.db"))
        try:
            going = store.create_run("hold", "just started", {})
            try:
                store.delete_run(going["id"])
                refused = False
            except dbmod.RunStillRunning:
                refused = True
            still = store.run(going["id"]) is not None
        finally:
            store.close()
        return (refused and still,
                "refused" if refused else "a running run was deleted")
    attempt("a run still marked running cannot be deleted from the store",
            running_row_not_deleted)

    def partial_delete_rolled_back():
        import sqlite3
        store = dbmod.Database(os.path.join(directory, "partial-delete.db"))
        made = store.create_run("hold", "half deleted", {})
        store.finish_run(made["id"], dbmod.COMPLETED, {})
        for n in range(2):
            store.add_event(made["id"], "warning", f"Olga: override {n}",
                            user="olga")

        class Breaks:
            """A connection whose second move fails, as a busy disk's might."""
            def __init__(self, real):
                self.real, self.moves = real, 0

            def execute(self, sql, *args):
                if sql.startswith("UPDATE events SET run_id=NULL"):
                    self.moves += 1
                    if self.moves == 2:
                        raise sqlite3.OperationalError("disk I/O error "
                                                       "(selftest)")
                return self.real.execute(sql, *args)

            def __getattr__(self, name):
                return getattr(self.real, name)

        real = store._db
        store._db = Breaks(real)
        try:
            try:
                store.delete_run(made["id"])
                failed = False
            except sqlite3.OperationalError:
                failed = True
        finally:
            store._db = real
        store.add_event(None, "info", "selftest: a later, unrelated write")
        moved = [e for e in store.events(limit=20)
                 if e["message"].startswith("Olga: override")
                 and e.get("run_id") is None]
        still = store.run(made["id"]) is not None
        store.close()
        return (failed and still and not moved,
                ("nothing moved" if not moved else
                 f"{len(moved)} records moved for a run still there"))
    attempt("a delete that fails part-way moves nothing", partial_delete_rolled_back)

    def stop_not_held_by_a_delete():
        finished_at = []

        class Watched(runs.Procedure):
            name = "selftest_watched"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

            def finish(self, engine):
                finished_at.append(time.monotonic())     # the gas off, say

        runs.CATALOGUE[Watched.name] = Watched
        store = dbmod.Database(os.path.join(directory, "stop-delete.db"))
        engine = runs.RunEngine(store, registry.Registry())
        old = store.create_run("hold", "tidied away", {})
        store.finish_run(old["id"], dbmod.COMPLETED, {})
        holding = threading.Event()

        def download():                          # a backup holding the store
            with store._lock:
                holding.set()
                time.sleep(3.0)

        took = None
        try:
            engine.start(Watched.name, {})
            wait_until(lambda: engine.status()["running"], 5, 0.05)
            threading.Thread(target=download, daemon=True).start()
            holding.wait(5)
            deleter = threading.Thread(target=lambda: engine.delete_run(
                old["id"]), daemon=True)
            deleter.start()                      # an admin tidies History
            time.sleep(0.2)
            pressed = time.monotonic()
            engine.stop("selftest")              # and the operator presses Stop
            wait_until(lambda: bool(finished_at), 10, 0.02)
            took = (finished_at[0] - pressed) if finished_at else None
            deleter.join(10)
            wait_ended(engine, 15)
        finally:
            del runs.CATALOGUE[Watched.name]
            store.close()
        return (took is not None and took < 1.5,
                f"the clean-up ran {took:.2f} s after Stop" if took is not None
                else "the clean-up never ran")
    attempt("a Stop is never held up by deleting another run", 
            stop_not_held_by_a_delete)

    def unknown_run_refused():
        store = dbmod.Database(os.path.join(directory, "unknown-run.db"))
        filed = []
        try:
            store.create_run("hold", "the only one", {})
            try:
                store.add_event(10 ** 6, "info", "selftest: for a run never made")
                raised = False
            except Exception:                            # noqa: BLE001
                raised = True
            filed = [e for e in store.events(limit=10) if e["message"]
                     == "selftest: for a run never made"]
        finally:
            store.close()
        return (raised and not filed,
                "refused" if raised else "filed as a deleted run")
    attempt("a record for a run never made is refused, not filed as deleted",
            unknown_run_refused)

    def close_waits_for_a_start():
        class Plain(runs.Procedure):
            name = "selftest_plain"
            phases = ("running", "done")
            interval = 0.2

            def step(self, engine, sample):
                return False

        runs.CATALOGUE[Plain.name] = Plain
        store = dbmod.Database(os.path.join(directory, "close-start.db"))
        engine = runs.RunEngine(store, registry.Registry())
        holding = threading.Event()

        def download():                          # holds the database a while
            with store._lock:
                holding.set()
                time.sleep(1.0)

        left = True
        try:
            threading.Thread(target=download, daemon=True).start()
            holding.wait(5)
            starter = threading.Thread(target=lambda: engine.start(Plain.name,
                                                                   {}),
                                       daemon=True)
            starter.start()
            wait_until(lambda: engine._lock.locked(), 5, 0.01)
            time.sleep(0.1)                      # start() waits on the database
            engine.close(deadline=time.monotonic() + 15)
            starter.join(10)
            time.sleep(0.3)
            left = bool(engine._thread and engine._thread.is_alive())
        finally:
            if engine._thread and engine._thread.is_alive():
                engine.stop("selftest")
                engine._thread.join(10)
            del runs.CATALOGUE[Plain.name]
            store.close()
        return not left, ("nothing left going after close()" if not left
                          else "a run began after close() had returned")
    attempt("a start already under way when the panel closes is stopped with it",
            close_waits_for_a_start)

    def budget_below_unit():
        from lab import api as apimod
        import re
        with open(os.path.join(ROOT, "deploy", "tc600-web.service")) as unit:
            found = re.search(r"^TimeoutStopSec=(\d+)", unit.read(), re.M)
        stop = int(found.group(1)) if found else None
        budget = getattr(apimod.Application, "CLOSE_BUDGET_S", None)
        return (stop is not None and budget is not None and stop >= budget + 10,
                f"systemd waits {stop} s, the shutdown may take {budget} s")
    attempt("systemd waits longer than the panel's own shutdown may take",
            budget_below_unit)

    def odd_download_path():
        import sqlite3
        from pathlib import Path
        folder = tempfile.mkdtemp(prefix="selftest-odd-", dir=directory)
        name = "copy#1.db"            # unencoded, "#" ends a file: address
        target = os.path.join(folder, name)
        database.snapshot(target)
        stray = sorted(set(os.listdir(folder)) - {name})
        with sqlite3.connect(Path(target).as_uri() + "?mode=ro",
                             uri=True) as connection:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        return (not stray and "runs" in tables
                and not tables & set(dbmod.ACCOUNT_TABLES),
                (f"the check opened another file too: {stray}" if stray else
                 "checked the copy itself")
                + ("" if "runs" in tables else "; the copy has no runs"))
    attempt("a download's last check opens the copy itself, whatever its name",
            odd_download_path)

    def empty_copy_refused():
        class NoCopy:
            """A connection whose backup copies nothing."""
            def __init__(self, real):
                self.real = real

            def backup(self, target):
                pass

            def __getattr__(self, name):
                return getattr(self.real, name)

        store = dbmod.Database(os.path.join(directory, "empty-copy.db"))
        # The copy is taken from a read-only connection of its own; older
        # code took it through the shared one. Both are made to copy nothing.
        real = store._db
        store._db = NoCopy(real)
        reader = getattr(store, "_read_only", None)
        if reader is not None:
            store._read_only = lambda: NoCopy(reader())
        try:
            store.snapshot(os.path.join(directory, "empty-copy-out.db"))
            refused, why = False, "an empty copy was offered as the download"
        except RuntimeError as exc:
            refused, why = True, str(exc)
        finally:
            store._db = real
            store.close()
        return refused, why
    attempt("a copy with no runs in it is refused, not served", empty_copy_refused)

    def separate_downloads():
        exports = tempfile.mkdtemp(prefix="selftest-exports-")
        start, paths = threading.Barrier(2), []

        def download():
            start.wait()                         # both at the same moment
            paths.append(export.database_snapshot(database, exports)[1])

        workers = [threading.Thread(target=download) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(30)
        import sqlite3
        leaked, modes = [], []
        for path in paths:
            with open(path, "rb") as handle:
                header = handle.read(20)
            modes.append(header[18:20])          # 2,2 = WAL; 1,1 = journal
            with sqlite3.connect(f"file:{path}?immutable=1", uri=True) as c:
                leaked += [row[0] for row in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")
                    if row[0] in dbmod.ACCOUNT_TABLES]
        plain = all(mode == b"\x01\x01" for mode in modes)
        return (len(set(paths)) == 2 and not leaked and plain,
                ("one file shared by both" if len(set(paths)) < 2 else
                 "a file each") + (f"; leaked {leaked}" if leaked else "")
                + ("" if plain else "; a copy left in WAL mode, its accounts "
                                    "only dropped in the -wal file"))
    attempt("two downloads in the same second get a file each, neither with "
            "the accounts", separate_downloads)

    def pump_back_later():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True
        fake_tc600.FAULTS["silent"] = True        # the drive comes back later
        cut_off = database.create_run("bakeout", "Pump back late", {})
        saved = orphans_retried()
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                time.sleep(14.0)                  # past any deadline and retry
                fake_tc600.FAULTS.clear()
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
                wait_until(lambda: not (engine._orphan_thread
                                        and engine._orphan_thread.is_alive()), 10)
                engine._flush()
        finally:
            restore(saved)
            fake_tc600.FAULTS.clear()
        recorded = any("was run" in e["message"]
                       for e in database.events(cut_off["id"]))
        still = owed(cut_off["id"])
        if still:
            database.set_result(cut_off["id"], {"cleanup_pending": False})
        return (off and recorded and not still,
                ("heater switched off once the pump answered" if off
                 else "heater left on until the next restart")
                + ("; recorded as done" if recorded else "; no record of it")
                + ("; still owed" if still else ""))
    attempt("a crashed run's clean-up waits for a pump that comes back late",
            pump_back_later)

    def event_order():
        engine = runs.RunEngine(database, registry.Registry())

        def holding():                            # a download's copy
            with database._lock:
                time.sleep(2.0)

        holder = threading.Thread(target=holding, daemon=True)
        holder.start()
        time.sleep(0.1)
        engine.log("info", "selftest order: first")
        time.sleep(0.5)
        later = threading.Thread(target=lambda: database.add_event(
            None, "info", "selftest order: second"), daemon=True)
        later.start()
        holder.join()
        later.join()
        engine._flush()
        at = {e["message"]: e["at"] for e in database.events(limit=100)
              if e["message"].startswith("selftest order")}
        first, second = at.get("selftest order: first"), \
            at.get("selftest order: second")
        return (first is not None and second is not None
                and first < second - 1.0,
                f"first at +{(first or 0) - (second or 0):.1f} s from second")
    attempt("an event is dated when it happened, not when it was written",
            event_order)

    def orphan_made_safe():
        fake_tc600.reset()
        fake_tc600.STATE[1] = True                # the heater, left on
        cut_off = database.create_run("bakeout", "Cut off", {"setpoint_c": 70})
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            supervisor.check()
            alarm = any(a["key"].startswith(f"run_{cut_off['id']}_cut_off")
                        for a in supervisor.describe()["recent"])
        return (off and alarm, ("heater switched off" if off else
                                "heater left on") + ("; alarm raised" if alarm
                                                      else "; no alarm"))
    attempt("a run cut off by a crash is made safe at the next start, with an "
            "alarm", orphan_made_safe)

    # --- smaller things ---------------------------------------------------------
    class Nominal(_QuietEngine):
        def nominal_speed(self):
            return 1500.0

    def dropout_at_base():
        engine = Nominal()
        recipe = runs.BaseCalibration({"base_rate_pct": 2, "base_settle_min": 0.5})
        recipe.begin(engine)
        recipe.enter("basepressure")
        outcome = None
        for t in range(0, 34, 2):                  # flat, at full speed
            outcome = recipe.phase_basepressure(
                engine, {"t": float(t), "p_best": 4.0e-6, "speed": 1500.0})
        dropped = recipe.phase_basepressure(       # the settle ends: no reading
            engine, {"t": 34.0, "p_best": None, "speed": 1500.0})
        after = recipe.phase_basepressure(
            engine, {"t": 36.0, "p_best": 4.0e-6, "speed": 1500.0})
        return (outcome is None and dropped is None and after == "done"
                and recipe.base_mbar == 4.0e-6,
                f"{dropped!r} on the dropout, {after!r} after, base "
                f"{recipe.base_mbar}")
    attempt("a gauge dropout as base pressure is reached does not crash the run",
            dropout_at_base)

    def standby_left():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tc600.STATE[2] = True                  # the pump in standby
        with Rig(pump_port, gauge_port, interval=None) as rig:
            engine = runs.RunEngine(database, rig.devices)
            engine.start("base_calibration", {"interval_s": 1})
            left = wait_until(lambda: fake_tc600.STATE[2] is False, 6, 0.1)
            engine.stop("selftest")
            wait_ended(engine, 10)
        return left, "left standby" if left else "still in standby"
    attempt("a run that holds the rotor at full speed takes it out of standby",
            standby_left)

    def rate_of_rise_isolates():
        engine = runs.RunEngine(database, registry.Registry(),
                                hardware=process.ProcessHardware())
        engine._run = database.create_run("rate_of_rise", "gate test", {})
        engine.mode = runs.GUIDED
        try:
            recipe = runs.RateOfRise({})
            recipe.begin(engine)
            recipe.step(engine, {"t": 0.0, "p_best": 1.0e-6})
            prompt = engine.prompt() or {}
        finally:
            database.finish_run(engine._run["id"], dbmod.ABORTED, {})
            engine._flush()              # its log, written before the close
        return (prompt.get("actuator") == "gate" and prompt.get("want") == "0",
                prompt.get("instruction") or "nobody was asked to close the gate")
    attempt("a rate-of-rise run closes the gate valve itself before it measures",
            rate_of_rise_isolates)

    attempt("full vacuum says what stopping it does",
            lambda: ("keep the vacuum" in runs.FullVacuum.description
                     and "Stopping it powers the pump down"
                     not in runs.FullVacuum.description,
                     runs.FullVacuum.description[-80:]))

    fake_tc600.reset()
    fake_tpg.reset()
    database.close()


def test_authority(pump_port: str) -> None:
    """Who may waive a rule, which rule, and who may change the drive.

    Over real HTTP with accounts on and the simulated pump in the registry,
    so the requests are exactly the panel's.
    """
    import json as jsonmod
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from lab import api as apimod
    from lab import auth as authmod

    print("\nOverrides name their rule; the drive's protections are an admin's")
    fake_tc600.reset()

    # --- the service: one rule waived, the next still applies ------------------
    def waives_only_its_rule():
        fake_tc600.reset()                       # station off, motor off
        with Rig(pump_port) as rig:              # and no gauge: pressure unknown
            first = rig.pump.command("motor_on", override="pressure_unknown")
            fake_tc600.STATE[10] = True
            rig.fresh(lambda s: s.get("setpoints", {}).get("PumpgStatn") is True)
            second = rig.pump.command("motor_on", override="backing_off")
        return (not first.get("ok") and first.get("rule") == "backing_off"
                and not second.get("ok") and second.get("rule")
                == "pressure_unknown" and fake_tc600.STATE[23] is False,
                f"waiving the pressure left {first.get('rule')}; waiving the "
                f"backing pump left {second.get('rule')}")
    attempt("an override waives the rule it names and no other",
            waives_only_its_rule)

    def every_refusal_named():
        service = pumpmod.PumpService("names", "auto")
        service.close()
        service._thread.join(timeout=5)
        unnamed = []
        for snap in ({"connected": False},
                     {"connected": True, "status": {"ActualSpd": 0},
                      "setpoints": {"PumpgStatn": True}, "identity": {}},
                     {"connected": True, "age": 0.5, "status": {},
                      "setpoints": {}, "identity": {}},
                     {"connected": True, "age": 30.0, "status": {"ActualSpd": 0},
                      "setpoints": {"PumpgStatn": True, "MotorPump": False},
                      "identity": {}}):
            for action, rule in service.interlocks(snap).items():
                if not rule["allowed"] and not rule.get("rule"):
                    unnamed.append(action)
        return not unnamed, (f"unnamed: {unnamed}" if unnamed
                             else "every refusal names its rule")
    attempt("every refusal names the rule that refused", every_refusal_named)

    # --- over HTTP, with accounts -------------------------------------------------
    directory = tempfile.mkdtemp(prefix="selftest-authority-")
    database = dbmod.Database(os.path.join(directory, "authority.db"))
    accounts = authmod.Auth(database, "chamber-1")
    devices = registry.Registry()
    pump_service = devices.add(pumpmod.PumpService("pump", pump_port, address=1))
    pump_service.interval = 0.3
    engine = runs.RunEngine(database, devices, chamber="chamber-1")
    application = apimod.Application(devices, database, engine, auth=accounts,
                                     chamber={"id": "chamber-1", "name": "C1"})
    handler = type("AuthorityHandler", (apimod.Handler,),
                   {"app": application, "log_message": lambda *a: None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(route, body=None, token=None):
        request = urllib.request.Request(
            base + route, method="POST" if body is not None else "GET",
            data=jsonmod.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, jsonmod.loads(response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, (jsonmod.loads(raw) if raw[:1] == b"{" else {})

    def param(name, value, token):
        return call("/api/command", {"device": "pump", "action": "param",
                                     "value": {"name": name, "value": value},
                                     "confirm": True}, token)

    try:
        wait_until(lambda: pump_service.snapshot().get("connected"), 10, 0.1)
        authmod.bootstrap_owner(database, "chamber-1", "olga", "olga-pass-1", "Olga")
        owner = call("/api/login", {"username": "olga",
                                    "password": "olga-pass-1"})[1]["token"]
        call("/api/signup", {"username": "ana", "name": "Ana",
                             "password": "ana-pass-1"})
        ana_id = next(p["id"] for p in call("/api/users", token=owner)[1]["people"]
                      if p["username"] == "ana")
        call("/api/users/approve", {"id": ana_id, "role": "operator"}, owner)
        ana = call("/api/login", {"username": "ana",
                                  "password": "ana-pass-1"})[1]["token"]

        code, body = param("RUTimeCtrl", False, ana)
        attempt("an operator cannot switch off the drive's run-up monitoring",
                lambda: (code == 403 and fake_tc600.STATE[4] is True,
                         f"{code}: {body.get('error', 'it was changed')[:60]}"))
        code, body = param("Heating", True, ana)
        heater = (code, body)
        attempt("an operator may still switch the pump's heater (bake-out)",
                lambda: (heater[0] == 200 and heater[1].get("ok")
                         and fake_tc600.STATE[1] is True,
                         f"{heater[0]}: {heater[1].get('error', 'switched on')}"))
        said = [e["message"] for e in database.events(limit=20)
                if "Heating changed from" in e["message"]]
        attempt("the record says what a setting was changed from, and to",
                lambda: (bool(said), said[0] if said else "no before/after "
                         "in the record"))
        offered = call("/api/device/pump/accessories", token=ana)[1]
        attempt("an operator's accessory page offers only the heater",
                lambda: (offered.get("writable") == ["Heating", "TMSHeatSVal"],
                         f"offered {offered.get('writable')}"))
        call("/api/control/release", {}, ana)

        code, body = param("RUTimeCtrl", False, owner)
        attempt("an admin may change it, and it is recorded",
                lambda: (code == 200 and body.get("ok")
                         and fake_tc600.STATE[4] is False
                         and body.get("previous") is True,
                         f"{code}: previous {body.get('previous')!r}, now "
                         f"{fake_tc600.STATE[4]!r}"))
        param("RUTimeCtrl", True, owner)

        start = {"device": "pump", "action": "motor_on", "confirm": True}
        waiver = {"rule": "pressure_unknown", "reason": "chamber checked by eye",
                  "checked": True}
        fake_tc600.STATE[10] = False                 # no backing pump
        wait_until(lambda: pump_service.snapshot().get("setpoints", {})
                   .get("PumpgStatn") is False, 10, 0.1)
        bad = [call("/api/command", {**start, "override": {
                   **waiver, "reason": reason}}, owner)[0]
               for reason in (None, 123, "   ")]
        attempt("an override without a reason in words is refused",
                lambda: (bad == [400, 400, 400] and fake_tc600.STATE[23] is False,
                         f"answers {bad}"))
        code, body = call("/api/command", {**start, "override": waiver}, owner)
        attempt("an admin's override of the pressure rule does not start a "
                "turbopump with no backing",
                lambda: (code == 200 and not body.get("ok")
                         and body.get("rule") == "backing_off"
                         and fake_tc600.STATE[23] is False,
                         f"refused by {body.get('rule')}: "
                         f"{body.get('error', 'it started')[:50]}"))
        fake_tc600.STATE[10] = True
        wait_until(lambda: pump_service.snapshot().get("setpoints", {})
                   .get("PumpgStatn") is True, 10, 0.1)
        code, body = call("/api/command", {**start, "override": waiver}, owner)
        logged = [e for e in database.events(limit=30)
                  if "interlock overridden for" in e["message"]]
        detail = (logged[0].get("detail") or {}) if logged else {}
        attempt("with the backing pump on, it does -- logged with the rule, the "
                "reason and the check",
                lambda: (code == 200 and body.get("ok")
                         and detail.get("rules") == ["pressure_unknown"]
                         and detail.get("checked") is True
                         and detail.get("reason") == "chamber checked by eye",
                         f"{body.get('overridden')}; logged {detail or 'nothing'}"))
        pump_service.command("motor_off")

        # A record that cannot be written must not cost the answer: by then
        # the rotor is already turning.
        import contextlib
        import io
        import sqlite3
        real_add = database.add_event

        def full_card(run_id, level, message, *args, **kwargs):
            if "interlock overridden" in message:
                raise sqlite3.OperationalError("disk I/O error (selftest)")
            return real_add(run_id, level, message, *args, **kwargs)

        wait_until(lambda: pump_service.snapshot().get("setpoints", {})
                   .get("MotorPump") is False, 10, 0.1)
        failures = (engine.storage_error or {}).get("count", 0)
        database.add_event = full_card
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    answered = call("/api/command",
                                    {**start, "override": waiver}, owner)
                except Exception as exc:                 # noqa: BLE001
                    answered = (None, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            del database.add_event
        pump_service.command("motor_off")
        counted = (engine.storage_error or {}).get("count", 0) > failures
        def full_for_actions(run_id, level, message, *args, **kwargs):
            if "changed from" in message:
                raise sqlite3.OperationalError("disk I/O error (selftest)")
            return real_add(run_id, level, message, *args, **kwargs)

        before_action = (engine.storage_error or {}).get("count", 0)
        database.add_event = full_for_actions
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                acted = param("Heating", True, owner)
        finally:
            del database.add_event
        param("Heating", False, owner)
        action_counted = (engine.storage_error or {}).get("count", 0) \
            > before_action
        attempt("an action whose record cannot be written is counted for the "
                "storage alarm",
                lambda: (acted[0] == 200 and acted[1].get("ok")
                         and action_counted,
                         f"{acted[0]}: " + ("counted" if action_counted
                                            else "dropped without a word")))
        attempt("an override whose record cannot be written still gets its "
                "answer, and the storage alarm hears of it",
                lambda: (answered[0] == 200 and answered[1].get("ok") and counted,
                         f"{answered[0]}: "
                         f"{answered[1].get('error') or 'answered'}"
                         + ("" if counted else "; the failure was not counted")))

        # Two rules in front of one command: the station off with the motor
        # flag still set, and the rotor only just read at rest.
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600._speed = 0.0                  # at rest (the fault holds it)
        wait_until(lambda: pump_service.snapshot().get("setpoints", {})
                   .get("MotorPump") is True and pump_service.snapshot()
                   .get("status", {}).get("ActualSpd") == 0, 10, 0.1)
        stop = {"device": "pump", "action": "off", "confirm": True}
        one = {"rules": ["motor_enabled"], "reason": "selftest", "checked": True}
        code1, first = call("/api/command", {**stop, "override": one}, owner)
        both = {**one, "rules": ["motor_enabled", "stop_dwell"]}
        code2, second = call("/api/command", {**stop, "override": both}, owner)
        attempt("an override can name the two rules that stand in front of one "
                "command",
                lambda: (first.get("rule") == "stop_dwell" and second.get("ok")
                         and fake_tc600.STATE[10] is False,
                         f"one rule left {first.get('rule')}; both: "
                         f"{second.get('error') or 'sent'}"))

        # --- deleting runs ----------------------------------------------------
        fake_tc600.reset()
        seen = pump_service.snapshot().get("seq", 0)
        wait_until(lambda: pump_service.snapshot().get("seq", 0) > seen + 1
                   and pump_service.snapshot().get("connected"), 10, 0.1)
        started = engine.start("hold", {"duration_min": 5, "interval_s": 1})
        going = engine.run_id()
        wait_until(lambda: engine.status()["running"], 5, 0.05)
        code, body = call(f"/api/runs/{going}/delete", {"confirm": True}, owner)
        attempt("the run in progress cannot be deleted",
                lambda: (bool(started.get("ok")) and code == 409
                         and database.run(going) is not None,
                         f"{code}: {body.get('error') or 'deleted'}"
                         + ("" if started.get("ok") else
                            f"; the run did not start: {started.get('error')}")))
        engine.stop("selftest")
        wait_until(lambda: not engine.status()["running"], 15, 0.1)
        # A run of its own, ended, then deleted: what follows must not fail.
        own = engine.start("hold", {"duration_min": 5, "interval_s": 1})
        last = engine.run_id()
        wait_until(lambda: engine.status()["running"], 5, 0.05)
        engine.stop("selftest")
        wait_until(lambda: not engine.status()["running"], 15, 0.1)
        engine._flush()
        param("Heating", False, owner)           # an action, filed under it
        failures = (engine.storage_error or {}).get("count", 0)
        code, body = call(f"/api/runs/{last}/delete", {"confirm": True}, owner)
        audited = [e for e in database.events(limit=30)
                   if "Heating changed from" in e["message"]
                   and e.get("run_id") is None
                   and (e.get("detail") or {}).get("deleted_run") == last]
        attempt("an account's action filed under the last run is kept when that "
                "run is deleted",
                lambda: (bool(audited), audited[0]["message"][:80] if audited
                         else "deleted with the run"))
        forgotten = engine.run_id() is None and engine.status().get("last") is None
        engine.log("info", "selftest: a record after the last run was deleted")
        engine._flush()
        failed = engine.storage_error
        added = (failed or {}).get("count", 0) - failures
        after = next((e for e in database.events(limit=10) if e["message"]
                      == "selftest: a record after the last run was deleted"), {})
        plain = after.get("run_id") is None \
            and "deleted_run" not in (after.get("detail") or {})
        attempt("deleting the last run leaves the engine's records working",
                lambda: (bool(own.get("ok")) and code == 200 and added == 0
                         and forgotten and plain,
                         f"{code}; " + (f"{added} writes failed: {failed['error']}"
                                        if added else "nothing failed")
                         + ("" if forgotten else "; the engine still names it")
                         + ("" if plain else "; later records filed under it")))
        owing = database.create_run("bakeout", "Owed, then deleted", {})
        database.finish_run(owing["id"], dbmod.ABORTED, {"cleanup_pending": True})
        trying = engine._remember(owing, "trying again", None, "retry")
        held, _ = call(f"/api/runs/{owing['id']}/delete", {"confirm": True},
                       owner)
        trying["ok"] = False                     # its tries have run out
        gone, _ = call(f"/api/runs/{owing['id']}/delete", {"confirm": True},
                       owner)
        noted = [e["message"] for e in database.events(limit=30)
                 if f"deleted run {owing['id']}" in e["message"]]
        attempt("a run whose clean-up is being tried cannot be deleted; deleting "
                "it later says what it still owed",
                lambda: (held == 409 and gone == 200 and bool(noted)
                         and "had not succeeded" in noted[0],
                         f"{held} then {gone}: "
                         + (noted[0][:90] if noted else "nothing noted")))
    finally:
        server.shutdown()
        server.server_close()
        devices.close()
        database.close()
        fake_tc600.reset()



def test_security(pump_port: str) -> None:
    """Hardening of the accounts: what an idle session, a burst of sign-ins,
    a wrong clock or a stray request can no longer do.

    Over real HTTP against the real handler (and the bounded server), with
    accounts on and an empty registry, like the accounts section.
    """
    import json as jsonmod
    import socket
    import sqlite3
    import urllib.error
    import urllib.request
    from lab import api as apimod
    from lab import auth as authmod

    print("\nAccount hardening: idle sessions, bursts, clocks, stray requests")
    directory = tempfile.mkdtemp(prefix="selftest-security-")
    database = dbmod.Database(os.path.join(directory, "security.db"))
    accounts = authmod.Auth(database, "chamber-1")
    engine = runs.RunEngine(database, registry.Registry(), chamber="chamber-1")
    application = apimod.Application(registry.Registry(), database, engine,
                                     auth=accounts,
                                     chamber={"id": "chamber-1", "name": "C1"})
    handler = type("SecurityHandler", (apimod.Handler,),
                   {"app": application, "log_message": lambda *a: None})
    server_class = getattr(apimod, "Server", None)
    if server_class is None:                      # the code before the cap
        from http.server import ThreadingHTTPServer as server_class
    server = server_class(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"

    def call(path, body=None, token=None):
        request = urllib.request.Request(
            base + path, method="POST" if body is not None else "GET",
            data=jsonmod.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, jsonmod.loads(response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, (jsonmod.loads(raw) if raw[:1] == b"{" else {})

    def login(name, password, **extra):
        return call("/api/login", {"username": name, "password": password,
                                   **extra})[1].get("token")

    def person(name, role):
        # Made directly: sign-ups are limited to a few an hour per address.
        user = database.create_user(name, authmod.hash_password(f"{name}-pass-1"),
                                    display_name=name.title(), status="active")
        database.set_role(user["id"], "chamber-1", role, "selftest")
        return user["id"], login(name, f"{name}-pass-1")

    def age(user_id, seconds):
        """An hour (or `seconds`) away from the panel, on both clocks."""
        with database._lock:
            database._db.execute("UPDATE sessions SET active_at = active_at - ? "
                                 "WHERE user_id=?", (seconds, user_id))
            database._db.commit()
        for row in database.sessions(user_id):
            if row["token_hash"] in accounts._touched:
                accounts._touched[row["token_hash"]] -= seconds

    try:
        authmod.bootstrap_owner(database, "chamber-1", "owner", "owner-pass-1")
        owner = login("owner", "owner-pass-1")
        owner_id = database.user_by_name("owner")["id"]
        ada_id, ada = person("ada", "admin")
        bo_id, bo = person("bob", "viewer")

        # --- an idle session administers nothing ---------------------------
        def idle_admin_refused():
            pending = database.create_user("pat", "", display_name="Pat",
                                           status="pending")
            database.create_role_request(bo_id, "chamber-1", "operator")
            asked_for = database.role_requests("chamber-1", user_id=bo_id)[0]["id"]
            wall = call("/api/devices/add", {"label": "Spare"}, owner)[1]["device"]["id"]
            age(ada_id, 3600)
            age(owner_id, 3600)
            asked = {
                "list": call("/api/users", token=ada),
                "role": call("/api/users/role", {"id": bo_id, "role": "operator"}, ada),
                "reset": call("/api/users/reset-password", {"id": bo_id}, ada),
                "tablet": call("/api/devices/add", {"label": "X"}, ada),
                "ask": call("/api/role-requests", {"role": "owner"}, ada),
                "approve": call("/api/users/approve", {"id": pending["id"],
                                                       "role": "viewer"}, ada),
                "decline": call("/api/users/decline", {"id": pending["id"]}, ada),
                "status": call("/api/users/status", {"id": bo_id, "enabled": False}, ada),
                "decide": call("/api/role-requests/decide",
                               {"id": asked_for, "approve": True}, ada),
                "code": call("/api/devices/code", {"id": wall}, ada),
                "remove": call("/api/devices/remove", {"id": wall}, ada),
                "transfer": call("/api/users/transfer-owner",
                                 {"id": ada_id, "password": "owner-pass-1"}, owner),
            }
            wrong = {k: c for k, (c, b) in asked.items()
                     if not (c == 401 and b.get("reauth") is True)}
            call("/api/session/reauth", {"password": "owner-pass-1"}, owner)
            return (not wrong and database.role(bo_id, "chamber-1") == "viewer"
                    and database.role(owner_id, "chamber-1") == "owner"
                    and database.user(bo_id)["status"] == "active",
                    f"answered otherwise: {wrong}" if wrong
                    else "each asked for the password")
        attempt("an idle admin session administers nothing; it is asked for "
                "the password", idle_admin_refused)

        def password_wakes_admin():
            woke = call("/api/session/reauth", {"password": "ada-pass-1"}, ada)[0]
            role = call("/api/users/role", {"id": bo_id, "role": "operator"}, ada)[0]
            call("/api/users/role", {"id": bo_id, "role": "viewer"}, ada)
            return woke == 200 and role == 200, f"reauth {woke}, role {role}"
        attempt("the password wakes it again", password_wakes_admin)

        # --- the owner is protected from a real admin ----------------------
        def admin_cannot_touch_owner():
            to_owner = call("/api/users/role", {"id": owner_id, "role": "viewer"}, ada)[0]
            cy_id, _ = person("cyd", "admin")
            to_admin = call("/api/users/role", {"id": cy_id, "role": "viewer"}, ada)[0]
            return (to_owner == 403 and to_admin == 403
                    and database.role(owner_id, "chamber-1") == "owner"
                    and database.role(cy_id, "chamber-1") == "admin",
                    f"owner {to_owner}, other admin {to_admin}")
        attempt("an admin (not the owner) cannot demote the owner or another "
                "admin", admin_cannot_touch_owner)

        # --- bursts ---------------------------------------------------------
        def in_flight_bounded():
            throttle = authmod.Throttle()
            first, second = throttle.begin("k"), throttle.begin("k")
            third = throttle.begin("k")
            throttle.end("k", failed=False)
            fourth = throttle.begin("k")
            return (first == 0 and second == 0 and third > 0 and fourth == 0,
                    f"{first}, {second}, {third}, {fourth}")
        attempt("only a few attempts under one name are checked at once",
                in_flight_bounded)

        def parallel_guesses():
            accounts.throttle = authmod.Throttle()
            answers = []
            def guess(i):
                answers.append(call("/api/login", {"username": "bob",
                                                   "password": f"wrong-{i}x"})[0])
            threads = [threading.Thread(target=guess, args=(i,)) for i in range(14)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            checked = answers.count(401)
            limit = authmod.Throttle.FREE + getattr(authmod.Throttle, "AT_ONCE", 0)
            accounts.throttle = authmod.Throttle()
            return (checked <= limit and len(answers) == 14,
                    f"{checked} of 14 parallel guesses were checked (at most {limit})")
        attempt("a burst of guesses in parallel is counted, not all checked",
                parallel_guesses)

        def scrypt_bounded():
            slots = authmod._scrypt_slots
            saved = authmod.SCRYPT_WAIT_S
            held = [slots.acquire(timeout=5) for _ in range(authmod.SCRYPT_AT_ONCE)]
            authmod.SCRYPT_WAIT_S = 0.2
            try:
                code, body = call("/api/login", {"username": "bob",
                                                 "password": "bob-pass-1"})
            finally:
                authmod.SCRYPT_WAIT_S = saved
                for got in held:
                    if got:
                        slots.release()
            after = call("/api/login", {"username": "bob", "password": "bob-pass-1"})[0]
            return (all(held) and code == 503 and after == 200,
                    f"busy: {code} ({body.get('error')}); then {after}")
        attempt("passwords are hashed a few at a time; the rest are told the "
                "server is busy", scrypt_bounded)

        def connections_bounded():
            if not hasattr(apimod, "MAX_CONNECTIONS_PER_ADDRESS"):
                return False, "no connection cap"
            saved = apimod.MAX_CONNECTIONS_PER_ADDRESS
            apimod.MAX_CONNECTIONS_PER_ADDRESS = 3
            idle = []
            try:
                for _ in range(3):
                    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
                    idle.append(sock)
                time.sleep(0.3)
                extra = socket.create_connection(("127.0.0.1", port), timeout=5)
                extra.sendall(b"GET /api/ping HTTP/1.0\r\n\r\n")
                refused = extra.recv(200)
                extra.close()
            finally:
                for sock in idle:
                    sock.close()
                apimod.MAX_CONNECTIONS_PER_ADDRESS = saved
            freed = wait_until(lambda: _try(lambda: call("/api/ping")[0] == 200),
                               timeout=10)
            return (refused.startswith(b"HTTP/1.1 503") and freed,
                    f"over the cap: {refused[:30]!r}; served again: {freed}")
        attempt("connections beyond the cap are turned away, and served again "
                "once others close", connections_bounded)

        # --- lockouts -------------------------------------------------------
        def no_lockout_from_elsewhere():
            accounts.throttle = authmod.Throttle()
            for i in range(8):
                accounts.login("bob", f"guess-{i}xx", address="10.9.9.9")
            mine = accounts.login("bob", "bob-pass-1", address="10.1.1.1")
            accounts.throttle = authmod.Throttle()
            return mine.get("ok") is True, mine.get("error") or "signed in"
        attempt("guesses from one address do not lock the account out from "
                "another", no_lockout_from_elsewhere)

        def reauth_per_session():
            accounts.throttle = authmod.Throttle()
            for i in range(8):
                call("/api/session/reauth", {"password": f"nope-{i}xx"}, bo)
            code = call("/api/login", {"username": "bob", "password": "bob-pass-1"})[0]
            accounts.throttle = authmod.Throttle()
            return code == 200, f"sign-in after wrong reauths: {code}"
        attempt("wrong passwords at the idle prompt do not stop signing in",
                reauth_per_session)

        def transfer_throttled():
            accounts.throttle = authmod.Throttle()
            codes = [call("/api/users/transfer-owner",
                          {"id": ada_id, "password": f"wrong-{i}x"}, owner)[0]
                     for i in range(7)]
            accounts.throttle = authmod.Throttle()
            return (429 in codes and database.role(owner_id, "chamber-1") == "owner",
                    f"{codes}")
        attempt("handing the chamber over is throttled like any password",
                transfer_throttled)

        # --- a person on a paired tablet ------------------------------------
        pairing = call("/api/devices/add", {"label": "Wall"}, owner)[1]
        tablet = call("/api/pair", {"code": pairing["code"]})[1].get("token")
        dee_id, _ = person("dee", "operator")

        def tablet_session_bounded():
            token = login("dee", "dee-pass-1", device=tablet)
            row = database.session(authmod.token_hash(token or ""))
            limit = time.time() + getattr(authmod, "TABLET_SESSION_S", 0) + 60
            return (bool(row) and row.get("device_id") is not None
                    and row["expires_at"] <= limit,
                    f"device {row and row.get('device_id')}, expires in "
                    f"{row and round((row['expires_at'] - time.time()) / 3600, 1)} h")
        attempt("a person signed in on a paired tablet gets a session tied to "
                "it, of 8 h at most", tablet_session_bounded)

        def tablet_session_ends_idle():
            on_tablet = login("dee", "dee-pass-1", device=tablet)
            personal = login("dee", "dee-pass-1")
            age(dee_id, 3600)
            code = call("/api/state", token=on_tablet)[0]
            gone = database.session(authmod.token_hash(on_tablet)) is None
            still = call("/api/me", token=personal)
            return (code == 401 and gone and still[0] == 200
                    and still[1]["me"]["idle"] is True,
                    f"tablet session {code}, deleted {gone}; personal "
                    f"{still[0]}, idle {still[1].get('me', {}).get('idle')}")
        attempt("idle on the tablet signs the person out there; a personal "
                "session just waits for the password", tablet_session_ends_idle)

        def repair_revokes():
            fresh = call("/api/devices/code", {"id": pairing["device"]["id"]}, owner)[1]
            again = call("/api/pair", {"code": fresh["code"]})[1].get("token")
            return (call("/api/state", token=tablet)[0] == 401
                    and call("/api/state", token=again)[0] == 200,
                    "the old tablet session ended")
        attempt("pairing a tablet again ends its old session", repair_revokes)

        def again_tablet():
            """A fresh pairing of the wall tablet: its session token."""
            code = call("/api/devices/code", {"id": pairing["device"]["id"]},
                        owner)[1]["code"]
            return call("/api/pair", {"code": code})[1].get("token")

        # --- clocks ---------------------------------------------------------
        accounts.throttle = authmod.Throttle()
        person("hal", "viewer")

        def future_touch_is_idle():
            token = login("hal", "hal-pass-1")
            hashed = authmod.token_hash(token)
            database.touch_session(hashed, active_at=time.time() + 6 * 3600)
            restarted = authmod.Auth(database, "chamber-1")   # nothing in memory
            restarted.clock_check = lambda: True              # even then
            seen = restarted.resolve(token)
            described = restarted.me(seen) if seen else {}
            return seen is not None and seen.idle() and described.get("idle"), \
                f"inactive {seen and seen.inactive_s}"
        attempt("after a restart, a touch dated in the future counts as idle "
                "(the clock was wrong)", future_touch_is_idle)

        def high_water_mark():
            token = login("hal", "hal-pass-1")
            hashed = authmod.token_hash(token)
            # The clock went back an hour at boot: the last touch, a minute
            # before the restart by the old clock, is "59 minutes ago".
            database.touch_session(hashed, active_at=time.time() - 60)
            database.set_meta("wall_high_water", repr(time.time() + 3600)) \
                if hasattr(database, "set_meta") else None
            restarted = authmod.Auth(database, "chamber-1")
            restarted.clock_check = lambda: True    # synchronised, then set back
            seen = restarted.resolve(token)
            return seen is not None and seen.idle(), \
                f"inactive {seen and round(seen.inactive_s)} s"
        attempt("the latest time ever seen is kept, so a clock set back at "
                "boot revives no idle session", high_water_mark)

        def high_water_kept():
            database.set_meta("wall_high_water", "0")
            fresh = authmod.Auth(database, "chamber-1")
            fresh._touch(authmod.token_hash(login("hal", "hal-pass-1")), write=True)
            mark = float(database.meta("wall_high_water") or 0)
            return abs(mark - time.time()) < 60, f"mark {mark:.0f}"
        attempt("the high-water mark is written as time passes", high_water_kept)

        # --- housekeeping ---------------------------------------------------
        def signup_text_kept_out():
            call("/api/signup", {"username": "eve", "name": "Free beer at the "
                                 "pump, click here", "password": "eve-pass-12"})
            leaked = [e["message"] for e in database.events(limit=50)
                      if "beer" in e["message"]]
            return not leaked, f"{leaked[:1]}" if leaked else "only the username"
        attempt("text typed at sign-up stays out of the activity feed",
                signup_text_kept_out)

        def stats_path_admin_only():
            hal = login("hal", "hal-pass-1")
            code, body = call("/api/stats", token=hal)
            viewer = body.get("stats", {})
            admin = call("/api/stats", token=owner)[1].get("stats", {})
            return (code == 200 and "runs" in viewer and "path" not in viewer
                    and "path" in admin, f"{code}: viewer sees {sorted(viewer)}")
        attempt("where the database lives is shown to admins only",
                stats_path_admin_only)

        def events_capped():
            for i in range(1100):
                database.add_event(None, "info", f"filler {i}")
            code, body = call("/api/events?limit=100000", token=owner)
            got = body.get("events", [])
            return code == 200 and len(got) == 1000, f"{code}: {len(got)} rows"
        attempt("a listing returns at most 1000 rows", events_capped)

        def run_edit_logged():
            run = database.create_run("hold", "Hold", {}, chamber="chamber-1")
            database.finish_run(run["id"], "completed", {})
            _, op = person("opal", "operator")
            code = call(f"/api/runs/{run['id']}", {"name": "Renamed"}, op)[0]
            logged = [e for e in database.events(limit=20)
                      if "edited" in e["message"] and e.get("user") == "opal"]
            return code == 200 and bool(logged), f"{code}, {len(logged)} record(s)"
        attempt("editing a run's record is logged under who did it",
                run_edit_logged)

        def stale_request_closed():
            fay_id, fay = person("fay", "viewer")
            call("/api/role-requests", {"role": "operator"}, fay)
            database.set_role(fay_id, "chamber-1", "admin", "selftest")
            request = database.role_requests("chamber-1", user_id=fay_id)[0]
            code = call("/api/role-requests/decide",
                        {"id": request["id"], "approve": True}, owner)[0]
            gus_id, gus = person("gus", "viewer")
            call("/api/role-requests", {"role": "operator"}, gus)
            database.update_user(gus_id, status="disabled")
            other = database.role_requests("chamber-1", user_id=gus_id)[0]
            code2 = call("/api/role-requests/decide",
                         {"id": other["id"], "approve": True}, owner)[0]
            return (code == 409 and code2 == 409
                    and database.role(fay_id, "chamber-1") == "admin"
                    and database.role(gus_id, "chamber-1") == "viewer"
                    and not database.role_requests("chamber-1", user_id=fay_id),
                    f"already above: {code}; disabled: {code2}")
        attempt("an out-of-date role request is closed, not applied",
                stale_request_closed)

        # --- downloads do not hold up the rig ------------------------------
        def copy_without_lock():
            done = threading.Event()
            target = os.path.join(directory, "copy.db")
            with database._lock:          # what every writer would be held by
                worker = threading.Thread(
                    target=lambda: (database.snapshot(target), done.set()),
                    daemon=True)
                worker.start()
                finished = done.wait(15)
            worker.join(timeout=30)
            return finished, ("copied while the store was busy" if finished
                              else "the copy waited for the store's lock")
        attempt("the downloadable copy is taken without holding up the "
                "rig's writes", copy_without_lock)

        def one_export_at_a_time():
            slot = getattr(application, "export_slot", None)
            if slot is None:
                return False, "no export slot"
            saved = apimod.EXPORT_WAIT_S
            apimod.EXPORT_WAIT_S = 0.2
            slot.acquire()
            try:
                busy = call("/api/export/runs.csv", token=owner)[0]
            finally:
                slot.release()
                apimod.EXPORT_WAIT_S = saved
            request = urllib.request.Request(base + "/api/export/runs.csv",
                                             headers={"Authorization": f"Bearer {owner}"})
            with urllib.request.urlopen(request, timeout=30) as response:
                after = response.status
            return busy == 429 and after == 200, f"while busy {busy}, after {after}"
        attempt("one download is built at a time; another is asked to wait",
                one_export_at_a_time)

        def archive_streamed():
            request = urllib.request.Request(base + "/api/export/archive.json",
                                             headers={"Authorization": f"Bearer {owner}"})
            with urllib.request.urlopen(request, timeout=60) as response:
                archive = jsonmod.loads(response.read())
            runs_in = archive.get("runs", [])
            # Removed by the server just after the last byte is sent.
            wait_until(lambda: not os.listdir(application._export_dir), 5, 0.05)
            left = os.listdir(application._export_dir)
            return (len(runs_in) == database.count_runs() and runs_in
                    and "samples" in runs_in[0] and "path" not in archive["stats"]
                    and not left,
                    f"{len(runs_in)} runs, stats {sorted(archive.get('stats', {}))}, "
                    f"left behind {left}")
        attempt("the full archive is written to disk run by run, is valid "
                "JSON, and is not left behind", archive_streamed)

        def on_demand_reads_kept():
            fake_tc600.reset()
            with Rig(pump_port) as rig:
                first = rig.pump.diagnostics()
                started = time.monotonic()
                second = rig.pump.diagnostics()
                quick = time.monotonic() - started
                accessories = rig.pump.accessories()
                again = rig.pump.accessories()
                wrote = rig.pump.write_param("TMSHeatSVal", 50)
                after = rig.pump.accessories()
            return (first.get("ok") and first.get("kept") is False
                    and second.get("kept") is True and quick < 0.05
                    and accessories.get("ok") and again.get("kept") is True
                    and wrote.get("ok") and after.get("kept") is False,
                    f"second diagnostics in {quick * 1000:.0f} ms, kept "
                    f"{second.get('kept')}; accessories kept {again.get('kept')}; "
                    f"after a write kept {after.get('kept')}")
        attempt("the pump's diagnostics and accessories are read once a "
                "minute, and afresh after a write", on_demand_reads_kept)

        # --- the network boundary -----------------------------------------
        def raw(path, headers, body=None):
            request = urllib.request.Request(
                base + path, method="POST" if body is not None else "GET",
                data=jsonmod.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, dict(response.headers)
            except urllib.error.HTTPError as error:
                error.read()
                return error.code, dict(error.headers)

        def foreign_host_refused():
            if not hasattr(apimod, "host_allowed"):
                return False, "no Host check"
            evil = raw("/api/ping", {"Host": "rebound.example.com"})[0]
            mine = raw("/api/ping", {"Host": f"127.0.0.1:{port}"})[0]
            local = raw("/api/ping", {"Host": f"localhost:{port}"})[0]
            v6 = apimod.host_allowed("[::1]:8600", set())
            return (evil == 421 and mine == 200 and local == 200 and v6,
                    f"foreign {evil}, address {mine}, localhost {local}")
        attempt("a request naming another host is refused (DNS rebinding)",
                foreign_host_refused)

        def cors_narrowed():
            hal = login("hal", "hal-pass-1")
            ping = raw("/api/ping", {"Origin": "http://evil.example"})[1]
            state = raw("/api/state", {"Origin": "http://evil.example",
                                       "Authorization": f"Bearer {hal}"})[1]
            application.origins = {"http://panel.example:8600"}
            named = raw("/api/state", {"Origin": "http://panel.example:8600",
                                       "Authorization": f"Bearer {hal}"})[1]
            application.origins = set()
            return (ping.get("Access-Control-Allow-Origin") == "*"
                    and "Access-Control-Allow-Origin" not in state
                    and named.get("Access-Control-Allow-Origin")
                    == "http://panel.example:8600",
                    f"ping {ping.get('Access-Control-Allow-Origin')}, state "
                    f"{state.get('Access-Control-Allow-Origin')}, named "
                    f"{named.get('Access-Control-Allow-Origin')}")
        attempt("only the ping answers any page; the rest only the panel's own "
                "or a named origin", cors_narrowed)

        def foreign_post_refused():
            body = {"username": "hal", "password": "hal-pass-1"}
            evil = raw("/api/login", {"Origin": "http://evil.example"}, body)[0]
            own = raw("/api/login", {"Origin": f"http://127.0.0.1:{port}"}, body)[0]
            none = raw("/api/login", {}, body)[0]
            return (evil == 403 and own == 200 and none == 200,
                    f"foreign page {evil}, own page {own}, no Origin {none}")
        attempt("a POST from another site's page is refused", foreign_post_refused)

        def admin_only_from_lab():
            if not hasattr(application, "admin_from"):
                return False, "no --admin-from"
            campus = type("CampusHandler", (handler,),
                          {"_address": lambda self: "172.16.2.50"})
            outside = server_class(("127.0.0.1", 0), campus)
            threading.Thread(target=outside.serve_forever, daemon=True).start()
            there = f"http://127.0.0.1:{outside.server_address[1]}"
            application.admin_from = apimod.parse_networks("10.0.0.0/8")

            def post(path, body, token):
                request = urllib.request.Request(
                    there + path, method="POST", data=jsonmod.dumps(body).encode(),
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {token}"})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        return response.status
                except urllib.error.HTTPError as error:
                    return error.code
            try:
                fresh = login("owner", "owner-pass-1")
                ivy_id, ivy = person("ivy", "operator")
                listed = urllib.request.Request(
                    there + "/api/users", headers={"Authorization": f"Bearer {fresh}"})
                try:
                    urllib.request.urlopen(listed, timeout=30)
                    users = 200
                except urllib.error.HTTPError as error:
                    users = error.code
                role = post("/api/users/role", {"id": ivy_id, "role": "viewer"}, fresh)
                override = post("/api/command", {
                    "device": "pump", "action": "on", "confirm": True,
                    "override": {"rule": "pressure_unknown", "reason": "test",
                                 "checked": True}}, fresh)
                protection = post("/api/command", {
                    "device": "pump", "action": "param", "confirm": True,
                    "value": {"name": "RUTimeCtrl", "value": 0}}, fresh)
                setup = post("/api/equipment", {"key": "gate", "installed": True,
                                                "confirm": True}, fresh)
                call("/api/control/release", {}, fresh)
                operate = post("/api/command", {"device": "pump", "action": "on",
                                                "confirm": True}, ivy)
                call("/api/control/release", {}, ivy)
            finally:
                application.admin_from = []
                outside.shutdown()
                outside.server_close()
            local = call("/api/users", token=fresh)[0]
            return (users == 403 and role == 403 and override == 403
                    and protection == 403 and setup == 403 and operate == 404
                    and local == 200
                    and database.role(ivy_id, "chamber-1") == "operator",
                    f"users {users}, role {role}, override {override}, drive "
                    f"protection {protection}, set-up {setup}; operating "
                    f"{operate}; from loopback {local}")
        attempt("what only an admin may do is refused from outside "
                "--admin-from; operating is not", admin_only_from_lab)

        def files_private():
            fresh_dir = tempfile.mkdtemp(prefix="selftest-modes-")
            path = os.path.join(fresh_dir, "modes.db")
            store = dbmod.Database(path)
            store.add_event(None, "info", "something to write")
            modes = {suffix or "db": oct(os.stat(path + suffix).st_mode & 0o777)
                     for suffix in ("", "-wal", "-shm")
                     if os.path.exists(path + suffix)}
            store.close()
            return (all(mode == "0o600" for mode in modes.values()) and modes,
                    f"{modes}")
        attempt("the database and its -wal and -shm are readable by their "
                "owner only", files_private)

        def unit_hardened():
            with open(os.path.join(ROOT, "deploy", "tc600-web.service")) as unit:
                text = unit.read()
            wanted = ("UMask=0077", "ProtectSystem=strict", "ReadWritePaths=",
                      "NoNewPrivileges=yes", "PrivateTmp=yes", "TasksMax=")
            missing = [w for w in wanted if f"\n{w}" not in text]
            stale = "--token" in text
            return not missing and not stale, (f"missing {missing}" if missing
                                               else "mentions --token" if stale
                                               else "hardened")
        attempt("the service unit keeps the service to its own files",
                unit_hardened)

        # --- what the review of these found --------------------------------
        def busy_is_not_a_failure():
            accounts.throttle = authmod.Throttle()
            jo_id, jo = person("jo", "operator")
            slots = authmod._scrypt_slots
            saved = authmod.SCRYPT_WAIT_S
            held = [slots.acquire(timeout=5) for _ in range(authmod.SCRYPT_AT_ONCE)]
            authmod.SCRYPT_WAIT_S = 0.05
            try:
                busy = [call("/api/session/reauth", {"password": "jo-pass-1"}, jo)[0]
                        for _ in range(6)]
                changes = [call("/api/me/password", {"current": "jo-pass-1",
                                                     "new": "jo-pass-2x"}, jo)[0]
                           for _ in range(2)]
            finally:
                authmod.SCRYPT_WAIT_S = saved
                for got in held:
                    if got:
                        slots.release()
            if hasattr(authmod, "Budget"):
                accounts.hashes = authmod.Budget(authmod.HASHES_PER_MINUTE)
            after = call("/api/session/reauth", {"password": "jo-pass-1"}, jo)[0]
            return (set(busy) == {503} and set(changes) == {503} and after == 200,
                    f"busy {busy}, change {changes}; then the right password: {after}")
        attempt("a password the server was too busy to check is not counted "
                "as wrong", busy_is_not_a_failure)

        def hashing_budgeted():
            accounts.throttle = authmod.Throttle()
            n = authmod.HASHES_PER_MINUTE
            kim_id, kim = person("kim", "viewer")
            phone = login("kim", "kim-pass-1")
            sessions = [login("kim", "kim-pass-1") for _ in range(3)]
            for name in ("lou", "liz", "max"):
                person(name, "viewer")
            accounts.hashes = authmod.Budget(n)       # the sign-ins above aside
            # A stranger at her abandoned screen guesses...
            stranger = [call("/api/session/reauth", {"password": f"guess-{i}x"},
                             kim)[0] for i in range(n + 3)]
            # ...and she can still wake her phone.
            mine = call("/api/session/reauth", {"password": "kim-pass-1"}, phone)[0]
            accounts.hashes = authmod.Budget(n)
            # Right passwords on several sessions share one account budget.
            spread = [call("/api/session/reauth", {"password": "kim-pass-1"},
                           token)[0] for token in sessions for _ in range(4)]
            accounts.hashes = authmod.Budget(n)
            remote = [accounts.login("lou", "lou-pass-1", address="10.4.4.4")
                      .get("status", 200) for _ in range(n + 3)]
            local = [accounts.login("liz", "liz-pass-1", address="127.0.0.1")
                     .get("status", 200) for _ in range(n + 3)]
            next_one = accounts.login("max", "max-pass-1", address="127.0.0.1")
            accounts.hashes = authmod.Budget(n)
            accounts.throttle = authmod.Throttle()
            return (200 not in stranger and mine == 200
                    and spread.count(200) == n and spread[-1] == 429
                    and remote.count(200) == n and remote[-1] == 429
                    and local.count(200) == n and local[-1] == 429
                    and next_one.get("ok") is True,
                    f"a stranger's guesses {stranger}, then her phone {mine}; "
                    f"right passwords over 3 sessions {spread}; one address's "
                    f"sign-ins {remote}; one account's from loopback {local}; "
                    f"the next person at the kiosk {next_one.get('ok')}")
        attempt("password checks are budgeted per session, per address and "
                "per account -- a stranger's guesses spend none of the "
                "account's, and the kiosk's next person is not held up",
                hashing_budgeted)

        def server_counts():
            if not hasattr(apimod, "MAX_CONNECTIONS"):
                return False, "no connection cap"
            saved = apimod.MAX_CONNECTIONS
            apimod.MAX_CONNECTIONS = 2
            ends = []
            try:
                def offer(address):
                    mine, theirs = socket.socketpair()
                    mine.settimeout(5)
                    ends.append(mine)
                    server.process_request(theirs, (address, 40000))
                    return mine
                offer("10.1.1.1")
                offer("10.1.1.2")
                third = offer("10.1.1.3")
                refused = third.recv(100)
                kiosk = offer("127.0.0.1")
                kiosk.sendall(b"GET /api/ping HTTP/1.0\r\n\r\n")
                answered = kiosk.recv(100)
            finally:
                apimod.MAX_CONNECTIONS = saved
                for end in ends:
                    end.close()
            drained = wait_until(lambda: not server._open, timeout=10)
            return (refused.startswith(b"HTTP/1.1 503")
                    and answered.startswith(b"HTTP/1.0 200") and drained,
                    f"over the cap {refused[:20]!r}; kiosk {answered[:20]!r}; "
                    f"all counted out {drained} ({server._open})")
        attempt("the connection cap turns the network away but not the kiosk, "
                "and every connection is counted out again", server_counts)

        def decline_keeps_text_out():
            call("/api/signup", {"username": "zed", "name": "Visit evil dot "
                                 "example now", "password": "zed-pass-12"},)
            zed = database.user_by_name("zed")
            if zed is None:                  # sign-ups from here used up
                zed = database.create_user("zed", "", display_name="Visit evil "
                                           "dot example now", status="pending")
            code = call("/api/users/decline", {"id": zed["id"]}, owner)[0]
            leaked = [e["message"] for e in database.events(limit=50)
                      if "evil" in e["message"]]
            return code == 200 and not leaked, f"{code}, {leaked[:1]}"
        attempt("declining a request keeps its typed name out of the feed too",
                decline_keeps_text_out)

        def listing_floor():
            code, body = call("/api/runs?limit=-1", token=owner)
            return code == 200 and len(body.get("runs", [])) <= 1, \
                f"{code}, {len(body.get('runs', []))} rows"
        attempt("a negative listing limit is no limit-less listing",
                listing_floor)

        def notes_bounded():
            run = database.create_run("hold", "Hold", {}, chamber="chamber-1")
            database.finish_run(run["id"], "completed", {})
            code = call(f"/api/runs/{run['id']}", {"notes": "x" * 20000}, owner)[0]
            stored = database.run(run["id"])["notes"] or ""
            logged = [e for e in database.events(limit=20)
                      if (e.get("detail") or {}).get("run") == run["id"]]
            shown = len(jsonmod.dumps(logged[0]["detail"])) if logged else 0
            return (code == 200 and len(stored) <= 4000 and logged
                    and shown < 1000,
                    f"stored {len(stored)} characters, logged {shown}")
        attempt("a run's notes are bounded, and logged shortened",
                notes_bounded)

        def old_store_migrates():
            import sqlite3
            path = os.path.join(directory, "before.db")
            with sqlite3.connect(path) as old:
                old.execute("CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, "
                            "user_id INTEGER NOT NULL, created_at REAL NOT NULL, "
                            "seen_at REAL NOT NULL, active_at REAL NOT NULL, "
                            "expires_at REAL, address TEXT DEFAULT '', "
                            "agent TEXT DEFAULT '')")
            store = dbmod.Database(path)
            with store._lock:
                columns = {row["name"] for row in
                           store._db.execute("PRAGMA table_info(sessions)")}
            store.close()
            return "device_id" in columns, f"{sorted(columns)}"
        attempt("a store from before gains the sessions' tablet column",
                old_store_migrates)

        def unsynchronised_clock():
            accounts.throttle = authmod.Throttle()
            token = login("hal", "hal-pass-1")
            restarted = authmod.Auth(database, "chamber-1")
            restarted.clock_check = lambda: False
            doubtful = restarted.resolve(token)
            trusting = authmod.Auth(database, "chamber-1")
            trusting.clock_check = lambda: True
            trusted = trusting.resolve(token)
            return (doubtful is not None and doubtful.idle()
                    and trusted is not None and not trusted.idle(),
                    f"unsynchronised: idle {doubtful and doubtful.idle()}; "
                    f"synchronised: idle {trusted and trusted.idle()}")
        attempt("after a restart, a stored touch counts only if network time "
                "vouches for the clock (a power cut)", unsynchronised_clock)

        def tablet_control_dropped():
            accounts.throttle = authmod.Throttle()
            dee = login("dee", "dee-pass-1", device=again_tablet())
            took = call("/api/control/take", {}, dee)[0]
            age(dee_id, 3600)
            gone = call("/api/state", token=dee)[0]
            held = call("/api/state", token=owner)[1]["control"].get("held")
            return took == 200 and gone == 401 and held is False, \
                f"took {took}; then {gone}; control still held: {held}"
        attempt("a tablet session that ends on idle lets go of control",
                tablet_control_dropped)

        def runner_on_tablet():
            dee = login("dee", "dee-pass-1", device=again_tablet())
            accounts.runner = lambda: "dee"
            try:
                age(dee_id, 20 * 60)
                code = call("/api/state", token=dee)[0]
            finally:
                accounts.runner = None
            return code == 200, f"after 20 min untouched, with her run going: {code}"
        attempt("the person whose run is going keeps the longer limit on the "
                "tablet too (open question 8, as it stands)", runner_on_tablet)

        def full_disk_session_writes():
            jo = login("jo", "jo-pass-1")
            real = database.touch_session

            def broken(*args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error (selftest)")
            database.touch_session = broken
            accounts._written.clear()           # the next touch writes
            with database._lock:
                database._db.execute("UPDATE sessions SET seen_at = seen_at - 600")
                database._db.commit()
            try:
                state = call("/api/state", token=jo)[0]
                secure = call("/api/pump/secure", {"confirm": True}, jo)[0]
            finally:
                database.touch_session = real
            return state == 200 and secure == 200, \
                f"state {state}, make safe {secure}"
        attempt("a full disk under the session bookkeeping does not cost "
                "Make safe its answer", full_disk_session_writes)

        # --- the second review -----------------------------------------------
        def failed_read_frees_slot():
            accounts.throttle = authmod.Throttle()
            accounts.hashes = authmod.Budget(authmod.HASHES_PER_MINUTE)
            ned_id, ned = person("ned", "viewer")
            real = database.user
            failures = {"left": 2}

            def flaky(user_id):
                # Only the password check's own read: the session's lookup
                # before it is another matter.
                checking = sys._getframe(1).f_code.co_name == "_check_own_password"
                if user_id == ned_id and checking and failures["left"] > 0:
                    failures["left"] -= 1
                    raise sqlite3.OperationalError("disk I/O error (selftest)")
                return real(user_id)
            database.user = flaky
            try:
                # Each fails outright (the store raised); what matters is after.
                first = [_try(lambda: call("/api/session/reauth",
                                           {"password": "ned-pass-1"}, ned)[0])
                         for _ in range(2)]
            finally:
                database.user = real
            after = call("/api/session/reauth", {"password": "ned-pass-1"}, ned)[0]
            return after == 200, f"while the store failed {first}; then {after}"
        attempt("a failed read of the account does not keep its attempt "
                "counted as in flight", failed_read_frees_slot)

        def loopback_counted_together():
            saved = apimod.MAX_CONNECTIONS_PER_ADDRESS
            apimod.MAX_CONNECTIONS_PER_ADDRESS = 2
            ends = []
            try:
                def offer(address):
                    mine, theirs = socket.socketpair()
                    mine.settimeout(5)
                    ends.append(mine)
                    server.process_request(theirs, (address, 40000))
                    return mine
                offer("127.0.0.2")
                offer("127.0.0.3")
                refused = offer("127.0.0.4").recv(100)
            finally:
                apimod.MAX_CONNECTIONS_PER_ADDRESS = saved
                for end in ends:
                    end.close()
            wait_until(lambda: not server._open, timeout=10)
            return refused.startswith(b"HTTP/1.1 503"), f"{refused[:20]!r}"
        attempt("every loopback address shares the one loopback share of "
                "connections", loopback_counted_together)

        def take_over_from_lab_only():
            campus = type("CampusHandler2", (handler,),
                          {"_address": lambda self: "172.16.2.51"})
            outside = server_class(("127.0.0.1", 0), campus)
            threading.Thread(target=outside.serve_forever, daemon=True).start()
            application.admin_from = apimod.parse_networks("10.0.0.0/8")
            try:
                _, op = person("oli", "operator")
                call("/api/control/take", {}, op)
                fresh = login("owner", "owner-pass-1")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{outside.server_address[1]}/api/control/take",
                    method="POST", data=b'{"force": true}',
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {fresh}"})
                try:
                    with urllib.request.urlopen(request, timeout=30) as response:
                        code = response.status
                except urllib.error.HTTPError as error:
                    code = error.code
                holder = call("/api/state", token=op)[1]["control"].get("mine")
                call("/api/control/release", {}, op)
            finally:
                application.admin_from = []
                outside.shutdown()
                outside.server_close()
            return code == 403 and holder is True, \
                f"take-over from outside: {code}; the operator still holds it: {holder}"
        attempt("an admin's take-over of control is refused from outside "
                "--admin-from too", take_over_from_lab_only)

        def diagnostics_after_fault():
            fake_tc600.reset()
            saved = pumpmod.ON_DEMAND_MIN_S
            try:
                with Rig(pump_port) as rig:
                    first = rig.pump.diagnostics()
                    kept = rig.pump.diagnostics()
                    pumpmod.ON_DEMAND_MIN_S = 0.0
                    asked = rig.pump.diagnostics(fresh=True)
                    pumpmod.ON_DEMAND_MIN_S = saved
                    fake_tc600.STATE[303] = "Err006"
                    rig.fresh(lambda snap: str(snap.get("status", {})
                                               .get("ErrorCode")).strip() == "Err006")
                    faulted = rig.pump.diagnostics()
                    rig.pump.drop()
                    rig.fresh()
                    reconnected = rig.pump.diagnostics()
            finally:
                pumpmod.ON_DEMAND_MIN_S = saved
                fake_tc600.reset()
            return (first.get("kept") is False and kept.get("kept") is True
                    and asked.get("kept") is False
                    and faulted.get("kept") is False
                    and reconnected.get("kept") is False,
                    f"kept: again {kept.get('kept')}, asked for "
                    f"{asked.get('kept')}, after a fault {faulted.get('kept')}, "
                    f"after a reconnect {reconnected.get('kept')}")
        attempt("the drive's diagnostics are read afresh after a fault, a "
                "reconnect, or a person asking", diagnostics_after_fault)

        def fresh_needs_operator():
            probe = apimod.Handler.__new__(handler)
            probe.path = "/api/device/pump/diagnostics?fresh=1"
            viewer = authmod.Principal(1, "v", "V", "person", "viewer", "x")
            operator = authmod.Principal(2, "o", "O", "person", "operator", "y")
            asked = probe._fresh(viewer), probe._fresh(operator)
            probe.path = "/api/device/pump/diagnostics"
            unasked = probe._fresh(operator)
            return (asked == (False, True) and unasked is False
                    and pumpmod.ON_DEMAND_MIN_S >= 30,
                    f"viewer {asked[0]}, operator {asked[1]}, not asked "
                    f"{unasked}; floor {pumpmod.ON_DEMAND_MIN_S} s")
        attempt("only somebody who may operate makes the drive be read now, "
                "and at most twice a minute", fresh_needs_operator)

        def sampler_keeps_reading():
            fake_tc600.reset()
            seen = []
            stop = threading.Event()
            with Rig(pump_port) as rig:
                rig.fresh()

                def watch():
                    while not stop.is_set():
                        seen.append((time.monotonic(), rig.pump.snapshot().get("seq")))
                        time.sleep(0.02)
                watcher = threading.Thread(target=watch, daemon=True)
                watcher.start()
                started = time.monotonic()
                result = rig.pump._read_diagnostics()
                ended = time.monotonic()
                stop.set()
                watcher.join(timeout=5)
            during = [seq for t, seq in seen if started <= t <= ended]
            fresh_readings = len(set(during)) - 1
            fake_tc600.reset()
            return (result.get("ok") and fresh_readings >= 2,
                    f"{fresh_readings} new pump readings during a "
                    f"{ended - started:.1f} s read of the drive's settings")
        attempt("the pump keeps being read while a page reads the drive's "
                "settings", sampler_keeps_reading)

        def write_not_behind_read():
            fake_tc600.reset()
            with Rig(pump_port) as rig:
                rig.fresh()
                reader = threading.Thread(
                    target=lambda: rig.pump.diagnostics(fresh=True), daemon=True)
                started = time.monotonic()
                reader.start()
                time.sleep(0.3)                      # the read is under way
                wrote = rig.pump.write_param("TMSHeatSVal", 55)
                waited = time.monotonic() - started - 0.3
                reader.join(timeout=30)
                read_for = time.monotonic() - started
                after = rig.pump.diagnostics()
            fake_tc600.reset()
            return (wrote.get("ok") and waited < read_for / 2
                    and after.get("kept") is False,
                    f"the write took {waited:.1f} s during a {read_for:.1f} s "
                    f"read; the read it overtook was kept: {after.get('kept')}")
        attempt("a write to the drive does not wait behind a page reading it, "
                "and the read it overtook is not kept", write_not_behind_read)

        def parallel_checks_budgeted():
            accounts.throttle = authmod.Throttle()
            n = authmod.HASHES_PER_MINUTE
            pia_id, _ = person("pia", "viewer")
            tokens = [login("pia", "pia-pass-1") for _ in range(n)]
            accounts.hashes = authmod.Budget(n)
            answers = []

            def wake(token):
                answers.append(call("/api/session/reauth",
                                    {"password": "pia-pass-1"}, token)[0])
            threads = [threading.Thread(target=wake, args=(t,))
                       for t in tokens for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            accounts.hashes = authmod.Budget(n)
            accounts.throttle = authmod.Throttle()
            return (answers.count(200) <= n and len(answers) == 2 * n,
                    f"{answers.count(200)} of {len(answers)} parallel right "
                    f"passwords accepted (budget {n})")
        attempt("password checks sent in parallel cannot overspend the "
                "account's budget", parallel_checks_budgeted)

        def guesses_hold_no_sign_in():
            accounts.throttle = authmod.Throttle()
            n = authmod.HASHES_PER_MINUTE
            person("quin", "viewer")
            accounts.hashes = authmod.Budget(n)
            slots = authmod._scrypt_slots
            held = [slots.acquire(timeout=5) for _ in range(authmod.SCRYPT_AT_ONCE)]
            guesses = [threading.Thread(
                target=lambda i=i: accounts.login("quin", f"guess-{i}x",
                                                  address=f"10.7.0.{i + 1}"),
                daemon=True) for i in range(n + 2)]
            for thread in guesses:
                thread.start()
            time.sleep(0.3)                      # all waiting for a slot
            answer = {}
            mine = threading.Thread(target=lambda: answer.update(
                accounts.login("quin", "quin-pass-1", address="10.8.0.1")),
                daemon=True)
            mine.start()
            time.sleep(0.2)
            for got in held:
                if got:
                    slots.release()
            mine.join(timeout=30)
            for thread in guesses:
                thread.join(timeout=30)
            accounts.hashes = authmod.Budget(n)
            accounts.throttle = authmod.Throttle()
            return (answer.get("ok") is True,
                    f"her sign-in among {n + 2} strangers' guesses: "
                    f"{answer.get('ok') or answer.get('error')}")
        attempt("strangers' guesses waiting their turn hold none of the "
                "person's sign-ins", guesses_hold_no_sign_in)

        def recovery_without_thread():
            from lab import alarms
            fake_tc600.reset()
            fake_tc600.STATE.update({10: True, 23: True})
            fake_tc600._speed = 1500.0
            start = threading.Thread.start

            def refuse(self, *args, **kwargs):
                if self.name == "auto-recovery":
                    raise RuntimeError("can't start new thread (selftest)")
                return start(self, *args, **kwargs)
            try:
                with Rig(pump_port) as rig:
                    engine_here = runs.RunEngine(database, rig.devices)
                    supervisor = alarms.Supervisor(rig.devices, engine_here,
                                                   database, auto_recover=True)
                    watching(supervisor, 1.5)
                    threading.Thread.start = refuse
                    fake_tc600.STATE[303] = "Err006"
                    watching(supervisor, 6.0)
                    threading.Thread.start = start
                    keys = [a["key"] for a in supervisor.describe()["recent"]]
                    safe = (fake_tc600.STATE[23] is False
                            and str(fake_tc600.STATE[303]).strip() in ("000000", "")
                            and fake_tc600.STATE[10] is True)
            finally:
                threading.Thread.start = start
                fake_tc600.reset()
            return ("recovery_thread" in keys and safe,
                    f"alarms {keys}; motor off, fault cleared, station on: {safe}")
        attempt("a recovery that cannot get a thread makes the pump safe "
                "anyway, and says so", recovery_without_thread)

        def unresolved_is_nobody():
            probe = apimod.Handler.__new__(handler)
            probe.principal = None
            who = probe._who()
            return (not who.at_least("viewer") and who.username != "local",
                    f"{who.username} as {who.role}")
        attempt("a request never resolved is nobody, never the local owner",
                unresolved_is_nobody)
    finally:
        server.shutdown()
        server.server_close()
        engine.registry.close()
        database.close()


#: Every section, by the name `--only` takes.
SECTIONS = ("pump", "gauge", "cold_cathode", "analysis", "process", "equipment",
            "deposition", "database", "accounts", "history", "engine", "faults",
            "timing", "lifecycle", "authority", "security", "discovery")


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=SECTIONS,
                        help="run only this section (repeatable)")
    args = parser.parse_args(argv)
    chosen = set(args.only or SECTIONS)

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

    sections = (
        ("pump", lambda: test_pump(pump_port)),
        ("gauge", lambda: test_gauge(gauge_port)),
        ("cold_cathode", test_cold_cathode_interlock),
        ("analysis", test_analysis),
        ("process", test_process),
        ("equipment", test_equipment),
        ("deposition", test_deposition_engine),
        ("database", test_database),
        ("accounts", test_accounts),
        ("history", test_history_and_charts),
        ("engine", lambda: test_run_engine(pump_port, gauge_port)),
        ("faults", lambda: test_faults(pump_port, gauge_port)),
        ("timing", lambda: test_timing(pump_port, gauge_port)),
        ("lifecycle", lambda: test_lifecycle(pump_port, gauge_port)),
        ("authority", lambda: test_authority(pump_port)),
        ("security", lambda: test_security(pump_port)),
        ("discovery", lambda: test_discovery(pump_port, gauge_port)),
    )
    try:
        for name, run in sections:
            if name in chosen:
                run()
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
