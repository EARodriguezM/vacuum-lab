#!/usr/bin/env python3
"""Vacuum lab control panel: TC 600 turbopump + TPG gauge controller.

Runs on the machine physically wired to the instruments and serves both the
web UI and a JSON API:

    ./webserver.py --serial /dev/ttyS1 --gauge /dev/ttyUSB0 --port 8600

Each instrument gets a background sampler that owns its serial port and
refreshes a cached snapshot. Everything else -- HTTP handlers, the test
engine, the interlock timers -- reads those caches, which keeps the UI fast
and means process timers are measured from continuous observation rather than
from whenever a browser last polled.

Either instrument can be left out (`--no-pump`, `--no-gauge`) and the panel
still starts, reporting the missing one as offline. That matters during
bring-up: a gauge that is not wired yet must not stop you driving the pump.
Starting the turbopump is the exception -- that is gated on the chamber
pressure, so with no gauge it is refused until an operator overrides it.
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab import (alarms, api, db as dbmod, discovery, gauge, history,  # noqa: E402
                 process, pump, registry, runs)


def build(args) -> api.Application:
    """Assemble the registry, store, process hardware and engine."""
    devices = registry.Registry()

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

    if args.simulate:
        # Everything in this process, deliberately: the simulated actuators
        # must move the same chamber object the gauge simulator reads from.
        from tools import fake_actuators, fake_rig
        rig = fake_rig.start_in_process(pressure=args.simulate_pressure)
        fake_actuators.attach(hardware, rig["chamber"])
        args.serial, args.gauge, args.gauge_baud = rig["pump"], rig["gauge"], 9600
        print("SIMULATION — no hardware in use")
        print(f"  simulated pump  on {rig['pump']}")
        print(f"  simulated gauge on {rig['gauge']}")
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

    pump_service = gauge_service = None
    pump_port = None
    if not args.no_pump:
        if args.simulate:
            pump_port = args.serial
        else:
            pump_port = discovery.startup("tc600", args.serial, args.address,
                                          memory, search=search)["port"]
        pump_service = devices.add(
            pump.PumpService("pump", pump_port, address=args.address))
    if not args.no_gauge:
        gauge_port, gauge_baud = args.gauge, args.gauge_baud
        if not args.simulate:
            found = discovery.startup("tpg", args.gauge, args.address, memory,
                                      search=search,
                                      exclude=[pump_port] if pump_port else [])
            gauge_port = found["port"]
            if found["found"]:
                gauge_baud = found["found"].get("baudrate") or gauge_baud
        gauge_service = devices.add(gauge.GaugeService(
            "gauge", gauge_port, baudrate=gauge_baud,
            channels=args.gauge_channels))

    # The same search, later, for an instrument that goes quiet: a replugged
    # adapter comes back as a different /dev/ttyUSBn.
    finder = discovery.Finder(devices, memory, enabled=search)
    for service in devices:
        service.finder = finder

    # The turbopump may not start above the crossover pressure, so the pump
    # service needs somewhere to read that pressure from. Wired explicitly
    # rather than discovered: with no gauge the interlock refuses the start
    # instead of guessing, which is the safe direction.
    if pump_service is not None and gauge_service is not None:
        pump_service.attach_pressure(gauge_service)

    database = dbmod.Database(args.db)
    engine = runs.RunEngine(database, devices, hardware=hardware,
                            equipment=equipment)
    # The chart recorder for the pump and gauge pages: the last day of the
    # whole rig, kept whether or not a run is recording. Memory only.
    recorder = history.History(devices, hardware).start()
    # Alarms, and the pump's safe state after a fault -- whether or not a run
    # is recording. It never switches the forevacuum pump off.
    supervisor = alarms.Supervisor(
        devices, engine, database,
        auto_recover=not args.no_auto_recovery).start()
    return api.Application(devices, database, engine, token=args.token,
                           history=recorder, supervisor=supervisor,
                           finder=finder)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", default="auto",
                        help="serial port of the TC 600 (via RS-485), or "
                             "'auto' to find it by asking each port; a given "
                             "port is tried first and searched from if "
                             "nothing answers there")
    parser.add_argument("--address", type=int, default=1,
                        help="RS-485 address of the TC 600")
    parser.add_argument("--gauge", default="auto",
                        help="serial port of the TPG gauge controller, or "
                             "'auto' to find it by asking each port")
    parser.add_argument("--gauge-baud", type=int, default=None,
                        help="gauge baud rate; omit to detect it")
    parser.add_argument("--gauge-channels", type=int, default=2,
                        help="measurement channels on the gauge controller")
    parser.add_argument("--simulate", action="store_true",
                        help="run the whole rig simulated, including the "
                             "argon, gate valve, magnetron and shutter, so a "
                             "deposition recipe runs end to end with no "
                             "hardware at all")
    parser.add_argument("--simulate-pressure", type=float, default=1013.0,
                        help="starting chamber pressure for --simulate, mbar")
    parser.add_argument("--no-pump", action="store_true",
                        help="run without the turbopump")
    parser.add_argument("--no-gauge", action="store_true",
                        help="run without the gauge controller")
    parser.add_argument("--db", default=None,
                        help=f"run database (default {dbmod.default_path()})")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--token", default=None,
                        help="require this shared token on API calls")
    parser.add_argument("--no-port-search", action="store_true",
                        help="use exactly the ports given; never probe other "
                             "ports for the instruments")
    parser.add_argument("--no-auto-recovery", action="store_true",
                        help="raise alarms on a drive fault but do not make "
                             "the pump safe automatically (motor off, fault "
                             "acknowledged, forevacuum pump kept on)")
    args = parser.parse_args()

    application = build(args)
    api.Handler.app = application

    httpd = ThreadingHTTPServer((args.bind, args.port), api.Handler)
    print(f"Vacuum lab panel on http://{args.bind}:{args.port}/")
    for device in application.registry:
        print(f"  {device.label}: {device.port}")
    print(f"  database: {application.db.path}")
    print(f"  interlocks: the turbopump may not start above "
          f"{pump.TURBO_START_MAX_MBAR:.0E} mbar; the station may not stop "
          f"until {pump.STOP_DWELL_SECONDS:.0f}s after the rotor reads 0 Hz")
    absent = application.engine.unavailable()
    if absent:
        names = [runs.CATALOGUE[name].label for name in absent]
        print(f"  switched off, equipment not installed: {', '.join(names)}")
        print("  (mark it installed in Settings -> Rig equipment once it is)")
    if args.no_gauge and not args.no_pump:
        print("  NO GAUGE: the chamber pressure is unknown, so starting the")
        print("  turbopump will be refused until you override it deliberately.")
    if not args.token:
        print("  NO TOKEN SET: anyone who can reach this port can start and")
        print("  stop the pump. Pass --token to require a shared secret.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        if application.supervisor is not None:
            application.supervisor.close()
        if application.history is not None:
            application.history.close()
        application.registry.close()
        application.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
