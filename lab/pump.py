"""TC 600 turbopump service: sampling, accessories, and process interlocks.

The interlocks refuse commands that would sequence the backing pump and the
turbopump in a damaging order. Each refusal names its rule, and an admin can
override that one rule deliberately -- `override="pressure_unknown"`, never a
blanket `True` -- because a safety rule with no escape hatch is its own hazard
once something has already gone wrong. Every override is logged.

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

from .registry import DeviceService, waived_rules

#: Actions that physically move the pump. The UI confirms them and the API
#: refuses them unless the caller repeats that confirmation. Acknowledging a
#: fault is one of them: the drive runs the rotor whenever the station and
#: the motor are both on and no fault is latched, so clearing the fault is a
#: way of starting the rotor.
GUARDED = {"on", "off", "motor_on", "motor_off", "standby_on", "standby_off",
           "speed", "param", "ack"}

#: An error code that means "no fault".
NO_FAULT = ("", "000000", "000", "0")

#: Attempts for a command's telegrams beyond the first. The sampler can lose a
#: reading and take the next; a command lost to one dropped reply would leave
#: the operator -- or a safety sequence -- believing it was refused.
COMMAND_RETRIES = 2

#: The accessory connectors, and the parameters behind them.
ACCESSORY_VIEW = (
    "Heating", "HeatType", "TMSActTemp", "TMSSteady", "TMSMaxTemp",
    "TMSHeatSVal", "CfgOut4",
    "EnableVent", "VentMode", "VentSpd", "VentTime",
    "OpModeBKP", "SwOnBKP", "SwOffBKP",
)

#: Parameters an operator may write: the pump's heater, for a bake-out --
#: on or off, and its setpoint (the user's decision, 2026-10-11). Everything
#: else in ALLOWED_WRITES is one of the drive's own protections or its
#: configuration, and is an admin's.
OPERATOR_WRITES = {"Heating", "TMSHeatSVal"}

#: Parameters the panel may write at all, admins included. Deliberately
#: narrower than "everything writable": the interface address,
#: parameter-set selection and the service line are not things to expose
#: behind a web button.
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

#: A reading of the pump older than this is no basis for a decision that
#: depends on what the pump is doing -- at least; a slow bus allows more, see
#: `PumpService.stale_after`. A sampler that has stopped leaves a frozen "0 Hz"
#: that would let the station stop under a rotor that is still turning.
SNAPSHOT_MAX_AGE_S = 5.0
#: ...and never more than this, however slow the bus has been.
SNAPSHOT_MAX_AGE_CAP_S = 15.0

#: After the rotor reaches zero, wait this long before the station may stop.
#: A coasting rotor still needs backing, and "0 Hz" from the drive is also the
#: point where a slow final coast is easiest to misread.
STOP_DWELL_SECONDS = 20.0

#: The stop dwell survives this many readings in a row without a speed. A
#: reply lost here and there must not restart a 20 s wait for ever; a silence
#: long enough that nobody knows what the rotor did must. Counted in readings
#: rather than seconds, because a reading that lost a reply takes longer.
STOP_GAP_READINGS = 3

#: Venting is refused above this fraction of nominal speed.
VENT_SPEED_LIMIT = 0.20

#: What the pump on this rig is rated for, so the panel can draw its limits
#: beside what it measures. Not read from the drive -- the firmware has no
#: parameter for any of these -- but from the operating instructions in this
#: repository, `Documentation/pm0504be_m.pdf` (PM 0504 BE), for the
#: TMH/TMU 071 P that the TC 600 drives:
#:
#:   * 24 VDC +-5 %                                  section 9, "Operating voltage"
#:   * nominal rotation speed 1500 Hz (90 000 rpm)   section 9
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
    "nominal_hz": 1500.0,
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
    """Raised when a command is refused because of process safety.

    `rule` is the refusing rule's stable id: what an admin's override names.
    """

    def __init__(self, reason: str, wait: float = 0.0, rule: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.wait = max(0.0, wait)
        self.rule = rule


def refused(stop: Interlock) -> dict:
    """An interlock's refusal, as a command result."""
    return {"ok": False, "error": stop.reason, "interlock": True,
            "wait": stop.wait, "rule": stop.rule}


class PumpService(DeviceService):
    """The TC 600 drive unit."""

    kind = "tc600"
    label = "TC 600 turbopump"
    #: A period (see DeviceService.interval). The fast set below takes about
    #: this long on the real bus, so the pump is read nearly back to back:
    #: what the safety checks act on is never much more than a second old.
    interval = 0.5
    probe_kind = "tc600"

    #: What identifies the drive. Read until every field has answered, not
    #: once: a reply lost at connect used to leave NominalSpd out for good.
    IDENTITY = ("NominalSpd", "ElecName", "FwVersion", "HWVersion")
    #: A field the firmware refuses (NO_DEF) is asked for again this seldom.
    IDENTITY_RETRY_S = 60.0

    #: Read every cycle: what the interlocks, the supervisor and a run's
    #: safety checks decide on. Ten telegrams.
    FAST = (("status", "ActualSpd"), ("status", "ErrorCode"),
            ("status", "OvTempElec"), ("status", "OvTempPump"),
            ("status", "DrvPower"), ("status", "DrvCurrent"),
            ("status", "DrvVoltage"), ("status", "PumpAccel"),
            ("setpoints", "MotorPump"), ("setpoints", "PumpgStatn"))
    #: Read SLOW_PER_CYCLE of these each cycle, in turn: what changes rarely,
    #: or that no safety check waits on. Each keeps its last value until it
    #: is read again, for at most SLOW_KEEP_S -- then it is unknown.
    SLOW = (("status", "SetRotSpd"), ("setpoints", "OpModeTMP"),
            ("setpoints", "Standby"), ("status", "TMSActTemp"),
            ("status", "SetSpdAtt"), ("status", "SpdSwPtAtt"),
            ("setpoints", "SpdSVal"), ("setpoints", "StdbySVal"),
            ("status", "TMSHeatSVal"), ("status", "Heating"),
            ("status", "OpHrsPump"))
    SLOW_PER_CYCLE = 2
    SLOW_KEEP_S = 30.0

    def __init__(self, key: str, port: str, address: int = 1,
                 timeout: float = 0.6, settle: float = 0.03, **options):
        self.address = address
        self.timeout = timeout
        #: Pause after transmitting, for the converter to turn round. The
        #: HXSP-485 has run at 30 ms; less is worth trying on the real one.
        self.settle = settle
        #: Values of the SLOW fields: {(group, name): (value, monotonic)}.
        self._slow: dict[tuple[str, str], tuple[object, float]] = {}
        self._slow_next = 0
        #: Interlock timers in time.monotonic() terms. None means "not yet".
        self._rotor_zero_since: float | None = None
        #: Readings in a row without a speed, for the gap the dwell tolerates.
        self._speed_unread = 0
        #: Identity fields the firmware refused, and when to ask again.
        self._identity_retry: dict[str, float] = {}
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

    # --- full speed ---------------------------------------------------------

    def nominal_speed(self, snap: dict | None = None) -> float:
        """The rotor's full speed in Hz -- never unknown.

        The drive's own NominalSpd (P315) once it has answered it. Until
        then: the speed it is driving at, if it says it is in final-speed
        operation and not in standby, else the pump's rating. Every check
        that compares the rotor with full speed -- the rotor held down by
        gas, the venting limit, base pressure only at full speed -- used to
        switch itself off when this was unknown, which one lost reply at
        connect made it for a whole run. A wrong guess ends a run early; no
        guess lets a dragged rotor trip the drive.
        """
        return self._nominal(snap or self.snapshot())[0]

    def stale_after(self, snap: dict) -> float:
        """How old a reading of this pump may be and still decide anything.

        Two of the sampler's own cycles past its period, and a second -- a
        reading is replaced about one period and one cycle after it began,
        and a lost reply makes a cycle longer -- but never under
        SNAPSHOT_MAX_AGE_S or over SNAPSHOT_MAX_AGE_CAP_S. A fixed 5 s was
        reached routinely at the real bus's speed, and refused the station
        stop and the turbopump start at random.
        """
        cycle = (snap.get("cycle_ms") or 0) / 1000.0
        return min(SNAPSHOT_MAX_AGE_CAP_S,
                   max(SNAPSHOT_MAX_AGE_S, self.interval + 2 * cycle + 1.0))

    def _nominal(self, snap: dict) -> tuple[float, str]:
        value = snap.get("identity", {}).get("NominalSpd")
        if isinstance(value, (int, float)) and value > 0:
            return float(value), "drive"
        setpoints, status = snap.get("setpoints", {}), snap.get("status", {})
        target = status.get("SetRotSpd")
        if (setpoints.get("OpModeTMP") == 0 and setpoints.get("Standby") is False
                and isinstance(target, (int, float)) and target > 0):
            return float(target), "setpoint"
        return float(RATINGS["nominal_hz"]), "rating"

    # --- link -------------------------------------------------------------

    def _open(self):
        return device.TC600(self.port, address=self.address,
                            timeout=self.timeout, retries=0, settle=self.settle)

    def offline(self, message: str) -> dict:
        snap = super().offline(message)
        snap.update({"address": self.address, "status": {}, "units": {},
                     "setpoints": {}})
        return snap

    # --- sampling ---------------------------------------------------------

    def _read(self) -> dict:
        """One poll: the fast set, two of the slow set, and any identity
        field still missing.

        There is no separate "is it there?" read any more: the pump is there
        if anything answers. One lost reply leaves one field out -- unknown,
        never 0 or "off" -- instead of marking the whole pump offline.
        """
        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(f"cannot open {self.port}: {exc}")

            if not self._identity:
                self._slow.clear()      # a new link may be another drive
            fields = list(self.FAST)
            for _ in range(min(self.SLOW_PER_CYCLE, len(self.SLOW))):
                fields.append(self.SLOW[self._slow_next % len(self.SLOW)])
                self._slow_next += 1

            values: dict[tuple[str, str], object] = {}
            answered, quiet = 0, 0
            try:
                for field in fields:
                    try:
                        values[field] = pump.read(field[1])
                        answered += 1
                        quiet = 0
                    except protocol.DeviceError:
                        answered += 1            # an answer, if not a value
                        quiet = 0
                    except protocol.ProtocolError:
                        # Two unanswered in a row: the pump has gone quiet,
                        # maybe half way through this cycle. Waiting out
                        # every remaining field held the reading back for
                        # fifteen seconds; what was not read is unknown.
                        quiet += 1
                        if quiet >= 2:
                            break
                if answered:
                    self._complete_identity(pump)
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return self.offline(str(exc))

            if not answered:
                self.note_failure()
                return self.offline(f"no reply from the pump at address "
                                    f"{self.address}")
            self.note_success()

            now = time.monotonic()
            for field in self.SLOW:
                if field in values:
                    self._slow[field] = (values[field], now)
                elif field in self._slow and \
                        now - self._slow[field][1] <= self.SLOW_KEEP_S:
                    values[field] = self._slow[field][0]

            status, units, setpoints = {}, {}, {}
            for (group, name), value in values.items():
                if group == "status":
                    status[name] = value
                    units[name] = params.lookup(name).unit
                else:
                    setpoints[name] = value
            return self.online(address=self.address, status=status,
                               units=units, setpoints=setpoints)

    def _complete_identity(self, pump) -> None:
        """Ask for whatever identity field has not answered yet.

        Called every cycle; costs nothing once the identity is complete. A
        field the firmware refuses outright (NO_DEF) is asked for again only
        once a minute, so a drive without it is not polled for it for ever.
        """
        if not self._identity:
            self._identity_retry.clear()          # a new link: ask afresh
        now = time.monotonic()
        for name in self.IDENTITY:
            if name in self._identity or now < self._identity_retry.get(name, 0):
                continue
            try:
                self._identity[name] = pump.read(name)
            except protocol.DeviceError:
                self._identity_retry[name] = now + self.IDENTITY_RETRY_S
            except protocol.ProtocolError:
                # Lost: ask again next cycle -- and stop here, so a pump that
                # has gone quiet costs one timeout per cycle, not four.
                break

    def observe(self, snap: dict) -> None:
        """Maintain the stop dwell timer from continuous observation.

        Timed from the first 0 Hz reading; any reading of a turning rotor
        restarts it. A reading with no speed in it -- a reply lost on the
        bus, or no contact at all -- neither counts as 0 Hz nor, unless
        STOP_GAP_READINGS of them come in a row, restarts the wait. It used
        to count as 0 Hz: a lost reply in the middle of a coast-down read as
        a rotor at rest, and the power-down stopped the station under it.
        """
        now = time.monotonic()
        speed = snap.get("status", {}).get("ActualSpd") \
            if snap.get("connected") else None
        if isinstance(speed, (int, float)):
            self._speed_unread = 0
            if speed > 0:
                self._rotor_zero_since = None
            elif self._rotor_zero_since is None:
                self._rotor_zero_since = now
        else:
            self._speed_unread += 1
            if self._speed_unread >= STOP_GAP_READINGS:
                self._rotor_zero_since = None

    def sample_fields(self, snap: dict) -> dict:
        status = snap.get("status", {})
        setpoints = snap.get("setpoints", {})

        def number(value):
            return value if isinstance(value, (int, float)) else None

        def flag(name, source=status):
            value = source.get(name)
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
            # NULL when the reading did not come back, not 0: a lost reply
            # is not the drive saying "off", and a run that took it for one
            # reset its timers on it.
            "station": flag("PumpgStatn", setpoints),
            "motor": flag("MotorPump", setpoints),
            "ov_temp_pump": flag("OvTempPump"),
            "ov_temp_elec": flag("OvTempElec"),
        }

    # --- interlocks -------------------------------------------------------

    def interlocks(self, snap: dict | None = None,
                   waive=frozenset()) -> dict:
        """Return {action: {allowed, reason, wait, rule}} for the guarded actions.

        Each refusal names its `rule`, a stable id, so that an admin's
        override can waive that one rule and no other: an override used to
        waive every rule of the action, so waiving "the pressure is unknown"
        also waived "the backing pump is not running". Rules are tried in
        order and the first that applies refuses; a rule in `waive` is
        skipped, and the entry's `waived` lists the ones that would have
        refused.
        """
        if snap is None:
            snap = self.snapshot()
        now = time.monotonic()
        out: dict[str, dict] = {}

        def judge(action, rules):
            """rules: (id, applies, reason or a function making it, wait)."""
            waived = []
            for rule, applies, reason, wait in rules:
                if not applies:
                    continue
                if rule in waive:
                    waived.append(rule)
                    continue
                out[action] = {"allowed": False, "rule": rule,
                               "reason": reason() if callable(reason) else reason,
                               "wait": int(round(wait)), "waived": waived}
                return
            out[action] = {"allowed": True, "rule": None, "reason": "",
                           "wait": 0, "waived": waived}

        if not snap.get("connected"):
            for action in ("on", "off", "motor_on", "motor_off", "ack"):
                judge(action, [("no_contact", True,
                                "no contact with the pump", 0)])
            return out

        status, setpoints = snap.get("status", {}), snap.get("setpoints", {})
        # A field missing from the reading is a reply that did not come
        # back: unknown, which is never the same as 0 Hz or "off".
        speed = status.get("ActualSpd")
        speed = speed if isinstance(speed, (int, float)) else None
        station = setpoints.get("PumpgStatn")
        motor = setpoints.get("MotorPump")
        age = snap.get("age")
        # No age at all is not "fresh": every published reading has one.
        stale = not isinstance(age, (int, float)) or age > self.stale_after(snap)
        old = ("the last reading of the pump has no age, so what it is doing "
               "now is not known" if not isinstance(age, (int, float)) else
               f"the last reading of the pump is {age:.0f} s old, so what it "
               f"is doing now is not known" if stale else "")

        # --- stopping the backing pump -----------------------------------
        # 0 Hz, from a fresh reading: the dwell runs from the first such
        # reading. With no timer at all the wait is the whole of it.
        waited = (now - self._rotor_zero_since
                  if self._rotor_zero_since is not None else 0.0)
        judge("off", [
            ("stale_reading", stale,
             lambda: f"{old}. The station stays on until a fresh reading "
                     "shows the rotor at rest.", 0),
            ("speed_unknown", speed is None,
             "the rotor speed did not come back in the last reading, so "
             "whether the rotor is still turning is not known. The station "
             "stays on until a reading shows it at rest.", 0),
            ("rotor_turning", speed is not None and speed > 0,
             lambda: f"the turbopump is still spinning at {speed} Hz. It "
                     "needs backing until it has fully stopped.", 0),
            ("motor_enabled", motor is True and station is True,
             "the turbopump motor is still enabled. Turn the motor off "
             "first, then stop the station.", 0),
            ("stop_dwell", waited < STOP_DWELL_SECONDS,
             "waiting for the rotor to settle after reaching 0 Hz.",
             STOP_DWELL_SECONDS - waited),
        ])

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
        # The one genuine no-op: the motor enable is already set, so the
        # command cannot change anything. Keyed on the drive's own setpoint
        # -- never on observed rotation -- and checked against the drive
        # again when it is sent (see command()).
        starting = motor is not True
        spinning = (f" The rotor is turning at {speed:.0f} Hz, but that is gas "
                    f"flow through a pump that is not driven, not evidence "
                    f"that it is safe to drive it." if speed else "")
        judge("motor_on", [
            ("stale_reading", stale,
             lambda: f"{old}. Starting the turbopump waits for a fresh one.", 0),
            ("station_unknown", starting and station is None,
             "whether the pumping station is on did not come back in the "
             "last reading. The turbopump needs the backing pump running "
             "before it starts.", 0),
            ("backing_off", starting and station is False,
             f"the backing pump is not running. Start the pumping station "
             f"first and rough the chamber down to {limit:.0E} mbar.", 0),
            ("pressure_unknown", starting and chamber["mbar"] is None,
             lambda: f"the chamber pressure is unknown: {chamber['reason']}. "
                     f"The turbopump must not be started above {limit:.0E} "
                     f"mbar, so this cannot be allowed without a "
                     f"reading.{spinning}", 0),
            ("pressure_high", starting and chamber["mbar"] is not None
             and chamber["mbar"] > limit,
             lambda: f"the chamber is at {chamber['mbar']:.2E} mbar. Keep "
                     f"roughing until it reaches {limit:.0E} mbar -- a "
                     f"turbopump started against an un-roughed foreline "
                     f"cannot develop compression, draws its current limit "
                     f"and trips on over-temperature.", 0),
        ])
        # Whether or not it is blocked, say what the rule is measuring, so the
        # panel can show the approach to crossover rather than just a refusal.
        out["motor_on"].update({"pressure_mbar": chamber["mbar"],
                                "limit_mbar": limit})

        # Stopping the turbopump is always the safe direction, so it is never
        # blocked. Starting the station and acknowledging a fault are not
        # blocked either, but either one can let the drive start the rotor,
        # so each switches the motor off first -- see command().
        for action in ("on", "motor_off", "ack"):
            judge(action, [])

        # --- speed policy during a deposition -----------------------------
        for action in ("speed", "standby_on"):
            judge(action, [
                ("full_speed_lock", bool(self._full_speed_reason),
                 lambda: f"the rotor is locked at full speed: "
                         f"{self._full_speed_reason}. Throttle with the gate "
                         f"valve instead. Stop the run to release this.", 0)])
        return out

    def _check(self, action: str, snap: dict, waive=frozenset()) -> list[str]:
        """Raise Interlock if `action` is refused; return the rules waived."""
        rule = self.interlocks(snap, waive).get(action)
        if rule and not rule["allowed"]:
            raise Interlock(rule["reason"], rule["wait"], rule["rule"])
        return list(rule.get("waived", [])) if rule else []

    def _check_param(self, name: str, value, snap: dict,
                     waive=frozenset()) -> list[str]:
        """Interlocks that apply to accessory parameter writes."""
        venting_now = (name == "VentMode" and int(value) == 2) or \
                      (name == "EnableVent" and int(value) == 1)
        if not venting_now:
            return []
        limit = self.nominal_speed(snap) * VENT_SPEED_LIMIT
        speed = snap.get("status", {}).get("ActualSpd")
        age = snap.get("age")
        if (not snap.get("connected") or not isinstance(speed, (int, float))
                or not isinstance(age, (int, float))
                or age > self.stale_after(snap)):
            if "vent_speed_unknown" in waive:
                return ["vent_speed_unknown"]
            raise Interlock(
                f"the rotor speed is not known right now. Venting is only "
                f"permitted below {int(limit)} Hz, so it waits for a fresh "
                f"reading -- letting air in at speed can wreck the rotor.",
                rule="vent_speed_unknown")
        if speed > limit:
            if "vent_speed" in waive:
                return ["vent_speed"]
            raise Interlock(
                f"the rotor is at {speed} Hz. Venting is only permitted below "
                f"{int(limit)} Hz -- letting air in at speed can wreck the rotor.",
                rule="vent_speed")
        return []

    def snapshot(self) -> dict:
        snap = super().snapshot()
        snap["interlocks"] = self.interlocks(snap)
        snap["ratings"] = RATINGS
        # Full speed, and where that number came from: the drive itself, the
        # speed it is driving at, or the pump's rating while it has not said.
        hz, source = self._nominal(snap)
        snap["nominal"] = {"hz": hz, "source": source}
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

    def command(self, action: str, value=None, override=None) -> dict:
        """Carry out `action`, through the interlocks.

        `override` names the rules to waive -- one rule id, or several --
        and only those: there is no waiving everything. The result's
        `overridden` lists the waived rules that would have refused; it is
        empty when the override changed nothing.
        """
        try:
            waive = waived_rules(override)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if action == "param":
            name, setting = (value or {}).get("name"), (value or {}).get("value")
            return self.write_param(str(name), setting, waive)

        snap = self.snapshot()
        try:
            used = set(self._check(action, snap, waive))
        except Interlock as stop:
            return refused(stop)

        started_rotor = False
        with self.lock:
            try:
                pump = self.driver()
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}
            try:
                if action == "on":
                    # Already on: send nothing. On Pfeiffer's newer drives
                    # P010 = 1 means "on, and acknowledge the error", and on a
                    # drive with the motor flag set and a fault latched that
                    # restarts the rotor past the crossover rule. Whether the
                    # TC 600 does the same is not documented here.
                    if self._fresh(pump, "PumpgStatn") is True:
                        result = True
                    else:
                        started_rotor = self._motor_off_first(
                            pump, "starting the pumping station", waive, used,
                            snap, station_after=True)
                        result = pump.write("PumpgStatn", True,
                                            retries=COMMAND_RETRIES)
                elif action == "off":
                    result = pump.write("PumpgStatn", False,
                                        retries=COMMAND_RETRIES)
                elif action == "motor_on":
                    checked, motor = self._recheck_start(pump, snap, waive)
                    used.update(checked)
                    if motor is True:
                        result = True          # already set: nothing to send
                    else:
                        # Stamped before sending: a start whose replies are
                        # all lost has still happened, and the supervisor
                        # must not blame the front panel for it.
                        started_rotor = True
                        self.commanded["motor_start"] = time.monotonic()
                        result = pump.write("MotorPump", True,
                                            retries=COMMAND_RETRIES)
                elif action == "motor_off":
                    result = pump.write("MotorPump", False,
                                        retries=COMMAND_RETRIES)
                elif action == "standby_on":
                    result = pump.standby(True)
                elif action == "standby_off":
                    result = pump.standby(False)
                elif action == "ack":
                    started_rotor = self._motor_off_first(
                        pump, "clearing the fault", waive, used, snap)
                    result = pump.write("ErrorAckn", True,
                                        retries=COMMAND_RETRIES)
                elif action == "speed":
                    result = pump.set_speed_percent(float(value))
                else:
                    return {"ok": False, "error": f"unknown action {action!r}"}
            except Interlock as stop:
                return refused(stop)
            except protocol.ProtocolError as exc:
                self.note_failure()
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self.drop()
                return {"ok": False, "error": str(exc)}

        self.note_success()
        now = time.monotonic()
        self.commanded[action] = now
        if started_rotor:
            # The supervisor alarms on a rotor that starts without this
            # panel having asked for it; this is the panel asking.
            self.commanded["motor_start"] = now
        return {"ok": True, "action": action, "result": result,
                "overridden": sorted(used)}

    # --- commands that could start the rotor ---------------------------------
    #
    # The drive runs the rotor whenever the station and the motor are both on
    # and no fault is latched. So three commands can start it, not one: the
    # motor itself, the station coming on while the motor flag is still set,
    # and a fault being cleared under both. Only the first used to pass the
    # crossover interlock; the other two started the turbopump into whatever
    # the chamber held. Each is now checked against the drive itself at the
    # moment of sending -- the cached reading can be seconds old.

    def _fresh(self, pump, name: str):
        """One setpoint read from the drive now; None if it did not answer."""
        try:
            return pump.read(name, retries=COMMAND_RETRIES)
        except (protocol.ProtocolError, ValueError):
            return None

    def _motor_off_first(self, pump, doing: str, waive, used: set,
                         snap: dict, station_after=None) -> bool:
        """Switch the motor off before `doing`, unless the drive says it is.

        Raises Interlock (rule "motor_off_first") if the motor cannot be
        switched off. With that rule waived, the command goes out with the
        motor possibly on -- which is a turbopump start, so it must pass the
        start's own rules first (the pressure, the reading's age) unless
        those are waived too: waiving one rule must never waive the others.
        `station_after` is the station once the command is done; None means
        read it now. Returns True when the command may start the rotor --
        this panel starting it, stamped as such before it is sent.
        """
        if self._fresh(pump, "MotorPump") is False:
            return False
        if "motor_off_first" in waive:
            station = (station_after if station_after is not None
                       else self._fresh(pump, "PumpgStatn"))
            if station is not False:
                # The rotor starts once station and motor are both on, so it
                # is judged as a start: station on, motor flag being set.
                start = dict(snap)
                start["setpoints"] = {**snap.get("setpoints", {}),
                                      "PumpgStatn": True, "MotorPump": False}
                used.update(self._check("motor_on", start, waive))
            used.add("motor_off_first")
            self.commanded["motor_start"] = time.monotonic()
            return True
        try:
            echoed = pump.write("MotorPump", False, retries=COMMAND_RETRIES)
        except protocol.ProtocolError as exc:
            echoed = exc
        if echoed is not False and self._fresh(pump, "MotorPump") is False:
            # The reply was lost or refused, but the drive says it is off --
            # which is all that matters. Trusting only the echo meant a lost
            # one could keep a fault latched, and the backing pump off, for
            # good.
            echoed = False
        if echoed is not False:
            raise Interlock(
                f"the turbopump motor could not be switched off before "
                f"{doing} ({echoed}). With it on, the drive would start the "
                f"rotor at once, past the crossover interlock, so nothing was "
                f"sent. Switch the motor off and try again.",
                rule="motor_off_first")
        return False

    def _recheck_start(self, pump, snap: dict, waive) -> tuple[list, object]:
        """The start rule again, with the station and motor read now.

        Returns the waived rules that applied, and the motor flag as read.
        """
        motor = self._fresh(pump, "MotorPump")
        fresh = dict(snap)
        fresh["setpoints"] = {**snap.get("setpoints", {}),
                              "PumpgStatn": self._fresh(pump, "PumpgStatn"),
                              "MotorPump": motor}
        return self._check("motor_on", fresh, waive), motor

    # --- the safe state after a fault ---------------------------------------

    #: How long a make-safe waits for one already under way.
    SECURE_WAIT_S = 30.0

    def secure_when_free(self, reason: str, **options) -> dict:
        """`secure`, once any sequence already under way has finished.

        A drive fault during a run asks for it twice at once: the run ends in
        pump trouble, and the supervisor recovers from the fault. The second
        used to be told "already securing" and record a failure while the
        pump was in fact being made safe. It now waits its turn, then checks
        -- and finishes -- whatever the first left.
        """
        deadline = time.monotonic() + self.SECURE_WAIT_S
        while True:
            result = self.secure(reason, **options)
            if not result.get("busy") or time.monotonic() >= deadline:
                return result
            time.sleep(0.25)

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
                    "actions": [], "busy": True}
        try:
            actions: list[dict] = []
            snap = self.snapshot()
            if not snap.get("connected"):
                return {"ok": False, "actions": actions,
                        "error": "no contact with the pump, so nothing can be "
                                 "done over RS-485; the drive keeps doing "
                                 "what it was last told"}
            setpoints = snap.get("setpoints", {})
            raw = snap.get("status", {}).get("ErrorCode")
            code = str(raw).strip() if raw is not None else ""
            # A code that did not come back is acknowledged anyway: with the
            # motor off first, clearing a fault that is not there costs
            # nothing, and leaving one latched holds the backing pump off.
            faulted = raw is None or code not in NO_FAULT

            def step(label, action):
                result = self.command(action)
                actions.append({"step": label, "ok": bool(result.get("ok")),
                                "error": result.get("error")})
                return result.get("ok")

            # Unless the drive says the motor is off, switch it off: a motor
            # flag that merely failed to read is still on as far as the
            # drive's next move is concerned.
            if stop_motor and setpoints.get("MotorPump") is not False:
                step("turbopump motor switched off", "motor_off")
            if faulted:
                step(f"fault {code} acknowledged" if raw is not None
                     else "drive fault acknowledged", "ack")
            if keep_backing and setpoints.get("PumpgStatn") is not True:
                step("pumping station (forevacuum pump) switched on", "on")

            # Check it took: the cached snapshot lags a poll behind. Only what
            # the drive says counts -- a field that did not come back is not
            # "off", "on" or "clear".
            deadline = time.monotonic() + settle_s
            state = {}
            while time.monotonic() < deadline:
                time.sleep(0.5)
                latest = self.snapshot()
                if not latest.get("connected"):
                    continue
                now_code = latest["status"].get("ErrorCode")
                now_code = None if now_code is None else str(now_code).strip()
                state = {
                    "fault": "unknown" if now_code is None
                    else None if now_code in NO_FAULT else now_code,
                    "station": latest["setpoints"].get("PumpgStatn"),
                    "motor": latest["setpoints"].get("MotorPump"),
                    "speed": latest["status"].get("ActualSpd"),
                }
                if (not state["fault"]
                        and (state["station"] is True or not keep_backing)
                        and (state["motor"] is False or not stop_motor)):
                    break
            # Say so even when nothing needed switching: "the forevacuum
            # pump is running" is the line an operator looks for after a fault.
            if keep_backing and state and not any(
                    a["step"].startswith("pumping station") for a in actions):
                actions.append({"step": "forevacuum pump confirmed running"
                                if state.get("station") is True else
                                "forevacuum pump NOT confirmed running",
                                "ok": state.get("station") is True,
                                "error": None})
            secured = bool(state) and not state.get("fault") and \
                (state.get("station") is True or not keep_backing) and \
                (state.get("motor") is False or not stop_motor)
            return {"ok": secured, "actions": actions, "state": state,
                    "reason": reason,
                    "error": None if secured else
                    ("no fresh reading from the pump to confirm it"
                     if not state else
                     "the drive's error code did not come back to confirm "
                     "the fault is cleared" if state.get("fault") == "unknown"
                     else "the fault is still latched" if state.get("fault")
                     else "the motor is not confirmed off"
                     if stop_motor and state.get("motor") is not False
                     else "the station did not come back on")}
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

    def write_param(self, name: str, value, override=None) -> dict:
        """Write one of ALLOWED_WRITES, through the venting rules.

        Reads the value first, so the record can say what it was changed
        from: a protective setting changed without its old value is hard to
        put back.
        """
        try:
            waive = waived_rules(override)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
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

        try:
            used = self._check_param(name, coerced, self.snapshot(), waive)
        except Interlock as stop:
            return refused(stop)

        with self.lock:
            try:
                pump = self.driver()
                try:
                    previous = pump.read(parameter.number,
                                         retries=COMMAND_RETRIES)
                except protocol.ProtocolError:
                    previous = None             # not readable: say so, write
                result = pump.write(parameter.number, coerced,
                                    retries=COMMAND_RETRIES)
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
                "previous": previous, "overridden": sorted(used)}
