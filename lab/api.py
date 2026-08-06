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
from . import export, runs
from .pump import GUARDED

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(os.path.dirname(HERE), "web")

#: Actions that move hardware and therefore need an explicit confirmation.
CONFIRM_REQUIRED = GUARDED | {
    "sensor_on", "sensor_off", "degas_on", "degas_off", "setpoint", "unit",
}


class Application:
    """Everything the request handler needs, assembled once at startup."""

    def __init__(self, registry, database: "dbmod.Database",
                 engine: "runs.RunEngine", token: str | None = None):
        self.registry = registry
        self.db = database
        self.engine = engine
        self.token = token
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
            "server_time": time.time(),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def live(self, limit: int = 600) -> dict:
        """The in-progress run's samples, for the live chart."""
        return {"ok": True, "samples": self.engine.recent_samples(limit),
                "run": self.engine.status()}


class Handler(BaseHTTPRequestHandler):
    """One request. `app` is attached to the class at startup."""

    server_version = "vacuum-lab/3.0"
    app: Application

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # The UI polls /api/state every couple of seconds; logging it buries
        # everything that matters.
        if self.path.startswith(("/api/state", "/api/live")):
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
                self.app.live(self._int("limit", 600))),
            "/api/devices": lambda: self._json(
                {"ok": True, "devices": self.app.registry.descriptors()}),
            "/api/procedures": lambda: self._json(
                {"ok": True, "procedures": runs.catalogue()}),
            "/api/stats": lambda: self._json(
                {"ok": True, "stats": self.app.db.stats(),
                 "devices": self.app.db.devices()}),
            "/api/events": lambda: self._json(
                {"ok": True, "events": self.app.db.events(
                    limit=self._int("limit", 200))}),
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
            self._json(self.app.engine.stop(
                str(payload.get("reason", "stopped by the operator"))))
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
            count = self.app.db.sample_count(run_id)
            # Decimate rather than truncate: a six-hour bakeout must still
            # plot end to end, just with fewer points.
            requested = self._int("max", 2000)
            every = max(1, -(-count // requested)) if requested else 1
            self._json({"ok": True, "samples": self.app.db.samples(
                run_id, every=every), "every": every, "total": count})
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
