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

import threading
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
#: it, it cannot -- it draws its current limit, heats up and trips.
#:
#: 8e-1 mbar is the crossover this rig is operated at, set 2026-09-03
#: (previously 5e-1, set 2026-09-01). It is the one number to change if that
#: operating decision changes: the recipes' crossover field defaults to it,
#: no recipe may cross over above it, and the panel quotes it in every
#: refusal, so nothing else needs editing.
TURBO_START_MAX_MBAR = 8.0e-1

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

#: What the pump on this rig is rated for, so the panel can draw its limits
#: beside what it measures. Not read from the drive -- the firmware has no
#: parameter for any of these -- but from the operating instructions in this
#: repository, `Documentation/pm0504be_m.pdf` (PM 0504 BE), for the
#: TMH/TMU 071 P that the TC 600 drives:
#:
#:   * 24 VDC +-5 %                                  section 9, "Operating voltage"
#:   * start-up time 2.5 min                         section 9
#:   * switchpoint 80 % of nominal speed             section 4.4, "Switching outputs"
#:   * gas-mode-0 power line, 60 W at 1500 Hz rising
#:     to 80 W at 1300 Hz (vertices C and D)          section 9 and 4.4
#:   * 4.1 A continuous, 4.6 A maximum               section 9
#:   * 0.58 mbar l/s N2 throughput at nominal
#:     speed, air-cooled                             section 9, note 3
#:
#: The power line is the one that explains the runs of 2026-10-08: above it
#: the drive lowers the rotor speed until gas friction and its permitted
#: power balance, and below the 80 % switchpoint it eventually trips with a
#: run-up error (Err006) or over-temperature. Argon is a heavy gas, so on
#: this rig it is the gas-mode-0 line that applies. If the pump is ever
#: replaced, this is the table to re-read from that pump's manual.
RATINGS = {
    "model": "TMH/TMU 071 P",
    "source": "PM 0504 BE, section 9",
    "supply_v": 24.0,
    "supply_tolerance": 0.05,
    "startup_s": 150.0,
    "switchpoint_fraction": 0.80,
    "power_line_full_w": 60.0,
    "power_line_reduced_w": 80.0,
    "power_line_reduced_hz": 1300.0,
    "current_continuous_a": 4.1,
    "current_max_a": 4.6,
    "throughput_air_mbar_l_s": 0.58,
}


#: Drive error codes: what each means, what usually causes it on a rig like
#: this one, and what to do. Err006 is the one this rig has actually seen
#: and is pinned down by this repository's own manual: PM 0504 BE gives the
#: factory settings (run-up time 15 min, switchpoint 80 %) and lists "pump
#: does not attain nominal speed within the run-up time / cuts out during
#: operation" with these causes. The rest follow Pfeiffer's error list as
#: published for its TC drives; the TC 600's own list is in PM 0547 BN, which
#: is not in this repository -- check a rarely seen code there before acting
#: on the wording below.
ERROR_CODES = {
    "Err001": ("Excess rotation speed",
               "The rotor ran above its permitted speed.",
               "Do not restart. Have the pump and drive checked by Pfeiffer service."),
    "Err002": ("Overvoltage",
               "The supply voltage to the drive was too high.",
               "Check the 24 V power supply and its setting."),
    "Err006": ("Run-up time error",
               "The rotor was below the 80 % switchpoint (1200 Hz) once the run-up "
               "time had run out -- 15 min from motor start, factory setting. On this "
               "rig: too much gas, which drags the rotor down; also a backing "
               "pressure too high, a leak, a sluggish bearing, or overheating.",
               "Stop the gas or close the leak, check the backing pump, then "
               "acknowledge and restart the turbopump through the crossover "
               "interlock."),
    "Err007": ("Operating fluid low",
               "The oil-lubricated fore-vacuum bearing is short of lubricant.",
               "Do not restart. Check the lubricant reservoir (PM 0504 BE, section 7)."),
    "Err008": ("Pump connection faulty",
               "The drive cannot see the pump: cable or plug between them.",
               "Power off, check the TC 600 seated on the pump, power on."),
    "Err021": ("Pump not recognised",
               "The drive does not identify the pump connected to it.",
               "Check the pump type and parameter 777 (nominal speed, 1500 Hz)."),
    "Err044": ("Electronics over temperature",
               "The drive electronics overheated.",
               "Check the fan and the airflow round the TC 600, and the room "
               "temperature (5-40 °C with air cooling)."),
    "Err045": ("Motor over temperature",
               "The pump motor overheated -- sustained high gas load or poor cooling.",
               "Let it cool with the backing pump running; reduce the gas load; "
               "check the fan."),
}


def describe_error(code) -> dict:
    """What a drive error code means, for the panel and the run log."""
    text = str(code or "").strip()
    if text in ("", "000000", "000", "0"):
        return {"code": None}
    title, cause, remedy = ERROR_CODES.get(
        text, ("Drive fault", "See the TC 600 error list (PM 0547 BN).",
               "Clear the cause, then acknowledge the fault."))
    return {"code": text, "title": title, "cause": cause, "remedy": remedy,
            "known": text in ERROR_CODES}


#: Parameters read on demand for diagnosis: the drive's own protective
#: settings and its error history. Not in the fast poll -- they change
#: rarely and cost a round trip each.
DIAGNOSTIC_VIEW = (
    "RUTimeCtrl", "RUTimeSVal", "SpdSwPt", "GasMode", "OpModeBKP",
    "VentMode", "EnableVent", "VentSpd", "OpModeRemote", "CtrlViaInt",
    "PwrSVal", "PumpCycles", "OpHrsElec",
    "ErrHist1", "ErrHist2", "ErrHist3", "ErrHist4", "ErrHist5",
    "ErrHist6", "ErrHist7", "ErrHist8", "ErrHist9", "ErrHist10",
)


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
    probe_kind = "tc600"

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
        #: When each action was last sent successfully, monotonic seconds. The
        #: supervisor uses it to tell a station-off this panel asked for from
        #: one that came from the drive, its front panel or its remote plug.
        self.commanded: dict[str, float] = {}
        self._secure_lock = threading.Lock()
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

            # Standby (P2) is read too: the panel's standby button and the
            # "held down by gas" warning both need to know whether a rotor
            # below full speed was asked to be there.
            setpoints = {}
            for name in ("SpdSVal", "StdbySVal", "OpModeTMP",
                         "PumpgStatn", "MotorPump", "Standby"):
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

        def flag(name):
            value = status.get(name)
            return (1 if value else 0) if isinstance(value, bool) else None

        # `tms_temp` is stored exactly as the drive reports it, including the
        # 0 it reads while the TMS heater is off. That 0 is not a temperature
        # -- firmware 017500 has no other sensor (P326, P330, P342 and P346
        # all answer NO_DEF) -- but the record keeps the instrument's word,
        # and the bake-out's cooling phase is written against it. The panel
        # is what reads 0 as "not measured", and says so.
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
            "ov_temp_pump": flag("OvTempPump"),
            "ov_temp_elec": flag("OvTempElec"),
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
        #
        # There is deliberately no "but it is already spinning" shortcut here.
        # An earlier version allowed the start whenever `speed > 0`, on the
        # cosmetic grounds that a running pump should not be shown as being
        # in breach of the rule. That is how run 13 (2026-09-02) came to spin
        # the rotor up to 1444 Hz against atmosphere: starting the backing
        # pump makes gas rush through the turbopump, which windmills the
        # rotor to a few Hz, and the shortcut read that as "already running"
        # and waved the command through while the gauge was overrange and the
        # pressure therefore unknown. A turning rotor is not evidence about
        # the foreline. Only the gauge is.
        #
        if motor:
            # The motor enable is already set, so the command cannot change
            # anything. This is the one genuine no-op, and it is keyed on the
            # drive's own setpoint -- never on observed rotation.
            allow("motor_on")
        elif not station:
            block("motor_on", "the backing pump is not running. Start the "
                              "pumping station first and rough the chamber "
                              f"down to {limit:.0E} mbar.")
        elif chamber["mbar"] is None:
            spinning = (f" The rotor is turning at {speed:.0f} Hz, but that is "
                        f"gas flow through a pump that is not driven, not "
                        f"evidence that it is safe to drive it."
                        if speed > 0 else "")
            block("motor_on",
                  f"the chamber pressure is unknown: {chamber['reason']}. The "
                  f"turbopump must not be started above {limit:.0E} mbar, so "
                  f"this cannot be allowed without a reading.{spinning}")
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
        snap["ratings"] = RATINGS
        # What the current error code means, so every page can say it in
        # words rather than leave "Err006" to be looked up.
        snap["fault"] = describe_error(snap.get("status", {}).get("ErrorCode"))
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
        self.commanded[action] = time.monotonic()
        return {"ok": True, "action": action, "result": result,
                "overridden": bool(override)}

    # --- the safe state after a fault ---------------------------------------

    def secure(self, reason: str, stop_motor: bool = True,
               keep_backing: bool = True, settle_s: float = 8.0) -> dict:
        """Put the pump in its safe state after a fault, without losing vacuum.

        The safe state for a turbopump in trouble is: motor off, rotor
        coasting with its backing pump still running, fault cleared. Three
        steps, in this order on purpose:

        1. **Motor off** -- first, so that clearing the fault cannot restart
           the rotor into whatever tripped it. The drive's MotorPump setpoint
           would otherwise still say "on", and acknowledging Err006 with the
           gas still flowing just trips it again.
        2. **Acknowledge the fault.** A latched fault holds the station in
           its fault state; clearing it is what lets the forevacuum pump run.
        3. **Station on.** The pumping station is what runs the forevacuum
           pump. Switched off, it leaves a coasting rotor without backing and
           lets the chamber leak back up through the stopped pumps, so after
           a fault it is kept on, never switched off. (This rig has no vent
           valve -- EnableVent off, confirmed 2026-10-09 -- so a station-off
           does not vent it; with one, the factory setting would.)

        Every step is a "safe direction" command that the interlocks never
        refuse. Returns what was done, so the caller can log it and the panel
        can say it.
        """
        if not self._secure_lock.acquire(blocking=False):
            return {"ok": False, "error": "already securing the pump",
                    "actions": []}
        try:
            actions: list[dict] = []
            snap = self.snapshot()
            if not snap.get("connected"):
                return {"ok": False, "actions": actions,
                        "error": "no contact with the pump, so nothing can be "
                                 "done over RS-485; the drive keeps doing "
                                 "what it was last told"}
            setpoints = snap.get("setpoints", {})
            code = str(snap.get("status", {}).get("ErrorCode") or "").strip()
            faulted = code not in ("", "000000", "000", "0")

            def step(label, action):
                result = self.command(action)
                actions.append({"step": label, "ok": bool(result.get("ok")),
                                "error": result.get("error")})
                return result.get("ok")

            if stop_motor and setpoints.get("MotorPump") is True:
                step("turbopump motor switched off", "motor_off")
            if faulted:
                step(f"fault {code} acknowledged", "ack")
            if keep_backing and setpoints.get("PumpgStatn") is not True:
                step("pumping station (forevacuum pump) switched on", "on")

            # Check it took: the cached snapshot lags a poll behind.
            deadline = time.monotonic() + settle_s
            state = {}
            while time.monotonic() < deadline:
                time.sleep(0.5)
                latest = self.snapshot()
                if not latest.get("connected"):
                    continue
                now_code = str(latest["status"].get("ErrorCode") or "").strip()
                state = {
                    "fault": None if now_code in ("", "000000", "000", "0")
                    else now_code,
                    "station": latest["setpoints"].get("PumpgStatn") is True,
                    "motor": latest["setpoints"].get("MotorPump") is True,
                    "speed": latest["status"].get("ActualSpd"),
                }
                if (not state["fault"] and (state["station"] or not keep_backing)
                        and (not state["motor"] or not stop_motor)):
                    break
            # Say so even when nothing needed switching: "the forevacuum
            # pump is running" is the line an operator looks for after a fault.
            if keep_backing and state and not any(
                    a["step"].startswith("pumping station") for a in actions):
                actions.append({"step": "forevacuum pump confirmed running"
                                if state.get("station") else
                                "forevacuum pump NOT running",
                                "ok": bool(state.get("station")), "error": None})
            secured = bool(state) and not state.get("fault") and \
                (state.get("station") or not keep_backing)
            return {"ok": secured, "actions": actions, "state": state,
                    "reason": reason,
                    "error": None if secured else
                    ("the fault is still latched" if state.get("fault")
                     else "the station did not come back on" if state
                     else "no fresh reading from the pump to confirm it")}
        finally:
            self._secure_lock.release()

    def diagnostics(self) -> dict:
        """The drive's protective settings and its fault history, on demand.

        Run-up time monitoring (P4) and its limit (P700) are what decide
        when a dragged rotor becomes Err006; the error history (P360-P369)
        says what happened before anybody looked. Anything this firmware
        does not implement answers NO_DEF and is listed as missing rather
        than guessed.
        """
        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}
            values, units, missing = {}, {}, []
            for name in DIAGNOSTIC_VIEW:
                parameter = params.BY_NAME[name.lower()]
                try:
                    values[name] = parameter.decode(pump.read_raw(parameter.number))
                    units[name] = parameter.unit
                except protocol.ProtocolError:
                    missing.append(name)
                except (serial.SerialException, OSError) as exc:
                    self.drop()
                    return {"ok": False, "error": str(exc)}
        history = []
        for index in range(1, 11):
            code = values.get(f"ErrHist{index}")
            if code is not None and str(code).strip() not in ("", "000000", "000"):
                history.append({"position": index, **describe_error(code)})
        return {"ok": True, "values": values, "units": units,
                "missing": missing, "history": history,
                "codes": {code: describe_error(code) for code in ERROR_CODES}}

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
