#!/usr/bin/env python3
"""Accounts, from the machine the panel runs on.

The panel can approve sign-ups, change roles and reset passwords, but it
cannot make the first account: whoever reaches the network first would own
the rig. The owner is made here, by somebody with a shell on the machine:

    ./tools/users.py owner esteban --name "Esteban Rodriguez"
    ./tools/users.py list
    ./tools/users.py passwd ana            # a new password; signs ana out
    ./tools/users.py role ana operator
    ./tools/users.py disable ana

`owner` again for the same name is the way back in for an owner who forgot
the password. Safe to run while the panel is running: both use the same
SQLite file, and the panel reads accounts from it on every request.

A username is the person's institutional address before the @ (webserver.py
--email-domain, unal.edu.co by default). A request for an account proves it
by typing a code mailed there, once the panel has an account to send from:

    ./tools/users.py mail set --host smtp.gmail.com --sender "Vacuum Lab <lab@...>"
    ./tools/users.py mail test someone@unal.edu.co
    ./tools/users.py mail show | off

The settings go in mail.json beside the database, readable by this account
only, and are never sent to the panel's pages nor copied by backup.py.

Passwords are asked for on the terminal, or read from standard input when
it is not one (`echo ... | ./tools/users.py owner NAME` over ssh).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lab import auth, db as dbmod, mailer  # noqa: E402


def ask_password(prompt: str = "Password") -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"{prompt}: ")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("The two passwords differ.")
    return first


def ask_secret(prompt: str) -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    return getpass.getpass(f"{prompt}: ")


def mail_command(args) -> int:
    path = os.path.join(os.path.dirname(os.path.abspath(
        args.db or dbmod.default_path())), mailer.SETTINGS_FILE)
    if args.mail_command == "set":
        settings = {"host": args.host.strip(), "port": args.port,
                    "security": args.security, "username": args.user.strip(),
                    "password": ask_secret("Password of the sending account")
                    if args.user.strip() else "",
                    "sender": args.sender.strip()}
        mailer.save_settings(path, settings)
        print(f"Saved {path} (0600). Try it: {sys.argv[0]} mail test ADDRESS")
        return 0
    if args.mail_command == "off":
        if os.path.exists(path):
            os.unlink(path)
        print("No mail account: requests' addresses are no longer checked.")
        return 0
    settings = mailer.Mailer(path).settings()
    if settings is None:
        print(f"No usable mail account in {path}.")
        return 1 if args.mail_command == "test" else 0
    if args.mail_command == "show":
        shown = {k: v for k, v in settings.items() if k != "password"}
        shown["password"] = "set" if settings.get("password") else "none"
        for key, value in shown.items():
            print(f"{key:<9} {value}")
        return 0
    try:
        mailer.Mailer(path).send(args.address, "Test from the vacuum lab panel",
                                 "This account can send the panel's sign-up "
                                 "codes.\n")
    except Exception as exc:                      # MailError, or a bug in it
        print(f"Not sent: {exc if isinstance(exc, mailer.MailError) else type(exc).__name__}",
              file=sys.stderr)
        return 1
    print(f"Sent to {args.address}.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=None,
                        help=f"database (default {dbmod.default_path()})")
    parser.add_argument("--chamber", default="chamber-1",
                        help="chamber the roles apply to (webserver.py "
                             "--chamber-id)")
    commands = parser.add_subparsers(dest="command", required=True)

    owner = commands.add_parser("owner", help="create the owner, or give the "
                                              "owner a new password")
    owner.add_argument("username")
    owner.add_argument("--name", default="", help="name shown in the panel")
    owner.add_argument("--replace", action="store_true",
                       help="hand ownership to this account; the previous "
                            "owner becomes an admin")

    commands.add_parser("list", help="every account and its role")

    passwd = commands.add_parser("passwd", help="set a new password; ends "
                                                "the account's sessions")
    passwd.add_argument("username")

    unconfirmed = ("let in a request that never confirmed its institutional "
                   "address -- only after checking it is the person it names: "
                   "while unconfirmed, the request may be anybody's")
    role = commands.add_parser("role", help="set a role: viewer, operator, "
                                            "admin")
    role.add_argument("username")
    role.add_argument("role", choices=("viewer", "operator", "admin"))
    role.add_argument("--unconfirmed", action="store_true", help=unconfirmed)

    for name, text in (("disable", "stop an account signing in"),
                       ("enable", "let a disabled or pending account in")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("username")
        if name == "enable":
            sub.add_argument("--unconfirmed", action="store_true", help=unconfirmed)

    mail = commands.add_parser("mail", help="the account that mails sign-up "
                                            "codes to institutional addresses")
    mail_commands = mail.add_subparsers(dest="mail_command", required=True)
    setting = mail_commands.add_parser("set", help="set the sending account "
                                                   "(asks for its password)")
    setting.add_argument("--host", required=True, help="e.g. smtp.gmail.com")
    setting.add_argument("--port", type=int, default=587)
    setting.add_argument("--security", choices=mailer.SECURITY,
                         default="starttls", help="starttls (587), ssl (465), "
                                                  "or none for a local relay")
    setting.add_argument("--user", default="", help="the login, usually the "
                                                    "sender's address")
    setting.add_argument("--sender", required=True,
                         help='"Vacuum Lab <lab@example.org>"')
    mail_commands.add_parser("show", help="the settings, without the password")
    trying = mail_commands.add_parser("test", help="send a test message")
    trying.add_argument("address")
    mail_commands.add_parser("off", help="remove the settings")

    args = parser.parse_args()
    if args.command == "mail":
        try:
            return mail_command(args)
        except ValueError as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 1
    database = dbmod.Database(args.db)
    try:
        return run(args, database)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    finally:
        database.close()


def run(args, database: "dbmod.Database") -> int:
    chamber = args.chamber

    if args.command == "owner":
        action = auth.bootstrap_owner(database, chamber, args.username,
                                      ask_password(), args.name, args.replace)
        print(f"Owner of {chamber} {action}: {args.username.lower()}")
        return 0

    if args.command == "list":
        rows = database.users(chamber)
        if not rows:
            print(f"No accounts. Create the owner: {sys.argv[0]} owner NAME")
            return 0
        for user in rows:
            seen = user.get("last_login_at")
            waiting = ""
            if user["kind"] == "person" and user["status"] == "pending":
                age = max(0, time.time() - user["created_at"]) / 60
                waiting = (f"  [address {'confirmed' if user.get('email_verified_at') else 'NOT confirmed'}, "
                           f"asked {age:.0f} min ago]")
            print(f"{user['username']:<20} {user['kind']:<7} "
                  f"{user['status']:<9} {user['role'] or '-':<9} "
                  f"{user['display_name']}"
                  + (f"  (last sign-in {time.strftime('%Y-%m-%d %H:%M', time.localtime(seen))})"
                     if seen else "") + waiting)
        return 0

    user = database.user_by_name(args.username.strip().lower())
    if user is None:
        raise ValueError(f"no account named {args.username!r}")
    # While the panel mails codes, a pending request whose address was never
    # confirmed may be anybody's -- somebody else may have asked first for
    # this name. Letting it in from here takes saying so.
    mail_path = os.path.join(os.path.dirname(os.path.abspath(
        args.db or dbmod.default_path())), mailer.SETTINGS_FILE)
    if (args.command in ("role", "enable") and user["kind"] == "person"
            and user["status"] == "pending" and not user.get("email_verified_at")
            and mailer.Mailer(mail_path).configured
            and not getattr(args, "unconfirmed", False)):
        raise ValueError(f"{user['username']} has not confirmed the address the "
                         "code was mailed to, so this request may not be theirs. "
                         "Check with the person, then add --unconfirmed")

    if args.command == "passwd":
        if user["kind"] != "person":
            raise ValueError("a tablet has no password; pair it again instead")
        password = ask_password("New password")
        problem = auth.check_password(password, user["username"])
        if problem:
            raise ValueError(problem)
        database.update_user(user["id"], password_hash=auth.hash_password(password),
                             must_change=0)
        database.delete_sessions(user["id"])
        print(f"Password set for {user['username']}; their sessions ended.")
    elif args.command == "role":
        if database.role(user["id"], chamber) == "owner":
            raise ValueError("that is the owner; hand ownership over first "
                             "(owner NEWNAME --replace)")
        database.set_role(user["id"], chamber, args.role, "tools/users.py")
        if user["status"] == "pending":
            database.update_user(user["id"], status="active")
        print(f"{user['username']} is now {args.role} in {chamber}.")
    elif args.command == "disable":
        if database.role(user["id"], chamber) == "owner":
            raise ValueError("the owner cannot be disabled")
        database.update_user(user["id"], status="disabled")
        database.delete_sessions(user["id"])
        print(f"{user['username']} disabled; their sessions ended.")
    elif args.command == "enable":
        database.update_user(user["id"], status="active")
        if database.role(user["id"], chamber) is None:
            database.set_role(user["id"], chamber, "viewer", "tools/users.py")
        print(f"{user['username']} enabled as "
              f"{database.role(user['id'], chamber)}.")
    database.add_event(None, "action", f"Account {user['username']}: "
                                       f"{args.command} on the machine"
                                       + (" without a confirmed address"
                                          if getattr(args, "unconfirmed", False)
                                          else ""),
                       user=user["username"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except sqlite3.Error as exc:
        raise SystemExit(f"database error: {exc}")
