"""Serial transport for a TC 600 reached over an RS-232 -> RS-485 converter.

Two quirks of this particular chain are handled here:

1. The HXSP-485 draws its operating power parasitically from the RS-232
   handshake lines (TXD, DTR, RTS). Both DTR and RTS are asserted on open and
   held asserted, otherwise the converter is simply unpowered and you see
   silence on the bus.

2. RS-485 here is two-wire half duplex. Some auto-direction converters loop the
   transmitted bytes back into the receiver, so `_read_telegram` has to tell an
   echo apart from a reply. A read request carries action code 00 while every
   reply carries 10, so an echoed read is unambiguous -- that is how the
   converter's behaviour is learned. A write request, however, is byte for byte
   identical to the reply it provokes, so echoes there are only skipped once
   the converter is known to loop back.

A reply is also matched to its request by address and parameter number. A
reply that arrives after its own exchange gave up lands in the next one, and
taken as that one's answer it puts one parameter's value under another's
name: a late speed of "001500" read as the error code is a drive fault that
never happened.
"""

from __future__ import annotations

import sys
import time

import serial

from . import params, protocol

#: Serial line settings fixed by the TC 600 (PM 0504 BE, section 3.11).
BAUDRATE = 9600
BYTESIZE = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOPBITS = serial.STOPBITS_ONE

TERMINATOR = b"\r"


class Timeout(protocol.ProtocolError):
    """Raised when the pump does not answer within the read timeout."""


class TC600:
    """A TC 600 on an RS-485 bus, addressed by its serial interface address."""

    def __init__(
        self,
        port: str,
        address: int = 1,
        timeout: float = 1.0,
        retries: int = 2,
        settle: float = 0.03,
        handshake: bool = True,
        rts_direction: bool = False,
        rts_invert: bool = False,
    ):
        """Open `port` and talk to the unit at RS-485 `address` (parameter 797).

        `retries` extra attempts are made per exchange, since a shared
        half-duplex bus drops the occasional frame. `settle` is the pause after
        transmitting, giving the converter time to flip direction before the
        pump answers.

        `handshake` asserts DTR and RTS, which an HXSP-485 needs because it
        draws its power from those lines. Turn it off for a direct USB-to-RS-485
        dongle that drives its transmitter from RTS: there, holding RTS
        asserted keeps the driver enabled and the reply is never heard.

        `rts_direction` instead toggles RTS around each transmission, which is
        what a bare MAX485 module needs when DE and RE are tied together and
        wired to RTS. It overrides `handshake` for the RTS line. Whether an
        asserted RTS reads as a high or a low at the pin depends on the
        adapter, so flip `rts_invert` if the pump stays silent.
        """
        self.address = address
        self.retries = retries
        self.settle = settle
        self.rts_direction = rts_direction
        self.rts_invert = rts_invert
        #: Whether this converter loops transmissions back; learned on the
        #: first read exchange, None until then.
        self.echoes: bool | None = None
        self.serial = serial.Serial(
            port=port,
            baudrate=BAUDRATE,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=timeout,
            write_timeout=timeout,
        )
        #: True once DTR/RTS are asserted, False if the port refused, None if
        #: asserting them was not requested.
        self.handshake_powered: bool | None = None
        if rts_direction:
            # Park the driver in receive; _transmit raises it per telegram.
            try:
                self.serial.rts = self.rts_invert
            except (OSError, ValueError):
                pass
        elif handshake:
            # Virtual ports (pty, socat) do not implement these ioctls, which
            # is harmless there, but on real hardware a refusal means the
            # HXSP-485 never powers up.
            try:
                self.serial.dtr = True
                self.serial.rts = True
                self.handshake_powered = True
            except (OSError, ValueError) as exc:
                self.handshake_powered = False
                print(
                    f"warning: {port} does not support DTR/RTS control "
                    f"({exc}); an HXSP-485 on this port would be unpowered",
                    file=sys.stderr,
                )
        # An FTDI adapter holds received bytes for up to 16 ms before passing
        # them on; in low-latency mode, 1 ms. At 9600 baud that is a fifth of
        # every exchange. A port without the mode -- a pty -- only loses the
        # speed.
        try:
            self.serial.set_low_latency_mode(True)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            pass
        time.sleep(0.1)
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    # --- context manager -------------------------------------------------

    def __enter__(self) -> "TC600":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self.serial.is_open:
            self.serial.close()

    # --- low level -------------------------------------------------------

    def _drain_seconds(self, length: int) -> float:
        """How long `length` bytes need to clock out, plus USB slack."""
        # 10 bit times per byte: start + 8 data + stop.
        return length * 10.0 / BAUDRATE + 0.005

    def _transmit(self, request: bytes) -> None:
        """Write one telegram, driving RTS as a direction line if configured."""
        if not self.rts_direction:
            self.serial.write(request)
            self.serial.flush()
            time.sleep(self.settle)
            return

        self.serial.rts = not self.rts_invert  # enable the MAX485 driver
        try:
            self.serial.write(request)
            self.serial.flush()
            # flush() only drains the kernel buffer; the bytes still have to
            # clock out of the USB adapter. Releasing DE any earlier truncates
            # the telegram mid-frame.
            time.sleep(self._drain_seconds(len(request)))
        finally:
            self.serial.rts = self.rts_invert  # back to listening

    def _read_telegram(self, sent: bytes) -> bytes:
        """Read the reply to `sent`, skipping any echo of it and stale replies."""
        deadline = time.monotonic() + (self.serial.timeout or 1.0) * 2
        buffer = bytearray()
        is_read = sent[3:5] == protocol.ACTION_READ.encode()
        saw_echo = False
        # Address and parameter number, as sent: the reply carries both.
        wanted = (sent[0:3], sent[5:8])

        while time.monotonic() < deadline:
            chunk = self.serial.read_until(TERMINATOR)
            if not chunk:
                continue
            buffer.extend(chunk)
            if not buffer.endswith(TERMINATOR):
                continue

            frame = bytes(buffer)
            buffer.clear()

            if frame.strip() == sent.strip():
                if is_read:
                    # Replies always carry action 10, so a frame matching a
                    # read request can only be our own transmission looping
                    # back through the converter.
                    self.echoes = True
                    saw_echo = True
                    continue
                if self.echoes and not saw_echo:
                    # A write reply is byte-identical to its request, so this
                    # is only skippable on a converter known to loop back.
                    saw_echo = True
                    continue

            # The answer to some earlier request, arriving after that exchange
            # had given up -- or noise ending in a CR. Either way not this
            # parameter's value.
            text = frame.strip(b"\r\n")
            if (text[0:3], text[5:8]) != wanted:
                continue

            if is_read and not saw_echo:
                self.echoes = False
            return frame

        raise Timeout(
            f"no reply from address {self.address} within the read timeout"
        )

    def _exchange(self, request: bytes,
                  retries: int | None = None) -> protocol.Reply:
        """Send one telegram and return the parsed reply, retrying on noise.

        `retries` overrides the instance's own count for this exchange: the
        sampler can afford to lose a reading, a command should not be lost to
        one dropped reply.
        """
        last_error: Exception | None = None
        retries = self.retries if retries is None else retries

        for attempt in range(retries + 1):
            if attempt:
                time.sleep(0.1)
            try:
                self.serial.reset_input_buffer()
                self._transmit(request)
                return protocol.parse(self._read_telegram(request))
            except protocol.DeviceError:
                # The pump understood us and said no; retrying will not help.
                raise
            except (protocol.ProtocolError, serial.SerialException) as exc:
                last_error = exc

        raise type(last_error)(  # type: ignore[misc]
            f"{last_error} (after {retries + 1} attempts)"
        ) from last_error

    # --- parameter access ------------------------------------------------

    def read_raw(self, number: int, retries: int | None = None) -> str:
        """Read a parameter and return its undecoded payload string."""
        return self._exchange(protocol.build_read(self.address, number),
                              retries).payload

    def write_raw(self, number: int, payload: str,
                  retries: int | None = None) -> str:
        """Write an already-encoded payload and return what the pump echoes."""
        return self._exchange(protocol.build_write(self.address, number, payload),
                              retries).payload

    def read(self, key: str | int, retries: int | None = None):
        """Read a known parameter by name or number, decoded to a Python value."""
        parameter = params.lookup(key)
        if not parameter.readable:
            raise ValueError(f"{parameter.name} (P{parameter.number}) is write-only")
        return parameter.decode(self.read_raw(parameter.number, retries))

    def write(self, key: str | int, value, retries: int | None = None):
        """Write a known parameter by name or number, encoding `value` for it."""
        parameter = params.lookup(key)
        if not parameter.writable:
            raise ValueError(f"{parameter.name} (P{parameter.number}) is read-only")
        payload = parameter.encode(value)
        echoed = self.write_raw(parameter.number, payload, retries)
        # The pump echoes the accepted value; decode it so callers see reality.
        try:
            return parameter.decode(echoed)
        except protocol.ProtocolError:
            return echoed

    # --- convenience -----------------------------------------------------

    def identify(self) -> dict:
        """Return the unit's identity fields, skipping any it does not support."""
        result = {}
        for key in ("ElecName", "FwVersion", "HWVersion", "NominalSpd"):
            try:
                result[key] = self.read(key)
            except protocol.ProtocolError:
                pass
        return result

    def status(self) -> dict:
        """Read the standard status view; unsupported parameters are omitted."""
        result = {}
        for number in params.STATUS_VIEW:
            parameter = params.BY_NUMBER[number]
            try:
                result[parameter.name] = parameter.decode(self.read_raw(number))
            except protocol.ProtocolError:
                continue
        return result

    def pumping_station(self, on: bool):
        """Switch the whole pumping station on or off (parameter 10)."""
        return self.write("PumpgStatn", on)

    def motor(self, on: bool):
        """Switch just the turbopump motor on or off (parameter 23)."""
        return self.write("MotorPump", on)

    def standby(self, on: bool):
        """Enter or leave standby, which runs at the parameter 717 setpoint."""
        return self.write("Standby", on)

    def acknowledge_error(self):
        """Clear a latched fault (parameter 9)."""
        return self.write("ErrorAckn", True)

    def set_speed_percent(self, percent: float):
        """Set the speed setpoint in percent, enabling setting mode first.

        Parameter 26 selects the operating mode and must be 1 (rotation speed
        setting mode) for the setpoint in parameter 707 to take effect.
        """
        if not 20.0 <= percent <= 100.0:
            raise ValueError(f"speed setpoint must be 20-100 %, got {percent}")
        self.write("OpModeTMP", 1)
        return self.write("SpdSVal", percent)


def scan(
    port: str,
    addresses=range(1, 17),
    timeout: float = 0.4,
    handshake: bool = True,
    rts_direction: bool = False,
    rts_invert: bool = False,
) -> list[int]:
    """Probe `addresses` on `port` and return those that answer.

    Useful when you do not know the unit's configured address (parameter 797);
    factory default is 1.
    """
    found = []
    for address in addresses:
        try:
            with TC600(
                port,
                address=address,
                timeout=timeout,
                retries=0,
                handshake=handshake,
                rts_direction=rts_direction,
                rts_invert=rts_invert,
            ) as pump:
                pump.read_raw(349)  # ElecName: present on every drive unit.
                found.append(address)
        except Exception:
            continue
    return found
