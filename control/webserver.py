#!/usr/bin/env python3
"""Vacuum lab control panel: one chamber, its instruments from its file.

Runs on the machine physically wired to the instruments and serves both the
web UI and a JSON API:

    ./webserver.py                                   # chambers/chamber-1.toml
    ./webserver.py --chamber chambers/chamber-2.toml --port 8601

Which instruments the chamber has -- the turbopump's drive unit, the gauge
controller -- and their ports and settings are in its file (lab/chamber.py).
`--serial`, `--gauge` and the other instrument flags override the file's
values for one start.

Each instrument gets a background sampler that owns its serial port and
refreshes a cached snapshot. Everything else -- HTTP handlers, the test
engine, the interlock timers -- reads those caches, which keeps the UI fast
and means process timers are measured from continuous observation rather than
from whenever a browser last polled.

Either instrument can be left out -- by the chamber's file, or `--no-pump`,
`--no-gauge` -- and the panel still starts without it. That matters during
bring-up: a gauge that is not wired yet must not stop you driving the pump.
Starting the turbopump is the exception -- that is gated on the chamber
pressure, so with no gauge it is refused until an admin overrides it.

Everybody signs in (lab/auth.py). The first account, the chamber's owner,
is made on this machine -- `./tools/users.py owner NAME` -- and approves
everyone else's sign-up from Settings -> People.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab import (alarms, api, auth, chamber as chambermod,  # noqa: E402
                 db as dbmod, discovery, history, mailer, process, pump,
                 registry, runs)


def build(args) -> api.Application:
    """Assemble the registry, store, process hardware and engine."""
    devices = registry.Registry()
    chamber = args.chamber_config

    # Argon, gate valve, magnetron and shutter. None is wired yet, so all
    # four start manual: recipes prompt the operator instead of driving them.
    # Attach a driver here as each is connected -- nothing else changes.
    hardware = process.ProcessHardware()
    database_path = args.db or dbmod.default_path()
    data_dir = os.path.dirname(os.path.abspath(database_path))

    # Which of that is on the rig at all, and the pump's heater: set from the
    # Settings page, kept beside the database. Nothing counts as installed
    # until somebody says so, and a procedure needing what is not there is
    # switched off. The simulator has everything.
    equipment = process.Equipment(
        None if args.simulate else os.path.join(data_dir, "equipment.json"),
        default=bool(args.simulate), hardware=hardware)

    simulated = None
    if args.simulate:
        # Everything in this process, deliberately: the simulated actuators
        # must move the same chamber object the gauge simulator reads from.
        from tools import fake_actuators, fake_rig
        rig = fake_rig.start_in_process(pressure=args.simulate_pressure)
        fake_actuators.attach(hardware, rig["chamber"])
        simulated = {"pump": rig["pump"], "gauge": rig["gauge"]}
        print("SIMULATION — no hardware in use")
        for role in chamber.instruments:
            print(f"  simulated {role:<6} on {rig[role]}")
        print(f"  argon, gate valve, magnetron and shutter are simulated too,")
        print(f"  so a whole deposition recipe runs end to end.")

    # Which port each instrument is on is found by asking, not assumed: a
    # given port is tried first, and if nothing of the right kind answers
    # there -- or the value is "auto" -- every free port is probed with
    # read-only queries. Simulated ptys are given explicitly and not searched.
    search = not args.no_port_search and not args.simulate
    memory = discovery.PortMemory(os.path.join(data_dir, "ports.json"))
    if not args.simulate:
        print("Looking for the instruments:")

    # One service per instrument the chamber's file lists, the turbopump
    # wired to the gauge for its crossover interlock (lab/chamber.py).
    chambermod.build(chamber, devices, memory, search=search,
                     simulated=simulated)
    # The engine judges procedures by the file's roles (`roles=` below); they
    # must be exactly what is registered, or a procedure could be offered on
    # an instrument that is not there.
    if set(devices.keys()) != set(chamber.instruments):
        raise RuntimeError(f"registered {sorted(devices.keys())}, but "
                           f"{chamber.source} lists {sorted(chamber.instruments)}")

    # The same search, later, for an instrument that goes quiet: a replugged
    # adapter comes back as a different /dev/ttyUSBn.
    finder = discovery.Finder(devices, memory, enabled=search,
                              kinds=[i.kind for i in chamber.instruments.values()])
    for service in devices:
        service.finder = finder

    database = dbmod.Database(args.db)
    engine = engine_for(chamber, database, devices, hardware, equipment)
    # The chart recorder for the pump and gauge pages: the last day of the
    # whole rig, kept whether or not a run is recording. Memory only.
    recorder = history.History(devices, hardware).start()
    # Alarms, and the pump's safe state after a fault -- whether or not a run
    # is recording. It never switches the forevacuum pump off. It keeps the
    # alarms and its own heartbeat in the database, so a restart is said
    # and nothing unacknowledged is lost; and it watches the backups --
    # except in the simulator, whose database is a scratch one.
    backups = None if (args.simulate or args.no_backup_alarm) else \
        os.path.join(data_dir, "backup-status.json")
    supervisor = alarms.Supervisor(
        devices, engine, database,
        auto_recover=not args.no_auto_recovery, persist=True,
        backup_status=backups, recorder=recorder)
    # Who may watch and who may drive, per chamber. Sessions outlive a
    # restart -- they are in the database -- so a deploy does not sign every
    # tablet out; the expired ones are swept here.
    # A username is the person's institutional address before the @; the
    # code that proves it is mailed through the account in mail.json beside
    # the database (tools/users.py mail set), read at each sending.
    accounts = auth.Auth(database, chamber.id, enabled=not args.no_auth,
                         idle_s=args.idle_minutes * 60,
                         run_idle_s=args.run_idle_minutes * 60,
                         runner=engine.runner, email_domain=args.email_domain,
                         mailer=mailer.Mailer(os.path.join(
                             data_dir, mailer.SETTINGS_FILE)))
    database.purge_sessions()
    application = api.Application(devices, database, engine, auth=accounts,
                           history=recorder, supervisor=supervisor,
                           finder=finder,
                           chamber={"id": chamber.id, "name": chamber.name},
                           setup=chamber.describe(),
                           allow=api.parse_networks(args.allow_from),
                           hosts=api.machine_names(
                               [n for n in args.host_name.split(",") if n]),
                           origins=[o for o in args.allow_origin.split(",") if o],
                           admin_from=api.parse_networks(args.admin_from),
                           status_file=status_path(args),
                           version=read_version())
    # Started once the Application has set its status hook.
    supervisor.start()
    return application


def engine_for(chamber, database, devices, hardware=None,
               equipment=None) -> runs.RunEngine:
    """The run engine for `chamber`: its procedures judged by the roles the
    chamber's file lists, as built (`build` checks they are registered)."""
    return runs.RunEngine(database, devices, hardware=hardware,
                          equipment=equipment, chamber=chamber.id,
                          roles=tuple(chamber.instruments))


def read_version() -> dict:
    """VERSION.json beside this file, written by tools/release.py; a working
    copy has none, and calls itself "dev"."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "VERSION.json")
    try:
        with open(path, encoding="utf-8") as fh:
            version = json.load(fh)
        return version if isinstance(version, dict) else {"release": "dev"}
    except (OSError, ValueError):
        return {"release": "dev"}


def status_path(args) -> str | None:
    """Where the panel says what it is doing: --status-file, else systemd's
    runtime directory for the unit (/run/vacuum-lab), else nowhere."""
    if args.status_file:
        return args.status_file
    runtime = os.environ.get("RUNTIME_DIRECTORY")
    return os.path.join(runtime.split(":")[0], "status.json") if runtime else None


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line, checked. tools/uitest.py builds a panel from it too."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--chamber", default=chambermod.DEFAULT,
                        help="the chamber's file: which instruments it has, "
                             "their ports and settings (default "
                             "chambers/chamber-1.toml). The flags below "
                             "override it for this start")
    parser.add_argument("--serial", default=None,
                        help="serial port of the turbopump's drive unit, or "
                             "'auto' to find it by asking each port; a given "
                             "port is tried first and searched from if "
                             "nothing answers there")
    parser.add_argument("--address", type=int, default=None,
                        help="RS-485 address of the TC 600")
    parser.add_argument("--pump-settle-ms", type=float, default=None,
                        help="pause after each telegram to the TC 600 before "
                             "listening for its reply, for the RS-485 "
                             "converter to turn round")
    parser.add_argument("--gauge", default=None,
                        help="serial port of the gauge controller, or "
                             "'auto' to find it by asking each port")
    parser.add_argument("--gauge-baud", type=int, default=None,
                        help="gauge baud rate; omit to detect it")
    parser.add_argument("--gauge-channels", type=int, default=None,
                        help="measurement channels on the gauge controller")
    parser.add_argument("--simulate", action="store_true",
                        help="run the whole rig simulated, including the "
                             "argon, gate valve, magnetron and shutter, so a "
                             "deposition recipe runs end to end with no "
                             "hardware at all")
    parser.add_argument("--simulate-pressure", type=float, default=1013.0,
                        help="starting chamber pressure for --simulate, mbar")
    parser.add_argument("--no-pump", action="store_true",
                        help="run without the turbopump, whatever the "
                             "chamber's file says")
    parser.add_argument("--no-gauge", action="store_true",
                        help="run without the gauge controller, whatever the "
                             "chamber's file says")
    parser.add_argument("--db", default=None,
                        help=f"run database (default {dbmod.default_path()})")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--chamber-id", default=None,
                        help="this rig's identifier, over the file's. Roles "
                             "are granted per chamber, so keep it once "
                             "accounts exist")
    parser.add_argument("--chamber-name", default=None,
                        help="the name the panel shows for this rig, over "
                             "the file's")
    parser.add_argument("--allow-from", default="",
                        help="comma-separated networks allowed to reach the "
                             "panel at all, e.g. 192.168.50.0/24. Loopback is "
                             "always allowed (the tablet on its USB cable). "
                             "Default: any address")
    parser.add_argument("--admin-from", default="",
                        help="comma-separated networks from which what only an "
                             "admin may do -- accounts, overrides, the drive's "
                             "protections, the rig's set-up -- is accepted, "
                             "e.g. the lab router's 192.168.50.0/24. Loopback "
                             "always is. Default: any address. Until the panel "
                             "has TLS, passwords cross the network in clear")
    parser.add_argument("--host-name", default="",
                        help="comma-separated names the panel answers to "
                             "besides its IP addresses and this machine's own "
                             "name, e.g. a domain. Requests naming any other "
                             "host are refused (DNS rebinding)")
    parser.add_argument("--allow-origin", default="",
                        help="comma-separated origins a panel may be served "
                             "from and still use this API, e.g. "
                             "http://192.168.50.10:8600. Default: only the "
                             "panel's own pages")
    parser.add_argument("--idle-minutes", type=float, default=15,
                        help="a person must type their password again to act "
                             "after this long without touching the panel")
    parser.add_argument("--run-idle-minutes", type=float, default=60,
                        help="the same, for the person whose run is going: a "
                             "deposition is mostly watching")
    parser.add_argument("--email-domain", default=auth.EMAIL_DOMAIN,
                        help="usernames are the part of an address at this "
                             "domain before the @; a request for an account "
                             "confirms it with a code mailed there, once a "
                             "mail account is set up (tools/users.py mail "
                             f"set). Default {auth.EMAIL_DOMAIN}")
    parser.add_argument("--no-auth", action="store_true",
                        help="switch accounts off: anyone who reaches the port "
                             "is the owner. For the simulator on a laptop, "
                             "never for the lab")
    parser.add_argument("--no-port-search", action="store_true",
                        help="use exactly the ports given; never probe other "
                             "ports for the instruments")
    parser.add_argument("--status-file", default="",
                        help="where to write what the panel is doing, every "
                             "few seconds (default: the systemd runtime "
                             "directory, /run/vacuum-lab/status.json)")
    parser.add_argument("--no-backup-alarm", action="store_true",
                        help="do not alarm about this database's backups -- "
                             "for a machine whose backups are made elsewhere")
    parser.add_argument("--no-auto-recovery", action="store_true",
                        help="raise alarms on a drive fault but do not make "
                             "the pump safe automatically (motor off, fault "
                             "acknowledged, forevacuum pump kept on)")
    args = parser.parse_args(argv)
    for flag, value in (("--allow-from", args.allow_from),
                        ("--admin-from", args.admin_from)):
        try:
            api.parse_networks(value)
        except ValueError as exc:
            parser.error(f"{flag}: {exc}")
    try:
        args.chamber_config = chamber_of(args)
    except chambermod.ChamberError as exc:
        parser.error(str(exc))
    return args


def chamber_of(args) -> chambermod.Chamber:
    """The chamber's file, with the command line over it."""
    chamber = chambermod.load(args.chamber)
    if args.chamber_id is not None:
        chamber.id = chambermod.check_id(args.chamber_id)
    if args.chamber_name:
        chamber.name = args.chamber_name
    for role, leave_out in (("pump", args.no_pump), ("gauge", args.no_gauge)):
        if leave_out:
            chamber.instruments.pop(role, None)
    if not args.no_pump and any(v is not None for v in (
            args.serial, args.address, args.pump_settle_ms)):
        chambermod.override(chamber, "pump", "--serial/--address/--pump-settle-ms",
                            port=args.serial, address=args.address,
                            settle_ms=args.pump_settle_ms)
    if not args.no_gauge and any(v is not None for v in (
            args.gauge, args.gauge_baud, args.gauge_channels)):
        chambermod.override(chamber, "gauge", "--gauge/--gauge-baud/--gauge-channels",
                            port=args.gauge, baudrate=args.gauge_baud,
                            channels=args.gauge_channels)
    return chamber


def main() -> int:
    args = parse()
    application = build(args)
    api.Handler.app = application

    httpd = api.Server((args.bind, args.port), api.Handler)
    chamber = args.chamber_config
    print(f"Vacuum lab panel for {chamber.name} ({chamber.id}) "
          f"on http://{args.bind}:{args.port}/")
    print(f"  instruments from {os.path.relpath(chamber.source)}"
          + (f" -- none for: {', '.join(chamber.describe()['absent'])}"
             if chamber.describe()["absent"] else ""))
    for device in application.registry:
        print(f"  {device.label}: {device.port}")
    print(f"  database: {application.db.path}")
    print(f"  release: {application.version.get('release')}"
          + (f", status in {application.status_file}"
             if application.status_file else ""))
    if "pump" in chamber.instruments:
        print(f"  interlocks: the turbopump may not start above "
              f"{pump.TURBO_START_MAX_MBAR:.0E} mbar; the station may not stop "
              f"until {pump.STOP_DWELL_SECONDS:.0f}s after the rotor reads 0 Hz")
    unconfigured = application.engine.unconfigured_all()
    if unconfigured:
        names = [runs.CATALOGUE[name].label for name in unconfigured]
        print(f"  switched off, no such instrument on this chamber: "
              f"{', '.join(names)}")
    absent = [name for name in application.engine.unavailable()
              if name not in unconfigured]
    if absent:
        names = [runs.CATALOGUE[name].label for name in absent]
        print(f"  switched off, equipment not installed: {', '.join(names)}")
        print("  (mark it installed in Settings -> Rig equipment once it is)")
    if "pump" in chamber.instruments and "gauge" not in chamber.instruments:
        print("  NO GAUGE: the chamber pressure is unknown, so starting the")
        print("  turbopump will be refused until an admin overrides it.")
    if args.no_auth:
        print("  ACCOUNTS OFF (--no-auth): anyone who can reach this port can")
        print("  start and stop the pump. For the simulator, not the lab.")
    elif not application.auth.status().get("owner"):
        print("  NO OWNER YET: nobody can sign in until one exists. On this")
        print("  machine: ./tools/users.py owner YOURNAME"
              + (f" --db {args.db}" if args.db else "")
              + (f" --chamber {chamber.id}"
                 if chamber.id != "chamber-1" else ""))
    if not args.no_auth:
        if application.auth.verifying:
            print(f"  account requests confirm their @{args.email_domain} "
                  "address by a mailed code")
        else:
            print(f"  NO MAIL ACCOUNT: requests' @{args.email_domain} addresses "
                  "are not checked;")
            print("  set one up on this machine: ./tools/users.py mail set"
                  + (f" --db {args.db}" if args.db else ""))
    if args.allow_from:
        print(f"  reachable only from: {args.allow_from} (and loopback)")
    if args.admin_from:
        print(f"  administered only from: {args.admin_from} (and loopback)")
    print(f"  answers to: any IP address, "
          f"{', '.join(sorted(application.hosts))}")

    # `systemctl stop` and `restart` send SIGTERM, which by default ends the
    # process on the spot -- with a run's clean-up (the heater, the gas)
    # never run. Treated like Ctrl-C instead: the run is ended properly.
    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...", flush=True)
    finally:
        # A second signal must not cut the clean-up short.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        application.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
