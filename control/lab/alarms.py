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

It also watches the panel itself: the chart recorder's heartbeat, the
backups (`backup_status`, written by tools/backup.py), and -- with
`persist` -- the gap a restart left: its own heartbeat is kept in the
database, so the next start can say how long nothing was watching, and
whether the last one stopped cleanly. With `persist` the alarms nobody has
acknowledged are kept there too, and are back on screen after a restart.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque

from . import logs
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
    #: How often the heartbeat is written to the database, with `persist`.
    ALIVE_EVERY_S = 60.0
    #: How often the backup status is read, and how old a good backup may be.
    BACKUP_CHECK_S = 60.0
    BACKUP_MAX_AGE_S = 48 * 3600.0
    #: A chart recorder silent this long has stopped.
    RECORDER_STALL_S = 30.0
    #: How often `on_tick` (the status file) is called, at most.
    TICK_HOOK_S = 5.0

    def __init__(self, registry, engine, database, auto_recover: bool = True,
                 pump_key: str = "pump", gauge_key: str = "gauge",
                 persist: bool = False, backup_status: str | None = None,
                 recorder=None):
        self.registry = registry
        self.engine = engine
        self.db = database
        self.auto_recover = auto_recover
        self.pump_key = pump_key
        self.gauge_key = gauge_key
        #: Keep the alarms and the heartbeat in the database (the real panel;
        #: tests that share a database between supervisors leave it off).
        self.persist = persist
        #: tools/backup.py's status beside the database, or None to not watch.
        self.backup_status = backup_status
        self._backup_checked = None
        #: The chart recorder, whose heartbeat is watched; or None.
        self.recorder = recorder
        #: Called every TICK_HOOK_S from the watching thread (the status
        #: file); its failures are logged, never raised.
        self.on_tick = None
        self._ticked = None
        #: When the watching loop last went round (monotonic), for the panel.
        self.beat = None
        self._alive_written = None
        self._alarms_dirty = False
        self._alarms_saved = None

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
        #: What the last panel left: its heartbeat, read once, announced at
        #: the first look (`_announce_restart`).
        self._previous_life = None
        if persist:
            self._restore()

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> "Supervisor":
        if self._thread is None:
            self._write_alive(clean=False)
            self._thread = threading.Thread(target=self._loop, name="supervisor",
                                            daemon=True)
            self._thread.start()
        return self

    def beat_age(self) -> float | None:
        """Seconds since the watching loop last went round, or None."""
        return None if self.beat is None else time.monotonic() - self.beat

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
            self.beat = time.monotonic()
            try:
                self.check()
            except Exception as exc:          # never kill the watcher --
                logs.error("supervisor", exc)  # but never say nothing either
            self._tick_hook()
            self._stop.wait(self.INTERVAL_S)

    def _tick_hook(self) -> None:
        now = time.monotonic()
        if self.on_tick is None or (self._ticked is not None
                                    and now - self._ticked < self.TICK_HOOK_S):
            return
        self._ticked = now
        try:
            self.on_tick()
        except Exception as exc:
            logs.error("status file", exc)

    # --- what outlives the process -------------------------------------------

    def _store(self, write, what: str) -> None:
        """A database write through the run engine's writer, so a download
        holding the database never holds up the watching."""
        queue = getattr(self.engine, "_write", None)
        try:
            if queue is not None:
                queue(write, what)
            else:
                write()
        except Exception as exc:
            logs.error(f"supervisor: {what}", exc)

    def _write_alive(self, clean: bool) -> None:
        """The heartbeat: when this panel was last watching. Written every
        ALIVE_EVERY_S -- which also finds out, without a run going, that the
        database cannot be written."""
        if not self.persist:
            return
        self._alive_written = time.monotonic()
        value = json.dumps({"at": time.time(), "clean": clean,
                            "pid": os.getpid()})
        self._store(lambda: self.db.set_meta("alive", value), "heartbeat")

    def mark_clean_stop(self) -> None:
        """The last thing written before the database closes, and written
        here and now rather than queued: a clean stop that never reached the
        disk reads, at the next start, as a crash. The alarms nobody
        acknowledged go with it."""
        if not self.persist:
            return
        try:
            self._flush_alarms(force=True, now=True)
            self.db.set_meta("alive", json.dumps(
                {"at": time.time(), "clean": True, "pid": os.getpid()}))
        except Exception as exc:
            logs.error("supervisor: the clean stop", exc)

    #: The alarms are kept at most this often: a link flapping all night
    #: would otherwise rewrite them on every flap, on an SD card.
    SAVE_EVERY_S = 5.0

    def _save_alarms(self) -> None:
        """Mark the alarms for keeping; the watching loop writes them."""
        if self.persist:
            self._alarms_dirty = True

    def _flush_alarms(self, force: bool = False, now: bool = False) -> None:
        if not self.persist or not getattr(self, "_alarms_dirty", False):
            return
        if not force and self._alarms_saved is not None and \
                time.monotonic() - self._alarms_saved < self.SAVE_EVERY_S:
            return
        self._alarms_dirty = False
        self._alarms_saved = time.monotonic()
        with self._lock:
            kept = [{k: a.get(k) for k in ("id", "key", "level", "title",
                                           "detail", "raised_at", "cleared_at",
                                           "actions", "restored")}
                    for a in self._alarms if not a["acknowledged"]]
            value = json.dumps({"next_id": self._next_id, "alarms": kept})
        if now:
            self.db.set_meta("alarms", value)
        else:
            self._store(lambda: self.db.set_meta("alarms", value), "alarms")

    #: Added to an alarm kept across a restart -- once.
    RESTORED_NOTE = " (Raised before the panel was restarted.)"

    def _restore(self) -> None:
        """What the last panel left: its heartbeat and its alarms. Whatever is
        in the database, this never stops the panel starting -- a store
        written by another release, say, after a rollback."""
        try:
            alive = self.db.meta("alive")
            kept = self.db.meta("alarms")
        except Exception as exc:
            logs.error("supervisor: reading what the last panel left", exc)
            return
        try:
            life = json.loads(alive) if alive else None
            self._previous_life = life if isinstance(life, dict) else None
        except ValueError:
            self._previous_life = None
        try:
            kept = json.loads(kept) if kept else []
        except ValueError:
            kept = []
        if isinstance(kept, dict):
            next_id, kept = kept.get("next_id"), kept.get("alarms")
        else:
            next_id = None
        if isinstance(next_id, int) and next_id > self._next_id:
            self._next_id = next_id            # ids never repeat across starts
        for old in kept if isinstance(kept, list) else []:
            try:
                self._restore_one(old)
            except Exception as exc:
                logs.error("supervisor: an alarm kept from before", exc)

    def _restore_one(self, old) -> None:
        if not isinstance(old, dict):
            return

        def text(value, default):
            return value if isinstance(value, str) and value else default

        def stamp(value):
            return value if isinstance(value, (int, float)) else time.time()
        detail = text(old.get("detail"), "")
        if not detail.endswith(self.RESTORED_NOTE):
            detail += self.RESTORED_NOTE
        level = old.get("level") if old.get("level") in ("warning",
                                                          "critical") else "warning"
        actions = old.get("actions") if isinstance(old.get("actions"), list) else []
        with self._lock:
            # Its condition is not known yet: shown as cleared, kept until
            # acknowledged. If it still holds, it is raised again, new.
            self._alarms.append({
                "id": self._next_id, "key": text(old.get("key"), "restored"),
                "level": level, "title": text(old.get("title"), "Alarm"),
                "detail": detail, "raised_at": stamp(old.get("raised_at")),
                "cleared_at": stamp(old.get("cleared_at")), "active": False,
                "acknowledged": False, "restored": True,
                "actions": [a for a in actions if isinstance(a, str)]})
            self._next_id += 1

    def _announce_restart(self) -> None:
        """Once, at the first look: the panel was not watching for a while."""
        life, self._previous_life = self._previous_life, None
        if not life or not isinstance(life.get("at"), (int, float)):
            return
        stopped = life["at"]
        gap = time.time() - stopped
        clock = time.strftime("%H:%M", time.localtime(stopped))
        span = (f"for about {gap / 60:.0f} min" if 0 <= gap < 7 * 86400 else
                "for an unknown time (the clock has moved)")
        key = "panel_restarted"
        if life.get("clean"):
            self.raise_alarm(
                "panel_restarted", "warning", "The panel was restarted",
                f"It stopped at {clock} and was not watching the rig {span}. "
                f"Nothing was recorded or alarmed in that time; the pump kept "
                f"doing what it was last told.")
        else:
            self.raise_alarm(
                "panel_restarted", "critical", "The panel stopped unexpectedly",
                f"Its last sign of life was at {clock}, and it did not stop "
                f"cleanly -- a crash or a power cut. Nothing watched the rig "
                f"{span}. Check the pump and the chamber, and the journal "
                f"(journalctl -u tc600-web) for why.")
        # An event, not a condition: on screen until acknowledged, never
        # "active" -- nothing will ever clear it, and a release counts the
        # active critical alarms a restart leaves.
        self.clear(key)

    def _watch_backup(self) -> None:
        if not self.backup_status:
            return
        now = time.monotonic()
        if self._backup_checked is not None and \
                now - self._backup_checked < self.BACKUP_CHECK_S:
            return
        self._backup_checked = now
        try:
            with open(self.backup_status, encoding="utf-8") as fh:
                status = json.load(fh)
        except FileNotFoundError:
            status = None
        except (OSError, ValueError) as exc:
            status = {"last_ok": False, "last_error": f"its status is "
                      f"unreadable: {exc}", "last_attempt": time.time()}
        how = ("Set the backups up (deploy/tc600-backup.service) and check "
               "with ./tools/backup.py status.")
        if not status:
            self.raise_alarm("backup", "warning",
                             "No backup of this database has been made",
                             f"The runs, the accounts and the rig's settings "
                             f"exist only here. {how}")
            return
        success = status.get("last_success")
        if not status.get("last_ok"):
            self.raise_alarm("backup", "warning", "The last backup failed",
                             f"{status.get('last_error') or 'no reason given'}. "
                             + (f"The last good one is "
                                f"{(time.time() - success) / 3600:.0f} h old."
                                if isinstance(success, (int, float)) else
                                "There is no good one yet."))
        elif not isinstance(success, (int, float)) or \
                time.time() - success > self.BACKUP_MAX_AGE_S:
            age = (time.time() - success) / 86400 \
                if isinstance(success, (int, float)) else None
            self.raise_alarm("backup", "warning",
                             "The last good backup is "
                             + (f"{age:.0f} days old" if age is not None
                                else "of unknown age"),
                             f"Backups are made every six hours; none has worked "
                             f"for more than {self.BACKUP_MAX_AGE_S / 3600:.0f} h. "
                             f"{how}")
        else:
            self.clear("backup")

    def _watch_recorder(self) -> None:
        age = self.recorder.beat_age() if self.recorder is not None and \
            hasattr(self.recorder, "beat_age") else None
        if age is not None and age > self.RECORDER_STALL_S:
            self.raise_alarm(
                "recorder_stalled", "warning", "The chart recorder has stopped",
                f"Nothing has been added to the charts for {age:.0f} s. Runs "
                f"still record; the pump and gauge pages do not. The journal "
                f"says why (journalctl -u tc600-web -p err).")
        else:
            self.clear("recorder_stalled")

    # --- alarms -------------------------------------------------------------

    def _log(self, level: str, message: str, detail: dict | None = None,
             user: str | None = None) -> None:
        """Into the events table -- through the run engine's writer, so that
        a download holding the database does not hold up the watching."""
        run_id, at = self.engine.run_id(), time.time()

        def write():
            self.db.add_event(run_id, level, message, detail, user=user, at=at)

        self._store(write, f"{level}: {message}")

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
                        self._alarms_dirty = True
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
        self._save_alarms()
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
        if cleared:
            self._save_alarms()

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
            self._save_alarms()
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
        if self._previous_life is not None:
            self._announce_restart()
        if self.persist and (self._alive_written is None or time.monotonic()
                             - self._alive_written >= self.ALIVE_EVERY_S):
            self._write_alive(clean=False)
        self._flush_alarms()
        self._watch_runs()
        self._watch_engine()
        self._watch_orphans()
        self._watch_storage()
        self._watch_backup()
        self._watch_recorder()
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
            sampler = getattr(service, "_thread", None)
            if sampler is not None and not sampler.is_alive():
                why = ("the panel's own sampler for it has stopped -- a fault "
                       "in the panel, not the instrument: restart the panel, "
                       "and see journalctl -u tc600-web -p err")
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

        thread = threading.Thread(target=work, name="auto-recovery",
                                  daemon=True)
        try:
            thread.start()
        except RuntimeError as exc:
            # No thread to be had -- the process is at its task limit. The
            # pump is made safe all the same, here on the watcher's thread:
            # alarms wait for one sequence, a faulted pump does not wait.
            self.raise_alarm(
                "recovery_thread", "critical",
                "Automatic recovery ran without its own thread",
                f"The panel could not start a thread ({exc}): too many "
                f"connections or threads at once. The pump is being made safe "
                f"anyway; check the Pump page, and who is connecting to the "
                f"panel.")
            self._recovery_thread = None
            work()
            return
        self._recovery_thread = thread

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
