#!/usr/bin/env python3
"""HTTP control panel for the TC 600, with process interlocks.

Runs on the machine physically wired to the pump and serves both the web UI
and a JSON API:

    ./webserver.py --serial /dev/ttyS1 --address 1 --port 8600

A background sampler owns the serial port and refreshes a cached snapshot every
couple of seconds. Everything else reads that cache, which keeps the UI fast and
-- more importantly -- means the interlock timers below are measured from
continuous observation rather than from whenever a browser last polled.

Interlocks (see INTERLOCKS below) refuse commands that would sequence the
pumping station and the turbopump in a damaging order. They can be overridden
deliberately with "override": true, because a safety rule with no escape hatch
is its own hazard when something has already gone wrong.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import serial  # noqa: E402

from tc600 import device, params, protocol  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(HERE, "web", "index.html")

#: Actions that physically move the pump. The UI confirms them, and the API
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

#: Parameters the panel may write. Narrower than "everything writable" on
#: purpose: interface address, parameter-set selection and the service line are
#: not things to expose behind a web button.
ALLOWED_WRITES = {
    "Heating", "TMSHeatSVal", "CfgOut4",
    "EnableVent", "VentMode", "VentSpd", "VentTime",
    "OpModeBKP", "SwOnBKP", "SwOffBKP",
    "GasMode", "RUTimeCtrl", "EnableBrake",
}

# --- interlock tuning -----------------------------------------------------

#: The backing pump must have run this long before the turbopump may start.
#: A turbopump spun up against an un-roughed foreline overheats and trips.
BACKING_LEAD_SECONDS = 60.0

#: After the rotor reaches zero, wait this long before the station may stop.
#: A coasting rotor still needs backing; "0 Hz" from the drive is also the
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


class PumpService:
    """Owns the serial connection, samples the pump, and enforces interlocks."""

    SAMPLE_INTERVAL = 2.0
    RECONNECT_AFTER = 3

    def __init__(self, port: str, address: int, timeout: float = 0.6):
        self.port = port
        self.address = address
        self.timeout = timeout
        self._lock = threading.Lock()
        self._pump: device.TC600 | None = None
        self._identity: dict = {}
        self._failures = 0

        #: Latest snapshot, replaced wholesale by the sampler.
        self._snap: dict = self._offline("starting up")
        self._snap_lock = threading.Lock()

        #: Interlock timers, in time.monotonic() terms. None means "not yet".
        self._station_on_since: float | None = None
        self._rotor_zero_since: float | None = None

        self._stop = threading.Event()
        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()

    # --- connection -------------------------------------------------------

    def _connect(self) -> device.TC600:
        if self._pump is None:
            self._pump = device.TC600(
                self.port, address=self.address,
                timeout=self.timeout, retries=0,
            )
        return self._pump

    def _drop(self) -> None:
        if self._pump is not None:
            try:
                self._pump.close()
            except Exception:
                pass
            self._pump = None
        self._identity = {}

    def _note_failure(self) -> None:
        # A serial handle can wedge open: writes stop reaching the UART while
        # the descriptor still looks healthy, so nothing ever raises. Counting
        # failures and forcing a reopen turns that into a blip.
        self._failures += 1
        if self._failures >= self.RECONNECT_AFTER:
            self._drop()
            self._failures = 0

    # --- sampling ---------------------------------------------------------

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._read_snapshot()
            except Exception as exc:                      # never kill the thread
                snap = self._offline(f"sampler error: {exc}")
            self._update_timers(snap)
            with self._snap_lock:
                self._snap = snap
            self._stop.wait(self.SAMPLE_INTERVAL)

    def _read_snapshot(self) -> dict:
        with self._lock:
            try:
                pump = self._connect()
            except (serial.SerialException, OSError) as exc:
                self._drop()
                return self._offline(f"cannot open {self.port}: {exc}")

            # One cheap read decides whether the pump is there at all.
            try:
                pump.read_raw(349)
                self._failures = 0
            except protocol.ProtocolError as exc:
                self._note_failure()
                return self._offline(str(exc))
            except (serial.SerialException, OSError) as exc:
                self._drop()
                return self._offline(str(exc))

            if not self._identity:
                try:
                    self._identity = pump.identify()
                except protocol.ProtocolError:
                    self._identity = {}

            status, units = {}, {}
            for number in params.STATUS_VIEW:
                p = params.BY_NUMBER[number]
                try:
                    status[p.name] = p.decode(pump.read_raw(number))
                    units[p.name] = p.unit
                except protocol.ProtocolError:
                    continue
                except (serial.SerialException, OSError) as exc:
                    self._drop()
                    return self._offline(str(exc))

            modes = {}
            for name in ("SpdSVal", "StdbySVal", "OpModeTMP",
                         "PumpgStatn", "MotorPump"):
                try:
                    modes[name] = pump.read(name)
                except (protocol.ProtocolError, KeyError, ValueError):
                    pass

            return {
                "ok": True, "connected": True,
                "port": self.port, "address": self.address,
                "identity": self._identity,
                "status": status, "units": units, "setpoints": modes,
                "timestamp": time.time(),
            }

    def _offline(self, message: str) -> dict:
        return {
            "ok": True, "connected": False,
            "port": self.port, "address": self.address,
            "error": message, "identity": {}, "status": {},
            "units": {}, "setpoints": {}, "timestamp": time.time(),
        }

    def _update_timers(self, snap: dict) -> None:
        """Maintain the two dwell timers from continuous observation."""
        now = time.monotonic()
        if not snap.get("connected"):
            # Contact lost: forget the timers rather than let a stale one
            # authorise something once contact returns.
            self._station_on_since = None
            self._rotor_zero_since = None
            return

        station = snap["setpoints"].get("PumpgStatn") is True
        speed = snap["status"].get("ActualSpd")

        if station:
            if self._station_on_since is None:
                self._station_on_since = now
        else:
            self._station_on_since = None

        if isinstance(speed, (int, float)):
            if speed > 0:
                self._rotor_zero_since = None
            elif self._rotor_zero_since is None:
                self._rotor_zero_since = now

    # --- interlocks -------------------------------------------------------

    def interlocks(self, snap: dict | None = None) -> dict:
        """Return {action: {allowed, reason, wait}} for the guarded actions."""
        if snap is None:
            with self._snap_lock:
                snap = self._snap
        now = time.monotonic()
        out: dict[str, dict] = {}

        def allow(action):
            out[action] = {"allowed": True, "reason": "", "wait": 0}

        def block(action, reason, wait=0.0):
            out[action] = {"allowed": False, "reason": reason,
                           "wait": int(round(wait))}

        if not snap.get("connected"):
            for a in ("on", "off", "motor_on", "motor_off"):
                block(a, "no contact with the pump")
            return out

        status, modes = snap["status"], snap["setpoints"]
        speed = status.get("ActualSpd")
        speed = speed if isinstance(speed, (int, float)) else 0
        station = modes.get("PumpgStatn") is True
        motor = modes.get("MotorPump") is True

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
        if not station:
            block("motor_on", "the backing pump is not running. Start the "
                              "pumping station first and let it rough down.")
        elif self._station_on_since is None:
            block("motor_on", "waiting to confirm how long the backing pump "
                              "has been running.", BACKING_LEAD_SECONDS)
        else:
            elapsed = now - self._station_on_since
            if elapsed < BACKING_LEAD_SECONDS:
                block("motor_on", "the backing pump needs to run for a minute "
                                  "before the turbopump may start.",
                      BACKING_LEAD_SECONDS - elapsed)
            else:
                allow("motor_on")

        # Starting the backing pump and stopping the turbopump are always the
        # safe direction, so they are never blocked.
        allow("on")
        allow("motor_off")
        return out

    def _check(self, action: str, snap: dict) -> None:
        rules = self.interlocks(snap)
        rule = rules.get(action)
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

    # --- public reads -----------------------------------------------------

    def state(self) -> dict:
        with self._snap_lock:
            snap = dict(self._snap)
        snap["interlocks"] = self.interlocks(snap)
        snap["age"] = round(time.time() - snap.get("timestamp", 0), 1)
        return snap

    def accessories(self) -> dict:
        with self._lock:
            try:
                pump = self._connect()
            except (serial.SerialException, OSError) as exc:
                self._drop()
                return {"ok": False, "error": f"cannot open {self.port}: {exc}"}

            values, units, missing = {}, {}, []
            for name in ACCESSORY_VIEW:
                p = params.BY_NAME[name.lower()]
                try:
                    values[name] = p.decode(pump.read_raw(p.number))
                    units[name] = p.unit
                except protocol.ProtocolError:
                    missing.append(name)
                except (serial.SerialException, OSError) as exc:
                    self._drop()
                    return {"ok": False, "error": str(exc)}

            return {"ok": True, "values": values, "units": units,
                    "missing": missing, "writable": sorted(ALLOWED_WRITES)}

    # --- writes -----------------------------------------------------------

    def command(self, action: str, value=None, override: bool = False) -> dict:
        with self._snap_lock:
            snap = dict(self._snap)

        if not override:
            try:
                self._check(action, snap)
            except Interlock as stop:
                return {"ok": False, "error": stop.reason,
                        "interlock": True, "wait": stop.wait}

        with self._lock:
            try:
                pump = self._connect()
            except (serial.SerialException, OSError) as exc:
                self._drop()
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
                self._note_failure()
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self._drop()
                return {"ok": False, "error": str(exc)}

        self._failures = 0
        # A command that changes station state must move its timer immediately;
        # waiting for the next sample would leave a two-second hole in which
        # the old timer still applies.
        now = time.monotonic()
        if action == "on":
            self._station_on_since = self._station_on_since or now
        elif action == "off":
            self._station_on_since = None
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
            with self._snap_lock:
                snap = dict(self._snap)
            try:
                self._check_param(name, coerced, snap)
            except Interlock as stop:
                return {"ok": False, "error": stop.reason,
                        "interlock": True, "wait": stop.wait}

        with self._lock:
            try:
                pump = self._connect()
                result = pump.write(parameter.number, coerced)
            except protocol.ProtocolError as exc:
                self._note_failure()
                return {"ok": False, "error": str(exc)}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            except (serial.SerialException, OSError) as exc:
                self._drop()
                return {"ok": False, "error": str(exc)}

        self._failures = 0
        return {"ok": True, "action": "param", "name": name, "result": result,
                "overridden": bool(override)}


class Handler(BaseHTTPRequestHandler):
    server_version = "tc600ctl/2.0"
    service: PumpService
    token: str | None

    def log_message(self, fmt, *args):
        if self.path.startswith("/api/state"):
            return
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Token", "")
        if not supplied and "token=" in self.path:
            supplied = self.path.split("token=", 1)[1].split("&")[0]
        return supplied == self.token

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route = self.path.split("?", 1)[0]

        if route in ("/", "/index.html"):
            try:
                with open(INDEX, "rb") as handle:
                    body = handle.read()
            except OSError as exc:
                self._json({"ok": False, "error": f"UI missing: {exc}"}, 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if route == "/api/ping":
            self._json({"ok": True, "service": "tc600",
                        "port": self.service.port,
                        "address": self.service.address})
            return

        if not self._authorised():
            self._json({"ok": False, "error": "bad token"}, 403)
            return

        if route == "/api/state":
            self._json(self.service.state())
            return
        if route == "/api/accessories":
            self._json(self.service.accessories())
            return
        if route == "/api/interlocks":
            self._json({"ok": True, "interlocks": self.service.interlocks()})
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        if route != "/api/command":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        if not self._authorised():
            self._json({"ok": False, "error": "bad token"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError) as exc:
            self._json({"ok": False, "error": f"bad request: {exc}"}, 400)
            return

        action = str(payload.get("action", ""))
        if action in GUARDED and not payload.get("confirm"):
            self._json({"ok": False,
                        "error": f"{action} moves the pump and needs confirm"},
                       400)
            return

        override = bool(payload.get("override"))
        if override:
            sys.stderr.write(f"  !! INTERLOCK OVERRIDDEN for {action} "
                             f"by {self.address_string()}\n")

        if action == "param":
            self._json(self.service.write_param(
                str(payload.get("name", "")), payload.get("value"), override))
            return
        self._json(self.service.command(action, payload.get("value"), override))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serial", default="/dev/ttyS1")
    parser.add_argument("--address", type=int, default=1)
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--token", default=None,
                        help="require this shared token on API calls")
    args = parser.parse_args()

    Handler.service = PumpService(args.serial, args.address)
    Handler.token = args.token

    httpd = ThreadingHTTPServer((args.bind, args.port), Handler)
    print(f"TC 600 control panel on http://{args.bind}:{args.port}/")
    print(f"  serial {args.serial}, RS-485 address {args.address}")
    print(f"  interlocks: backing pump must lead the turbopump by "
          f"{BACKING_LEAD_SECONDS:.0f}s; station may not stop until "
          f"{STOP_DWELL_SECONDS:.0f}s after the rotor reaches 0 Hz")
    if not args.token:
        print("  NO TOKEN SET: anyone who can reach this port can start and")
        print("  stop the pump. Pass --token to require a shared secret.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
