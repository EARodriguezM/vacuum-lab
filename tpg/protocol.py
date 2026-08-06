"""Pfeiffer Vacuum gauge-controller serial protocol (the "mnemonic" protocol).

Shared by the TPG 25x / 26x / 36x family and the TPG 256 A MaxiGauge. Nothing
like the TC 600's checksummed telegrams -- this one is a two-step handshake:

    host  -> "PR1<CR><LF>"        the mnemonic, optionally with parameters
    unit  -> "<ACK><CR><LF>"      understood        (or <NAK> for refused)
    host  -> "<ENQ>"              "now send it"
    unit  -> "0,1.2340E-05<CR><LF>"

The second leg is the part people get wrong. The unit answers the mnemonic
with nothing but an acknowledgement; the *data* only comes after a separate
enquiry byte. Skipping the ENQ leaves the reply sitting in the unit and every
subsequent exchange one frame out of step.

<ETX> (0x03) aborts a part-received command and clears the unit's input
buffer, which is why every exchange here starts by sending one: it makes a
resynchronisation cost one byte instead of a power cycle.

Provenance: this module is written from the published family protocol, not
from a TPG 252 A manual -- no such manual is in this repository, and the unit
was not reachable when the driver was written.
`tpgctl.py probe` asks the hardware which mnemonics it actually implements;
trust that over the table in commands.py.
"""

from __future__ import annotations

# --- control bytes --------------------------------------------------------

ETX = b"\x03"  #: abort the current command, flush the unit's input buffer
ENQ = b"\x05"  #: "send me the data you acknowledged"
ACK = b"\x06"  #: mnemonic understood
NAK = b"\x15"  #: mnemonic refused
CR = b"\x0d"
LF = b"\x0a"

TERMINATOR = CR + LF


class TpgError(Exception):
    """Base class for every fault this protocol can report."""


class ProtocolError(TpgError):
    """Raised when a frame is malformed or arrives out of step."""


class NakError(TpgError):
    """Raised when the unit refuses a mnemonic outright.

    Almost always one of: the mnemonic does not exist on this model, a
    parameter is out of range, or the unit is locked against remote writes.
    Call `error_status()` to have the unit say which.
    """


class Timeout(TpgError):
    """Raised when the unit does not answer within the read timeout."""


# --- measurement status ---------------------------------------------------

#: The status digit that prefixes every pressure reading. Only status 0 means
#: the number after it is a real measurement -- the rest are the gauge telling
#: you why it is not, and the accompanying value is meaningless.
MEASUREMENT_STATUS = {
    0: ("ok", "measurement data okay"),
    1: ("underrange", "pressure below the gauge's measuring range"),
    2: ("overrange", "pressure above the gauge's measuring range"),
    3: ("sensor_error", "sensor error"),
    4: ("sensor_off", "sensor switched off"),
    5: ("no_sensor", "no sensor connected"),
    6: ("id_error", "gauge identification error"),
}

#: Statuses where the numeric value must not be plotted or logged as pressure.
INVALID_STATUSES = frozenset({1, 2, 3, 4, 5, 6})


# --- error register -------------------------------------------------------

#: `ERR` answers with a 4-digit word; the digits are independent flags, so
#: several can be set at once (e.g. "0011" = bad parameter *and* bad syntax).
ERROR_BITS = (
    (0, "CONTROLLER", "controller error -- see the unit's own display"),
    (1, "NO_HWR", "no hardware: the addressed option is not fitted"),
    (2, "PAR", "inadmissible parameter"),
    (3, "SYN", "syntax error"),
)


def decode_error(word: str) -> list[str]:
    """Turn an `ERR` reply such as '0010' into a list of human descriptions."""
    word = word.strip()
    if not word or not word.isdigit():
        raise ProtocolError(f"malformed error word {word!r}")
    if set(word) == {"0"}:
        return []
    faults = []
    for index, _name, description in ERROR_BITS:
        if index < len(word) and word[index] != "0":
            faults.append(description)
    return faults or [f"unrecognised error word {word}"]


# --- framing --------------------------------------------------------------


def build(mnemonic: str, *parameters) -> bytes:
    """Assemble one command frame.

    Parameters are comma-separated after the mnemonic. Sending a mnemonic with
    no parameters is the family's universal "report the current value" form,
    which is what makes a read-only capability probe possible.
    """
    text = mnemonic.strip().upper()
    if not text.isalnum() or not 2 <= len(text) <= 4:
        raise ValueError(f"implausible mnemonic {mnemonic!r}")
    if parameters:
        text += "," + ",".join(_format_parameter(p) for p in parameters)
    return text.encode("ascii") + TERMINATOR


def _format_parameter(value) -> str:
    """Render one parameter the way the unit expects to read it back."""
    if isinstance(value, bool):
        # No bare booleans on the wire: every flag here is an enumeration
        # where 0 usually means "no change" rather than "off", so silently
        # mapping True/False to 1/0 would write the wrong thing.
        raise TypeError("pass the explicit enumeration value, not a bool")
    if isinstance(value, float):
        return f"{value:.4E}"
    return str(value)


def split_data(line: str) -> list[str]:
    """Split a data line into its comma-separated fields, trimming blanks."""
    return [field.strip() for field in line.strip("\r\n").split(",")]


def parse_pressure(status_field: str, value_field: str) -> tuple[int, float]:
    """Parse one `status,value` pair into (status code, value).

    The value is returned even when the status says it is not a measurement,
    because callers logging a run still want the raw number on record; use
    `INVALID_STATUSES` to decide whether it means anything.
    """
    try:
        status = int(status_field)
    except ValueError as exc:
        raise ProtocolError(f"non-numeric status {status_field!r}") from exc
    try:
        value = float(value_field)
    except ValueError as exc:
        raise ProtocolError(f"non-numeric pressure {value_field!r}") from exc
    if status not in MEASUREMENT_STATUS:
        raise ProtocolError(f"unknown measurement status {status}")
    return status, value


def status_name(status: int) -> str:
    return MEASUREMENT_STATUS.get(status, ("unknown", ""))[0]


def status_text(status: int) -> str:
    return MEASUREMENT_STATUS.get(status, ("unknown", f"status {status}"))[1]
