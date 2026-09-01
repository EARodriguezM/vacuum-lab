"""TC 600 turbopump service: sampling, accessories, and process interlocks.

The interlocks refuse commands that would sequence the backing pump and the
turbopump in a damaging order. They can be overridden deliberately with
`override=True`, because a safety rule with no escape hatch is its own hazard
once something has already gone wrong -- but every override is logged.

The turbopump gate is a **pressure**, not a timer. A turbopump started
against a chamber that has not been roughed out cannot develop compression:
it draws its current limit and trips on over-temperature, and repeating that
wears the bearings. How long the backing pump has been running does not
answer the question -- a leaking chamber can be roughed for an hour and still
sit at 10 mbar, while a clean small chamber is ready in seconds. The gauge
answers it, so `TURBO_START_MAX_MBAR` is what this service enforces.

That gate needs a pressure, so a gauge must be attached with
`attach_pressure`. With no gauge, no *fresh* reading, or no valid channel,
the turbopump start is refused rather than allowed: a start that might be
into atmosphere is the case the rule exists for. The override is still there
for an operator who can see the chamber is roughed.

The stop dwell is still a timer, measured from continuous observation by the
sampler thread rather than from whenever a browser last polled -- which is
the whole reason this service owns the port instead of opening it per
request.
"""

from __future__ import annotations

import time

import serial

from tc600 import device, params, protocol

from .registry import DeviceService

#: Actions that physically move the pump. The UI confirms them and the API
#: refuses them unless the caller repeats that confirmation.
GUARDED = {"on", "off", "motor_on", "motor_off", "standby_on", "standby_off",
           "speed", "param"}

#: The accessory connectors, and the parameters behind them.
ACCESSORY_VIEW = (
    "Heating", "HeatType", "TMSActTemp", "TMSSteady", "TMSMaxTemp",
    "TMSHeatSVal", "CfgOut4",
    "EnableVent", "VentMode", "VentSpd", "VentTime",
    "OpModeBKP", "SwOnBKP", "SwOffBKP",
)

#: Parameters the panel may write. Deliberately narrower than "everything
#: writable": the interface address, parameter-set selection and the service
#: line are not things to expose behind a web button.
ALLOWED_WRITES = {
    "Heating", "TMSHeatSVal", "CfgOut4",
    "EnableVent", "VentMode", "VentSpd", "VentTime",
    "OpModeBKP", "SwOnBKP", "SwOffBKP",
    # OpModeTMP selects final-rotation-speed operation (0) versus the speed
    # setting mode (1). A deposition run writes 0 to hold the rotor flat out;
    # GasMode 0 tells the drive unit it is pumping argon, a heavy gas.
    "OpModeTMP", "GasMode", "RUTimeCtrl", "EnableBrake",
}

# --- interlock tuning -----------------------------------------------------

#: The turbopump may not be started above this chamber pressure.
#: Below roughly this pressure the foreline is in viscous-to-transitional
#: flow and the turbopump can develop compression as it accelerates; above
#: it, it cannot -- it draws its current limit, heats up and trips. 2e-1 mbar
#: is the crossover this rig is operated at.
TURBO_START_MAX_MBAR = 2.0e-1

#: A pressure reading older than this is not evidence about the chamber.
#: A sampler thread can wedge with a plausible number in its cache, and a
#: stale 1e-3 mbar would authorise exactly the start the rule forbids.
PRESSURE_MAX_AGE_S = 15.0

#: After the rotor reaches zero, wait this long before the station may stop.
#: A coasting rotor still needs backing, and "0 Hz" from the drive is also the
#: point where a slow final coast is easiest to misread.
STOP_DWELL_SECONDS = 20.0

#: Venting is refused above this fraction of nominal speed.
VENT_SPEED_LIMIT = 0.20


class Interlock(Exception):
    """Raised when a command is refused because of process safety."""

    def __init__(self, reason: str, wait: float = 0.0):
        super().__init__(reason)
        self.reason = reason
        self.wait = max(0.0, wait)


class PumpService(DeviceService):
    """The TC 600 drive unit."""

    kind = "tc600"
    label = "TC 600 turbopump"
    interval = 2.0

    def __init__(self, key: str, port: str, address: int = 1,
                 timeout: float = 0.6, **options):
        self.address = address
        self.timeout = timeout
        #: Interlock timers in time.monotonic() terms. None means "not yet".
        self._rotor_zero_since: float | None = None
        #: Where the turbopump-start interlock reads chamber pressure from.
        #: None until a gauge is attached, and then the start is refused.
        self._pressure_source = None
        #: Set by the run engine while a recipe needs the rotor flat out.
        #: Sputtering in argon throttles with the gate valve, not the rotor:
        #: dropping the speed to hold pressure costs compression exactly when
        #: the gas load is highest.
        self._full_speed_reason: str | None = None
        super().__init__(key, port, address=address, **options)

    # --- where the chamber pressure comes from -----------------------------

    def attach_pressure(self, source) -> None:
        """Give the interlock a gauge to read the chamber pressure from.

        `source` is anything with `snapshot()` and `best_pressure(snap)` --
        in practice the `GaugeService`, but duck-typed so a test or a second
        gauge can stand in without this module importing that one.
        """
        self._pressure_source = source

    def chamber_pressure(self) -> dict:
        """What this service believes the chamber pressure is, in millibar.

        Returns `{"mbar": float|None, "age": float|None, "reason": str|None}`.
        `reason` is filled in only when there is no usable number, and says
        why -- the interlock quotes it, because "the turbopump start is held"
        is not actionable but "no contact with the gauge" is.
        """
        source = self._pressure_source
        if source is None:
            return {"mbar": None, "age": None,
                    "reason": "no gauge is attached to this panel"}
        try:
            snap = source.snapshot()
            if not snap.get("connected"):
                return {"mbar": None, "age": None,
                        "reason": f"no contact with the gauge "
                                  f"({snap.get('error') or 'offline'})"}
            age = snap.get("age")
            if isinstance(age, (int, float)) and age > PRESSURE_MAX_AGE_S:
                return {"mbar": None, "age": age,
                        "reason": f"the gauge reading is {age:.0f} s old"}
            value = source.best_pressure(snap)
        except Exception as exc:                  # a wedged gauge is "unknown"
            return {"mbar": None, "age": None,
                    "reason": f"the gauge could not be read ({exc})"}
        if not isinstance(value, (int, float)):
            return {"mbar": None, "age": age,
                    "reason": "no gauge channel is reading a valid pressure"}
        return {"mbar": float(value), "age": age, "reason": None}

    # --- full-speed lock --------------------------------------------------

    def lock_full_speed(self, reason: str) -> None:
        self._full_speed_reason = reason

    def unlock_full_speed(self) -> None:
        self._full_speed_reason = None

    # --- link -------------------------------------------------------------

    def _open(self):
        return device.TC600(self.port, address=self.address,
                            timeout=self.timeout, retries=0)

    def offline(self, message: str) -> dict:
        snap = super().offline(message)
        snap.update({"address": self.address, "status": {}, "units": {},
                     "setpoints": {}})
        return snap

    # --- sampling ---------------------------------------------------------

    def _read(self) -> dict:
        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(f"cannot open {self.port}: {exc}")

            # One cheap read decides whether the pump is there at all.
            try:
                pump.read_raw(349)
                self.note_success()
            except protocol.ProtocolError as exc:
                self.note_failure()
                return self.offline(str(exc))
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(str(exc))

            if not self._identity:
                try:
                    self._identity = pump.identify()
                except protocol.ProtocolError:
                    self._identity = {}

            status, units = {}, {}
            for number in params.STATUS_VIEW:
                parameter = params.BY_NUMBER[number]
                try:
                    status[parameter.name] = parameter.decode(pump.read_raw(number))
                    units[parameter.name] = parameter.unit
                except protocol.ProtocolError:
                    continue
                except (serial.SerialException, OSError) as exc:
                    self.drop()
                    return self.offline(str(exc))

            setpoints = {}
            for name in ("SpdSVal", "StdbySVal", "OpModeTMP",
                         "PumpgStatn", "MotorPump"):
                try:
                    setpoints[name] = pump.read(name)
                except (protocol.ProtocolError, KeyError, ValueError):
                    pass

            # The bakeout procedure needs the TMS temperature every sample, so
            # it rides along with the status view rather than costing a
            # separate accessory read per cycle.
            for name in ("TMSActTemp", "TMSHeatSVal", "Heating"):
                try:
                    parameter = params.BY_NAME[name.lower()]
                    status[name] = parameter.decode(pump.read_raw(parameter.number))
                    units[name] = parameter.unit
                except (protocol.ProtocolError, KeyError):
                    continue
                except (serial.SerialException, OSError) as exc:
                    self.drop()
                    return self.offline(str(exc))

            return self.online(address=self.address, status=status,
                               units=units, setpoints=setpoints)

    def observe(self, snap: dict) -> None:
        """Maintain the stop dwell timer from continuous observation."""
        now = time.monotonic()
        if not snap.get("connected"):
            # Contact lost: forget the timer rather than let a stale one
            # authorise something once contact returns.
            self._rotor_zero_since = None
            return

        speed = snap["status"].get("ActualSpd")

        if isinstance(speed, (int, float)):
            if speed > 0:
                self._rotor_zero_since = None
            elif self._rotor_zero_since is None:
                self._rotor_zero_since = now

    def sample_fields(self, snap: dict) -> dict:
        status = snap.get("status", {})
        setpoints = snap.get("setpoints", {})

        def number(value):
            return value if isinstance(value, (int, float)) else None

        return {
            "speed": number(status.get("ActualSpd")),
            "set_speed": number(status.get("SetRotSpd")),
            "current": number(status.get("DrvCurrent")),
            "voltage": number(status.get("DrvVoltage")),
            "power": number(status.get("DrvPower")),
            "tms_temp": number(status.get("TMSActTemp")),
            "error_code": status.get("ErrorCode"),
            "station": 1 if setpoints.get("PumpgStatn") is True else 0,
            "motor": 1 if setpoints.get("MotorPump") is True else 0,
        }

    # --- interlocks -------------------------------------------------------

    def interlocks(self, snap: dict | None = None) -> dict:
        """Return {action: {allowed, reason, wait}} for the guarded actions."""
        if snap is None:
            snap = self.snapshot()
        now = time.monotonic()
        out: dict[str, dict] = {}

        def allow(action):
            out[action] = {"allowed": True, "reason": "", "wait": 0}

        def block(action, reason, wait=0.0):
            out[action] = {"allowed": False, "reason": reason,
                           "wait": int(round(wait))}

        if not snap.get("connected"):
            for action in ("on", "off", "motor_on", "motor_off"):
                block(action, "no contact with the pump")
            return out

        status, setpoints = snap["status"], snap["setpoints"]
        speed = status.get("ActualSpd")
        speed = speed if isinstance(speed, (int, float)) else 0
        station = setpoints.get("PumpgStatn") is True
        motor = setpoints.get("MotorPump") is True

        # --- stopping the backing pump -----------------------------------
        if speed > 0:
            block("off", f"the turbopump is still spinning at {speed} Hz. "
                         "It needs backing until it has fully stopped.")
        elif motor and station:
            block("off", "the turbopump motor is still enabled. Turn the "
                         "motor off first, then stop the station.")
        elif self._rotor_zero_since is not None:
            waited = now - self._rotor_zero_since
            if waited < STOP_DWELL_SECONDS:
                block("off", "waiting for the rotor to settle after reaching "
                             "0 Hz.", STOP_DWELL_SECONDS - waited)
            else:
                allow("off")
        else:
            allow("off")

        # --- starting the turbopump ---------------------------------------
        # The gate is the chamber pressure, not a stopwatch: the turbopump
        # needs a roughed foreline to develop compression against, and only
        # the gauge knows whether it has one.
        chamber = self.chamber_pressure()
        limit = TURBO_START_MAX_MBAR
        if motor or speed > 0:
            # Already running. Re-sending "on" changes nothing, and refusing
            # it would read as though the running pump were in breach.
            allow("motor_on")
        elif not station:
            block("motor_on", "the backing pump is not running. Start the "
                              "pumping station first and rough the chamber "
                              f"down to {limit:.0E} mbar.")
        elif chamber["mbar"] is None:
            block("motor_on",
                  f"the chamber pressure is unknown: {chamber['reason']}. The "
                  f"turbopump must not be started above {limit:.0E} mbar, so "
                  f"this cannot be allowed without a reading.")
        elif chamber["mbar"] > limit:
            block("motor_on",
                  f"the chamber is at {chamber['mbar']:.2E} mbar. Keep "
                  f"roughing until it reaches {limit:.0E} mbar -- a turbopump "
                  f"started against an un-roughed foreline cannot develop "
                  f"compression, draws its current limit and trips on "
                  f"over-temperature.")
        else:
            allow("motor_on")
        # Whether or not it is blocked, say what the rule is measuring, so the
        # panel can show the approach to crossover rather than just a refusal.
        out["motor_on"].update({"pressure_mbar": chamber["mbar"],
                                "limit_mbar": limit})

        # Starting the backing pump and stopping the turbopump are always the
        # safe direction, so they are never blocked.
        allow("on")
        allow("motor_off")

        # --- speed policy during a deposition -----------------------------
        if self._full_speed_reason:
            for action in ("speed", "standby_on"):
                block(action,
                      f"the rotor is locked at full speed: "
                      f"{self._full_speed_reason}. Throttle with the gate "
                      f"valve instead. Stop the run to release this.")
        else:
            allow("speed")
            allow("standby_on")
        return out

    def _check(self, action: str, snap: dict) -> None:
        rule = self.interlocks(snap).get(action)
        if rule and not rule["allowed"]:
            raise Interlock(rule["reason"], rule["wait"])

    def _check_param(self, name: str, value, snap: dict) -> None:
        """Interlocks that apply to accessory parameter writes."""
        status = snap.get("status", {})
        identity = snap.get("identity", {})
        speed = status.get("ActualSpd")
        speed = speed if isinstance(speed, (int, float)) else 0
        nominal = identity.get("NominalSpd") or 0
        limit = nominal * VENT_SPEED_LIMIT if nominal else 0

        venting_now = (name == "VentMode" and int(value) == 2) or \
                      (name == "EnableVent" and int(value) == 1)
        if venting_now and nominal and speed > limit:
            raise Interlock(
                f"the rotor is at {speed} Hz. Venting is only permitted below "
                f"{int(limit)} Hz -- letting air in at speed can wreck the rotor.")

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["interlocks"] = self.interlocks(snap)
        return snap

    # --- accessories ------------------------------------------------------

    def accessories(self) -> dict:
        """Read the accessory-connector parameters.

        Served on demand rather than in the status poll: they change rarely
        and reading them costs another fourteen round trips.
        """
        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}

            values, units, missing = {}, {}, []
            for name in ACCESSORY_VIEW:
                parameter = params.BY_NAME[name.lower()]
                try:
                    values[name] = parameter.decode(pump.read_raw(parameter.number))
                    units[name] = parameter.unit
                except protocol.ProtocolError:
                    missing.append(name)
                except (serial.SerialException, OSError) as exc:
                    self.drop()
                    return {"ok": False, "error": str(exc)}

            return {"ok": True, "values": values, "units": units,
                    "missing": missing, "writable": sorted(ALLOWED_WRITES)}

    # --- commands ---------------------------------------------------------

    def command(self, action: str, value=None, override: bool = False) -> dict:
        if action == "param":
            name, setting = (value or {}).get("name"), (value or {}).get("value")
            return self.write_param(str(name), setting, override)

        snap = self.snapshot()
        if not override:
            try:
                self._check(action, snap)
            except Interlock as stop:
                return {"ok": False, "error": stop.reason,
                        "interlock": True, "wait": stop.wait}

        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}
            try:
                if action == "on":
                    result = pump.pumping_station(True)
                elif action == "off":
                    result = pump.pumping_station(False)
                elif action == "motor_on":
                    result = pump.motor(True)
                elif action == "motor_off":
                    result = pump.motor(False)
                elif action == "standby_on":
                    result = pump.standby(True)
                elif action == "standby_off":
                    result = pump.standby(False)
                elif action == "ack":
                    result = pump.acknowledge_error()
                elif action == "speed":
                    result = pump.set_speed_percent(float(value))
                else:
                    return {"ok": False, "error": f"unknown action {action!r}"}
            except protocol.ProtocolError as exc:
                self.note_failure()
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": str(exc)}

        self.note_success()
        return {"ok": True, "action": action, "result": result,
                "overridden": bool(override)}

    def write_param(self, name: str, value, override: bool = False) -> dict:
        if name not in ALLOWED_WRITES:
            return {"ok": False, "error": f"{name} is not writable from here"}
        try:
            parameter = params.lookup(name)
        except KeyError as exc:
            return {"ok": False, "error": str(exc)}

        try:
            if parameter.datatype in (protocol.BooleanOld, protocol.BooleanNew):
                coerced = value.strip().lower() in ("1", "on", "true", "yes") \
                    if isinstance(value, str) else bool(value)
            elif parameter.datatype in (protocol.UInteger, protocol.UShortInt):
                coerced = int(value)
            else:
                coerced = float(value)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"bad value {value!r} for {name}"}

        if not override:
            try:
                self._check_param(name, coerced, self.snapshot())
            except Interlock as stop:
                return {"ok": False, "error": stop.reason,
                        "interlock": True, "wait": stop.wait}

        with self.lock:
            try:
                pump = self.driver()
                result = pump.write(parameter.number, coerced)
            except protocol.ProtocolError as exc:
                self.note_failure()
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": str(exc)}

        self.note_success()
        return {"ok": True, "action": "param", "name": name, "result": result,
                "overridden": bool(override)}
