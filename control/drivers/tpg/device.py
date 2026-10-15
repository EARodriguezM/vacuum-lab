"""Serial transport for a Pfeiffer TPG gauge controller.

Point-to-point RS-232, so none of the RS-485 direction-switching and echo
detection that `tc600/device.py` needs applies here. What does need care is
the two-leg handshake: an exchange that is abandoned between the ACK and the
ENQ leaves the unit holding a data line that the *next* exchange will read as
its own answer. Every exchange therefore opens with ETX, which discards
anything pending, so one bad round trip cannot desynchronise the session.
"""

from __future__ import annotations

import time

import serial

from . import commands, protocol

#: Line settings. Only the rate is configurable on the unit (`BAU`).
BYTESIZE = serial.EIGHTBITS
PARITY = serial.PARITY_NONE
STOPBITS = serial.STOPBITS_ONE

#: Rates the family offers, most likely first.
BAUD_RATES = (9600, 19200, 38400)


class TPG:
    """A TPG gauge controller on a serial port."""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        timeout: float = 1.0,
        retries: int = 2,
        channels: int = 2,
    ):
        """Open `port` at `baudrate`.

        `retries` extra attempts are made per exchange. `channels` is how many
        measurement channels the model has -- two for a TPG 252 A -- and is
        used to size the reply from `PRX`.
        """
        self.port = port
        self.channels = channels
        self.retries = retries
        self.serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=BYTESIZE,
            parity=PARITY,
            stopbits=STOPBITS,
            timeout=timeout,
            write_timeout=timeout,
        )
        #: Cached unit code, so pressure reads do not cost an extra round trip
        #: each. Invalidated by `set_unit`.
        self._unit_code: int | None = None
        # The gauge's FTDI adapter would otherwise hold each reply up to
        # 16 ms; a port without low-latency mode only loses the speed.
        try:
            self.serial.set_low_latency_mode(True)
        except (AttributeError, NotImplementedError, OSError, ValueError):
            pass
        time.sleep(0.1)
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

    # --- context manager -------------------------------------------------

    def __enter__(self) -> "TPG":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self.serial.is_open:
            try:
                # Leave the unit idle rather than mid-command, so the next
                # process to open the port starts from a known state.
                self.serial.write(protocol.ETX)
                self.serial.flush()
            except (serial.SerialException, OSError):
                pass
            self.serial.close()

    @property
    def baudrate(self) -> int:
        return self.serial.baudrate

    # --- low level -------------------------------------------------------

    def _read_line(self) -> bytes:
        line = self.serial.read_until(protocol.TERMINATOR)
        if not line:
            raise protocol.Timeout(f"no answer from {self.port} within the timeout")
        return line

    def _once(self, frame: bytes) -> str:
        """One complete two-leg exchange."""
        self.serial.write(protocol.ETX)
        self.serial.flush()
        time.sleep(0.02)
        self.serial.reset_input_buffer()

        self.serial.write(frame)
        self.serial.flush()

        acknowledgement = self._read_line()
        if acknowledgement.startswith(protocol.NAK):
            raise protocol.NakError(
                f"{frame.rstrip().decode('ascii', 'replace')} refused by the unit"
            )
        if not acknowledgement.startswith(protocol.ACK):
            raise protocol.ProtocolError(
                f"expected ACK or NAK, got {acknowledgement!r}"
            )

        self.serial.write(protocol.ENQ)
        self.serial.flush()
        return self._read_line().decode("ascii", "replace").strip("\r\n")

    def exchange(self, mnemonic: str, *parameters) -> str:
        """Send one mnemonic and return its data line, retrying on noise."""
        frame = protocol.build(mnemonic, *parameters)
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(0.1)
            try:
                return self._once(frame)
            except protocol.NakError:
                # The unit understood us and said no; retrying will not help.
                raise
            except (protocol.TpgError, serial.SerialException) as exc:
                last_error = exc

        raise type(last_error)(  # type: ignore[misc]
            f"{last_error} (after {self.retries + 1} attempts)"
        ) from last_error

    def fields(self, mnemonic: str, *parameters) -> list[str]:
        """Exchange, and split the data line into fields."""
        return protocol.split_data(self.exchange(mnemonic, *parameters))

    # --- identity --------------------------------------------------------

    def firmware(self) -> str:
        return self.exchange("PNR")

    def gauge_types(self) -> list[dict]:
        """Which gauge is fitted to each channel."""
        out = []
        for index, identifier in enumerate(self.fields("TID")[: self.channels], 1):
            out.append({
                "channel": index,
                "identifier": identifier,
                "description": commands.describe_gauge(identifier),
                "present": commands.gauge_present(identifier),
            })
        return out

    def identify(self) -> dict:
        """Identity fields, skipping anything this model does not implement."""
        result: dict = {}
        try:
            result["firmware"] = self.firmware()
        except protocol.TpgError:
            pass
        try:
            result["gauges"] = self.gauge_types()
        except protocol.TpgError:
            pass
        try:
            code, name = self.unit()
            result["unit"] = name
            result["unit_code"] = code
        except protocol.TpgError:
            pass
        result["channels"] = self.channels
        result["baudrate"] = self.baudrate
        return result

    # --- measurement -----------------------------------------------------

    def unit(self) -> tuple[int, str]:
        """Read the configured pressure unit as (code, name)."""
        code = int(self.fields("UNI")[0])
        self._unit_code = code
        return code, commands.UNITS.get(code, f"unit {code}")

    def set_unit(self, code: int) -> tuple[int, str]:
        if code not in commands.UNITS:
            raise ValueError(f"unknown unit code {code}")
        self.exchange("UNI", code)
        self._unit_code = None
        return self.unit()

    def unit_code(self) -> int:
        """The cached unit code, read once on first use."""
        if self._unit_code is None:
            self.unit()
        return self._unit_code  # type: ignore[return-value]

    def pressures(self) -> list[dict]:
        """Read every channel in one round trip.

        Values are reported both in the unit the controller is set to and in
        millibar, because everything downstream stores millibar and a run
        recorded in Torr must still compare against one recorded in mbar.
        """
        fields = self.fields("PRX")
        unit_code = self.unit_code()
        readings = []
        for index in range(self.channels):
            status_field, value_field = fields[index * 2], fields[index * 2 + 1]
            status, value = protocol.parse_pressure(status_field, value_field)
            valid = status not in protocol.INVALID_STATUSES
            readings.append({
                "channel": index + 1,
                "status": status,
                "status_name": protocol.status_name(status),
                "status_text": protocol.status_text(status),
                "valid": valid,
                "value": value,
                "unit": commands.UNITS.get(unit_code, ""),
                "mbar": commands.to_mbar(value, unit_code) if valid else None,
            })
        return readings

    def pressure(self, channel: int = 1) -> dict:
        """Read one channel. Cheaper than `pressures` only for a single gauge."""
        if not 1 <= channel <= self.channels:
            raise ValueError(f"channel must be 1-{self.channels}, got {channel}")
        status_field, value_field = self.fields(f"PR{channel}")[:2]
        status, value = protocol.parse_pressure(status_field, value_field)
        valid = status not in protocol.INVALID_STATUSES
        unit_code = self.unit_code()
        return {
            "channel": channel,
            "status": status,
            "status_name": protocol.status_name(status),
            "status_text": protocol.status_text(status),
            "valid": valid,
            "value": value,
            "unit": commands.UNITS.get(unit_code, ""),
            "mbar": commands.to_mbar(value, unit_code) if valid else None,
        }

    # --- gauge control ---------------------------------------------------

    def sensor_state(self) -> list[dict]:
        """Whether each gauge is on, off, or not switchable."""
        fields = self.fields("SEN", *([0] * self.channels))
        out = []
        for index, code in enumerate(fields[: self.channels]):
            value = int(code)
            out.append({
                "channel": index + 1,
                "code": value,
                "state": commands.SENSOR_READ.get(value, f"code {value}"),
                "on": value in commands.SENSOR_ON_CODES,
            })
        return out

    def set_sensor(self, channel: int, on: bool) -> list[dict]:
        """Switch one gauge on or off, leaving the others untouched.

        A cold-cathode gauge struck at atmosphere will not ignite and can
        contaminate; the caller is responsible for the pressure being low
        enough. `lab/gauge.py` enforces that for anything driven from the UI.
        """
        if not 1 <= channel <= self.channels:
            raise ValueError(f"channel must be 1-{self.channels}, got {channel}")
        # 0 is "no change" for the channels we are not addressing.
        args = [0] * self.channels
        args[channel - 1] = 2 if on else 1
        self.exchange("SEN", *args)
        return self.sensor_state()

    def degas(self, channel: int, on: bool) -> list[str]:
        if not 1 <= channel <= self.channels:
            raise ValueError(f"channel must be 1-{self.channels}, got {channel}")
        args = [0] * self.channels
        args[channel - 1] = 1 if on else 0
        return protocol.split_data(self.exchange("DGS", *args))

    def filter_state(self) -> list[dict]:
        fields = self.fields("FIL")
        return [
            {"channel": index + 1, "code": int(code),
             "state": commands.FILTER.get(int(code), f"code {code}")}
            for index, code in enumerate(fields[: self.channels])
        ]

    def set_filter(self, channel: int, mode: int) -> list[dict]:
        if mode not in commands.FILTER:
            raise ValueError(f"filter mode must be 0-2, got {mode}")
        current = [entry["code"] for entry in self.filter_state()]
        current[channel - 1] = mode
        self.exchange("FIL", *current)
        return self.filter_state()

    # --- switching functions ---------------------------------------------

    def setpoint_status(self) -> list[dict]:
        """Which switching functions are currently closed."""
        fields = self.fields("SPS")
        return [
            {"setpoint": index + 1, "closed": code == "1"}
            for index, code in enumerate(fields)
        ]

    def setpoint(self, number: int) -> dict:
        """Read one switching function's thresholds, and its channel if any.

        Two reply shapes exist in this family:

            "channel,lower,upper"   models that associate each switching
                                    function with a measurement channel
            "lower,upper"           the real TPG 252 A (firmware BG509727-C),
                                    where the association is fixed

        Assuming the three-field form is what broke this against the real
        instrument: `int("1.00E-11")` raises, and the whole switching-function
        panel silently disappeared. `channel` is None on the shorter form.
        """
        fields = self.fields(f"SP{number}")
        if len(fields) >= 3:
            return {"setpoint": number, "channel": int(fields[0]),
                    "lower": float(fields[1]), "upper": float(fields[2])}
        if len(fields) == 2:
            return {"setpoint": number, "channel": None,
                    "lower": float(fields[0]), "upper": float(fields[1])}
        raise protocol.ProtocolError(
            f"SP{number} answered {len(fields)} field(s): {fields!r}"
        )

    def setpoint_count(self) -> int:
        """How many switching functions this model implements.

        The family allows four; the real TPG 252 A refuses SP3 and SP4.
        """
        count = 0
        for number in range(1, 5):
            try:
                self.setpoint(number)
            except (protocol.TpgError, ValueError, IndexError):
                break
            count += 1
        return count

    def set_setpoint(self, number: int, lower: float, upper: float,
                     channel: int | None = None) -> dict:
        """Write one switching function, matching the model's reply shape.

        `channel` is only sent when this model uses it; passing it to a unit
        that does not expect it shifts every parameter by one position.
        """
        if lower >= upper:
            raise ValueError(
                f"lower threshold {lower:g} must be below upper {upper:g}"
            )
        if channel is None:
            channel = self.setpoint(number).get("channel")
        if channel is None:
            self.exchange(f"SP{number}", lower, upper)
        else:
            self.exchange(f"SP{number}", channel, lower, upper)
        return self.setpoint(number)

    # --- housekeeping ----------------------------------------------------

    def error_status(self) -> dict:
        word = self.fields("ERR")[0]
        faults = protocol.decode_error(word)
        return {"word": word, "ok": not faults, "faults": faults}

    def keylock(self, locked: bool) -> str:
        return self.exchange("LOC", 1 if locked else 0)

    def reset(self) -> list[str]:
        """Clear the latched error list, returning what was latched."""
        return protocol.split_data(self.exchange("RES"))

    # --- capability probe -------------------------------------------------

    def probe(self) -> dict:
        """Ask the unit which mnemonics it implements.

        Sends only the bare query form of everything in `SAFE_TO_PROBE`, so it
        changes nothing. This is the gauge's equivalent of `tc600ctl.py
        verify`, and its output should be trusted over the table in
        commands.py.
        """
        supported, refused, silent = {}, [], []
        for mnemonic in commands.SAFE_TO_PROBE:
            try:
                supported[mnemonic] = self.exchange(mnemonic)
            except protocol.NakError:
                refused.append(mnemonic)
            except protocol.TpgError:
                silent.append(mnemonic)
        return {
            "baudrate": self.baudrate,
            "supported": supported,
            "refused": refused,
            "silent": silent,
        }


def autodetect(
    port: str,
    rates=BAUD_RATES,
    timeout: float = 0.6,
    channels: int = 2,
) -> "TPG | None":
    """Open `port` at whichever rate the unit answers on, or return None.

    `PNR` is the discriminator: every model in the family implements it, and
    it changes nothing.
    """
    for rate in rates:
        unit = None
        try:
            unit = TPG(port, baudrate=rate, timeout=timeout,
                       retries=1, channels=channels)
            unit.firmware()
            return unit
        except (protocol.TpgError, serial.SerialException, OSError):
            if unit is not None:
                unit.close()
    return None
