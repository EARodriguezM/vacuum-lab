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
        engine = runs.RunEngine(database, reg.Registry(),
                                roles=runs.ALL_ROLES, hardware=hardware,
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
        engine = runs.RunEngine(database, reg.Registry(),
                                roles=runs.ALL_ROLES, hardware=hardware)
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

        # --- a Done names the step it was pressed for ----------------------
        engine.mode = runs.GUIDED
        engine._satisfied, engine._prompt = {}, None
        engine.require(hardware.gas, 0)
        first = engine.prompt() or {}
        engine.acknowledge(prompt_id=first.get("id"))
        engine.require(hardware.gas, 20)
        second = engine.prompt() or {}
        stale = engine.acknowledge(prompt_id=first.get("id"))
        check("every prompt has its own number",
              first.get("id") is not None and second.get("id") is not None
              and first.get("id") != second.get("id"), f"{first.get('id')}, "
              f"{second.get('id')}")
        check("a Done for a step that was replaced is refused, and confirms "
              "nothing", stale.get("ok") is False and stale.get("code") == 409
              and engine.prompt() is not None and not hardware.gas.confirmed,
              stale.get("error", "")[:60])
        current = engine.acknowledge(prompt_id=second.get("id"))
        check("the Done for the step on screen is taken",
              current.get("ok") and engine.prompt() is None
              and hardware.gas.confirmed, str(current)[:60])

        # --- process timers count from the step taking effect ---------------
        # A guided pre-sputter used to count from the phase's start: the
        # time somebody took to walk over and strike the plasma came off it.
        recipe = runs.Deposition({})
        recipe.power, recipe.presputter, recipe.deposit_time = 100.0, 60.0, 120.0
        recipe.plasma_started_at = recipe.deposition_started_at = None
        recipe.deposit_time_done_at = recipe.deposition_ended_at = None
        recipe._recovering_since = None
        engine._satisfied, engine._prompt, engine._confirmed_at = {}, None, {}
        engine._run_started = time.monotonic() - 1000.0
        recipe.enter("presputter")
        recipe.phase_started = time.monotonic() - 70.0     # the walk: 70 s
        waiting = recipe.phase_presputter(engine, {"t": 1000.0})
        timer_before = recipe.timer(1000.0)
        engine.acknowledge(prompt_id=(engine.prompt() or {}).get("id"))
        struck = engine.satisfied_at(hardware.magnetron)
        early = recipe.phase_presputter(engine, {"t": struck + 5})
        check("the pre-sputter counts from the plasma being confirmed, not from "
              "the prompt", waiting is None and early is None
              and recipe.plasma_started_at == struck,
              f"after 5 s of plasma and 75 s in the phase: {early or 'still cleaning'}")
        check("its countdown waits for the plasma",
              timer_before and timer_before["elapsed_s"] is None
              and recipe.timer(struck + 5)["elapsed_s"] == 5.0,
              f"{timer_before} then {recipe.timer(struck + 5)}")
        check("and ends after the time set",
              recipe.phase_presputter(engine, {"t": struck + 60.5}) == "deposit")
        recipe.enter("deposit")
        recipe.phase_deposit(engine, {"t": struck + 61})
        engine.acknowledge(prompt_id=(engine.prompt() or {}).get("id"))
        opened = engine.satisfied_at(hardware.shutter)
        still = recipe.phase_deposit(engine, {"t": opened + 119})
        over = recipe.phase_deposit(engine, {"t": opened + 120.5})
        check("the deposition counts from the shutter being confirmed open",
              recipe.deposition_started_at == opened and still is None
              and over == "recover", f"{still}, then {over}")
        recipe.enter("recover")
        engine._run_started -= 150.0         # closed 150 s after it opened
        recipe.phase_recover(engine, {"t": opened + 121})
        asked = (engine.prompt() or {}).get("note", "")
        engine.acknowledge(prompt_id=(engine.prompt() or {}).get("id"))
        recipe.phase_recover(engine, {"t": opened + 152})
        exposed = recipe.deposition_ended_at - recipe.deposition_started_at
        check("the deposition ends when the shutter is confirmed closed, and "
              "the record says it ran over", recipe.deposition_ended_at
              == engine.satisfied_at(hardware.shutter) and 145 < exposed < 160,
              f"exposed {exposed:.0f} s for 120 s set")
        check("the prompt says why this step, now",
              "deposition time is over" in asked, asked[:60])
        recipe.base_mbar = recipe.base_reached_at = recipe.crossover = None
        recipe.crossed_at = recipe.full_speed_at = None
        recipe.working, recipe.argon, recipe.gate_open = 5e-3, 20.0, 100.0
        summary = recipe.summarise(engine, [{"t": opened + 152, "phase": "recover",
                                             "p_best": 5e-3}])
        check("the result quotes the exposure, not the time set",
              145 < (summary.get("deposition_s") or 0) < 160
              and summary.get("deposition_complete") is True
              and summary.get("shutter_closed") is True,
              f"{summary.get('deposition_s')} s, complete "
              f"{summary.get('deposition_complete')}")

        # --- what an ending leaves to a person ------------------------------
        manual = process.ProcessHardware()
        try:
            manual.magnetron.set(100)
        except process.OperatorRequired:
            manual.magnetron.confirm()               # the plasma is burning
        try:
            manual.shutter.set(False)
        except process.OperatorRequired:
            manual.shutter.confirm()                 # closed, and confirmed
        steps = []
        manual.safe_shutdown(steps=steps)
        check("an ending leaves the plasma to a person, and nothing that was "
              "never on or is already confirmed off",
              [s["actuator"] for s in steps] == ["magnetron"]
              and manual.shutter.confirmed and manual.shutter.requested is False,
              str([s["actuator"] for s in steps]))
        engine.leave_by_hand(steps)
        listed = engine.checklist()
        refused = engine.start("hold", {"duration_min": 1})
        check("no run starts while a step is left by hand",
              listed and refused.get("ok") is False
              and "by hand" in refused.get("error", ""),
              refused.get("error", "")[:70])
        wrong = engine.confirm_step(-1)
        done = engine.confirm_step(listed[0]["id"]) if listed else {}
        check("confirming the step takes it off the list, and only that step",
              wrong.get("code") == 409 and done.get("ok")
              and engine.checklist() == [], f"{wrong.get('code')}, {done}")

        engine._flush()                  # its log, written before the close
        database.close()
    _left_by_hand_survives()


def _left_by_hand_survives() -> None:
    """What a run leaves by hand survives a crash and a restart; a Done
    pressed as a run ends confirms nothing; a mark does not hold runs back."""
    import threading
    from lab import alarms, process, registry as reg
    from tools import fake_actuators
    from tools.checks.common import wait_until

    with tempfile.TemporaryDirectory() as directory:
        # --- a deposition cut off by a crash: every manual step is owed ------
        path = os.path.join(directory, "crash.db")
        database = dbmod.Database(path)
        cut = database.create_run("deposition", "Cut off", {})   # left running
        database.close()
        database = dbmod.Database(path)
        engine = runs.RunEngine(database, reg.Registry(), roles=runs.ALL_ROLES,
                                hardware=process.ProcessHardware())
        listed = sorted(step["actuator"] for step in engine.checklist())
        # Once its own clean-up has been tried, the checklist still holds runs.
        wait_until(lambda: not (engine._orphan_thread
                                and engine._orphan_thread.is_alive()), 10, 0.05)
        refused = engine.start("hold", {"duration_min": 1, "interval_s": 1})
        stored = (database.run(cut["id"])["result"] or {}).get("by_hand") or []
        check("a deposition cut off by a crash leaves all three steps by hand, "
              "and no run starts", listed == ["gas", "magnetron", "shutter"]
              and not refused.get("ok") and "by hand" in refused.get("error", "")
              and len(stored) == 3,
              f"{listed}; start {refused.get('error', 'started')[:40]}")
        engine.close(timeout=5)
        database.close()

        # --- ended by a restart with a step owed: rebuilt, and alarmed -------
        path = os.path.join(directory, "restart.db")
        database = dbmod.Database(path)
        ended = database.create_run("deposition", "Ended by a restart", {})
        step = {"actuator": "magnetron", "label": "Magnetron",
                "instruction": "Switch the magnetron power supply OFF.",
                "value": "0 W"}
        database.finish_run(ended["id"], dbmod.ABORTED,
                            {"interrupted": True, "reported": False,
                             "by_hand": [step]})
        engine = runs.RunEngine(database, reg.Registry(), roles=runs.ALL_ROLES,
                                hardware=process.ProcessHardware())
        supervisor = alarms.Supervisor(reg.Registry(), engine, database)
        supervisor._watch_orphans()
        alarm = next((a for a in supervisor.describe()["alarms"]
                      if a.get("title", "").startswith(f"Run {ended['id']}")), {})
        rebuilt = engine.checklist()
        check("a step left by hand comes back after a restart, and its alarm "
              "is critical", [s["actuator"] for s in rebuilt] == ["magnetron"]
              and alarm.get("level") == "critical"
              and "magnetron" in alarm.get("detail", "").lower(),
              f"{[s['actuator'] for s in rebuilt]}; {alarm.get('level')}")
        engine.confirm_step(rebuilt[0]["id"]) if rebuilt else None
        engine._flush()
        check("once confirmed, its run's record owes it no longer",
              (database.run(ended["id"])["result"] or {}).get("by_hand") == [])
        engine.close(timeout=5)
        database.close()

        # --- found by id, not by date; kept until confirmed ------------------
        path = os.path.join(directory, "clock.db")
        database = dbmod.Database(path)
        behind = None
        for n in range(25):
            run = database.create_run("hold", f"Run {n}", {})
            database.finish_run(run["id"], dbmod.COMPLETED,
                                {"by_hand": [step]} if n == 24 else {})
            behind = run["id"]
        with database._lock:                 # started while the clock was behind
            database._db.execute("UPDATE runs SET started_at=started_at-86400 "
                                 "WHERE id=?", (behind,))
            database._db.commit()
        engine = runs.RunEngine(database, reg.Registry(), roles=runs.ALL_ROLES,
                                hardware=process.ProcessHardware())
        found = [s["run_id"] for s in engine.checklist()]
        held = engine.delete_run(behind)
        check("a run's steps by hand are found after a restart however its "
              "clock was set, and the run cannot be deleted until they are "
              "confirmed", found == [behind] and held.get("code") == 409,
              f"rebuilt for {found}; delete {held.get('code')}")
        second = {"actuator": "gas", "label": "Argon flow",
                  "instruction": "Close the argon valve completely.", "value": "0 sccm"}
        engine._rebuild_by_hand(behind, [second])
        for left in engine.checklist():
            engine.confirm_step(left["id"])
        engine._flush()
        kept = (database.run(behind)["result"] or {}).get("by_hand")
        gone = engine.delete_run(behind)
        check("two steps confirmed one after the other leave none stored; then "
              "the run may be deleted", kept == [] and gone.get("ok"),
              f"stored {kept}; delete {gone.get('error') or 'ok'}")

        # --- a mark that cannot be written in time changes nothing ----------
        owed = database.create_run("bakeout", "Slow card", {})
        database.finish_run(owed["id"], dbmod.ABORTED, {"cleanup_pending": True})
        engine.MARK_WAIT_S = 0.3
        engine._write(lambda: time.sleep(1.5), "a slow card")
        slow = engine.mark_cleanup_checked(owed["id"], "selftest: checked by eye")
        engine._flush()
        after = database.run(owed["id"])["result"] or {}
        check("a mark whose record is not written in time says so, and does not "
              "land later", slow.get("code") == 503
              and after.get("cleanup_pending") is True
              and "cleanup_checked" not in after,
              f"{slow.get('code')}; pending {after.get('cleanup_pending')}")
        engine.close(timeout=5)
        database.close()

        # --- a Done pressed while the run ends confirms nothing --------------
        database = dbmod.Database(os.path.join(directory, "late.db"))
        hardware = process.ProcessHardware()
        engine = runs.RunEngine(database, reg.Registry(), roles=runs.ALL_ROLES,
                                hardware=hardware)
        entered, release = threading.Event(), threading.Event()

        class Slow(runs.Procedure):
            name, label, phases, interval = "selftest_slow", "Slow", \
                ("running", "done"), 0.2

            def step(self, engine, sample):
                engine.require(engine.hardware.magnetron, 100)
                return False

            def timer(self, now):
                raise RuntimeError("selftest: a broken timer")

            def finish(self, engine):
                entered.set()
                release.wait(5)
                return runs.Deposition.finish(self, engine)

        runs.CATALOGUE[Slow.name] = Slow
        try:
            engine.mode = runs.GUIDED
            engine.start(Slow.name, {})
            wait_until(lambda: engine.prompt() is not None, 10, 0.05)
            shown = engine.prompt() or {}
            status = engine.status()
            engine.stop("selftest")
            entered.wait(5)
            late = engine.acknowledge(prompt_id=shown.get("id"))
            release.set()
            wait_until(lambda: not engine.status()["running"], 10, 0.05)
            check("the state poll survives a recipe whose timer fails",
                  status.get("running") and status.get("timer") is None)
            check("a Done pressed while the run ends is refused, and the plasma "
                  "is not recorded as confirmed off",
                  late.get("ok") is False and not hardware.magnetron.confirmed
                  and [s["actuator"] for s in engine.checklist()] == ["magnetron"],
                  late.get("error", "taken")[:50])
            requested = engine.prompt()
            hardware.magnetron.requested, hardware.magnetron.confirmed = 0, False
            engine._prompt = {**shown}
            stale = engine.acknowledge(prompt_id=shown.get("id"))
            check("a Done for a value the actuator is no longer asked for is "
                  "refused", requested is None and stale.get("code") == 409
                  and not hardware.magnetron.confirmed, str(stale)[:60])
            engine._prompt = None
        finally:
            release.set()
            del runs.CATALOGUE[Slow.name]
            for left in engine.checklist():
                engine.confirm_step(left["id"])
            engine.close(timeout=5)
            database.close()

        # --- a mark does not hold the next run back --------------------------
        database = dbmod.Database(os.path.join(directory, "mark.db"))
        engine = runs.RunEngine(database, reg.Registry(), roles=runs.ALL_ROLES,
                                hardware=process.ProcessHardware())
        owed = database.create_run("bakeout", "Heater, by hand", {})
        database.finish_run(owed["id"], dbmod.ABORTED, {"cleanup_pending": True})
        trying = engine._remember(owed, "trying again", None, "retry")
        engine._start_cleanups()
        wait_until(lambda: trying["attempts"] >= 1, 10, 0.05)   # no pump: fails
        supervisor = alarms.Supervisor(reg.Registry(), engine, database)
        supervisor.raise_alarm(f"run_{owed['id']}_cleanup_failed", "critical",
                               "selftest", "its clean-up failed")
        mark = engine.mark_cleanup_checked(owed["id"], "selftest: the TMS reads off")
        supervisor._watch_orphans()
        alarm = next((a for a in supervisor.describe()["alarms"]
                      if a.get("title") == "selftest"), {})
        started = engine.start("hold", {"duration_min": 1, "interval_s": 1})
        check("a run starts straight after an owed clean-up is marked done by "
              "hand, and its alarm is cleared",
              mark.get("ok") and started.get("ok") and alarm.get("active") is False,
              f"mark {mark.get('error') or 'ok'}; start "
              f"{started.get('error') or 'ok'}; alarm active {alarm.get('active')}")
        engine.stop("selftest")
        wait_until(lambda: not engine.status()["running"], 15, 0.1)
        engine.close(timeout=5)
        database.close()

    # --- a driven shutter closed by the ending ends the deposition then ------
    hardware = process.ProcessHardware()
    fake_actuators.attach(hardware, fake_tpg.Chamber())
    recipe = runs.Deposition({})
    recipe.deposition_started_at, recipe.deposition_ended_at = 10.0, None
    stub = runs.RunEngine.__new__(runs.RunEngine)
    stub.hardware, stub._run_started = hardware, time.monotonic() - 50.0
    stub.leave_by_hand = lambda steps: None
    stub.log = lambda *args, **kwargs: None
    hardware.shutter.set(True)
    recipe.finish(stub)
    check("a driven shutter closed as the run ends ends the deposition there",
          recipe.deposition_ended_at is not None
          and 49 < recipe.deposition_ended_at < 52,
          str(recipe.deposition_ended_at))
