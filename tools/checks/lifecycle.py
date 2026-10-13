"""How a run starts, ends and cleans up when the database, the service or
the process fails around it."""

from __future__ import annotations

import math
import os
import sys
import tempfile
import threading
import time

from lab import db as dbmod
from lab import export, registry, runs
from tools import fake_tc600, fake_tpg
from tools.checks.common import (
    ROOT, wait_until, attempt, Rig, watching, FailingDatabase, _try, _QuietEngine)


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
                first = runs.RunEngine(database, rig.devices)
                time.sleep(4.0)
                first.close(timeout=5)            # ... and the panel stops again
            fake_tc600.FAULTS.clear()             # restarted again, pump back
            with Rig(pump_port) as rig:
                second = runs.RunEngine(database, rig.devices)
                off = wait_until(lambda: fake_tc600.STATE[1] is False, 15, 0.1)
                again = any(o["id"] == cut_off["id"] for o in second.orphans)
                second.close(timeout=5)
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

    def at_base(config=None):
        engine = Nominal()
        recipe = runs.BaseCalibration({"base_rate_pct": 2, "base_settle_min": 0.5,
                                       **(config or {})})
        recipe.begin(engine)
        recipe.enter("basepressure")
        return engine, recipe

    def flat(t, pressure=4.0e-6, speed=1500.0):
        return {"t": float(t), "p_best": pressure, "speed": speed}

    def dropout_at_base():
        # The control: the same flat curve, read throughout, names the sample
        # base pressure is called on -- whatever the thresholds say.
        engine, control = at_base()
        due = next((t for t in range(0, 600, 2)
                    if control.phase_basepressure(engine, flat(t)) == "done"), None)
        if due is None:
            return False, "the control never reached base pressure"
        engine, recipe = at_base()
        outcome = None
        for t in range(0, due, 2):                 # flat, at full speed
            outcome = recipe.phase_basepressure(engine, flat(t))
        dropped = recipe.phase_basepressure(       # the settle ends: no reading
            engine, flat(due, pressure=None))
        after = recipe.phase_basepressure(engine, flat(due + 2))
        return (outcome is None and dropped is None and after == "done"
                and recipe.base_mbar == 4.0e-6,
                f"due at {due} s: {dropped!r} on the dropout there, "
                f"{after!r} after, base {recipe.base_mbar}")
    attempt("a gauge dropout as base pressure is reached does not crash the run",
            dropout_at_base)

    def base_at(interval, settle_min=0.5, curve=None):
        """When base pressure is called, counted from the phase's start, on a
        curve sampled the way the engine steps: on a deadline every
        `interval` s, a tick late at times, the first reading one interval
        in, one reading lost on the way."""
        engine, recipe = at_base({"interval_s": interval,
                                  "base_settle_min": settle_min})
        for n in range(1, 2000):
            t = n * interval + (0.9 if n % 3 == 0 else 0.0)
            pressure = curve(t) if curve else 4.0e-6
            sample = flat(t, pressure=None if n == 7 else pressure)
            if recipe.phase_basepressure(engine, sample) == "done":
                return t
        return None

    def slow_sampling():
        found = {i: base_at(i) for i in (2, 30, 40, 60)}
        return (all(v is not None for v in found.values()),
                ", ".join(f"every {i} s: " + (f"{v:.0f} s" if v is not None
                                              else "never")
                          for i, v in found.items()))
    attempt("base pressure is reached at every sample interval the form allows",
            slow_sampling)

    def full_vacuum_rates():
        engine = Nominal()
        recipe = runs.FullVacuum({"interval_s": 60})
        recipe.begin(engine)
        recipe.enter("basepressure")
        rates, t = [], 0.0
        for n in range(30):
            sample = flat(t)
            recipe.step(engine, sample)
            rates.append(sample.get("rate_pct_min"))
            t += 60.0
        quoted = [r for r in rates if r is not None]
        return (len(quoted) > 10, f"{len(quoted)} of {len(rates)} samples "
                                  f"carried a rate, sampling once a minute")
    attempt("a full vacuum sampling once a minute still quotes its rate",
            full_vacuum_rates)

    def minimum_suffices():
        missed = []
        for interval in range(1, 61):
            for settle in (0.5, 3.0):
                took = base_at(interval, settle)
                named = runs.VacuumSequence.base_minimum_s(interval, settle * 60)
                if took is None or took > named:
                    missed.append(f"{interval} s/{settle:g} min: "
                                  f"{took} > {named:.0f}")
        return not missed, "; ".join(missed[:3]) or "every interval, 1 to 60 s"
    attempt("the shortest base timeout allowed is long enough on a flat curve",
            minimum_suffices)

    def turned_upward():
        # Falling 3 %/min for an hour, then rising 2.5 %/min -- a leak
        # opening -- sampled once a minute.
        turn = 3600.0

        def curve(t):
            if t < turn:
                return 1.0e-5 * math.exp(-0.03 / 60 * t)
            return 1.0e-5 * math.exp(-0.03 / 60 * turn
                                     + 0.025 / 60 * (t - turn))
        engine, recipe = at_base({"interval_s": 60, "base_settle_min": 3,
                                  "base_rate_pct": 2})
        called = None
        for n in range(1, 120):
            t = n * 60.0
            if recipe.phase_basepressure(engine, flat(t, pressure=curve(t))) \
                    == "done":
                called = t
                break
        return (called is None,
                f"called at {called - turn:.0f} s after the turn" if called
                else "not called while the pressure rose")
    attempt("at long intervals a curve that has just turned upward is not "
            "base pressure", turned_upward)

    def short_timeout_refused():
        _, short = runs.settle_config(runs.BaseCalibration,
                                      {"base_timeout_min": 1})
        _, slow = runs.settle_config(runs.BaseCalibration,
                                     {"base_timeout_min": 3, "interval_s": 60})
        _, fine = runs.settle_config(runs.BaseCalibration, {})
        return (short and slow and not fine,
                (short or ["accepted"])[0][:90])
    attempt("a base timeout too short ever to find base pressure is refused",
            short_timeout_refused)

    def timeout_says_why():
        """Each curve built first, then the deadline passes on its last reading."""
        curves = {
            "no reading": [flat(t, pressure=None) for t in range(0, 60, 2)],
            "too few": [flat(t) for t in range(0, 6, 2)],
            "flat": [flat(t) for t in range(0, 62, 2)],
            "falling": [flat(t, pressure=1.0e-3 * math.exp(-t / 60.0))
                        for t in range(0, 62, 2)],
        }
        said = {}
        for case, samples in curves.items():
            engine, recipe = at_base()
            for sample in samples[:-1]:
                recipe.phase_basepressure(engine, sample)
            recipe.base_timeout = 60.0
            recipe.phase_started -= 61.0             # the deadline has passed
            try:
                recipe.phase_basepressure(engine, samples[-1])
                said[case] = "no abort"
            except runs.Abort as stop:
                said[case] = str(stop)
        expected = {"no reading": "without a usable pressure reading",
                    "too few": "too few readings", "flat": "stayed flat for only",
                    "falling": "still improving"}
        return (all(expected[k] in said[k] for k in expected),
                " | ".join(f"{k}: {said[k][17:70]}" for k in said))
    attempt("a base-pressure timeout says why: no reading, too few readings, "
            "too short a flat, or still improving", timeout_says_why)

    def lost_speed_at_base():
        engine, recipe = at_base()
        logged = []
        engine.log = lambda level, message, *a, **k: logged.append(message)
        t, outcome = 0, None
        while outcome is None and t < 600:
            # Every fifth speed reading lost, as on a bus that drops replies.
            outcome = recipe.phase_basepressure(
                engine, flat(t, speed=None if t % 10 == 0 else 1500.0))
            t += 2
        dragged = [m for m in logged if "held below full speed" in m]
        return (outcome == "done" and not dragged,
                f"base at {t - 2} s; {len(dragged)} 'held below' messages")
    attempt("a lost speed reading at base pressure is not a rotor held down",
            lost_speed_at_base)

    def falling_rise():
        engine = _QuietEngine()
        recipe = runs.RateOfRise({})
        recipe.begin(engine)
        recipe.fit_from = 0.0
        samples = [{"t": float(t), "p_best": 1.0e-4 - 1.0e-7 * t}
                   for t in range(0, 60, 2)]
        summary = recipe.summarise(engine, samples)
        return (summary.get("verdict") == "falling"
                and summary.get("leak_rate_mbar_l_s") is None
                and "gate" in summary.get("caveat", ""),
                f"verdict {summary.get('verdict')}, leak rate "
                f"{summary.get('leak_rate_mbar_l_s')}")
    attempt("a rate of rise that falls is not reported as a leak rate",
            falling_rise)

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
