"""Guests: watching without an account, once an admin allows it -- and
nothing more, over real HTTP against the real handler."""

from __future__ import annotations

import os
import tempfile
import threading

from lab import db as dbmod
from lab import registry, runs
from tools.checks.common import attempt, check


def test_guests() -> None:
    """A guest is the widest door the panel has: anyone who reaches it. So
    nearly every check here is a refusal -- an action, a download, the
    serial ports, an account page -- and the rest are about the door itself:
    shut until an admin opens it, shut again for everyone at once, never
    written to the database, and bounded in number and per address."""
    import json as jsonmod
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    from lab import api as apimod
    from lab import auth as authmod

    print("\nGuests")
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "guests.db")
        database = dbmod.Database(path)
        accounts = authmod.Auth(database, "chamber-1")
        engine = runs.RunEngine(database, registry.Registry(),
                                roles=runs.ALL_ROLES, chamber="chamber-1")
        application = apimod.Application(registry.Registry(), database, engine,
                                         auth=accounts,
                                         chamber={"id": "chamber-1",
                                                  "name": "Chamber 1"})
        handler = type("QuietHandler", (apimod.Handler,),
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
                with urllib.request.urlopen(request) as response:
                    raw = response.read()
                    return response.status, (jsonmod.loads(raw)
                                             if raw[:1] == b"{" else {})
            except urllib.error.HTTPError as error:
                raw = error.read()
                return error.code, (jsonmod.loads(raw) if raw[:1] == b"{" else {})

        def login(name, password):
            return call("/api/login", {"username": name,
                                       "password": password})[1].get("token")

        def guests_on():
            return call("/api/ping")[1].get("auth", {}).get("guests")

        try:
            authmod.bootstrap_owner(database, "chamber-1", "owner", "owner-pass-1",
                                    "Olga")
            ana = database.create_user("ana", authmod.hash_password("ana-pass-1"),
                                       display_name="Ana", status="active")
            database.set_role(ana["id"], "chamber-1", "operator", "owner")
            owner, operator = login("owner", "owner-pass-1"), login("ana", "ana-pass-1")
            # A long stored run: what a guest may not take in one request.
            long_run = database.create_run("hold", "long", {})["id"]
            with database._lock:
                database._db.executemany(
                    "INSERT INTO samples (run_id, t, at, p1) VALUES (?, ?, ?, ?)",
                    [(long_run, float(i), 0.0, 1e-5) for i in range(5000)])
                database._db.commit()

            # --- shut until an admin opens it ----------------------------------
            code, body = call("/api/guest", {})
            check("guests are not allowed until an admin says so: the sign-in "
                  "page offers nothing, and asking is refused",
                  guests_on() is False and code == 403 and "token" not in body,
                  f"ping guests {guests_on()}; /api/guest {code}")
            code, _ = call("/api/users/guests", {"allowed": True}, operator)
            check("an operator cannot let guests in",
                  code == 403 and guests_on() is False, f"HTTP {code}")
            code, body = call("/api/users/guests", {"allowed": True}, owner)
            logged = [e for e in database.events(limit=20)
                      if "allowed guests" in e["message"]]
            check("an admin lets guests watch: the ping says so, and it is "
                  "logged under their name",
                  code == 200 and body.get("guests", {}).get("allowed") is True
                  and guests_on() is True and logged
                  and logged[0].get("user") == "owner",
                  f"HTTP {code}; logged {[e['message'] for e in logged]}")

            # --- a guest watches -----------------------------------------------
            sessions_before = len(database.sessions())
            code, body = call("/api/guest", {})
            guest = body.get("token")
            me = body.get("me") or {}
            check("a guest gets a session that only watches",
                  code == 200 and bool(guest) and me.get("kind") == "guest"
                  and me.get("role") == "viewer"
                  and not any((me.get("can") or {}).values()),
                  f"HTTP {code}; me {me}")
            check("a guest is never written to the database",
                  len(database.sessions()) == sessions_before,
                  f"{sessions_before} -> {len(database.sessions())} session rows")
            code, state = call("/api/state", token=guest)
            check("a guest sees the live state, the runs and the log",
                  code == 200 and state.get("me", {}).get("kind") == "guest"
                  and call("/api/runs", token=guest)[0] == 200
                  and call("/api/events", token=guest)[0] == 200
                  and call("/api/history?seconds=60", token=guest)[0] == 200,
                  f"state HTTP {code}")
            check("a guest never holds control, and is not shown as holding it",
                  state.get("control", {}).get("mine") is False
                  and state.get("control", {}).get("held") is False)

            # --- and nothing more ----------------------------------------------
            # Closed by default: what is not named is refused, however the
            # path is spelled. /api/runsx once reached the runs handler.
            refused_gets = [
                "/api/export/runs.csv", "/api/export/database.db",
                "/api/export/archive.json", "/api/runs/1/export.csv",
                "/api/runs/1/csv", "/api/runs//1/json", "/api/runs/1/export.json/",
                "/api/runsx/1/csv", "/api/runs_/1/export.json", "/api/runs/1/",
                "/api/runs/01", "/api/runs/1/samples/x", "/api/ports",
                "/api/ports/survey", "/api/users", "/api/stats",
                "/api/device/gauge/details", "/api/device/pump/diagnostics",
                "/api/device/pump/accessories", "/api/device//state",
                "/api/nothing"]
            got = {route: call(route, token=guest)[0] for route in refused_gets}
            check("a guest downloads nothing, reads no instrument directly, and "
                  "sees neither the serial ports, the store's figures nor the "
                  "accounts -- whatever the spelling",
                  all(code == 403 for code in got.values()),
                  ", ".join(f"{r} {c}" for r, c in got.items() if c != 403)
                  or f"all {len(got)} refused")
            allowed_gets = ["/api/me", "/api/live", "/api/devices", "/api/procedures",
                            "/api/equipment", "/api/trends", "/api/actuators",
                            "/api/runs?limit=5", "/api/runs/1", "/api/runs/1/samples",
                            "/api/runs/1/events", "/api/device/pump/state"]
            got = {route: call(route, token=guest)[0] for route in allowed_gets}
            check("... while what the pages show is open to it",
                  all(code != 403 for code in got.values()),
                  ", ".join(f"{r} {c}" for r, c in got.items() if c == 403)
                  or "none refused")
            check("a path that only begins like /api/runs no longer reaches "
                  "the runs, for anybody",
                  call(f"/api/runsx/{long_run}/csv", token=owner)[0] == 404
                  and call(f"/api/runs/{long_run}/csv", token=owner)[0] == 200)

            def one_request_bounded():
                def rows(route, token):
                    return len(call(route, token=token)[1].get("samples") or [])
                stored = {f"max={m}": rows(f"/api/runs/{long_run}/samples?max={m}",
                                           guest)
                          for m in (0, -5, 100000)}
                theirs = rows(f"/api/runs/{long_run}/samples?max=0", owner)
                # The live run, as the engine would hand it over.
                run = [{"t": float(i)} for i in range(5000)]
                asked = {}
                engine.recent_samples = lambda limit: run[-limit:]
                engine.samples_since = lambda since: [s for s in run
                                                      if s["t"] > since]

                def decimated(points):
                    asked["points"] = points
                    return run[:points]
                engine.run_samples = decimated
                live = {q: rows(f"/api/live?{q}", guest)
                        for q in ("limit=0", "limit=100000", "since=-1")}
                rows("/api/live?points=100000", guest)
                windows = []
                kept = application.history_window
                application.history_window = lambda seconds, points, since: (
                    windows.append(points) or {"ok": True, "points": []})
                listed = []
                kept_runs = database.runs
                database.runs = lambda **kw: listed.append(kw["limit"]) or []
                try:
                    call("/api/history?points=20000", token=guest)
                    call("/api/runs?limit=1000", token=guest)
                finally:
                    application.history_window = kept
                    database.runs = kept_runs
                    for name in ("recent_samples", "samples_since", "run_samples"):
                        delattr(engine, name)
                ok = (max(stored.values()) <= 3000 and stored["max=-5"] <= 2000
                      and theirs == 5000 and max(live.values()) <= 600
                      and asked.get("points") == 1500 and windows == [3000]
                      and listed == [200])
                return ok, (f"stored {stored}, an account's {theirs}; live {live}, "
                            f"points {asked.get('points')}; history {windows}; "
                            f"runs {listed}")
            attempt("one guest request carries no more than the panel asks for: "
                    "a stored curve, the live run, the history, the run list",
                    one_request_bounded)

            def live_points_real():
                # The engine's own decimation, not a stand-in: a negative
                # `points` once meant "every row".
                engine._samples = [{"t": float(i), "p1": 1e-5, "p2": None,
                                    "speed": 1500.0, "power": 10.0}
                                   for i in range(5000)]
                try:
                    got = {q: len(call(f"/api/live?{q}", token=guest)[1]
                                  .get("samples") or [])
                           for q in ("points=-1", "points=-100000",
                                     "points=100000", "points=0")}
                finally:
                    engine._samples = []
                return all(n <= 1500 for n in got.values()), str(got)
            attempt("a negative or huge `points` gets a guest no more of the "
                    "live run than the panel asks for", live_points_real)

            def far_offset():
                codes = {who: call("/api/runs?offset=10000000000000000000",
                                   token=token)[0]
                         for who, token in (("guest", guest), ("owner", owner))}
                return all(code == 200 for code in codes.values()), str(codes)
            attempt("an offset past SQLite's integers is an answer, never a "
                    "dropped connection", far_offset)

            def heavy_turns():
                slot = application.guest_slot
                taken = [slot.acquire(timeout=1)
                         for _ in range(apimod.GUEST_HEAVY_AT_ONCE)]
                waited = apimod.GUEST_HEAVY_WAIT_S
                apimod.GUEST_HEAVY_WAIT_S = 0.2
                heavy = [f"/api/runs/{long_run}/samples", f"/api/runs/{long_run}",
                         f"/api/runs/{long_run}/events", "/api/runs?limit=5",
                         "/api/trends", "/api/live?points=10"]
                try:
                    as_guest = {r: call(r, token=guest) for r in heavy}
                    as_owner = {r: call(r, token=owner)[0] for r in heavy}
                    light = call("/api/state", token=guest)[0]
                finally:
                    apimod.GUEST_HEAVY_WAIT_S = waited
                    for held in taken:
                        if held:
                            slot.release()
                turned = [r for r, (code, body) in as_guest.items()
                          if code == 429 and "busy with other guests"
                          in body.get("error", "")]
                return (all(taken) and turned == heavy
                        and all(c == 200 for c in as_owner.values())
                        and light == 200,
                        f"turned away {len(turned)}/{len(heavy)}; accounts "
                        f"{set(as_owner.values())}; guest's state {light}")
            attempt(f"at most {apimod.GUEST_HEAVY_AT_ONCE} heavy guest requests at "
                    "once: the next waits its turn, then 429 -- accounts and a "
                    "guest's poll are not held to it", heavy_turns)

            def slot_given_back():
                kept = application.guest_curve

                def broken(run_id):
                    raise RuntimeError("selftest")
                application.guest_curve = broken
                try:
                    call(f"/api/runs/{long_run}/samples", token=guest)
                except Exception:
                    pass                      # the connection is dropped: fine
                finally:
                    application.guest_curve = kept
                slot = application.guest_slot
                free = [slot.acquire(timeout=0.5)
                        for _ in range(apimod.GUEST_HEAVY_AT_ONCE)]
                for held in free:
                    if held:
                        slot.release()
                return all(free), f"free afterwards: {free}"
            attempt("a guest request that fails gives its place back",
                    slot_given_back)

            def curve_kept():
                with application._guest_curves_lock:
                    application._guest_curves.clear()
                reads = []
                real = database.samples
                database.samples = lambda run_id: reads.append(run_id) or real(run_id)
                try:
                    sizes = [len(call(f"/api/runs/{long_run}/samples?max={m}",
                                      token=guest)[1].get("samples") or [])
                             for m in (1500, 3000, 2000)]
                finally:
                    database.samples = real
                    database.add_sample(long_run, {"t": 5000.0, "at": 0.0,
                                                   "p1": 2e-5})
                    database.samples = (lambda run_id: reads.append(run_id)
                                        or real(run_id))
                    try:
                        after = call(f"/api/runs/{long_run}/samples",
                                     token=guest)[1].get("total")
                    finally:
                        database.samples = real
                return (len(reads) == 2 and len(set(sizes)) == 1
                        and 0 < sizes[0] <= apimod.GUEST_SAMPLES
                        and after == 5001,
                        f"{len(reads)} reads of the store; sizes {sizes}; "
                        f"after a new sample, total {after}")
            attempt("a stored curve is read once for every guest asking, at one "
                    "size whatever they ask", curve_kept)

            def since_searched():
                # Searched, not walked: asked "since the start" over a long
                # run, the answer is the same and costs a handful of steps.
                samples = [{"t": float(i)} for i in range(200000)]
                engine._samples = samples
                try:
                    same = all(
                        engine.samples_since(x) == [s for s in samples if s["t"] > x]
                        for x in (-1.0, 0.0, 0.5, 199998.0, 199999.0, 1e9))
                    reads = []

                    class Counted(list):
                        def __getitem__(self, index):
                            reads.append(index)
                            return list.__getitem__(self, index)
                    engine._samples = Counted(samples)
                    engine.samples_since(-1.0)
                    guest_rows = call("/api/live?since=-1", token=guest)[1]
                    rows = len(guest_rows.get("samples") or [])
                finally:
                    engine._samples = []
                return (same and len(reads) < 64 and rows == 600,
                        f"same {same}; {len(reads)} rows looked at; guest got {rows}")
            attempt("the live run since a time is found by searching, not by "
                    "walking a week of rows", since_searched)

            def events_indexed():
                with database._lock:
                    plan = " ".join(str(tuple(row)) for row in database._db.execute(
                        "EXPLAIN QUERY PLAN SELECT * FROM events ORDER BY at "
                        "DESC LIMIT 200").fetchall())
                return "events_by_at" in plan and "TEMP B-TREE" not in plan, plan
            attempt("the rig's latest events come from an index, not a sort of "
                    "every event kept", events_indexed)

            def huge_id():
                digits = "9" * 5000
                as_guest = call(f"/api/runs/{digits}", token=guest)[0]
                as_admin = call(f"/api/runs/{digits}/delete", {"confirm": True},
                                owner)[0]
                call("/api/control/release", {}, owner)  # the attempt took it
                return (as_guest == 403 and 400 <= as_admin < 500,
                        f"guest {as_guest}, admin {as_admin}")
            attempt("a run id of thousands of digits is refused, never a "
                    "dropped connection", huge_id)
            posts = {
                "/api/command": {"device": "pump", "action": "on", "confirm": True},
                "/api/run/start": {"procedure": "hold", "confirm": True},
                "/api/run/stop": {},
                "/api/pump/secure": {"confirm": True},
                "/api/pump/shutdown": {"confirm": True},
                "/api/pump/cancel-shutdown": {"confirm": True},
                "/api/alarms/ack": {"id": "all"},
                "/api/run/acknowledge": {"prompt": 1},
                "/api/run/checklist": {"id": 1},
                "/api/control/take": {"force": True},
                "/api/control/release": {},
                "/api/session/touch": {},
                "/api/session/reauth": {"password": "x"},
                "/api/me/password": {"current": "x", "new": "y" * 12},
                "/api/role-requests": {"role": "operator"},
                "/api/users/guests": {"allowed": True},
                "/api/users/approve": {"id": 1, "role": "admin"},
                "/api/devices/add": {"label": "x"},
                "/api/equipment": {"key": "gas", "installed": True, "confirm": True},
                "/api/update/install": {"release": "x", "confirm": True},
                "/api/runs/1/update": {"name": "x"},
                "/api/runs/1/delete": {"confirm": True},
            }
            got = {route: call(route, body, guest)[0]
                   for route, body in posts.items()}
            check("a guest can do nothing: every action, safety actions and "
                  "account pages included, is refused",
                  all(code == 403 for code in got.values()),
                  ", ".join(f"{r} {c}" for r, c in got.items() if c != 403)
                  or f"all {len(got)} refused")
            check("... and holds no control afterwards",
                  call("/api/state", token=owner)[1].get("control", {})
                  .get("held") is False)

            code, _ = call("/api/logout", {}, guest)
            check("a guest can stop watching; its session is then refused",
                  code == 200 and call("/api/state", token=guest)[0] == 401)

            # --- bounded -------------------------------------------------------
            def paced():
                token = call("/api/guest", {})[1]["token"]
                kept = accounts._guest_requests
                accounts._guest_requests = authmod.Budget(5)
                try:
                    codes = [call("/api/state", token=token)[0] for _ in range(7)]
                finally:
                    accounts._guest_requests = kept
                return codes[:5] == [200] * 5 and codes[5:] == [429, 429], str(codes)
            attempt("a guest is held to a pace: past its requests a minute, "
                    "429", paced)
            def idle_dropped():
                token = call("/api/guest", {})[1]["token"]
                hashed = authmod.token_hash(token)
                with accounts._lock:
                    accounts._guests[hashed]["seen"] -= authmod.GUEST_IDLE_S + 1
                return (call("/api/state", token=token)[0] == 401,
                        "a guest that stopped polling is dropped")
            attempt("a guest that stops polling is dropped (a hidden page "
                    "joins again when shown)", idle_dropped)

            def too_old():
                token = call("/api/guest", {})[1]["token"]
                hashed = authmod.token_hash(token)
                with accounts._lock:
                    accounts._guests[hashed]["since"] -= authmod.GUEST_SESSION_S + 1
                return call("/api/state", token=token)[0] == 401, "dropped"
            attempt("a guest session ends after its longest life, polled or not",
                    too_old)

            def capped():
                with accounts._lock:
                    accounts._guests.clear()
                tokens = [call("/api/guest", {})[1].get("token")
                          for _ in range(authmod.GUESTS_MAX)]
                code, body = call("/api/guest", {})
                held = accounts.guests_report()["watching"]
                with accounts._lock:
                    accounts._guests.clear()
                return (all(tokens) and code == 503 and "token" not in body
                        and held == authmod.GUESTS_MAX,
                        f"{sum(map(bool, tokens))} let in, then HTTP {code}; "
                        f"{held} watching")
            attempt(f"at most {authmod.GUESTS_MAX} guests watch at once", capped)

            def per_address():
                # Loopback (the kiosk, the machine itself) has no budget; an
                # address on the network has GUEST_STARTS_PER_HOUR an hour.
                answers = []
                for _ in range(authmod.GUEST_STARTS_PER_HOUR):
                    result = accounts.guest("10.9.9.9")
                    answers.append(result.get("ok"))
                    if result.get("ok"):
                        accounts.logout(accounts.resolve(result["token"]))
                over = accounts.guest("10.9.9.9")
                other = accounts.guest("10.9.9.10")
                if other.get("ok"):
                    accounts.logout(accounts.resolve(other["token"]))
                return (all(answers) and over.get("status") == 429
                        and other.get("ok"),
                        f"{sum(map(bool, answers))} started, then "
                        f"{over.get('status')}; another address "
                        f"{'let in' if other.get('ok') else other}")
            def per_address_at_once():
                made = [accounts.guest("10.9.8.7")
                        for _ in range(authmod.GUESTS_PER_ADDRESS)]
                over = accounts.guest("10.9.8.7")
                for result in made:
                    if result.get("ok"):
                        accounts.logout(accounts.resolve(result["token"]))
                return (all(r.get("ok") for r in made)
                        and over.get("status") == 503,
                        f"{sum(bool(r.get('ok')) for r in made)} let in, then "
                        f"{over.get('status')}")
            attempt(f"one address holds at most {authmod.GUESTS_PER_ADDRESS} "
                    "guest places at once", per_address_at_once)

            def switched_off_meanwhile():
                # Off between the first look and the place being taken: a
                # refusal, never a guest -- nor a crash on the way out.
                starts = accounts._guest_starts

                class Flips:
                    def take(self, *keys):
                        accounts._guests_on = False
                        return 0.0

                    def give_back(self, *keys):
                        pass
                accounts._guest_starts = Flips()
                try:
                    result = accounts.guest("10.4.4.4")
                finally:
                    accounts._guest_starts = starts
                    accounts._guests_on = True
                return (result.get("ok") is False and result.get("status") == 403
                        and not accounts._guests, str(result)[:80])
            attempt("guests switched off during a start: refused, no session",
                    switched_off_meanwhile)

            attempt("one address starts at most "
                    f"{authmod.GUEST_STARTS_PER_HOUR} guest sessions an hour",
                    per_address)

            def from_elsewhere():
                request = urllib.request.Request(
                    base + "/api/guest", method="POST", data=b"{}",
                    headers={"Content-Type": "application/json",
                             "Origin": "http://evil.example"})
                try:
                    urllib.request.urlopen(request)
                    foreign = 200
                except urllib.error.HTTPError as error:
                    foreign = error.code
                request = urllib.request.Request(
                    base + "/api/guest", method="POST", data=b"{}",
                    headers={"Content-Type": "application/json",
                             "Host": "evil.example"})
                try:
                    urllib.request.urlopen(request)
                    rebound = 200
                except urllib.error.HTTPError as error:
                    rebound = error.code
                return foreign == 403 and rebound == 421, f"{foreign}, {rebound}"
            attempt("a guest session is refused to another site's page (403) and "
                    "to another host name (421)", from_elsewhere)

            code, _ = call("/api/users/guests", {"allowed": False}, owner)
            code, _ = call("/api/users/guests", {"allowed": "false"}, owner)
            check("only true opens the door: \"false\" is a string, and closed "
                  "it stays", guests_on() is False, f"HTTP {code}")
            call("/api/users/guests", {"allowed": True}, owner)

            # --- shut again, for everyone at once --------------------------------
            watching = call("/api/guest", {})[1]["token"]
            code, body = call("/api/users/guests", {"allowed": False}, owner)
            logged = [e for e in database.events(limit=20)
                      if "stopped guests watching" in e["message"]]
            check("switching guests off ends every guest at once, refuses new "
                  "ones, and is logged",
                  code == 200 and call("/api/state", token=watching)[0] == 401
                  and call("/api/guest", {})[0] == 403 and guests_on() is False
                  and bool(logged),
                  f"HTTP {code}; logged {bool(logged)}")

            call("/api/users/guests", {"allowed": True}, owner)
            again = authmod.Auth(database, "chamber-1")
            check("the choice survives a restart; the guests themselves do not",
                  again.status().get("guests") is True
                  and again.resolve(call("/api/guest", {})[1]["token"]) is None)

            off = authmod.Auth(database, "chamber-1", enabled=False)
            check("with accounts off there is no guest door, and nobody can "
                  "leave it open for the next start",
                  "guests" not in off.status()
                  and off.guest("10.0.0.1").get("ok") is False
                  and off.set_guests(authmod.LOCAL, True).get("ok") is False)

            def open_on_full_disk():
                call("/api/users/guests", {"allowed": False}, owner)
                real = database.set_meta

                def full(*args, **kwargs):
                    raise OSError("disk full (selftest)")
                database.set_meta = full
                try:
                    code, _ = call("/api/users/guests", {"allowed": True}, owner)
                finally:
                    database.set_meta = real
                stayed = guests_on() is False
                call("/api/users/guests", {"allowed": True}, owner)
                return code == 500 and stayed, f"HTTP {code}, stayed shut {stayed}"
            attempt("guests are not let in when the choice cannot be recorded",
                    open_on_full_disk)

            def shut_on_full_disk():
                token = call("/api/guest", {})[1]["token"]
                real = database.set_meta

                def full(*args, **kwargs):
                    raise OSError("disk full (selftest)")
                database.set_meta = full
                try:
                    code, body = call("/api/users/guests", {"allowed": False}, owner)
                finally:
                    database.set_meta = real
                return (code == 500 and "Guests are off now" in body.get("error", "")
                        and guests_on() is False
                        and call("/api/state", token=token)[0] == 401,
                        f"HTTP {code}: {body.get('error', '')[:70]}")
            attempt("switching guests off works with a full disk -- shut first, "
                    "and said that it could not be recorded", shut_on_full_disk)
        finally:
            server.shutdown()
            server.server_close()
            engine.registry.close()
            database.close()
