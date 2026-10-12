"""The rig's supervisor: alarms, and the pump's safe state after a fault.

The run engine watches the rig only while a recipe is recording, and before
this nothing watched it at all otherwise. A fault at three in the morning
went unremarked; worse, a latched drive fault can hold the forevacuum pump
off until somebody acknowledges it, and a chamber left without backing for
hours is a chamber at atmosphere by breakfast.

So this runs all the time, reads the same cached snapshots as everything
else (it never touches a serial port), and does two things:

* **Raises alarms** -- a drive fault, over-temperature, a rotor held down by
  gas, a pressure jump at full speed, the station switched off or the rotor
  started without this panel asking, a lost link, a run that ended badly. An
  alarm stays on screen until somebody acknowledges it, even after its
  condition has cleared, so the night's events are still there in the
  morning. Each is also written to the events table.

  The drive's flags are believed only when two readings in a row agree. A
  reply lost on the bus leaves its field out of the reading, which is no
  reading at all -- it once read as "station off" and raised a critical
  alarm, and as "fault cleared", so the next reading looked like a new
  fault and spent one of the three recoveries.
* **Makes the pump safe after a drive fault** (`auto_recover`): motor off,
  fault acknowledged, pumping station -- and so the forevacuum pump -- kept
  on (`PumpService.secure`). Once per fault, at most `MAX_RECOVERIES` times
  in `RETRY_WINDOW_S`, so a fault that re-latches the moment it is cleared
  is reported rather than fought.

It never switches the station off, and it never restarts the turbopump:
restarting is a person's decision, through the crossover interlock.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from .pump import NO_FAULT, TURBO_START_MAX_MBAR, describe_error
from .runs import within

#: Process phases during which a pressure rise is the recipe's own doing.
PROCESS_PHASES = {"backfill", "presputter", "deposit"}


class Steady:
    """A reading believed only once two readings in a row agree.

    Fed one reading per new snapshot -- never the same snapshot twice, which
    is why the supervisor goes by its sequence number. None is "no reading"
    and changes nothing; a value different from the accepted one is held
    back until the next reading confirms it.
    """

    def __init__(self, needed: int = 2):
        self.needed = needed
        #: The accepted value, None until two readings have agreed.
        self.value = None
        #: What it was before the last change.
        self.previous = None
        self._candidate = None
        self._count = 0

    def update(self, reading) -> bool:
        """Take one reading; True if it changed the accepted value."""
        if reading is None:
            return False
        if reading == self.value:
            self._candidate, self._count = None, 0
            return False
        if reading != self._candidate:
            self._candidate, self._count = reading, 0
        self._count += 1
        if self._count < self.needed:
            return False
        self.previous, self.value = self.value, reading
        self._candidate, self._count = None, 0
        return True


class Supervisor:
    """Watches the rig on its own thread; keeps a list of alarms."""

    INTERVAL_S = 1.0
    #: Silence from an instrument this long is an alarm, not a hiccup.
    OFFLINE_AFTER_S = 15.0
    #: A rotor below full speed and not accelerating this long is "held down".
    HELD_AFTER_S = 4.0
    #: Recovery attempts allowed in a window, before it stops trying.
    MAX_RECOVERIES = 3
    RETRY_WINDOW_S = 900.0
    #: How long a recovery waits for one still under way, and close() for it.
    RECOVERY_WAIT_S = 30.0
    #: A tenfold pressure rise within this many seconds is a "jump".
    JUMP_WINDOW_S = 30.0
    #: A running run whose safety tick has not run for this long is stuck,
    #: and nothing it is meant to check is being checked.
    ENGINE_STALL_S = 5.0
    #: An ending run's clean-up -- the heater, the gas -- taking this long has
    #: hung on something.
    CLEANUP_STALL_S = 60.0
    KEEP = 200

    def __init__(self, registry, engine, database, auto_recover: bool = True,
                 pump_key: str = "pump", gauge_key: str = "gauge"):
        self.registry = registry
        self.engine = engine
        self.db = database
        self.auto_recover = auto_recover
        self.pump_key = pump_key
        self.gauge_key = gauge_key

        self._alarms: list[dict] = []
        self._next_id = 1
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        #: The pump reading last judged, by its sequence number: the cached
        #: snapshot is read every second but changes only when the sampler
        #: publishes, and "two readings in a row" means two readings.
        self._pump_seq = None
        self._fault = Steady()          # "" for no fault, else the code
        self._station = Steady()
        self._motor = Steady()
        self._hot_pump = Steady()
        self._hot_elec = Steady()
        #: Whether the drive was driving the rotor, by the accepted readings.
        self._driven: bool | None = None
        #: When the rotor was found driven at startup, until that is judged.
        self._found_driven: float | None = None
        #: Since when the pump has been out of contact, monotonic, until two
        #: readings after it came back; and how many of those there have been.
        self._pump_lost_at: float | None = None
        self._back_readings = 0
        self._offline_since: dict[str, float | None] = {}
        self._held_since: float | None = None
        self._recoveries: deque = deque()
        self._last_run_seen = (engine.status().get("last") or {}).get("id")
        self._pressures: deque = deque()
        self._jump_at: float | None = None
        self._moves_seen: dict[str, float] = {}
        self._orphans_seen: dict[str, str] = {}
        self._storage_seen = None
        #: The current or last automatic recovery, for the panel.
        self.recovering: dict | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> "Supervisor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="supervisor",
                                            daemon=True)
            self._thread.start()
        return self

    def close(self, timeout: float = 5.0, deadline: float | None = None) -> None:
        """Stop watching; nothing here waits past `deadline` (monotonic)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(within(timeout, deadline))
        # A recovery under way finishes its sequence (see RunEngine.close);
        # the latest waits for any before it.
        recovering = getattr(self, "_recovery_thread", None)
        if recovering is not None and recovering.is_alive():
            recovering.join(within(self.RECOVERY_WAIT_S, deadline))

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:                 # never kill the watcher
                pass
            self._stop.wait(self.INTERVAL_S)

    # --- alarms -------------------------------------------------------------

    def _log(self, level: str, message: str, detail: dict | None = None,
             user: str | None = None) -> None:
        """Into the events table -- through the run engine's writer, so that
        a download holding the database does not hold up the watching."""
        run_id, at = self.engine.run_id(), time.time()

        def write():
            self.db.add_event(run_id, level, message, detail, user=user, at=at)

        queue = getattr(self.engine, "_write", None)
        try:
            if queue is not None:
                queue(write, f"{level}: {message}")
            else:
                write()
        except Exception:
            pass

    def raise_alarm(self, key: str, level: str, title: str, detail: str,
                    actions: list | None = None) -> dict:
        """Open an alarm, or update the one already open under this key."""
        now = time.time()
        with self._lock:
            for alarm in self._alarms:
                if alarm["key"] == key and alarm["active"]:
                    escalated = level == "critical" and alarm["level"] != "critical"
                    alarm.update({"title": title, "detail": detail,
                                  "level": "critical" if escalated
                                  else alarm["level"]})
                    if actions:
                        alarm["actions"] = actions
                    if escalated:
                        alarm["acknowledged"] = False
                    return dict(alarm)
            alarm = {"id": self._next_id, "key": key, "level": level,
                     "title": title, "detail": detail, "raised_at": now,
                     "cleared_at": None, "active": True, "acknowledged": False,
                     "actions": actions or []}
            self._next_id += 1
            self._alarms.append(alarm)
            if len(self._alarms) > self.KEEP:
                self._alarms = self._alarms[-self.KEEP:]
        self._log("error" if level == "critical" else "warning",
                  f"ALARM: {title}. {detail}")
        return dict(alarm)

    def clear(self, key: str) -> None:
        """The condition behind an alarm has gone; it stays until acknowledged."""
        cleared = []
        with self._lock:
            for alarm in self._alarms:
                if alarm["key"] == key and alarm["active"]:
                    alarm["active"] = False
                    alarm["cleared_at"] = time.time()
                    cleared.append(alarm["title"])
        for title in cleared:
            self._log("info", f"Cleared: {title}")

    def acknowledge(self, which="all", user=None) -> dict:
        """`user` is whoever acknowledged (lab/auth.py's Principal), or None."""
        count = 0
        with self._lock:
            for alarm in self._alarms:
                if alarm["acknowledged"]:
                    continue
                if which == "all" or alarm["id"] == which:
                    alarm["acknowledged"] = True
                    alarm["acknowledged_at"] = time.time()
                    alarm["acknowledged_by"] = getattr(user, "name", None)
                    count += 1
        if count:
            who = getattr(user, "name", None) or "Operator"
            self._log("action", f"{who} acknowledged {count} alarm"
                                f"{'s' if count != 1 else ''}",
                      user=getattr(user, "username", None))
        return {"ok": True, "acknowledged": count}

    def describe(self) -> dict:
        with self._lock:
            alarms = [dict(a) for a in self._alarms]
        unacknowledged = [a for a in reversed(alarms) if not a["acknowledged"]]
        unacknowledged.sort(key=lambda a: (a["level"] != "critical", -a["raised_at"]))
        return {"alarms": unacknowledged, "recent": list(reversed(alarms))[:40],
                "auto_recover": self.auto_recover, "recovering": self.recovering}

    def set_auto_recover(self, on: bool, user=None) -> dict:
        self.auto_recover = bool(on)
        self._log("action", "Automatic fault recovery switched "
                            + ("on" if on else "off") + " by "
                            + (getattr(user, "name", None) or "the operator"),
                  user=getattr(user, "username", None))
        return {"ok": True, "auto_recover": self.auto_recover}

    # --- watching -----------------------------------------------------------

    def check(self) -> None:
        pump = (self.registry.get(self.pump_key)
                if self.pump_key in self.registry else None)
        gauge = (self.registry.get(self.gauge_key)
                 if self.gauge_key in self.registry else None)
        psnap = pump.snapshot() if pump else None
        gsnap = gauge.snapshot() if gauge else None

        self._watch_link("pump", psnap, "TC 600 turbopump", pump)
        self._watch_link("gauge", gsnap, "TPG gauge controller", gauge)
        self._watch_moves()
        if pump and psnap and not psnap.get("connected"):
            self._pump_lost_at = self._pump_lost_at or time.monotonic()
            self._back_readings = 0
        if (pump and psnap and psnap.get("connected")
                and psnap.get("seq") != self._pump_seq):
            self._pump_seq = psnap.get("seq")
            self._watch_pump(pump, psnap)
        self._watch_runs()
        self._watch_engine()
        self._watch_orphans()
        self._watch_storage()
        if gauge and gsnap and gsnap.get("connected"):
            self._watch_pressure(gauge, gsnap, psnap)

    def _watch_link(self, key: str, snap: dict | None, label: str,
                    service=None) -> None:
        if snap is None:
            return
        now = time.monotonic()
        # "Connected" with a reading nobody has renewed for a while is a
        # sampler held up -- behind a long exchange with a drive gone quiet,
        # say -- and as blind as no contact at all.
        age = snap.get("age")
        limit = (service.stale_after(snap) if hasattr(service, "stale_after")
                 else 15.0)
        if snap.get("connected") and isinstance(age, (int, float)) \
                and age <= limit:
            self._offline_since[key] = None
            self.clear(f"{key}_offline")
            return
        since = self._offline_since.get(key) or now
        self._offline_since[key] = since
        if now - since >= self.OFFLINE_AFTER_S:
            why = (snap.get("error") if not snap.get("connected") else
                   f"its last reading is {age:.0f} s old" if isinstance(
                       age, (int, float)) else "its reading has no age")
            self.raise_alarm(
                f"{key}_offline", "critical" if key == "pump" else "warning",
                f"No contact with the {label}",
                f"Nothing has answered for {now - since:.0f} s ({why}). "
                f"The panel cannot see it or protect it; the instrument keeps "
                f"doing whatever it was last told. Check its power and its "
                f"serial cable.")

    def _watch_moves(self) -> None:
        """An instrument found on another port and reconnected: say so."""
        for service in self.registry:
            moved = getattr(service, "moved", None)
            if not moved or self._moves_seen.get(service.key) == moved["at"]:
                continue
            self._moves_seen[service.key] = moved["at"]
            key = f"{service.key}_moved_{int(moved['at'])}"
            self.raise_alarm(
                key, "warning", f"The {service.label} was found on another port",
                f"It stopped answering on {moved['from']} and answered on "
                f"{moved['to']}"
                + (f" ({moved['device']})" if moved.get("device")
                   and moved["device"] != moved["to"] else "")
                + "; the panel reconnected to it by itself. A replugged USB "
                  "adapter does this.")
            self.clear(key)

    @staticmethod
    def _nominal(pump, snap: dict) -> float:
        """Full speed, never unknown (see PumpService.nominal_speed)."""
        if hasattr(pump, "nominal_speed"):
            return float(pump.nominal_speed(snap))
        return float(snap.get("identity", {}).get("NominalSpd") or 0)

    def _expected_speed(self, snap: dict, nominal: float) -> float:
        status, setpoints = snap.get("status", {}), snap.get("setpoints", {})
        if setpoints.get("Standby") is True and nominal:
            return nominal * float(setpoints.get("StdbySVal") or 66) / 100.0
        if setpoints.get("OpModeTMP") == 1 and nominal:
            return nominal * float(setpoints.get("SpdSVal") or 100) / 100.0
        return float(status.get("SetRotSpd") or nominal or 0)

    def _watch_pump(self, pump, snap: dict) -> None:
        status, setpoints = snap.get("status", {}), snap.get("setpoints", {})
        raw = status.get("ErrorCode")
        code = None if raw is None else str(raw).strip()
        if code in NO_FAULT:
            code = ""
        nominal = self._nominal(pump, snap)
        speed = status.get("ActualSpd")
        speed = float(speed) if isinstance(speed, (int, float)) else None
        now = time.monotonic()
        # The station as it stood before this reading, for a recovery.
        station_before = self._station.value

        # 1. A drive fault, believed once two readings show it. Recovery
        #    runs once per fault: a reading lost in the middle of one is no
        #    reading, not the fault clearing and coming back.
        if self._fault.update(code):
            if self._fault.value and not self._fault.previous \
                    and self.auto_recover:
                self._recover(pump, self._fault.value, station_before)
        if self._fault.value:
            meaning = describe_error(self._fault.value)
            self.raise_alarm(
                "pump_fault", "critical",
                f"TC 600 fault {self._fault.value}: "
                f"{meaning.get('title', 'drive fault')}",
                f"{meaning.get('cause', '')} {meaning.get('remedy', '')}".strip())
        elif self._fault.value == "":
            self.clear("pump_fault")

        # 2. Over temperature: the drive's own verdict -- no value is
        #    published, but it measures motor and electronics internally.
        self._hot_pump.update(status.get("OvTempPump"))
        self._hot_elec.update(status.get("OvTempElec"))
        hot_pump = self._hot_pump.value is True
        hot_elec = self._hot_elec.value is True
        if hot_pump or hot_elec:
            self.raise_alarm(
                "overtemp", "critical",
                f"{'Pump' if hot_pump else 'Drive electronics'} over temperature",
                "The drive measured its "
                + ("motor or pump body" if hot_pump else "electronics")
                + " too hot and is cutting the motor current; the rotor will "
                  "slow towards its switchpoint. Reduce the gas load; check "
                  "the fan and that the room is within 5-40 °C. This firmware "
                  "publishes the verdict, not the temperature.")
        else:
            self.clear("overtemp")

        # 3. A rotor held down by gas: below full speed, motor on, and not
        #    accelerating. The minutes before an Err006 or an over-temperature.
        #    A reading without the speed or the motor flag changes nothing --
        #    it neither starts the clock nor stops it.
        self._motor.update(setpoints.get("MotorPump"))
        expected = self._expected_speed(snap, nominal)
        if speed is not None and self._motor.value is not None:
            held = (self._motor.value is True and speed > 0 and expected
                    and speed < 0.95 * expected
                    and status.get("PumpAccel") is not True)
            if held:
                self._held_since = self._held_since or now
                if now - self._held_since >= self.HELD_AFTER_S:
                    switchpoint = 0.8 * nominal if nominal else None
                    close = nominal and speed < 0.85 * nominal
                    self.raise_alarm(
                        "rotor_held", "critical" if close else "warning",
                        f"Rotor held down by gas load — {speed:.0f} Hz",
                        "Below full speed and not accelerating: there is more "
                        "gas than the drive may carry. Cut the gas flow or "
                        "throttle the gate valve"
                        + (f" — below {switchpoint:.0f} Hz the drive trips "
                           f"(Err006)." if switchpoint else "."))
            else:
                self._held_since = None
                self.clear("rotor_held")

        # 4. The station switched off, and not by this panel. That stops the
        #    forevacuum pump: a turning rotor loses its backing, and the
        #    chamber leaks back up through the stopped pumps.
        if self._station.update(setpoints.get("PumpgStatn")) \
                and self._station.previous is True \
                and self._station.value is False:
            commanded = pump.commanded.get("off")
            ours = commanded is not None and now - commanded < 30
            powering_down = bool((self.engine.shutdown_status() or {}).get("active"))
            if not ours and not powering_down:
                # Without a speed in the reading the rotor may be turning:
                # that is the critical case until a reading says otherwise.
                still = None if speed is None else speed > 0
                self.raise_alarm(
                    "station_off", "warning" if still is False else "critical",
                    "The pumping station was switched off outside this panel",
                    "The forevacuum pump has stopped"
                    + (f" with the rotor still at {speed:.0f} Hz — a coasting "
                       f"rotor needs backing" if still else
                       " and the rotor speed was not read, so it may still "
                       "be turning — a coasting rotor needs backing"
                       if still is None else "")
                    + ". If nobody meant to, switch the station back on from "
                      "the Pump page.")
        if self._station.value is True:
            self.clear("station_off")

        # 5. The rotor started, and not by this panel. The drive runs it
        #    whenever the station and the motor are on with no fault
        #    latched, so a start can come from the front panel, the remote
        #    plug, a fault clearing by itself -- none of which passed the
        #    crossover interlock. Only accepted readings count, so a lost
        #    reply never looks like a start.
        known = None not in (self._station.value, self._motor.value,
                             self._fault.value)
        # Back after a silence: judged on the second reading since, so the
        # flags it is judged on are the drive's now, not from before.
        outage = None
        if self._pump_lost_at is not None:
            self._back_readings += 1
            if self._back_readings >= 2:
                outage = now - self._pump_lost_at
                self._pump_lost_at, self._back_readings = None, 0
        if known:
            driven = (self._station.value is True and self._motor.value is True
                      and self._fault.value == "")
            if driven and self._driven is None:
                # The first state this panel knows, and the rotor is driven:
                # it was started before the panel was watching -- after a
                # power cut that took the PC down too, the drive restarts by
                # itself. Judged once the gauge has had time to answer.
                self._found_driven = now
            if driven and self._driven is False:
                started = pump.commanded.get("motor_start")
                if started is None or now - started > 30:
                    self._started_outside(pump)
            elif (driven and self._driven is not None and outage is not None
                  and outage >= self.OFFLINE_AFTER_S):
                # Driving the rotor after a silence long enough to alarm on:
                # whatever happened meanwhile was not watched. A drive with
                # the factory bridges on its remote plug restarts by itself
                # when its power returns (PM 0504 BE, 4.1), past the
                # crossover rule.
                self._back_driving(pump, outage)
            if not driven:
                self.clear("motor_started")
                self.clear("motor_at_start")
                self._found_driven = None
            self._driven = driven
        if self._found_driven is not None:
            self._judge_found_driven(pump, now)

    #: How long the gauge has to give a reading before a rotor found running
    #: at startup is judged without one.
    FOUND_DRIVEN_WAIT_S = 20.0

    def _judge_found_driven(self, pump, now: float) -> None:
        chamber = (pump.chamber_pressure() if hasattr(pump, "chamber_pressure")
                   else {"mbar": None})
        mbar = chamber.get("mbar")
        if mbar is not None and mbar <= TURBO_START_MAX_MBAR:
            self._found_driven = None              # roughed: nothing to say
            return
        if mbar is None and now - self._found_driven < self.FOUND_DRIVEN_WAIT_S:
            return                                 # give the gauge its chance
        self._found_driven = None
        self.raise_alarm(
            "motor_at_start", "critical",
            "The turbopump was already running when the panel started",
            "The drive is driving the rotor, and this panel did not start it "
            "-- it was running when the panel came up, after a restart or a "
            "power cut (a drive with the factory bridges restarts by itself). "
            "The crossover interlock never checked the chamber, which "
            + (f"is at {mbar:.2E} mbar, above the {TURBO_START_MAX_MBAR:.0E} "
               f"mbar crossover." if mbar is not None else
               "has no pressure reading.")
            + " Switch the motor off unless the chamber is known to be "
              "roughed.")

    def _back_driving(self, pump, silent_s: float) -> None:
        chamber = (pump.chamber_pressure() if hasattr(pump, "chamber_pressure")
                   else {"mbar": None})
        mbar = chamber.get("mbar")
        self.raise_alarm(
            "motor_after_outage",
            "critical" if mbar is None or mbar > TURBO_START_MAX_MBAR
            else "warning",
            "The turbopump is running after a silence the panel did not watch",
            f"There was no contact with the drive for {silent_s:.0f} s, and it "
            f"came back driving the rotor. If its power was cut meanwhile it "
            f"restarted by itself, and the crossover interlock never checked "
            f"the chamber"
            + (f" -- which is at {mbar:.2E} mbar." if mbar is not None
               else " -- whose pressure is not known.")
            + " Check that the rotor is at full speed and the chamber pumped "
              "down.")

    def _started_outside(self, pump) -> None:
        chamber = (pump.chamber_pressure() if hasattr(pump, "chamber_pressure")
                   else {"mbar": None})
        mbar = chamber.get("mbar")
        unsafe = mbar is None or mbar > TURBO_START_MAX_MBAR
        self.raise_alarm(
            "motor_started", "critical" if unsafe else "warning",
            "The turbopump was started without this panel asking",
            "The drive is driving the rotor, but this panel sent no start: it "
            "came from the front panel, the remote plug, or a fault that "
            "cleared by itself, so the crossover interlock never checked the "
            "chamber"
            + (f" -- which is at {mbar:.2E} mbar" if mbar is not None
               else " -- whose pressure is not known")
            + (f". Above {TURBO_START_MAX_MBAR:.0E} mbar the rotor cannot "
               f"develop compression: switch the motor off unless the "
               f"chamber is known to be roughed." if unsafe else "."))

    def _recover(self, pump, code: str, station_before) -> None:
        """Make the pump safe after a new fault, within the retry budget."""
        now = time.monotonic()
        while self._recoveries and now - self._recoveries[0] > self.RETRY_WINDOW_S:
            self._recoveries.popleft()
        if len(self._recoveries) >= self.MAX_RECOVERIES:
            self.raise_alarm(
                "recovery_gave_up", "critical",
                "Automatic fault recovery has stopped trying",
                f"The drive faulted {len(self._recoveries) + 1} times in "
                f"{self.RETRY_WINDOW_S / 60:.0f} min, so the fault is not "
                f"transient. It is left latched for a person to look at: find "
                f"the cause, then use Make safe on the Pump page.")
            return
        self._recoveries.append(now)
        # Keep the backing pump only if it was running before the fault: a
        # station that somebody had deliberately switched off stays off.
        # Not known counts as running.
        station_was_on = station_before is not False
        status = {"active": True, "code": code, "started_at": time.time(),
                  "actions": []}
        self.recovering = status
        previous = getattr(self, "_recovery_thread", None)

        def work():
            # One sequence at a time. A recovery for an earlier fault still
            # under way finishes first; so does the make-safe of a run that
            # ended on this same fault -- waited for, not reported as a
            # failure, and then what it left is checked.
            if previous is not None and previous.is_alive():
                previous.join(self.RECOVERY_WAIT_S)
            result = pump.secure_when_free(f"drive fault {code}",
                                           stop_motor=True,
                                           keep_backing=station_was_on)
            done = ", ".join(a["step"] for a in result.get("actions", [])
                             if a.get("ok")) or "nothing needed changing"
            finished = {**status, "active": False, "ok": bool(result.get("ok")),
                        "actions": result.get("actions", []),
                        "error": result.get("error"), "ended_at": time.time()}
            if self.recovering is status:        # not replaced by a later one
                self.recovering = finished
            with self._lock:
                for alarm in self._alarms:
                    if alarm["key"] == "pump_fault" and alarm["raised_at"] >= \
                            status["started_at"] - 5:
                        alarm["actions"] = result.get("actions", [])
            self._log("action" if result.get("ok") else "error",
                      f"Automatic recovery after fault {code}: {done}."
                      + ("" if result.get("ok") else
                         f" Not confirmed: {result.get('error')}."),
                      {"result": result})

        self._recovery_thread = threading.Thread(target=work, name="auto-recovery",
                                                 daemon=True)
        self._recovery_thread.start()

    def _watch_orphans(self) -> None:
        """A run cut off by a crash or a power cut, found at startup."""
        for orphan in list(getattr(self.engine, "orphans", None) or []):
            key = f"run_{orphan['id']}_cut_off"
            if self._orphans_seen.get(key) == orphan.get("state"):
                continue
            if orphan.get("kind") == "retry" and \
                    orphan["id"] > (self._last_run_seen or 0):
                continue                      # its run's own alarm comes first
            self._orphans_seen[key] = orphan.get("state")
            check = {
                "deposition": " Check by eye that the argon is shut, the "
                              "magnetron supply is off and the shutter is "
                              "closed: the panel cannot see the ones worked "
                              "by hand.",
                "bakeout": " The pump's heater is switched off by that "
                           "clean-up; check the TMS reads it off.",
            }.get(orphan.get("procedure"), "")
            if orphan.get("kind") == "retry":
                # Tried again while the panel runs: the run's own alarm said
                # it failed; this says how the retry went.
                if orphan.get("ok") is True:
                    self.clear(f"run_{orphan['id']}_cleanup_failed")
                elif orphan.get("ok") is False:
                    self.raise_alarm(
                        f"run_{orphan['id']}_retry_failed", "critical",
                        f"Run {orphan['id']} still could not be made safe: "
                        f"{orphan.get('label')}",
                        f"Its clean-up was tried again after the run ended: "
                        f"{orphan.get('state')}.{check} Check the rig by hand "
                        f"now.")
                continue
            if orphan.get("ok") is False:
                # Its own alarm: the cut-off one may be acknowledged by now,
                # and this has to be heard again.
                self.raise_alarm(
                    f"run_{orphan['id']}_cleanup_failed", "critical",
                    f"Run {orphan['id']} could not be made safe: "
                    f"{orphan.get('label')}",
                    f"After the restart, {orphan.get('state')}.{check} Check "
                    f"the rig by hand now.")
            if orphan.get("kind") == "unfinished":
                self.raise_alarm(
                    key, "critical",
                    f"Run {orphan['id']} was not made safe when it ended: "
                    f"{orphan.get('label')}",
                    "Its clean-up -- the heater, the gas or the plasma off -- "
                    "did not succeed when it ended"
                    + (", as a restart of the panel stopped it"
                       if orphan.get("interrupted") else "")
                    + f", so the panel runs it again: {orphan.get('state')}."
                    f"{check}")
                continue
            if orphan.get("kind") == "restart":
                self.raise_alarm(
                    key, "warning",
                    f"Run {orphan['id']} was ended by a restart of the panel: "
                    f"{orphan.get('label')}",
                    f"{orphan.get('state')[:1].upper()}{orphan.get('state')[1:]}."
                    f"{check}")
                continue
            self.raise_alarm(
                key, "critical",
                f"Run {orphan['id']} was cut off: {orphan.get('label')}",
                "The panel stopped while this run was going -- a crash or a "
                "power cut -- so nothing ended it properly. When the panel "
                f"came back, {orphan.get('state')}.{check}")

    def _watch_storage(self) -> None:
        """Database writes failing: the record is being lost."""
        failure = getattr(self.engine, "storage_error", None)
        if not failure or failure.get("count") == self._storage_seen:
            return
        self._storage_seen = failure.get("count")
        self.raise_alarm(
            "storage", "critical", "Runs are not being written to the database",
            f"{failure['count']} writes have failed, the last one with: "
            f"{failure['error']}. The run's safety checks keep running, but its "
            f"samples and its log are being lost. Check the free space and the "
            f"health of the disk or SD card.")

    def _watch_engine(self) -> None:
        """A run that has stopped ticking is protecting nothing."""
        tick_age = getattr(self.engine, "tick_age", None)
        age = tick_age() if tick_age else None
        if age is not None and age > self.ENGINE_STALL_S:
            self.raise_alarm(
                "engine_stalled", "critical",
                "The run has stopped checking the rig",
                f"The run in progress has not looked at the instruments for "
                f"{age:.0f} s, so its checks on the rotor, the pressure and the "
                f"drive are not running. Watch the rig yourself, and stop the "
                f"run if it does not recover.")
        else:
            self.clear("engine_stalled")
        cleanup_age = getattr(self.engine, "cleanup_age", None)
        cleaning = cleanup_age() if cleanup_age else None
        if cleaning is not None and cleaning > self.CLEANUP_STALL_S:
            self.raise_alarm(
                "cleanup_stalled", "critical",
                "A run's clean-up has not finished",
                f"The run ended {cleaning:.0f} s ago and its clean-up -- "
                f"switching the heater, the gas or the plasma off -- has not "
                f"finished. Check those by hand.")
        else:
            self.clear("cleanup_stalled")

    def _watch_runs(self) -> None:
        last = self.engine.status().get("last") or {}
        if not last or last.get("id") == self._last_run_seen:
            return
        self._last_run_seen = last.get("id")
        if last.get("cleanup_failed"):
            # Whatever ended it: the heater, the gas or the plasma may still
            # be on, with nothing watching them.
            self.raise_alarm(
                f"run_{last.get('id')}_cleanup_failed", "critical",
                f"Run {last.get('id')} could not be made safe: "
                f"{last.get('label')}",
                "Its clean-up -- the heater, the gas or the plasma off -- did "
                "not succeed; the run log says which step. Check the rig by "
                "hand now. "
                + (f"The panel tries it again once the pump answers, up to "
                   f"{getattr(self.engine, 'ORPHAN_TRIES', 5)} times a minute "
                   f"apart, and no run can start meanwhile."
                   if last.get("retrying") else
                   "The panel tries it again at its next start."))
        if last.get("status") == "completed" or last.get("by_operator"):
            return
        self.raise_alarm(
            f"run_{last.get('id')}",
            "critical" if last.get("status") == "failed" else "warning",
            f"Run {last.get('id')} {last.get('status')}: {last.get('label')}",
            (last.get("message") or "").rstrip(". ")
            + (". The turbopump motor has been switched off and the forevacuum "
               "pump kept running." if last.get("pump_trouble") else
               ". The pump has been left running."))

    def _watch_pressure(self, gauge, gsnap: dict, psnap: dict | None) -> None:
        now = time.monotonic()
        pressure = gauge.best_pressure(gsnap)
        if isinstance(pressure, (int, float)) and pressure > 0:
            self._pressures.append((now, pressure))
        while self._pressures and now - self._pressures[0][0] > self.JUMP_WINDOW_S:
            self._pressures.popleft()
        if self._jump_at and now - self._jump_at > 120:
            self._jump_at = None
            self.clear("pressure_jump")
        if not self._pressures or psnap is None or not psnap.get("connected"):
            return
        pump = (self.registry.get(self.pump_key)
                if self.pump_key in self.registry else None)
        nominal = self._nominal(pump, psnap)
        speed = psnap.get("status", {}).get("ActualSpd")
        status = self.engine.status()
        in_process = status.get("running") and status.get("phase") in PROCESS_PHASES
        if (not nominal or not isinstance(speed, (int, float))
                or speed < 0.95 * nominal or in_process):
            return
        low_t, low = min(self._pressures, key=lambda item: item[1])
        latest = self._pressures[-1][1]
        if latest > 1e-4 and latest > 10 * low and self._pressures[-1][0] > low_t:
            self._jump_at = now
            self.raise_alarm(
                "pressure_jump", "warning",
                f"Pressure jumped from {low:.1E} to {latest:.1E} mbar",
                f"A tenfold rise in {self._pressures[-1][0] - low_t:.0f} s with "
                f"the turbopump at full speed: gas was let in or a leak "
                f"opened. Watch the rotor — if the gas drags it down, cut "
                f"the gas.")
