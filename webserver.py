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
"""

from __future__ import annotations

import argparse
import os
import sys
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lab import api, db as dbmod, gauge, pump, registry, runs  # noqa: E402


def build(args) -> api.Application:
    """Assemble the registry, store and engine from the command line."""
    devices = registry.Registry()

    if not args.no_pump:
        devices.add(pump.PumpService("pump", args.serial, address=args.address))
    if not args.no_gauge:
        devices.add(gauge.GaugeService(
            "gauge", args.gauge, baudrate=args.gauge_baud,
            channels=args.gauge_channels))

    database = dbmod.Database(args.db)
    engine = runs.RunEngine(database, devices)
    return api.Application(devices, database, engine, token=args.token)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", default="/dev/ttyS1",
                        help="serial port of the TC 600 (via RS-485)")
    parser.add_argument("--address", type=int, default=1,
                        help="RS-485 address of the TC 600")
    parser.add_argument("--gauge", default="/dev/ttyS0",
                        help="serial port of the TPG gauge controller "
                             "(onboard COM1 on the lab PC; USB adapters were "
                             "tried and none worked)")
    parser.add_argument("--gauge-baud", type=int, default=None,
                        help="gauge baud rate; omit to detect it")
    parser.add_argument("--gauge-channels", type=int, default=2,
                        help="measurement channels on the gauge controller")
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
    args = parser.parse_args()

    application = build(args)
    api.Handler.app = application

    httpd = ThreadingHTTPServer((args.bind, args.port), api.Handler)
    print(f"Vacuum lab panel on http://{args.bind}:{args.port}/")
    for device in application.registry:
        print(f"  {device.label}: {device.port}")
    print(f"  database: {application.db.path}")
    print(f"  interlocks: backing pump must lead the turbopump by "
          f"{pump.BACKING_LEAD_SECONDS:.0f}s; station may not stop until "
          f"{pump.STOP_DWELL_SECONDS:.0f}s after the rotor reaches 0 Hz")
    if not args.token:
        print("  NO TOKEN SET: anyone who can reach this port can start and")
        print("  stop the pump. Pass --token to require a shared secret.")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        application.registry.close()
        application.db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
