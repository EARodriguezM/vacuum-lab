"""The vacuum-test engine: procedures, the run thread, and result analysis.

A procedure is a small state machine. The engine samples every registered
device on a fixed cadence, writes each sample to the database, hands it to the
procedure, and lets the procedure decide whether to change phase or finish.
That split is what keeps the procedures readable: none of them touches a
serial port, a thread, or SQL.

Two rules apply to every procedure:

* **Abort conditions are checked before the procedure sees the sample.** A
  pump fault or a lost link ends the run regardless of what the procedure
  wanted to do next.
* **Commands go through the same interlocks as the UI.** The engine holds no
  override. A procedure that cannot start the turbopump because the chamber
  is not roughed down to the crossover pressure yet keeps roughing and asks
  again, exactly as an operator would have to.

Adding a procedure means subclassing `Procedure` and adding it to `CATALOGUE`;
the API publishes its `fields` and the UI builds the form from them.
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from collections import deque

from . import db as dbmod
from . import judgement
from . import process
from . import pump as pumpmod
from .history import SHAPE_KEYS, decimate
# Kept importable from here: the curve maths moved to lab/judgement.py.
from .judgement import linear_fit, relative_rate  # noqa: F401
from .pump import TURBO_START_MAX_MBAR

#: Payload of a TC 600 error-code parameter when nothing is wrong. (It read
#: "000, 0" once -- a typo that made "000" and "0" count as faults.)
NO_ERROR = pumpmod.NO_FAULT + (None,)

#: How a run treats steps the software cannot perform itself.
GUIDED = "guided"        # stop, tell the operator, wait for acknowledgement
AUTOMATIC = "auto"       # refuse to start unless everything is under control


def within(seconds: float, deadline: float | None) -> float:
    """`seconds`, or what is left before `deadline` (monotonic) if less."""
    if deadline is None:
        return seconds
    return max(0.0, min(seconds, deadline - time.monotonic()))


class Abort(Exception):
    """Raised inside the run thread to end a run early.

    `pump_trouble` marks the endings where the turbopump itself is at risk --
    a drive fault, over-temperature, a rotor dragged down by gas. Those put
    the pump in its safe state afterwards: motor off, fault cleared,
    forevacuum pump kept running (`PumpService.secure`). Every other ending
    leaves the pump exactly as it is. None of them switches the station off.
    """

    def __init__(self, reason: str, status: str = dbmod.ABORTED,
                 pump_trouble: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.pump_trouble = pump_trouble


# --- analysis helpers -----------------------------------------------------


def statistics(values: list[float]) -> dict:
    if not values:
        return {}
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n if n > 1 else 0.0
    return {
        "min": min(values), "max": max(values), "mean": mean,
        "stdev": math.sqrt(variance), "first": values[0], "last": values[-1],
        "count": n,
    }


# --- procedures -----------------------------------------------------------


class Procedure:
    """Base class. Subclasses describe their own configuration and phases."""

    name = "procedure"
    label = "Procedure"
    description = ""

    #: Form description for the UI. Each entry becomes one input.
    fields: tuple[dict, ...] = ()

    #: Default seconds between samples.
    interval = 2.0

    #: Phases in order, for the progress display.
    phases: tuple[str, ...] = ("running",)

    #: Pressure above which the run aborts, or None for no ceiling.
    pressure_ceiling: float | None = None

    #: Equipment beyond the pump and the gauge that this procedure cannot do
    #: without -- keys of `process.EQUIPMENT`. While any of it is not
    #: installed the procedure is listed but refused, guided or automatic.
    requires: tuple[str, ...] = ()

    #: Whether the operator stopping this run counts as completing it. False
    #: for procedures with an ending of their own -- stopping a deposition
    #: half way through really is an abort. True only where there is no
    #: natural end, so that stopping is the intended way to finish and
    #: filing it under failures would be wrong.
    stop_is_completion = False

    @classmethod
    def config_problems(cls, config: dict) -> list[str]:
        """What is wrong with a configuration whose every field is in range:
        fields that cannot work together. Nothing, unless a recipe says."""
        return []

    #: Seconds between repeats of the same "waiting for the interlock" note.
    HOLD_LOG_S = 60.0

    def __init__(self, config: dict):
        self.config = config
        self.phase = self.phases[0]
        self.started = time.monotonic()
        self.phase_started = self.started
        self._hold_reason: str | None = None      # the condition's key
        self._hold_message: str | None = None     # what it last said
        self._hold_logged = 0.0
        self._paused = 0.0                  # seconds this phase spent prompting
        self._paused_since: float | None = None

    def note_hold(self, engine: "RunEngine", reason: str | None,
                  key: str) -> None:
        """Log why a command is being refused, without flooding the log.

        A procedure that re-asks every cycle is refused every cycle until the
        condition clears. Silence there is what makes a run look stuck; a
        line every two seconds is what makes the log unreadable.

        The throttle is on `key`, not on the message, because the message
        moves: "the chamber is at 4.31E-01 mbar" differs from the last one
        every single sample, so comparing text logs the whole roughing curve
        one refusal at a time. `key` names the *condition* -- one line when
        it first bites, one a minute while it persists, and one more when it
        changes to a different condition.
        """
        reason = (reason or "the interlock is holding it").strip()
        self._hold_message = reason
        now = time.monotonic()
        if key == self._hold_reason and now - self._hold_logged < self.HOLD_LOG_S:
            return
        self._hold_reason, self._hold_logged = key, now
        engine.log("info", f"Waiting: {reason}")

    def last_hold(self) -> str:
        """Whatever last refused this procedure, for an abort message."""
        return self._hold_message or "the interlock is still holding it"

    # -- configuration helpers --------------------------------------------

    def number(self, key: str, default: float = 0.0) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def enter(self, phase: str) -> None:
        self.phase = phase
        self.phase_started = time.monotonic()
        self._paused = 0.0
        self._paused_since = None

    def phase_age(self, engine: "RunEngine") -> float:
        """Seconds in this phase, not counting time spent waiting on a human.

        A guided run stops for as long as it takes somebody to walk to the
        chamber and turn a valve, and that is the procedure working as
        intended -- it must not spend the phase's patience. So the clock is
        paused while a prompt is outstanding, which means a phase timeout
        measures the *rig* failing to get somewhere, never the operator being
        slow.

        Call this once per sample in any phase that has a timeout: the pause
        is accumulated as it is observed.
        """
        now = time.monotonic()
        waiting = engine.prompt() is not None
        if waiting and self._paused_since is None:
            self._paused_since = now
        elif not waiting and self._paused_since is not None:
            self._paused += now - self._paused_since
            self._paused_since = None
        outstanding = now - self._paused_since if self._paused_since else 0.0
        return max(0.0, now - self.phase_started - self._paused - outstanding)

    @property
    def in_phase(self) -> float:
        return time.monotonic() - self.phase_started

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started

    # -- lifecycle ---------------------------------------------------------

    def begin(self, engine: "RunEngine") -> None:
        """Called once before the first sample."""

    def guard(self, engine: "RunEngine", sample: dict) -> None:
        """Checks that must not wait for the next sample. Raise Abort.

        Run on every safety tick with new data -- once a second, whatever
        the procedure's sample interval -- before `step`. Only judgements
        belong here: no phase changes, no commands to the hardware. A
        procedure sampling once a minute used to look at its rotor once a
        minute too.
        """

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        """Called per sample. Return True when the run is finished."""
        raise NotImplementedError

    def current_pressure_ceiling(self, sample: dict) -> float | None:
        """The pressure above which this run must abort, right now.

        A method rather than a constant because the answer depends on where
        the recipe has got to: see `Deposition`, where a ceiling meant to
        protect a spinning turbopump must not be applied to a chamber that is
        still at atmosphere being roughed out.
        """
        return self.pressure_ceiling

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        """Compute the run's result from everything recorded."""
        return {}

    def finish(self, engine: "RunEngine") -> bool | None:
        """Called after the last sample, whatever the outcome.

        Returns False when the clean-up could not be done -- the run then
        stays owed it -- and True or None otherwise (None: there was nothing
        to confirm).

        It may be called again, minutes or weeks later, on a fresh instance
        that never began: an owed clean-up is tried again while the panel
        runs, and at the next start. Called like that it must only switch
        things off or close them: the heater off, the gas and the magnetron
        to 0, the shutter closed -- never the station, the motor or one of
        the drive's protections, where 0 or "off" can mean the opposite
        (VentMode 0 is controlled venting). Anything else -- a rate-of-rise
        reopening the gate valve it closed -- must hang on state that only
        the run itself sets, as `isolated_at` does. The selftest replays
        every procedure's `finish()` this way and checks what it asks for.
        """


class PumpDown(Procedure):
    """Record p(t) from wherever the chamber is now down to a target."""

    name = "pumpdown"
    label = "Pump-down curve"
    description = ("Records pressure against time while the chamber is pumped "
                   "down, and reports how long it took to reach the target.")
    phases = ("roughing", "turbo", "pumping", "done")
    fields = (
        {"key": "target_mbar", "label": "Target pressure", "type": "exponent",
         "default": 1e-5, "unit": "mbar", "min": 1e-10, "max": 1000,
         "help": "The run completes when the chamber reaches this."},
        {"key": "timeout_min", "label": "Give up after", "type": "number",
         "default": 120, "unit": "min", "min": 1, "max": 1440},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": 2, "unit": "s", "min": 1, "max": 60},
        {"key": "auto_start", "label": "Start the pump automatically",
         "type": "boolean", "default": True,
         "help": "Starts the station, then the turbopump as soon as the "
                 f"chamber is roughed below {TURBO_START_MAX_MBAR:.0E} mbar. "
                 "Leave off to drive the pump yourself."},
    )

    def begin(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 2))
        self.target = self.number("target_mbar", 1e-5)
        self.deadline = self.number("timeout_min", 120) * 60.0
        self.reached_at: float | None = None
        self.motor_requested = False

        if self.flag("auto_start", True):
            result = engine.pump_command("on")
            engine.log("action", "Started the pumping station"
                       if result.get("ok") else
                       f"Could not start the station: {result.get('error')}")
        else:
            engine.log("info", "Manual mode: waiting for you to start the pump")

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        pressure = sample.get("p_best")
        speed = sample.get("speed") or 0

        if self.phase == "roughing":
            if self.flag("auto_start", True) and not self.motor_requested:
                # Ask every cycle until the crossover interlock clears; the
                # engine holds no override, so this is simply waiting for the
                # chamber to be roughed out.
                result = engine.pump_command("motor_on")
                if result.get("ok"):
                    self.motor_requested = True
                    engine.log("action", "Started the turbopump")
                    self.enter("turbo")
                else:
                    self.note_hold(engine, result.get("error"), "motor_on")
            elif sample.get("motor"):
                # The drive's own motor enable, not the rotor speed: gas
                # rushing through the pump as roughing starts windmills the
                # rotor to a few Hz with the motor off, and that is not the
                # turbopump running.
                self.enter("turbo")

        elif self.phase == "turbo":
            nominal = engine.nominal_speed() or 0
            if nominal and speed >= judgement.FULL_SPEED_FRACTION * nominal:
                engine.log("info", f"Turbopump at full speed ({speed:.0f} Hz)")
                self.enter("pumping")
            elif not nominal and speed > 0:
                self.enter("pumping")

        if pressure is not None and pressure <= self.target:
            if self.reached_at is None:
                self.reached_at = sample["t"]
                engine.log("info",
                           f"Reached {self.target:.2E} mbar after "
                           f"{self.reached_at / 60:.1f} min")
            self.enter("done")
            return True

        if self.elapsed >= self.deadline:
            engine.log("warning",
                       f"Gave up after {self.deadline / 60:.0f} min without "
                       f"reaching {self.target:.2E} mbar")
            return True
        return False

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        pressures = [s["p_best"] for s in samples
                     if isinstance(s.get("p_best"), (int, float))]
        summary = {
            "target_mbar": self.target,
            "target_reached": self.reached_at is not None,
            "time_to_target_s": self.reached_at,
            "ultimate_mbar": min(pressures) if pressures else None,
            "final_mbar": pressures[-1] if pressures else None,
            "start_mbar": pressures[0] if pressures else None,
            "duration_s": samples[-1]["t"] if samples else 0,
        }
        # Decade crossings make two pump-downs comparable even when they
        # started from different pressures.
        crossings = {}
        for decade in (1e0, 1e-1, 1e-2, 1e-3, 1e-4, 1e-5, 1e-6, 1e-7):
            for entry in samples:
                value = entry.get("p_best")
                if isinstance(value, (int, float)) and value <= decade:
                    crossings[f"{decade:.0E}"] = entry["t"]
                    break
        summary["decade_crossings_s"] = crossings
        return summary


class RateOfRise(Procedure):
    """Isolate the chamber and measure how fast the pressure climbs."""

    name = "rate_of_rise"
    label = "Rate of rise / leak test"
    # Isolating the chamber from the pump is closing the gate valve.
    requires = ("gate",)
    description = ("With the chamber isolated, fits the pressure rise against "
                   "time and reports the leak rate.")
    phases = ("settle", "measuring", "done")
    fields = (
        {"key": "volume_l", "label": "Chamber volume", "type": "number",
         "default": 10, "unit": "L", "min": 0.01, "max": 10000,
         "help": "Needed to turn a pressure rise into a leak rate."},
        {"key": "settle_s", "label": "Settle for", "type": "number",
         "default": 60, "unit": "s", "min": 0, "max": 3600,
         "help": "Time after isolation before the fit starts, so the initial "
                 "transient is excluded."},
        {"key": "duration_s", "label": "Measure for", "type": "number",
         "default": 600, "unit": "s", "min": 30, "max": 86400},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": 5, "unit": "s", "min": 1, "max": 60},
    )

    #: Above this the gate is not opened again by itself at the end: a
    #: spinning turbopump handed a chamber this far up takes a gas load it
    #: may not carry. Somebody opens it, slowly, once they have looked.
    REOPEN_MAX_MBAR = 1.0e-2

    def begin(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 5))
        self.settle = self.number("settle_s", 60)
        self.duration = self.number("duration_s", 600)
        self.volume = self.number("volume_l", 10)
        self.fit_from: float | None = None
        self.isolated_at: float | None = None

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        if self.phase == "settle":
            # The test is meaningless with the pump still pumping the
            # chamber, so it begins by closing the gate -- by hand, in a
            # guided run -- and settles from then, not from the start.
            if self.isolated_at is None:
                if not engine.require(engine.hardware.gate, 0):
                    return False
                self.isolated_at = sample["t"]
                engine.log("info", f"Chamber isolated. The fit starts in "
                                   f"{self.settle:.0f} s.")
            if sample["t"] - self.isolated_at >= self.settle:
                self.fit_from = sample["t"]
                self.enter("measuring")
                engine.log("info", "Settling done, measuring the rise")
            return False

        if self.in_phase >= self.duration:
            self.enter("done")
            return True
        return False

    def finish(self, engine: "RunEngine") -> None:
        """Open the chamber to the pump again, if that is safe to do blind."""
        # Not isolated by this run -- or a run cut off by a crash, cleaned up
        # at the next start: nobody knows the gate's state, so leave it.
        if getattr(self, "isolated_at", None) is None:
            return
        gauge = engine.gauge()
        pressure = gauge.best_pressure() if gauge else None
        if not isinstance(pressure, (int, float)) or \
                pressure > self.REOPEN_MAX_MBAR:
            engine.log("warning",
                       "The gate valve to the turbopump is left closed: the "
                       + (f"chamber rose to {pressure:.2E} mbar while isolated"
                          if isinstance(pressure, (int, float)) else
                          "chamber pressure is not known")
                       + ". Open it slowly once the pressure has been "
                         "checked -- a spinning turbopump must not take "
                         "that much gas at once.")
            return
        try:
            engine.hardware.gate.set(100)
            engine.log("action", "Gate valve opened again")
        except process.OperatorRequired as prompt:
            engine.log("warning", f"MANUAL STEP: {prompt.instruction}")
        except process.ActuatorError as exc:
            engine.log("error", f"Could not open the gate valve: {exc}")

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        start = self.fit_from if self.fit_from is not None else 0.0
        points = [(s["t"], s["p_best"]) for s in samples
                  if s["t"] >= start
                  and isinstance(s.get("p_best"), (int, float))]
        fit = linear_fit(points)
        summary: dict = {
            "volume_l": self.volume,
            "fit_from_s": start,
            "fit_points": len(points),
            "start_mbar": points[0][1] if points else None,
            "end_mbar": points[-1][1] if points else None,
        }
        if fit and len({p for _, p in points}) == 1:
            summary["verdict"] = "frozen"
            # Not one reading changed. An isolated chamber always rises, if
            # only by outgassing, and the gauge reports five significant
            # digits: a curve this flat is a gauge that has stopped
            # measuring. Its fit is "R² 1, no rise at all" -- a perfect
            # leak-tight chamber, which is the one thing it is not evidence of.
            summary.update({
                "rise_mbar_per_s": None, "rise_mbar_per_h": None,
                "leak_rate_mbar_l_s": None, "r_squared": None,
                "linear": False,
                "caveat": (
                    f"The pressure did not change at all over {len(points)} "
                    f"readings ({points[0][1]:.2E} mbar each time). An "
                    f"isolated chamber always rises, so the gauge has "
                    f"stopped measuring: check it before trusting any "
                    f"leak rate."),
            })
        elif fit and fit["slope"] < 0:
            # Falling: something is still pumping the chamber -- a gate not
            # shut, or a second pump. Its slope is not a leak rate at all.
            summary.update({
                "rise_mbar_per_s": fit["slope"],
                "rise_mbar_per_h": fit["slope"] * 3600.0,
                "leak_rate_mbar_l_s": None, "r_squared": fit["r_squared"],
                "linear": False, "verdict": "falling",
                "caveat": (
                    "The pressure fell while the chamber was meant to be "
                    "isolated, so something was still pumping it: check that "
                    "the gate valve is shut. A falling curve has no leak "
                    "rate."),
            })
        elif fit:
            rise = fit["slope"]                       # mbar per second
            summary.update({
                "rise_mbar_per_s": rise,
                "rise_mbar_per_h": rise * 3600.0,
                "leak_rate_mbar_l_s": rise * self.volume,
                "r_squared": fit["r_squared"],
                "linear": fit["r_squared"] >= judgement.LINEAR_R2,
                "verdict": ("linear" if fit["r_squared"] >= judgement.LINEAR_R2
                            else "bent"),
            })
            if fit["r_squared"] < judgement.LINEAR_R2:
                summary["caveat"] = (
                    f"The rise is not linear (R² < {judgement.LINEAR_R2}), so "
                    "this figure is an "
                    "outgassing transient rather than a leak rate. Pump "
                    "longer before isolating, or extend the settling time.")
        else:
            summary["caveat"] = "Too few valid pressure readings to fit."
        return summary


class Hold(Procedure):
    """Pump continuously and record how stable the ultimate pressure is."""

    name = "hold"
    label = "Ultimate pressure / hold"
    description = ("Pumps for a set time and reports the lowest pressure "
                   "reached, the final pressure, and the drift between them.")
    phases = ("holding", "done")
    fields = (
        {"key": "duration_min", "label": "Hold for", "type": "number",
         "default": 60, "unit": "min", "min": 0.1, "max": 10080},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": 5, "unit": "s", "min": 1, "max": 300},
    )

    def begin(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 5))
        self.duration = self.number("duration_min", 60) * 60.0

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        if self.elapsed >= self.duration:
            self.enter("done")
            return True
        return False

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        pressures = [s["p_best"] for s in samples
                     if isinstance(s.get("p_best"), (int, float))]
        summary = {"pressure": statistics(pressures),
                   "duration_s": samples[-1]["t"] if samples else 0}
        if pressures:
            summary["ultimate_mbar"] = min(pressures)
            summary["final_mbar"] = pressures[-1]
            summary["drift_mbar"] = pressures[-1] - min(pressures)
            # A run whose last quarter is still falling has not bottomed out.
            tail = pressures[max(0, len(pressures) * 3 // 4):]
            if len(tail) >= 3:
                fit = linear_fit(list(enumerate(tail)))
                summary["settled"] = bool(fit and abs(fit["slope"]) <
                                          0.01 * abs(min(pressures)))
        return summary


class Bakeout(Procedure):
    """Drive the TC 600's TMS through heat, soak and cool while logging."""

    name = "bakeout"
    label = "Bake-out / TMS profile"
    # With nothing on the HEAT/TMS output, the heating phase can only time out.
    requires = ("heater",)
    description = ("Ramps the pump's temperature management system to a "
                   "setpoint, soaks, then cools, logging pressure and "
                   "temperature throughout.")
    phases = ("heating", "soak", "cooling", "done")
    fields = (
        {"key": "setpoint_c", "label": "TMS setpoint", "type": "number",
         "default": 70, "unit": "°C", "min": 30, "max": 90,
         "help": "The TC 600 accepts 30-90 °C on parameter 704."},
        {"key": "soak_hours", "label": "Soak for", "type": "number",
         "default": 4, "unit": "h", "min": 0.1, "max": 168},
        {"key": "cool_to_c", "label": "Cool down to", "type": "number",
         "default": 40, "unit": "°C", "min": 20, "max": 80},
        {"key": "max_pressure_mbar", "label": "Abort above", "type": "exponent",
         "default": 1e-3, "unit": "mbar", "min": 1e-9, "max": 1e-1,
         "help": "Bake-out drives outgassing; abort if the pressure climbs "
                 "past this."},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": 10, "unit": "s", "min": 1, "max": 600},
        {"key": "heat_timeout_min", "label": "Heating timeout", "type": "number",
         "default": 60, "unit": "min", "min": 1, "max": 600},
    )

    def begin(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 10))
        self.setpoint = self.number("setpoint_c", 70)
        self.soak = self.number("soak_hours", 4) * 3600.0
        self.cool_to = self.number("cool_to_c", 40)
        self.heat_timeout = self.number("heat_timeout_min", 60) * 60.0
        # Never `or None`: a 0 that slipped through was "no ceiling at all".
        self.pressure_ceiling = self.number("max_pressure_mbar", 1e-3)
        if not self.pressure_ceiling > 0:
            raise Abort("the pressure limit for the bake-out must be above "
                        "zero", dbmod.FAILED)
        self.peak_pressure = 0.0
        self.peak_temperature = 0.0

        if not 30 <= self.setpoint <= 90:
            raise Abort(f"TMS setpoint {self.setpoint:g} °C is outside the "
                        "30-90 °C the drive unit accepts", dbmod.FAILED)

        written = engine.pump_param("TMSHeatSVal", int(self.setpoint))
        if not written.get("ok"):
            raise Abort(f"could not set the TMS setpoint: "
                        f"{written.get('error')}", dbmod.FAILED)
        switched = engine.pump_param("Heating", True)
        if not switched.get("ok"):
            raise Abort(f"could not switch the heating on: "
                        f"{switched.get('error')}", dbmod.FAILED)
        engine.log("action", f"Heating on, TMS setpoint {self.setpoint:.0f} °C")

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        temperature = sample.get("tms_temp")
        pressure = sample.get("p_best")
        if isinstance(pressure, (int, float)):
            self.peak_pressure = max(self.peak_pressure, pressure)
        if isinstance(temperature, (int, float)):
            self.peak_temperature = max(self.peak_temperature, temperature)

        if self.phase == "heating":
            if isinstance(temperature, (int, float)) and \
                    temperature >= self.setpoint - 2:
                engine.log("info", f"Reached {temperature:.0f} °C, soaking for "
                                   f"{self.soak / 3600:.1f} h")
                self.enter("soak")
            elif self.in_phase >= self.heat_timeout:
                raise Abort(
                    f"the TMS did not reach {self.setpoint:.0f} °C within "
                    f"{self.heat_timeout / 60:.0f} min (it is at "
                    f"{temperature if temperature is not None else '?'} °C)")

        elif self.phase == "soak":
            if self.in_phase >= self.soak:
                engine.pump_param("Heating", False)
                engine.log("action", "Soak complete, heating off, cooling down")
                self.enter("cooling")

        elif self.phase == "cooling":
            if isinstance(temperature, (int, float)) and \
                    temperature <= self.cool_to:
                self.enter("done")
                return True
        return False

    def finish(self, engine: "RunEngine") -> bool:
        # Whatever happened -- completion, abort, operator stop -- the heater
        # must not be left on by a run that is no longer being watched.
        result = engine.pump_param("Heating", False)
        engine.log("action", "Heating switched off" if result.get("ok") else
                   f"WARNING: could not switch the heating off: "
                   f"{result.get('error')}")
        # Done only if the drive itself says the heater is off.
        return bool(result.get("ok")) and result.get("result") is False

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        temperatures = [s["tms_temp"] for s in samples
                        if isinstance(s.get("tms_temp"), (int, float))]
        pressures = [s["p_best"] for s in samples
                     if isinstance(s.get("p_best"), (int, float))]
        return {
            "setpoint_c": self.setpoint,
            "soak_hours": self.soak / 3600.0,
            "peak_temperature_c": self.peak_temperature or None,
            "peak_pressure_mbar": self.peak_pressure or None,
            "final_pressure_mbar": pressures[-1] if pressures else None,
            "start_pressure_mbar": pressures[0] if pressures else None,
            "temperature": statistics(temperatures),
            "pressure": statistics(pressures),
            "duration_s": samples[-1]["t"] if samples else 0,
        }


class VacuumSequence(Procedure):
    """The rig's standard route to base pressure, shared by every recipe.

        rough        forevacuum pump alone, atmosphere down to the crossover
        crossover    at the crossover pressure, start the turbopump
        spinup       wait for full rotation speed
        basepressure keep pumping until the curve flattens

    Crossing over on *pressure* rather than on a timer is the point. A
    turbopump started against a chamber that has not been roughed out cannot
    develop compression, draws its current limit and trips on over-temperature.
    The gauge knows when the foreline is ready; a stopwatch does not.

    "Flattens" is measured as a fractional rate (percent per minute), not an
    absolute one -- see `relative_rate`. The chamber decides its own base
    pressure and the run records what it turned out to be, which is the
    calibration the professor asked for.
    """

    #: Defaults for the shared route. Each is named once and referenced
    #: twice -- by the published field descriptor and by the fallback in
    #: `begin_vacuum` -- because those two disagreeing is how a deposition
    #: came to throttle a valve the operator was shown as open. A subclass
    #: that wants a different default overrides the attribute and points its
    #: own field descriptor at it.
    DEFAULT_INTERVAL_S = 2.0
    DEFAULT_CROSSOVER_MBAR = TURBO_START_MAX_MBAR
    DEFAULT_BASE_RATE_PCT = 2.0
    DEFAULT_BASE_SETTLE_MIN = 3.0
    DEFAULT_BASE_TIMEOUT_MIN = 120.0
    DEFAULT_STAGE_TIMEOUT_MIN = 45.0

    #: Fields every recipe built on this sequence shares.
    VACUUM_FIELDS: tuple[dict, ...] = (
        {"key": "mode", "label": "Run mode", "type": "select",
         "default": GUIDED,
         "options": [{"value": GUIDED, "label": "Guided — prompt me for "
                                                "anything not yet wired"},
                     {"value": AUTOMATIC, "label": "Automatic — refuse to "
                                                   "start unless fully wired"}],
         "help": "Automatic needs every step this procedure takes under "
                 "software control. One that only pumps -- base calibration, "
                 "full vacuum -- runs either way; a deposition stays guided "
                 "until the argon, gate valve, magnetron and shutter are "
                 "wired."},
        {"key": "crossover_mbar", "label": "Start the turbopump at",
         "type": "exponent", "default": DEFAULT_CROSSOVER_MBAR, "unit": "mbar",
         "min": 1e-4, "max": 1000,
         "help": "The forevacuum pump works alone until the chamber reaches "
                 f"this. {TURBO_START_MAX_MBAR:.0E} mbar is the highest the "
                 "interlock will allow; asking for more is lowered to it, "
                 "since a turbopump started against an un-roughed foreline "
                 "cannot develop compression and trips."},
        {"key": "base_rate_pct", "label": "Call it base pressure below",
         "type": "number", "default": DEFAULT_BASE_RATE_PCT, "unit": "%/min",
         "min": 0.1, "max": 50,
         "help": "When the pressure is improving more slowly than this, the "
                 "chamber has stopped getting better."},
        {"key": "base_settle_min", "label": "…sustained for", "type": "number",
         "default": DEFAULT_BASE_SETTLE_MIN, "unit": "min", "min": 0.5,
         "max": 120},
        {"key": "base_timeout_min", "label": "Give up looking for base after",
         "type": "number", "default": DEFAULT_BASE_TIMEOUT_MIN, "unit": "min",
         "min": 1, "max": 1440},
        {"key": "stage_timeout_min", "label": "Give up on any other stage after",
         "type": "number", "default": DEFAULT_STAGE_TIMEOUT_MIN, "unit": "min",
         "min": 1, "max": 1440,
         "help": "Roughing, the turbopump start, spin-up and the argon "
                 "backfill each get this long before the run gives up. Time "
                 "spent waiting for you to do something by hand does not "
                 "count against it."},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": DEFAULT_INTERVAL_S, "unit": "s", "min": 1, "max": 60},
    )

    #: Rolling window used to judge whether the pressure has flattened. The
    #: thresholds here are lab/judgement.py's, which the panel draws with.
    RATE_WINDOW_S = judgement.RATE_WINDOW_S

    @classmethod
    def rate_window_for(cls, interval: float) -> float:
        """The window the rate is fitted over at this sample interval.

        The shared 120 s, or wide enough to hold the readings a rate needs --
        and one more, so a single lost reading does not restart the settle --
        when they come further apart. At 30 s and more a fixed 120 s window
        never held five readings, and base pressure was never declared.
        """
        return max(cls.RATE_WINDOW_S,
                   (judgement.RATE_MIN_POINTS + 1) * max(1.0, interval))

    @classmethod
    def base_minimum_s(cls, interval: float, settle: float) -> float:
        """The soonest base pressure can be called on a flat curve, counted
        the way the engine steps: the first reading one interval in, then
        enough readings for a rate -- the minimum number, spanning their
        share of the window -- then the settle, in whole intervals; and one
        interval and one tick more, for a reading lost and one taken late."""
        interval = max(1.0, interval)
        span = judgement.RATE_MIN_SPAN * cls.rate_window_for(interval)
        readings = max((judgement.RATE_MIN_POINTS - 1) * interval,
                       math.ceil(span / interval) * interval)
        return (interval + readings + math.ceil(settle / interval) * interval
                + interval + RunEngine.TICK_S)

    @classmethod
    def config_problems(cls, config: dict) -> list[str]:
        """A base timeout too short for base pressure ever to be called."""
        try:
            timeout = float(config.get("base_timeout_min") or 0) * 60.0
            interval = max(1.0, float(config.get("interval_s")
                                      or cls.DEFAULT_INTERVAL_S))
            settle = float(config.get("base_settle_min")
                           or cls.DEFAULT_BASE_SETTLE_MIN) * 60.0
        except (TypeError, ValueError):
            return []
        if timeout <= 0:                    # no limit
            return []
        needed = cls.base_minimum_s(interval, settle)
        if timeout >= needed:
            return []
        return [f'"Give up looking for base after" must be at least '
                f"{math.ceil(needed / 60)} min with these settings: with a "
                f"reading every {interval:g} s the rate needs a while to be "
                f"known, and must then stay flat for {settle / 60:g} min"]

    def _recent_rate(self, now: float) -> float | None:
        """The rate over the last two minutes alone (at least two readings'
        worth): at long sample intervals the window widens to hold its
        readings, and a wide window lags -- a curve that has just turned
        upward still averages flat. None with fewer than three readings."""
        span = max(self.RATE_WINDOW_S, 2 * self.interval) + self.interval / 2
        recent = [(t, math.log(p)) for t, p in self._window
                  if now - t <= span and p > 0]
        if len(recent) < 3:
            return None
        fit = linear_fit(recent)
        return fit["slope"] * 6000.0 if fit else None

    #: The rotor counts as "at full speed" from this fraction of nominal. Spin-up
    #: ends here, and base pressure is only judged while the rotor stays here.
    FULL_SPEED_FRACTION = judgement.FULL_SPEED_FRACTION

    #: How long the gas load may hold the rotor below full speed, once it has
    #: reached it, before the run ends. Run 26 (2026-10-08) sat there twelve
    #: minutes, drive at its power limit, before it tripped on
    #: over-temperature. Two minutes is long enough to back a needle valve
    #: off after an overshoot, and far short of that.
    DRAGGED_LIMIT_S = 120.0

    #: Below this fraction of nominal the run ends at once: the 80 %
    #: switchpoint plus a margin. Run 25 tripped with Err006 five seconds
    #: after its rotor crossed 1200 Hz -- and only 65 s after it first left
    #: full speed -- so a dwell timer alone protects nothing there. At the
    #: drag rate that run showed, 85 % comes about half a minute before 80 %.
    PROTECT_FRACTION = judgement.PROTECT_FRACTION

    #: Why the rotor is held at its final rotation speed for this recipe.
    #: Overridden where the reason is not the deposition one.
    full_speed_reason = ("this rig sputters in argon, so the turbopump runs "
                         "at its final rotation speed for the whole run")

    def begin_vacuum(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s",
                                             self.DEFAULT_INTERVAL_S))
        self.rate_window = self.rate_window_for(self.interval)
        # A recipe may cross over deeper than the interlock demands, never
        # shallower: asking for a value above TURBO_START_MAX_MBAR would leave
        # the run stuck in the crossover phase asking for a start the pump
        # service keeps refusing.
        requested = self.number("crossover_mbar", self.DEFAULT_CROSSOVER_MBAR)
        self.crossover = min(requested, TURBO_START_MAX_MBAR)
        if self.crossover < requested:
            engine.log("warning",
                       f"Crossover lowered from {requested:.2E} to "
                       f"{self.crossover:.2E} mbar: the turbopump may not be "
                       f"started above {TURBO_START_MAX_MBAR:.0E} mbar.")
        self.base_rate_limit = self.number("base_rate_pct",
                                           self.DEFAULT_BASE_RATE_PCT)
        self.base_settle = self.number("base_settle_min",
                                       self.DEFAULT_BASE_SETTLE_MIN) * 60.0
        # Both timeouts take 0 to mean "no limit". Every procedure built on
        # this sequence wants a bound except `FullVacuum`, whose whole point
        # is to keep pumping until a person says stop.
        self.base_timeout = max(0.0, self.number(
            "base_timeout_min", self.DEFAULT_BASE_TIMEOUT_MIN)) * 60.0
        # Every other phase needs a bound too. Without one, a chamber that
        # never reaches crossover -- a shut roughing valve, a dead forepump,
        # a gross leak -- roughs for ever, and a run that cannot finish still
        # looks alive.
        self.stage_timeout = max(0.0, self.number(
            "stage_timeout_min", self.DEFAULT_STAGE_TIMEOUT_MIN)) * 60.0

        self.base_mbar: float | None = None
        self.base_reached_at: float | None = None
        self.crossed_at: float | None = None
        self.full_speed_at: float | None = None
        self._dragged_since: float | None = None
        self._flat_since: float | None = None
        self._window: deque = deque()
        self.turbo_requested = False
        self._motor_seen_on = False
        self._station_seen_on = False

        engine.enforce_full_speed(self.full_speed_reason)
        result = engine.pump_command("on")
        engine.log("action", "Forevacuum pump started" if result.get("ok")
                   else f"Could not start the forevacuum pump: "
                        f"{result.get('error')}")

    # -- phases ------------------------------------------------------------

    def phase_rough(self, engine: "RunEngine", sample: dict) -> str | None:
        pressure = sample.get("p_best")
        if self.stage_timeout and self.phase_age(engine) >= self.stage_timeout:
            raise Abort(
                f"roughing ran for {self.stage_timeout / 60:.0f} min without "
                f"reaching the {self.crossover:.2E} mbar crossover"
                + (f" (the chamber is at {pressure:.2E} mbar)"
                   if isinstance(pressure, (int, float)) else
                   " (and no gauge channel is reading)")
                + ". Check the forevacuum pump, the roughing valve, and the "
                  "chamber for a gross leak.")
        if pressure is None:
            # Crossing over is a pressure decision, so no pressure means no
            # crossover, however long the roughing runs. Do not let that look
            # like a slow pump-down.
            self.note_hold(engine, "no valid pressure reading, so the "
                                   "crossover pressure cannot be recognised",
                           "no_pressure")
            return None
        if pressure <= self.crossover:
            self.crossed_at = sample["t"]
            engine.log("info", f"Reached the crossover pressure "
                               f"{pressure:.2E} mbar after "
                               f"{self.crossed_at / 60:.1f} min")
            return "crossover"
        return None

    def phase_crossover(self, engine: "RunEngine", sample: dict) -> str | None:
        if self.stage_timeout and self.phase_age(engine) >= self.stage_timeout:
            raise Abort(
                f"the turbopump could not be started within "
                f"{self.stage_timeout / 60:.0f} min of reaching crossover: "
                f"{self.last_hold()}")
        if not self.turbo_requested:
            result = engine.pump_command("motor_on")
            if result.get("ok"):
                self.turbo_requested = True
                engine.log("action", "Turbopump started")
            else:
                # Not an error: the chamber may have crept back above the
                # crossover pressure, or the gauge may have blinked. Keep
                # roughing and keep asking, saying why in the run log.
                self.note_hold(engine, result.get("error"), "motor_on")
                return None
        return "spinup"

    def phase_spinup(self, engine: "RunEngine", sample: dict) -> str | None:
        speed = sample.get("speed") or 0
        nominal = engine.nominal_speed() or 0
        if self.stage_timeout and self.phase_age(engine) >= self.stage_timeout:
            raise Abort(
                f"the rotor did not reach full speed within "
                f"{self.stage_timeout / 60:.0f} min (it is at {speed:.0f} Hz "
                f"of {nominal or '?'} Hz). Check the drive unit for a fault "
                f"and the foreline for a leak.")
        if nominal and speed >= self.FULL_SPEED_FRACTION * nominal:
            self.full_speed_at = sample["t"]
            engine.log("info", f"Turbopump at full speed ({speed:.0f} Hz)")
            return "basepressure"
        if not nominal and speed > 0 and sample.get("motor"):
            # With no nominal speed to compare against there is no way to
            # know "full", so any rotation has to do -- but only while the
            # motor is actually enabled. Windmilling would otherwise call a
            # coasting rotor "up to speed".
            return "basepressure"
        return None

    def phase_basepressure(self, engine: "RunEngine", sample: dict) -> str | None:
        pressure = sample.get("p_best")
        now = sample["t"]
        if isinstance(pressure, (int, float)) and pressure > 0:
            self._window.append((now, pressure))
            while self._window and now - self._window[0][0] > self.rate_window:
                self._window.popleft()

        rate = relative_rate(list(self._window), self.rate_window)
        sample["rate_pct_min"] = rate

        # A flat curve is only a base pressure if the pump is doing its full
        # work. Under a gas load it cannot carry, the TC 600 lowers the rotor
        # speed until gas friction and its permitted power balance, and the
        # pressure then sits flat at whatever that equilibrium is -- run 26
        # (2026-10-08) was called "base pressure 3.36E-02 mbar" with the
        # rotor dragged down to 1160 Hz of 1500, twelve minutes before it
        # tripped on over-temperature. That plateau describes the gas
        # coming in, not the chamber, so it must not be recorded as either.
        speed = sample.get("speed")
        # A speed that did not come back is not a rotor held down -- it used
        # to read as 0 Hz, log "held below full speed by the gas load" and
        # restart the settle -- but nor does it show full speed, so base
        # pressure is never called on that sample.
        speed_read = isinstance(speed, (int, float))
        nominal = engine.nominal_speed() or 0
        if nominal and speed_read and speed < self.FULL_SPEED_FRACTION * nominal:
            self._flat_since = None
            self.note_hold(
                engine,
                f"the rotor is at {speed:.0f} Hz of {nominal:.0f} Hz, held "
                f"below full speed by the gas load, so this is not a base "
                f"pressure. Look for an open gas valve or a leak"
                + (f" (the chamber is at {pressure:.2E} mbar)."
                   if isinstance(pressure, (int, float)) else "."),
                "rotor_dragged")
        elif rate is not None and abs(rate) <= self.base_rate_limit and (
                (recent := self._recent_rate(now)) is None
                or abs(recent) <= self.base_rate_limit):
            if self._flat_since is None:
                self._flat_since = now
            elif now - self._flat_since >= self.base_settle and \
                    isinstance(pressure, (int, float)) and \
                    (speed_read or not nominal):
                # The base pressure is this sample's reading, so it waits
                # for a sample that has one: a gauge dropout here used to
                # crash the run on formatting None.
                self.base_mbar = pressure
                self.base_reached_at = now
                held = (f"{self.base_settle:.0f} s" if self.base_settle < 60
                        else f"{self.base_settle / 60:.0f} min")
                engine.log("info",
                           f"Base pressure {pressure:.2E} mbar — improving by "
                           f"only {abs(rate):.2f} %/min for {held}")
                return self.after_base_pressure()
        else:
            self._flat_since = None

        if self.base_timeout and self.phase_age(engine) >= self.base_timeout:
            raise Abort(self._base_timeout_message(pressure, rate, now))
        return None

    def _base_timeout_message(self, pressure, rate, now: float) -> str:
        """Why base pressure was not reached in time -- each cause by name."""
        after = f"gave up after {self.base_timeout / 60:.0f} min"
        where = (f"at {pressure:.2E} mbar" if isinstance(pressure, (int, float))
                 else "without a reading at this moment")
        if rate is None and not self._window:
            return f"{after} without a usable pressure reading"
        if rate is None:
            return (f"{after} with too few readings to judge the rate: "
                    f"{len(self._window)} in the last {self.rate_window:.0f} s, "
                    f"where it needs {judgement.RATE_MIN_POINTS} spanning "
                    f"{judgement.RATE_MIN_SPAN * self.rate_window:.0f} s. "
                    f"Look for gauge dropouts, or sample more often.")
        if abs(rate) <= self.base_rate_limit:
            flat = now - self._flat_since if self._flat_since is not None else 0
            return (f"{after}: the chamber is {where} and flat ({rate:+.2f} "
                    f"%/min), but had stayed flat for only {flat:.0f} s of the "
                    f"{self.base_settle:.0f} s needed. Allow more time, or "
                    f"shorten the settle.")
        return (f"{after}: the chamber is {where} and still "
                + ("improving" if rate < 0 else "rising")
                + f" by {abs(rate):.1f} %/min. Suspect a leak, a wet chamber, "
                  f"or a target that needs baking.")

    def after_base_pressure(self) -> str:
        """What happens once base pressure is reached. Recipes override this."""
        return "done"

    def vacuum_summary(self) -> dict:
        return {
            "base_mbar": self.base_mbar,
            "time_to_base_s": self.base_reached_at,
            "crossover_mbar": self.crossover,
            "time_to_crossover_s": self.crossed_at,
            "time_to_full_speed_s": self.full_speed_at,
        }

    # -- protecting the pump ------------------------------------------------

    def check_rotor(self, engine: "RunEngine", sample: dict) -> None:
        """End the run if the gas load holds the rotor below full speed.

        Once the rotor has reached full speed in this run, with its motor
        still on, falling below `FULL_SPEED_FRACTION` means the drive is at
        its power limit and the gas wants more: the TC 600 lowers the speed
        until gas friction and its permitted power balance. Left there it
        trips -- Err006 below the switchpoint, or over-temperature -- after
        minutes of running a hot pump. Ending the run first means a
        deposition's abort path shuts the argon off and the pump recovers.

        Applies only to this sequence, which locks the rotor to full speed:
        standby and the speed-setting mode are refused while it runs, so
        nothing but the gas can be holding the rotor down.
        """
        nominal = engine.nominal_speed() or 0
        speed = sample.get("speed")
        motor = sample.get("motor")
        if not isinstance(speed, (int, float)) or motor is None:
            # A reading that did not come back neither starts the clock nor
            # stops it. It used to stop it: one lost reply every two minutes
            # kept a dragged rotor from ever being stopped for it.
            return
        if (self.full_speed_at is None or not nominal or not motor
                or speed >= self.FULL_SPEED_FRACTION * nominal):
            self._dragged_since = None
            return
        if self._dragged_since is None:
            self._dragged_since = sample["t"]
            pressure = sample.get("p_best")
            engine.log("warning",
                       f"The rotor has fallen to {speed:.0f} Hz of "
                       f"{nominal:.0f} Hz with the motor on: the gas load is "
                       f"more than the pump can carry at full speed"
                       + (f" (the chamber is at {pressure:.2E} mbar)"
                          if isinstance(pressure, (int, float)) else "")
                       + f". The run ends if it is not back within "
                         f"{self.DRAGGED_LIMIT_S:.0f} s -- cut the gas flow "
                         f"or throttle the gate valve.")
            # No return here: the first reading below full speed may already
            # be below PROTECT_FRACTION, and waiting a sample to say so is
            # waiting the run-up window out.
        held = sample["t"] - self._dragged_since
        if speed < self.PROTECT_FRACTION * nominal:
            raise Abort(
                f"the gas load has dragged the rotor down to {speed:.0f} Hz of "
                f"{nominal:.0f} Hz, close to the {0.8 * nominal:.0f} Hz "
                f"switchpoint below which the drive trips (Err006). That is "
                f"more gas than this pump can carry. Shut the gas off now; "
                f"lower the flow or throttle the gate valve before running "
                f"again.", pump_trouble=True)
        if held >= self.DRAGGED_LIMIT_S:
            raise Abort(
                f"the gas load held the rotor below full speed for "
                f"{held:.0f} s (it is at {speed:.0f} Hz of {nominal:.0f} Hz). "
                f"That is more gas than this pump can carry: left there the "
                f"drive trips below its {0.8 * nominal:.0f} Hz switchpoint "
                f"(Err006) or on over-temperature. Lower the gas flow or "
                f"throttle the gate valve before running again.",
                pump_trouble=True)

    # -- dispatch ----------------------------------------------------------

    def check_temperature_flags(self, engine: "RunEngine") -> None:
        """End the run when the drive raises an over-temperature flag.

        The drive measures its motor and its electronics internally and
        reports only the verdict (P304, P305) -- this firmware publishes no
        temperature. Raised, it means the drive is already cutting the motor
        current to protect itself, and the rotor will sink towards the
        switchpoint. Every recipe on this sequence stops there: a deposition
        must not keep feeding gas to a pump that is overheating.
        """
        status = engine.pump_status()
        if status.get("OvTempPump") is True or status.get("OvTempElec") is True:
            which = "pump" if status.get("OvTempPump") is True else "electronics"
            raise Abort(f"the drive unit reports the {which} over temperature. "
                        f"The turbopump motor is being switched off; the "
                        f"forevacuum pump keeps running.", pump_trouble=True)

    def check_pump_kept(self, engine: "RunEngine", sample: dict) -> None:
        """End the run if its pump is switched off under it.

        Nothing else noticed: with the motor off, `check_rotor` stands down
        for a coasting rotor, and a deposition kept its argon flowing into a
        pump that was no longer pumping. Judged only once the run has seen
        each one on, so a reading taken just before the start is not
        mistaken for a stop. A lost reading (None) is not a stop either.
        """
        motor, station = sample.get("motor"), sample.get("station")
        if motor == 1:
            self._motor_seen_on = True
        if station == 1:
            self._station_seen_on = True
        stopped = ("turbopump motor" if motor == 0 and self._motor_seen_on
                   else "pumping station" if station == 0
                   and self._station_seen_on else None)
        if stopped:
            raise Abort(f"the {stopped} was switched off while the run still "
                        f"needed it -- from this panel, the drive's front "
                        f"panel or its remote plug -- so the run has ended. "
                        f"The run changed nothing else on the pump.")

    def guard(self, engine: "RunEngine", sample: dict) -> None:
        """The pump's protection, on every safety tick."""
        self.check_temperature_flags(engine)
        self.check_pump_kept(engine, sample)
        self.check_rotor(engine, sample)

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        handler = getattr(self, f"phase_{self.phase}", None)
        if handler is None:
            return True
        nxt = handler(engine, sample)
        if nxt == "done":
            self.enter("done")
            return True
        if nxt:
            self.enter(nxt)
        return False


class BaseCalibration(VacuumSequence):
    """Find out how good this chamber actually gets, and how long it takes."""

    name = "base_calibration"
    label = "Base pressure calibration"
    description = ("Pumps the chamber down the standard route and keeps going "
                   "until the pressure stops improving, recording the base "
                   "pressure and the time to reach it. Run this after any "
                   "change to the chamber; later depositions compare against "
                   "it.")
    phases = ("rough", "crossover", "spinup", "basepressure", "done")
    fields = VacuumSequence.VACUUM_FIELDS

    def begin(self, engine: "RunEngine") -> None:
        self.begin_vacuum(engine)

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        pressures = [s["p_best"] for s in samples
                     if isinstance(s.get("p_best"), (int, float))]
        summary = self.vacuum_summary()
        summary.update({
            "ultimate_mbar": min(pressures) if pressures else None,
            "duration_s": samples[-1]["t"] if samples else 0,
            "is_reference": True,
        })
        # A calibration is only meaningful next to the previous one.
        previous = engine.last_base_pressure(exclude_run=engine.run_id())
        if previous and summary["base_mbar"]:
            summary["previous_base_mbar"] = previous
            ratio = summary["base_mbar"] / previous
            summary["change_factor"] = ratio
            if ratio > 3:
                summary["caveat"] = (
                    f"This chamber now bases out {ratio:.1f}x worse than last "
                    f"time ({previous:.2E} mbar). That is a leak, a wet "
                    f"chamber, or a contaminated target — not normal drift.")
        return summary


class Deposition(VacuumSequence):
    """A full magnetron sputter deposition in argon.

    After base pressure the recipe leaves pure vacuum work and starts making
    a film:

        backfill    throttle the gate valve and admit argon to the working
                    pressure -- typically 1e-3 to 1e-2 mbar, four decades
                    above base
        presputter  strike the plasma with the shutter CLOSED, to sputter the
                    target's own oxide layer away before it reaches the sample
        deposit     open the shutter for the deposition time
        recover     magnetron off, gas off, pump back down

    The turbopump runs at full speed throughout and the gate valve does the
    throttling. That is why the pump's speed setpoint is locked out for the
    duration: dropping the rotor to hold pressure would cost compression
    exactly when the gas load is highest.
    """

    name = "deposition"
    label = "Magnetron deposition (argon)"
    requires = ("gas", "gate", "magnetron", "shutter")
    description = ("The full process: pump to base pressure, backfill with "
                   "argon, pre-sputter the target, then deposit for a set "
                   "time. Steps that are not wired up yet are prompted.")
    phases = ("rough", "crossover", "spinup", "basepressure", "backfill",
              "presputter", "deposit", "recover", "done")

    fields = VacuumSequence.VACUUM_FIELDS + (
        {"key": "working_mbar", "label": "Argon working pressure",
         "type": "exponent", "default": 5e-3, "unit": "mbar",
         "min": 1e-4, "max": 5e-2,
         "help": "Typical magnetron sputtering runs 1e-3 to 1e-2 mbar. Too "
                 "low and the plasma will not sustain; too high and the film "
                 "goes porous from gas scattering."},
        {"key": "argon_sccm", "label": "Argon flow", "type": "number",
         "default": 20, "unit": "sccm", "min": 0, "max": 500,
         "help": "The flow, the gate opening and the working pressure are "
                 "one equation, not three settings: the chamber settles "
                 "where the flow divided by the throttled pumping speed puts "
                 "it. If it settles above the setpoint, cut the flow or open "
                 "the gate; the run gives up rather than backfilling for "
                 "ever."},
        {"key": "gate_percent", "label": "Gate valve opening", "type": "number",
         "default": 100, "unit": "%", "min": 0, "max": 100,
         "help": "Leave fully open unless the turbopump is too fast for your "
                 "gas flow to hold the working pressure. Throttling here is "
                 "what lets the rotor stay flat out while the chamber sits "
                 "four decades above its base pressure."},
        {"key": "magnetron_w", "label": "Magnetron power", "type": "number",
         "default": 100, "unit": "W", "min": 0, "max": 2000},
        {"key": "presputter_min", "label": "Pre-sputter (shutter closed)",
         "type": "number", "default": 5, "unit": "min", "min": 0, "max": 240},
        {"key": "deposit_min", "label": "Deposit (shutter open)",
         "type": "number", "default": 20, "unit": "min", "min": 0.1,
         "max": 1440},
        {"key": "max_inlet_mbar", "label": "Abort if pressure exceeds",
         "type": "exponent", "default": 5e-2, "unit": "mbar",
         "min": 1e-4, "max": 1e-1,
         "help": "Protects the turbopump: at full speed it must not be asked "
                 "to swallow more than this continuously."},
        {"key": "settle_min", "label": "Let the pressure settle for",
         "type": "number", "default": 2, "unit": "min", "min": 0, "max": 60},
    )

    def begin(self, engine: "RunEngine") -> None:
        self.working = self.number("working_mbar", 5e-3)
        self.argon = self.number("argon_sccm", 20)
        self.gate_open = self.number("gate_percent", 100)
        self.power = self.number("magnetron_w", 100)
        self.presputter = self.number("presputter_min", 5) * 60.0
        self.deposit_time = self.number("deposit_min", 20) * 60.0
        self.settle = self.number("settle_min", 2) * 60.0
        # Never `or None`: a cleared field arrived as 0, which removed the
        # one limit protecting the spinning turbopump from the gas.
        self.pressure_ceiling = self.number("max_inlet_mbar", 5e-2)
        if not self.pressure_ceiling > 0:
            raise Abort("the pressure ceiling must be above zero",
                        dbmod.FAILED)

        self.deposition_started_at: float | None = None
        self.deposition_ended_at: float | None = None
        self.plasma_started_at: float | None = None
        self._working_since: float | None = None

        if self.working <= 0:
            raise Abort("the argon working pressure must be above zero",
                        dbmod.FAILED)
        if self.pressure_ceiling and self.working >= self.pressure_ceiling:
            raise Abort(
                f"the working pressure {self.working:.2E} mbar is at or above "
                f"the {self.pressure_ceiling:.2E} mbar abort ceiling, so the "
                f"run would stop the moment it reached its own setpoint",
                dbmod.FAILED)

        engine.require_argon_gas_mode()
        self.begin_vacuum(engine)

    def after_base_pressure(self) -> str:
        return "backfill"

    def current_pressure_ceiling(self, sample: dict) -> float | None:
        """The ceiling protects a *spinning* turbopump from too much gas.

        It cannot apply while the chamber is still being roughed out. The
        rough phase legitimately starts at atmosphere, and applying a
        5e-2 mbar ceiling there aborts the run three seconds after it begins
        — which is exactly what happened the first time this ran. Gate it on
        the rotor actually turning, which is the condition the limit is
        really about.
        """
        if self.phase in ("rough", "crossover", "spinup"):
            return None
        # A rotor read at rest is not the pump the ceiling protects; an
        # unread speed is not a rotor at rest -- it used to lift the ceiling
        # for every sample whose speed reply was lost.
        speed = sample.get("speed")
        if isinstance(speed, (int, float)) and speed <= 0:
            return None
        return self.pressure_ceiling

    # -- process phases ----------------------------------------------------

    def phase_backfill(self, engine: "RunEngine", sample: dict) -> str | None:
        """Throttle the pump and let argon in until working pressure holds."""
        # Bounded, because this phase has the gas on. A flow and a gate
        # opening that cannot meet in the middle at the working pressure
        # would otherwise sit here for ever with argon running into the
        # chamber and nobody watching -- the abort path shuts the gas off.
        if self.stage_timeout and self.phase_age(engine) >= self.stage_timeout:
            reading = sample.get("p_best")
            raise Abort(
                f"the chamber did not hold {self.working:.2E} mbar within "
                f"{self.stage_timeout / 60:.0f} min of the argon going on"
                + (f" (it settled at {reading:.2E} mbar)"
                   if isinstance(reading, (int, float)) else "")
                + f". {self.argon:g} sccm through a {self.gate_open:g}% gate "
                  f"does not reach that pressure: change the flow or the "
                  f"throttle and run it again.")
        if not engine.require(engine.hardware.shutter, False):
            return None
        if not engine.require(engine.hardware.gate, self.gate_open):
            return None
        if not engine.require(engine.hardware.gas, self.argon):
            return None

        pressure = sample.get("p_best")
        if not isinstance(pressure, (int, float)):
            return None

        # Within a factor of two of the setpoint counts as "there"; a needle
        # valve set by hand will not land on the number exactly.
        if 0.5 * self.working <= pressure <= 2.0 * self.working:
            if self._working_since is None:
                self._working_since = sample["t"]
                engine.log("info", f"Working pressure {pressure:.2E} mbar "
                                   f"reached, settling")
            elif sample["t"] - self._working_since >= self.settle:
                return "presputter"
        else:
            self._working_since = None
        return None

    def phase_presputter(self, engine: "RunEngine", sample: dict) -> str | None:
        """Clean the target with the shutter closed."""
        if not engine.require(engine.hardware.magnetron, self.power):
            return None
        if self.plasma_started_at is None:
            self.plasma_started_at = sample["t"]
            engine.log("action", f"Plasma on at {self.power:g} W, shutter "
                                 f"closed — pre-sputtering the target")
        if self.in_phase >= self.presputter:
            return "deposit"
        return None

    def phase_deposit(self, engine: "RunEngine", sample: dict) -> str | None:
        """Open the shutter; this is the part that makes the film."""
        if not engine.require(engine.hardware.shutter, True):
            return None
        if self.deposition_started_at is None:
            self.deposition_started_at = sample["t"]
            engine.log("action", "Shutter open — deposition started")
        if self.in_phase >= self.deposit_time:
            self.deposition_ended_at = sample["t"]
            engine.log("action", "Deposition time complete")
            return "recover"
        return None

    def phase_recover(self, engine: "RunEngine", sample: dict) -> str | None:
        """Shut the process down in the safe order and pump back out."""
        if not engine.require(engine.hardware.shutter, False):
            return None
        if not engine.require(engine.hardware.magnetron, 0):
            return None
        if not engine.require(engine.hardware.gas, 0):
            return None
        if not engine.require(engine.hardware.gate, 100):
            return None
        # A minute of pumping with the gas off, so the run's last samples
        # record the chamber actually recovering rather than a guess.
        if self.in_phase >= 60:
            return "done"
        return None

    def finish(self, engine: "RunEngine") -> bool:
        """However this ended, the plasma and the gas must not be left on."""
        return engine.hardware.safe_shutdown(log=engine.log)

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        summary = self.vacuum_summary()
        during = [s for s in samples
                  if s.get("phase") == "deposit"
                  and isinstance(s.get("p_best"), (int, float))]
        pressures = [s["p_best"] for s in during]

        deposited = None
        if self.deposition_started_at is not None:
            end = self.deposition_ended_at or (samples[-1]["t"] if samples else 0)
            deposited = end - self.deposition_started_at

        summary.update({
            "working_mbar_setpoint": self.working,
            "working_mbar_actual": statistics(pressures) if pressures else {},
            "argon_sccm": self.argon,
            "gate_percent": self.gate_open,
            "magnetron_w": self.power,
            "presputter_s": self.presputter,
            "deposition_s": deposited,
            "deposition_complete": self.deposition_ended_at is not None,
            "duration_s": samples[-1]["t"] if samples else 0,
        })
        if pressures:
            spread = (max(pressures) - min(pressures)) / self.working
            summary["working_pressure_spread"] = spread
            if spread > 0.5:
                summary["caveat"] = (
                    f"The working pressure wandered by {spread * 100:.0f}% of "
                    f"its setpoint during deposition. Film uniformity and "
                    f"composition depend on it holding steady — check the gas "
                    f"flow and the gate valve throttle.")
        if not summary["deposition_complete"]:
            summary.setdefault(
                "caveat", "The run ended before the deposition step finished, "
                          "so this sample did not receive its full dose.")
        return summary



class FullVacuum(VacuumSequence):
    """Pump the chamber out and hold it there until somebody says stop.

    The rig's everyday state, and the only procedure with no ending of its
    own. It follows the same route as every other recipe -- rough, cross over
    on pressure, spin up, find base pressure -- and then simply keeps going,
    recording the whole time, for as long as the chamber is wanted under
    vacuum. Reaching base pressure is noted in the log and in the summary but
    does not end anything.

    Two consequences of "unlimited" are deliberate:

    * **The base-pressure search has no deadline.** `base_timeout_min` is 0
      here. A chamber that takes six hours to flatten is still doing the
      right thing, and this run has nowhere to be.
    * **The approach phases keep theirs.** Roughing, the turbopump start and
      spin-up are bounded, because a rig that cannot get to vacuum at all is
      failing rather than working slowly, and it should say so instead of
      sitting at atmosphere overnight looking busy.

    Stopping it asks what to leave behind: the chamber under vacuum, or the
    pump powered down in the order the hardware needs -- motor off, rotor to
    rest with the backing pump still running, then the station. It never
    powers down by itself.
    """

    name = "full_vacuum"
    label = "Full vacuum (hold until stopped)"

    #: Stopping it is how this one ends. Every other procedure runs out of
    #: its own accord, so a stop there is genuinely an abort.
    stop_is_completion = True

    #: This run may last for days, so it samples more slowly than a recipe
    #: that lasts an hour, and gives the approach phases longer before it
    #: calls them stalled. Both are referenced by the fields below, so the
    #: form and the fallback cannot disagree.
    DEFAULT_INTERVAL_S = 5.0
    DEFAULT_STAGE_TIMEOUT_MIN = 60.0
    description = ("Starts the forevacuum pump, crosses over to the "
                   "turbopump at the crossover pressure, runs up to full "
                   "speed and then holds the chamber under vacuum "
                   "indefinitely, recording pressure, rotor speed, drive "
                   "current, voltage, power and temperature throughout. "
                   "When you stop it you choose: keep the vacuum, or power "
                   "the pump down in the correct order.")
    phases = ("rough", "crossover", "spinup", "basepressure", "holding",
              "done")

    #: The rotor is held at final rotation speed here for the ordinary
    #: reason, not the sputtering one: a turbopump has one working speed.
    full_speed_reason = ("a turbopump has one working speed, and this run "
                         "holds the chamber under vacuum")

    fields = (
        {"key": "crossover_mbar", "label": "Start the turbopump at",
         "type": "exponent", "default": VacuumSequence.DEFAULT_CROSSOVER_MBAR,
         "unit": "mbar", "min": 1e-4, "max": 1000,
         "help": "The forevacuum pump works alone until the chamber reaches "
                 f"this. {TURBO_START_MAX_MBAR:.0E} mbar is the highest the "
                 "interlock will allow; asking for more is lowered to it."},
        {"key": "base_rate_pct", "label": "Call it base pressure below",
         "type": "number", "default": VacuumSequence.DEFAULT_BASE_RATE_PCT,
         "unit": "%/min", "min": 0.1, "max": 50,
         "help": "Only a note in the log and the summary — this run does not "
                 "end when the chamber stops improving, it keeps pumping."},
        {"key": "base_settle_min", "label": "…sustained for", "type": "number",
         "default": VacuumSequence.DEFAULT_BASE_SETTLE_MIN, "unit": "min",
         "min": 0.5, "max": 120},
        {"key": "stage_timeout_min",
         "label": "Give up if roughing or spin-up stalls after",
         "type": "number", "default": DEFAULT_STAGE_TIMEOUT_MIN, "unit": "min",
         "min": 1, "max": 1440,
         "help": "Bounds getting to vacuum, never the holding itself. A rig "
                 "that cannot rough down or spin up is failing, not working "
                 "slowly."},
        {"key": "max_temp_c", "label": "Abort above a pump temperature of",
         "type": "number", "default": 70, "unit": "°C", "min": 0, "max": 200,
         "help": "0 turns this off. The drive's own over-temperature flags "
                 "abort the run whatever this says."},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": DEFAULT_INTERVAL_S, "unit": "s", "min": 1, "max": 60,
         "help": "5 s rather than 2 s: this run may last for days, and a "
                 "day at 2 s is 43 000 rows."},
    )

    def begin(self, engine: "RunEngine") -> None:
        self.max_temp = self.number("max_temp_c", 70)
        self.holding_since: float | None = None
        self.ultimate_mbar: float | None = None
        self.peak_temp: float | None = None
        self.peak_current: float | None = None
        self.begin_vacuum(engine)
        # No deadline on the search for base pressure: this run has nowhere
        # to be, and a slow chamber is not a failed one.
        self.base_timeout = 0.0

    def after_base_pressure(self) -> str:
        return "holding"

    # -- the unlimited phase ----------------------------------------------

    def phase_holding(self, engine: "RunEngine", sample: dict) -> str | None:
        """Keep pumping, keep recording. Nothing here ever ends the run."""
        if self.holding_since is None:
            self.holding_since = sample["t"]
            engine.log("info",
                       f"Holding under vacuum. The chamber stays here, and "
                       f"the run keeps recording, until you stop it.")
        # Keep quoting the rate so the panel shows whether it is still
        # improving, drifting up, or genuinely flat.
        now = sample["t"]
        pressure = sample.get("p_best")
        if isinstance(pressure, (int, float)) and pressure > 0:
            self._window.append((now, pressure))
            while self._window and now - self._window[0][0] > self.rate_window:
                self._window.popleft()
        sample["rate_pct_min"] = relative_rate(list(self._window),
                                               self.rate_window)
        return None

    # -- watching, in every phase -----------------------------------------

    def guard(self, engine: "RunEngine", sample: dict) -> None:
        self._watch_temperature(engine, sample)
        super().guard(engine, sample)

    def _watch_temperature(self, engine: "RunEngine", sample: dict) -> None:
        """Abort on the drive's own over-temperature flags, or on a limit.

        A run that lasts days is a run nobody is watching, so the conditions
        an operator would notice have to be watched here. The flags come from
        the drive rather than from a sample column: they are booleans no
        chart wants, and `engine.pump_status()` is the way to reach them.
        """
        for value in (sample.get("p_best"),):
            if isinstance(value, (int, float)):
                if self.ultimate_mbar is None or value < self.ultimate_mbar:
                    self.ultimate_mbar = value

        temperature = sample.get("tms_temp")
        if isinstance(temperature, (int, float)):
            if self.peak_temp is None or temperature > self.peak_temp:
                self.peak_temp = temperature
        current = sample.get("current")
        if isinstance(current, (int, float)):
            if self.peak_current is None or current > self.peak_current:
                self.peak_current = current

        if self.max_temp and isinstance(temperature, (int, float)) \
                and temperature > self.max_temp:
            raise Abort(f"the pump reached {temperature:.0f} °C, above the "
                        f"{self.max_temp:.0f} °C limit set for this run",
                        pump_trouble=True)

    # -- ending ------------------------------------------------------------

    def finish(self, engine: "RunEngine") -> None:
        """Leave the pump running unless the operator asked to power down.

        This used to power the rig down on every ending, errors included --
        and a station switched off stops the forevacuum pump, so a fault at
        night meant a chamber that had leaked back up by the morning.
        Now only an operator who stops the run and chooses "power down" gets
        a power-down (the engine starts it from `stop()`). An ending on a
        fault is handled by the engine instead: motor off, fault cleared,
        forevacuum pump kept running.
        """
        if engine.stop_choice() is False:
            engine.log("info", "Run stopped with the vacuum left as it is, "
                               "as asked. The pump is still running.")

    def summarise(self, engine: "RunEngine", samples: list[dict]) -> dict:
        pressures = [s["p_best"] for s in samples
                     if isinstance(s.get("p_best"), (int, float))]
        summary = self.vacuum_summary()
        held = None
        if self.holding_since is not None and samples:
            held = samples[-1]["t"] - self.holding_since
        summary.update({
            "ultimate_mbar": min(pressures) if pressures else None,
            "final_mbar": pressures[-1] if pressures else None,
            "held_s": held,
            "duration_s": samples[-1]["t"] if samples else 0,
            "peak_temperature_c": self.peak_temp,
            "peak_current_a": self.peak_current,
            "pressure": statistics(pressures),
            "powered_down": engine.stop_choice() is True,
        })
        # While holding, the pressure should be flat. A chamber that drifts
        # up over hours is leaking or outgassing, and that is the whole
        # reason to leave this running.
        holding = [s["p_best"] for s in samples
                   if s.get("phase") == "holding"
                   and isinstance(s.get("p_best"), (int, float))]
        if len(holding) >= 3 and holding[0] > 0:
            drift = (holding[-1] - holding[0]) / holding[0]
            summary["holding_drift_fraction"] = drift
            if drift > 0.5:
                summary["caveat"] = (
                    f"The pressure rose {drift * 100:.0f}% while holding, "
                    f"from {holding[0]:.2E} to {holding[-1]:.2E} mbar. That "
                    f"is a leak or an outgassing load, not a settled "
                    f"chamber — run a rate-of-rise test.")
        return summary


CATALOGUE: dict[str, type[Procedure]] = {
    procedure.name: procedure
    for procedure in (Deposition, BaseCalibration, FullVacuum, PumpDown,
                      RateOfRise, Hold, Bakeout)
}


def _finite(value) -> float | None:
    """A number from a form or an API call, or None if it is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def settle_config(procedure: type[Procedure], given) -> tuple[dict, list[str]]:
    """The configuration a run will use, and what is wrong with the one given.

    A field left out takes its published default. A field given must be the
    right kind of value inside the field's own range. The form used to be
    the only place those ranges lived, and a field cleared on it arrived as
    0 -- which is "no limit" to a timeout and "no ceiling" to the pressure
    that protects a spinning turbopump. Keys no field declares are dropped,
    so the stored configuration is the one that ran.
    """
    given = given if isinstance(given, dict) else {}
    config: dict = {}
    problems: list[str] = []
    for field in procedure.fields:
        key, kind = field["key"], field.get("type", "number")
        label = field.get("label", key).lstrip("…")
        value = given.get(key)
        if value is None:
            config[key] = field.get("default")
        elif kind == "boolean":
            config[key] = (value.strip().lower() in ("1", "true", "yes", "on")
                           if isinstance(value, str) else bool(value))
        elif kind == "select":
            options = [str(option["value"]) for option in field.get("options", [])]
            if str(value) in options:
                config[key] = str(value)
            else:
                problems.append(f'"{label}": {value!r} is not one of '
                                f"{', '.join(options)}")
        elif kind in ("number", "exponent"):
            number = _finite(value)
            low, high = field.get("min"), field.get("max")
            unit = f" {field['unit']}" if field.get("unit") else ""
            if number is None:
                problems.append(f'"{label}" needs a number'
                                + (" -- it was left empty"
                                   if str(value).strip() == "" else
                                   f", not {value!r}"))
            elif (low is not None and number < low) or \
                    (high is not None and number > high):
                bound = (f"between {low:g} and {high:g}" if None not in (low, high)
                         else f"at least {low:g}" if low is not None
                         else f"at most {high:g}")
                problems.append(f'"{label}" must be {bound}{unit}; it was '
                                f"{number:g}")
            else:
                config[key] = number
        else:
            config[key] = value
    if not problems:
        # Fields that are each in range can still not work together.
        problems.extend(procedure.config_problems(config))
    return config, problems


def not_installed(label: str, missing: list[str]) -> str:
    """Why a procedure cannot start: what it needs that is not on the rig."""
    plural = len(missing) > 1
    names = [name[:1].lower() + name[1:] for name in missing]
    listed = names[0] if not plural else f"{', '.join(names[:-1])} and {names[-1]}"
    return (f"{label} needs the {listed}, which "
            f"{'are' if plural else 'is'} not installed on this rig yet. Once "
            f"{'they are' if plural else 'it is'} in place, mark "
            f"{'them' if plural else 'it'} installed in Settings → Rig equipment.")


def catalogue() -> list[dict]:
    """Describe every procedure, so the UI can build its forms from the API."""
    return [
        {"name": p.name, "label": p.label, "description": p.description,
         "phases": list(p.phases), "fields": [dict(f) for f in p.fields],
         "requires": list(p.requires)}
        for p in CATALOGUE.values()
    ]


# --- the engine -----------------------------------------------------------


class RunEngine:
    """Runs one procedure at a time against the registered devices."""

    #: Seconds without contact before a run is abandoned: long enough to ride
    #: out a reconnect, short enough that nothing records a flat line for
    #: hours. Counted in seconds, not samples -- thirty samples was half an
    #: hour for a run sampling once a minute.
    MAX_SILENT_S = 60.0

    #: The safety tick. Abort conditions and each procedure's `guard` run
    #: this often -- on new readings -- whatever the procedure's sample
    #: interval, which only decides how often a sample is stored and the
    #: procedure moves on. A condition found on one reading ends the run
    #: within about two seconds at any interval.
    TICK_S = 1.0

    #: Give up on an unattended shutdown after this long. A rotor that will
    #: not coast to rest, or a link that has gone away, must not leave a
    #: thread waiting for ever -- but giving up leaves the backing pump
    #: *running*, because the one thing worse than an unfinished shutdown is
    #: a spinning rotor with no backing.
    SHUTDOWN_TIMEOUT_S = 1800.0

    #: How often the shutdown sequence re-asks whether it may take the next
    #: step. The rotor coasts for minutes; there is nothing to gain from
    #: asking faster than the pump's own sampler answers.
    SHUTDOWN_POLL_S = 2.0

    #: An owed clean-up -- a run cut off by a crash or a power cut, or one
    #: whose clean-up failed as it ended: how often it looks for the pump
    #: (there is no deadline -- a run cannot start without the pump anyway),
    #: and, once the pump answers, how many attempts a minute apart before it
    #: is left owed to the next start.
    ORPHAN_POLL_S = 2.0
    ORPHAN_RETRY_S = 60.0
    ORPHAN_TRIES = 5

    def __init__(self, database: "dbmod.Database", registry,
                 hardware: "process.ProcessHardware | None" = None,
                 pump_key: str = "pump", gauge_key: str = "gauge",
                 equipment: "process.Equipment | None" = None,
                 chamber: str | None = None):
        self.db = database
        self.registry = registry
        #: Recorded on every run, so two rigs' records stay apart.
        self.chamber = chamber
        self.hardware = hardware or process.ProcessHardware()
        # What is on the rig at all. Without a record, everything is: that is
        # the simulator, and the tests, where every actuator exists.
        self.equipment = equipment or process.Equipment(default=True)
        if self.equipment.hardware is None:
            self.equipment.hardware = self.hardware
        self.pump_key = pump_key
        self.gauge_key = gauge_key

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stop_interrupted = False
        #: Database writes that failed, for the supervisor's alarm: a full or
        #: worn-out SD card must cost the record, never the run's clean-up.
        self.storage_error: dict | None = None
        self._storage_lock = threading.Lock()
        #: The run's writes go to one writer thread through this queue, in
        #: order, so the safety tick never waits on the database -- whose
        #: lock a download holds through a backup and a VACUUM.
        self._writes: queue.Queue = queue.Queue()
        threading.Thread(target=self._writer, name="run-writer",
                         daemon=True).start()
        #: Runs whose clean-up is owed -- found at startup, or failed as a run
        #: ended -- and what has been done for each; tried on their own thread.
        self.orphans: list[dict] = []
        self._orphan_thread: threading.Thread | None = None
        self._orphan_lock = threading.Lock()
        #: Set by close(): nothing is started or tried again from then on.
        self._closing = threading.Event()
        self._procedure: Procedure | None = None
        self._run: dict | None = None
        self._samples: list[dict] = []
        #: Since when an instrument has been out of contact, monotonic.
        self._silent_since: float | None = None
        #: When the run's safety tick last ran, monotonic; None between runs
        #: and while a procedure is still beginning.
        self._last_tick: float | None = None
        #: When an ending run began its clean-up, monotonic.
        self._cleanup_since: float | None = None

        #: Progress of a guided power-down, or None if none has been asked
        #: for. Kept after it finishes so the panel can show the outcome.
        self._shutdown: dict | None = None
        self._shutdown_thread: threading.Thread | None = None
        self._cancel_shutdown = threading.Event()
        #: What the operator chose when they stopped the run: True to power
        #: the pump down, False to leave the vacuum as it is, None if the run
        #: ended on its own. A procedure that powers down when it finishes
        #: must not override an operator who explicitly asked to keep it.
        self._stop_shutdown: bool | None = None

        #: Progress of the safe-state sequence after a pump-trouble ending,
        #: or None. Kept after it finishes so the panel can say what was done.
        self._secure: dict | None = None
        #: The make-safe sequence under way, and the lock it is started
        #: under: a press of Make safe as a run ends in pump trouble must not
        #: start a second one beside it.
        self._secure_thread: threading.Thread | None = None
        self._secure_start = threading.Lock()
        #: How the most recent run ended, so the panel can keep showing it --
        #: and its live aftermath -- instead of snapping back to a blank form.
        self._last: dict | None = None

        #: Run mode, and the manual step currently blocking progress.
        self.mode = GUIDED
        self._prompt: dict | None = None
        #: The value each actuator was last confirmed at, so `require` asks
        #: once per step rather than once per sample.
        #:
        #: Keyed by actuator, holding the *current* value -- not a set of
        #: (actuator, value) pairs it has ever seen. With a set, a recipe that
        #: closes the shutter, opens it to deposit, then closes it again finds
        #: the final close already in the set and skips it, leaving the
        #: shutter open over the sample for the whole recovery. Found exactly
        #: that way in the first end-to-end simulated deposition.
        self._satisfied: dict[str, str] = {}

        self._close_orphans()

    # --- process hardware -------------------------------------------------

    def require(self, actuator, value, note: str = "") -> bool:
        """Ask for an actuator state. True once it is actually in effect.

        Returns False while a human is still needed, and the caller stays in
        its phase. Deliberately non-blocking: the sampler keeps running and
        the abort checks keep firing while somebody walks to the chamber,
        which is exactly when a plasma-on chamber must not go unwatched.
        """
        want = repr(value)
        if self._satisfied.get(actuator.key) == want:
            return True

        try:
            actuator.set(value)
        except process.OperatorRequired as prompt:
            if self.mode == AUTOMATIC:
                raise Abort(
                    f"this run was set to automatic, but {actuator.label} is "
                    f"not under software control yet. Run it in guided mode, "
                    f"or wire the hardware up.", dbmod.FAILED)
            if (not self._prompt or self._prompt.get("actuator") != actuator.key
                    or self._prompt.get("want") != want):
                self._prompt = {
                    "actuator": actuator.key,
                    "want": want,
                    "label": actuator.label,
                    "instruction": prompt.instruction,
                    "note": note,
                    "value": actuator.format(value),
                    "since": time.time(),
                }
                self.log("action", f"MANUAL STEP: {prompt.instruction}")
            return False
        except process.ActuatorError as exc:
            raise Abort(str(exc), dbmod.FAILED)

        self._satisfied[actuator.key] = want
        self.log("action", f"{actuator.label} set to {actuator.format(value)}")
        return True

    def acknowledge(self, note: str = "") -> dict:
        """Operator says the manual step is done; the recipe may continue."""
        if not self._prompt:
            return {"ok": False, "error": "nothing is waiting on you"}
        pending = self._prompt
        self._satisfied[pending["actuator"]] = pending["want"]
        try:
            self.hardware.get(pending["actuator"]).confirm()
        except process.ActuatorError:
            pass
        self._prompt = None
        self.log("action",
                 f"Operator confirmed: {pending['instruction']}"
                 + (f" ({note})" if note else ""))
        return {"ok": True, "acknowledged": pending["instruction"]}

    def prompt(self) -> dict | None:
        return self._prompt

    # --- pump policy ------------------------------------------------------

    def enforce_full_speed(self, why: str) -> None:
        """Put the drive unit in final-rotation-speed mode and lock it there.

        This rig deposits in argon, so the turbopump runs flat out for the
        whole run: throttling is the gate valve's job. Dropping the rotor to
        hold pressure would cost compression exactly when the gas load is
        highest.
        """
        result = self.pump_param("OpModeTMP", 0)
        if result.get("ok"):
            self.log("action", f"Turbopump locked to full speed — {why}")
        else:
            self.log("warning", f"Could not select final-speed mode: "
                                f"{result.get('error')}")
        # Final-speed mode does not leave standby, and standby holds the rotor
        # at its standby speed: a run started in standby would read its own
        # rotor as dragged down by gas.
        left = self.pump_command("standby_off")
        if not left.get("ok"):
            self.log("warning", f"Could not take the pump out of standby: "
                                f"{left.get('error')}")
        pump = self.pump()
        if pump is not None:
            pump.lock_full_speed(why)

    def require_argon_gas_mode(self) -> None:
        """Tell the drive unit it is pumping a heavy gas.

        Parameter 27 selects the gas: 0 heavy, 1 light, 2 helium. Argon is
        heavy, and the setting changes how the drive unit interprets its own
        load protection. Getting it wrong is not dangerous, but it makes the
        pump's power limits fit the wrong gas.
        """
        result = self.pump_param("GasMode", 0)
        self.log("action" if result.get("ok") else "warning",
                 "Gas mode set to heavy gases (argon)" if result.get("ok")
                 else f"Could not set the gas mode: {result.get('error')}")

    # --- history ----------------------------------------------------------

    def run_id(self) -> int | None:
        run = self._run                          # read once: _forget may clear it
        return run["id"] if run else None

    def runner(self) -> str | None:
        """The account that started the run in progress, or None.

        lab/auth.py gives that person a longer idle limit while it goes.
        """
        run = self._run
        if run is None or not (self._thread and self._thread.is_alive()):
            return None
        return run.get("started_by")

    def last_base_pressure(self, exclude_run: int | None = None) -> float | None:
        """Base pressure from the most recent completed calibration."""
        for record in self.db.runs(procedure="base_calibration",
                                   status=dbmod.COMPLETED, limit=10):
            if exclude_run is not None and record["id"] == exclude_run:
                continue
            value = (record.get("result") or {}).get("base_mbar")
            if isinstance(value, (int, float)) and value > 0:
                return value
        return None

    def _close_orphans(self) -> None:
        """Close out runs left open by a crash or a power cut -- and make
        their hardware safe.

        A run still marked running belongs to a process that died without
        its clean-up: a bake-out's heater may still be on, a deposition's
        argon still flowing. Each is closed as failed, its procedure's own
        clean-up (`finish`, as for any ending) is run once the pump answers,
        and the supervisor raises an alarm for each. So is a run that ended
        properly but whose clean-up did not succeed -- while the panel ran,
        or as a restart stopped it: it is owed exactly as a cut-off one is.
        """
        remember = self._remember
        for run in self.db.orphaned_runs():
            # Marked as still owing its clean-up until that has run against
            # a pump that answered: a second restart before then must find
            # it again, or a heater stays on with nobody to switch it off.
            self.db.finish_run(
                run["id"], dbmod.FAILED,
                {"error": "the control process stopped while this run was "
                          "in progress", "cleanup_pending": True,
                 "cut_off": True})
            self.db.add_event(run["id"], "error",
                              "Run abandoned: the control process stopped")
            remember(run, "waiting for the pump", None, "crash")
        for run in self.db.runs_owing_cleanup():
            if run["id"] not in {o["id"] for o in self.orphans}:
                cut_off = (run.get("result") or {}).get("cut_off")
                remember(run, "waiting for the pump", None,
                         "crash" if cut_off else "unfinished")
        for run in self.db.runs(status=dbmod.ABORTED, limit=5):
            result = run.get("result") or {}
            if result.get("interrupted") and not result.get("reported"):
                if run["id"] not in {o["id"] for o in self.orphans}:
                    remember(run, "the panel was stopped (a restart or a "
                                  "shutdown) while it was going; its clean-up "
                                  "ran then", True, "restart")
                self.db.set_result(run["id"], {**result, "reported": True})
        self._start_cleanups()

    def _remember(self, run: dict, state: str, ok, kind: str) -> dict:
        """List a run whose clean-up is owed, or done, for the supervisor.

        `kind`: "crash" (cut off), "unfinished" (its clean-up failed when it
        ended, found at a start), "restart" (ended by a restart, clean-up
        done) or "retry" (its clean-up failed as it ended, just now).
        """
        procedure = CATALOGUE.get(run.get("procedure"))
        entry = {
            "id": run["id"], "name": run.get("name"),
            "procedure": run.get("procedure"),
            "label": procedure.label if procedure else run.get("procedure"),
            "config": run.get("config") or {}, "kind": kind,
            "interrupted": bool((run.get("result") or {}).get("interrupted")),
            "state": state, "ok": ok, "attempts": 0}
        with self._orphan_lock:
            self.orphans.append(entry)
        return entry

    def _start_cleanups(self) -> None:
        """Try the owed clean-ups on their own thread, starting it if needed."""
        with self._orphan_lock:
            if self._closing.is_set():
                return
            if self._orphan_thread is not None and self._orphan_thread.is_alive():
                return
            if not any(o["ok"] is None for o in self.orphans):
                return
            self._orphan_thread = threading.Thread(
                target=self._clean_up_orphans, name="orphan-cleanup",
                daemon=True)
            self._orphan_thread.start()

    def _clean_up_orphans(self) -> None:
        """Run each owed run's own clean-up once the pump answers.

        Kept at until it works: a pump that comes back fifteen minutes after
        the PC must still get its heater switched off without another
        restart, and so must one whose link dropped as a bake-out ended.
        `start()` refuses meanwhile, so nothing this does can touch a run
        started since. Each run has its own count of attempts.
        """
        pump = self.pump()
        while not self._closing.is_set():
            with self._orphan_lock:
                owed = [o for o in self.orphans if o["ok"] is None]
                if not owed:
                    self._orphan_thread = None
                    return
            if pump is not None and not pump.snapshot().get("connected"):
                self._closing.wait(self.ORPHAN_POLL_S)
                continue
            for orphan in owed:
                if self._closing.is_set():
                    break
                orphan["attempts"] += 1
                self._clean_up_one(orphan, orphan["attempts"],
                                   orphan["attempts"] >= self.ORPHAN_TRIES)
            if any(orphan["ok"] is None for orphan in owed):
                self._closing.wait(self.ORPHAN_RETRY_S)
        with self._orphan_lock:
            if self._orphan_thread is threading.current_thread():
                self._orphan_thread = None

    def _clean_up_one(self, orphan: dict, attempt: int, last: bool) -> None:
        procedure = CATALOGUE.get(orphan["procedure"])
        if procedure is None:
            done, why = True, ("no clean-up is known for this procedure; check "
                               "the rig by eye")
        else:
            try:
                done = procedure(orphan["config"]).finish(self) is not False
                why = ("its clean-up was run" if done else
                       "its clean-up did not succeed")
            except Exception as exc:              # never lose the thread
                done, why = False, f"its clean-up failed: {exc}"
        if done:
            orphan.update(ok=True, state=why)
        elif last:
            orphan.update(ok=False, state=f"{why} after {attempt} attempts; "
                                          f"the panel tries again at its next "
                                          f"start")
        else:
            orphan["state"] = f"{why} yet; trying again in a minute"
            return
        # Bound now, not when the writer gets to them: the loop moves on.
        run_id, state, ok, at = orphan["id"], orphan["state"], orphan["ok"], \
            time.time()
        when = ("Clean-up tried again" if orphan.get("kind") == "retry"
                else "After the restart")
        self._write(lambda: self.db.add_event(
            run_id, "action" if ok else "error", f"{when}: {state}.", at=at))
        if ok:
            # Owed no longer: the next start has nothing to repeat.
            self._write(lambda: self._settle_cleanup(run_id))

    def _settle_cleanup(self, run_id: int) -> None:
        record = self.db.run(run_id)
        if record:
            self.db.set_result(run_id, {**(record.get("result") or {}),
                                        "cleanup_pending": False})

    def _pump_not_ready(self) -> str | None:
        """Why no run may start on the pump as it is now, or None."""
        pump = self.pump()
        if pump is None:
            return None
        snap = pump.snapshot()
        if not snap.get("connected"):
            return (f"no contact with the pump ({snap.get('error') or 'offline'}). "
                    f"A run cannot start on a drive it cannot read.")
        code = snap.get("status", {}).get("ErrorCode")
        if code is None:
            return ("the drive's error code did not come back in the last "
                    "reading, so whether a fault is latched is not known. "
                    "Try again in a moment.")
        if str(code).strip() not in pumpmod.NO_FAULT:
            title = pumpmod.describe_error(code).get("title", "drive fault")
            return (f"the drive has fault {str(code).strip()} latched "
                    f"({title.lower()}). Find the cause and use Make safe on "
                    f"the pump page, then start the run.")
        return None

    # --- device access ----------------------------------------------------

    def pump(self):
        return self.registry.get(self.pump_key) if self.pump_key in self.registry \
            else None

    def gauge(self):
        return self.registry.get(self.gauge_key) if self.gauge_key in self.registry \
            else None

    def pump_command(self, action: str, value=None) -> dict:
        pump = self.pump()
        if pump is None:
            return {"ok": False, "error": "no pump is configured"}
        return pump.command(action, value)

    def pump_param(self, name: str, value) -> dict:
        pump = self.pump()
        if pump is None:
            return {"ok": False, "error": "no pump is configured"}
        return pump.command("param", {"name": name, "value": value})

    def nominal_speed(self) -> float | None:
        """The rotor's full speed in Hz; None only with no pump configured.

        The pump service never leaves it unknown (see
        `PumpService.nominal_speed`): every check against full speed used to
        switch itself off for a whole run after one lost reply at connect.
        """
        pump = self.pump()
        if pump is None:
            return None
        if hasattr(pump, "nominal_speed"):
            return pump.nominal_speed()
        value = pump.snapshot().get("identity", {}).get("NominalSpd")
        return value if isinstance(value, (int, float)) else None

    # --- what this rig can run ---------------------------------------------

    def catalogue(self) -> list[dict]:
        """The procedures, each saying whether this rig can run it now."""
        entries = catalogue()
        for entry in entries:
            requires = CATALOGUE[entry["name"]].requires
            missing = self.equipment.missing(requires)
            entry["available"] = not missing
            entry["missing"] = missing
            entry["automatic_ready"] = (not missing
                                        and not self.hardware.missing(requires))
        return entries

    def unavailable(self) -> dict:
        """Procedure name -> what it is missing, for those that cannot run."""
        return {name: missing for name, procedure in CATALOGUE.items()
                if (missing := self.equipment.missing(procedure.requires))}

    def actuators(self) -> list[dict]:
        return [{**described, "installed": self.equipment.installed(described["key"])}
                for described in self.hardware.describe()]

    def equipment_report(self) -> list[dict]:
        rows = self.equipment.describe()
        for row in rows:
            row["used_by"] = [p.label for p in CATALOGUE.values()
                              if row["key"] in p.requires]
        return rows

    def set_equipment(self, key: str, installed: bool, user=None) -> dict:
        """Mark a piece of equipment installed or not, from the Settings page.

        `user` is whoever asked (lab/auth.py's Principal), for the record.
        """
        procedure = self._procedure
        if (not installed and procedure is not None and self._thread
                and self._thread.is_alive() and key in procedure.requires):
            return {"ok": False, "error": f"{procedure.label} is running and "
                                          "uses it; stop the run first"}
        result = self.equipment.set(key, installed)
        if result.get("ok"):
            label = process.EQUIPMENT[key]
            # The rig's log, not a run's: this is not something a run did.
            self.db.add_event(
                None, "action",
                (f"{user.name}: " if user else "") + f"Rig equipment: {label} "
                f"marked {'installed' if installed else 'not installed'}",
                user=getattr(user, "username", None))
            result["equipment"] = self.equipment_report()
        return result

    # --- logging ----------------------------------------------------------

    def log(self, level: str, message: str, detail: dict | None = None) -> None:
        run = self._run                          # read once: _forget may clear it
        run_id = run["id"] if run else None
        # The run's own log is what its own thread writes. Everything else
        # the engine files under a run -- a power-down, which goes ahead
        # after a minute even if the run's ending is still stuck, a clean-up
        # tried again -- is the rig's, kept if that run is deleted.
        origin = "run" if threading.current_thread() is self._thread else None
        # Stamped now: the writer may get to it seconds later.
        at = time.time()
        self._write(lambda: self.db.add_event(run_id, level, message, detail,
                                              at=at, origin=origin),
                    f"{level}: {message}")

    def _write(self, write, what: str = "") -> None:
        """Queue one database write that must not end anything if it fails.

        Done in order on the writer thread. A failed write is counted for the
        supervisor's alarm and said on stderr, and nobody waits for it: what
        the run is doing to the hardware matters more than what it manages to
        write down, and when.
        """
        self._writes.put((write, what, None))

    def _flush(self, timeout: float = 15.0) -> bool:
        """Wait until every write queued so far is done."""
        done = threading.Event()
        self._writes.put((None, "", done))
        return done.wait(timeout)

    def _writer(self) -> None:
        while True:
            write, what, done = self._writes.get()
            if done is not None:
                done.set()
                continue
            try:
                write()
            except Exception as exc:              # never lose the thread
                self.note_write_failure(exc, what)

    def note_write_failure(self, exc: Exception, what: str = "") -> None:
        """Count a database write that failed, for the supervisor's `storage`
        alarm -- the run's, or one the panel itself could not make."""
        with self._storage_lock:
            failed = self.storage_error or {"count": 0}
            self.storage_error = {"count": failed["count"] + 1,
                                  "error": str(exc), "at": time.time()}
        if failed["count"] == 0 or failed["count"] % 100 == 0:
            sys.stderr.write(f"  !! database write failed ({exc})"
                             + (f": {what[:120]}" if what else "") + "\n")

    # --- lifecycle --------------------------------------------------------

    def start(self, procedure_name: str, config: dict, name: str = "",
              operator: str = "", notes: str = "",
              started_by: str | None = None) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False,
                        "error": "a run is already in progress; stop it first"}
            if procedure_name not in CATALOGUE:
                return {"ok": False,
                        "error": f"unknown procedure {procedure_name!r}"}
            absent = self.equipment.missing(CATALOGUE[procedure_name].requires)
            if absent:
                return {"ok": False, "error": not_installed(
                    CATALOGUE[procedure_name].label, absent)}
            config, problems = settle_config(CATALOGUE[procedure_name], config)
            if problems:
                return {"ok": False, "problems": problems,
                        "error": "The run was not started: "
                                 + "; ".join(problems) + "."}
            # A procedure's `begin` starts the station and selects the drive's
            # modes before the first abort check looks at anything, so a drive
            # that cannot be read, or has a fault latched, is refused here.
            refusal = self._pump_not_ready()
            if refusal:
                return {"ok": False, "error": refusal}
            # A power-down still bringing the rotor to rest must not run
            # alongside a new recipe: it would stop the station whenever the
            # rotor reached 0 Hz, in the middle of the new run. That is what
            # happened between runs 25 and 26 on 2026-10-08.
            if self._shutdown_thread and self._shutdown_thread.is_alive():
                return {"ok": False,
                        "error": "the pump is still being powered down. Wait "
                                 "until the rotor is at rest, or cancel the "
                                 "power-down on the Pump page, then start "
                                 "the run."}
            if self._orphan_thread and self._orphan_thread.is_alive():
                waiting = "; ".join(f"run {o['id']}: {o['state']}"
                                    for o in self.orphans if o["ok"] is None)
                return {"ok": False,
                        "error": f"the panel is still trying the clean-up of "
                                 f"an earlier run ({waiting}). It tries "
                                 f"{self.ORPHAN_TRIES} times once the pump "
                                 f"answers; if that fails, an alarm asks for "
                                 f"the rig to be checked by hand -- do that "
                                 f"before starting a run."}
            if self._closing.is_set():
                return {"ok": False, "error": "the panel is shutting down"}

            self._stop_shutdown = None
            self._stop_interrupted = False
            # A finished power-down is kept only so the panel can report the
            # outcome. Starting a new run makes it history: the rig is
            # plainly running again, and a stale "rotor at rest, station off"
            # is worse than no message at all.
            self._shutdown = None
            procedure = CATALOGUE[procedure_name](config)

            # Refuse an automatic run before anything is created, rather than
            # letting it get to the first manual step and fail there with the
            # chamber already pumped down.
            self.mode = GUIDED if str(config.get("mode", GUIDED)) != AUTOMATIC \
                else AUTOMATIC
            if self.mode == AUTOMATIC:
                missing = self.hardware.missing(procedure.requires)
                if missing:
                    return {"ok": False, "error":
                            "automatic mode needs every actuator under "
                            "software control, and these are not wired yet: "
                            + ", ".join(missing) +
                            ". Run it in guided mode instead."}

            run = self.db.create_run(
                procedure_name, name or procedure.label, config,
                operator=operator, notes=notes, started_by=started_by,
                chamber=self.chamber)
            if self._closing.is_set():
                # close() came while the record was written -- a download can
                # hold the database that long: the run never begins. (Should
                # the store be closed already, the record stays "running" and
                # the next start reports it cut off: the safe direction.)
                try:
                    self.db.finish_run(run["id"], dbmod.ABORTED,
                                       {"error": "the panel was shutting down"})
                except Exception:
                    pass
                return {"ok": False, "error": "the panel is shutting down"}

            self._procedure = procedure
            self._run = run
            self._samples = []
            self._silent_since = None
            self._last_tick = None
            self._cleanup_since = None
            self._prompt = None
            self._satisfied = {}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._drive, name=f"run-{run['id']}", daemon=True)
            self._thread.start()
            return {"ok": True, "run": run}

    def stop(self, reason: str = "stopped by the operator",
             shutdown: bool = False, interrupted: bool = False) -> dict:
        """End the run. With `shutdown`, also bring the pump to a full stop.

        The two are separate decisions and the panel asks for both, because
        they are genuinely different intentions: ending a deposition usually
        means leaving the chamber under vacuum for the next run, while
        finishing for the day means the rotor should be at rest and the
        backing pump off. Stopping the *run* alone leaves the hardware
        exactly as it is -- the TC 600 keeps doing what it was last told.
        """
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return {"ok": False, "error": "no run is in progress"}
            self._stop_reason = reason
            self._stop_shutdown = bool(shutdown)
            # Not the operator's decision to end it: an abort, even for the
            # one procedure whose stop is otherwise its completion.
            self._stop_interrupted = bool(interrupted)
            self._stop.set()
            thread = self._thread
        if shutdown:
            self.shutdown_pump(f"after the run was stopped: {reason}",
                               after=thread)
        return {"ok": True, "stopping": True, "shutdown": bool(shutdown)}

    def close(self, timeout: float = 20.0,
              deadline: float | None = None) -> None:
        """End a run in progress, with its clean-up, before the process exits.

        `systemctl restart` sends SIGTERM, which used to end the process on
        the spot: a deploy during a bake-out left the heater on with nothing
        watching it. The run is stopped -- not the pump; the vacuum stays as
        it is -- and waited for, which is what runs its clean-up; so is a
        make-safe sequence, whether under way or started by the run as it
        ends, however long its clean-up took. Cut between the motor off and
        the acknowledgement, a make-safe leaves a latched fault holding the
        forevacuum pump off. A power-down still waiting for its rotor is
        cancelled, leaving the backing pump running. Owed clean-ups are not
        tried again from here on: the next start does them.

        Nothing here waits past `deadline` (monotonic), or `timeout` seconds
        from now without one.
        """
        if deadline is None:
            deadline = time.monotonic() + timeout
        # From here no start() begins a run: it looks at the flag under its
        # lock, and again once its record is written. One already past both
        # finishes under the lock; taking the lock once means its run, if it
        # began, is in `_thread` -- and is stopped below.
        self._closing.set()
        settled = self._lock.acquire(timeout=within(5.0, deadline))
        if settled:
            self._lock.release()
        powering_down = self._shutdown_thread
        if powering_down and powering_down.is_alive():
            self._cancel_shutdown.set()
        cancelled_by = time.monotonic() + self.SHUTDOWN_POLL_S * 3
        stopped = None
        while time.monotonic() < deadline:
            if not settled:
                # A start() is still holding the lock -- a download can hold
                # the database it is writing to: keep waiting for it.
                settled = self._lock.acquire(timeout=within(0.2, deadline))
                if settled:
                    self._lock.release()
            thread = self._thread
            if thread is not None and thread.is_alive() and thread is not stopped:
                self.stop("the control process was stopped (the panel was "
                          "restarted or shut down)", shutdown=False,
                          interrupted=True)
                stopped = thread
            busy = [t for t in (thread, self._secure_thread)
                    if t is not None and t.is_alive()]
            if powering_down is not None and powering_down.is_alive() \
                    and time.monotonic() < cancelled_by:
                busy.append(powering_down)
            if busy:
                busy[0].join(within(0.2, deadline))
            elif settled:
                break
        self._flush(within(5.0, deadline))

    def delete_run(self, run_id: int) -> dict:
        """Delete a stored run, unless it is going or its clean-up is being
        tried.

        The database work is done outside the engine's lock: an operator's
        Stop takes that lock, and must never wait on a download holding the
        database. A run starting meanwhile is safe all the same -- its
        record says "running" from before its thread exists, and the
        database refuses to delete a running run.
        """
        in_progress = {"ok": False, "code": 409,
                       "error": f"run {run_id} is in progress; stop it first"}
        with self._lock:
            if self._thread is not None and self._thread.is_alive() \
                    and self._run and self._run.get("id") == run_id:
                return in_progress
            if any(o["id"] == run_id and o["ok"] is None
                   for o in list(self.orphans)):
                return {"ok": False, "code": 409,
                        "error": f"run {run_id}'s clean-up is being tried "
                                 f"again; delete it once that has ended"}
        record = self.db.run(run_id)
        try:
            deleted = self.db.delete_run(run_id)
        except dbmod.RunStillRunning:
            with self._lock:
                going = self._thread is not None and self._thread.is_alive() \
                    and self._run and self._run.get("id") == run_id
            return in_progress if going else {
                "ok": False, "code": 409,
                "error": f"run {run_id} is still marked as running: its end "
                         f"was never recorded. The panel closes it at its "
                         f"next start; delete it after that"}
        if not deleted:
            return {"ok": False, "code": 404, "error": f"no run {run_id}"}
        with self._lock:
            self._forget(run_id)
        return {"ok": True, "record": record}

    def forget_run(self, run_id: int) -> None:
        """A run was deleted: stop naming it. The engine's log and the
        supervisor's records go to the last run between runs, and a row that
        is gone makes every one of them fail -- a false "storage" alarm.
        Under start()'s lock, or a run started from another screen at that
        moment could lose its name instead."""
        with self._lock:
            self._forget(run_id)

    def _forget(self, run_id: int) -> None:
        """forget_run's body; the caller holds the lock. A run going is never
        the one deleted -- the store refuses a running row -- so a match is
        always a run that has ended, even with another one started since."""
        if self._run and self._run.get("id") == run_id:
            self._run = None
        if self._last and self._last.get("id") == run_id:
            self._last = None

    # --- guided power-down ------------------------------------------------

    def shutdown_pump(self, reason: str = "requested by the operator",
                      after: threading.Thread | None = None) -> dict:
        """Bring the pump to a full stop in the order the hardware needs.

        A sequence, not a command. A turbopump rotor takes minutes to coast
        down and has to be backed the whole way, so the only safe order is:
        switch the motor off, wait for the rotor to reach rest, let the stop
        dwell pass, and only then stop the station.

        Every step goes through the same interlocks as the panel's buttons --
        the engine holds no override. That is the point: rather than
        re-implementing "may I stop the station yet?" here, where it could
        drift out of step with `lab/pump.py`, this simply asks the pump
        service and waits until it stops saying no.
        """
        with self._lock:
            if self._shutdown_thread and self._shutdown_thread.is_alive():
                return {"ok": False, "error": "a shutdown is already running"}
            if self.pump() is None:
                return {"ok": False, "error": "no pump is configured"}
            # Cut short by the process ending, a power-down could stop half
            # way; close() cancels them, and none begins after it.
            if self._closing.is_set():
                return {"ok": False, "error": "the panel is shutting down"}
            # Under a running run, only through Stop: the run must end first
            # -- its gas off, its heater off -- or it carries on, feeding a
            # deposition's argon to a turbopump that is being switched off.
            if self._thread and self._thread.is_alive() and after is not self._thread:
                return {"ok": False,
                        "error": "a run is in progress. Stop the run and choose "
                                 "Power down: the run ends first, with its own "
                                 "clean-up, and then the pump is brought to "
                                 "rest."}
            self._shutdown = {
                "active": True, "phase": "starting", "reason": reason,
                "message": "starting the power-down sequence",
                "rotor_hz": None, "started_at": time.time(), "ok": None,
            }
            self._cancel_shutdown.clear()
            self._shutdown_thread = threading.Thread(
                target=self._drive_shutdown, args=(reason, after),
                name="pump-shutdown", daemon=True)
            self._shutdown_thread.start()
        return {"ok": True, "shutdown": True}

    def _note_shutdown(self, phase: str, message: str,
                       rotor: float | None = None, ok: bool | None = None) -> None:
        state = dict(self._shutdown or {})
        state.update({"phase": phase, "message": message, "rotor_hz": rotor})
        if ok is not None:
            state["ok"] = ok
            state["active"] = False
            state["ended_at"] = time.time()
        self._shutdown = state

    def _drive_shutdown(self, reason: str, after: threading.Thread | None) -> None:
        # Let the run finish first: its own `finish()` may still be switching
        # a heater or the gas off, and two threads driving the same serial
        # port through different intentions is how a shutdown ends up
        # interleaved with a recipe's last actions.
        if after is not None:
            after.join(timeout=60)

        pump = self.pump()
        if pump is None:
            self._note_shutdown("failed", "no pump is configured", ok=False)
            return

        self.log("action", f"Power-down started ({reason})")
        deadline = time.monotonic() + self.SHUTDOWN_TIMEOUT_S

        # 1. Stop driving the rotor. Everything else is waiting.
        self._note_shutdown("motor_off", "switching the turbopump motor off")
        result = self.pump_command("motor_off")
        if not result.get("ok"):
            self.log("error", f"Power-down: could not switch the turbopump "
                              f"motor off: {result.get('error')}. The backing "
                              f"pump has been left running.")
            self._note_shutdown("failed", f"could not switch the motor off: "
                                          f"{result.get('error')}", ok=False)
            return
        self.log("action", "Power-down: turbopump motor off, rotor coasting")

        # 2. Wait until the pump service stops refusing to stop the station.
        #    That single question covers all three rules -- rotor still
        #    turning, motor still enabled, and the stop dwell after 0 Hz.
        while time.monotonic() < deadline:
            if self._cancel_shutdown.is_set():
                self.log("action", "Power-down cancelled by the operator. The "
                                   "turbopump motor is off and the backing "
                                   "pump is still running.")
                self._note_shutdown("cancelled", "cancelled: the motor is off "
                                                 "and the backing pump keeps "
                                                 "running", ok=False)
                return
            snap = pump.snapshot()
            speed = snap.get("status", {}).get("ActualSpd")
            speed = speed if isinstance(speed, (int, float)) else None
            rule = pump.interlocks(snap).get("off", {})
            if rule.get("allowed"):
                break
            self._note_shutdown(
                "coasting",
                f"{rule.get('reason') or 'waiting for the rotor to stop'} "
                f"The backing pump keeps running until it has.",
                rotor=speed)
            time.sleep(self.SHUTDOWN_POLL_S)
        else:
            self.log("warning",
                     f"Power-down gave up after "
                     f"{self.SHUTDOWN_TIMEOUT_S / 60:.0f} min waiting for the "
                     f"rotor to stop. The backing pump has been left running "
                     f"on purpose -- a coasting rotor still needs backing.")
            self._note_shutdown("failed", "the rotor did not come to rest in "
                                          "time; the backing pump was left "
                                          "running", ok=False)
            return

        # 3. Only now may the station stop.
        self._note_shutdown("station_off", "stopping the pumping station")
        result = self.pump_command("off")
        if not result.get("ok"):
            self.log("error", f"Power-down: could not stop the station: "
                              f"{result.get('error')}")
            self._note_shutdown("failed", f"could not stop the station: "
                                          f"{result.get('error')}", ok=False)
            return

        self.log("action", "Power-down complete: rotor at rest, station off")
        self._note_shutdown("done", "rotor at rest, pumping station off",
                            rotor=0.0, ok=True)

    def shutdown_status(self) -> dict | None:
        return self._shutdown

    def cancel_shutdown(self) -> dict:
        """Stop a power-down where it is, leaving the backing pump running.

        Safe at any point: the sequence only ever waits between steps, and
        the step it has not taken yet is the station off.
        """
        thread = self._shutdown_thread
        if not (thread and thread.is_alive()):
            return {"ok": False, "error": "no power-down is running"}
        self._cancel_shutdown.set()
        thread.join(timeout=self.SHUTDOWN_POLL_S * 3)
        return {"ok": True}

    # --- the safe state after pump trouble ---------------------------------

    def secure_pump(self, reason: str) -> dict:
        """Motor off, fault cleared, forevacuum pump on -- in the background.

        Called when a run ends because the turbopump itself was in trouble.
        The sequence is `PumpService.secure`; this records what it did, in
        the run log and for the panel.
        """
        pump = self.pump()
        if pump is None:
            return {"ok": False, "error": "no pump is configured"}
        with self._secure_start:
            running = self._secure_thread
            if running is not None and running.is_alive():
                return {"ok": False,
                        "error": "the pump is already being made safe"}
            return self._start_securing(pump, reason)

    def _start_securing(self, pump, reason: str) -> dict:
        self._secure = {"active": True, "reason": reason,
                        "started_at": time.time(), "actions": [], "ok": None}
        run_id = self.run_id()

        def work():
            # The supervisor's recovery from the same fault may be under way:
            # wait for it, then check what it left, rather than report it busy.
            result = pump.secure_when_free(reason)
            done = ", ".join(a["step"] for a in result.get("actions", [])
                             if a.get("ok")) or "nothing needed changing"
            at = time.time()
            self._write(lambda: self.db.add_event(
                run_id, "action" if result.get("ok") else "error",
                f"Pump made safe after the run ended: {done}."
                + ("" if result.get("ok") else
                   f" Not confirmed: {result.get('error')}."),
                {"reason": reason, "result": result}, at=at))
            self._secure = {**self._secure, "active": False,
                            "ok": bool(result.get("ok")),
                            "actions": result.get("actions", []),
                            "state": result.get("state"),
                            "error": result.get("error"),
                            "ended_at": time.time()}

        self._secure_thread = threading.Thread(target=work, name="pump-secure",
                                               daemon=True)
        self._secure_thread.start()
        return {"ok": True}

    def stop_choice(self) -> bool | None:
        """What the operator asked for when stopping, or None if they did not.

        A procedure that powers the rig down when it ends consults this so an
        explicit "leave the vacuum alone" is honoured rather than undone two
        seconds later.
        """
        return self._stop_shutdown

    def pump_status(self) -> dict:
        """The drive unit's decoded status, for conditions not in a sample.

        The sample row carries what every run charts and stores; this is the
        escape hatch for the rest -- the over-temperature flags, say, which
        are booleans no chart wants but an unattended run must watch.
        """
        pump = self.pump()
        return pump.snapshot().get("status", {}) if pump else {}

    def status(self) -> dict:
        run = self._run
        procedure = self._procedure
        running = bool(self._thread and self._thread.is_alive())
        if not running or run is None or procedure is None:
            return {"running": False, "run": None,
                    "shutdown": self._shutdown, "secure": self._secure,
                    "last": self._last}
        return {
            "running": True,
            "run": run,
            "procedure": procedure.name,
            "label": procedure.label,
            "phase": procedure.phase,
            "phases": list(procedure.phases),
            "elapsed_s": round(procedure.elapsed, 1),
            "in_phase_s": round(procedure.in_phase, 1),
            "samples": len(self._samples),
            "interval_s": procedure.interval,
            "tick_age_s": (round(age, 1) if (age := self.tick_age()) is not None
                           else None),
            "latest": self._samples[-1] if self._samples else None,
            "stopping": self._stop.is_set(),
            "mode": self.mode,
            "prompt": self._prompt,
            "requires": list(procedure.requires),
            "actuators": self.actuators(),
            "shutdown": self._shutdown,
            "secure": self._secure,
            "last": self._last,
        }

    def cleanup_age(self) -> float | None:
        """Seconds an ending run has spent on its clean-up; None otherwise."""
        since = self._cleanup_since
        if since is None or not (self._thread and self._thread.is_alive()):
            return None
        return time.monotonic() - since

    def tick_age(self) -> float | None:
        """Seconds since the run's safety tick last ran; None with no run.

        The supervisor alarms when this grows: a run whose loop is stuck --
        on a command that will not return, say -- is checking nothing.
        """
        tick = self._last_tick
        if tick is None or not (self._thread and self._thread.is_alive()):
            return None
        return time.monotonic() - tick

    def recent_samples(self, limit: int = 600) -> list[dict]:
        return self._samples[-limit:]

    def samples_since(self, t: float) -> list[dict]:
        """The run's samples after run time `t`, oldest first.

        Walks back from the end: a panel polling every couple of seconds is
        one or two rows behind, so this touches a handful of rows whatever
        the length of the run.
        """
        samples = self._samples
        start = len(samples)
        while start > 0 and (samples[start - 1].get("t") or 0) > t:
            start -= 1
        return samples[start:]

    def run_samples(self, points: int = 1500) -> list[dict]:
        """The whole run in progress, decimated to about `points` rows.

        The live charts want the run from its first sample -- the phases,
        the crossover, the spin-up -- not only its last half hour, and a
        deposition at 2 s is 4 000 rows by the time it is depositing.
        Decimation keeps every extreme, so a pressure step is never lost.
        """
        samples = list(self._samples)
        columns = [[s.get(key) for s in samples] for key in SHAPE_KEYS]
        return [samples[i] for i in decimate(len(samples), points, columns)]

    # --- the run thread ---------------------------------------------------

    def _collect(self, started: float) -> dict:
        """One sample row, merged from every registered device.

        `t`, the run time every timer of a procedure is measured in, is on
        the monotonic clock from `started`; `at` is the wall-clock time, for
        display. A wall clock corrected by the network mid-run used to move
        `t` with it -- an hour forward is a two-minute timer run out, an
        hour back is one that cannot run out.
        """
        row = self.registry.sample_row()
        row.update(self.hardware.sample_fields())
        row["t"] = round(time.monotonic() - started, 3)
        row["at"] = time.time()
        row["phase"] = self._procedure.phase if self._procedure else ""

        gauge = self.gauge()
        row["p_best"] = gauge.best_pressure() if gauge else None
        return row

    def _check_aborts(self, sample: dict) -> None:
        """Conditions that end a run whatever the procedure wanted."""
        pump = self.pump()
        gauge = self.gauge()

        pump_up = pump is None or self._answering(pump)
        gauge_up = gauge is None or self._answering(gauge)
        if not (pump_up and gauge_up):
            now = time.monotonic()
            self._silent_since = self._silent_since or now
            silent = now - self._silent_since
            if silent >= self.MAX_SILENT_S:
                lost = "pump" if not pump_up else "gauge"
                raise Abort(f"lost contact with the {lost} for "
                            f"{silent:.0f} s")
        else:
            self._silent_since = None

        code = sample.get("error_code")
        if code is not None and str(code).strip() not in NO_ERROR:
            meaning = pumpmod.describe_error(code)
            raise Abort(f"the pump reported fault {code} "
                        f"({meaning.get('title', 'drive fault').lower()})",
                        dbmod.FAILED, pump_trouble=True)

        ceiling = (self._procedure.current_pressure_ceiling(sample)
                   if self._procedure else None)
        pressure = sample.get("p_best")
        if ceiling and isinstance(pressure, (int, float)) and pressure > ceiling:
            raise Abort(f"pressure rose to {pressure:.2E} mbar, above the "
                        f"{ceiling:.2E} mbar ceiling set for this run")

    @staticmethod
    def _answering(service) -> bool:
        """In contact, and with a reading that is still being renewed: one
        that says "connected" but has stopped changing -- a sampler held up
        behind a long exchange -- is silence too."""
        snap = service.snapshot()
        if not snap.get("connected"):
            return False
        age = snap.get("age")
        limit = (service.stale_after(snap) if hasattr(service, "stale_after")
                 else pumpmod.PRESSURE_MAX_AGE_S)
        return isinstance(age, (int, float)) and age <= limit

    def _readings(self) -> tuple:
        """Which readings the instruments have published, by number."""
        return tuple(service.snapshot().get("seq") for service in self.registry)

    def _keep(self, run_id: int, sample: dict) -> None:
        self._samples.append(sample)
        self._write(lambda: self.db.add_sample(run_id, sample), "a sample")

    def _drive(self) -> None:
        procedure, run = self._procedure, self._run
        assert procedure is not None and run is not None
        started = time.monotonic()
        status, result, message = dbmod.COMPLETED, {}, ""
        pump_trouble = False
        by_operator = False
        interrupted = False

        try:
            procedure.begin(self)
            first = procedure.phase
            self._write(lambda: self.db.set_phase(run["id"], first))
            phase = procedure.phase
            due = time.monotonic()          # when the next sample is stored
            seen = None                      # the readings last judged

            # Every tick: the abort conditions, and the procedure's guards on
            # readings they have not judged yet. Every `interval`: the
            # procedure's step, and the sample stored -- after the step, so
            # it carries what the procedure concluded from it (the
            # base-pressure rate).
            while not self._stop.is_set():
                tick = time.monotonic()
                self._last_tick = tick
                # Which readings these are, taken before the sample: one
                # published in between is then judged next tick, not marked
                # judged without having been.
                readings = self._readings()
                sample = self._collect(started)
                fresh = readings != seen or not readings
                seen = readings
                stepping = tick >= due
                finished = False
                try:
                    self._check_aborts(sample)
                    if fresh:
                        procedure.guard(self, sample)
                    if stepping:
                        finished = procedure.step(self, sample)
                except Exception:
                    # The sample that ended the run is the most important
                    # row of a failed run: kept whether or not it was due,
                    # and whatever ended it.
                    self._keep(run["id"], sample)
                    raise
                if stepping:
                    self._keep(run["id"], sample)
                    due = max(due + procedure.interval, tick)
                    if finished:
                        break
                    if procedure.phase != phase:
                        phase = procedure.phase
                        self._write(lambda now=phase: self.db.set_phase(
                            run["id"], now))
                        self.log("info", f"Phase: {phase}")

                tick_s = min(self.TICK_S, procedure.interval)
                self._stop.wait(max(0.0, tick + tick_s - time.monotonic()))
            else:
                interrupted = self._stop_interrupted
                status = (dbmod.COMPLETED if procedure.stop_is_completion
                          and not interrupted else dbmod.ABORTED)
                message = getattr(self, "_stop_reason", "stopped by the operator")
                by_operator = not interrupted

        except Abort as abort:
            status, message = abort.status, abort.reason
            pump_trouble = abort.pump_trouble
            self.log("error", f"Run aborted: {abort.reason}")
        except Exception as exc:                      # never lose the thread
            status, message = dbmod.FAILED, f"internal error: {exc}"
            self.log("error", message)
        # The checks have stopped on purpose: a slow clean-up is not a stall --
        # but one that never ends is, and has its own clock.
        self._last_tick = None
        self._cleanup_since = time.monotonic()

        # The hardware first, each step on its own, and only then the record:
        # a write that fails -- a full or worn SD card -- used to replace the
        # abort and skip everything after it, the heater and the gas included.
        try:
            cleaned = procedure.finish(self) is not False
        except Exception as exc:
            cleaned = False
            self.log("error", f"Cleanup after the run failed: {exc}")

        # Whatever happened, the manual controls come back and nothing is
        # left waiting on an operator who has no run to acknowledge.
        self._prompt = None
        pump = self.pump()
        if pump is not None:
            try:
                pump.unlock_full_speed()
            except Exception:
                pass

        # The pump itself was the problem: put it in its safe state -- motor
        # off, fault cleared, forevacuum pump running. Never the station off.
        if pump_trouble:
            try:
                begun = self.secure_pump(message)
                if not begun.get("ok"):
                    self.log("warning", f"Making the pump safe did not start: "
                                        f"{begun.get('error')}.")
            except Exception as exc:
                self.log("error", f"Could not start making the pump safe: {exc}")
        # The hardware's part is over; what follows is only the record.
        self._cleanup_since = None

        try:
            result = procedure.summarise(self, self._samples)
        except Exception as exc:
            result = {"error": f"could not summarise: {exc}"}

        result["status"] = status
        if message:
            result["message"] = message
        result["samples"] = len(self._samples)
        if interrupted:
            # Stopped by the panel shutting down: the next start says so.
            result.update({"interrupted": True, "reported": False})
        if not cleaned:
            # Owed to the next start, whatever ended the run: a restart must
            # not turn "the heater did not go off" into "its clean-up ran".
            result["cleanup_pending"] = True

        self._write(lambda: self.db.finish_run(run["id"], status, result),
                    f"the end of run {run['id']}")
        self.log("info" if status == dbmod.COMPLETED else "warning",
                 f"Run {status}" + (f": {message}" if message else ""))
        self._flush()
        try:
            self._run = self.db.run(run["id"]) or self._run
        except Exception:
            pass
        # A failed clean-up is tried again in the background, as at a start:
        # a heater left on because the pump's link dropped is switched off
        # once the pump answers, not at the next restart, weeks later.
        retrying = not cleaned and not interrupted and not self._closing.is_set()
        self._last = {
            "id": run["id"], "name": run.get("name"), "procedure": procedure.name,
            "label": procedure.label, "status": status, "message": message,
            "started_at": run.get("started_at"), "ended_at": time.time(),
            "phases": list(procedure.phases), "by_operator": by_operator,
            "pump_trouble": pump_trouble, "cleanup_failed": not cleaned,
            "retrying": retrying,
        }
        if retrying:
            self._remember({**run, "procedure": procedure.name},
                           "trying again", None, "retry")
            self._start_cleanups()
