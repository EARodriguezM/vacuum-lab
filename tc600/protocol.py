"""Pfeiffer Vacuum serial protocol ("PV protocol") telegram encoding/decoding.

Telegram layout (plain ASCII, terminated by CR):

    AAA  AC  PPP  LL  DATA...  CCC  <CR>
    |    |   |    |   |        |
    |    |   |    |   |        +-- checksum, 3 digits
    |    |   |    |   +----------- payload, LL characters
    |    |   |    +--------------- payload length, 2 digits
    |    |   +-------------------- parameter number, 3 digits
    |    +------------------------ action: 00 = read, 10 = write/reply
    +----------------------------- device address, 3 digits

Checksum is the sum of the ASCII values of every character before it,
modulo 256, rendered as three decimal digits.
"""

from __future__ import annotations

# --- action codes ---------------------------------------------------------

ACTION_READ = "00"
ACTION_WRITE = "10"

#: Payload used to request a value rather than set one.
QUERY_PAYLOAD = "=?"

#: Broadcast address; every TC 600 on the bus listens but none replies.
GROUP_ADDRESS = 960

#: Error payloads the pump can return in place of a value.
ERROR_PAYLOADS = {
    "NO_DEF": "parameter does not exist on this device",
    "_RANGE": "value outside the permitted range",
    "_LOGIC": "access violation (wrong mode, or parameter is read-only)",
}


class ProtocolError(Exception):
    """Raised when a telegram is malformed or the pump reports an error."""


class ChecksumError(ProtocolError):
    """Raised when a received telegram's checksum does not match its body."""


class DeviceError(ProtocolError):
    """Raised when the pump answers with NO_DEF / _RANGE / _LOGIC."""


def checksum(body: str) -> str:
    """Return the 3-digit checksum for everything preceding the checksum field."""
    return f"{sum(body.encode('ascii')) % 256:03d}"


def build(address: int, action: str, parameter: int, payload: str) -> bytes:
    """Assemble one telegram, ready to write to the serial port."""
    if not 0 <= address <= 999:
        raise ValueError(f"address out of range: {address}")
    if not 0 <= parameter <= 999:
        raise ValueError(f"parameter out of range: {parameter}")
    if len(payload) > 99:
        raise ValueError(f"payload too long ({len(payload)} chars)")

    body = f"{address:03d}{action}{parameter:03d}{len(payload):02d}{payload}"
    return (body + checksum(body) + "\r").encode("ascii")


def build_read(address: int, parameter: int) -> bytes:
    """Assemble a read request for one parameter."""
    return build(address, ACTION_READ, parameter, QUERY_PAYLOAD)


def build_write(address: int, parameter: int, payload: str) -> bytes:
    """Assemble a write request. `payload` must already be type-encoded."""
    return build(address, ACTION_WRITE, parameter, payload)


class Reply:
    """A parsed response telegram."""

    __slots__ = ("address", "action", "parameter", "payload", "raw")

    def __init__(self, address: int, action: str, parameter: int, payload: str, raw: str):
        self.address = address
        self.action = action
        self.parameter = parameter
        self.payload = payload
        self.raw = raw

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Reply(address={self.address}, parameter={self.parameter}, "
            f"payload={self.payload!r})"
        )


def parse(frame: bytes) -> Reply:
    """Parse one telegram (with or without its trailing CR) into a `Reply`.

    Raises `ChecksumError` if the checksum disagrees and `DeviceError` if the
    pump answered with one of the documented error payloads.
    """
    text = frame.decode("ascii", errors="replace").strip("\r\n")

    # 3 addr + 2 action + 3 param + 2 len + 3 checksum = 13 characters minimum.
    if len(text) < 13:
        raise ProtocolError(f"telegram too short ({len(text)} chars): {text!r}")

    body, received_sum = text[:-3], text[-3:]
    expected_sum = checksum(body)
    if received_sum != expected_sum:
        raise ChecksumError(
            f"checksum {received_sum} does not match computed {expected_sum} "
            f"for telegram {text!r}"
        )

    try:
        address = int(body[0:3])
        parameter = int(body[5:8])
        length = int(body[8:10])
    except ValueError as exc:
        raise ProtocolError(f"non-numeric header field in {text!r}") from exc

    action = body[3:5]
    payload = body[10:]

    if len(payload) != length:
        raise ProtocolError(
            f"declared payload length {length} but got {len(payload)} chars: {text!r}"
        )

    if payload in ERROR_PAYLOADS:
        raise DeviceError(f"parameter {parameter}: {ERROR_PAYLOADS[payload]}")

    return Reply(address, action, parameter, payload, text)


# --- data types -----------------------------------------------------------
#
# Each entry knows how to turn a payload string into a Python value and back.
# Names match the type numbers used in the Pfeiffer parameter documentation.


class DataType:
    """Base class: a payload codec identified by its Pfeiffer type number."""

    number: int
    width: int

    @classmethod
    def decode(cls, payload: str):
        raise NotImplementedError

    @classmethod
    def encode(cls, value) -> str:
        raise NotImplementedError


class BooleanOld(DataType):
    """Type 0: '111111' for true, '000000' for false."""

    number, width = 0, 6

    @classmethod
    def decode(cls, payload: str) -> bool:
        if payload == "111111":
            return True
        if payload == "000000":
            return False
        raise ProtocolError(f"invalid boolean_old payload {payload!r}")

    @classmethod
    def encode(cls, value) -> str:
        return "111111" if value else "000000"


class UInteger(DataType):
    """Type 1: six decimal digits, plain unsigned integer."""

    number, width = 1, 6

    @classmethod
    def decode(cls, payload: str) -> int:
        return int(payload)

    @classmethod
    def encode(cls, value) -> str:
        return f"{int(value):06d}"


class UReal(DataType):
    """Type 2: six digits representing a fixed-point value with 2 decimals."""

    number, width = 2, 6

    @classmethod
    def decode(cls, payload: str) -> float:
        return int(payload) / 100.0

    @classmethod
    def encode(cls, value) -> str:
        return f"{round(float(value) * 100):06d}"


class String6(DataType):
    """Type 4: six ASCII characters."""

    number, width = 4, 6

    @classmethod
    def decode(cls, payload: str) -> str:
        return payload.strip()

    @classmethod
    def encode(cls, value) -> str:
        return f"{str(value):<6.6}"


class BooleanNew(DataType):
    """Type 6: a single '1' or '0'."""

    number, width = 6, 1

    @classmethod
    def decode(cls, payload: str) -> bool:
        if payload == "1":
            return True
        if payload == "0":
            return False
        raise ProtocolError(f"invalid boolean_new payload {payload!r}")

    @classmethod
    def encode(cls, value) -> str:
        return "1" if value else "0"


class UShortInt(DataType):
    """Type 7: three decimal digits."""

    number, width = 7, 3

    @classmethod
    def decode(cls, payload: str) -> int:
        return int(payload)

    @classmethod
    def encode(cls, value) -> str:
        return f"{int(value):03d}"


class UExpoNew(DataType):
    """Type 10: 4-digit mantissa + 2-digit exponent, value = m.mmm * 10^(e-20)."""

    number, width = 10, 6

    @classmethod
    def decode(cls, payload: str) -> float:
        mantissa = int(payload[:4]) / 1000.0
        exponent = int(payload[4:]) - 20
        return mantissa * (10.0**exponent)

    @classmethod
    def encode(cls, value) -> str:
        value = float(value)
        if value <= 0:
            return "000000"
        exponent = 0
        mantissa = value
        while mantissa >= 10.0:
            mantissa /= 10.0
            exponent += 1
        while mantissa < 1.0:
            mantissa *= 10.0
            exponent -= 1
        return f"{round(mantissa * 1000):04d}{exponent + 20:02d}"


class String16(DataType):
    """Type 11: sixteen ASCII characters."""

    number, width = 11, 16

    @classmethod
    def decode(cls, payload: str) -> str:
        return payload.strip()

    @classmethod
    def encode(cls, value) -> str:
        return f"{str(value):<16.16}"


BY_NUMBER = {
    cls.number: cls
    for cls in (
        BooleanOld,
        UInteger,
        UReal,
        String6,
        BooleanNew,
        UShortInt,
        UExpoNew,
        String16,
    )
}
