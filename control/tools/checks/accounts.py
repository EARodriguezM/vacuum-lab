"""Accounts, roles per chamber, sessions and the control lease."""

from __future__ import annotations

import os
import tempfile
import threading
import time

from lab import db as dbmod
from lab import registry, runs
from tools.checks.common import (
    check, attempt)


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
        engine = runs.RunEngine(database, registry.Registry(),
                                roles=runs.ALL_ROLES, chamber="chamber-1")
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
                  and ping.get("auth") == {"enabled": True, "owner": False,
                                           "email_domain": "unal.edu.co",
                                           "verify_email": False})
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
            from lab import judgement
            check("the state carries the thresholds the engine judges by",
                  state.get("judgement") == judgement.published(),
                  str(state.get("judgement"))[:80])
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
