#!/usr/bin/env python3
"""Command line control for a Pfeiffer Vacuum TC 600 over RS-485.

Run `./tc600ctl.py --help` for the command list, or start with:

    ./tc600ctl.py ports          # which serial adapters does Linux see?
    ./tc600ctl.py scan           # which RS-485 addresses answer?
    ./tc600ctl.py info           # identity + full status
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time

from drivers.tc600 import device, params, protocol

DEFAULT_PORT = "/dev/ttyUSB0"

#: Commands that physically move the pump; these require --yes.
DANGEROUS = {"on", "off", "motor", "standby", "speed"}


# --- helpers --------------------------------------------------------------


def fail(message: str, code: int = 1):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def format_value(parameter: params.Parameter, value) -> str:
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return f"{text} {parameter.unit}".strip()


def open_pump(args) -> device.TC600:
    try:
        return device.TC600(
            args.port,
            address=args.address,
            timeout=args.timeout,
            handshake=not args.no_handshake,
            rts_direction=args.rts_direction,
            rts_invert=args.rts_invert,
        )
    except Exception as exc:
        fail(f"cannot open {args.port}: {exc}")


# --- commands -------------------------------------------------------------


def uart_type(device_path: str) -> int | None:
    """Return the sysfs UART type for an 8250-style port, or None if it has none.

    The kernel creates /dev/ttyS* nodes speculatively, so most of them have no
    hardware behind them and report type 0 (PORT_UNKNOWN). A real motherboard
    serial port -- the kind on an office desktop's back panel -- reports a
    nonzero type such as 3 for a 16550A. USB adapters have no such file.
    """
    name = os.path.basename(device_path)
    try:
        with open(f"/sys/class/tty/{name}/type") as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


def cmd_ports(args):
    """List serial ports Linux currently exposes."""
    try:
        from serial.tools import list_ports
    except ImportError:
        fail("pyserial's list_ports is unavailable")

    found = list(list_ports.comports())
    if not found:
        print("No serial ports found.")
        print("Plug in the USB-to-RS-232 adapter, then re-run.")
        return 1

    real, placeholders = [], []
    for port in found:
        kind = uart_type(port.device)
        if kind is None:
            # No sysfs UART type: judge a USB adapter by its USB ID instead.
            usable = bool(port.hwid and port.hwid != "n/a")
            label = port.hwid
        else:
            usable = kind != 0
            label = f"onboard UART (sysfs type {kind})"
        (real if usable else placeholders).append((port, label))

    if real:
        print("Usable ports:")
        for port, label in sorted(real, key=lambda item: item[0].device):
            print(f"  {port.device}\t{port.description}\t{label}")
    else:
        print("No usable serial port detected.")

    if placeholders:
        names = ", ".join(sorted(p.device for p, _ in placeholders))
        print(f"\n{len(placeholders)} port node(s) with no hardware: {names}")
    return 0


def cmd_scan(args):
    """Probe RS-485 addresses and report which ones answer."""
    addresses = range(args.first, args.last + 1)
    print(f"Scanning {args.port} addresses {args.first}-{args.last} ...")
    found = device.scan(args.port, addresses, timeout=args.timeout,
                        handshake=not args.no_handshake,
                        rts_direction=args.rts_direction,
                        rts_invert=args.rts_invert)
    if not found:
        print("No devices answered. See the troubleshooting section in README.md.")
        return 1
    for address in found:
        print(f"  address {address}: responding")
    return 0


def cmd_info(args):
    """Print identity fields followed by the full status view."""
    with open_pump(args) as pump:
        identity = pump.identify()
        if not identity:
            fail("pump answered but reported no identity fields")
        print("Device")
        for key, value in identity.items():
            print(f"  {key:<14} {value}")
        print("\nStatus")
        for name, value in pump.status().items():
            parameter = params.BY_NAME[name.lower()]
            print(f"  {name:<14} {format_value(parameter, value)}")
    return 0


def cmd_status(args):
    """Print the status view once."""
    with open_pump(args) as pump:
        for name, value in pump.status().items():
            parameter = params.BY_NAME[name.lower()]
            print(f"{name:<14} {format_value(parameter, value)}")
    return 0


def cmd_read(args):
    """Read one parameter by name or number."""
    try:
        parameter = params.lookup(args.parameter)
    except KeyError as exc:
        fail(str(exc))

    with open_pump(args) as pump:
        if args.raw:
            print(pump.read_raw(parameter.number))
        else:
            try:
                print(format_value(parameter, pump.read(parameter.number)))
            except protocol.ProtocolError as exc:
                fail(str(exc))
    return 0


def cmd_write(args):
    """Write one parameter by name or number."""
    try:
        parameter = params.lookup(args.parameter)
    except KeyError as exc:
        fail(str(exc))

    value: object = args.value
    if parameter.datatype in (protocol.BooleanOld, protocol.BooleanNew):
        lowered = str(args.value).strip().lower()
        if lowered in ("1", "on", "true", "yes"):
            value = True
        elif lowered in ("0", "off", "false", "no"):
            value = False
        else:
            fail(f"expected a boolean for {parameter.name}, got {args.value!r}")
    elif parameter.datatype in (protocol.UInteger, protocol.UShortInt):
        value = int(args.value)
    elif parameter.datatype in (protocol.UReal, protocol.UExpoNew):
        value = float(args.value)

    with open_pump(args) as pump:
        try:
            result = pump.write(parameter.number, value)
        except protocol.ProtocolError as exc:
            fail(str(exc))
        print(f"{parameter.name} = {format_value(parameter, result)}")
    return 0


def cmd_verify(args):
    """Read every parameter in the table and report which the firmware knows.

    This is how you confirm the shipped parameter table against your actual
    unit without needing PM 0488 BN.
    """
    supported, missing, failed = [], [], []
    with open_pump(args) as pump:
        for parameter in params.PARAMETERS:
            if not parameter.readable:
                continue
            try:
                value = parameter.decode(pump.read_raw(parameter.number))
                supported.append((parameter, value))
                print(f"  P{parameter.number:03d} {parameter.name:<14} "
                      f"{format_value(parameter, value)}")
            except protocol.DeviceError:
                missing.append(parameter)
            except protocol.ProtocolError as exc:
                failed.append((parameter, exc))

    print(f"\n{len(supported)} readable, {len(missing)} not implemented, "
          f"{len(failed)} errored")
    if missing:
        print("Not implemented: " + ", ".join(f"P{p.number}" for p in missing))
    for parameter, exc in failed:
        print(f"P{parameter.number} {parameter.name}: {exc}")
    return 0


def cmd_monitor(args):
    """Refresh the status view in place until interrupted."""
    with open_pump(args) as pump:
        try:
            while True:
                readings = pump.status()
                stamp = dt.datetime.now().strftime("%H:%M:%S")
                lines = [f"TC 600 @ {args.port} address {args.address}   {stamp}", ""]
                for name, value in readings.items():
                    parameter = params.BY_NAME[name.lower()]
                    lines.append(f"  {name:<14} {format_value(parameter, value)}")
                lines.append("")
                lines.append("  Ctrl-C to stop")
                print("\033[2J\033[H" + "\n".join(lines), end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
    return 0


def cmd_log(args):
    """Append status readings to a CSV file until interrupted."""
    fieldnames = ["timestamp"] + [params.BY_NUMBER[n].name for n in params.STATUS_VIEW]
    with open(args.output, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if handle.tell() == 0:
            writer.writeheader()

        with open_pump(args) as pump:
            print(f"Logging to {args.output} every {args.interval}s. Ctrl-C to stop.")
            try:
                while True:
                    row = pump.status()
                    row["timestamp"] = dt.datetime.now().isoformat(timespec="seconds")
                    writer.writerow(row)
                    handle.flush()
                    print(f"  {row['timestamp']}  "
                          f"{row.get('ActualSpd', '?')} Hz", flush=True)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nStopped.")
    return 0


def _confirm(args, action: str) -> bool:
    if args.yes:
        return True
    print(f"About to {action} on {args.port} address {args.address}.")
    print("Re-run with --yes to confirm.")
    return False


def cmd_on(args):
    """Switch the pumping station on."""
    if not _confirm(args, "START the pumping station"):
        return 2
    with open_pump(args) as pump:
        print(f"PumpgStatn = {pump.pumping_station(True)}")
    return 0


def cmd_off(args):
    """Switch the pumping station off."""
    if not _confirm(args, "STOP the pumping station"):
        return 2
    with open_pump(args) as pump:
        print(f"PumpgStatn = {pump.pumping_station(False)}")
    return 0


def cmd_motor(args):
    """Switch the turbopump motor on or off."""
    on = args.state == "on"
    if not _confirm(args, f"turn the turbopump motor {args.state.upper()}"):
        return 2
    with open_pump(args) as pump:
        print(f"MotorPump = {pump.motor(on)}")
    return 0


def cmd_standby(args):
    """Enter or leave standby speed."""
    on = args.state == "on"
    if not _confirm(args, f"turn standby {args.state.upper()}"):
        return 2
    with open_pump(args) as pump:
        print(f"Standby = {pump.standby(on)}")
    return 0


def cmd_speed(args):
    """Set the rotation speed setpoint in percent (enables setting mode)."""
    if not _confirm(args, f"set the speed setpoint to {args.percent} %"):
        return 2
    with open_pump(args) as pump:
        try:
            print(f"SpdSVal = {pump.set_speed_percent(args.percent)} %")
        except (ValueError, protocol.ProtocolError) as exc:
            fail(str(exc))
    return 0


def cmd_ack(args):
    """Acknowledge a latched fault."""
    with open_pump(args) as pump:
        pump.acknowledge_error()
        try:
            print(f"ErrorCode = {pump.read('ErrorCode')}")
        except protocol.ProtocolError:
            print("Acknowledged.")
    return 0


def cmd_raw(args):
    """Send an arbitrary parameter number and payload; print the raw reply."""
    with open_pump(args) as pump:
        try:
            if args.payload is None:
                print(pump.read_raw(args.number))
            else:
                print(pump.write_raw(args.number, args.payload))
        except protocol.ProtocolError as exc:
            fail(str(exc))
    return 0


def cmd_params(args):
    """List the parameter table shipped with this tool."""
    for parameter in params.PARAMETERS:
        unit = f" [{parameter.unit}]" if parameter.unit else ""
        print(f"P{parameter.number:03d}  {parameter.name:<14} {parameter.access:<2}  "
              f"{parameter.description}{unit}")
    return 0


# --- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tc600ctl",
        description="Control a Pfeiffer Vacuum TC 600 turbopump drive unit.",
    )
    parser.add_argument("-p", "--port", default=DEFAULT_PORT,
                        help=f"serial port (default {DEFAULT_PORT})")
    parser.add_argument("-a", "--address", type=int, default=1,
                        help="RS-485 address, parameter 797 (default 1)")
    parser.add_argument("-t", "--timeout", type=float, default=1.0,
                        help="read timeout in seconds (default 1.0)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="confirm commands that physically move the pump")
    parser.add_argument("--no-handshake", action="store_true",
                        help="do not assert DTR/RTS; use for a direct "
                             "USB-to-RS-485 dongle that drives its "
                             "transmitter from RTS")
    parser.add_argument("--rts-direction", action="store_true",
                        help="toggle RTS around each telegram; use for a bare "
                             "MAX485 with DE+RE wired to RTS")
    parser.add_argument("--rts-invert", action="store_true",
                        help="invert the --rts-direction polarity; try this "
                             "if the pump stays silent")

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        p = sub.add_parser(name, help=help_text, description=handler.__doc__)
        p.set_defaults(handler=handler)
        return p

    add("ports", cmd_ports, "list serial ports Linux can see")
    add("params", cmd_params, "list the known parameter table")

    p = add("scan", cmd_scan, "probe RS-485 addresses")
    p.add_argument("--first", type=int, default=1)
    p.add_argument("--last", type=int, default=16)

    add("info", cmd_info, "print identity and status")
    add("status", cmd_status, "print the status view once")
    add("verify", cmd_verify, "check the parameter table against the firmware")
    add("ack", cmd_ack, "acknowledge a latched fault")

    p = add("read", cmd_read, "read one parameter")
    p.add_argument("parameter", help="parameter name or number")
    p.add_argument("--raw", action="store_true", help="print the undecoded payload")

    p = add("write", cmd_write, "write one parameter")
    p.add_argument("parameter", help="parameter name or number")
    p.add_argument("value")

    p = add("monitor", cmd_monitor, "live status display")
    p.add_argument("-i", "--interval", type=float, default=1.0)

    p = add("log", cmd_log, "append status readings to CSV")
    p.add_argument("output")
    p.add_argument("-i", "--interval", type=float, default=5.0)

    add("on", cmd_on, "switch the pumping station on")
    add("off", cmd_off, "switch the pumping station off")

    p = add("motor", cmd_motor, "switch the turbopump motor")
    p.add_argument("state", choices=["on", "off"])

    p = add("standby", cmd_standby, "switch standby mode")
    p.add_argument("state", choices=["on", "off"])

    p = add("speed", cmd_speed, "set the speed setpoint in percent")
    p.add_argument("percent", type=float)

    p = add("raw", cmd_raw, "send a raw parameter request")
    p.add_argument("number", type=int)
    p.add_argument("payload", nargs="?", default=None,
                   help="omit to read; supply an encoded payload to write")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
