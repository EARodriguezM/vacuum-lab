"""The SQLite store, and the rolling chart recorder with its decimation."""

from __future__ import annotations

import os
import tempfile
import time

from lab import db as dbmod
from lab import pump as pumpmod
from lab import export, registry, runs
from tools import fake_tc600
from tools.checks.common import (
    check, _QuietEngine)


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
        runs.RunEngine(database, registry.Registry(), roles=runs.ALL_ROLES)
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
            engine = runs.RunEngine(database, registry.Registry(),
                                    roles=runs.ALL_ROLES)
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
