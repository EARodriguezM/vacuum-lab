#!/usr/bin/env python3
"""Command line control for a Pfeiffer TPG gauge controller over RS-232.

Run `./tpgctl.py --help` for the command list, or start with:

    ./tpgctl.py ports            # which serial adapters does Linux see?
    ./tpgctl.py detect           # which baud rate does the unit answer on?
    ./tpgctl.py info             # identity, gauges, pressures
    ./tpgctl.py probe            # which mnemonics this firmware implements
    ./tpgctl.py monitor          # live pressure display

`probe` is the one to trust: the command table in `tpg/commands.py` is the
published family set, and every model implements a subset of it.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import tpg  # noqa: E402
from tpg import commands, protocol  # noqa: E402

#: Onboard COM1 on the lab PC. Two different USB-serial adapters were tried
#: on this instrument and neither worked; see the README.
DEFAULT_PORT = "/dev/ttyS0"

#: Commands that change what the instrument does.
DANGEROUS = {"sensor", "degas", "unit", "setpoint", "filter", "reset", "lock"}


# --- helpers --------------------------------------------------------------


def fail(message: str, code: int = 1):
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def open_gauge(args) -> tpg.TPG:
    """Open the port, hunting for the baud rate unless one was given."""
    try:
        if args.baud:
            return tpg.TPG(args.port, baudrate=args.baud, timeout=args.timeout,
                           channels=args.channels)
        unit = tpg.autodetect(args.port, timeout=args.timeout,
                              channels=args.channels)
    except Exception as exc:
        fail(f"cannot open {args.port}: {exc}")
    if unit is None:
        fail(f"nothing answered on {args.port} at 9600, 19200 or 38400 baud.\n"
             "  Check that the unit is powered, that the cable reaches its\n"
             "  RS-232 socket, and that the adapter is a true RS-232 one --\n"
             "  a TTL-level dongle cannot talk to it. `./tpgctl.py diagnose`\n"
             "  narrows it down.")
    return unit  # type: ignore[return-value]


def format_pressure(entry: dict) -> str:
    if not entry.get("valid"):
        return f"{entry['status_name']:<12} ({entry['status_text']})"
    return f"{entry['value']:.3E} {entry['unit']:<5} = {entry['mbar']:.3E} mbar"


def confirm(args, what: str) -> None:
    if not args.yes:
        fail(f"{what} changes the instrument. Re-run with --yes to confirm.")


# --- commands -------------------------------------------------------------


def cmd_ports(args):
    """List serial ports Linux can see."""
    try:
        from serial.tools import list_ports
    except ImportError:
        fail("pyserial's list_ports is unavailable")
    found = list(list_ports.comports())
    if not found:
        print("no serial ports found")
        return 1
    for port in found:
        print(f"{port.device:<20} {port.description}")
        if port.hwid and port.hwid != "n/a":
            print(f"{'':<20} {port.hwid}")
    return 0


def cmd_detect(args):
    """Find which baud rate the unit answers on."""
    print(f"probing {args.port} ...")
    for rate in tpg.device.BAUD_RATES:
        unit = None
        try:
            unit = tpg.TPG(args.port, baudrate=rate, timeout=args.timeout,
                           retries=0, channels=args.channels)
            firmware = unit.firmware()
            print(f"  {rate:>6} baud: ANSWERED, firmware {firmware}")
            unit.close()
            return 0
        except Exception as exc:
            print(f"  {rate:>6} baud: {type(exc).__name__}: {exc}")
        finally:
            if unit is not None:
                unit.close()
    print("\nNo answer at any rate. Run `./tpgctl.py diagnose` next.")
    return 1


def cmd_diagnose(args):
    """Work out why a silent link is silent."""
    import serial

    print(f"=== {args.port} ===\n")
    print("[1] input lines (CTS/DSR/CD show whether anything is wired back)")
    try:
        with serial.Serial(args.port, 9600, timeout=0.2) as port:
            print(f"    cts={port.cts} dsr={port.dsr} cd={port.cd} ri={port.ri}")
    except Exception as exc:
        print(f"    cannot open: {exc}")
        return 1

    print("\n[2] passive listen -- does anything arrive unprompted?")
    for rate in tpg.device.BAUD_RATES:
        try:
            with serial.Serial(args.port, rate, timeout=0.3) as port:
                port.reset_input_buffer()
                blob = bytearray()
                end = time.monotonic() + 2.0
                while time.monotonic() < end:
                    blob.extend(port.read(256))
            print(f"    {rate:>6}: {len(blob)} bytes "
                  f"{bytes(blob[:32]).hex(' ') if blob else ''}")
        except Exception as exc:
            print(f"    {rate:>6}: {exc}")

    print("\n[3] PNR under varied terminator and handshake")
    for rate in (9600, 19200):
        for label, terminator, dtr, rts in (
            ("CRLF dtr=1 rts=1", b"\r\n", True, True),
            ("CRLF dtr=0 rts=0", b"\r\n", False, False),
            ("CR   dtr=1 rts=1", b"\r", True, True),
        ):
            try:
                with serial.Serial(args.port, rate, timeout=1.0) as port:
                    port.dtr, port.rts = dtr, rts
                    time.sleep(0.2)
                    port.reset_input_buffer()
                    port.write(b"PNR" + terminator)
                    port.flush()
                    time.sleep(0.4)
                    got = port.read(64)
                    port.write(protocol.ENQ)
                    time.sleep(0.4)
                    got += port.read(64)
                print(f"    {rate:>6} {label:<18} -> "
                      f"{got.hex(' ') if got else '(silence)'}")
            except Exception as exc:
                print(f"    {rate:>6} {label:<18} -> {exc}")

    print("\n[4] loopback -- short DB9 pins 2 and 3 on the adapter, then re-run")
    try:
        with serial.Serial(args.port, 9600, timeout=0.6) as port:
            port.reset_input_buffer()
            probe = b"LOOPBACK\r\n"
            port.write(probe)
            port.flush()
            time.sleep(0.3)
            echo = port.read(64)
        if echo.strip() == probe.strip():
            print("    adapter loops back: it transmits and receives fine, so")
            print("    the fault lies beyond it -- cable, crossover, or unit.")
        elif echo:
            print(f"    garbled echo: {echo.hex(' ')} (check the baud rate)")
        else:
            print("    no echo. Either pins 2-3 are not shorted, or the")
            print("    adapter cannot hear itself and is faulty or TTL-level.")
    except Exception as exc:
        print(f"    failed: {exc}")
    return 0


def cmd_info(args):
    """Print identity, fitted gauges and current pressures."""
    with open_gauge(args) as unit:
        identity = unit.identify()
        print(f"port          {args.port} @ {unit.baudrate} baud 8N1")
        print(f"firmware      {identity.get('firmware', '?')}")
        print(f"unit          {identity.get('unit', '?')}")
        print(f"channels      {unit.channels}")
        print()
        for gauge in identity.get("gauges", []):
            print(f"  channel {gauge['channel']}: {gauge['identifier']:<8} "
                  f"{gauge['description']}")
        print()
        for entry in unit.pressures():
            print(f"  p{entry['channel']} = {format_pressure(entry)}")
        print()
        try:
            errors = unit.error_status()
            print(f"errors        {errors['word']}"
                  f"{'' if errors['ok'] else ' -- ' + '; '.join(errors['faults'])}")
        except protocol.TpgError as exc:
            print(f"errors        unavailable ({exc})")
        for entry in unit.sensor_state():
            print(f"  gauge {entry['channel']} is {entry['state']}")
    return 0


def cmd_pressure(args):
    """Print the pressures once, one line per channel."""
    with open_gauge(args) as unit:
        for entry in unit.pressures():
            print(f"p{entry['channel']}\t{entry['status']}\t"
                  f"{entry['value']:.4E}\t{entry['unit']}\t"
                  f"{entry['mbar'] if entry['mbar'] is not None else ''}")
    return 0


def cmd_probe(args):
    """Ask the unit which mnemonics it implements. Changes nothing."""
    with open_gauge(args) as unit:
        print(f"probing {args.port} @ {unit.baudrate} baud "
              f"({len(commands.SAFE_TO_PROBE)} mnemonics, read-only)\n")
        result = unit.probe()

    print(f"{'mnemonic':<10} {'name':<20} data")
    print("-" * 66)
    for mnemonic, data in result["supported"].items():
        entry = commands.BY_MNEMONIC.get(mnemonic)
        print(f"{mnemonic:<10} {entry.name if entry else '':<20} {data}")

    print(f"\nimplemented ({len(result['supported'])}): "
          f"{' '.join(result['supported'])}")
    if result["refused"]:
        print(f"not implemented ({len(result['refused'])}): "
              f"{' '.join(result['refused'])}")
    if result["silent"]:
        print(f"no answer ({len(result['silent'])}): "
              f"{' '.join(result['silent'])}")
    return 0


def cmd_monitor(args):
    """Live pressure display; Ctrl-C to stop."""
    with open_gauge(args) as unit:
        print(f"{args.port} @ {unit.baudrate} baud -- Ctrl-C to stop\n")
        try:
            while True:
                stamp = dt.datetime.now().strftime("%H:%M:%S")
                try:
                    entries = unit.pressures()
                    parts = []
                    for entry in entries:
                        if entry["valid"]:
                            parts.append(f"p{entry['channel']}="
                                         f"{entry['mbar']:.3E} mbar")
                        else:
                            parts.append(f"p{entry['channel']}="
                                         f"{entry['status_name']}")
                    line = "   ".join(parts)
                except protocol.TpgError as exc:
                    line = f"read failed: {exc}"
                print(f"\r{stamp}  {line}   ", end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
    return 0


def cmd_log(args):
    """Append pressure readings to a CSV file."""
    new = not os.path.exists(args.output)
    with open_gauge(args) as unit, open(args.output, "a", newline="") as handle:
        writer = csv.writer(handle)
        if new:
            header = ["timestamp", "iso_time"]
            for channel in range(1, unit.channels + 1):
                header += [f"p{channel}_mbar", f"p{channel}_status"]
            writer.writerow(header)
        print(f"logging to {args.output} every {args.interval}s -- Ctrl-C to stop")
        try:
            while True:
                now = time.time()
                row = [f"{now:.3f}", dt.datetime.fromtimestamp(now).isoformat()]
                try:
                    for entry in unit.pressures():
                        row += [entry["mbar"] if entry["mbar"] is not None else "",
                                entry["status"]]
                except protocol.TpgError as exc:
                    print(f"\nread failed: {exc}", file=sys.stderr)
                    time.sleep(args.interval)
                    continue
                writer.writerow(row)
                handle.flush()
                print(f"\r{row[1]}  {row[2]}   ", end="", flush=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print()
    return 0


def cmd_sensor(args):
    """Switch a gauge on or off."""
    confirm(args, "switching a gauge")
    with open_gauge(args) as unit:
        try:
            for entry in unit.set_sensor(args.channel, args.state == "on"):
                print(f"gauge {entry['channel']} is {entry['state']}")
        except protocol.TpgError as exc:
            fail(str(exc))
    return 0


def cmd_degas(args):
    """Switch degassing on or off for one gauge."""
    confirm(args, "degassing")
    with open_gauge(args) as unit:
        try:
            print(",".join(unit.degas(args.channel, args.state == "on")))
        except protocol.TpgError as exc:
            fail(str(exc))
    return 0


def cmd_unit(args):
    """Read or set the pressure unit."""
    with open_gauge(args) as unit:
        if args.value is None:
            code, name = unit.unit()
            print(f"{code}  {name}")
            return 0
        confirm(args, "changing the pressure unit")
        try:
            code, name = unit.set_unit(args.value)
        except (protocol.TpgError, ValueError) as exc:
            fail(str(exc))
        print(f"{code}  {name}")
    return 0


def cmd_setpoint(args):
    """Read or set a switching function."""
    with open_gauge(args) as unit:
        try:
            if args.lower is None:
                entry = unit.setpoint(args.number)
            else:
                confirm(args, "changing a switching function")
                entry = unit.set_setpoint(args.number, args.lower, args.upper,
                                          channel=args.channel)
        except (protocol.TpgError, ValueError) as exc:
            fail(str(exc))
        # Models that do not associate a channel with a switching function
        # report None for it; saying "channel None" would be noise.
        where = f"channel {entry['channel']}  " if entry["channel"] else ""
        print(f"SP{entry['setpoint']}: {where}"
              f"lower {entry['lower']:.3E}  upper {entry['upper']:.3E}")
    return 0


def cmd_errors(args):
    """Print the error register."""
    with open_gauge(args) as unit:
        status = unit.error_status()
        print(f"{status['word']}  {'no faults' if status['ok'] else ''}")
        for fault in status["faults"]:
            print(f"  - {fault}")
    return 0


def cmd_raw(args):
    """Send an arbitrary mnemonic and print the reply."""
    with open_gauge(args) as unit:
        try:
            print(unit.exchange(args.mnemonic, *args.parameters))
        except protocol.TpgError as exc:
            fail(str(exc))
    return 0


def cmd_commands(args):
    """List the command table shipped with this tool."""
    for entry in commands.COMMANDS:
        params_text = f"({', '.join(entry.parameters)})" if entry.parameters else ""
        print(f"{entry.mnemonic:<6} {entry.access:<2} {entry.name:<20} "
              f"{entry.description} {params_text}")
    return 0


# --- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tpgctl",
        description="Control a Pfeiffer TPG gauge controller over RS-232.",
    )
    parser.add_argument("-p", "--port", default=DEFAULT_PORT,
                        help=f"serial port (default {DEFAULT_PORT})")
    parser.add_argument("-b", "--baud", type=int, default=None,
                        help="baud rate; omit to detect it automatically")
    parser.add_argument("-t", "--timeout", type=float, default=1.0,
                        help="read timeout in seconds (default 1.0)")
    parser.add_argument("-c", "--channels", type=int, default=2,
                        help="measurement channels on this model (default 2)")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="confirm commands that change the instrument")

    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, help_text):
        p = sub.add_parser(name, help=help_text, description=handler.__doc__)
        p.set_defaults(handler=handler)
        return p

    add("ports", cmd_ports, "list serial ports Linux can see")
    add("commands", cmd_commands, "list the known command table")
    add("detect", cmd_detect, "find the unit's baud rate")
    add("diagnose", cmd_diagnose, "work out why a silent link is silent")
    add("info", cmd_info, "identity, gauges and pressures")
    add("pressure", cmd_pressure, "print the pressures once")
    add("probe", cmd_probe, "check the command table against the firmware")
    add("errors", cmd_errors, "print the error register")

    p = add("monitor", cmd_monitor, "live pressure display")
    p.add_argument("-i", "--interval", type=float, default=1.0)

    p = add("log", cmd_log, "append pressure readings to CSV")
    p.add_argument("output")
    p.add_argument("-i", "--interval", type=float, default=5.0)

    p = add("sensor", cmd_sensor, "switch a gauge on or off")
    p.add_argument("channel", type=int)
    p.add_argument("state", choices=["on", "off"])

    p = add("degas", cmd_degas, "switch degassing for one gauge")
    p.add_argument("channel", type=int)
    p.add_argument("state", choices=["on", "off"])

    p = add("unit", cmd_unit, "read or set the pressure unit")
    p.add_argument("value", nargs="?", type=int, default=None,
                   help="0=mbar 1=Torr 2=Pa 3=micron 4=hPa 5=V")

    p = add("setpoint", cmd_setpoint, "read or set a switching function")
    p.add_argument("number", type=int, choices=[1, 2, 3, 4])
    p.add_argument("lower", nargs="?", type=float, default=None)
    p.add_argument("upper", nargs="?", type=float, default=None)
    p.add_argument("--channel", type=int, default=None,
                   help="only for models that tie a switching function to a "
                        "measurement channel; omitted, it is detected")

    p = add("raw", cmd_raw, "send an arbitrary mnemonic")
    p.add_argument("mnemonic")
    p.add_argument("parameters", nargs="*")

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
