"""Idle sessions and accounts, bursts of sign-ins and connections, wrong
clocks, stray hosts and origins, file modes, and downloads and drive reads
that must not hold up the rig."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

from lab import db as dbmod
from lab import pump as pumpmod
from lab import registry, runs
from tools import fake_tc600
from tools.checks.common import (
    ROOT, wait_until, attempt, Rig, watching, _try)


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
