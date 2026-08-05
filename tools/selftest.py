#!/usr/bin/env python3
"""End-to-end test of the TC 600 stack against the simulator, no hardware needed.

Runs the fake pump in a background thread on a pseudo-terminal and drives the
real client code against it: `./tools/selftest.py`
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tc600 import TC600, protocol, scan  # noqa: E402
from tools import fake_tc600  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    master, slave = os.openpty()
    port = os.ttyname(slave)
    stop = threading.Event()

    def serve_loudly():
        # A bare thread swallows its traceback, and a dead simulator looks
        # exactly like a timeout from the client side. Surface it instead.
        try:
            fake_tc600.serve(master, stop.is_set)
        except Exception:
            traceback.print_exc()
            print("!! simulator thread died")

    thread = threading.Thread(target=serve_loudly, daemon=True)
    thread.start()
    time.sleep(0.2)
    print(f"Simulated TC 600 on {port}\n")

    try:
        with TC600(port, address=1, timeout=1.0) as pump:
            print("Identity and readout")
            identity = pump.identify()
            check("identify() returns fields", bool(identity), str(identity))
            check("ElecName is TC600", identity.get("ElecName") == "TC600")
            check("NominalSpd is 1000 Hz", identity.get("NominalSpd") == 1000)

            check("DrvVoltage decodes as u_real",
                  pump.read("DrvVoltage") == 48.0, f"{pump.read('DrvVoltage')} V")
            check("lookup by number works", pump.read(311) == pump.read("OpHrsPump"))

            print("\nStatus view")
            status = pump.status()
            check("status() populated", len(status) >= 10, f"{len(status)} fields")
            check("pump is stopped", status["ActualSpd"] == 0)

            print("\nError paths")
            try:
                pump.read_raw(326)  # TempElec: NO_DEF on real firmware 017500
                check("NO_DEF raises DeviceError", False)
            except protocol.DeviceError as exc:
                check("NO_DEF raises DeviceError", True, str(exc)[:40])

            try:
                pump.write_raw(309, "000100")  # ActualSpd is read-only
                check("_LOGIC raises DeviceError", False)
            except protocol.DeviceError as exc:
                check("_LOGIC raises DeviceError", True, str(exc)[:40])

            try:
                pump.write("ActualSpd", 100)
                check("client blocks read-only writes", False)
            except ValueError:
                check("client blocks read-only writes", True)

            print("\nControl")
            check("PumpgStatn write echoes True", pump.pumping_station(True) is True)
            time.sleep(1.5)
            speed = pump.read("ActualSpd")
            check("rotor spins up", speed > 0, f"{speed} Hz")
            check("PumpAccel is set", pump.read("PumpAccel") is True)

            pump.set_speed_percent(50.0)
            check("OpModeTMP set to speed-setting", pump.read("OpModeTMP") == 1)
            check("SpdSVal is 50 %", pump.read("SpdSVal") == 50.0)

            before_off = pump.read("ActualSpd")
            check("PumpgStatn write echoes False",
                  pump.pumping_station(False) is False)
            time.sleep(0.8)
            after_off = pump.read("ActualSpd")
            check("rotor slows down", after_off < before_off,
                  f"{before_off} -> {after_off} Hz")

        print("\nBus scan")
        found = scan(port, range(1, 4), timeout=0.4)
        check("scan finds address 1", found == [1], str(found))

    finally:
        stop.set()
        thread.join(timeout=2)
        os.close(slave)

    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
