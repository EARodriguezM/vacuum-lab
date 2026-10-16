"""Institutional accounts: a username is the institutional address before the
@, and a request is made only by a Google sign-in of that account, before an
admin may approve it (lab/google.py, the device flow, against
tools/fake_google.py)."""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

from lab import db as dbmod
from lab import registry, runs
from tools.checks.common import ROOT, attempt


def test_addresses() -> None:
    """Over real HTTP against the real handler, with a fake Google.

    What it asks: is a request made only for the account Google proved, the
    moment it proves it, once; only for an account of the institution's own
    organization; is Google asked no faster than it allows, and nothing
    secret repeated anywhere; does an admin decide only the request they
    were shown -- and does a server with no Google client behave as before,
    while a broken one takes nothing.
    """
    import sqlite3
    import urllib.error
    import urllib.request
    from lab import api as apimod
    from lab import auth as authmod
    from lab import google as googlemod
    from tools.fake_google import FakeGoogle

    print("\nInstitutional accounts: a request made by a Google sign-in")
    directory = tempfile.mkdtemp(prefix="selftest-addresses-")
    db_path = os.path.join(directory, "addresses.db")
    database = dbmod.Database(db_path)
    google_path = os.path.join(directory, googlemod.SETTINGS_FILE)
    google = FakeGoogle()
    saved_pending = authmod.MAX_PENDING
    saved = (authmod.SIGNUPS_PER_HOUR, authmod.GOOGLE_POLL_MIN_S,
             authmod.GOOGLE_FLOWS_MAX, authmod.GOOGLE_STARTS_PER_HOUR,
             authmod.GOOGLE_STARTS_PER_HOUR_ALL,
             googlemod.DEVICE_URL, googlemod.TOKEN_URL)
    # Every request here comes from 127.0.0.1, and nobody waits 5 s between
    # polls: the limits and the pace are checked on their own below.
    authmod.SIGNUPS_PER_HOUR = 100
    authmod.GOOGLE_POLL_MIN_S = 0.0
    authmod.GOOGLE_STARTS_PER_HOUR = authmod.GOOGLE_STARTS_PER_HOUR_ALL = 1000
    google.interval = 0
    googlemod.DEVICE_URL, googlemod.TOKEN_URL = google.device_url, google.token_url
    accounts = authmod.Auth(database, "chamber-1",
                            google=googlemod.Google(google_path))
    engine = runs.RunEngine(database, registry.Registry(),
                            roles=runs.ALL_ROLES, chamber="chamber-1")
    application = apimod.Application(registry.Registry(), database, engine,
                                     auth=accounts,
                                     chamber={"id": "chamber-1", "name": "C1"})
    handler = type("AddressHandler", (apimod.Handler,),
                   {"app": application, "log_message": lambda *a: None})
    server = apimod.Server(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, token=None):
        request = urllib.request.Request(
            base + path, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, (json.loads(raw) if raw[:1] == b"{" else {})

    def user(name):
        return database.user_by_name(name) or {}

    def users_tool(*args):
        return subprocess.run([sys.executable, os.path.join(ROOT, "tools", "users.py"),
                               "--db", db_path, *args], capture_output=True,
                              text=True, timeout=60, stdin=subprocess.DEVNULL)

    def start(password="good-pass-1", note=""):
        return call("/api/signup/google/start", {"password": password, "note": note})

    def poll(flow):
        return call("/api/signup/google/poll", {"flow": flow})

    def requested(email, name="Somebody", password="good-pass-1", note="",
                  **changes):
        """A request started, the person signing in at Google, and polled to
        its end: (flow, the last poll's status, its answer)."""
        code, began = start(password, note)
        if code != 200:
            return None, code, began
        google.approve(began["user_code"], email, name, **changes)
        for _ in range(40):
            code, answer = poll(began["flow"])
            if code != 200 or answer.get("state") == "done":
                return began["flow"], code, answer
            time.sleep(0.05)
        return began["flow"], code, answer

    def entry_for(began):
        """The server's own record of a sign-in, found by its code."""
        with accounts._flows_lock:
            return next(v for v in accounts._flows.values()
                        if google.flows.get(v["device_code"], {}).get("user_code")
                        == began["user_code"])

    try:
        authmod.bootstrap_owner(database, "chamber-1", "owner", "owner-pass-1")
        owner = call("/api/login", {"username": "owner",
                                    "password": "owner-pass-1"})[1]["token"]

        # --- the username is the address before the @ -------------------------
        def usernames():
            got = {typed: accounts.username_from(typed) for typed in
                   ("anperez", "AnPerez@UNAL.edu.co", "anperez@gmail.com",
                    "an@perez@unal.edu.co", "anperez\n@unal.edu.co")}
            return (got["anperez"] == ("anperez", None)
                    and got["AnPerez@UNAL.edu.co"] == ("anperez", None)
                    and got["anperez@gmail.com"][1] is not None
                    and got["an@perez@unal.edu.co"][1] is not None
                    and got["anperez\n@unal.edu.co"] == ("anperez", None)
                    and authmod.check_username("anperez\n") is not None,
                    str(got)[:150])
        attempt("a username is the institutional address before the @; another "
                "domain, or a hidden line break, is refused", usernames)

        # --- no Google client: as before, and said -----------------------------
        ping = call("/api/ping")[1].get("auth", {})
        code, body = call("/api/signup", {"username": "ana", "name": "Ana",
                                          "password": "ana-pass-1"})
        approved = call("/api/users/approve", {"id": user("ana").get("id"),
                                               "role": "viewer"}, owner)[0]
        signed = call("/api/login", {"username": "ana@unal.edu.co",
                                     "password": "ana-pass-1"})[0]
        no_start = start()[0]
        attempt("with no Google client a request is not checked and can be "
                "approved, the sign-in page is told, and the whole address "
                "signs in", lambda: (code == 200 and body.get("verified") is False
                                     and approved == 200 and signed == 200
                                     and no_start == 409
                                     and ping.get("google_signin") is False
                                     and ping.get("email_domain") == "unal.edu.co",
                                     f"{code} {body}; approve {approved}; sign-in "
                                     f"{signed}; start {no_start}; ping {ping}"))
        call("/api/signup", {"username": "old.request", "password": "old-pass-1"})

        # --- the client, set on the machine -------------------------------------
        def client_file():
            downloaded = os.path.join(directory, "client_secret_x.json")
            with open(downloaded, "w") as fh:
                json.dump({"installed": {**google.settings(),
                                         "project_id": "p", "auth_uri": "u"}}, fh)
            bare = os.path.join(directory, "broken.json")
            with open(bare, "w") as fh:
                json.dump({"web": {"client_id": "only-an-id"}}, fh)
            refused = users_tool("google", "set", "--client-file", bare)
            made = users_tool("google", "set", "--client-file", downloaded)
            shown = users_tool("google", "show").stdout
            mode = os.stat(google_path).st_mode & 0o777
            live = call("/api/ping")[1].get("auth", {}).get("google_signin")
            return (refused.returncode == 1 and made.returncode == 0
                    and mode == 0o600 and google.client_secret not in shown
                    and google.client_id in shown and live is True,
                    f"broken file {refused.returncode}, good {made.returncode}, "
                    f"mode {oct(mode)}, panel sees it: {live}")
        attempt("the client is read from Google's download, kept 0600, shown "
                "without its secret, and used without a restart", client_file)

        # --- a request, through Google ----------------------------------------
        code, began = start("her-pass-1", "photonics group")
        early = poll(began.get("flow", ""))
        nothing_yet = user("ana.perez")
        google.approve(began.get("user_code", ""), "Ana.Perez@unal.edu.co",
                       "Ana  María Pérez")
        done = poll(began.get("flow", ""))
        after = poll(began.get("flow", ""))
        stored = user("ana.perez")
        attempt("a request waits for Google, then is made for the account Google "
                "proved -- with the password and note given first -- marked "
                "verified; asked again, the sign-in says done",
                lambda: (code == 200 and began.get("url") == "https://www.google.com/device"
                         and early == (200, {"ok": True, "state": "pending"})
                         and not nothing_yet
                         and done[1].get("state") == "done"
                         and done[1].get("username") == "ana.perez"
                         and stored.get("display_name") == "Ana María Pérez"
                         and stored.get("note") == "photonics group"
                         and stored.get("status") == "pending"
                         and stored.get("email_verified_at")
                         and after == done,
                         f"start {code}; {early}; then {done}; then {after}"))
        approved = call("/api/users/approve", {"id": stored.get("id"), "role": "operator",
                                               "created_at": stored.get("created_at")},
                        owner)[0]
        signed = [call("/api/login", {"username": typed, "password": "her-pass-1"})[0]
                  for typed in ("ana.perez", "ana.perez@unal.edu.co")]
        attempt("an admin approves it, and the person signs in with the username "
                "and the password chosen", lambda: (approved == 200
                                                    and signed == [200, 200],
                                                    f"approve {approved}; {signed}"))
        refused = [call("/api/signup", {"username": "x.y", "password": "x-pass-1"})[0],
                   poll("made-up")[0], start("short")[0]]
        attempt("with Google set up nothing is taken typed, an unknown sign-in "
                "is no sign-in, and a weak password is refused before Google",
                lambda: (refused == [400, 410, 400] and not user("x.y"), str(refused)))

        # --- what Google's answer must say --------------------------------------
        cases = {
            "another domain": dict(email="ana@gmail.com", hd=None),
            "a personal account at the address": dict(email="bo@unal.edu.co", hd=None),
            "another organization": dict(email="bo@unal.edu.co", hd="other.edu"),
            "an address not verified": dict(email="bo@unal.edu.co", verified=False),
            "another client": dict(email="bo@unal.edu.co", aud="other-client"),
            "another issuer": dict(email="bo@unal.edu.co", iss="https://evil.example"),
            "an old answer": dict(email="bo@unal.edu.co", exp=int(time.time()) - 600),
            "no username in it": dict(email="b@unal.edu.co"),
        }
        outcomes = {}
        for label, change in cases.items():
            email = change.pop("email")
            flow, code, answer = requested(email, **change)
            gone = poll(flow)[0] if flow else None
            outcomes[label] = (code, gone)
        attempt("refused: another domain, a personal account, another "
                "organization, an unverified address, another client or issuer, "
                "an old answer, an address that cannot be a username",
                lambda: (all(v == (403, 410) for v in outcomes.values())
                         and not user("bo"),
                         str({k: v for k, v in outcomes.items() if v != (403, 410)})
                         or "all refused, the sign-in forgotten"))
        flow, code, answer = requested("cyd@unal.edu.co", "Cyd",
                                       aud=[google.client_id, "another"],
                                       verified="true")
        attempt("an audience given as a list, and verified as the word true, "
                "are read as Google means them",
                lambda: (code == 200 and user("cyd").get("email_verified_at"),
                         f"{code} {answer}"))
        same = [requested("gilberto@unal.edu.co", password=typed)[1]
                for typed in ("gilberto", "GILBERTO", " Gilberto ")]
        attempt("the password cannot be the username Google gives, in any case",
                lambda: (same == [400, 400, 400] and not user("gilberto"), str(same)))
        code, began = start()
        google.deny(began["user_code"])
        denied = poll(began["flow"])[0]
        code, began = start()
        google.expire(began["user_code"])
        expired = poll(began["flow"])[0]
        attempt("a sign-in declined or expired at Google is refused",
                lambda: (denied == 403 and expired == 410, f"{denied}, {expired}"))

        # --- the pace Google asks for -------------------------------------------
        def paced():
            code, began = start()
            flow = entry_for(began)
            flow["interval"], flow["next"] = 30.0, 0.0
            before = google.token_calls
            answers = [poll(began["flow"])[1].get("state") for _ in range(6)]
            asked = google.token_calls - before
            google.slow_down = True
            flow["next"] = 0.0
            poll(began["flow"])
            google.slow_down = False
            return (asked == 1 and answers == ["pending"] * 6
                    and flow["interval"] == 35.0,
                    f"Google asked {asked} time(s) for 6 polls; interval "
                    f"{flow['interval']}")
        attempt("Google is asked at most once per interval, and slower when it "
                "says so", paced)

        def once_only():
            code, began = start()
            google.approve(began["user_code"], "dina@unal.edu.co", "Dina")
            answers = []
            threads = [threading.Thread(target=lambda: answers.append(
                poll(began["flow"]))) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            for _ in range(20):
                if any(a[1].get("state") == "done" for a in answers):
                    break
                answers.append(poll(began["flow"]))
            made = [e for e in database.events(limit=50)
                    if "An account was requested (dina)" in e["message"]]
            return (len(made) == 1 and user("dina").get("email_verified_at")
                    and sum(a[1].get("state") == "done" for a in answers) == 1,
                    f"{len(made)} request(s) made from {len(answers)} polls")
        attempt("polls sent at once for one sign-in make one request", once_only)

        def odd_answers():
            code, began = start()
            outcomes = []
            for flag in ("cut_wait", "html_429"):
                setattr(google, flag, True)
                entry_for(began)["next"] = 0.0
                outcomes.append(poll(began["flow"]))
                setattr(google, flag, False)
            google.approve(began["user_code"], "gus@unal.edu.co", "Gus")
            entry_for(began)["next"] = 0.0
            done = poll(began["flow"])
            again = poll(began["flow"])
            return (all(o == (200, {"ok": True, "state": "pending"}) for o in outcomes)
                    and done[1].get("state") == "done" and again == done
                    and user("gus").get("email_verified_at"),
                    f"{outcomes}; then {done[0]}, again {again[0]}")
        attempt("an answer cut short or an HTML 429 while waiting is a moment's "
                "trouble, not the end of the sign-in", odd_answers)

        def ended_says_so():
            pending = len([u for u in database.users("chamber-1")
                           if u["status"] == "pending"])
            authmod.MAX_PENDING = pending
            try:
                flow, code, answer = requested("jon@unal.edu.co", "Jon")
            finally:
                authmod.MAX_PENDING = saved_pending
            return (code == 429 and answer.get("ended") is True
                    and poll(flow)[0] == 410 and not user("jon"),
                    f"{code} {answer}")
        attempt("a refusal that ends the sign-in says so, with its own reason",
                ended_says_so)

        def record_lost_not_request():
            real = database.add_event
            failures = []

            def failing(*args, **kwargs):
                if "(kim)" in str(args[2] if len(args) > 2 else ""):
                    failures.append(1)
                    raise sqlite3.OperationalError("disk full (selftest)")
                return real(*args, **kwargs)
            database.add_event = failing
            caught, stderr = io.StringIO(), sys.stderr
            sys.stderr = caught
            try:
                flow, code, answer = requested("kim@unal.edu.co", "Kim")
                first = user("kim").get("uuid")
                again = [poll(flow) for _ in range(3)]
            finally:
                database.add_event = real
                sys.stderr = stderr
            return (code == 200 and answer.get("state") == "done" and failures
                    and all(a == (200, answer) for a in again)
                    and user("kim").get("uuid") == first,
                    f"{code}; {len(failures)} record(s) failed; the request kept: "
                    f"{user('kim').get('uuid') == first}")
        attempt("a record that cannot be written costs the log line, never a "
                "second request", record_lost_not_request)

        def decided_as_checked():
            requested("ivo@unal.edu.co", "Ivo")
            ivo = user("ivo")
            wrong = [database.activate_pending(ivo["id"], "another-uuid"),
                     database.delete_pending(ivo["id"], "another-uuid")]
            return (wrong == [False, False] and user("ivo").get("status") == "pending",
                    str(wrong))
        attempt("approving or declining writes only the request that was checked",
                decided_as_checked)

        # --- Google unreachable or refusing, and secrets -------------------------
        caught, real = io.StringIO(), sys.stderr
        sys.stderr = caught
        try:
            code, began = start()
            google.down = True
            outage = poll(began["flow"])
            refused_start = start()
            google.down = False
            google.approve(began["user_code"], "dee@unal.edu.co", "Dee")
            entry_for(began)["next"] = 0.0
            recovered = poll(began["flow"])
            code, began = start()
            google.approve(began["user_code"], "eva@unal.edu.co", "Eva")
            google.client_secret = "rotated"
            entry_for(began)["next"] = 0.0
            rotated = poll(began["flow"])
            google.client_secret = "GOCSPX-test-secret"
            rotated_after = poll(began["flow"])[0]
            code, began = start()
            google.approve(began["user_code"], "fio@unal.edu.co", "Fio")
            google.garbage = True
            garbled = poll(began["flow"])[0]
            google.garbage = False
            garbled_after = poll(began["flow"])[0]
        finally:
            sys.stderr = real
            google.down = google.garbage = False
            google.client_secret = "GOCSPX-test-secret"
        pages = (json.dumps(call("/api/ping")[1])
                 + json.dumps(call("/api/users", token=owner)[1])
                 + json.dumps(call("/api/state", token=owner)[1]))
        attempt("Google out of reach: a sign-in waits rather than failing, a new "
                "one is refused plainly",
                lambda: (outage == (200, {"ok": True, "state": "pending"})
                         and refused_start[0] == 503
                         and recovered[1].get("state") == "done",
                         f"outage {outage}; new {refused_start[0]}; then "
                         f"{recovered[1].get('state')}"))
        attempt("a refusal that asking again cannot mend -- a rotated secret, an "
                "answer that is not one -- ends the sign-in instead of waiting "
                "30 minutes", lambda: (rotated[0] == 503 and rotated_after == 410
                                       and garbled == 503 and garbled_after == 410
                                       and not user("eva") and not user("fio"),
                                       f"rotated {rotated[0]} then {rotated_after}; "
                                       f"garbled {garbled} then {garbled_after}"))
        attempt("the client secret and the passwords are in no page, reply or "
                "log line", lambda: (all(secret not in caught.getvalue()
                                         + json.dumps(refused_start[1])
                                         + json.dumps(rotated[1]) + pages
                                         for secret in (google.client_secret,
                                                        "good-pass-1", "her-pass-1"))
                                     and "google sign-in" in caught.getvalue(),
                                     "nothing found"))

        def no_redirect():
            seen = []

            class Elsewhere(http.server.BaseHTTPRequestHandler):
                def do_GET(self):
                    seen.append(self.path)
                    self.send_response(200)
                    self.end_headers()
                do_POST = do_GET

                def log_message(self, *args):
                    pass
            far = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)

            class Redirect(Elsewhere):
                def do_POST(self):
                    self.rfile.read(int(self.headers.get("Content-Length") or 0))
                    self.send_response(302)        # urllib would follow, as a GET
                    self.send_header("Location", f"http://127.0.0.1:"
                                                 f"{far.server_address[1]}/x")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
            near = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
            for one in (far, near):
                threading.Thread(target=one.serve_forever, daemon=True).start()
            try:
                status, _ = googlemod._post(f"http://127.0.0.1:{near.server_address[1]}/t",
                                            {"client_secret": "s3cret"})
            finally:
                for one in (far, near):
                    one.shutdown()
                    one.server_close()
            return status == 302 and not seen, f"HTTP {status}; followed: {bool(seen)}"
        attempt("a redirect from Google is not followed", no_redirect)

        # --- limits ---------------------------------------------------------------
        accounts._google_starts = authmod.Budget(1, 3600.0)
        per_address = [start()[0], start()[0]]
        accounts._google_starts = authmod.Budget(100, 3600.0)
        with accounts._flows_lock:
            waiting = len(accounts._flows)
        authmod.GOOGLE_FLOWS_MAX = waiting + 1
        before = accounts._google_starts_all.wait("all")
        crowded = [start()[0], start()[0]]
        authmod.GOOGLE_FLOWS_MAX = saved[2]
        attempt("sign-ins started per address, and waited for at once, are "
                "limited -- the crowded one costing nobody's hourly budget",
                lambda: (per_address == [200, 429] and crowded == [200, 429]
                         and before == 0.0, f"{per_address}, {crowded}"))

        # --- one's own request, and the request an admin was shown -----------------
        def own_request():
            requested("fay@unal.edu.co", "Fay", password="fay-pass-1")
            first = user("fay")
            report = next(p for p in call("/api/users", token=owner)[1]["people"]
                          if p["username"] == "fay")
            requested("fay@unal.edu.co", "Fay", password="fay-pass-2",
                      note="second try")
            second = user("fay")
            stale = call("/api/users/approve", {"id": report["id"], "role": "operator",
                                                "created_at": report["created_at"]},
                         owner)
            stale_decline = call("/api/users/decline", {
                "id": report["id"], "created_at": report["created_at"]}, owner)[0]
            logged = [e["message"] for e in database.events(limit=30)
                      if "(fay)" in e["message"] and "replacing" in e["message"]]
            current = call("/api/users/approve", {
                "id": second.get("id"), "role": "viewer",
                "created_at": second.get("created_at")}, owner)[0]
            flow, taken, _ = requested("ana.perez@unal.edu.co", "Ana",
                                       password="x-pass-1")
            return (second.get("uuid") not in (None, first.get("uuid"))
                    and second.get("note") == "second try"
                    and stale[0] == 409 and "again" in stale[1].get("error", "")
                    and stale_decline == 409 and bool(logged) and current == 200
                    and call("/api/login", {"username": "fay",
                                            "password": "fay-pass-2"})[0] == 200
                    and taken == 409 and user("ana.perez").get("status") == "active",
                    f"stale approve {stale[0]}, decline {stale_decline}; current "
                    f"{current}; replacement logged {bool(logged)}; an active "
                    f"account {taken}")
        attempt("a verified person may make their own waiting request again; an "
                "admin decides only the request they were shown; an account in "
                "use is never replaced", own_request)

        def unverified_kept_out():
            pending = user("old.request")
            refused = call("/api/users/approve", {"id": pending.get("id"),
                                                  "role": "viewer"}, owner)
            listed = users_tool("list").stdout
            without = users_tool("role", "old.request", "viewer")
            told = users_tool("role", "old.request", "viewer", "--unconfirmed")
            return (refused[0] == 409 and "Google" in refused[1].get("error", "")
                    and "NOT verified" in listed and "verified with Google" in listed
                    and without.returncode == 1 and told.returncode == 0
                    and user("old.request").get("status") == "active",
                    f"approve {refused[0]}; on the machine {without.returncode} "
                    f"then {told.returncode}")
        attempt("a request made before Google was set up is not approved in the "
                "panel; on the machine only when told so", unverified_kept_out)

        def fails_closed():
            call("/api/signup", {})                  # nothing; just the route
            database.create_user("hal", "", status="pending")
            outcomes = {}
            for label in ("unreadable", "broken"):
                if label == "unreadable":
                    os.chmod(google_path, 0)
                else:
                    os.chmod(google_path, 0o600)
                    with open(google_path) as fh:
                        good = fh.read()
                    with open(google_path, "w") as fh:
                        fh.write(good[:10])
                typed = call("/api/signup", {"username": "victim.name",
                                             "password": "v-pass-1"})[0]
                began = start()[0]
                approve = call("/api/users/approve", {"id": user("hal").get("id"),
                                                      "role": "viewer"}, owner)[0]
                live = call("/api/ping")[1]["auth"]["google_signin"]
                told = call("/api/users", token=owner)[1]["email"].get("usable")
                outcomes[label] = (typed, began, approve, live, told)
            os.chmod(google_path, 0o600)
            with open(google_path, "w") as fh:
                fh.write(good)
            return (all(v == (400, 503, 409, True, False) for v in outcomes.values())
                    and not user("victim.name"), str(outcomes))
        if os.geteuid() != 0:                    # root reads a mode-000 file
            attempt("a client file that cannot be read, or is broken, takes "
                    "nothing: no typed request, no sign-in, no approval of an "
                    "unverified one", fails_closed)

        def old_store():
            path = os.path.join(directory, "before.db")
            with sqlite3.connect(path) as old:
                old.execute("CREATE TABLE email_codes (username TEXT PRIMARY KEY)")
            reopened = dbmod.Database(path)
            try:
                left = {r[0] for r in reopened._db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                columns = {r[1] for r in reopened._db.execute(
                    "PRAGMA table_info(users)")}
            finally:
                reopened.close()
            return ("email_codes" not in left and "email_verified_at" in columns,
                    sorted(left)[:6])
        attempt("the mailed codes' table of an older database is dropped", old_store)
        off = users_tool("google", "off").returncode
        attempt("google off stops the checking", lambda: (
            off == 0 and call("/api/ping")[1]["auth"]["google_signin"] is False, str(off)))
    finally:
        (authmod.SIGNUPS_PER_HOUR, authmod.GOOGLE_POLL_MIN_S,
         authmod.GOOGLE_FLOWS_MAX, authmod.GOOGLE_STARTS_PER_HOUR,
         authmod.GOOGLE_STARTS_PER_HOUR_ALL,
         googlemod.DEVICE_URL, googlemod.TOKEN_URL) = saved
        google.close()
        server.shutdown()
        server.server_close()
        engine.registry.close()
        database.close()
