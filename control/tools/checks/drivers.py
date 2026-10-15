"""The two instrument drivers against their simulators, and the gauge's
cold-cathode interlock."""

from __future__ import annotations

import time

from drivers import tpg
from drivers.tc600 import TC600, protocol, scan
from lab import gauge as gaugemod
from tools.checks.common import (
    check)


def test_pump(port: str) -> None:
    print("TC 600 driver")
    with TC600(port, address=1, timeout=1.0) as pump:
        identity = pump.identify()
        check("identify() returns fields", bool(identity), str(identity))
        check("ElecName is TC600", identity.get("ElecName") == "TC600")
        check("NominalSpd is 1500 Hz", identity.get("NominalSpd") == 1500)
        check("DrvVoltage decodes as u_real",
              pump.read("DrvVoltage") == 23.25, f"{pump.read('DrvVoltage')} V")
        check("lookup by number works", pump.read(311) == pump.read("OpHrsPump"))

        status = pump.status()
        check("status() populated", len(status) >= 10, f"{len(status)} fields")

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

        check("PumpgStatn write echoes True", pump.pumping_station(True) is True)
        check("MotorPump write echoes True", pump.motor(True) is True)
        time.sleep(1.5)
        speed = pump.read("ActualSpd")
        check("rotor spins up", speed > 0, f"{speed} Hz")

        pump.set_speed_percent(50.0)
        check("OpModeTMP set to speed-setting", pump.read("OpModeTMP") == 1)
        check("SpdSVal is 50 %", pump.read("SpdSVal") == 50.0)

        before = pump.read("ActualSpd")
        pump.pumping_station(False)
        pump.motor(False)
        time.sleep(0.8)
        check("rotor slows down", pump.read("ActualSpd") < before)

    found = scan(port, range(1, 4), timeout=0.4)
    check("scan finds address 1", found == [1], str(found))


def test_gauge(port: str) -> None:
    print("\nTPG driver")
    with tpg.TPG(port, baudrate=9600, timeout=1.0) as unit:
        check("firmware reads back", unit.firmware() == "BG509727-C",
              unit.firmware())

        gauges = unit.gauge_types()
        check("two gauges identified", len(gauges) == 2,
              ", ".join(g["identifier"] for g in gauges))
        check("gauge 1 recognised as Pirani", "Pirani" in gauges[0]["description"])

        code, name = unit.unit()
        check("unit reads as mbar", code == 0 and name == "mbar")

        readings = unit.pressures()
        check("PRX returns both channels", len(readings) == 2)
        check("status decodes to a name",
              all(r["status_name"] for r in readings),
              ", ".join(r["status_name"] for r in readings))
        # The chamber starts at atmosphere, so the cold cathode is off and the
        # Pirani is over its range: both must report invalid, not a number.
        check("invalid readings carry no mbar value",
              all(r["mbar"] is None for r in readings if not r["valid"]))

        errors = unit.error_status()
        check("error register is clear", errors["ok"], errors["word"])

        # The real TPG 252 A answers "lower,upper" with no channel field, and
        # assuming otherwise made int("1.00E-11") raise and the whole
        # switching-function panel vanish. Both shapes must parse.
        first = unit.setpoint(1)
        check("setpoint reads back", first["setpoint"] == 1)
        check("channel-less setpoint parses", first["channel"] is None,
              f"lower {first['lower']:.2E} upper {first['upper']:.2E}")
        check("setpoint count matches the model", unit.setpoint_count() == 2,
              f"{unit.setpoint_count()} switching functions")

        updated = unit.set_setpoint(2, 1.0e-4, 5.0e-4)
        check("setpoint writes back", abs(updated["lower"] - 1.0e-4) < 1e-9,
              f"{updated['lower']:.2E}")

        try:
            unit.set_setpoint(1, 5.0e-4, 1.0e-4)   # lower above upper
            check("setpoint rejects inverted thresholds", False)
        except ValueError:
            check("setpoint rejects inverted thresholds", True)

        # SEN reports 3, not 2, for a live gauge on the real instrument.
        sensors = unit.sensor_state()
        check("live gauge reads as on", sensors[0]["on"] is True,
              f"code {sensors[0]['code']} -> {sensors[0]['state']}")
        check("empty channel identifier recognised",
              not tpg.commands.gauge_present("noSe")
              and tpg.commands.gauge_present("CO9"))

        try:
            unit.exchange("SCX")     # in UNIMPLEMENTED on the fake firmware
            check("unimplemented mnemonic raises NakError", False)
        except tpg.NakError:
            check("unimplemented mnemonic raises NakError", True)

        # An exchange abandoned after its ACK must not desynchronise the next
        # one -- that is what the ETX at the start of every exchange is for.
        unit.serial.write(b"PNR\r\n")
        unit.serial.flush()
        time.sleep(0.1)
        check("resynchronises after an abandoned exchange",
              unit.firmware() == "BG509727-C")

        result = unit.probe()
        check("probe finds the implemented set", len(result["supported"]) > 15,
              f"{len(result['supported'])} implemented, "
              f"{len(result['refused'])} refused")
        check("probe reports refusals", len(result["refused"]) > 0)

    detected = tpg.autodetect(port, timeout=0.8)
    check("autodetect finds the baud rate", detected is not None
          and detected.baudrate == 9600)
    if detected:
        detected.close()


def test_cold_cathode_interlock() -> None:
    """The gauge on the rig answers "CO9", not "PKR": the rule must still bite."""
    print("\nCold-cathode interlock")
    service = gaugemod.GaugeService("gauge", "/dev/null-gauge")

    def snap(mbar):
        return {"readings": [{"channel": 1, "valid": True, "mbar": mbar}]}

    def refused(identifier, mbar):
        service._identity = {"gauges": [{"channel": 1, "identifier": identifier}]}
        try:
            service._check_sensor_on(1, snap(mbar))
        except gaugemod.GaugeInterlock:
            return True
        return False

    try:
        check("the rig's CO9 (a PKR 251) is not struck at 4e2 mbar",
              refused("CO9", 412.0))
        check("nor just above the 1e-2 mbar limit", refused("CO9", 2.0e-2))
        check("it may be switched on below the limit",
              not refused("CO9", 1.0e-3))
        check("the published names still count", refused("PKR", 412.0)
              and refused("IKR9", 1.0))
        check("a Pirani is never held back", not refused("TPR", 412.0))
        check("CO9 is described as what it is",
              "PKR 251" in tpg.commands.describe_gauge("CO9"))
    finally:
        service.close()
