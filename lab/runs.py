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
  override. A procedure that cannot start the turbopump because the backing
  pump has not led it by a minute waits, exactly as an operator would have to.

Adding a procedure means subclassing `Procedure` and adding it to `CATALOGUE`;
the API publishes its `fields` and the UI builds the form from them.
"""

from __future__ import annotations

import math
import threading
import time

from . import db as dbmod

#: Payload of a TC 600 error-code parameter when nothing is wrong.
NO_ERROR = ("000000", "000, 0", "", None)


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

    def __init__(self, config: dict):
        self.config = config
        self.phase = self.phases[0]
        self.started = time.monotonic()
        self.phase_started = self.started

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
         "help": "Starts the station, then the turbopump once the interlock "
                 "allows it. Leave off to drive the pump yourself."},
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
                # Ask every cycle until the backing-lead interlock clears; the
                # engine holds no override, so this is simply waiting.
                result = engine.pump_command("motor_on")
                if result.get("ok"):
                    self.motor_requested = True
                    engine.log("action", "Started the turbopump")
                    self.enter("turbo")
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


CATALOGUE: dict[str, type[Procedure]] = {
    procedure.name: procedure
    for procedure in (PumpDown, RateOfRise, Hold, Bakeout)
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

    def __init__(self, database: "dbmod.Database", registry, pump_key: str = "pump",
                 gauge_key: str = "gauge"):
        self.db = database
        self.registry = registry
        self.pump_key = pump_key
        self.gauge_key = gauge_key

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._procedure: Procedure | None = None
        self._run: dict | None = None
        self._samples: list[dict] = []
        self._silent = 0

        self._close_orphans()

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
            run = self.db.create_run(
                procedure_name, name or procedure.label, config,
                operator=operator, notes=notes)

            self._procedure = procedure
            self._run = run
            self._samples = []
            self._silent = 0
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
            "samples": len(self._samples),
            "interval_s": procedure.interval,
            "latest": self._samples[-1] if self._samples else None,
            "stopping": self._stop.is_set(),
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

        ceiling = getattr(self._procedure, "pressure_ceiling", None)
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
