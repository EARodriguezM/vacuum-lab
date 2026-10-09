"""HTTP API and static file serving for the vacuum lab panel.

Routing is a small explicit table rather than a framework: the whole surface
is about thirty endpoints, the standard library's `http.server` is already
here, and the lab PC is offline so a dependency would have to be vendored.

Two conventions run through it:

* Every response is JSON with an `ok` field, except the file downloads and the
  static assets.
* Anything that physically moves hardware needs `confirm: true` in the body.
  The UI asks the operator; the API refuses to take the UI's word for it
  implicitly. A stray GET cannot start a pump.
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import db as dbmod
from . import discovery, export, history, runs, trends
from .pump import GUARDED

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(os.path.dirname(HERE), "web")

#: Actions that move hardware and therefore need an explicit confirmation.
CONFIRM_REQUIRED = GUARDED | {
    "sensor_on", "sensor_off", "degas_on", "degas_off", "setpoint", "unit",
}


#: How a serial port is classified, and whether an onboard one is real, live
#: in lab/discovery.py, which needs them to know where to look.
classify_port = discovery.classify_port
uart_present = discovery.uart_present


def list_ports(kind: str = "all", include_phantom: bool = False) -> dict:
    """Serial ports Linux can see, filtered by how they attach.

    Returns the ports plus how many phantoms were hidden, so the UI can say
    so rather than leaving somebody hunting for a port that was filtered out.
    """
    try:
        from serial.tools import list_ports as tools
    except ImportError:
        return {"ports": [], "hidden": 0}

    found, hidden = [], 0
    for port in sorted(tools.comports(), key=lambda p: p.device):
        category = classify_port(port)
        if kind not in ("all", "", category):
            continue
        real = uart_present(port.device)
        if not real and not include_phantom:
            hidden += 1
            continue
        found.append({
            "device": port.device,
            "kind": category,
            "usable": real,
            "description": port.description or "",
            "hwid": port.hwid or "",
            "manufacturer": getattr(port, "manufacturer", "") or "",
            "product": getattr(port, "product", "") or "",
            "serial_number": getattr(port, "serial_number", "") or "",
        })
    return {"ports": found, "hidden": hidden}


class Application:
    """Everything the request handler needs, assembled once at startup."""

    def __init__(self, registry, database: "dbmod.Database",
                 engine: "runs.RunEngine", token: str | None = None,
                 history=None, supervisor=None, finder=None):
        self.registry = registry
        self.db = database
        self.engine = engine
        self.token = token
        #: The rolling chart recorder, or None if this server keeps none.
        self.history = history
        #: Alarms and the automatic safe state after a fault, or None.
        self.supervisor = supervisor
        #: Finds the instruments' ports by asking, or None.
        self.finder = finder
        self.started_at = time.time()
        self._export_dir = tempfile.mkdtemp(prefix="vacuum-lab-export-")

    # --- composed views ---------------------------------------------------

    def state(self) -> dict:
        """The single poll that drives the whole UI."""
        return {
            "ok": True,
            "devices": self.registry.snapshot(),
            "order": self.registry.keys(),
            "run": self.engine.status(),
            "actuators": self.engine.hardware.describe(),
            "automatic_ready": self.engine.hardware.automatic_ready,
            "alarms": self.supervisor.describe() if self.supervisor else None,
            "ports": {"search": bool(self.finder and self.finder.enabled),
                      "devices": self.registry.descriptors()},
            "server_time": time.time(),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def live(self, limit: int = 600, points: int | None = None) -> dict:
        """The in-progress run's samples, for the live chart.

        `points` asks for the whole run decimated to about that many rows;
        without it, the most recent `limit` samples as before.
        """
        samples = (self.engine.run_samples(points) if points
                   else self.engine.recent_samples(limit))
        return {"ok": True, "samples": samples, "run": self.engine.status()}

    def trends(self) -> dict:
        """Health indicators of every finished run, oldest first."""
        nominal = None
        if "pump" in self.registry:
            value = self.registry.get("pump").snapshot().get("identity", {}) \
                .get("NominalSpd")
            nominal = value if isinstance(value, (int, float)) else None
        return {"ok": True, "runs": trends.trends(self.db, nominal)}

    def history_window(self, seconds: float | None, points: int,
                       since: float | None) -> dict:
        if self.history is None:
            return {"ok": False, "error": "this server keeps no history"}
        return self.history.window(seconds, points=points, since=since)


class Handler(BaseHTTPRequestHandler):
    """One request. `app` is attached to the class at startup."""

    server_version = "vacuum-lab/3.0"
    app: Application

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # The UI polls /api/state every couple of seconds; logging it buries
        # everything that matters.
        if self.path.startswith(("/api/state", "/api/live", "/api/history")):
            return
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, payload, code: int = 200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _download(self, name: str, text: str, content_type: str):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _download_file(self, name: str, path: str, content_type: str):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{name}"')
            self._cors()
            self.end_headers()
            with open(path, "rb") as handle:
                while chunk := handle.read(65536):
                    self.wfile.write(chunk)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _static(self, route: str):
        """Serve the SPA and its assets, refusing anything outside web/."""
        relative = posixpath.normpath(route.lstrip("/")) or "index.html"
        if relative.startswith(("..", "/")):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        path = os.path.join(WEB_ROOT, relative)
        if os.path.isdir(path):
            path = os.path.join(path, "index.html")
        if not os.path.isfile(path):
            # Unknown non-API path: hand it to the SPA, which owns routing.
            path = os.path.join(WEB_ROOT, "index.html")
        if not os.path.abspath(path).startswith(os.path.abspath(WEB_ROOT)):
            self._json({"ok": False, "error": "not found"}, 404)
            return

        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith(
                ("javascript", "json")):
            content_type += "; charset=utf-8"
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            self._json({"ok": False, "error": f"cannot read UI: {exc}"}, 500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The UI is served from the same box that drives the pump; a stale
        # cached bundle after an update is a support call nobody needs.
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        if not self.app.token:
            return True
        supplied = self.headers.get("X-Token", "")
        if not supplied:
            supplied = self._query().get("token", [""])[0]
        return supplied == self.app.token

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._query().get(key, [default])[0])
        except (TypeError, ValueError):
            return default

    def _float(self, key: str, default: float | None) -> float | None:
        try:
            value = float(self._query().get(key, [default])[0])
        except (TypeError, ValueError):
            return default
        return value if value == value else default      # NaN is no value

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}

    # --- verbs ------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path).path

        if not route.startswith("/api/"):
            self._static(route)
            return

        if route == "/api/ping":
            self._json({"ok": True, "service": "vacuum-lab",
                        "devices": self.app.registry.descriptors(),
                        "token_required": bool(self.app.token)})
            return

        if not self._authorised():
            self._json({"ok": False, "error": "bad token"}, 403)
            return

        handler = {
            "/api/state": lambda: self._json(self.app.state()),
            "/api/live": lambda: self._json(
                self.app.live(self._int("limit", 600),
                              self._int("points", 0) or None)),
            "/api/history": lambda: self._json(
                self.app.history_window(
                    self._float("seconds", 3600.0),
                    max(50, min(self._int("points", 1500), 20000)),
                    self._float("since", None))),
            "/api/devices": lambda: self._json(
                {"ok": True, "devices": self.app.registry.descriptors()}),
            "/api/procedures": lambda: self._json(
                {"ok": True, "procedures": runs.catalogue()}),
            "/api/trends": lambda: self._json(self.app.trends()),
            "/api/stats": lambda: self._json(
                {"ok": True, "stats": self.app.db.stats(),
                 "devices": self.app.db.devices()}),
            "/api/events": lambda: self._json(
                {"ok": True, "events": self.app.db.events(
                    limit=self._int("limit", 200))}),
            "/api/ports": lambda: self._json(
                {"ok": True,
                 "kind": (self._query().get("kind") or ["all"])[0],
                 **list_ports(
                     (self._query().get("kind") or ["all"])[0],
                     include_phantom=bool(self._query().get("all")))}),
            "/api/ports/survey": lambda: self._json(
                self.app.finder.survey() if self.app.finder
                else {"ok": False, "error": "port search is not available"}),
            "/api/actuators": lambda: self._json(
                {"ok": True,
                 "actuators": self.app.engine.hardware.describe(),
                 "automatic_ready": self.app.engine.hardware.automatic_ready,
                 "missing": self.app.engine.hardware.missing()}),
        }.get(route)
        if handler:
            handler()
            return

        if route.startswith("/api/device/"):
            self._device_get(route)
            return
        if route.startswith("/api/runs"):
            self._runs_get(route)
            return
        if route.startswith("/api/export/"):
            self._export_get(route)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        if not self._authorised():
            self._json({"ok": False, "error": "bad token"}, 403)
            return

        payload = self._body()

        if route == "/api/command":
            self._command(payload)
            return
        if route == "/api/run/start":
            self._json(self.app.engine.start(
                str(payload.get("procedure", "")),
                payload.get("config") or {},
                name=str(payload.get("name", "")),
                operator=str(payload.get("operator", "")),
                notes=str(payload.get("notes", "")),
            ))
            return
        if route == "/api/run/stop":
            # `shutdown` is a separate decision from stopping the run: it
            # asks for the pump to be brought to rest as well, in the order
            # the hardware needs. Without it the rig is left exactly as it
            # is, still under vacuum.
            self._json(self.app.engine.stop(
                str(payload.get("reason", "stopped by the operator")),
                shutdown=bool(payload.get("shutdown"))))
            return
        if route == "/api/alarms/ack":
            # Acknowledging moves nothing, so it needs no confirm.
            if self.app.supervisor is None:
                self._json({"ok": False, "error": "no supervisor"}, 404)
                return
            which = payload.get("id", "all")
            self._json(self.app.supervisor.acknowledge(
                which if which == "all" else int(which)))
            return
        if route == "/api/supervisor":
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "changing automatic recovery "
                                                  "needs confirm"}, 400)
                return
            if self.app.supervisor is None:
                self._json({"ok": False, "error": "no supervisor"}, 404)
                return
            self._json(self.app.supervisor.set_auto_recover(
                bool(payload.get("auto_recover"))))
            return
        if route == "/api/pump/secure":
            # The manual "make safe": motor off, fault acknowledged, station
            # (forevacuum pump) on. Moves hardware, so it is confirmed.
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "making the pump safe needs "
                                                  "confirm"}, 400)
                return
            self._json(self.app.engine.secure_pump(
                str(payload.get("reason", "requested by the operator"))))
            return
        if route == "/api/pump/cancel-shutdown":
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "cancelling needs confirm"}, 400)
                return
            self._json(self.app.engine.cancel_shutdown())
            return
        if route == "/api/pump/shutdown":
            # The same guided power-down, without a run to stop. Guarded like
            # any other hardware action: a stray GET cannot stop a pump.
            if not payload.get("confirm"):
                self._json({"ok": False,
                            "error": "shutting the pump down needs confirm"},
                           400)
                return
            self._json(self.app.engine.shutdown_pump(
                str(payload.get("reason", "requested by the operator"))))
            return
        if route == "/api/run/acknowledge":
            self._json(self.app.engine.acknowledge(
                str(payload.get("note", ""))))
            return
        if route == "/api/ports/searching":
            # Turning the automatic search on or off moves nothing.
            if self.app.finder is None:
                self._json({"ok": False, "error": "port search is not available"},
                           404)
                return
            self.app.finder.enabled = bool(payload.get("enabled"))
            self.app.db.add_event(None, "action",
                                  "Automatic port search switched "
                                  + ("on" if self.app.finder.enabled else "off"))
            self._json({"ok": True, "enabled": self.app.finder.enabled})
            return
        if route.startswith("/api/device/") and route.endswith("/search"):
            # Search for an instrument that is not answering, now rather than
            # at its next scheduled search. Done on its own sampler thread,
            # so nothing else touches its port meanwhile.
            key = route.split("/")[3]
            if key not in self.app.registry:
                self._json({"ok": False, "error": f"no device named {key!r}"}, 404)
                return
            service = self.app.registry.get(key)
            if service.snapshot().get("connected"):
                self._json({"ok": False, "error": f"{service.label} is answering "
                                                  f"on {service.port}; there is "
                                                  f"nothing to search for"})
                return
            if service.finder is None:
                self._json({"ok": False, "error": "port search is not available"})
                return
            service._searched_at = 0.0
            service._silent_since = time.monotonic() - service.SEARCH_AFTER_S
            self._json({"ok": True, "searching": True})
            return
        if route == "/api/device/port":
            # Moving an instrument to a different port. Deliberately guarded:
            # it drops the link and reopens it, which is not something to do
            # by accident while a run is recording.
            if not payload.get("confirm"):
                self._json({"ok": False,
                            "error": "changing a port needs confirm"}, 400)
                return
            if self.app.engine.status().get("running"):
                self._json({"ok": False,
                            "error": "a run is in progress; stop it before "
                                     "moving an instrument to another port"},
                           409)
                return
            key = str(payload.get("device", ""))
            if key not in self.app.registry:
                self._json({"ok": False,
                            "error": f"no device named {key!r}"}, 404)
                return
            service = self.app.registry.get(key)
            result = service.rebind(
                str(payload.get("port", "")),
                baudrate=payload.get("baudrate"),
                address=payload.get("address"))
            # A port chosen by hand that works is remembered like a found one.
            if result.get("ok") and self.app.finder and service.probe_kind:
                self.app.finder.memory.remember(service.probe_kind, service.port)
            self._json(result)
            return
        if route.startswith("/api/runs/"):
            self._runs_post(route, payload)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    # --- route groups -----------------------------------------------------

    def _device_get(self, route: str):
        parts = route.split("/")           # ['', 'api', 'device', key, view]
        if len(parts) < 5:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        key, view = parts[3], parts[4]
        if key not in self.app.registry:
            self._json({"ok": False, "error": f"no device named {key!r}"}, 404)
            return
        device = self.app.registry.get(key)

        if view == "state":
            self._json({"ok": True, "device": device.snapshot()})
        elif view == "accessories" and hasattr(device, "accessories"):
            self._json(device.accessories())
        elif view == "details" and hasattr(device, "details"):
            self._json(device.details())
        elif view == "diagnostics" and hasattr(device, "diagnostics"):
            self._json(device.diagnostics())
        else:
            self._json({"ok": False,
                        "error": f"{key} has no view {view!r}"}, 404)

    def _runs_get(self, route: str):
        parts = [p for p in route.split("/") if p]   # ['api', 'runs', ...]

        if len(parts) == 2:
            self._json({
                "ok": True,
                "runs": self.app.db.runs(
                    limit=self._int("limit", 100),
                    offset=self._int("offset", 0),
                    procedure=(self._query().get("procedure") or [None])[0],
                    status=(self._query().get("status") or [None])[0],
                    search=(self._query().get("search") or [None])[0],
                ),
                "total": self.app.db.count_runs(
                    procedure=(self._query().get("procedure") or [None])[0],
                    status=(self._query().get("status") or [None])[0],
                ),
            })
            return

        try:
            run_id = int(parts[2])
        except ValueError:
            self._json({"ok": False, "error": "bad run id"}, 400)
            return

        run = self.app.db.run(run_id)
        if not run:
            self._json({"ok": False, "error": f"no run {run_id}"}, 404)
            return

        view = parts[3] if len(parts) > 3 else ""

        if not view:
            self._json({"ok": True, "run": run,
                        "sample_count": self.app.db.sample_count(run_id)})
        elif view == "samples":
            # Decimate rather than truncate: a six-hour bakeout must still
            # plot end to end, just with fewer points. And decimate keeping
            # every extreme rather than every Nth row -- a stride steps
            # straight over the two-sample pressure jump that tripped the
            # pump, which is the one feature of the curve that matters.
            samples = self.app.db.samples(run_id)
            requested = self._int("max", 2000)
            if requested and len(samples) > requested:
                columns = [[s.get(key) for s in samples]
                           for key in history.SHAPE_KEYS]
                samples = [samples[i] for i in history.decimate(
                    len(samples), requested, columns)]
            self._json({"ok": True, "samples": samples,
                        "total": self.app.db.sample_count(run_id)})
        elif view == "events":
            self._json({"ok": True,
                        "events": self.app.db.events(run_id, limit=5000)})
        elif view in ("export.csv", "csv"):
            result = export.run_csv(self.app.db, run_id)
            if result:
                self._download(*result, "text/csv")
        elif view in ("export.json", "json"):
            result = export.run_json(self.app.db, run_id)
            if result:
                self._download(*result, "application/json")
        else:
            self._json({"ok": False, "error": f"no view {view!r}"}, 404)

    def _runs_post(self, route: str, payload: dict):
        parts = [p for p in route.split("/") if p]
        try:
            run_id = int(parts[2])
        except (IndexError, ValueError):
            self._json({"ok": False, "error": "bad run id"}, 400)
            return
        action = parts[3] if len(parts) > 3 else "update"

        if action == "update":
            updated = self.app.db.update_run(
                run_id,
                name=payload.get("name"),
                operator=payload.get("operator"),
                notes=payload.get("notes"),
            )
            if updated is None:
                self._json({"ok": False, "error": f"no run {run_id}"}, 404)
            else:
                self._json({"ok": True, "run": updated})
        elif action == "delete":
            if not payload.get("confirm"):
                self._json({"ok": False,
                            "error": "deleting a run needs confirm"}, 400)
                return
            deleted = self.app.db.delete_run(run_id)
            self._json({"ok": deleted,
                        "error": None if deleted else f"no run {run_id}"})
        else:
            self._json({"ok": False, "error": f"no action {action!r}"}, 404)

    def _export_get(self, route: str):
        name = route.rsplit("/", 1)[-1]
        if name == "runs.csv":
            self._download(*export.index_csv(self.app.db), "text/csv")
        elif name == "runs.json":
            self._download(*export.index_json(self.app.db), "application/json")
        elif name == "archive.json":
            self._download(*export.bundle_json(self.app.db), "application/json")
        elif name == "database.db":
            filename, path = export.database_snapshot(
                self.app.db, self.app._export_dir)
            self._download_file(filename, path, "application/vnd.sqlite3")
        else:
            self._json({"ok": False, "error": f"no export {name!r}"}, 404)

    def _command(self, payload: dict):
        key = str(payload.get("device", "pump"))
        action = str(payload.get("action", ""))

        if key not in self.app.registry:
            self._json({"ok": False, "error": f"no device named {key!r}"}, 404)
            return
        if action in CONFIRM_REQUIRED and not payload.get("confirm"):
            self._json({"ok": False,
                        "error": f"{action} changes the hardware and needs "
                                 f"confirm"}, 400)
            return

        override = bool(payload.get("override"))
        if override:
            sys.stderr.write(f"  !! INTERLOCK OVERRIDDEN for {key}.{action} "
                             f"by {self.address_string()}\n")
            self.app.db.add_event(
                None, "warning", f"Interlock overridden: {key}.{action}",
                {"from": self.address_string(), "value": payload.get("value")})

        device = self.app.registry.get(key)
        result = device.command(action, payload.get("value"), override)

        # Operator actions belong in the run's narrative, so a curve with a
        # step in it can be explained months later.
        if result.get("ok"):
            active = self.app.engine.status()
            if active.get("running"):
                self.app.db.add_event(
                    active["run"]["id"], "action",
                    f"Operator: {key}.{action}",
                    {"value": payload.get("value"), "overridden": override})
        self._json(result)
