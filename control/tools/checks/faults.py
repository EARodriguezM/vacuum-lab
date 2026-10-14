"""Faults on the bus, and the rules that must hold anyway.

The simulators answer perfectly unless told otherwise, and the real bus does
not: replies are lost, arrive late, or come back for the wrong question.
Each check below makes one such thing happen and asserts a rule that must
hold anyway. Each one that covers a fix failed against the code before it --
which is the point of writing them first; the rest (the rig's gauge layout,
a fault that returns) keep behaviour that was already right from breaking."""

from __future__ import annotations

import os
import tempfile
import time

from tc600 import TC600, protocol
from lab import db as dbmod
from lab import pump as pumpmod
from lab import registry, runs
from tools import fake_tc600, fake_tpg
from tools.checks.common import (
    wait_until, attempt, Rig, watching, _QuietEngine)


def test_faults(pump_port: str, gauge_port: str) -> None:
    print("\nFaults on the bus, and the rules that must hold anyway")
    from lab import alarms
    from lab import api as apimod

    directory = tempfile.mkdtemp(prefix="selftest-faults-")
    database = dbmod.Database(os.path.join(directory, "faults.db"))
    limit = pumpmod.TURBO_START_MAX_MBAR

    # --- the driver: a late reply is not the next question's answer --------
    def late_reply():
        fake_tc600.reset()
        fake_tc600.FAULTS.update({"value": {309: 1500}, "late": {309: 1}})
        with TC600(pump_port, address=1, timeout=0.3, retries=0) as pump:
            try:
                pump.read_raw(309)          # its reply comes after it gave up
            except protocol.ProtocolError:
                pass
            code = pump.read("ErrorCode")
            speed = pump.read("ActualSpd")
        return (code == "000000" and speed == 1500,
                f"error code read as {code!r}, then speed {speed}")
    attempt("a reply that arrives late is never taken for the next parameter's",
            late_reply)

    def echoing_converter():
        fake_tc600.reset()
        fake_tc600.FAULTS["echo"] = True         # every request looped back
        with TC600(pump_port, address=1, timeout=0.5, retries=0) as pump:
            speed = pump.read("ActualSpd")
            learned = pump.echoes
            written = pump.write("Standby", True)
            standby = fake_tc600.STATE[2]
        fake_tc600.reset()
        return (speed == 0 and learned is True and written is True
                and standby is True,
                f"speed {speed}, echo learned {learned}, write read back "
                f"{written}")
    attempt("an RS-485 converter that echoes is learned, and its echo never "
            "taken for the reply", echoing_converter)

    # --- every combination of what the pump reading can say ------------------
    service = pumpmod.PumpService("table", "auto")
    service.close()
    service._thread.join(timeout=5)

    class Gauge:
        """Stands in for the gauge: a pressure, or none."""
        mbar = None

        def snapshot(self):
            return {"connected": True, "age": 0.5}

        def best_pressure(self, snap=None):
            return self.mbar

    gauge = Gauge()
    service.attach_pressure(gauge)

    def table():
        import itertools

        max_age = getattr(pumpmod, "SNAPSHOT_MAX_AGE_S", 5.0)
        breaches, cases = [], 0
        for (connected, station, motor, speed, fault, mbar, age,
             dwell) in itertools.product(
                (True, False),                   # contact
                (True, False, None),             # station: on, off, not read
                (True, False, None),             # motor flag
                (None, 0, 4, 1500),              # speed: unread, rest, windmill, top
                ("000000", "Err006", None),      # error code
                (None, 1e-3, 5.0),               # chamber: unknown, roughed, not
                (0.5, 30.0),                     # age of the reading, s
                (None, 5.0, 60.0)):              # seconds since 0 Hz was first read
            cases += 1
            now = time.monotonic()
            gauge.mbar = mbar
            service._rotor_zero_since = None if dwell is None else now - dwell
            service._speed_unread = 0
            status, setpoints = {}, {}
            for target, name, value in ((status, "ActualSpd", speed),
                                        (status, "ErrorCode", fault),
                                        (setpoints, "PumpgStatn", station),
                                        (setpoints, "MotorPump", motor)):
                if value is not None:
                    target[name] = value
            rules = service.interlocks({
                "connected": connected, "age": age, "status": status,
                "setpoints": setpoints, "identity": {"NominalSpd": 1500}})
            fresh = connected and age <= max_age
            settled = dwell is not None and dwell >= pumpmod.STOP_DWELL_SECONDS
            may_stop = (fresh and speed == 0 and settled
                        and not (station is True and motor is True))
            may_start = fresh and (motor is True or (
                station is True and mbar is not None and mbar <= limit))
            case = (f"station {station}, motor {motor}, speed {speed}, fault "
                    f"{fault}, {mbar} mbar, {age} s old, dwell {dwell}")
            if rules.get("off", {}).get("allowed") and not may_stop:
                breaches.append(f"station off with {case}")
            if rules.get("motor_on", {}).get("allowed") and not may_start:
                breaches.append(f"motor start with {case}")
        return (not breaches, f"{len(breaches)} unsafe of {cases} combinations"
                + (f", e.g. {breaches[0]}" if breaches else ""))
    attempt("no combination of readings lets the station stop or the turbopump "
            "start unsafely", table)

    def vent(snap):
        try:
            service._check_param("EnableVent", 1, snap)
        except pumpmod.Interlock as stop:
            return True, stop.reason
        return False, "venting was allowed"
    attempt("venting is refused while the rotor speed is unknown",
            lambda: vent({"connected": True, "age": 0.5, "status": {},
                          "setpoints": {}, "identity": {"NominalSpd": 1500}}))
    attempt("venting at full speed is refused before the nominal speed is known",
            lambda: vent({"connected": True, "age": 0.5,
                          "status": {"ActualSpd": 1500}, "setpoints": {},
                          "identity": {}}))

    # --- the run's dragged-rotor clock survives a lost reading -----------------
    class Nominal(_QuietEngine):
        def nominal_speed(self):
            return 1500.0

    def dragged_through(gap):
        engine = Nominal()
        recipe = runs.FullVacuum({})
        recipe.begin(engine)
        recipe.full_speed_at = 0.0
        t = 10.0
        try:
            for _ in range(50):                       # 100 s held at 90 %
                recipe.check_rotor(engine, {"t": t, "speed": 1350.0, "motor": 1})
                t += 2.0
            recipe.check_rotor(engine, {"t": t, **gap})
            t += 2.0
            for _ in range(20):                       # and 40 s more
                recipe.check_rotor(engine, {"t": t, "speed": 1350.0, "motor": 1})
                t += 2.0
        except runs.Abort as abort:
            return True, abort.reason[:60]
        return False, "two and a half minutes held down, and the run went on"
    attempt("a lost speed reading does not restart the two-minute clock",
            lambda: dragged_through({"speed": None, "motor": 1}))
    attempt("nor does a lost motor-flag reading",
            lambda: dragged_through({"speed": 1350.0, "motor": None}))

    # --- three ways to start the rotor, only one of them was checked -----------
    def station_on_with_motor_set():
        fake_tc600.reset()
        fake_tc600.STATE[23] = True              # the motor flag left set
        with Rig(pump_port) as rig:
            result = rig.pump.command("on")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (result.get("ok") and fake_tc600.STATE[10] is True and spun == 0
                and fake_tc600.STATE[23] is False,
                f"rotor at {spun:.0f} Hz, motor flag {fake_tc600.STATE[23]}: "
                f"{result.get('error') or 'sent'}")
    attempt("starting the station with the motor flag set does not start the "
            "rotor", station_on_with_motor_set)

    def ack_with_motor_set():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (spun == 0 and str(fake_tc600.STATE[303]) == "000000"
                and fake_tc600.STATE[23] is False,
                f"rotor at {spun:.0f} Hz, fault {fake_tc600.STATE[303]}: "
                f"{result.get('error') or 'sent'}")
    attempt("acknowledging a fault with the motor on does not restart the rotor",
            ack_with_motor_set)

    def secure_motor_unread():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["drop"] = {23: -1}     # the motor flag never answers
        with Rig(pump_port) as rig:
            unread = rig.fresh(lambda s: s.get("connected")
                               and "MotorPump" not in s.get("setpoints", {}))
            rig.pump.secure("selftest", settle_s=3.0)
            time.sleep(2.0)
            spun = fake_tc600._speed
        # The motor-off was sent (the drive did it), but with no reply and no
        # reading to confirm it the fault is left latched: acknowledging it
        # unconfirmed is what restarted the rotor.
        return (unread is not None and spun == 0
                and fake_tc600.STATE[23] is False
                and fake_tc600.STATE[303] == "Err006",
                f"rotor at {spun:.0f} Hz, motor flag {fake_tc600.STATE[23]}, "
                f"fault {fake_tc600.STATE[303]}")
    attempt("making safe switches a motor it cannot read off before clearing "
            "the fault", secure_motor_unread)

    attempt("acknowledging a fault needs the operator's confirmation",
            lambda: ("ack" in apimod.CONFIRM_REQUIRED, "ack is guarded"
                     if "ack" in apimod.CONFIRM_REQUIRED else "ack is not"))

    def station_on_acknowledges():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["station_acks"] = True  # P010 = 1 also acknowledges
        with Rig(pump_port) as rig:
            result = rig.pump.command("on")
            time.sleep(2.5)
            spun = fake_tc600._speed
        return (result.get("ok") and spun == 0
                and fake_tc600.STATE[303] == "Err006",
                f"rotor at {spun:.0f} Hz, fault {fake_tc600.STATE[303]}")
    attempt("\"station on\" with the station already on sends nothing that "
            "could clear a fault", station_on_acknowledges)

    def echo_lost_motor_off():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["drop_write"] = {23: -1}   # the write works, unheard
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (result.get("ok") and fake_tc600.STATE[303] == "000000"
                and fake_tc600.STATE[23] is False and spun == 0,
                f"{result.get('error') or 'acknowledged'}; rotor {spun:.0f} Hz")
    attempt("a motor-off whose reply is lost, but which the drive confirms, "
            "lets the fault be cleared", echo_lost_motor_off)

    def motor_write_refused():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True, 303: "Err006"})
        fake_tc600.FAULTS["refuse_write"] = {23: "_LOGIC"}
        with Rig(pump_port) as rig:
            result = rig.pump.command("ack")
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (not result.get("ok") and result.get("rule") == "motor_off_first"
                and fake_tc600.STATE[303] == "Err006" and spun == 0,
                f"{(result.get('error') or 'acknowledged')[:60]}; rule "
                f"{result.get('rule')}; rotor {spun:.0f} Hz")
    attempt("a drive that refuses the motor-off keeps its fault until a person "
            "decides, and says which rule", motor_write_refused)

    def waived_motor_off_is_a_start():
        fake_tc600.reset()
        fake_tc600.STATE[23] = True              # motor flag set, station off
        with Rig(pump_port) as rig:          # no gauge: the pressure unknown
            result = rig.pump.command("on", override=["motor_off_first"])
            time.sleep(2.0)
            spun = fake_tc600._speed
        return (not result.get("ok") and result.get("rule") == "pressure_unknown"
                and fake_tc600.STATE[10] is False and spun == 0,
                f"refused by {result.get('rule')}; station "
                f"{fake_tc600.STATE[10]}, rotor {spun:.0f} Hz")
    attempt("waiving \"motor off first\" does not waive the start's pressure "
            "rule", waived_motor_off_is_a_start)

    def waived_ack():
        fake_tc600.reset()
        fake_tc600.STATE.update({23: True, 303: "Err006"})   # station off
        with Rig(pump_port) as rig:
            off = rig.pump.command("ack", override=["motor_off_first"])
            cleared = fake_tc600.STATE[303] == "000000"
            fake_tc600.STATE.update({10: True, 303: "Err006"})
            rig.fresh(lambda s: s.get("setpoints", {}).get("PumpgStatn") is True)
            on = rig.pump.command("ack", override=["motor_off_first"])
            time.sleep(1.5)
            spun = fake_tc600._speed
        return (off.get("ok") and cleared and not on.get("ok")
                and on.get("rule") == "pressure_unknown" and spun == 0,
                f"station off: {off.get('error') or 'acknowledged'}; station on: "
                f"refused by {on.get('rule')}, rotor {spun:.0f} Hz")
    attempt("a waived \"motor off first\" on acknowledge is judged as a start "
            "only when the station is on", waived_ack)

    def found_running_at_start():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0               # running before the panel
        saved = getattr(alarms.Supervisor, "FOUND_DRIVEN_WAIT_S", None)
        alarms.Supervisor.FOUND_DRIVEN_WAIT_S = 2.0
        try:
            with Rig(pump_port) as rig:          # no gauge: pressure unknown
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 6.0)
                found = [a for a in supervisor.describe()["recent"]
                         if a["key"] == "motor_at_start"]
        finally:
            if saved is None:
                del alarms.Supervisor.FOUND_DRIVEN_WAIT_S
            else:
                alarms.Supervisor.FOUND_DRIVEN_WAIT_S = saved
        return bool(found), found[0]["title"] if found else "nothing said"
    attempt("a turbopump found running at startup with the pressure unknown "
            "raises an alarm", found_running_at_start)

    def deploy_under_vacuum():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-6        # pumped down
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        with Rig(pump_port, gauge_port) as rig:
            wait_until(lambda: rig.gauge.best_pressure() is not None, 10, 0.1)
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 6.0)
            keys = [a["key"] for a in supervisor.describe()["recent"]]
        return ("motor_at_start" not in keys and "motor_started" not in keys
                and "motor_after_outage" not in keys,
                f"alarms: {keys or 'none'}")
    attempt("restarting the panel with the turbopump at speed under vacuum "
            "raises nothing", deploy_under_vacuum)

    def late_drive_under_vacuum():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-6
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        fake_tc600.FAULTS["silent"] = True       # the drive answers late
        saved = alarms.Supervisor.OFFLINE_AFTER_S
        alarms.Supervisor.OFFLINE_AFTER_S = 2.0
        try:
            with Rig(pump_port, gauge_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 5.0)
                fake_tc600.FAULTS.clear()
                watching(supervisor, 6.0)
                keys = [a["key"] for a in supervisor.describe()["recent"]]
        finally:
            alarms.Supervisor.OFFLINE_AFTER_S = saved
        return ("motor_after_outage" not in keys and "motor_at_start" not in keys,
                f"alarms: {keys}")
    attempt("a drive that answers late, running under vacuum, is not taken for "
            "one restarted after an outage", late_drive_under_vacuum)

    # --- the reading the decisions are made on, at the real bus's speed ---------
    def slow_bus():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["delay_s"] = 0.08      # ~ the real 9600 baud bus
        stale, judged, dwell_restarts = 0, 0, 0
        lose = ("ErrorCode", "MotorPump", "PumpgStatn", "DrvPower")
        numbers = {"ErrorCode": 303, "MotorPump": 23, "PumpgStatn": 10,
                   "DrvPower": 316}
        with Rig(pump_port, interval=None) as rig:
            rig.fresh()
            since = rig.pump._rotor_zero_since
            seen, turn = 0, 0
            end = time.monotonic() + 15
            while time.monotonic() < end:
                snap = rig.pump.snapshot()
                if snap.get("timestamp", 0) != seen:     # one lost reply a cycle
                    seen = snap.get("timestamp", 0)
                    name = lose[turn % len(lose)]
                    fake_tc600.FAULTS["drop"] = {numbers[name]: 1}
                    turn += 1
                rules = rig.pump.interlocks(snap)
                judged += 1
                if "stale_reading" in (rules["off"].get("rule"),
                                       rules["motor_on"].get("rule")):
                    stale += 1
                time.sleep(0.05)
            # and one lost speed reply does not restart the stop dwell
            fake_tc600.FAULTS["drop"] = {309: 1}
            rig.fresh(lambda s: "ActualSpd" not in s.get("status", {}))
            rig.fresh(lambda s: "ActualSpd" in s.get("status", {}))
            if rig.pump._rotor_zero_since != since:
                dwell_restarts += 1
        return (stale == 0 and dwell_restarts == 0,
                f"{stale} of {judged} judgements refused as stale; the stop "
                f"dwell {'restarted' if dwell_restarts else 'kept running'}")
    attempt("at the real bus's speed, with a reply lost every cycle, nothing is "
            "refused as stale", slow_bus)

    # --- a coasting rotor with its speed unread ---------------------------------
    def coast_unread():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600._speed = 400.0                # coasting, motor off
        fake_tc600.FAULTS["drop"] = {309: -1}    # the speed never answers
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            snap = rig.fresh(lambda s: s.get("connected")
                             and "ActualSpd" not in s.get("status", {}))
            rule = rig.pump.interlocks(snap)["off"] if snap else {}
            dwell, poll = pumpmod.STOP_DWELL_SECONDS, runs.RunEngine.SHUTDOWN_POLL_S
            pumpmod.STOP_DWELL_SECONDS, runs.RunEngine.SHUTDOWN_POLL_S = 1.0, 0.2
            stopped_at = None
            try:
                engine.shutdown_pump("selftest")
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and fake_tc600._speed > 0:
                    if fake_tc600.STATE[10] is False:
                        stopped_at = fake_tc600._speed
                        break
                    time.sleep(0.05)
                engine.cancel_shutdown()
            finally:
                pumpmod.STOP_DWELL_SECONDS = dwell
                runs.RunEngine.SHUTDOWN_POLL_S = poll
        return (rule.get("allowed") is False and stopped_at is None,
                "station stopped under a rotor at "
                f"{stopped_at:.0f} Hz" if stopped_at is not None
                else rule.get("reason", "no reading without the speed"))
    attempt("an unknown speed never lets the station stop, nor the power-down",
            coast_unread)

    # --- the supervisor, on a noisy bus ------------------------------------------
    def start_outside():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 4.0)            # station on, motor off
            fake_tc600.STATE[23] = True          # switched on at the drive
            found = wait_until(
                lambda: (supervisor.check() or True) and any(
                    a["key"] == "motor_started"
                    for a in supervisor.describe()["recent"]), 12, 0.1)
            fake_tc600.STATE[23] = False
            watching(supervisor, 4.0)
            asked = time.time()
            rig.pump.command("motor_on", override=["pressure_unknown"])
            watching(supervisor, 5.0)
            ours = [a for a in supervisor.describe()["recent"]
                    if a["key"] == "motor_started" and a["raised_at"] >= asked]
        return (found and not ours,
                ("no alarm for the start nobody asked for" if not found else
                 "an alarm for the panel's own start" if ours else
                 "alarm for the start at the drive, none for the panel's own"))
    attempt("a rotor started without the panel asking raises an alarm",
            start_outside)

    def start_unheard():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=False)
            watching(supervisor, 4.0)
            fake_tc600.FAULTS["drop_write"] = {23: -1}   # it starts, unheard
            asked = time.time()
            result = rig.pump.command("motor_on",
                                      override=["pressure_unknown"])
            fake_tc600.FAULTS.pop("drop_write", None)
            watching(supervisor, 5.0)
            blamed = [a for a in supervisor.describe()["recent"]
                      if a["key"] == "motor_started" and a["raised_at"] >= asked]
        return (fake_tc600.STATE[23] is True and not blamed,
                f"command said {result.get('ok')}; "
                + ("blamed on somebody else" if blamed else "no false alarm"))
    attempt("a start whose replies were lost is not blamed on the front panel",
            start_unheard)

    class Snapshots:
        """A pump that says exactly what it is told to, one reading each."""

        def __init__(self):
            self.commanded = {}
            self.snap = {}
            self.seq = 0

        def say(self, station, motor, speed, code="000000"):
            self.seq += 1
            status = {"ErrorCode": code}
            if speed is not None:
                status["ActualSpd"] = speed
            self.snap = {"connected": True, "seq": self.seq, "age": 0.2,
                         "status": status, "identity": {"NominalSpd": 1500},
                         "setpoints": {"PumpgStatn": station,
                                       "MotorPump": motor}}

        def snapshot(self):
            return dict(self.snap)

        def nominal_speed(self, snap=None):
            return 1500.0

        def chamber_pressure(self):
            return {"mbar": None}

    def station_off_unread_speed():
        fake = Snapshots()
        devices = registry.Registry()
        devices._devices["pump"] = fake
        engine = runs.RunEngine(database, registry.Registry())
        supervisor = alarms.Supervisor(devices, engine, database,
                                       auto_recover=False)
        for _ in range(3):
            fake.say(True, False, 0)
            supervisor.check()
        for _ in range(2):
            fake.say(False, False, None)        # off, and no speed in it
            supervisor.check()
        found = [a for a in supervisor.describe()["recent"]
                 if a["key"] == "station_off"]
        return (bool(found) and found[0]["level"] == "critical",
                found[0]["level"] if found else "no alarm")
    attempt("a station-off read without the rotor speed is critical",
            station_off_unread_speed)

    def back_after_outage():
        fake_tc600.reset()
        fake_tc600.STATE.update({10: True, 23: True})
        fake_tc600._speed = 1500.0
        saved = alarms.Supervisor.OFFLINE_AFTER_S
        alarms.Supervisor.OFFLINE_AFTER_S = 3.0
        try:
            with Rig(pump_port) as rig:
                engine = runs.RunEngine(database, rig.devices)
                supervisor = alarms.Supervisor(rig.devices, engine, database,
                                               auto_recover=False)
                watching(supervisor, 4.0)        # running, before the cut
                # Its power is cut -- for well over the 3 s threshold: the
                # sampler needs a cycle or two to say "no contact" at all.
                fake_tc600.FAULTS["silent"] = True
                watching(supervisor, 10.0)
                fake_tc600.FAULTS.pop("silent")      # ... and comes back on
                watching(supervisor, 5.0)
                found = [a for a in supervisor.describe()["recent"]
                         if a["key"] == "motor_after_outage"]
        finally:
            alarms.Supervisor.OFFLINE_AFTER_S = saved
        return bool(found), found[0]["title"] if found else "nothing said"
    attempt("a rotor found running after a silence nobody watched raises an "
            "alarm", back_after_outage)

    def noise():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            watching(supervisor, 4.0)
            lose, seen, turn = (10, 303, 23), 0, 0
            end = time.monotonic() + 14
            while time.monotonic() < end:
                stamp = rig.pump.snapshot().get("timestamp", 0)
                if stamp != seen:                # one lost reply per reading
                    seen = stamp
                    fake_tc600.FAULTS["drop"] = {lose[turn % 3]: 1}
                    turn += 1
                supervisor.check()
                time.sleep(0.05)
            keys = [a["key"] for a in supervisor.describe()["recent"]]
        bad = [k for k in keys if k in ("station_off", "pump_fault",
                                        "motor_started")]
        return (not bad and supervisor.recovering is None,
                f"{turn} readings each missing one reply; alarms: {keys or 'none'}")
    attempt("line noise alone raises no alarm and starts no recovery", noise)

    def one_recovery():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["value"] = {303: "Err006"}   # a fault that stays
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            wait_until(lambda: (supervisor.check() or True)
                       and supervisor.recovering is not None, 12, 0.1)
            for _ in range(3):
                fake_tc600.FAULTS["drop"] = {303: 1}
                rig.fresh(lambda s: "ErrorCode" not in s.get("status", {}))
                watching(supervisor, 2.0)
            count = len(supervisor._recoveries)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
        return count == 1, f"{count} recoveries for one fault"
    attempt("a lost error-code reply does not spend another recovery",
            one_recovery)

    def fault_returns():
        fake_tc600.reset()
        fake_tc600.STATE[10] = True
        fake_tc600.FAULTS["value"] = {303: "Err006"}
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            supervisor = alarms.Supervisor(rig.devices, engine, database,
                                           auto_recover=True)
            wait_until(lambda: (supervisor.check() or True)
                       and supervisor.recovering is not None, 12, 0.1)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
            fake_tc600.FAULTS.pop("value")       # it clears ...
            fake_tc600.STATE[303] = "000000"
            watching(supervisor, 4.0)
            fake_tc600.STATE[303] = "Err006"     # ... and comes back
            wait_until(lambda: (supervisor.check() or True)
                       and len(supervisor._recoveries) == 2, 12, 0.1)
            count = len(supervisor._recoveries)
            wait_until(lambda: not (supervisor.recovering or {}).get("active"), 15)
        return count == 2, f"{count} recoveries for two faults"
    attempt("a fault that clears and comes back gets its own recovery",
            fault_returns)

    # --- an identity that did not all answer at connect --------------------------
    def nominal_after_loss():
        fake_tc600.reset()
        fake_tc600.FAULTS["drop"] = {315: -1}
        with Rig(pump_port) as rig:
            engine = runs.RunEngine(database, rig.devices)
            missing = "NominalSpd" not in rig.pump.snapshot().get("identity", {})
            during = engine.nominal_speed()
            fake_tc600.FAULTS["drop"] = {}
            read = wait_until(lambda: rig.pump.snapshot().get("identity", {})
                              .get("NominalSpd") == 1500, 10, 0.1)
        return (missing and during == 1500 and read,
                f"nominal speed {during} while unread; "
                + ("read once it answered" if read else "never read again"))
    attempt("a lost nominal-speed reply is asked for again, and full speed is "
            "never unknown meanwhile", nominal_after_loss)

    def gauge_unidentified():
        fake_tpg.reset()
        fake_tpg.FAULTS["drop"] = {"TID": -1}
        with Rig(gauge_port=gauge_port) as rig:
            missing = not rig.gauge.snapshot().get("identity", {}).get("gauges")
            result = rig.gauge.command("sensor_on", {"channel": 2})
            fake_tpg.FAULTS["drop"] = {}
            read = wait_until(lambda: bool(rig.gauge.snapshot()
                                           .get("identity", {}).get("gauges")),
                              12, 0.1)
        return (missing and not result.get("ok") and read,
                (result.get("error") or "switched on at atmosphere")[:70]
                + ("; identified later" if read else "; never identified"))
    attempt("an unidentified gauge is treated as a cold cathode, and identified "
            "later", gauge_unidentified)

    def rig_layout():
        fake_tpg.reset()
        fake_tpg.FAULTS["layout"] = "rig"
        with Rig(gauge_port=gauge_port) as rig:
            wait_until(lambda: bool(rig.gauge.snapshot().get("identity", {})
                                    .get("gauges")), 10, 0.1)
            snap = rig.gauge.snapshot()
            fitted = [g["identifier"] for g in snap["identity"].get("gauges", [])]
            empty = [r for r in snap.get("readings", []) if r["channel"] == 2]
            refused = rig.gauge.command("sensor_on", {"channel": 1})
        return (fitted == ["CO9", "noSe"] and empty and not empty[0]["valid"]
                and not refused.get("ok"),
                f"{fitted}, channel 2 status {empty[0]['status'] if empty else '?'}")
    attempt("the simulator can be the rig's own gauge, and the rules hold on it",
            rig_layout)

    # --- a drive that never names its full speed, through a whole run ---------
    def dragged_without_nominal():
        fake_tc600.reset()
        fake_tpg.reset()
        # Pumped down already: station and motor on, rotor at full speed.
        fake_tc600.STATE[10] = fake_tc600.STATE[23] = True
        fake_tc600._speed = 1500.0
        fake_tc600.STATE[309] = 1500
        fake_tpg.CHAMBER.pressure = 1.0e-5
        fake_tpg.set_pumping(1.0, True)
        fake_tc600.FAULTS["drop"] = {315: -1}       # NominalSpd never answers
        with Rig(pump_port, gauge_port, interval=None) as rig:
            engine = runs.RunEngine(database, rig.devices)
            started = engine.start("base_calibration", {"interval_s": 1})
            if not started.get("ok"):
                return False, started.get("error")
            reached = wait_until(lambda: engine.status().get("phase")
                                 == "basepressure", 20, 0.2)
            unnamed = "NominalSpd" not in rig.pump.snapshot().get("identity", {})
            fake_tc600.FAULTS["value"] = {309: 1200}     # dragged to 80 %
            ended = wait_until(lambda: not engine.status()["running"], 8, 0.1)
            last = engine.status().get("last") or {}
            if not ended:
                engine.stop("selftest")
                wait_until(lambda: not engine.status()["running"], 10, 0.1)
            wait_until(lambda: not (engine.status().get("secure") or {})
                       .get("active"), 15)
        fake_tc600.FAULTS.clear()
        return (reached and unnamed and ended and last.get("pump_trouble"),
                f"reached base pressure {reached}, NominalSpd unread {unnamed}, "
                + (f"ended: {last.get('message', '')[:60]}" if ended
                   else "still running 8 s after the rotor sank to 80 %"))
    attempt("a run on a drive that never answers its nominal speed still ends "
            "for a dragged rotor", dragged_without_nominal)

    class Unnamed(_QuietEngine):
        """An engine with no full speed to compare against at all."""

        def nominal_speed(self):
            return None

    def windmill_without_nominal():
        engine = Unnamed()
        recipe = runs.FullVacuum({})
        recipe.begin(engine)
        recipe.enter("spinup")
        coasting = recipe.phase_spinup(engine, {"t": 5.0, "speed": 40.0,
                                                "motor": False})
        driven = recipe.phase_spinup(engine, {"t": 6.0, "speed": 40.0,
                                              "motor": True})
        return (coasting is None and driven == "basepressure",
                f"motor off: {coasting}, motor on: {driven}")
    attempt("with no full speed known, a windmilling rotor is not taken for a "
            "running turbopump", windmill_without_nominal)

    # --- a gauge that acknowledges and then says nothing ----------------------
    def acknowledged_without_data():
        fake_tc600.reset()
        fake_tpg.reset()
        fake_tpg.CHAMBER.pressure = 1.0e-1           # roughed, below crossover
        fake_tpg.set_pumping(0.0, True)
        with Rig(pump_port, gauge_port) as rig:
            rig.pump.command("on")
            wait_until(lambda: rig.pump.snapshot().get("setpoints", {})
                       .get("PumpgStatn") is True, 10, 0.1)
            wait_until(lambda: rig.pump.chamber_pressure().get("mbar")
                       is not None, 10, 0.1)
            before = rig.pump.interlocks()["motor_on"]
            fake_tpg.FAULTS["ack_only"] = True
            refused = wait_until(lambda: rig.pump.interlocks()["motor_on"]
                                 .get("rule") == "pressure_unknown", 25, 0.2)
            during = rig.pump.interlocks()["motor_on"]
            fake_tpg.FAULTS.pop("ack_only")
            back = wait_until(lambda: rig.pump.interlocks()["motor_on"]
                              .get("allowed") is True, 15, 0.2)
            reading = rig.gauge.best_pressure()
            actual = fake_tpg.CHAMBER.pressure
            in_step = (isinstance(reading, float) and actual > 0
                       and 0.5 < reading / actual < 2.0)
            rig.pump.command("off")
        return (before.get("allowed") is True and refused and back and in_step,
                f"before: {before.get('allowed')}; during: "
                f"{during.get('rule')}; after: "
                + (f"{reading:.2E} against {actual:.2E} mbar"
                   if isinstance(reading, float) else "no reading"))
    attempt("a gauge that acknowledges but never sends data is no reading, and "
            "its next answers are in step", acknowledged_without_data)

    # --- a gauge stuck on one value -------------------------------------------
    def frozen_rate_of_rise():
        fake_tpg.reset()
        fake_tpg.FAULTS["layout"] = "rig"
        fake_tpg.FAULTS["frozen"] = 2.0e-5
        samples, last = [], None
        with Rig(gauge_port=gauge_port) as rig:
            begun = time.monotonic()
            while len(samples) < 12 and time.monotonic() - begun < 15:
                snap = rig.gauge.snapshot()
                value = rig.gauge.best_pressure(snap)
                if snap.get("timestamp") != last and value is not None:
                    last = snap.get("timestamp")
                    samples.append({"t": time.monotonic() - begun,
                                    "p_best": value})
                time.sleep(0.05)
        fake_tpg.reset()
        engine = _QuietEngine()
        recipe = runs.RateOfRise({})
        recipe.begin(engine)
        recipe.fit_from = 0.0
        summary = recipe.summarise(engine, samples)
        return (len(samples) >= 10 and summary.get("linear") is False
                and summary.get("leak_rate_mbar_l_s") is None
                and "stopped measuring" in summary.get("caveat", ""),
                f"{len(samples)} readings: leak rate "
                f"{summary.get('leak_rate_mbar_l_s')}, "
                f"R² {summary.get('r_squared')}")
    attempt("a frozen gauge is not reported as a leak-tight chamber",
            frozen_rate_of_rise)

    fake_tc600.reset()
    fake_tpg.reset()
    database.close()
