"""TPG gauge-controller service: pressure sampling and gauge control.

Everything downstream stores millibar. The controller's own display unit is
read once and cached, and each reading is converted, so a run recorded while
the front panel was set to Torr still plots against one recorded in mbar.

One interlock lives here: striking a cold-cathode gauge at too high a pressure
will not ignite a stable discharge and sputters the electrodes, so switching
one on is refused when the rough gauge says the chamber is still near
atmosphere. The controller's own auto-on logic normally prevents this, but the
serial `SEN` command bypasses the front panel, so the guard belongs on this
side of the link too.
"""

from __future__ import annotations

import serial

import tpg
from tpg import commands as tpg_commands
from tpg import protocol as tpg_protocol

from .registry import DeviceService

#: Cold-cathode and FullRange gauges must not be struck above this pressure.
#: 1e-2 mbar is the conventional ceiling for a Penning discharge.
COLD_CATHODE_LIMIT_MBAR = 1.0e-2

#: Gauge identifiers whose discharge is pressure-sensitive in that way.
COLD_CATHODE_PREFIXES = ("IKR", "PKR", "PBR", "IMR")


class GaugeInterlock(Exception):
    """Raised when a gauge command is refused because of the process state."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class GaugeService(DeviceService):
    """A TPG 25x / 26x gauge controller."""

    kind = "tpg"
    label = "TPG gauge controller"
    interval = 1.0          # cheap to read, and the curve wants resolution

    def __init__(self, key: str, port: str, baudrate: int | None = None,
                 channels: int = 2, timeout: float = 0.8, **options):
        self.baudrate = baudrate
        self.channels = channels
        self.timeout = timeout
        super().__init__(key, port, baudrate=baudrate, channels=channels,
                         **options)

    # --- link -------------------------------------------------------------

    def _open(self):
        if self.baudrate:
            return tpg.TPG(self.port, baudrate=self.baudrate,
                           timeout=self.timeout, retries=1,
                           channels=self.channels)
        # No rate configured: hunt for it once, then remember what worked so a
        # reconnect does not repeat the sweep.
        unit = tpg.autodetect(self.port, timeout=self.timeout,
                              channels=self.channels)
        if unit is None:
            raise serial.SerialException(
                f"nothing answered on {self.port} at any supported baud rate"
            )
        self.baudrate = unit.baudrate
        return unit

    def rebind(self, port: str, **options) -> dict:
        # A different port may be a different unit, or the same unit set to a
        # different rate. Forget the learned one unless the caller names it,
        # so `_open` hunts again instead of insisting on the old answer.
        if options.get("baudrate") is None:
            self.baudrate = None
        return super().rebind(port, **options)

    def offline(self, message: str) -> dict:
        snap = super().offline(message)
        snap.update({"readings": [], "sensors": [], "unit": None})
        return snap

    # --- sampling ---------------------------------------------------------

    def _read(self) -> dict:
        with self.lock:
            try:
                unit = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(f"cannot open {self.port}: {exc}")

            try:
                readings = unit.pressures()
                self.note_success()
            except tpg_protocol.TpgError as exc:
                self.note_failure()
                return self.offline(str(exc))
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(str(exc))

            if not self._identity:
                try:
                    self._identity = unit.identify()
                except tpg_protocol.TpgError:
                    self._identity = {}

            extras: dict = {}
            for name, reader in (("sensors", unit.sensor_state),
                                 ("setpoints", unit.setpoint_status)):
                try:
                    extras[name] = reader()
                except (tpg_protocol.TpgError, ValueError, IndexError):
                    extras[name] = []

            return self.online(
                readings=readings,
                unit=self._identity.get("unit"),
                baudrate=self.baudrate,
                **extras,
            )

    def sample_fields(self, snap: dict) -> dict:
        """Contribute p1/p2 in millibar plus their status codes."""
        fields: dict = {}
        for reading in snap.get("readings", []):
            index = reading.get("channel")
            if index not in (1, 2):
                continue
            fields[f"p{index}"] = reading.get("mbar")
            fields[f"p{index}_status"] = reading.get("status")
        return fields

    # --- helpers ----------------------------------------------------------

    def best_pressure(self, snap: dict | None = None) -> float | None:
        """The most trustworthy pressure reading available, in millibar.

        Where both channels are valid the lower one wins: a rough gauge at its
        underrange floor and a high-vacuum gauge reading properly disagree, and
        the high-vacuum gauge is the one telling the truth.
        """
        snap = snap or self.snapshot()
        values = [r["mbar"] for r in snap.get("readings", [])
                  if r.get("valid") and isinstance(r.get("mbar"), (int, float))]
        return min(values) if values else None

    def _gauge_identifier(self, channel: int) -> str:
        for entry in self._identity.get("gauges", []):
            if entry.get("channel") == channel:
                return str(entry.get("identifier", ""))
        return ""

    def _check_sensor_on(self, channel: int, snap: dict) -> None:
        identifier = self._gauge_identifier(channel).upper()
        if not any(identifier.startswith(p) for p in COLD_CATHODE_PREFIXES):
            return
        pressure = self.best_pressure(snap)
        if pressure is None:
            raise GaugeInterlock(
                f"channel {channel} is a cold-cathode gauge and no other "
                "channel is reading, so the chamber pressure is unknown. "
                "Switch it on from the front panel if you are sure.")
        if pressure > COLD_CATHODE_LIMIT_MBAR:
            raise GaugeInterlock(
                f"the chamber is at {pressure:.2E} mbar. A cold-cathode gauge "
                f"must not be struck above {COLD_CATHODE_LIMIT_MBAR:.0E} mbar "
                "-- the discharge will not stabilise and the electrodes "
                "sputter.")

    # --- commands ---------------------------------------------------------

    def command(self, action: str, value=None, override: bool = False) -> dict:
        payload = value if isinstance(value, dict) else {"value": value}

        with self.lock:
            try:
                unit = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}

            try:
                if action == "unit":
                    code = int(payload.get("value"))
                    result = unit.set_unit(code)
                    self._identity = unit.identify()

                elif action in ("sensor_on", "sensor_off"):
                    channel = int(payload.get("channel", 1))
                    turning_on = action == "sensor_on"
                    if turning_on and not override:
                        self._check_sensor_on(channel, self.snapshot())
                    result = unit.set_sensor(channel, turning_on)

                elif action in ("degas_on", "degas_off"):
                    channel = int(payload.get("channel", 1))
                    result = unit.degas(channel, action == "degas_on")

                elif action == "filter":
                    result = unit.set_filter(int(payload.get("channel", 1)),
                                             int(payload.get("value", 1)))

                elif action == "setpoint":
                    channel = payload.get("channel")
                    result = unit.set_setpoint(
                        int(payload.get("setpoint", 1)),
                        float(payload.get("lower")),
                        float(payload.get("upper")),
                        channel=int(channel) if channel is not None else None,
                    )

                elif action == "keylock":
                    result = unit.keylock(bool(payload.get("value")))

                elif action == "reset_errors":
                    result = unit.reset()

                elif action == "probe":
                    result = unit.probe()

                else:
                    return {"ok": False, "error": f"unknown action {action!r}"}

            except GaugeInterlock as stop:
                return {"ok": False, "error": stop.reason, "interlock": True}
            except tpg_protocol.NakError as exc:
                return {"ok": False, "error": f"{exc}. The unit may be "
                                              "keylocked, or the model may not "
                                              "implement this command."}
            except tpg_protocol.TpgError as exc:
                self.note_failure()
                return {"ok": False, "error": str(exc)}
            except (TypeError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": str(exc)}

        self.note_success()
        return {"ok": True, "action": action, "result": result,
                "overridden": bool(override)}

    def details(self) -> dict:
        """Everything the gauge page needs but the fast poll does not carry."""
        with self.lock:
            try:
                unit = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}

            out: dict = {"ok": True, "units": tpg_commands.UNITS,
                         "filters": tpg_commands.FILTER}
            for name, reader in (
                ("errors", unit.error_status),
                ("filter", unit.filter_state),
                ("sensors", unit.sensor_state),
                ("gauges", unit.gauge_types),
            ):
                try:
                    out[name] = reader()
                except (tpg_protocol.TpgError, ValueError, IndexError) as exc:
                    out[name] = {"error": str(exc)}
                except (serial.SerialException, OSError) as exc:
                    self.drop()
                    return {"ok": False, "error": str(exc)}

            setpoints = []
            for number in range(1, 5):
                try:
                    setpoints.append(unit.setpoint(number))
                except (tpg_protocol.TpgError, ValueError, IndexError):
                    break
            out["setpoints"] = setpoints

            # Which optional commands this model actually has, so the UI can
            # hide controls the instrument would only refuse. The real
            # TPG 252 A has no degas.
            supported = {}
            for name, mnemonic in (("degas", "DGS"), ("correction", "COR")):
                try:
                    unit.exchange(mnemonic)
                    supported[name] = True
                except tpg_protocol.NakError:
                    supported[name] = False
                except (tpg_protocol.TpgError, serial.SerialException, OSError):
                    supported[name] = None      # unknown, not "absent"
            out["supports"] = supported
            return out
