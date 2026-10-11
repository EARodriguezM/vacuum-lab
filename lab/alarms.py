"""The rig's supervisor: alarms, and the pump's safe state after a fault.

The run engine watches the rig only while a recipe is recording, and before
this nothing watched it at all otherwise. A fault at three in the morning
went unremarked; worse, a latched drive fault can hold the forevacuum pump
off until somebody acknowledges it, and a chamber left without backing for
hours is a chamber at atmosphere by breakfast.

So this runs all the time, reads the same cached snapshots as everything
else (it never touches a serial port), and does two things:

* **Raises alarms** -- a drive fault, over-temperature, a rotor held down by
  gas, a pressure jump at full speed, the station switched off outside the
  panel, a lost link, a run that ended badly. An alarm stays on screen until
  somebody acknowledges it, even after its condition has cleared, so the
  night's events are still there in the morning. Each is also written to the
  events table.
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

from .pump import describe_error

#: Process phases during which a pressure rise is the recipe's own doing.
PROCESS_PHASES = {"backfill", "presputter", "deposit"}


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
    #: A tenfold pressure rise within this many seconds is a "jump".
    JUMP_WINDOW_S = 30.0
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

        self._prev_pump: dict | None = None
        self._offline_since: dict[str, float | None] = {}
        self._held_since: float | None = None
        self._recoveries: deque = deque()
        self._last_run_seen = (engine.status().get("last") or {}).get("id")
        self._pressures: deque = deque()
        self._jump_at: float | None = None
        self._moves_seen: dict[str, float] = {}
        #: The current or last automatic recovery, for the panel.
        self.recovering: dict | None = None

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> "Supervisor":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="supervisor",
                                            daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check()
            except Exception:                 # never kill the watcher
                pass
            self._stop.wait(self.INTERVAL_S)

    # --- alarms -------------------------------------------------------------

    def _log(self, level: str, message: str, detail: dict | None = None) -> None:
        try:
            self.db.add_event(self.engine.run_id(), level, message, detail)
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

    def acknowledge(self, which="all") -> dict:
        count = 0
        with self._lock:
            for alarm in self._alarms:
                if alarm["acknowledged"]:
                    continue
                if which == "all" or alarm["id"] == which:
                    alarm["acknowledged"] = True
                    alarm["acknowledged_at"] = time.time()
                    count += 1
        if count:
            self._log("action", f"Operator acknowledged {count} alarm"
                                f"{'s' if count != 1 else ''}")
        return {"ok": True, "acknowledged": count}

    def describe(self) -> dict:
        with self._lock:
            alarms = [dict(a) for a in self._alarms]
        unacknowledged = [a for a in reversed(alarms) if not a["acknowledged"]]
        unacknowledged.sort(key=lambda a: (a["level"] != "critical", -a["raised_at"]))
        return {"alarms": unacknowledged, "recent": list(reversed(alarms))[:40],
                "auto_recover": self.auto_recover, "recovering": self.recovering}

    def set_auto_recover(self, on: bool) -> dict:
        self.auto_recover = bool(on)
        self._log("action", "Automatic fault recovery switched "
                            + ("on" if on else "off") + " by the operator")
        return {"ok": True, "auto_recover": self.auto_recover}

    # --- watching -----------------------------------------------------------

    def check(self) -> None:
        pump = (self.registry.get(self.pump_key)
                if self.pump_key in self.registry else None)
        gauge = (self.registry.get(self.gauge_key)
                 if self.gauge_key in self.registry else None)
        psnap = pump.snapshot() if pump else None
        gsnap = gauge.snapshot() if gauge else None

        self._watch_link("pump", psnap, "TC 600 turbopump")
        self._watch_link("gauge", gsnap, "TPG gauge controller")
        self._watch_moves()
        if pump and psnap and psnap.get("connected"):
            self._watch_pump(pump, psnap)
            self._prev_pump = psnap
        self._watch_runs()
        if gauge and gsnap and gsnap.get("connected"):
            self._watch_pressure(gauge, gsnap, psnap)

    def _watch_link(self, key: str, snap: dict | None, label: str) -> None:
        if snap is None:
            return
        now = time.monotonic()
        if snap.get("connected"):
            self._offline_since[key] = None
            self.clear(f"{key}_offline")
            return
        since = self._offline_since.get(key) or now
        self._offline_since[key] = since
        if now - since >= self.OFFLINE_AFTER_S:
            self.raise_alarm(
                f"{key}_offline", "critical" if key == "pump" else "warning",
                f"No contact with the {label}",
                f"Nothing has answered for {now - since:.0f} s ({snap.get('error')}). "
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

    def _expected_speed(self, snap: dict) -> float:
        status, setpoints = snap.get("status", {}), snap.get("setpoints", {})
        nominal = float(snap.get("identity", {}).get("NominalSpd") or 0)
        if setpoints.get("Standby") is True and nominal:
            return nominal * float(setpoints.get("StdbySVal") or 66) / 100.0
        if setpoints.get("OpModeTMP") == 1 and nominal:
            return nominal * float(setpoints.get("SpdSVal") or 100) / 100.0
        return float(status.get("SetRotSpd") or nominal or 0)

    def _watch_pump(self, pump, snap: dict) -> None:
        status, setpoints = snap.get("status", {}), snap.get("setpoints", {})
        prev = self._prev_pump or {}
        prev_status = prev.get("status", {})
        code = str(status.get("ErrorCode") or "").strip()
        faulted = code not in ("", "000000", "000", "0")
        prev_code = str(prev_status.get("ErrorCode") or "").strip()
        was_faulted = prev_code not in ("", "000000", "000", "0")
        nominal = float(snap.get("identity", {}).get("NominalSpd") or 0)
        speed = status.get("ActualSpd")
        speed = float(speed) if isinstance(speed, (int, float)) else 0.0

        # 1. A drive fault.
        if faulted:
            meaning = describe_error(code)
            self.raise_alarm(
                "pump_fault", "critical",
                f"TC 600 fault {code}: {meaning.get('title', 'drive fault')}",
                f"{meaning.get('cause', '')} {meaning.get('remedy', '')}".strip())
            if not was_faulted and self.auto_recover:
                self._recover(pump, code, prev)
        else:
            self.clear("pump_fault")

        # 2. Over temperature: the drive's own verdict -- no value is
        #    published, but it measures motor and electronics internally.
        hot_pump = status.get("OvTempPump") is True
        hot_elec = status.get("OvTempElec") is True
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
        expected = self._expected_speed(snap)
        held = (setpoints.get("MotorPump") is True and speed > 0 and expected
                and speed < 0.95 * expected and status.get("PumpAccel") is not True)
        now = time.monotonic()
        if held:
            self._held_since = self._held_since or now
            if now - self._held_since >= self.HELD_AFTER_S:
                switchpoint = 0.8 * nominal if nominal else None
                close = nominal and speed < 0.85 * nominal
                self.raise_alarm(
                    "rotor_held", "critical" if close else "warning",
                    f"Rotor held down by gas load — {speed:.0f} Hz",
                    "Below full speed and not accelerating: there is more gas "
                    "than the drive may carry. Cut the gas flow or throttle "
                    "the gate valve"
                    + (f" — below {switchpoint:.0f} Hz the drive trips (Err006)."
                       if switchpoint else "."))
        else:
            self._held_since = None
            self.clear("rotor_held")

        # 4. The station switched off, and not by this panel. That stops the
        #    forevacuum pump: a turning rotor loses its backing, and the
        #    chamber leaks back up through the stopped pumps.
        station = setpoints.get("PumpgStatn") is True
        was_on = prev.get("setpoints", {}).get("PumpgStatn") is True
        if was_on and not station:
            commanded = pump.commanded.get("off")
            ours = commanded is not None and now - commanded < 30
            powering_down = bool((self.engine.shutdown_status() or {}).get("active"))
            if not ours and not powering_down:
                self.raise_alarm(
                    "station_off", "critical" if speed > 0 else "warning",
                    "The pumping station was switched off outside this panel",
                    "The forevacuum pump has stopped"
                    + (f" with the rotor still at {speed:.0f} Hz — a coasting "
                       f"rotor needs backing" if speed > 0 else "")
                    + ". If nobody meant to, switch the station back on from "
                      "the Pump page.")
        if station:
            self.clear("station_off")

    def _recover(self, pump, code: str, prev: dict) -> None:
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
        station_was_on = prev.get("setpoints", {}).get("PumpgStatn") is not False
        self.recovering = {"active": True, "code": code, "started_at": time.time(),
                           "actions": []}

        def work():
            result = pump.secure(f"drive fault {code}", stop_motor=True,
                                 keep_backing=station_was_on)
            done = ", ".join(a["step"] for a in result.get("actions", [])
                             if a.get("ok")) or "nothing needed changing"
            self.recovering = {**self.recovering, "active": False,
                               "ok": bool(result.get("ok")),
                               "actions": result.get("actions", []),
                               "error": result.get("error"),
                               "ended_at": time.time()}
            with self._lock:
                for alarm in self._alarms:
                    if alarm["key"] == "pump_fault" and alarm["raised_at"] >= \
                            self.recovering["started_at"] - 5:
                        alarm["actions"] = result.get("actions", [])
            self._log("action" if result.get("ok") else "error",
                      f"Automatic recovery after fault {code}: {done}."
                      + ("" if result.get("ok") else
                         f" Not confirmed: {result.get('error')}."),
                      {"result": result})

        threading.Thread(target=work, name="auto-recovery", daemon=True).start()

    def _watch_runs(self) -> None:
        last = self.engine.status().get("last") or {}
        if not last or last.get("id") == self._last_run_seen:
            return
        self._last_run_seen = last.get("id")
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
        nominal = float(psnap.get("identity", {}).get("NominalSpd") or 0)
        speed = psnap.get("status", {}).get("ActualSpd") or 0
        status = self.engine.status()
        in_process = status.get("running") and status.get("phase") in PROCESS_PHASES
        if not nominal or speed < 0.95 * nominal or in_process:
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
