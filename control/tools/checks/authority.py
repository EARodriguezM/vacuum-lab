"""Who may waive which interlock, and who may change the drive, over real
HTTP."""

from __future__ import annotations

import os
import tempfile
import threading

from lab import db as dbmod
from lab import pump as pumpmod
from lab import registry, runs
from tools import fake_tc600
from tools.checks.common import (
    wait_until, attempt, Rig)


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
    engine = runs.RunEngine(database, devices,
                            roles=runs.ALL_ROLES, chamber="chamber-1")
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

        # --- who ran it ---------------------------------------------------------
        # The account that pressed Start, whatever name the request carries:
        # a typed operator could not be told from the truth afterwards.
        code, body = call("/api/run/start", {
            "procedure": "hold", "confirm": True, "name": "Typed for somebody",
            "operator": "Mallory", "config": {"duration_min": 5, "interval_s": 1}},
            owner)
        ran = (body.get("run") or {}).get("id")
        if ran:
            wait_until(lambda: engine.status()["running"], 5, 0.05)
            engine.stop("selftest")
            wait_until(lambda: not engine.status()["running"], 15, 0.1)
        stored = (database.run(ran) if ran else None) or {}
        attempt("a run's operator is whoever started it, not a name sent with it",
                lambda: (code == 200 and stored.get("operator") == "Olga"
                         and stored.get("started_by") == "olga",
                         f"{code}: operator {stored.get('operator')!r}, started by "
                         f"{stored.get('started_by')!r}"
                         + ("" if ran else f"; {body.get('error')}")))
        changed, said = call(f"/api/runs/{ran}", {"operator": "Mallory"}, ana)
        both, _ = call(f"/api/runs/{ran}", {"name": "Sneaky",
                                            "operator": "Mallory"}, ana)
        # Refused as a whole: the name sent with it is not written either.
        untouched = ((database.run(ran) if ran else None) or {}).get("name")
        # An older panel sends every field back, the operator unchanged.
        renamed, _ = call(f"/api/runs/{ran}", {"name": "Renamed",
                                               "operator": "Olga"}, ana)
        noted, _ = call(f"/api/runs/{ran}", {"notes": "fine", "operator": None}, ana)
        after = (database.run(ran) if ran else None) or {}
        refusal = said.get("error", "")
        attempt("nobody can change who ran a run; its name and notes stay editable",
                lambda: (changed == 400 and "cannot be changed" in refusal
                         and both == 400 and untouched == "Typed for somebody"
                         and renamed == 200 and noted == 200
                         and after.get("operator") == "Olga"
                         and after.get("name") == "Renamed"
                         and after.get("notes") == "fine",
                         f"changing it: {changed} {refusal[:40]!r}; with a new "
                         f"name: {both}, name then {untouched!r}; renaming: "
                         f"{renamed}; operator null: "
                         f"{noted}; operator {after.get('operator')!r}, name "
                         f"{after.get('name')!r}"))
        records = [e for e in database.events(limit=40)
                   if (e.get("detail") or {}).get("run") == ran]
        tried = [e for e in records if "tried to change who ran" in e["message"]]
        edits = [sorted((e.get("detail") or {}).get("changes", {}))
                 for e in records if "edited the" in e["message"]]
        attempt("an attempt to change who ran a run is logged; an edit's record "
                "names only what changed",
                lambda: (len(tried) == 2 and all(e.get("user") == "ana" for e in tried)
                         and edits and all(c in (["name"], ["notes"]) for c in edits),
                         f"{len(tried)} attempt(s) logged; edits {edits}"))

        # A person signed in on a paired tablet is that person, not the tablet.
        pairing = call("/api/devices/add", {"label": "Wall"}, owner)[1].get("code")
        wall = call("/api/pair", {"code": pairing})[1].get("token")
        ana_here = call("/api/login", {"username": "ana", "password": "ana-pass-1",
                                       "device": wall})[1].get("token")
        call("/api/control/release", {}, owner)
        code, body = call("/api/run/start", {
            "procedure": "hold", "operator": "Wall",
            "config": {"duration_min": 5, "interval_s": 1}}, ana_here)
        there = (body.get("run") or {}).get("id")
        if there:
            wait_until(lambda: engine.status()["running"], 5, 0.05)
            engine.stop("selftest")
            wait_until(lambda: not engine.status()["running"], 15, 0.1)
        stored = (database.run(there) if there else None) or {}
        attempt("on a paired tablet the run is the signed-in person's",
                lambda: (code == 200 and stored.get("operator") == "Ana"
                         and stored.get("started_by") == "ana",
                         f"{code}: operator {stored.get('operator')!r}, started by "
                         f"{stored.get('started_by')!r}"
                         + ("" if there else f"; {body.get('error')}")))
        if ran:
            database.update_run(ran, operator="Mallory")
        kept = ((database.run(ran) if ran else None) or {}).get("operator")
        attempt("the store will not change it either",
                lambda: (kept == "Olga", f"operator {kept!r}"))
    finally:
        server.shutdown()
        server.server_close()
        devices.close()
        database.close()
        fake_tc600.reset()
