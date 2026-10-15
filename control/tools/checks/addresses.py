"""Institutional addresses: a username is the part before the @, proved by a
code mailed there -- typed back with the request's own password -- before an
admin may approve the request."""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time

from lab import db as dbmod
from lab import registry, runs
from tools.checks.common import attempt


def test_addresses() -> None:
    """Over real HTTP against the real handler, with the fake mail server.

    What it asks: is a username the institutional address before the @, does
    nothing get approved until the code mailed there comes back from the
    person who made the request, can nobody confirm, refill or take over
    somebody else's request -- and does a server with no mail account behave
    as before, and say so.
    """
    import json as jsonmod
    import sqlite3
    import urllib.error
    import urllib.request
    from lab import api as apimod
    from lab import auth as authmod
    from lab import mailer as mailermod
    from tools.fake_smtp import FakeSMTP

    print("\nInstitutional addresses: the username, and the code mailed there")
    directory = tempfile.mkdtemp(prefix="selftest-addresses-")
    database = dbmod.Database(os.path.join(directory, "addresses.db"))
    mail_path = os.path.join(directory, mailermod.SETTINGS_FILE)
    # Sign-ups and code checks all come from 127.0.0.1 here; the limits on
    # them are checked on their own below. Put back at the end.
    saved = (authmod.SIGNUPS_PER_HOUR, authmod.CODE_CHECKS_PER_MINUTE,
             authmod.CODE_REQUESTS_PER_HOUR, mailermod.SEND_TIMEOUT_S,
             authmod.SEND_LOCK_WAIT_S)
    authmod.SIGNUPS_PER_HOUR = 100
    authmod.CODE_CHECKS_PER_MINUTE = 1000
    authmod.CODE_REQUESTS_PER_HOUR = 1000
    accounts = authmod.Auth(database, "chamber-1",
                            mailer=mailermod.Mailer(mail_path))
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
    smtp = FakeSMTP()
    silent = None

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

    def signup(name, password=None):
        return call("/api/signup", {"username": name, "name": "Zed Quillfeather",
                                    "password": f"{name}-pass-1" if password is None else password})

    def forgive():
        """The wrong passwords a check sent on purpose are forgotten, or the
        throttle on this address's request passwords would hold up every
        check after it (all of them come from 127.0.0.1)."""
        accounts.throttle = authmod.Throttle()

    def confirm(name, code, password=None):
        return call("/api/signup/verify", {"username": name, "code": code,
                                           "password": f"{name}-pass-1" if password is None else password})

    def new_code(name, password=None):
        return call("/api/signup/code", {"username": name,
                                         "password": f"{name}-pass-1" if password is None else password})

    def user(name):
        return database.user_by_name(name) or {}

    def shift(table, column, key, value, seconds):
        """Move a stored time `seconds` into the past."""
        with database._lock:
            database._db.execute(f"UPDATE {table} SET {column} = {column} - ? "
                                 f"WHERE {key}=?", (seconds, value))
            database._db.commit()

    def age_code(name, seconds):
        shift("email_codes", "sent_at", "username", name, seconds)
        shift("email_codes", "expires_at", "username", name, seconds)

    def expire_request(name):
        """Old enough, and its code expired, to be replaced."""
        shift("users", "created_at", "username", name, authmod.EMAIL_CODE_TTL_S + 60)
        age_code(name, authmod.EMAIL_CODE_TTL_S + 60)

    try:
        authmod.bootstrap_owner(database, "chamber-1", "owner", "owner-pass-1")
        owner = call("/api/login", {"username": "owner",
                                    "password": "owner-pass-1"})[1]["token"]

        # --- the username is the address before the @ -------------------------
        def usernames():
            got = {typed: accounts.username_from(typed) for typed in
                   ("anperez", "AnPerez@UNAL.edu.co", "anperez@gmail.com",
                    "an@perez@unal.edu.co", "anperez\n@unal.edu.co")}
            other = authmod.Auth(database, "chamber-1", email_domain="Example.EDU")
            return (got["anperez"] == ("anperez", None)
                    and got["AnPerez@UNAL.edu.co"] == ("anperez", None)
                    and got["anperez@gmail.com"][1] is not None
                    and got["an@perez@unal.edu.co"][1] is not None
                    and got["anperez\n@unal.edu.co"] == ("anperez", None)
                    and authmod.check_username("anperez\n") is not None
                    and other.username_from("x.y@example.edu") == ("x.y", None),
                    str(got)[:150])
        attempt("a username is the institutional address before the @; another "
                "domain, or a hidden line break, is refused", usernames)

        # --- no mail account: as before, and said ------------------------------
        ping = call("/api/ping")[1].get("auth", {})
        code, body = signup("ana")
        approved = call("/api/users/approve", {"id": user("ana").get("id"),
                                               "role": "viewer"}, owner)[0]
        signed = call("/api/login", {"username": "ana@unal.edu.co",
                                     "password": "ana-pass-1"})[0]
        attempt("with no mail account a request is not checked and can be "
                "approved, the sign-in page is told, and the whole address "
                "signs in", lambda: (code == 200 and body.get("verify") is False
                                     and approved == 200 and signed == 200
                                     and ping.get("verify_email") is False
                                     and ping.get("email_domain") == "unal.edu.co",
                                     f"{code} {body}; approve {approved}; "
                                     f"sign-in {signed}; ping {ping}"))
        signup("old")
        shift("users", "created_at", "username", "old", authmod.EMAIL_CODE_TTL_S + 60)
        kept = signup("old")[0]
        attempt("with no mail account no request is ever replaced",
                lambda: (kept == 409, str(kept)))

        # --- a mail account: the code ---------------------------------------
        def ascii_only():
            try:
                mailermod.save_settings(mail_path, {**smtp.settings(),
                                                    "username": "lab",
                                                    "password": "contraseña"})
            except ValueError as exc:
                return not os.path.exists(mail_path), str(exc)
            return False, "saved a password smtplib cannot send"
        attempt("a mail password other than ASCII is refused when it is set",
                ascii_only)
        mailermod.save_settings(mail_path, {**smtp.settings(),
                                            "password": "never-shown-1"})
        mode = os.stat(mail_path).st_mode & 0o777
        ping = call("/api/ping")[1].get("auth", {})
        attempt("the mail settings are 0600, and the panel uses them without a "
                "restart", lambda: (mode == 0o600 and ping.get("verify_email"),
                                    f"mode {oct(mode)}; ping {ping}"))

        code, body = signup("bea@unal.edu.co", "bea-pass-1")
        mailed = smtp.codes("bea@unal.edu.co")
        stored = database.email_code("bea") or {}
        attempt("a request mails one 6-digit code to the address, and keeps only "
                "a hash of it",
                lambda: (code == 200 and body.get("verify") and body.get("sent")
                         and body.get("email") == "bea@unal.edu.co"
                         and len(mailed) == 1
                         and mailed[0] not in stored.get("code_hash", mailed[0]),
                         f"{code} {body}; mailed {mailed}"))
        letter = (smtp.sent_to("bea@unal.edu.co") or [""])[0]
        attempt("the mail carries nothing the requester typed but the username",
                lambda: ("Quillfeather" not in letter and "bea" in letter,
                         letter[:80]))
        refused, said = call("/api/users/approve", {"id": user("bea").get("id"),
                                                    "role": "viewer"}, owner)
        attempt("an admin cannot approve it before the code comes back",
                lambda: (refused == 409 and "bea@unal.edu.co" in said.get("error", "")
                         and user("bea")["status"] == "pending",
                         f"{refused}: {said.get('error', '')[:70]}"))
        listed = next((p for p in call("/api/users", token=owner)[1]["people"]
                       if p["username"] == "bea"), {})
        report = call("/api/users", token=owner)[1].get("email", {})
        attempt("the People page shows the address and that it is not confirmed",
                lambda: (listed.get("email") == "bea@unal.edu.co"
                         and listed.get("email_verified_at") is None
                         and report == {"domain": "unal.edu.co", "verifying": True},
                         f"{listed.get('email')}, {listed.get('email_verified_at')}, "
                         f"{report}"))

        right = mailed[0] if mailed else "000000"
        wrong = f"{(int(right) + 1) % 1000000:06d}"
        strangers = [confirm("bea", right, "not-her-pass-1")[0],
                     confirm("bea", right, "")[0],
                     new_code("bea", "not-her-pass-1")[0]]
        attempt("without the request's password nobody can confirm it or have a "
                "new code mailed, even with the right code",
                lambda: (strangers == [403, 403, 403]
                         and not user("bea").get("email_verified_at")
                         and len(smtp.codes("bea@unal.edu.co")) == 1,
                         f"{strangers}"))
        forgive()
        answers = [confirm("bea", wrong)[0] for _ in range(authmod.EMAIL_CODE_TRIES)]
        late = confirm("bea", right)[0]
        attempt(f"{authmod.EMAIL_CODE_TRIES} wrong codes and the right one no "
                "longer works", lambda: (answers == [400] * authmod.EMAIL_CODE_TRIES
                                         and late == 400
                                         and not user("bea").get("email_verified_at"),
                                         f"{answers} then {late}"))
        soon = new_code("bea")[0]
        age_code("bea", authmod.EMAIL_RESEND_S + 1)
        again, _ = new_code("bea")
        newest = smtp.codes("bea@unal.edu.co")[-1:]
        confirmed = confirm("bea@unal.edu.co", f" {newest[0]} " if newest else "",
                            "bea-pass-1")[0]
        approved = call("/api/users/approve", {"id": user("bea").get("id"),
                                               "role": "viewer"}, owner)[0]
        logged = [e["message"] for e in database.events(limit=20)
                  if "confirmed their institutional address" in e["message"]]
        signed = call("/api/login", {"username": "BEA@unal.edu.co",
                                     "password": "bea-pass-1"})[0]
        attempt("a new code waits a minute; typed back with the password, the "
                "address is confirmed, logged, the request can be approved, and "
                "the whole address signs in",
                lambda: (soon == 429 and again == 200 and confirmed == 200
                         and approved == 200 and bool(logged) and signed == 200
                         and not (database.email_code("bea") or {}).get("code_hash"),
                         f"too soon {soon}; again {again}; confirm {confirmed}; "
                         f"approve {approved}; {len(logged)} logged; sign-in {signed}"))

        signup("cal")
        age_code("cal", authmod.EMAIL_CODE_TTL_S + 60)
        expired = confirm("cal", (smtp.codes("cal@unal.edu.co") or ["0"])[-1])[0]
        attempt("an expired code is refused", lambda: (expired == 400, str(expired)))

        # --- taking over somebody else's name -------------------------------
        signup("anperez", "ana-real-pass-1")
        expire_request("anperez")
        code, body = signup("anperez", "mallory-pass-1")
        theirs = (smtp.codes("anperez@unal.edu.co") or ["0"])[-1]
        hijack = confirm("anperez", theirs, "ana-real-pass-1")[0]
        forgive()
        attempt("a request taken over after its code expired cannot be confirmed "
                "by the address's owner for the one who took it",
                lambda: (code == 200 and body.get("sent") and hijack == 403
                         and not user("anperez").get("email_verified_at"),
                         f"take-over {code}; owner's code and password: {hijack}"))

        signup("cid")
        with database._lock:
            database._db.execute("UPDATE email_codes SET sends=?, sent_at=sent_at-? "
                                 "WHERE username='cid'",
                                 (authmod.EMAIL_SENDS_PER_DAY, 120))
            database._db.commit()
        capped = new_code("cid")[0]
        expire_request("cid")
        code, body = signup("cid", "another-pass-1")
        attempt(f"at most {authmod.EMAIL_SENDS_PER_DAY} codes a day to one address, "
                "whoever asks and however often the request is replaced",
                lambda: (capped == 429 and code == 200 and body.get("sent") is False
                         and (database.email_code("cid") or {}).get("sends")
                         == authmod.EMAIL_SENDS_PER_DAY,
                         f"then {capped}; replaced: {code} sent {body.get('sent')}"))

        def leaves_alone():
            answers = {}
            # Confirmed but not yet approved.
            signup("dot")
            confirm("dot", (smtp.codes("dot@unal.edu.co") or ["0"])[-1])
            # Disabled, and a tablet.
            database.create_user("eli", "", status="disabled")
            database.create_user("wall", "", kind="device", status="active")
            for name in ("dot", "eli", "wall"):
                shift("users", "created_at", "username", name,
                      authmod.EMAIL_CODE_TTL_S + 60)
                answers[name] = signup(name)[0]
            return (answers == {"dot": 409, "eli": 409, "wall": 409}
                    and user("dot").get("email_verified_at"), str(answers))
        attempt("a confirmed request, a disabled account and a tablet are never "
                "replaced", leaves_alone)
        confirmed_name = signup("bea")[0]
        attempt("nor an approved account", lambda: (confirmed_name == 409,
                                                     str(confirmed_name)))

        def parallel_tries():
            signup("fox")
            right = (smtp.codes("fox@unal.edu.co") or ["0"])[-1]
            wrong = f"{(int(right) + 1) % 1000000:06d}"
            threads = [threading.Thread(target=confirm, args=("fox", wrong))
                       for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            tries = (database.email_code("fox") or {}).get("tries", 0)
            after = confirm("fox", right)[0]
            return (tries <= authmod.EMAIL_CODE_TRIES
                    and (tries < authmod.EMAIL_CODE_TRIES or after == 400),
                    f"{tries} tries counted; the right code then {after}")
        attempt("wrong codes sent in parallel never get more than the tries "
                "allowed", parallel_tries)

        # --- the mail server failing --------------------------------------------
        smtp.close()
        code, body = signup("dee")
        counted = database.email_code("dee")
        smtp = FakeSMTP()
        mailermod.save_settings(mail_path, smtp.settings())
        later = new_code("dee")[0]
        attempt("a mail server that cannot be reached: the request is kept, "
                "nothing is counted, the reason is not shown, and a code can be "
                "asked for later",
                lambda: (code == 200 and body.get("sent") is False
                         and "127.0.0.1" not in str(body.get("problem"))
                         and user("dee").get("status") == "pending"
                         and counted is None and later == 200
                         and len(smtp.codes("dee@unal.edu.co")) == 1,
                         f"{code} {body}; later {later}"))

        def nothing_leaks():
            real = accounts.mailer.send

            def broken(*args, **kwargs):
                raise UnicodeEncodeError("ascii", "secret-ñ-word", 7, 8,
                                         "ordinal not in range(128)")
            accounts.mailer.send = broken
            try:
                code, body = signup("gil")
            finally:
                accounts.mailer.send = real
            text = jsonmod.dumps(body, ensure_ascii=False)
            return (code == 200 and body.get("sent") is False
                    and "secret" not in text and "ñ" not in text, text[:120])
        attempt("a mailer failing in any way tells the person asking nothing of "
                "it", nothing_leaks)

        def hung_server():
            nonlocal silent
            silent = socket.socket()
            silent.bind(("127.0.0.1", 0))
            silent.listen(8)                     # accepts, never says a word
            mailermod.save_settings(mail_path, {**smtp.settings(),
                                                "port": silent.getsockname()[1]})
            mailermod.SEND_TIMEOUT_S = 2.0
            authmod.SEND_LOCK_WAIT_S = 0.5
            first = {}
            holder = threading.Thread(target=lambda: first.update(
                answer=signup("hal")))
            holder.start()
            time.sleep(0.3)
            began = time.monotonic()
            second = signup("ida")
            pinged = call("/api/ping")[0]
            took = time.monotonic() - began
            holder.join()
            mailermod.save_settings(mail_path, smtp.settings())
            return (second[0] == 200 and second[1].get("sent") is False
                    and "busy" in str(second[1].get("problem"))
                    and pinged == 200 and took < 1.5
                    and first.get("answer", (0, {}))[1].get("sent") is False,
                    f"second {second}; ping {pinged} after {took:.1f} s")
        attempt("a mail server that never answers holds up one request at most; "
                "the next is told the panel is busy, and the panel answers",
                hung_server)

        # --- what never leaves ------------------------------------------------
        bodies = (jsonmod.dumps(call("/api/ping")[1])
                  + jsonmod.dumps(call("/api/users", token=owner)[1])
                  + jsonmod.dumps(call("/api/state", token=owner)[1]))
        copy = os.path.join(directory, "copy.db")
        database.snapshot(copy)
        with sqlite3.connect(copy) as connection:
            tables = {r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        attempt("the mail password is in no page, and the codes are in no "
                "download", lambda: ("never-shown-1" not in bodies
                                     and "email_codes" not in tables,
                                     f"tables {sorted(tables)}"))

        def header_injection():
            try:
                mailermod.Mailer(mail_path).send("a@unal.edu.co\r\nBcc: b@x.org",
                                                 "subject", "body")
            except mailermod.MailError:
                return True, "refused"
            return False, "sent with a line break in the address"
        attempt("the mailer refuses a line break in an address", header_injection)

        # --- what a stranger's wrong passwords cost -----------------------------
        forgive()
        signup("sam")
        refusals = [confirm("sam", "123456", "not-sams-pass-1")[0] for _ in range(6)]
        owner_in = call("/api/login", {"username": "owner",
                                       "password": "owner-pass-1"})[0]
        attempt("wrong passwords for a request do not hold up sign-in from the "
                "same address (the kiosk is one address)",
                lambda: (refusals[0] == 403 and owner_in == 200,
                         f"{refusals}; then the owner's sign-in {owner_in}"))
        forgive()

        def strangers_spend_nothing():
            accounts._code_checks = authmod.Budget(3)
            spent = [accounts.verify_email("sam", "not-sams-pass-1", "123456",
                                           "10.0.0.9").get("status")
                     for _ in range(4)]
            code = (smtp.codes("sam@unal.edu.co") or ["0"])[-1]
            own = accounts.verify_email("sam", "sam-pass-1", code, "10.0.0.7")
            accounts._code_checks = authmod.Budget(1000)
            return (spent == [403, 403, 403, 429] and own.get("ok"),
                    f"stranger {spent}; then the requester {own}")
        attempt("a stranger's tries do not spend the requester's own",
                strangers_spend_nothing)

        def bound_to_the_request():
            signup("tia")
            made = user("tia")
            return (not database.confirm_email(made["id"], "another-uuid", time.time())
                    and not database.delete_unconfirmed_request(made["id"],
                                                                "another-uuid")
                    and user("tia").get("uuid") == made["uuid"]
                    and not user("tia").get("email_verified_at"),
                    "nothing written for another request's uuid")
        attempt("a confirmation and a replacement apply only to the request they "
                "were checked against", bound_to_the_request)

        def machine_asks_first():
            import subprocess
            import sys
            tool = os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "users.py")
            db_path = os.path.join(directory, "addresses.db")

            def users(*args):
                return subprocess.run([sys.executable, tool, "--db", db_path, *args],
                                      capture_output=True, text=True, timeout=60)
            listed = users("list").stdout
            refused = users("role", "tia", "viewer")
            allowed = users("role", "tia", "viewer", "--unconfirmed")
            return (refused.returncode == 1 and "--unconfirmed" in refused.stderr
                    and allowed.returncode == 0 and "NOT confirmed" in listed
                    and user("tia").get("status") == "active",
                    f"refused {refused.returncode}, then {allowed.returncode}")
        attempt("on the machine, an unconfirmed request is let in only when told "
                "so, and the list says which are unconfirmed", machine_asks_first)

        # --- limits ---------------------------------------------------------------
        signup("ray")
        forgive()
        accounts._code_checks = authmod.Budget(3)
        tries = [confirm("ray", "1")[0] for _ in range(4)]
        attempt("codes typed from one address are limited per minute",
                lambda: (tries[:3] == [400] * 3 and tries[3] == 429, str(tries)))
        accounts._code_requests = authmod.Budget(1, 3600.0)
        asked = [new_code("dee")[0], new_code("dee")[0]]
        attempt("new codes asked for from one address are limited per hour",
                lambda: (asked[1] == 429, str(asked)))
        accounts._mail_sent = authmod.Budget(1, 3600.0)
        hourly = [signup(name)[1].get("sent") for name in ("joe", "kai")]
        accounts._mail_sent = authmod.Budget(100, 3600.0)
        accounts._mail_daily = authmod.Budget(1, 86400.0)
        accounts._mail_daily.take("all")          # the day's mail all sent
        daily = signup("lee")[1]
        attempt("the panel mails at most so much an hour and a day, everybody "
                "together", lambda: (hourly == [True, False]
                                     and daily.get("sent") is False,
                                     f"hour {hourly}; day {daily}"))

        def old_store():
            path = os.path.join(directory, "before.db")
            with sqlite3.connect(path) as old:
                old.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, uuid TEXT "
                            "NOT NULL UNIQUE, username TEXT NOT NULL UNIQUE, "
                            "display_name TEXT NOT NULL DEFAULT '', kind TEXT NOT "
                            "NULL DEFAULT 'person', password_hash TEXT NOT NULL "
                            "DEFAULT '', status TEXT NOT NULL DEFAULT 'pending', "
                            "must_change INTEGER NOT NULL DEFAULT 0, note TEXT "
                            "DEFAULT '', created_at REAL NOT NULL, updated_at REAL "
                            "NOT NULL, last_login_at REAL)")
            reopened = dbmod.Database(path)
            try:
                made = reopened.create_user("fay", "", status="pending")
                reopened.update_user(made["id"], email_verified_at=1.0)
                return reopened.user(made["id"])["email_verified_at"] == 1.0, "migrated"
            finally:
                reopened.close()
        attempt("an older database gains the column", old_store)
    finally:
        (authmod.SIGNUPS_PER_HOUR, authmod.CODE_CHECKS_PER_MINUTE,
         authmod.CODE_REQUESTS_PER_HOUR, mailermod.SEND_TIMEOUT_S,
         authmod.SEND_LOCK_WAIT_S) = saved
        if silent is not None:
            silent.close()
        smtp.close()
        server.shutdown()
        server.server_close()
        engine.registry.close()
        database.close()
