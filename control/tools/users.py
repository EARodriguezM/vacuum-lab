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

from lab import auth, db as dbmod  # noqa: E402


def ask_password(prompt: str = "Password") -> str:
    if not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(f"{prompt}: ")
    if getpass.getpass("Again: ") != first:
        raise SystemExit("The two passwords differ.")
    return first


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

    role = commands.add_parser("role", help="set a role: viewer, operator, "
                                            "admin")
    role.add_argument("username")
    role.add_argument("role", choices=("viewer", "operator", "admin"))

    for name, text in (("disable", "stop an account signing in"),
                       ("enable", "let a disabled or pending account in")):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("username")

    args = parser.parse_args()
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
            print(f"{user['username']:<20} {user['kind']:<7} "
                  f"{user['status']:<9} {user['role'] or '-':<9} "
                  f"{user['display_name']}"
                  + (f"  (last sign-in {time.strftime('%Y-%m-%d %H:%M', time.localtime(seen))})"
                     if seen else ""))
        return 0

    user = database.user_by_name(args.username.strip().lower())
    if user is None:
        raise ValueError(f"no account named {args.username!r}")

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
                                       f"{args.command} on the machine",
                       user=user["username"])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except sqlite3.Error as exc:
        raise SystemExit(f"database error: {exc}")
