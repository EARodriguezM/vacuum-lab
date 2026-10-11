"""HTTP API and static file serving for the vacuum lab panel.

Routing is a small explicit table rather than a framework: the whole surface
is about thirty endpoints, the standard library's `http.server` is already
here, and the lab PC is offline so a dependency would have to be vendored.

Three conventions run through it:

* Every response is JSON with an `ok` field, except the file downloads and the
  static assets.
* Anything that physically moves hardware needs `confirm: true` in the body.
  The UI asks the operator; the API refuses to take the UI's word for it
  implicitly. A stray GET cannot start a pump.
* Every API call except /api/ping and signing in carries a session
  (`Authorization: Bearer ...`), and every POST is checked against
  POST_RULES before it does anything. The rules themselves -- roles, control,
  idle sessions -- are lab/auth.py's; this module only applies them.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import mimetypes
import os
import posixpath
import sys
import tempfile
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from . import auth as authmod
from . import db as dbmod
from . import discovery, export, history, runs, trends
from .pump import GUARDED

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_ROOT = os.path.join(os.path.dirname(HERE), "web")

#: Actions that move hardware and therefore need an explicit confirmation.
CONFIRM_REQUIRED = GUARDED | {
    "sensor_on", "sensor_off", "degas_on", "degas_off", "setpoint", "unit",
}

#: Who may make each POST: the lowest role, and whether the caller must hold
#: control of the rig (lab/auth.py). Whatever moves hardware needs control.
#: What makes the rig safer does not -- making the pump safe, cancelling a
#: power-down, acknowledging alarms, and stopping a run without powering down
#: (see _post_rule) -- because an operator who sees something wrong must not
#: have to wait for whoever holds control to notice. Setting the rig up --
#: equipment, ports, automatic recovery -- is an admin's.
POST_RULES = {
    "/api/command":              ("operator", True),
    "/api/run/start":            ("operator", True),
    "/api/run/acknowledge":      ("operator", True),
    "/api/pump/shutdown":        ("operator", True),
    "/api/pump/secure":          ("operator", False),
    "/api/pump/cancel-shutdown": ("operator", False),
    "/api/alarms/ack":           ("operator", False),
    "/api/control/release":      ("operator", False),
    "/api/equipment":            ("admin", False),
    "/api/supervisor":           ("admin", False),
    "/api/device/port":          ("admin", False),
    "/api/ports/searching":      ("admin", False),
}

#: Reachable without a session: how a sign-in page finds out where it is.
PUBLIC_POSTS = ("/api/login", "/api/signup", "/api/pair")

#: GETs that need more than watching.
ADMIN_GETS = ("/api/ports/survey",)

#: The largest request body accepted. Every real one is a few hundred bytes;
#: without a cap, anyone on the network could make the server allocate
#: whatever a Content-Length claims, before any password is checked.
MAX_BODY_BYTES = 64 * 1024


def web_build(root: str = WEB_ROOT) -> str:
    """A short fingerprint of the panel's files, taken at startup.

    Served with every state poll. A panel that sees it change -- the files
    were deployed under it -- reloads itself, so a tablet on the wall does
    not run last week's page against this week's server.
    """
    digest = hashlib.sha1()
    for folder, subfolders, files in sorted(os.walk(root)):
        subfolders.sort()
        for name in sorted(files):
            path = os.path.join(folder, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            digest.update(f"{os.path.relpath(path, root)}:{info.st_size}:"
                          f"{info.st_mtime_ns};".encode())
    return digest.hexdigest()[:12]


def parse_networks(text: str | None) -> list:
    """`--allow-from 172.16.2.0/24,10.0.0.0/8` into networks; empty is none."""
    networks = []
    for part in str(text or "").split(","):
        part = part.strip()
        if part:
            networks.append(ipaddress.ip_network(part, strict=False))
    return networks


def address_allowed(address: str, networks: list) -> bool:
    """Whether a client may reach the panel at all. Loopback always may: the
    tablet on its USB cable arrives as 127.0.0.1."""
    if not networks:
        return True
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return any(ip in network for network in networks)


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
                 engine: "runs.RunEngine", auth: "authmod.Auth | None" = None,
                 history=None, supervisor=None, finder=None,
                 chamber: dict | None = None, allow: list | None = None):
        self.registry = registry
        self.db = database
        self.engine = engine
        #: Accounts and control. Without one, accounts are off: every request
        #: is the local owner, which is what --no-auth and the tests ask for.
        self.auth = auth or authmod.Auth(database, "chamber-1", enabled=False)
        #: Which chamber this server drives: {"id", "name"}.
        self.chamber = chamber or {"id": self.auth.chamber,
                                   "name": self.auth.chamber}
        #: Networks allowed to reach the panel at all; empty allows any.
        self.allow = allow or []
        #: The rolling chart recorder, or None if this server keeps none.
        self.history = history
        #: Alarms and the automatic safe state after a fault, or None.
        self.supervisor = supervisor
        #: Finds the instruments' ports by asking, or None.
        self.finder = finder
        self.started_at = time.time()
        #: Fingerprint of web/ when this server started (see web_build).
        self.build = web_build()
        self._export_dir = tempfile.mkdtemp(prefix="vacuum-lab-export-")

    # --- composed views ---------------------------------------------------

    def state(self, principal: "authmod.Principal | None" = None) -> dict:
        """The single poll that drives the whole UI."""
        principal = principal or authmod.LOCAL
        return {
            "ok": True,
            "build": self.build,
            "chamber": self.chamber,
            "me": self.auth.me(principal),
            "control": self.auth.control(principal),
            "devices": self.registry.snapshot(),
            "order": self.registry.keys(),
            "run": self.engine.status(),
            "actuators": self.engine.actuators(),
            "automatic_ready": self.engine.hardware.automatic_ready,
            # Procedures this rig cannot run, and what each is missing.
            "unavailable": self.engine.unavailable(),
            "alarms": self.supervisor.describe() if self.supervisor else None,
            "ports": {"search": bool(self.finder and self.finder.enabled),
                      "devices": self.registry.descriptors()},
            "server_time": time.time(),
            "uptime_s": round(time.time() - self.started_at, 1),
        }

    def live(self, limit: int = 600, points: int | None = None,
             since: float | None = None) -> dict:
        """The in-progress run's samples, for the live chart.

        `points` asks for the whole run decimated to about that many rows;
        `since` for only the samples after that run time (`t`, seconds) --
        what a panel already holding the run needs on each poll, one or two
        rows instead of the whole run again. Without either, the most recent
        `limit` samples.
        """
        if since is not None:
            samples = self.engine.samples_since(since)
        elif points:
            samples = self.engine.run_samples(points)
        else:
            samples = self.engine.recent_samples(limit)
        status = self.engine.status()
        return {"ok": True, "samples": samples, "run": status,
                "run_id": (status.get("run") or {}).get("id")}

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
    #: Seconds a connection may sit silent before it is dropped: a client
    #: that opens connections and sends nothing must not hold a thread each
    #: for ever. Applies per read or write, not to a whole download.
    timeout = 30
    app: Application
    #: Whoever sent this request, once it has been resolved.
    principal: "authmod.Principal | None" = None

    # --- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        # The UI polls /api/state every couple of seconds; logging it buries
        # everything that matters.
        if self.path.startswith(("/api/state", "/api/live", "/api/history")):
            return
        sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

    def _cors(self):
        # Permissive on purpose: the panel may be opened from another machine
        # and pointed here. That is safe because the session travels in a
        # header that no other page can read, never in a cookie a browser
        # would attach to somebody else's request.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization")
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

    def _address(self) -> str:
        return str(self.client_address[0]) if self.client_address else ""

    def _reachable(self) -> bool:
        """Refuse a client outside --allow-from before anything else."""
        if address_allowed(self._address(), self.app.allow):
            return True
        self._json({"ok": False, "error": "This panel only answers the lab "
                                          "network."}, 403)
        return False

    def _principal(self) -> "authmod.Principal | None":
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        self.principal = self.app.auth.resolve(token, self._address())
        return self.principal

    def _refused(self, refusal: dict) -> None:
        body = dict(refusal)
        self._json(body, int(body.pop("status", 403)))

    def _check(self, principal: "authmod.Principal", minimum: str,
               control: bool) -> dict | None:
        """The reason this principal may not make this request, or None."""
        auth = self.app.auth
        if not auth.enabled:
            return None
        if principal.kind == "device":
            return authmod.refusal("This tablet only watches. Sign in to act.",
                                   403)
        if principal.must_change:
            return authmod.refusal("Choose a new password first.", 403,
                                   must_change=True)
        if not principal.at_least(minimum):
            return authmod.refusal(f"This needs the {minimum} role; you are "
                                   f"{principal.role}.", 403)
        if principal.idle():
            return authmod.refusal("Your session has been idle. Enter your "
                                   "password to act again.", 401, reauth=True)
        if control:
            return auth.ensure_control(principal)
        return None

    def _audit(self, message: str, level: str = "action",
               detail: dict | None = None) -> None:
        """Put an action in the rig's record under the name of who took it."""
        who = self.principal or authmod.LOCAL
        try:
            self.app.db.add_event(self.app.engine.run_id(), level,
                                  f"{who.name}: {message}", detail,
                                  user=who.username)
        except Exception:
            pass

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

    def _body(self) -> dict | None:
        """The JSON object in the request, or None once a refusal is sent.

        A negative length would read until the client hangs up, and a huge
        one would allocate whatever it claims: both are refused before a
        byte is read. A body that is not a JSON object reads as empty.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = -1
        if length < 0:
            self._json({"ok": False, "error": "bad Content-Length"}, 400)
            return None
        if length > MAX_BODY_BYTES:
            self._json({"ok": False, "error": "request too large"}, 413)
            return None
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    # --- verbs ------------------------------------------------------------

    def do_OPTIONS(self):
        if not self._reachable():
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        route = urlparse(self.path).path
        if not self._reachable():
            return

        # The panel itself is public -- it has to be, to show a sign-in page
        # -- and so is the ping, which is how the kiosk tablet recognises the
        # server. Keep "service": "vacuum-lab" in it.
        if not route.startswith("/api/"):
            self._static(route)
            return

        if route == "/api/ping":
            # Open to anyone on the network, so it says only what a sign-in
            # page and the kiosk need -- not which serial port is which.
            self._json({"ok": True, "service": "vacuum-lab",
                        "chamber": self.app.chamber,
                        "auth": self.app.auth.status(),
                        "build": self.app.build})
            return

        principal = self._principal()
        if principal is None:
            self._json({"ok": False, "error": "Sign in first.", "login": True},
                       401)
            return
        # The controller's own polling is what keeps control alive.
        self.app.auth.renew(principal)

        if route in ADMIN_GETS:
            refusal = self._check(principal, "admin", False)
            if refusal:
                self._refused(refusal)
                return
        if route == "/api/me":
            self._json({"ok": True, "me": self.app.auth.me(principal),
                        "control": self.app.auth.control(principal)})
            return
        if route == "/api/users":
            report = self.app.auth.users_report(principal)
            if report.get("ok"):
                self._json(report)
            else:
                self._refused(report)
            return

        handler = {
            "/api/state": lambda: self._json(self.app.state(principal)),
            "/api/live": lambda: self._json(
                self.app.live(self._int("limit", 600),
                              self._int("points", 0) or None,
                              self._float("since", None))),
            "/api/history": lambda: self._json(
                self.app.history_window(
                    self._float("seconds", 3600.0),
                    max(50, min(self._int("points", 1500), 20000)),
                    self._float("since", None))),
            "/api/devices": lambda: self._json(
                {"ok": True, "devices": self.app.registry.descriptors()}),
            "/api/procedures": lambda: self._json(
                {"ok": True, "procedures": self.app.engine.catalogue()}),
            "/api/equipment": lambda: self._json(
                {"ok": True, "equipment": self.app.engine.equipment_report()}),
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
                 "actuators": self.app.engine.actuators(),
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
        if not self._reachable():
            return
        payload = self._body()
        if payload is None:
            return

        if route in PUBLIC_POSTS:
            self._public_post(route, payload)
            return

        principal = self._principal()
        if principal is None:
            self._json({"ok": False, "error": "Sign in first.", "login": True},
                       401)
            return
        if route.startswith(("/api/session/", "/api/me/", "/api/logout",
                             "/api/control/take", "/api/users/",
                             "/api/devices/", "/api/role-requests")):
            self._account_post(route, payload, principal)
            return

        minimum, control = self._post_rule(route, payload)
        refusal = self._check(principal, minimum, control)
        if refusal:
            self._refused(refusal)
            return
        # Doing something on purpose is a touch: it keeps the session awake.
        self.app.auth.mark_active(principal)

        if route == "/api/command":
            self._command(payload)
            return
        if route == "/api/run/start":
            result = self.app.engine.start(
                str(payload.get("procedure", "")),
                payload.get("config") or {},
                name=str(payload.get("name", "")),
                operator=str(payload.get("operator", "")) or principal.name,
                notes=str(payload.get("notes", "")),
                started_by=principal.username,
            )
            if result.get("ok"):
                self._audit(f"started {result['run']['name']} "
                            f"(run {result['run']['id']})")
            self._json(result)
            return
        if route == "/api/run/stop":
            # `shutdown` is a separate decision from stopping the run: it
            # asks for the pump to be brought to rest as well, in the order
            # the hardware needs. Without it the rig is left exactly as it
            # is, still under vacuum.
            shutdown = bool(payload.get("shutdown"))
            result = self.app.engine.stop(
                str(payload.get("reason", "stopped by the operator")),
                shutdown=shutdown)
            if result.get("ok"):
                self._audit("stopped the run, " + ("powering the pump down"
                                                   if shutdown else
                                                   "keeping the vacuum"))
            self._json(result)
            return
        if route == "/api/alarms/ack":
            # Acknowledging moves nothing, so it needs no confirm.
            if self.app.supervisor is None:
                self._json({"ok": False, "error": "no supervisor"}, 404)
                return
            which = payload.get("id", "all")
            self._json(self.app.supervisor.acknowledge(
                which if which == "all" else int(which), user=principal))
            return
        if route == "/api/equipment":
            # Moves nothing, but changes which procedures may run: confirmed.
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "changing the rig's equipment "
                                                  "needs confirm"}, 400)
                return
            self._json(self.app.engine.set_equipment(
                str(payload.get("key", "")), bool(payload.get("installed")),
                user=principal))
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
                bool(payload.get("auto_recover")), user=principal))
            return
        if route == "/api/pump/secure":
            # The manual "make safe": motor off, fault acknowledged, station
            # (forevacuum pump) on. Moves hardware, so it is confirmed.
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "making the pump safe needs "
                                                  "confirm"}, 400)
                return
            result = self.app.engine.secure_pump(
                str(payload.get("reason", "requested by the operator")))
            if result.get("ok"):
                self._audit("made the pump safe (motor off, fault acknowledged, "
                            "forevacuum pump on)")
            self._json(result)
            return
        if route == "/api/pump/cancel-shutdown":
            if not payload.get("confirm"):
                self._json({"ok": False, "error": "cancelling needs confirm"}, 400)
                return
            result = self.app.engine.cancel_shutdown()
            if result.get("ok"):
                self._audit("cancelled the power-down; the backing pump keeps "
                            "running")
            self._json(result)
            return
        if route == "/api/pump/shutdown":
            # The same guided power-down, without a run to stop. Guarded like
            # any other hardware action: a stray GET cannot stop a pump.
            if not payload.get("confirm"):
                self._json({"ok": False,
                            "error": "shutting the pump down needs confirm"},
                           400)
                return
            result = self.app.engine.shutdown_pump(
                str(payload.get("reason", "requested by the operator")))
            if result.get("ok"):
                self._audit("asked for the pump to be powered down")
            self._json(result)
            return
        if route == "/api/run/acknowledge":
            result = self.app.engine.acknowledge(str(payload.get("note", "")))
            if result.get("ok"):
                self._audit("confirmed the manual step")
            self._json(result)
            return
        if route == "/api/control/release":
            self._json(self.app.auth.release(principal))
            return
        if route == "/api/ports/searching":
            # Turning the automatic search on or off moves nothing.
            if self.app.finder is None:
                self._json({"ok": False, "error": "port search is not available"},
                           404)
                return
            self.app.finder.enabled = bool(payload.get("enabled"))
            self._audit("switched the automatic port search "
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
            if result.get("ok"):
                self._audit(f"moved the {service.label} to {service.port}")
            # A port chosen by hand that works is remembered like a found one.
            if result.get("ok") and self.app.finder and service.probe_kind:
                self.app.finder.memory.remember(service.probe_kind, service.port)
            self._json(result)
            return
        if route.startswith("/api/runs/"):
            self._runs_post(route, payload)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    # --- who may post what -----------------------------------------------

    def _post_rule(self, route: str, payload: dict) -> tuple[str, bool]:
        """The lowest role a POST needs, and whether it needs control."""
        if route in POST_RULES:
            return POST_RULES[route]
        if route == "/api/run/stop":
            # Stopping a run and keeping the vacuum makes the rig safer -- it
            # shuts a deposition's gas off -- so any operator may, control or
            # not. Powering the pump down is a decision, and needs control.
            return "operator", bool(payload.get("shutdown"))
        if route.startswith("/api/device/") and route.endswith("/search"):
            return "operator", False
        if route.startswith("/api/runs/"):
            # Editing a run's name or notes is bookkeeping; deleting a run
            # destroys a record, which is an admin's call.
            return ("admin" if route.endswith("/delete") else "operator"), False
        # Anything not listed needs the most, not the least.
        return "admin", True

    def _public_post(self, route: str, payload: dict) -> None:
        auth = self.app.auth
        agent = self.headers.get("User-Agent", "")
        if route == "/api/login":
            result = auth.login(str(payload.get("username", "")),
                                str(payload.get("password", "")),
                                self._address(), agent)
        elif route == "/api/signup":
            result = auth.signup(str(payload.get("username", "")),
                                 str(payload.get("name", "")),
                                 str(payload.get("password", "")),
                                 str(payload.get("note", "")), self._address())
        else:
            result = auth.pair(str(payload.get("code", "")), self._address(),
                               agent)
        if result.get("ok"):
            self._json(result)
        else:
            self._refused(result)

    def _account_post(self, route: str, payload: dict,
                      principal: "authmod.Principal") -> None:
        """Signing out, the idle prompt, one's own password, and the admin
        pages. lab/auth.py checks each of these itself."""
        auth = self.app.auth

        def number(key: str) -> int:
            try:
                return int(payload.get(key))
            except (TypeError, ValueError):
                return -1

        if route == "/api/logout":
            result = auth.logout(principal)
        elif route == "/api/session/touch":
            result = {"ok": True, "active": auth.mark_active(principal),
                      "me": auth.me(principal)}
        elif route == "/api/session/reauth":
            result = auth.reauth(principal, str(payload.get("password", "")))
        elif route == "/api/me/password":
            result = auth.change_password(principal,
                                          str(payload.get("current", "")),
                                          str(payload.get("new", "")))
        elif route == "/api/control/take":
            if principal.must_change:
                result = authmod.refusal("Choose a new password first.", 403,
                                         must_change=True)
            else:
                result = auth.take(principal, force=bool(payload.get("force")))
            if result.get("ok"):
                auth.mark_active(principal)
        elif route == "/api/role-requests":
            result = auth.request_role(principal, str(payload.get("role", "")),
                                       str(payload.get("note", "")))
        elif route == "/api/role-requests/decide":
            result = auth.decide(principal, number("id"),
                                 bool(payload.get("approve")))
        elif route == "/api/users/approve":
            result = auth.approve(principal, number("id"),
                                  str(payload.get("role", "viewer")))
        elif route == "/api/users/decline":
            result = auth.decline(principal, number("id"))
        elif route == "/api/users/role":
            result = auth.set_role(principal, number("id"),
                                   str(payload.get("role", "")))
        elif route == "/api/users/status":
            result = auth.set_status(principal, number("id"),
                                     bool(payload.get("enabled")))
        elif route == "/api/users/reset-password":
            result = auth.reset_password(principal, number("id"))
        elif route == "/api/users/transfer-owner":
            result = auth.transfer_owner(principal, number("id"),
                                         str(payload.get("password", "")))
        elif route == "/api/devices/add":
            result = auth.create_device(principal, str(payload.get("label", "")))
        elif route == "/api/devices/code":
            result = auth.renew_code(principal, number("id"))
        elif route == "/api/devices/remove":
            result = auth.remove_device(principal, number("id"))
        else:
            result = authmod.refusal("not found", 404)
        if result.get("ok"):
            self._json(result)
        else:
            self._refused(result)

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
            record = self.app.db.run(run_id)
            deleted = self.app.db.delete_run(run_id)
            if deleted:
                # Kept in the rig's log, not the run's, which is now gone.
                who = self.principal or authmod.LOCAL
                self.app.db.add_event(
                    None, "warning",
                    f"{who.name}: deleted run {run_id} "
                    f"({(record or {}).get('name', '?')})", user=who.username)
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
        who = self.principal or authmod.LOCAL
        override = bool(payload.get("override"))

        # Bypassing a safety rule is an admin's call, whoever holds control.
        # Checked first, so the answer never depends on anything else.
        if override and not who.at_least("admin"):
            self._json({"ok": False, "interlock": True,
                        "error": "Only an admin may override an interlock. "
                                 "Ask one, or deal with what the interlock "
                                 "is refusing."}, 403)
            return
        if key not in self.app.registry:
            self._json({"ok": False, "error": f"no device named {key!r}"}, 404)
            return
        if action in CONFIRM_REQUIRED and not payload.get("confirm"):
            self._json({"ok": False,
                        "error": f"{action} changes the hardware and needs "
                                 f"confirm"}, 400)
            return

        if override:
            sys.stderr.write(f"  !! INTERLOCK OVERRIDDEN for {key}.{action} "
                             f"by {who.username} at {self.address_string()}\n")
            self.app.db.add_event(
                self.app.engine.run_id(), "warning",
                f"{who.name}: interlock overridden for {key}.{action}",
                {"from": self.address_string(), "value": payload.get("value")},
                user=who.username)

        device = self.app.registry.get(key)
        result = device.command(action, payload.get("value"), override)

        # Actions that change the hardware belong in the rig's narrative, so
        # a curve with a step in it can be explained months later -- and now
        # say who took them.
        if result.get("ok") and (action in CONFIRM_REQUIRED or override):
            value = payload.get("value")
            self._audit(f"{device.label}, {action.replace('_', ' ')}"
                        + (f" {value}" if value is not None else "")
                        + (" (interlock overridden)" if override else ""),
                        detail={"device": key, "action": action, "value": value,
                                "overridden": override})
        self._json(result)
