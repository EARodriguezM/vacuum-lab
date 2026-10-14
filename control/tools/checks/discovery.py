"""Finding each instrument's port by asking every port."""

from __future__ import annotations

import os
import tempfile

from lab import pump as pumpmod
from lab import registry
from tools.checks.common import (
    check, wait_until)


def test_discovery(pump_port: str, gauge_port: str) -> None:
    """Finding the instruments by asking: no port is assumed.

    Runs after every service has let go of the simulators' ports -- a probe
    is a second client on a port, and one owner per port is the rule.
    """
    print("\nFinding the instruments")
    from lab import discovery

    found = discovery.probe_tc600(pump_port)
    check("the TC 600 probe recognises the pump",
          found is not None and found["identity"].get("ElecName") == "TC600",
          found["says"] if found else "nothing answered")
    check("the TC 600 probe does not mistake the gauge for a pump",
          discovery.probe_tc600(gauge_port) is None)
    found = discovery.probe_tpg(gauge_port)
    check("the TPG probe recognises the gauge and its baud rate",
          found is not None and found["baudrate"] == 9600,
          found["says"] if found else "nothing answered")
    check("the TPG probe does not mistake the pump for a gauge",
          discovery.probe_tpg(pump_port) is None)

    tried: list = []
    found = discovery.find("tc600", ["/dev/does-not-exist", gauge_port, pump_port],
                           tried=tried)
    check("a search finds the pump on whichever port it is",
          found is not None and found["device"] == os.path.realpath(pump_port))
    check("the search reports what each port said",
          [t["answered"] for t in tried] == [False, True], str(tried))

    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "ports.json")
        discovery.PortMemory(path).remember("tc600", pump_port, "BG03FP5F")
        memory = discovery.PortMemory(path)
        check("the adapter that carried an instrument is remembered",
              memory.preferred("tc600", [{"device": "/dev/ttyUSB7",
                                          "serial_number": "BG03FP5F"}])[0]
              == "/dev/ttyUSB7")

        # A service started on a dead port finds its instrument by itself.
        devices = registry.Registry()
        service = devices.add(pumpmod.PumpService("pump", "/dev/no-such-port"))
        service.SEARCH_AFTER_S = 0.5
        service.SEARCH_EVERY_S = 1.0
        # Only the simulators' ports: on the lab PC the default search
        # reaches /dev/tc600, and the real pump answers there.
        simulated = [{"device": gauge_port, "serial_number": ""},
                     {"device": pump_port, "serial_number": ""}]
        service.finder = discovery.Finder(
            devices, memory, enabled=True, udev_names={},
            ports=lambda exclude=(): [p for p in simulated
                                      if discovery.real(p["device"])
                                      not in {discovery.real(x) for x in exclude}])
        try:
            moved = wait_until(lambda: service.snapshot().get("connected"), 30)
            check("a silent instrument is found on another port and reconnected",
                  moved and os.path.realpath(service.port)
                  == os.path.realpath(pump_port),
                  f"now on {service.port}")
            check("the move is recorded for the alarm",
                  (service.moved or {}).get("from") == "/dev/no-such-port")
        finally:
            devices.close()
