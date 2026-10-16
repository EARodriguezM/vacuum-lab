"""What the panel says about itself: errors logged instead of swallowed, its
threads' heartbeats, alarms that outlive a restart, the restart itself, the
backups, and the status file a release reads."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from lab import db as dbmod
from lab import logs, registry, runs
from tools.checks.common import attempt, check, wait_until


def _rig(directory: str, **options):
    from lab import alarms
    database = dbmod.Database(os.path.join(directory, "lab.db"))
    engine = runs.RunEngine(database, registry.Registry(), roles=runs.ALL_ROLES)
    supervisor = alarms.Supervisor(registry.Registry(), engine, database,
                                   **options)
    return database, engine, supervisor


def _stop(database, engine, supervisor, clean: bool = True) -> None:
    """Stop as the panel does (Application.close), or as a crash does."""
    if clean:
        supervisor.close()
    else:
        supervisor._stop.set()           # as if the process died
        if supervisor._thread is not None:
            supervisor._thread.join(5)
    engine._flush(10)
    engine.close(timeout=5)
    if clean:
        supervisor.mark_clean_stop()
    database.close()


#: The section's own scratch folder, removed when it ends.
_ROOT: list[str] = []


def _scratch() -> str:
    return tempfile.mkdtemp(dir=_ROOT[-1])


def test_observability() -> None:
    _ROOT.append(tempfile.mkdtemp(prefix="selftest-observe-"))
    try:
        _sections()
    finally:
        shutil.rmtree(_ROOT.pop(), ignore_errors=True)


def _sections() -> None:
    print("\nWhat the panel says about itself")
    from lab import alarms, history

    # --- an error is said once, with its traceback ------------------------------
    def logged_once():
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink):
            for n in range(12):
                try:
                    raise ValueError("selftest boom")
                except ValueError as exc:
                    logs.error("selftest", exc)
        text = sink.getvalue()
        lines = text.splitlines()
        return (lines and lines[0].startswith("<3>selftest: ValueError")
                and "Traceback" in text and "10 times now" in text
                # said twice -- the first time and the tenth -- of twelve
                and sum(line.startswith("<3>selftest: ValueError")
                        for line in lines) == 2,
                f"{len(lines)} lines for 12 errors")
    attempt("an error is logged the first time with its traceback, at error "
            "priority, and counted after", logged_once)

    def same_place_new_text():
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink):
            for n in range(5):
                try:
                    raise KeyError(f"selftest key {n}")    # a new message each time
                except KeyError as exc:
                    logs.error("selftest-place", exc)
        firsts = sum(line.startswith("<3>selftest-place: KeyError")
                     for line in sink.getvalue().splitlines())
        return firsts == 1, f"{firsts} full reports for 5 errors from one line"
    attempt("an error whose message changes is still one kind, by the line "
            "that raised it", same_place_new_text)

    def unprintable():
        class Broken(Exception):
            def __str__(self):
                raise RuntimeError("no text")
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink):
            try:
                raise Broken()
            except Broken as exc:
                logs.error("selftest-broken", exc)
        return "selftest-broken" in sink.getvalue(), sink.getvalue()[:60]
    attempt("an exception that cannot be printed is still logged, and the "
            "logger never raises", unprintable)

    def watchers_survive():
        directory = _scratch()
        database, engine, supervisor = _rig(directory)
        recorder = history.History(registry.Registry(), interval=0.1)

        def broken(*args, **kwargs):
            raise RuntimeError("selftest watcher")
        supervisor.INTERVAL_S = 0.1
        supervisor.check = broken
        recorder.record = broken
        sink = io.StringIO()
        with contextlib.redirect_stderr(sink):
            supervisor.start()
            recorder.start()
            time.sleep(0.6)
            alive = supervisor._thread.is_alive() and recorder._thread.is_alive()
            beating = (supervisor.beat_age() or 99) < 1 and \
                (recorder.beat_age() or 99) < 1
            recorder.close()
            _stop(database, engine, supervisor)
        said = sink.getvalue()
        return (alive and beating and "supervisor" in said and "history" in said,
                f"alive {alive}, beating {beating}, logged "
                f"{said.count('selftest watcher')} times")
    attempt("the supervisor and the chart recorder outlive an exception, say "
            "so, and keep beating", watchers_survive)

    # --- alarms and the restart --------------------------------------------------
    def alarms_outlive_restart():
        directory = _scratch()
        database, engine, supervisor = _rig(directory, persist=True)
        supervisor.raise_alarm("selftest_fault", "critical", "A selftest fault",
                               "Raised before the restart.")
        supervisor.raise_alarm("selftest_seen", "warning", "Seen already",
                               "Acknowledged before the restart.")
        supervisor.acknowledge(
            next(a["id"] for a in supervisor.describe()["alarms"]
                 if a["key"] == "selftest_seen"))
        _stop(database, engine, supervisor)
        database, engine, supervisor = _rig(directory, persist=True)
        shown = {a["key"]: a for a in supervisor.describe()["alarms"]}
        _stop(database, engine, supervisor)
        fault = shown.get("selftest_fault") or {}
        return (fault and not fault.get("active") and fault.get("restored")
                and "selftest_seen" not in shown,
                f"after the restart: {sorted(shown)}")
    attempt("an unacknowledged alarm is still on screen after a restart; an "
            "acknowledged one is not", alarms_outlive_restart)

    def ids_and_notes_across_restarts():
        directory = _scratch()
        database, engine, supervisor = _rig(directory, persist=True)
        first = supervisor.raise_alarm("selftest_a", "warning", "A", "Before.")
        _stop(database, engine, supervisor)
        seen = []
        for _ in range(2):
            database, engine, supervisor = _rig(directory, persist=True)
            new = supervisor.raise_alarm("selftest_b", "warning", "B", "Now.")
            seen.append((new["id"], [a for a in supervisor.describe()["alarms"]
                                     if a["key"] == "selftest_a"]))
            _stop(database, engine, supervisor)
        ids = [first["id"]] + [n for n, _ in seen]
        details = [old[0]["detail"] for _, old in seen if old]
        return (len(set(ids)) == len(ids) and ids == sorted(ids)
                and all(d.count("before the panel was restarted") == 1
                        for d in details) and len(details) == 2,
                f"ids {ids}; notes per detail "
                f"{[d.count('before the panel was restarted') for d in details]}")
    attempt("alarm ids never repeat across restarts, and a kept alarm says so "
            "once", ids_and_notes_across_restarts)

    def malformed_store():
        directory = _scratch()
        database = dbmod.Database(os.path.join(directory, "lab.db"))
        database.set_meta("alarms", json.dumps(
            [None, "pump_offline", {"key": "x", "detail": ["a", "list"]},
             {"key": "good", "level": "critical", "title": "Kept",
              "detail": "Fine."}]))
        database.set_meta("alive", json.dumps(["not", "a", "dict"]))
        database.close()
        try:
            database, engine, supervisor = _rig(directory, persist=True)
        except Exception as exc:                            # noqa: BLE001
            return False, f"the panel would not start: {type(exc).__name__}: {exc}"
        supervisor.check()
        keys = [a["key"] for a in supervisor.describe()["alarms"]]
        _stop(database, engine, supervisor)
        return ("good" in keys and "panel_restarted" not in keys,
                f"started; kept {keys}")
    attempt("whatever the database holds from before, the panel starts, and "
            "keeps what it can read", malformed_store)

    def restart_announced():
        found = {}
        for clean in (True, False):
            directory = _scratch()
            database, engine, supervisor = _rig(directory, persist=True)
            supervisor.start()
            wait_until(lambda: database.meta("alive") is not None, 5, 0.1)
            _stop(database, engine, supervisor, clean=clean)
            database, engine, supervisor = _rig(directory, persist=True)
            supervisor.check()
            restart = [a for a in supervisor.describe()["alarms"]
                       if a["key"] == "panel_restarted"]
            found[clean] = restart[0]["title"] if restart else None
            _stop(database, engine, supervisor)
        return (found[True] and found[False] and found[True] != found[False]
                and "unexpectedly" in found[False],
                f"clean: {found[True]}; crash: {found[False]}")
    attempt("a restart raises an alarm saying how long nothing watched, and "
            "whether it stopped cleanly", restart_announced)

    def first_start_quiet():
        directory = _scratch()
        database, engine, supervisor = _rig(directory, persist=True)
        supervisor.check()
        keys = [a["key"] for a in supervisor.describe()["alarms"]]
        _stop(database, engine, supervisor)
        return "panel_restarted" not in keys, str(keys)
    attempt("a panel starting for the first time is not a restart",
            first_start_quiet)

    def clean_stop_through_close():
        from lab import api as apimod
        directory = _scratch()
        database, engine, supervisor = _rig(directory, persist=True)
        supervisor.start()
        app = apimod.Application(registry.Registry(), database, engine,
                                 supervisor=supervisor)
        app.close(budget=20)
        database, engine, supervisor = _rig(directory, persist=True)
        supervisor.check()
        restart = [a for a in supervisor.describe()["alarms"]
                   if a["key"] == "panel_restarted"]
        state = {"title": restart[0]["title"], "active": restart[0]["active"],
                 "level": restart[0]["level"]} if restart else None
        _stop(database, engine, supervisor)
        return (state and state["level"] == "warning" and not state["active"],
                str(state))
    attempt("a panel stopped the way systemd stops it starts again with a "
            "warning, not a crash, and the restart is never an active alarm",
            clean_stop_through_close)

    def recorder_stall():
        directory = _scratch()
        recorder = history.History(registry.Registry(), interval=0.1)
        database, engine, supervisor = _rig(directory, recorder=recorder)
        recorder.beat = time.monotonic() - 60          # as if it had stopped
        supervisor.check()
        stalled = [a for a in supervisor.describe()["alarms"]
                   if a["key"] == "recorder_stalled" and a["active"]]
        recorder.beat = time.monotonic()
        supervisor.check()
        cleared = not [a for a in supervisor.describe()["recent"]
                       if a["key"] == "recorder_stalled" and a["active"]]
        _stop(database, engine, supervisor)
        return bool(stalled) and cleared, \
            f"raised {bool(stalled)}, cleared once it beat again {cleared}"
    attempt("a chart recorder that stops beating raises an alarm, cleared "
            "when it beats again", recorder_stall)

    # --- backups ----------------------------------------------------------------------
    def backup_alarm():
        directory = _scratch()
        status = os.path.join(directory, "backup-status.json")
        database, engine, supervisor = _rig(directory, backup_status=status)
        seen = {}

        def look(label, content):
            if content is None:
                if os.path.exists(status):
                    os.remove(status)
            else:
                with open(status, "w") as fh:
                    json.dump(content, fh)
            supervisor._backup_checked = 0.0
            supervisor.check()
            alarm = [a for a in supervisor.describe()["recent"]
                     if a["key"] == "backup" and a["active"]]
            seen[label] = alarm[0]["title"] if alarm else None

        now = time.time()
        look("none", None)
        look("failed", {"last_attempt": now, "last_ok": False,
                        "last_error": "stick not mounted",
                        "last_success": now - 3600})
        look("stale", {"last_attempt": now - 50 * 3600, "last_ok": True,
                       "last_success": now - 50 * 3600})
        look("fresh", {"last_attempt": now - 600, "last_ok": True,
                       "last_success": now - 600})
        _stop(database, engine, supervisor)
        return (seen["none"] and seen["failed"] and seen["stale"]
                and seen["fresh"] is None,
                "; ".join(f"{k}: {v}" for k, v in seen.items()))
    attempt("no backup, a failed one or one over 48 h old raises an alarm; a "
            "fresh one clears it", backup_alarm)

    def no_backup_alarm_without_path():
        directory = _scratch()
        database, engine, supervisor = _rig(directory)
        supervisor.check()
        keys = [a["key"] for a in supervisor.describe()["alarms"]]
        _stop(database, engine, supervisor)
        return "backup" not in keys, str(keys)
    attempt("the simulator, with no backup status to watch, raises no backup "
            "alarm", no_backup_alarm_without_path)

    # --- the status file and the health block -------------------------------------------
    def status_file():
        from lab import api as apimod
        directory = _scratch()
        database, engine, supervisor = _rig(directory)
        path = os.path.join(directory, "run", "status.json")
        app = apimod.Application(registry.Registry(), database, engine,
                                 supervisor=supervisor, status_file=path,
                                 version={"release": "selftest-1"})
        app.write_status()
        with open(path) as fh:
            written = json.load(fh)
        mode = oct(os.stat(path).st_mode & 0o777)
        health = app.state()["health"]
        _stop(database, engine, supervisor)
        wanted = {"written", "release", "run", "shutdown", "securing", "ports",
                  "critical"}
        return (wanted <= set(written) and written["release"] == "selftest-1"
                and written["run"] is None and mode == "0o600"
                and {"supervisor_s", "history_s", "errors"} <= set(health),
                f"keys {sorted(written)}, {mode}, health {sorted(health)}")
    attempt("the status file says what a release needs to know, privately, and "
            "the state poll carries the threads' heartbeats", status_file)

    # --- the morning report ------------------------------------------------------------
    def morning_report():
        from tools import report as reportmod
        directory = _scratch()
        path = os.path.join(directory, "lab.db")
        database = dbmod.Database(path)
        try:
            run = database.create_run("bakeout", "Night bake", {}, operator="me")
            database.finish_run(run["id"], dbmod.FAILED,
                                {"cleanup_pending": True, "message": "cut"})
            database.add_event(None, "error", "ALARM: Drive fault Err006. Tripped.")
            database.add_event(None, "warning",
                               "Adam: interlock overridden for pump.motor_on: "
                               "pressure_unknown",
                               {"reason": "gauge unplugged, chamber roughed"},
                               user="adam")
        finally:
            database.close()
        with open(os.path.join(directory, "backup-status.json"), "w") as fh:
            json.dump({"last_attempt": time.time(), "last_ok": True,
                       "last_success": time.time()}, fh)
        text, attention = reportmod.report(Path(path), time.time() - 3600)
        wanted = ("Night bake", "CRITICAL", "Drive fault Err006",
                  "gauge unplugged", "CLEAN-UP OWED")
        missing = [w for w in wanted if w not in text]
        return (not missing and attention,
                f"missing {missing}" if missing else
                "runs, alarms, overrides and owed clean-ups reported; flagged")
    attempt("the morning report names the night's runs, alarms, overrides and "
            "what is owed, and says it needs a person", morning_report)

    def waiting_step_alarm():
        directory = _scratch()
        database, engine, supervisor = _rig(directory)
        try:
            supervisor.PROMPT_ALARM_S = 0.3
            engine._prompt = {"id": 41, "actuator": "gas", "want": "20",
                              "label": "Argon flow", "value": "20 sccm",
                              "instruction": "Open the argon needle valve.",
                              "since": time.time()}
            supervisor.check()
            early = [a for a in supervisor.describe()["alarms"]
                     if a["key"].startswith("prompt_")]
            time.sleep(0.4)
            supervisor.check()
            raised = [a for a in supervisor.describe()["alarms"]
                      if a["key"] == "prompt_41"]
            engine._prompt = None                  # confirmed, by Done
            engine.last_confirmed = {"id": 41, "user": None}
            supervisor.check()
            after = next((a for a in supervisor.describe()["recent"]
                          if a["key"] == "prompt_41"), {})
            # A new step starts its own wait; a run ending under one leaves
            # its alarm for somebody to acknowledge.
            engine._prompt = {"id": 42, "label": "Magnetron",
                              "instruction": "Strike the plasma."}
            supervisor.check()
            fresh = [a for a in supervisor.describe()["alarms"]
                     if a["key"] == "prompt_42"]
            time.sleep(0.4)
            supervisor.check()
            engine._prompt = None                  # the run ended instead
            supervisor.check()
            left = next((a for a in supervisor.describe()["alarms"]
                         if a["key"] == "prompt_42"), {})
        finally:
            _stop(database, engine, supervisor)
            shutil.rmtree(directory, ignore_errors=True)
        return (not early and raised and raised[0]["level"] == "critical"
                and "Open the argon" in raised[0]["detail"]
                and after.get("active") is False and after.get("acknowledged")
                and not fresh and left.get("active") is False
                and left.get("acknowledged") is False,
                f"before the limit {len(early)}; after it "
                f"{raised[0]['level'] if raised else 'nothing'}; once Done: "
                f"active {after.get('active')}, acknowledged "
                f"{after.get('acknowledged')}; a new step at once {len(fresh)}; "
                f"ended under it: acknowledged {left.get('acknowledged')}")
    attempt("a guided step waiting past its limit raises a critical alarm; "
            "its Done clears and acknowledges it, a run ending under it only "
            "clears it; the next step waits afresh", waiting_step_alarm)
