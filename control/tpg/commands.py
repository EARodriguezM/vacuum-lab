"""Mnemonic table for the Pfeiffer TPG gauge-controller family.

Same provenance caveat as `tc600/params.py`: this is the published family
command set, and a given model implements a subset of it. A TPG 252 A has two
measurement channels and four switching functions, so the six-channel and
display-control mnemonics below are expected to be refused on it -- they are
listed because the probe costs one round trip each and knowing what is *not*
there is worth as much as knowing what is.

`SAFE_TO_PROBE` is the set that can be sent with no parameters as a pure
query. Everything outside it either changes state with no parameters (RES
clears the error latch, SAV burns EEPROM, COM starts an unsolicited stream) or
is a factory test function, so the probe never sends those.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Command:
    """One mnemonic: what it does, and what its parameters mean."""

    mnemonic: str
    name: str
    access: str  # "r", "w" or "rw"
    description: str
    parameters: tuple[str, ...] = ()
    values: dict = field(default_factory=dict)

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access


# --- enumerations ---------------------------------------------------------

#: Pressure unit (`UNI`). A TPG 252 A is expected to offer 0-2 only.
UNITS = {
    0: "mbar",
    1: "Torr",
    2: "Pa",
    3: "micron",
    4: "hPa",
    5: "V",
}

#: Display suffix for each unit, and the factor to millibar. Everything inside
#: this software is stored in millibar so that runs stay comparable no matter
#: what the front panel was set to when they were recorded.
TO_MBAR = {
    0: 1.0,
    1: 1.33322,          # Torr  -> mbar
    2: 0.01,             # Pa    -> mbar
    3: 1.33322e-3,       # micron (mTorr) -> mbar
    4: 1.0,              # hPa   == mbar
    5: 1.0,              # volts: not a pressure, no conversion is meaningful
}

#: Gauge on/off (`SEN`). 0 is "no change" when writing but "cannot be switched"
#: when reading, which is a genuine wart in the protocol rather than a typo.
SENSOR_WRITE = {0: "no change", 1: "off", 2: "on"}
#: Code 3 is not in the published family table but is what the real TPG 252 A
#: (firmware BG509727-C) returns for its live channel: that channel reads a
#: valid pressure with measurement status 0, so the gauge is unambiguously on.
#: Whether 3 additionally means "cannot be switched off" is unconfirmed -- the
#: TPG 252 A manual is not in this repository. Treated as on, which is what
#: matters to callers.
SENSOR_READ = {0: "cannot be switched", 1: "off", 2: "on", 3: "on"}

#: Read-back codes that mean the gauge is running. Used instead of `== 2`
#: so the UI does not offer to switch on a gauge that is already on.
SENSOR_ON_CODES = frozenset({2, 3})

#: Degas (`DGS`). Only meaningful for hot/cold cathode gauges.
DEGAS = {0: "off", 1: "on"}

#: Measurement filter time constant (`FIL`).
FILTER = {0: "fast", 1: "normal", 2: "slow"}

#: Interface baud rate (`BAU`). The real TPG 252 A answers 4 while running at
#: 9600 baud, so this model does not use the family's 0/1/2 encoding and the
#: code cannot be mapped to a rate without its manual. Read
#: `TPG.baudrate` for the rate actually in use; it is the measured truth.
BAUD = {0: 9600, 1: 19200, 2: 38400}

#: Continuous-output interval (`COM`).
CONTINUOUS = {1: "100 ms", 2: "1 s", 3: "60 s"}

#: Keylock (`LOC`).
KEYLOCK = {0: "unlocked", 1: "locked"}

#: Gauge type identifiers returned by `TID`, and their measuring principle.
GAUGE_TYPES = {
    "TPR": "Pirani (TPR / PCR)",
    "PCR": "Pirani capacitive",
    "IKR9": "cold cathode, 1e-9 mbar",
    "IKR11": "cold cathode, 1e-11 mbar",
    "IKR": "cold cathode",
    "PKR": "FullRange, Pirani + cold cathode",
    "APR": "Piezo / linear",
    "AWR": "linear",
    "CMR": "capacitive",
    "IMR": "Pirani + hot cathode",
    "PBR": "FullRange, Pirani + Bayard-Alpert",
    # What the real TPG 252 A (firmware BG509727-C) calls the gauge on the rig,
    # whose label reads Pfeiffer PKR 251 (no. PTR26002x) -- a FullRange
    # Pirani + cold cathode head. Confirmed from the label, 2026-10-10.
    "CO9": "FullRange, Pirani + cold cathode (PKR 251)",
    # Confirmed on the real TPG 252 A (firmware BG509727-C): an empty channel
    # answers "noSe", not the "noSEn" the family documentation shows.
    "noSe": "no sensor",
    "noSEn": "no sensor",
    "noSen": "no sensor",
    "noid": "no identifier",
    "noIDE": "no identifier",
}

#: Identifiers that mean "nothing is plugged into this channel". Matched
#: case-insensitively; see `describe_gauge` and `TPG.gauge_types`.
ABSENT_GAUGES = frozenset({"nose", "nosen", "noid", "noide", ""})


COMMANDS: tuple[Command, ...] = (
    # --- identity ---------------------------------------------------------
    Command("PNR", "firmware", "r", "Program number / firmware version"),
    Command("TID", "gauge_types", "r",
            "Transmitter identification: the gauge fitted to each channel"),
    Command("RSX", "interface", "rw", "Interface type and mode",
            ("mode",)),
    Command("NAD", "node_address", "rw", "RS-485 node address", ("address",)),
    Command("BAU", "baud", "rw", "Interface baud rate", ("rate",), BAUD),
    # --- measurement ------------------------------------------------------
    Command("PRX", "pressures", "r",
            "Pressure of every channel: status,value pairs"),
    Command("PR1", "pressure_1", "r", "Pressure of channel 1: status,value"),
    Command("PR2", "pressure_2", "r", "Pressure of channel 2: status,value"),
    Command("UNI", "unit", "rw", "Pressure unit of the display and the bus",
            ("unit",), UNITS),
    Command("FIL", "filter", "rw", "Measurement filter time constant",
            ("ch1", "ch2"), FILTER),
    Command("COR", "correction", "rw",
            "Gas correction factor per channel", ("ch1", "ch2")),
    Command("CAL", "calibration", "rw",
            "Calibration factor per channel", ("ch1", "ch2")),
    Command("OFC", "offset_correction", "rw",
            "Offset correction for linear gauges", ("ch1", "ch2")),
    Command("OFD", "offset_display", "rw", "Offset display", ("ch1", "ch2")),
    Command("FSR", "full_scale", "rw",
            "Full-scale range of a linear gauge", ("ch1", "ch2")),
    # --- gauge control ----------------------------------------------------
    Command("SEN", "sensor", "rw", "Switch each gauge on or off",
            ("ch1", "ch2"), SENSOR_WRITE),
    Command("DGS", "degas", "rw", "Degas each gauge", ("ch1", "ch2"), DEGAS),
    Command("SCX", "sensor_control", "rw",
            "Automatic gauge switch-on/off by another channel",
            ("channel", "lower", "upper")),
    Command("PUC", "underrange_ctrl", "rw",
            "Penning underrange control", ("mode",)),
    # --- switching functions ---------------------------------------------
    Command("SPS", "setpoint_status", "r",
            "State of every switching function: 0 = open, 1 = closed"),
    # Two reply shapes exist in this family. Models that associate a switching
    # function with a channel answer "channel,lower,upper"; the real TPG 252 A
    # answers "lower,upper" and the association is fixed. `TPG.setpoint`
    # handles both. That unit implements SP1 and SP2 only -- SP3 and SP4 are
    # refused -- so treat four as the family maximum, not the expectation.
    Command("SP1", "setpoint_1", "rw",
            "Switching function 1: lower and upper threshold",
            ("lower", "upper")),
    Command("SP2", "setpoint_2", "rw", "Switching function 2",
            ("lower", "upper")),
    Command("SP3", "setpoint_3", "rw", "Switching function 3",
            ("lower", "upper")),
    Command("SP4", "setpoint_4", "rw", "Switching function 4",
            ("lower", "upper")),
    # --- housekeeping -----------------------------------------------------
    Command("ERR", "error_status", "r", "Error register, 4 digits"),
    Command("LOC", "keylock", "rw", "Front-panel keylock", ("state",), KEYLOCK),
    Command("TLC", "torr_lock", "rw", "Lock the Torr unit out", ("state",)),
    Command("WDT", "watchdog", "rw", "Interface watchdog", ("state",)),
    Command("DCB", "display_bargraph", "rw", "Display bargraph", ("mode",)),
    Command("DCC", "display_contrast", "rw", "Display contrast", ("value",)),
    Command("DCD", "display_digits", "rw", "Displayed digits", ("value",)),
    Command("DCS", "display_screensave", "rw", "Screen saver", ("state",)),
    # --- state-changing: never sent by the probe --------------------------
    Command("RES", "reset", "rw",
            "Read the error list and clear the latched errors"),
    Command("SAV", "save", "w", "Write the current parameters to EEPROM",
            ("slot",)),
    Command("COM", "continuous", "rw",
            "Stream measurements unsolicited until another command arrives",
            ("interval",), CONTINUOUS),
)

BY_MNEMONIC: dict[str, Command] = {c.mnemonic: c for c in COMMANDS}
BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}

#: Mnemonics the capability probe may send bare. Excludes RES (clears the
#: error latch), SAV (writes EEPROM) and COM (starts an unsolicited stream);
#: all three do something even with no parameters.
SAFE_TO_PROBE: tuple[str, ...] = tuple(
    c.mnemonic for c in COMMANDS if c.mnemonic not in {"RES", "SAV", "COM"}
)


def lookup(key: str) -> Command:
    """Resolve a command by mnemonic ('PR1') or by name ('pressure_1')."""
    text = key.strip()
    if text.upper() in BY_MNEMONIC:
        return BY_MNEMONIC[text.upper()]
    if text.lower() in BY_NAME:
        return BY_NAME[text.lower()]
    raise KeyError(f"unknown command {key!r}")


def describe_gauge(identifier: str) -> str:
    """Expand a `TID` identifier into a readable gauge description.

    Falls back to the raw identifier rather than guessing, so an unrecognised
    gauge shows what the instrument actually said. The real TPG 252 A reports
    "CO9" on its live channel, which is not in the published family list.
    """
    text = identifier.strip()
    if text in GAUGE_TYPES:
        return GAUGE_TYPES[text]
    for prefix, description in GAUGE_TYPES.items():
        if text.upper().startswith(prefix.upper()):
            return description
    return text or "unknown"


def gauge_present(identifier: str) -> bool:
    """Whether a `TID` identifier means a gauge is actually plugged in."""
    return identifier.strip().lower() not in ABSENT_GAUGES


def to_mbar(value: float, unit_code: int) -> float:
    """Convert a reading in the unit the controller is set to into millibar."""
    return value * TO_MBAR.get(unit_code, 1.0)
