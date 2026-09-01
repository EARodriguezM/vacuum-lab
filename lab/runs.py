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
import threading
import time
from collections import deque

from . import db as dbmod
from . import process
from .pump import TURBO_START_MAX_MBAR

#: Payload of a TC 600 error-code parameter when nothing is wrong.
NO_ERROR = ("000000", "000, 0", "", None)

#: How a run treats steps the software cannot perform itself.
GUIDED = "guided"        # stop, tell the operator, wait for acknowledgement
AUTOMATIC = "auto"       # refuse to start unless everything is under control


class Abort(Exception):
    """Raised inside the run thread to end a run early."""

    def __init__(self, reason: str, status: str = dbmod.ABORTED):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# --- analysis helpers -----------------------------------------------------


def linear_fit(points: list[tuple[float, float]]) -> dict | None:
    """Least-squares fit of p = a + b*t, with the coefficient of determination.

    R² is reported because a rate-of-rise measurement is only meaningful when
    the rise is linear: a curve that bends is still outgassing or still
    settling, and its slope is not a leak rate.
    """
    n = len(points)
    if n < 3:
        return None
    sum_t = sum(t for t, _ in points)
    sum_p = sum(p for _, p in points)
    mean_t, mean_p = sum_t / n, sum_p / n

    variance = sum((t - mean_t) ** 2 for t, _ in points)
    if variance == 0:
        return None
    covariance = sum((t - mean_t) * (p - mean_p) for t, p in points)
    slope = covariance / variance
    intercept = mean_p - slope * mean_t

    total = sum((p - mean_p) ** 2 for _, p in points)
    residual = sum((p - (intercept + slope * t)) ** 2 for t, p in points)
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r_squared": r_squared,
            "points": n}


def relative_rate(points: list[tuple[float, float]]) -> float | None:
    """How fast a pressure is changing, in percent per minute.

    Fits ln(p) against time rather than p, because d(ln p)/dt is exactly
    (dp/dt)/p -- a fractional rate. On a quantity that spans decades that is
    the only rate worth quoting: 1e-4 mbar falling by 1e-5 mbar/min and 1e-6
    falling by 1e-7 mbar/min are the same process at different depths, and an
    absolute threshold would call one "still pumping" and the other "flat".

    Negative means falling. Returns None if there is not enough to fit.
    """
    usable = [(t, math.log(p)) for t, p in points if p > 0]
    fit = linear_fit(usable)
    if fit is None:
        return None
    return fit["slope"] * 60.0 * 100.0


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

    def finish(self, engine: "RunEngine") -> None:
        """Called once after the last sample, whatever the outcome."""


class PumpDown(Procedure):
    """Record p(t) from wherever the chamber is now down to a target."""

    name = "pumpdown"
    label = "Pump-down curve"
    description = ("Records pressure against time while the chamber is pumped "
                   "down, and reports how long it took to reach the target.")
    phases = ("roughing", "turbo", "pumping", "done")
    fields = (
        {"key": "target_mbar", "label": "Target pressure", "type": "exponent",
         "default": 1e-5, "unit": "mbar",
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
            elif speed > 0:
                self.enter("turbo")

        elif self.phase == "turbo":
            nominal = engine.nominal_speed() or 0
            if nominal and speed >= 0.95 * nominal:
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
    description = ("With the chamber isolated, fits the pressure rise against "
                   "time and reports the leak rate.")
    phases = ("settle", "measuring", "done")
    fields = (
        {"key": "volume_l", "label": "Chamber volume", "type": "number",
         "default": 10, "unit": "L", "min": 0.01,
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

    def begin(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 5))
        self.settle = self.number("settle_s", 60)
        self.duration = self.number("duration_s", 600)
        self.volume = self.number("volume_l", 10)
        self.fit_from: float | None = None
        engine.log("info",
                   "Isolate the chamber now: close the valve to the pump. "
                   f"The fit starts in {self.settle:.0f} s.")

    def step(self, engine: "RunEngine", sample: dict) -> bool:
        if self.phase == "settle":
            if self.in_phase >= self.settle:
                self.fit_from = sample["t"]
                self.enter("measuring")
                engine.log("info", "Settling done, measuring the rise")
            return False

        if self.in_phase >= self.duration:
            self.enter("done")
            return True
        return False

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
        if fit:
            rise = fit["slope"]                       # mbar per second
            summary.update({
                "rise_mbar_per_s": rise,
                "rise_mbar_per_h": rise * 3600.0,
                "leak_rate_mbar_l_s": rise * self.volume,
                "r_squared": fit["r_squared"],
                "linear": fit["r_squared"] >= 0.95,
            })
            if fit["r_squared"] < 0.95:
                summary["caveat"] = (
                    "The rise is not linear (R² < 0.95), so this figure is an "
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
         "default": 60, "unit": "min", "min": 1, "max": 10080},
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
         "default": 1e-3, "unit": "mbar",
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
        self.pressure_ceiling = self.number("max_pressure_mbar", 1e-3) or None
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

    def finish(self, engine: "RunEngine") -> None:
        # Whatever happened -- completion, abort, operator stop -- the heater
        # must not be left on by a run that is no longer being watched.
        result = engine.pump_param("Heating", False)
        engine.log("action", "Heating switched off" if result.get("ok") else
                   f"WARNING: could not switch the heating off: "
                   f"{result.get('error')}")

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

    #: Fields every recipe built on this sequence shares.
    VACUUM_FIELDS: tuple[dict, ...] = (
        {"key": "mode", "label": "Run mode", "type": "select",
         "default": GUIDED,
         "options": [{"value": GUIDED, "label": "Guided — prompt me for "
                                                "anything not yet wired"},
                     {"value": AUTOMATIC, "label": "Automatic — refuse to "
                                                   "start unless fully wired"}],
         "help": "Nothing is wired to the argon, gate valve, magnetron or "
                 "shutter yet, so guided is the only mode that will run "
                 "today."},
        {"key": "crossover_mbar", "label": "Start the turbopump at",
         "type": "exponent", "default": TURBO_START_MAX_MBAR, "unit": "mbar",
         "help": "The forevacuum pump works alone until the chamber reaches "
                 f"this. {TURBO_START_MAX_MBAR:.0E} mbar is the highest the "
                 "interlock will allow; asking for more is lowered to it, "
                 "since a turbopump started against an un-roughed foreline "
                 "cannot develop compression and trips."},
        {"key": "base_rate_pct", "label": "Call it base pressure below",
         "type": "number", "default": 2.0, "unit": "%/min", "min": 0.1,
         "max": 50,
         "help": "When the pressure is improving more slowly than this, the "
                 "chamber has stopped getting better."},
        {"key": "base_settle_min", "label": "…sustained for", "type": "number",
         "default": 3, "unit": "min", "min": 0.5, "max": 120},
        {"key": "base_timeout_min", "label": "Give up looking for base after",
         "type": "number", "default": 120, "unit": "min", "min": 1, "max": 1440},
        {"key": "stage_timeout_min", "label": "Give up on any other stage after",
         "type": "number", "default": 45, "unit": "min", "min": 1, "max": 1440,
         "help": "Roughing, the turbopump start, spin-up and the argon "
                 "backfill each get this long before the run gives up. Time "
                 "spent waiting for you to do something by hand does not "
                 "count against it."},
        {"key": "interval_s", "label": "Sample every", "type": "number",
         "default": 2, "unit": "s", "min": 1, "max": 60},
    )

    #: Rolling window used to judge whether the pressure has flattened.
    RATE_WINDOW_S = 120.0

    def begin_vacuum(self, engine: "RunEngine") -> None:
        self.interval = max(1.0, self.number("interval_s", 2))
        # A recipe may cross over deeper than the interlock demands, never
        # shallower: asking for 5e-1 mbar would leave the run stuck in the
        # crossover phase asking for a start the pump service keeps refusing.
        requested = self.number("crossover_mbar", TURBO_START_MAX_MBAR)
        self.crossover = min(requested, TURBO_START_MAX_MBAR)
        if self.crossover < requested:
            engine.log("warning",
                       f"Crossover lowered from {requested:.2E} to "
                       f"{self.crossover:.2E} mbar: the turbopump may not be "
                       f"started above {TURBO_START_MAX_MBAR:.0E} mbar.")
        self.base_rate_limit = self.number("base_rate_pct", 2.0)
        self.base_settle = self.number("base_settle_min", 3) * 60.0
        self.base_timeout = self.number("base_timeout_min", 120) * 60.0
        # Every other phase needs a bound too. Without one, a chamber that
        # never reaches crossover -- a shut roughing valve, a dead forepump,
        # a gross leak -- roughs for ever, and a run that cannot finish still
        # looks alive.
        self.stage_timeout = self.number("stage_timeout_min", 45) * 60.0

        self.base_mbar: float | None = None
        self.base_reached_at: float | None = None
        self.crossed_at: float | None = None
        self.full_speed_at: float | None = None
        self._flat_since: float | None = None
        self._window: deque = deque()
        self.turbo_requested = False

        engine.enforce_full_speed("this rig sputters in argon, so the "
                                  "turbopump runs at its final rotation "
                                  "speed for the whole run")
        result = engine.pump_command("on")
        engine.log("action", "Forevacuum pump started" if result.get("ok")
                   else f"Could not start the forevacuum pump: "
                        f"{result.get('error')}")

    # -- phases ------------------------------------------------------------

    def phase_rough(self, engine: "RunEngine", sample: dict) -> str | None:
        pressure = sample.get("p_best")
        if self.phase_age(engine) >= self.stage_timeout:
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
        if self.phase_age(engine) >= self.stage_timeout:
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
        if self.phase_age(engine) >= self.stage_timeout:
            raise Abort(
                f"the rotor did not reach full speed within "
                f"{self.stage_timeout / 60:.0f} min (it is at {speed:.0f} Hz "
                f"of {nominal or '?'} Hz). Check the drive unit for a fault "
                f"and the foreline for a leak.")
        if nominal and speed >= 0.95 * nominal:
            self.full_speed_at = sample["t"]
            engine.log("info", f"Turbopump at full speed ({speed:.0f} Hz)")
            return "basepressure"
        if not nominal and speed > 0:
            return "basepressure"
        return None

    def phase_basepressure(self, engine: "RunEngine", sample: dict) -> str | None:
        pressure = sample.get("p_best")
        now = sample["t"]
        if isinstance(pressure, (int, float)) and pressure > 0:
            self._window.append((now, pressure))
            while self._window and now - self._window[0][0] > self.RATE_WINDOW_S:
                self._window.popleft()

        rate = relative_rate(list(self._window))
        sample["rate_pct_min"] = rate

        if rate is not None and abs(rate) <= self.base_rate_limit:
            if self._flat_since is None:
                self._flat_since = now
            elif now - self._flat_since >= self.base_settle:
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

        if self.in_phase >= self.base_timeout:
            raise Abort(
                f"gave up after {self.base_timeout / 60:.0f} min: the chamber "
                f"is at {pressure:.2E} mbar and still improving by "
                f"{abs(rate):.1f} %/min. Suspect a leak, a wet chamber, or a "
                f"target that needs baking." if rate is not None else
                f"gave up after {self.base_timeout / 60:.0f} min without a "
                f"usable pressure reading")
        return None

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

    # -- dispatch ----------------------------------------------------------

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
    description = ("The full process: pump to base pressure, backfill with "
                   "argon, pre-sputter the target, then deposit for a set "
                   "time. Steps that are not wired up yet are prompted.")
    phases = ("rough", "crossover", "spinup", "basepressure", "backfill",
              "presputter", "deposit", "recover", "done")

    fields = VacuumSequence.VACUUM_FIELDS + (
        {"key": "working_mbar", "label": "Argon working pressure",
         "type": "exponent", "default": 5e-3, "unit": "mbar",
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
        self.pressure_ceiling = self.number("max_inlet_mbar", 5e-2) or None

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
        if (sample.get("speed") or 0) <= 0:
            return None
        if self.phase in ("rough", "crossover", "spinup"):
            return None
        return self.pressure_ceiling

    # -- process phases ----------------------------------------------------

    def phase_backfill(self, engine: "RunEngine", sample: dict) -> str | None:
        """Throttle the pump and let argon in until working pressure holds."""
        # Bounded, because this phase has the gas on. A flow and a gate
        # opening that cannot meet in the middle at the working pressure
        # would otherwise sit here for ever with argon running into the
        # chamber and nobody watching -- the abort path shuts the gas off.
        if self.phase_age(engine) >= self.stage_timeout:
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

    def finish(self, engine: "RunEngine") -> None:
        """However this ended, the plasma and the gas must not be left on."""
        engine.hardware.safe_shutdown(log=engine.log)

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


CATALOGUE: dict[str, type[Procedure]] = {
    procedure.name: procedure
    for procedure in (Deposition, BaseCalibration, PumpDown, RateOfRise,
                      Hold, Bakeout)
}


def catalogue() -> list[dict]:
    """Describe every procedure, so the UI can build its forms from the API."""
    return [
        {"name": p.name, "label": p.label, "description": p.description,
         "phases": list(p.phases), "fields": [dict(f) for f in p.fields]}
        for p in CATALOGUE.values()
    ]


# --- the engine -----------------------------------------------------------


class RunEngine:
    """Runs one procedure at a time against the registered devices."""

    #: Consecutive samples with no contact before a run is abandoned. At a
    #: 2 s cadence that is a minute of silence, long enough to ride out a
    #: reconnect but short enough that nothing records a flat line for hours.
    MAX_SILENT_SAMPLES = 30

    def __init__(self, database: "dbmod.Database", registry,
                 hardware: "process.ProcessHardware | None" = None,
                 pump_key: str = "pump", gauge_key: str = "gauge"):
        self.db = database
        self.registry = registry
        self.hardware = hardware or process.ProcessHardware()
        self.pump_key = pump_key
        self.gauge_key = gauge_key

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._procedure: Procedure | None = None
        self._run: dict | None = None
        self._samples: list[dict] = []
        self._silent = 0

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
        return self._run["id"] if self._run else None

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
        """Close out runs left open by a crash or a restart."""
        for run in self.db.orphaned_runs():
            self.db.finish_run(
                run["id"], dbmod.FAILED,
                {"error": "the control process stopped while this run was "
                          "in progress"})
            self.db.add_event(run["id"], "error",
                              "Run abandoned: the control process stopped")

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
        pump = self.pump()
        if pump is None:
            return None
        value = pump.snapshot().get("identity", {}).get("NominalSpd")
        return value if isinstance(value, (int, float)) else None

    # --- logging ----------------------------------------------------------

    def log(self, level: str, message: str, detail: dict | None = None) -> None:
        run_id = self._run["id"] if self._run else None
        self.db.add_event(run_id, level, message, detail)

    # --- lifecycle --------------------------------------------------------

    def start(self, procedure_name: str, config: dict, name: str = "",
              operator: str = "", notes: str = "") -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return {"ok": False,
                        "error": "a run is already in progress; stop it first"}
            if procedure_name not in CATALOGUE:
                return {"ok": False,
                        "error": f"unknown procedure {procedure_name!r}"}

            procedure = CATALOGUE[procedure_name](config)

            # Refuse an automatic run before anything is created, rather than
            # letting it get to the first manual step and fail there with the
            # chamber already pumped down.
            self.mode = GUIDED if str(config.get("mode", GUIDED)) != AUTOMATIC \
                else AUTOMATIC
            if self.mode == AUTOMATIC:
                missing = self.hardware.missing()
                if missing:
                    return {"ok": False, "error":
                            "automatic mode needs every actuator under "
                            "software control, and these are not wired yet: "
                            + ", ".join(missing) +
                            ". Run it in guided mode instead."}

            run = self.db.create_run(
                procedure_name, name or procedure.label, config,
                operator=operator, notes=notes)

            self._procedure = procedure
            self._run = run
            self._samples = []
            self._silent = 0
            self._prompt = None
            self._satisfied = {}
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._drive, name=f"run-{run['id']}", daemon=True)
            self._thread.start()
            return {"ok": True, "run": run}

    def stop(self, reason: str = "stopped by the operator") -> dict:
        with self._lock:
            if not (self._thread and self._thread.is_alive()):
                return {"ok": False, "error": "no run is in progress"}
            self._stop_reason = reason
            self._stop.set()
            return {"ok": True, "stopping": True}

    def status(self) -> dict:
        run = self._run
        procedure = self._procedure
        running = bool(self._thread and self._thread.is_alive())
        if not running or run is None or procedure is None:
            return {"running": False, "run": None}
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
            "latest": self._samples[-1] if self._samples else None,
            "stopping": self._stop.is_set(),
            "mode": self.mode,
            "prompt": self._prompt,
            "actuators": self.hardware.describe(),
        }

    def recent_samples(self, limit: int = 600) -> list[dict]:
        return self._samples[-limit:]

    # --- the run thread ---------------------------------------------------

    def _collect(self, started_at: float) -> dict:
        """One sample row, merged from every registered device."""
        row = self.registry.sample_row()
        now = time.time()
        row["t"] = round(now - started_at, 3)
        row["at"] = now
        row["phase"] = self._procedure.phase if self._procedure else ""

        gauge = self.gauge()
        row["p_best"] = gauge.best_pressure() if gauge else None
        return row

    def _check_aborts(self, sample: dict) -> None:
        """Conditions that end a run whatever the procedure wanted."""
        pump = self.pump()
        gauge = self.gauge()

        pump_up = pump is None or pump.snapshot().get("connected")
        gauge_up = gauge is None or gauge.snapshot().get("connected")
        if not (pump_up and gauge_up):
            self._silent += 1
            if self._silent >= self.MAX_SILENT_SAMPLES:
                lost = "pump" if not pump_up else "gauge"
                raise Abort(f"lost contact with the {lost} for "
                            f"{self._silent} samples")
        else:
            self._silent = 0

        code = sample.get("error_code")
        if code is not None and str(code).strip() not in NO_ERROR:
            raise Abort(f"the pump reported fault {code}", dbmod.FAILED)

        ceiling = (self._procedure.current_pressure_ceiling(sample)
                   if self._procedure else None)
        pressure = sample.get("p_best")
        if ceiling and isinstance(pressure, (int, float)) and pressure > ceiling:
            raise Abort(f"pressure rose to {pressure:.2E} mbar, above the "
                        f"{ceiling:.2E} mbar ceiling set for this run")

    def _drive(self) -> None:
        procedure, run = self._procedure, self._run
        assert procedure is not None and run is not None
        started_at = time.time()
        status, result, message = dbmod.COMPLETED, {}, ""

        try:
            procedure.begin(self)
            self.db.set_phase(run["id"], procedure.phase)
            phase = procedure.phase

            while not self._stop.is_set():
                sample = self._collect(started_at)
                self._samples.append(sample)
                self.db.add_sample(run["id"], sample)

                self._check_aborts(sample)

                if procedure.step(self, sample):
                    break
                if procedure.phase != phase:
                    phase = procedure.phase
                    self.db.set_phase(run["id"], phase)
                    self.log("info", f"Phase: {phase}")

                self._stop.wait(procedure.interval)
            else:
                status = dbmod.ABORTED
                message = getattr(self, "_stop_reason", "stopped by the operator")

        except Abort as abort:
            status, message = abort.status, abort.reason
            self.log("error", f"Run aborted: {abort.reason}")
        except Exception as exc:                      # never lose the thread
            status, message = dbmod.FAILED, f"internal error: {exc}"
            self.log("error", message)

        try:
            procedure.finish(self)
        except Exception as exc:
            self.log("error", f"Cleanup after the run failed: {exc}")

        # Whatever happened, the manual controls come back and nothing is
        # left waiting on an operator who has no run to acknowledge.
        self._prompt = None
        pump = self.pump()
        if pump is not None:
            pump.unlock_full_speed()

        try:
            result = procedure.summarise(self, self._samples)
        except Exception as exc:
            result = {"error": f"could not summarise: {exc}"}

        result["status"] = status
        if message:
            result["message"] = message
        result["samples"] = len(self._samples)

        self.db.finish_run(run["id"], status, result)
        self.log("info" if status == dbmod.COMPLETED else "warning",
                 f"Run {status}" + (f": {message}" if message else ""))
        self._run = self.db.run(run["id"])
