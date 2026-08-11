#!/usr/bin/env python3
"""Both simulators at once, wired together as one rig.

    ./tools/fake_rig.py
    # -> pump  on /dev/pts/7
    # -> gauge on /dev/pts/8
    ./webserver.py --serial /dev/pts/7 --gauge /dev/pts/8 --db /tmp/lab.db

The point of running them in one process is the link between them: the fake
TC 600's rotor speed is fed to the fake TPG's chamber model every cycle, so
starting the pumping station really does make the pressure fall, and stopping
it really does make the pressure rise again. That makes a pump-down or a
rate-of-rise run testable end to end with no hardware.

Without the link each simulator is honest on its own and the pair is
nonsense: a chamber that pumps down while the pump is off proves nothing
about the run engine.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools import fake_tc600, fake_tpg  # noqa: E402


def link(stop: threading.Event, interval: float = 0.25) -> None:
    """Feed the simulated rotor speed into the simulated chamber."""
    while not stop.is_set():
        nominal = max(1, fake_tc600.STATE.get(315) or 1)
        speed = fake_tc600.STATE.get(309) or 0
        fake_tpg.set_pumping(speed / nominal, bool(fake_tc600.STATE.get(10)))
        stop.wait(interval)


def start_in_process(pressure: float = 1013.0, isolated: bool = False) -> dict:
    """Run both simulators on ptys inside the calling process.

    `webserver.py --simulate` uses this rather than spawning fake_rig.py as a
    separate process, because the simulated actuators have to manipulate the
    *same* chamber object the gauge simulator is reading. Across a process
    boundary they would be two unrelated chambers and the argon would never
    show up in the pressure.

    Returns the two pty paths, the chamber, and the stop event. The slave file
    descriptors are kept in the result: closing them would tear the ptys down.
    """
    fake_tpg.CHAMBER.pressure = pressure
    fake_tpg.CHAMBER.isolated = isolated

    pump_master, pump_slave = os.openpty()
    gauge_master, gauge_slave = os.openpty()
    stop = threading.Event()

    for target, master, name in (
        (fake_tc600.serve, pump_master, "sim-pump"),
        (fake_tpg.serve, gauge_master, "sim-gauge"),
    ):
        threading.Thread(target=target, args=(master, stop.is_set),
                         daemon=True, name=name).start()
    threading.Thread(target=link, args=(stop,), daemon=True,
                     name="sim-link").start()

    return {
        "pump": os.ttyname(pump_slave),
        "gauge": os.ttyname(gauge_slave),
        "chamber": fake_tpg.CHAMBER,
        "stop": stop,
        "_fds": (pump_slave, gauge_slave),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pressure", type=float, default=1013.0,
                        help="starting chamber pressure in mbar")
    parser.add_argument("--isolated", action="store_true",
                        help="start with the chamber valved off, for a "
                             "rate-of-rise test")
    args = parser.parse_args()

    fake_tpg.CHAMBER.pressure = args.pressure
    fake_tpg.CHAMBER.isolated = args.isolated

    pump_master, pump_slave = os.openpty()
    gauge_master, gauge_slave = os.openpty()
    stop = threading.Event()

    threads = [
        threading.Thread(target=fake_tc600.serve, args=(pump_master, stop.is_set),
                         daemon=True),
        threading.Thread(target=fake_tpg.serve, args=(gauge_master, stop.is_set),
                         daemon=True),
        threading.Thread(target=link, args=(stop,), daemon=True),
    ]
    for thread in threads:
        thread.start()

    print(f"pump  on {os.ttyname(pump_slave)}", flush=True)
    print(f"gauge on {os.ttyname(gauge_slave)}", flush=True)
    print(f"chamber starts at {args.pressure:g} mbar"
          f"{' (isolated)' if args.isolated else ''}", flush=True)
    print("\n  ./webserver.py --serial "
          f"{os.ttyname(pump_slave)} --gauge {os.ttyname(gauge_slave)}\n",
          flush=True)

    try:
        while True:
            time.sleep(1.0)
            if os.environ.get("FAKE_RIG_VERBOSE"):
                print(f"\r  {fake_tc600.STATE[309]:>5} Hz   "
                      f"{fake_tpg.CHAMBER.pressure:.3E} mbar   ",
                      end="", flush=True)
    except KeyboardInterrupt:
        stop.set()
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
