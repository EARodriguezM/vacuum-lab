"""Which instruments a chamber has: its file in `chambers/`, read and checked.

The panel knows its instruments by *role* -- `pump`, `gauge` -- and the run
engine, the interlocks, the alarms, the charts and the pages all ask for a
role, never for a model. A chamber's file (TOML, read with the standard
library's `tomllib`, so it can carry comments) says which driver fills each
role on that chamber, on which port and with which settings:

    id = "chamber-1"
    name = "Chamber 1"
    about = "Magnetron sputtering in argon"

    [instruments.pump]
    kind = "tc600"
    port = "auto"
    address = 1

    [instruments.gauge]
    kind = "tpg"
    port = "auto"
    channels = 2

A role the file leaves out is not there: the panel starts without it, and
every procedure that needs it is switched off, saying what it is missing
(`Procedure.instruments`). A rig is never assumed to be the first one.

The file is read strictly. An unknown kind, a kind in a role it cannot fill,
an option no driver has -- a typo, most likely -- stops the panel at start
with the file and the field named, rather than starting it on a default
nobody chose.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field

from . import discovery, gauge, pump

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Where the chambers' files are, and the one the panel starts with.
FOLDER = os.path.join(HERE, "chambers")
DEFAULT = os.path.join(FOLDER, "chamber-1.toml")

#: The roles, in the order the instruments are opened -- the pump first, so
#: the gauge's port search can leave the pump's port alone -- and what each
#: is called where a procedure says it needs one.
ROLES = {
    "pump": "turbopump",
    "gauge": "gauge controller",
}

def _tc600(role: str, port: str, options: dict, found: dict | None):
    return pump.PumpService(role, port, address=options["address"],
                            settle=options["settle_ms"] / 1000.0)


def _tpg(role: str, port: str, options: dict, found: dict | None):
    # The rate the port search found it answering at, over the file's.
    baudrate = (found or {}).get("baudrate") or options["baudrate"]
    return gauge.GaugeService(role, port, baudrate=baudrate,
                              channels=options["channels"])


#: Each kind of instrument the panel has a driver for: the role it fills,
#: its options with their type and default, and how its service is made --
#: `service(role, port, options, found)`, `found` being what the port search
#: learned (None if it was not asked). A new driver is one entry here, its
#: package in drivers/ and its service in lab/; its `kind` must be the one
#: lab/discovery.py probes it by.
KINDS = {
    "tc600": {
        "role": "pump",
        "label": "Pfeiffer TC 600 drive unit (RS-485)",
        "options": {"address": (int, 1), "settle_ms": (float, 30.0)},
        "service": _tc600,
    },
    "tpg": {
        "role": "gauge",
        "label": "Pfeiffer TPG 25x/26x gauge controller (RS-232)",
        "options": {"baudrate": (int, None), "channels": (int, 2)},
        "service": _tpg,
    },
}

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


class ChamberError(ValueError):
    """A chamber's file that cannot be used, and why."""


@dataclass
class Instrument:
    role: str
    kind: str
    port: str = "auto"
    options: dict = field(default_factory=dict)


@dataclass
class Chamber:
    id: str
    name: str
    about: str = ""
    instruments: dict[str, Instrument] = field(default_factory=dict)
    source: str = ""

    def describe(self) -> dict:
        """What the panel says about this chamber: which kind fills each
        role, and which roles are empty."""
        return {"id": self.id, "name": self.name, "about": self.about,
                "instruments": {role: i.kind for role, i in self.instruments.items()},
                "absent": [ROLES[r] for r in ROLES if r not in self.instruments],
                "file": os.path.basename(self.source) if self.source else ""}


def load(path: str) -> Chamber:
    """The chamber described by the TOML file at `path`."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except OSError as exc:
        raise ChamberError(f"{path}: cannot be read ({exc.strerror or exc})")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ChamberError(f"{path}: not valid TOML ({exc})")
    return parse(data, source=path)


def parse(data, source: str = "") -> Chamber:
    """A chamber from its file's contents, every field checked."""
    where = source or "the chamber's file"

    def fail(message: str):
        raise ChamberError(f"{where}: {message}")

    if not isinstance(data, dict):
        fail("must be a table of fields")
    unknown = sorted(set(data) - {"id", "name", "about", "instruments"})
    if unknown:
        fail(f"unknown field {unknown[0]!r} (fields: id, name, about, instruments)")
    ident = data.get("id")
    if not isinstance(ident, str) or not _ID.match(ident):
        fail("\"id\" must be lower-case letters, digits and dashes, e.g. "
             "\"chamber-2\" -- accounts' roles are granted per chamber id")
    name = data.get("name", ident)
    about = data.get("about", "")
    if not isinstance(name, str) or not name.strip():
        fail("\"name\" must be a non-empty string")
    if not isinstance(about, str):
        fail("\"about\" must be a string")
    raw = data.get("instruments", {})
    if not isinstance(raw, dict):
        fail("\"instruments\" must be a table: [instruments.<role>]")
    instruments = {}
    for role, entry in raw.items():
        if role not in ROLES:
            fail(f"unknown role {role!r} (roles: {', '.join(ROLES)})")
        instruments[role] = _instrument(role, entry, fail)
    return Chamber(id=ident, name=name.strip(), about=about.strip(),
                   instruments=instruments, source=source)


def _instrument(role: str, entry, fail) -> Instrument:
    if not isinstance(entry, dict):
        fail(f"instruments.{role} must be a table with a \"kind\"")
    kind = entry.get("kind")
    if kind not in KINDS:
        fail(f"instruments.{role}: unknown kind {kind!r} (kinds: "
             f"{', '.join(KINDS)})")
    spec = KINDS[kind]
    if spec["role"] != role:
        fail(f"instruments.{role}: a {kind} is a {spec['role']}, not a {role}")
    port = entry.get("port", "auto")
    if not isinstance(port, str) or not port.strip():
        fail(f"instruments.{role}.port must be a device path or \"auto\"")
    options = {}
    for key, value in entry.items():
        if key in ("kind", "port"):
            continue
        if key not in spec["options"]:
            fail(f"instruments.{role}: a {kind} has no option {key!r} "
                 f"(options: {', '.join(spec['options'])})")
        wanted, default = spec["options"][key]
        if value is None and default is None:
            options[key] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or (wanted is int and not isinstance(value, int)):
            fail(f"instruments.{role}.{key} must be "
                 f"{'a whole number' if wanted is int else 'a number'}")
        options[key] = wanted(value)
    for key, (wanted, default) in spec["options"].items():
        options.setdefault(key, default)
    if kind == "tc600" and not 1 <= options["address"] <= 255:
        fail(f"instruments.{role}.address must be 1 to 255")
    if kind == "tc600" and not 0 <= options["settle_ms"] <= 1000:
        fail(f"instruments.{role}.settle_ms must be 0 to 1000")
    if kind == "tpg" and not 1 <= options["channels"] <= 6:
        fail(f"instruments.{role}.channels must be 1 to 6")
    return Instrument(role=role, kind=kind, port=port.strip(), options=options)


def check_id(value: str) -> str:
    """A chamber id as the file must give it, or ChamberError."""
    if not isinstance(value, str) or not _ID.match(value):
        raise ChamberError(f"chamber id {value!r}: lower-case letters, digits "
                           f"and dashes, e.g. \"chamber-2\"")
    return value


def override(chamber: Chamber, role: str, flag: str, port: str | None = None,
             **options) -> None:
    """The command line's values for one instrument over its file's --
    `--serial`, `--address` and the rest -- checked as the file's are.
    Options given as None are left as the file has them."""
    instrument = chamber.instruments.get(role)
    if instrument is None:
        raise ChamberError(f"{flag} is for the {ROLES[role]}, and "
                           f"{os.path.basename(chamber.source) or 'this chamber'} "
                           f"has none")
    entry = {"kind": instrument.kind, "port": port or instrument.port,
             **instrument.options,
             **{k: v for k, v in options.items() if v is not None}}

    def fail(message: str):
        raise ChamberError(f"{flag}: {message}")
    chamber.instruments[role] = _instrument(role, entry, fail)


def build(chamber: Chamber, registry, memory: "discovery.PortMemory",
          search: bool = True, simulated: dict | None = None,
          log=print) -> dict:
    """Open each instrument the chamber has and add its service to
    `registry`; returns role -> service.

    Ports are found by asking (lab/discovery.py), each instrument leaving the
    ports already claimed alone; `simulated` (role -> pty) gives them
    instead. The turbopump is then wired to the gauge for its crossover
    interlock -- explicitly, not discovered: with no gauge the interlock
    refuses the start instead of guessing, which is the safe direction.
    """
    services, claimed = {}, []
    for role in ROLES:
        instrument = chamber.instruments.get(role)
        if instrument is None:
            continue
        options = instrument.options
        if simulated is not None:
            # The simulators answer at 9600 baud, and are not searched for.
            port, found = simulated[role], {"baudrate": 9600}
        else:
            result = discovery.startup(instrument.kind, instrument.port,
                                       options.get("address", 1), memory,
                                       search=search, exclude=claimed, log=log)
            port, found = result["port"], result["found"]
        claimed.append(port)
        service = KINDS[instrument.kind]["service"](role, port, options, found)
        services[role] = registry.add(service)
    if "pump" in services and "gauge" in services:
        services["pump"].attach_pressure(services["gauge"])
    return services
