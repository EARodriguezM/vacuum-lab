"""Each chamber's instruments from its file: the files themselves, what a bad
one is refused for, the command line over them, and a chamber without a
turbopump -- which must start, and refuse every procedure that needs one."""

from __future__ import annotations

import contextlib
import io
import os
import re
import shlex
import tempfile
import time

from lab import chamber as chambermod
from lab import db as dbmod
from lab import discovery, registry, runs
from tools.checks.common import ROOT, attempt, check, wait_until


def _refused(data) -> str:
    """The message a chamber's contents are refused with, or ''."""
    try:
        chambermod.parse(data, source="test.toml")
    except chambermod.ChamberError as exc:
        return str(exc)
    return ""


def _parse_args(argv) -> tuple[object, str]:
    """webserver.parse(argv): (args, "") or (None, what it printed)."""
    import webserver
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            return webserver.parse(argv), ""
    except SystemExit:
        return None, err.getvalue()


def _write(folder: str, name: str, text: str) -> str:
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


GAUGE_ONLY = {"id": "chamber-2", "name": "Chamber 2",
              "instruments": {"gauge": {"kind": "tpg", "port": "auto"}}}
PUMP_ONLY = {"id": "chamber-3", "name": "Chamber 3",
             "instruments": {"pump": {"kind": "tc600", "port": "auto"}}}


def test_chambers(pump_port: str, gauge_port: str) -> None:
    print("\nChambers: each one's instruments from its file")
    with tempfile.TemporaryDirectory() as folder:
        _files()
        _refusals()
        _command_line(folder)
        _built(folder, pump_port, gauge_port)


def _files() -> None:
    first = chambermod.load(chambermod.DEFAULT)
    pump, gauge = first.instruments.get("pump"), first.instruments.get("gauge")
    check("chamber 1 is the TC 600 at RS-485 address 1 and the TPG with two "
          "channels, both found by asking",
          first.id == "chamber-1" and pump and gauge
          and (pump.kind, pump.port, pump.options["address"],
               pump.options["settle_ms"]) == ("tc600", "auto", 1, 30.0)
          and (gauge.kind, gauge.port, gauge.options["channels"],
               gauge.options["baudrate"]) == ("tpg", "auto", 2, None),
          str(first.describe()))
    problems = []
    for name in sorted(os.listdir(chambermod.FOLDER)):
        if name.endswith(".toml"):
            try:
                chambermod.load(os.path.join(chambermod.FOLDER, name))
            except chambermod.ChamberError as exc:
                problems.append(str(exc))
    check("every chamber's file in chambers/ can be used", not problems,
          "; ".join(problems)[:150])


def _refusals() -> None:
    pump = {"kind": "tc600", "port": "auto"}
    cases = {
        "an unknown kind": ({"id": "c", "instruments": {"pump": {"kind": "hipace"}}},
                            "unknown kind 'hipace'"),
        "a kind in the wrong role": ({"id": "c", "instruments": {"pump": {"kind": "tpg"}}},
                                     "a tpg is a gauge, not a pump"),
        "an unknown role": ({"id": "c", "instruments": {"valve": pump}},
                            "unknown role 'valve'"),
        "a misspelt option": ({"id": "c", "instruments": {"pump": {**pump, "adress": 2}}},
                              "no option 'adress'"),
        "a number as text": ({"id": "c", "instruments": {"pump": {**pump, "address": "1"}}},
                             "address must be a whole number"),
        "true as a number": ({"id": "c", "instruments": {"gauge": {"kind": "tpg", "channels": True}}},
                             "channels must be a whole number"),
        "an address out of range": ({"id": "c", "instruments": {"pump": {**pump, "address": 0}}},
                                    "address must be 1 to 255"),
        "an id with capitals and a space": ({"id": "Chamber 2"}, "\"id\" must be"),
        "an unknown field": ({"id": "c", "pumps": {}}, "unknown field 'pumps'"),
        "instruments as a list": ({"id": "c", "instruments": [pump]},
                                  "\"instruments\" must be a table"),
    }
    wrong = {label: _refused(data) or "ACCEPTED" for label, (data, says) in cases.items()
             if says not in _refused(data) or "test.toml" not in _refused(data)}
    check("a chamber's file is refused, naming the file and the field, for "
          + ", ".join(cases), not wrong,
          "; ".join(f"{k}: {v[:50]}" for k, v in wrong.items()))
    empty = chambermod.parse({"id": "bare"}, source="bare.json")
    check("a chamber with no instruments at all can be described",
          empty.instruments == {} and empty.name == "bare"
          and set(empty.describe()["absent"]) == set(chambermod.ROLES.values()))


def _command_line(folder: str) -> None:
    def plain():
        args, said = _parse_args([])
        return (args is not None and args.chamber_config.describe()
                == chambermod.load(chambermod.DEFAULT).describe(), said[-120:])
    attempt("with no flags the panel is chamber 1, as its file says", plain)

    def as_the_unit_starts_it():
        with open(os.path.join(ROOT, "deploy", "vacuum-lab-web.service")) as unit:
            text = unit.read()
        command = re.search(r"^ExecStart=(.*?)(?<!\\)$", text,
                            re.M | re.S).group(1).replace("\\\n", " ")
        # The unit names the release's own folder; here, this tree.
        argv = [a.replace("/home/esarodriguezm/vacuum-lab/current", ROOT)
                for a in shlex.split(command)[2:]]
        args, said = _parse_args(argv)
        same = args is not None and all(
            (getattr(args.chamber_config.instruments[r], "port"),
             args.chamber_config.instruments[r].options)
            == (i.port, i.options)
            for r, i in chambermod.load(chambermod.DEFAULT).instruments.items())
        return same, said[-120:] or " ".join(argv)
    attempt("the lab PC's unit starts chamber 1 as its file says, with no "
            "instrument flag over it", as_the_unit_starts_it)

    def unit_names_no_instrument():
        with open(os.path.join(ROOT, "deploy", "vacuum-lab-web.service")) as unit:
            text = unit.read()
        flags = [f for f in ("--serial", "--address", "--gauge", "--no-pump",
                             "--no-gauge", "--pump-settle-ms")
                 if re.search(rf"^\s.*{f}\b", text, re.M) or
                 re.search(rf"^ExecStart=.*{f}\b", text, re.M)]
        return not flags and "--chamber" in text, f"flags in the unit: {flags}"
    attempt("the unit leaves the instruments to the chamber's file",
            unit_names_no_instrument)

    def flags_over_the_file():
        args, said = _parse_args(["--serial", "/dev/ttyUSB9", "--address", "3",
                                  "--gauge-channels", "1", "--chamber-name", "Rig A"])
        if args is None:
            return False, said[-120:]
        c = args.chamber_config
        return ((c.instruments["pump"].port, c.instruments["pump"].options["address"],
                 c.instruments["gauge"].options["channels"], c.name, c.id)
                == ("/dev/ttyUSB9", 3, 1, "Rig A", "chamber-1"), str(c.describe()))
    attempt("--serial, --address, --gauge-channels and --chamber-name override "
            "the file for one start", flags_over_the_file)

    def left_out():
        args, _ = _parse_args(["--no-pump"])
        return (args is not None and "pump" not in args.chamber_config.instruments
                and "gauge" in args.chamber_config.instruments, "")
    attempt("--no-pump leaves the turbopump out whatever the file says", left_out)

    gauge_only = _write(folder, "chamber-2.toml",
                        'id = "chamber-2"\n# no turbopump the panel drives\n'
                        '[instruments.gauge]\nkind = "tpg"\nport = "auto"\n')
    broken = _write(folder, "broken.toml", 'id = "chamber-9\n[instruments\n')
    refusals = {
        "--serial on a chamber with no pump":
            (["--chamber", gauge_only, "--serial", "/dev/ttyUSB0"], "has none"),
        "an address out of range": (["--address", "0"], "address must be 1 to 255"),
        "a bad chamber id": (["--chamber-id", "Chamber 2"], "chamber id"),
        "a file that is not there": (["--chamber", os.path.join(folder, "none.toml")],
                                     "cannot be read"),
        "a file that is not TOML": (["--chamber", broken], "not valid TOML"),
    }
    wrong = {}
    for label, (argv, says) in refusals.items():
        args, said = _parse_args(argv)
        if args is not None or says not in said:
            wrong[label] = said.strip()[-60:] or "ACCEPTED"
    check("the command line is refused before anything opens for "
          + ", ".join(refusals), not wrong,
          "; ".join(f"{k}: {v}" for k, v in wrong.items()))


def _built(folder: str, pump_port: str, gauge_port: str) -> None:
    memory = discovery.PortMemory(os.path.join(folder, "ports.json"))
    ports = {"pump": pump_port, "gauge": gauge_port}

    # Chamber 1: both instruments, the pump reading its pressure from the gauge.
    devices = registry.Registry()
    try:
        services = chambermod.build(chambermod.load(chambermod.DEFAULT), devices,
                                    memory, search=False, simulated=ports,
                                    log=lambda *a: None)
        check("chamber 1 opens a TC 600 as the pump and a TPG as the gauge, the "
              "pump's interlock reading the gauge",
              devices.keys() == ["pump", "gauge"]
              and services["pump"].kind == "tc600"
              and services["gauge"].kind == "tpg"
              and services["pump"]._pressure_source is services["gauge"],
              f"{devices.keys()}")
    finally:
        devices.close()

    # A chamber with a gauge and no turbopump the panel can drive.
    devices = registry.Registry()
    database = dbmod.Database(os.path.join(folder, "two.db"))
    try:
        chambermod.build(chambermod.parse(GAUGE_ONLY, "chamber-2.json"), devices,
                         memory, search=False, simulated=ports,
                         log=lambda *a: None)
        engine = runs.RunEngine(database, devices, chamber="chamber-2")
        wait_until(lambda: devices.get("gauge").snapshot().get("connected"), 10)
        listed = {e["name"]: e for e in engine.catalogue()}
        needs_pump = sorted(n for n, p in runs.CATALOGUE.items()
                            if "pump" in p.instruments)
        off = engine.unconfigured_all()
        check("without a turbopump every procedure that needs one is listed but "
              "switched off, saying so -- and the hold, which drives nothing, "
              "is not",
              devices.keys() == ["gauge"] and sorted(off) == needs_pump
              and all(off[n] == ["turbopump"] for n in needs_pump)
              and all(listed[n]["available"] is False
                      and "turbopump" in listed[n]["missing"] for n in needs_pump)
              and listed["hold"]["available"] is True and "hold" not in off
              and all("turbopump" in engine.unavailable()[n] for n in needs_pump),
              f"off: {sorted(off)}")
        started = engine.start("pumpdown", {})
        check("... and refused if asked for anyway, with no command sent",
              started["ok"] is False and "started without one" in started.get("error", "")
              and engine.status()["running"] is False,
              started.get("error", "STARTED")[:120])
        held = engine.start("hold", {"duration_min": 0.1, "interval_s": 1},
                            name="Selftest gauge-only hold")
        finished = held["ok"] and wait_until(
            lambda: not engine.status()["running"], 30, 0.3)
        record = database.runs(limit=1)[0] if held["ok"] else {}
        check("a hold runs to the end on a chamber with only a gauge",
              finished and record.get("status") == "completed"
              and record.get("chamber") in (None, "chamber-2")
              and database.sample_count(record["id"]) >= 3,
              held.get("error") or f"{record.get('status')}, "
              f"{database.sample_count(record['id']) if record else 0} samples")
    finally:
        devices.close()
        database.close()

    # The port survey asks free ports only what this chamber has: another
    # chamber's controller is never sent TC 600 questions by a gauge-only panel.
    asked = []
    real_find = discovery.find
    discovery.find = lambda kind, ports, **k: asked.append(kind)
    try:
        discovery.Finder(registry.Registry(), memory, kinds=["tpg"],
                         ports=lambda exclude=(): [{"device": "/dev/ttyFREE9",
                                                    "serial_number": ""}]).survey()
        everything = []
        discovery.find = lambda kind, ports, **k: everything.append(kind)
        discovery.Finder(registry.Registry(), memory,
                         ports=lambda exclude=(): [{"device": "/dev/ttyFREE9",
                                                    "serial_number": ""}]).survey()
    finally:
        discovery.find = real_find
    check("a chamber's port survey asks free ports only for the kinds it has",
          asked == ["tpg"] and sorted(everything) == ["tc600", "tpg"],
          f"gauge-only asked {asked}; unrestricted asked {everything}")

    # The panel's own wiring: --no-pump on chamber 1, built as webserver.py
    # builds it, refuses a pump-down before anything is sent.
    import webserver
    args, said = _parse_args(["--no-pump"])
    devices = registry.Registry()
    database = dbmod.Database(os.path.join(folder, "nopump.db"))
    try:
        chambermod.build(args.chamber_config, devices, memory, search=False,
                         simulated=ports, log=lambda *a: None)
        engine = webserver.engine_for(args.chamber_config, database, devices)
        refused = engine.start("pumpdown", {})
        check("the panel started with --no-pump refuses a pump-down, as wired "
              "by webserver.py",
              refused["ok"] is False and "turbopump" in refused.get("error", "")
              and engine.roles == {"gauge"} and devices.keys() == ["gauge"],
              refused.get("error", "STARTED")[:100])
    finally:
        devices.close()
        database.close()

    # The panel tells the engine its chamber's roles (webserver.py passes
    # the file's): judged by those, attached or not.
    database = dbmod.Database(os.path.join(folder, "told.db"))
    try:
        told = runs.RunEngine(database, registry.Registry(), roles=("gauge",))
        full = runs.RunEngine(database, registry.Registry(), roles=runs.ALL_ROLES)
        needs_pump = sorted(n for n, p in runs.CATALOGUE.items()
                            if "pump" in p.instruments)
        check("an engine told its chamber's roles judges procedures by them",
              sorted(told.unconfigured_all()) == needs_pump
              and full.unconfigured_all() == {},
              f"{sorted(told.unconfigured_all())} / {full.unconfigured_all()}")
    finally:
        database.close()

    # A pump and no gauge: nothing may run -- the crossover interlock and
    # every curve need the pressure.
    devices = registry.Registry()
    database = dbmod.Database(os.path.join(folder, "three.db"))
    try:
        chambermod.build(chambermod.parse(PUMP_ONLY, "chamber-3.json"), devices,
                         memory, search=False, simulated=ports,
                         log=lambda *a: None)
        engine = runs.RunEngine(database, devices, chamber="chamber-3")
        off = engine.unconfigured_all()
        check("without a gauge no procedure can run at all",
              sorted(off) == sorted(runs.CATALOGUE)
              and all("gauge controller" in v for v in off.values()),
              f"{len(off)} of {len(runs.CATALOGUE)} switched off")
    finally:
        devices.close()
        database.close()
    time.sleep(0.2)
