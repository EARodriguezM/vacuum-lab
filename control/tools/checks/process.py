"""The process hardware (argon, gate valve, magnetron, shutter), which of it
is installed on the rig, and the deposition recipe's defaults."""

from __future__ import annotations

import os
import tempfile
import time

from lab import db as dbmod
from lab import runs
from tools import fake_tpg
from tools.checks.common import (
    check, _QuietEngine)


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
