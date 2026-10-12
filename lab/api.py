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
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import auth as authmod
from . import db as dbmod
from . import discovery, export, history, runs, trends
from .pump import GUARDED, OPERATOR_WRITES

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

#: The most rows one listing returns, whatever the request asks for.
MAX_LIST = 1000

#: How long a run's name, operator and notes may be, in characters.
RUN_NAME_MAX, RUN_OPERATOR_MAX, RUN_NOTES_MAX = 120, 80, 4000

#: Connections served at once, in all and from one address. Each holds a
#: thread; beyond these a new connection is turned away, so a flood from the
#: network cannot take the threads and memory the rig runs on. Loopback --
#: the kiosk tablet on its USB cable -- is held only to the per-address cap,
#: so a flood from the network cannot take the wall screen with it.
MAX_CONNECTIONS = 64
MAX_CONNECTIONS_PER_ADDRESS = 16

#: Whoever a request turned out to be when, with accounts on, it was never
#: resolved: no role, no rights. Code that reached for the local owner as a
#: fallback would otherwise hand an unresolved request the owner's name.
NOBODY = authmod.Principal(-1, "unknown", "Unknown", "none", "none", None)

#: How long a download waits for the one before it to be built.
EXPORT_WAIT_S = 10.0

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


def machine_names(extra: list[str] | None = None) -> set[str]:
    """The names this panel answers to, besides any IP address: this
    machine's own, and whatever `--host-name` adds (a domain, once there is
    one)."""
    names = {"localhost"}
    try:
        own = socket.gethostname().lower().rstrip(".")
        names |= {own, own.split(".")[0], f"{own.split('.')[0]}.local",
                  socket.getfqdn().lower().rstrip(".")}
    except OSError:
        pass
    names |= {str(n).strip().lower().rstrip(".") for n in (extra or [])
              if str(n).strip()}
    names.discard("")
    return names


def host_allowed(header: str | None, names: set[str]) -> bool:
    """Whether a request's Host header names this panel.

    A browser page from anywhere can make its own domain resolve to this
    machine (DNS rebinding) and then read the answers as its own; it cannot
    make the browser send this machine's name or address as the Host. A
    request with no Host at all is no browser's, and passes.
    """
    host = str(header or "").strip().lower()
    if not host:
        return True
    if host.startswith("["):                       # [v6]:port
        host = host[1:host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":")[0]
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return host in names


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


_loopback = authmod._loopback


def _client_key(client_address) -> str:
    """Whom a connection is counted against: its address, with the whole of
    loopback (127.0.0.0/8, ::1) as one -- a local process must not multiply
    its share by connecting from 127.0.0.2, .3, ..."""
    address = str(client_address[0]) if client_address else ""
    return "loopback" if _loopback(address) else address


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer with a bound on the threads connections may take.

    The standard server starts a thread for every connection, without limit.
    A connection over MAX_CONNECTIONS, or over MAX_CONNECTIONS_PER_ADDRESS
    from one client, is closed at once instead.
    """

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self._count_lock = threading.Lock()
        self._open: dict[str, int] = {}
        self.refused = 0
        self._reported = -60.0
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        address = _client_key(client_address)
        local = address == "loopback"
        with self._count_lock:
            total = sum(n for a, n in self._open.items() if a != "loopback")
            if (total >= MAX_CONNECTIONS and not local) or \
                    self._open.get(address, 0) >= MAX_CONNECTIONS_PER_ADDRESS:
                self.refused += 1
                admitted = False
                # Said on stderr, the first time and then once a minute.
                now = time.monotonic()
                report = now - self._reported >= 60.0
                if report:
                    self._reported = now
            else:
                self._open[address] = self._open.get(address, 0) + 1
                admitted = True
        if not admitted:
            if report:
                sys.stderr.write(
                    f"  !! connection from {address} turned away: "
                    f"{self.refused} so far ({total} open, "
                    f"{self._open.get(address, 0)} from that address)\n")
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\n"
                                b"Content-Length: 0\r\nConnection: close\r\n"
                                b"Retry-After: 5\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._closed(address)
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._closed(_client_key(client_address))

    def _closed(self, address: str) -> None:
        with self._count_lock:
            left = self._open.get(address, 0) - 1
            if left > 0:
                self._open[address] = left
            else:
                self._open.pop(address, None)


class Application:
    """Everything the request handler needs, assembled once at startup."""

    def __init__(self, registry, database: "dbmod.Database",
                 engine: "runs.RunEngine", auth: "authmod.Auth | None" = None,
                 history=None, supervisor=None, finder=None,
                 chamber: dict | None = None, allow: list | None = None,
                 hosts: set[str] | None = None, origins: list | None = None,
                 admin_from: list | None = None):
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
        #: Names the panel answers to besides IP addresses (host_allowed).
        self.hosts = hosts if hosts is not None else machine_names()
        #: Other origins a panel may be served from and still use this API
        #: (--allow-origin). Same-origin requests need nothing.
        self.origins = {str(o).rstrip("/").lower() for o in (origins or [])}
        #: Networks from which what only an admin may do is accepted
        #: (--admin-from); empty accepts any. Loopback always may.
        self.admin_from = admin_from or []
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
        #: One download built at a time: each reads the whole store, and two
        #: at once double the memory and the disk the rig shares with them.
        self.export_slot = threading.BoundedSemaphore(1)

    #: The longest an orderly shutdown may take, all of it. Below the unit's
    #: TimeoutStopSec (90 s), so systemd never kills the process in the
    #: middle of a clean-up or a make-safe sequence.
    CLOSE_BUDGET_S = 75.0
    #: Of it, what is kept for after the run: the supervisor's last look, a
    #: recovery that look starts, and the last records.
    CLOSE_RESERVE_S = 30.0

    def close(self, budget: float | None = None) -> None:
        """Shut down in order: the run first -- its clean-up needs the
        instruments -- then the watchers, the instruments and the store,
        all of it within `budget` seconds."""
        budget = budget or self.CLOSE_BUDGET_S
        deadline = time.monotonic() + budget
        try:
            # The run's clean-up and a make-safe it starts get all but the
            # reserve -- 45 s of the 75.
            self.engine.close(
                deadline=deadline - min(self.CLOSE_RESERVE_S, budget * 0.4))
        finally:
            if self.supervisor is not None:
                self.supervisor.close(deadline=deadline)
                # Its thread is stopped now, so one last look of its own:
                # the run that just ended gets its alarm and its record.
                try:
                    self.supervisor.check()
                except Exception:
                    pass
                # ...and a recovery that look started is let finish.
                self.supervisor.close(deadline=deadline)
            # The supervisor's last records, queued while the run ended.
            try:
                self.engine._flush(runs.within(5.0, deadline))
            except Exception:
                pass
            if self.history is not None:
                self.history.close()
            self.registry.close()
            self.db.close()

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
        # The ping is open to any page: it says only what a sign-in page and
        # the kiosk need. Everything else answers another origin only if it
        # was named with --allow-origin -- a panel opened elsewhere and
        # pointed here. The session travels in a header no other page can
        # read, never in a cookie, so this is a second wall, not the first.
        origin = (self.headers.get("Origin") or "").rstrip("/").lower()
        if urlparse(self.path).path == "/api/ping":
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin and origin in self.app.origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        else:
            return
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
        """Refuse a client outside --allow-from, or a request naming another
        host, before anything else."""
        if not address_allowed(self._address(), self.app.allow):
            self._json({"ok": False, "error": "This panel only answers the lab "
                                              "network."}, 403)
            return False
        if not host_allowed(self.headers.get("Host"), self.app.hosts):
            self._json({"ok": False, "error": "This panel does not answer to "
                                              "that name."}, 421)
            return False
        return True

    def _same_site(self) -> bool:
        """A POST carrying an Origin must come from this panel's own pages
        or from one named with --allow-origin."""
        origin = (self.headers.get("Origin") or "").rstrip("/").lower()
        if not origin:
            return True                    # not sent by a browser page
        if origin in self.app.origins:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        return bool(host) and urlparse(origin).netloc == host

    def _admin_reachable(self) -> bool:
        """Whether what only an admin may do is accepted from this client
        (--admin-from). Loopback always is."""
        return address_allowed(self._address(), self.app.admin_from)

    def _admin_refused(self) -> bool:
        """Send the refusal and say so, if this client may not administer."""
        if self._admin_reachable():
            return False
        self._json({"ok": False, "error": "This is only done from the lab's own "
                                          "network."}, 403)
        return True

    def _who(self) -> "authmod.Principal":
        """Who sent this request: the resolved principal; the local owner
        only with accounts off; otherwise nobody at all, never the owner."""
        if self.principal is not None:
            return self.principal
        return authmod.LOCAL if not self.app.auth.enabled else NOBODY

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

    def _record(self, run_id, level: str, message: str,
                detail: dict | None = None, user: str | None = None) -> None:
        """Write one event; a failure is said on stderr, never raised. The
        hardware has already moved by the time most of these are written:
        a full SD card must cost the record, not the panel its answer."""
        try:
            self.app.db.add_event(run_id, level, message, detail, user=user)
        except Exception as exc:
            # Counted with the run's own failed writes: the `storage` alarm
            # puts it on screen, not only in the system log.
            note = getattr(self.app.engine, "note_write_failure", None)
            if note is not None:
                note(exc, message)
            else:
                sys.stderr.write(f"  !! could not record ({exc}): {message}\n")

    def _audit(self, message: str, level: str = "action",
               detail: dict | None = None) -> None:
        """Put an action in the rig's record under the name of who took it."""
        who = self._who()
        self._record(self.app.engine.run_id(), level, f"{who.name}: {message}",
                     detail, user=who.username)

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self._query().get(key, [default])[0])
        except (TypeError, ValueError):
            return default

    def _fresh(self, who: "authmod.Principal") -> bool:
        """Whether to read the drive now rather than serve a kept reading:
        asked for, by somebody who may operate. Every such read holds the
        serial line the pump's safety readings come through, so a viewer --
        or a script on a shared login -- gets the kept one."""
        return bool(self._int("fresh", 0)) and (
            not self.app.auth.enabled or who.at_least("operator"))

    def _limit(self, key: str, default: int) -> int:
        """A row count from the query, within 1..MAX_LIST."""
        return max(1, min(self._int(key, default), MAX_LIST))

    def _stats(self, principal: "authmod.Principal") -> dict:
        """The store's figures. Where the file lives is the machine's
        business: shown to an admin, not to everyone who may watch."""
        stats = self.app.db.stats()
        if self.app.auth.enabled and not principal.at_least("admin"):
            stats.pop("path", None)
        return stats

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

        if route in ADMIN_GETS or route == "/api/users":
            if route in ADMIN_GETS:
                refusal = self._check(principal, "admin", False)
                if refusal:
                    self._refused(refusal)
                    return
            if self._admin_refused():
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
                {"ok": True, "stats": self._stats(principal),
                 "devices": self.app.db.devices()}),
            "/api/events": lambda: self._json(
                {"ok": True, "events": self.app.db.events(
                    limit=self._limit("limit", 200))}),
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
        if not self._same_site():
            self._json({"ok": False, "error": "Refused: sent from another "
                                              "site's page."}, 403)
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
        if minimum == "admin" and self._admin_refused():
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
        try:
            if route == "/api/login":
                # A paired tablet sends its own session along: the person
                # signing in on it is signed out again when idle.
                device = payload.get("device")
                result = auth.login(str(payload.get("username", "")),
                                    str(payload.get("password", "")),
                                    self._address(), agent,
                                    device_token=device
                                    if isinstance(device, str) else None)
            elif route == "/api/signup":
                result = auth.signup(str(payload.get("username", "")),
                                     str(payload.get("name", "")),
                                     str(payload.get("password", "")),
                                     str(payload.get("note", "")),
                                     self._address())
            else:
                result = auth.pair(str(payload.get("code", "")),
                                   self._address(), agent)
        except authmod.Busy as busy:
            result = authmod.refusal(str(busy), 503, retry_after=2)
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

        if route.startswith(("/api/users/", "/api/devices/",
                             "/api/role-requests/decide")) \
                and self._admin_refused():
            return
        try:
            result = self._account_action(route, payload, principal, number)
        except authmod.Busy as busy:
            result = authmod.refusal(str(busy), 503, retry_after=2)
        if result.get("ok") and route.startswith(
                ("/api/users/", "/api/devices/", "/api/role-requests")):
            # Administering is a deliberate touch, like any other action.
            auth.mark_active(principal)
        if result.get("ok"):
            self._json(result)
        else:
            self._refused(result)

    def _account_action(self, route: str, payload: dict,
                        principal: "authmod.Principal", number) -> dict:
        auth = self.app.auth
        if route == "/api/logout":
            result = auth.logout(principal)
        elif route == "/api/session/touch":
            result = {"ok": True, "active": auth.mark_active(principal),
                      "me": auth.me(principal)}
        elif route == "/api/session/reauth":
            result = auth.reauth(principal, str(payload.get("password", "")),
                                 self._address())
        elif route == "/api/me/password":
            result = auth.change_password(principal,
                                          str(payload.get("current", "")),
                                          str(payload.get("new", "")),
                                          self._address())
        elif route == "/api/control/take":
            if payload.get("force") and not self._admin_reachable():
                result = authmod.refusal("Taking control over is only done from "
                                         "the lab's own network.", 403)
            elif principal.must_change:
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
                                         str(payload.get("password", "")),
                                         self._address())
        elif route == "/api/devices/add":
            result = auth.create_device(principal, str(payload.get("label", "")))
        elif route == "/api/devices/code":
            result = auth.renew_code(principal, number("id"))
        elif route == "/api/devices/remove":
            result = auth.remove_device(principal, number("id"))
        else:
            result = authmod.refusal("not found", 404)
        return result

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
            # What this person may write: the panel greys out the rest.
            who = self._who()
            report = device.accessories(fresh=self._fresh(who))
            if report.get("ok") and not who.at_least("admin"):
                report["writable"] = sorted(set(report.get("writable", []))
                                            & OPERATOR_WRITES)
            self._json(report)
        elif view == "details" and hasattr(device, "details"):
            self._json(device.details())
        elif view == "diagnostics" and hasattr(device, "diagnostics"):
            self._json(device.diagnostics(fresh=self._fresh(self._who())))
        else:
            self._json({"ok": False,
                        "error": f"{key} has no view {view!r}"}, 404)

    def _runs_get(self, route: str):
        parts = [p for p in route.split("/") if p]   # ['api', 'runs', ...]

        if len(parts) == 2:
            self._json({
                "ok": True,
                "runs": self.app.db.runs(
                    limit=self._limit("limit", 100),
                    offset=max(0, self._int("offset", 0)),
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
            self._one_export(lambda: self._download(
                *export.run_csv(self.app.db, run_id), "text/csv"))
        elif view in ("export.json", "json"):
            self._one_export(lambda: self._download(
                *export.run_json(self.app.db, run_id), "application/json"))
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
            def bounded(key: str, limit: int):
                value = payload.get(key)
                return None if value is None else str(value)[:limit]
            before = self.app.db.run(run_id)
            updated = self.app.db.update_run(
                run_id,
                name=bounded("name", RUN_NAME_MAX),
                operator=bounded("operator", RUN_OPERATOR_MAX),
                notes=bounded("notes", RUN_NOTES_MAX),
            )
            if updated is None:
                self._json({"ok": False, "error": f"no run {run_id}"}, 404)
                return
            # A run's record is evidence: who changed its name, operator or
            # notes, and from what, is kept in the rig's log.
            def short(text):
                text = "" if text is None else str(text)
                return text if len(text) <= 200 else text[:200] + "..."
            changed = {key: {"from": short((before or {}).get(key)),
                             "to": short(updated.get(key))}
                       for key in ("name", "operator", "notes")
                       if (before or {}).get(key) != updated.get(key)}
            if changed:
                who = self._who()
                self._record(None, "info",
                             f"{who.name}: edited the {', '.join(changed)} of "
                             f"run {run_id}",
                             {"run": run_id, "changes": changed},
                             user=who.username)
            self._json({"ok": True, "run": updated})
        elif action == "delete":
            if not payload.get("confirm"):
                self._json({"ok": False,
                            "error": "deleting a run needs confirm"}, 400)
                return
            outcome = self.app.engine.delete_run(run_id)
            if not outcome.get("ok"):
                self._json({"ok": False, "error": outcome.get("error")},
                           outcome.get("code", 400))
                return
            # Kept in the rig's log, not the run's, which is now gone -- with
            # what it still owed, which nothing will try again.
            record = outcome.get("record") or {}
            who = self._who()
            owed = (record.get("result") or {}).get("cleanup_pending") is True
            self._record(
                None, "warning",
                f"{who.name}: deleted run {run_id} ({record.get('name', '?')})"
                + (f", whose clean-up ({record.get('procedure')}) had not "
                   f"succeeded; it will not be tried again" if owed else ""),
                user=who.username)
            self._json({"ok": True, "error": None})
        else:
            self._json({"ok": False, "error": f"no action {action!r}"}, 404)

    def _one_export(self, build) -> None:
        """Build one download, waiting for any other being built."""
        slot = self.app.export_slot
        if not slot.acquire(timeout=EXPORT_WAIT_S):
            self._json({"ok": False, "error": "Another download is being "
                                              "prepared. Try again in a "
                                              "moment."}, 429)
            return
        try:
            build()
        finally:
            slot.release()

    def _export_get(self, route: str):
        name = route.rsplit("/", 1)[-1]
        db, folder = self.app.db, self.app._export_dir
        builders = {
            "runs.csv": lambda: self._download(*export.index_csv(db),
                                               "text/csv"),
            "runs.json": lambda: self._download(*export.index_json(db),
                                                "application/json"),
            "archive.json": lambda: self._download_file(
                *export.bundle_json(db, folder), "application/json"),
            "database.db": lambda: self._download_file(
                *export.database_snapshot(db, folder),
                "application/vnd.sqlite3"),
        }
        if name not in builders:
            self._json({"ok": False, "error": f"no export {name!r}"}, 404)
            return
        self._one_export(builders[name])

    def _command(self, payload: dict):
        key = str(payload.get("device", "pump"))
        action = str(payload.get("action", ""))
        who = self._who()
        raw = payload.get("override")
        overriding = raw not in (None, False)

        # Bypassing a safety rule is an admin's call, whoever holds control.
        # Checked first, so the answer never depends on anything else.
        if overriding and not who.at_least("admin"):
            self._json({"ok": False, "interlock": True,
                        "error": "Only an admin may override an interlock. "
                                 "Ask one, or deal with what the interlock "
                                 "is refusing."}, 403)
            return
        # An override names the rules it waives -- one, or the few that stood
        # in the way of the same command -- with the admin's reason in their
        # own words and their word that they checked the rig:
        # `{"rules": [...], "reason": ..., "checked": true}` ("rule" for one).
        # `true` alone -- "waive whatever is in the way" -- is refused.
        rules: list[str] = []
        if overriding:
            named = (raw.get("rules") or [raw.get("rule")]
                     if isinstance(raw, dict) else [])
            rules = [str(r) for r in (named if isinstance(named, list)
                                      else [named]) if r]
            if not rules:
                self._json({"ok": False, "error": "An override names the rules "
                                                  "it waives: {rules, reason, "
                                                  "checked}."}, 400)
                return
            if raw.get("checked") is not True:
                self._json({"ok": False, "error": "An override needs the "
                                                  "confirmation that the rig "
                                                  "was checked in person."}, 400)
                return
            reason = raw.get("reason")
            if not isinstance(reason, str) or len(reason.strip()) < 3:
                self._json({"ok": False, "error": "An override needs the reason "
                                                  "it is safe, in your own "
                                                  "words."}, 400)
                return
        # The drive's own protections and settings are an admin's to change;
        # an operator keeps the pump's heater, for a bake-out.
        if overriding and self._admin_refused():
            return
        if action == "param":
            name = str((payload.get("value") or {}).get("name", ""))
            if name not in OPERATOR_WRITES and self._admin_refused():
                return
            if name not in OPERATOR_WRITES and not who.at_least("admin"):
                self._json({"ok": False,
                            "error": f"{name} is one of the drive's own "
                                     f"protections or settings, which only an "
                                     f"admin may change. An operator may "
                                     f"switch the pump's heater and set its "
                                     f"temperature."}, 403)
                return
        if key not in self.app.registry:
            self._json({"ok": False, "error": f"no device named {key!r}"}, 404)
            return
        if action in CONFIRM_REQUIRED and not payload.get("confirm"):
            self._json({"ok": False,
                        "error": f"{action} changes the hardware and needs "
                                 f"confirm"}, 400)
            return

        device = self.app.registry.get(key)
        result = device.command(action, payload.get("value"), rules or None)
        waived = result.get("overridden") or []

        # Logged twice, under the admin's name, with the rule and the reason
        # they were shown -- and only when the rule really stood in the way.
        if waived:
            sys.stderr.write(f"  !! INTERLOCK OVERRIDDEN for {key}.{action} "
                             f"(rule {', '.join(waived)}) by {who.username} at "
                             f"{self.address_string()}\n")
            self._record(
                self.app.engine.run_id(), "warning",
                f"{who.name}: interlock overridden for {key}.{action}: "
                f"{', '.join(waived)}",
                {"from": self.address_string(), "value": payload.get("value"),
                 "rules": rules, "waived": waived,
                 "reason": str(raw.get("reason", ""))[:500],
                 "refusal": str(raw.get("refusal", ""))[:500],
                 "checked": True, "ok": bool(result.get("ok"))},
                user=who.username)
        elif overriding:
            # Asked for, but that rule was not what stood in the way (or the
            # command failed before it): recorded, so the attempt is not lost.
            self._record(
                self.app.engine.run_id(), "info",
                f"{who.name}: asked to override {', '.join(rules)} for "
                f"{key}.{action}; "
                + ("not needed, nothing was waived" if result.get("ok") else
                   f"refused: {result.get('error')}"),
                {"from": self.address_string(), "rules": rules,
                 "reason": str(raw.get("reason", ""))[:500]},
                user=who.username)

        # Actions that change the hardware belong in the rig's narrative, so
        # a curve with a step in it can be explained months later -- and now
        # say who took them.
        if result.get("ok") and (action in CONFIRM_REQUIRED or waived):
            value = payload.get("value")
            if action == "param" and isinstance(value, dict):
                said = (f"{device.label}: {value.get('name')} changed from "
                        f"{result.get('previous')!r} to {result.get('result')!r}")
            else:
                said = (f"{device.label}, {action.replace('_', ' ')}"
                        + (f" {value}" if value is not None else ""))
            self._audit(said + (" (interlock overridden)" if waived else ""),
                        detail={"device": key, "action": action, "value": value,
                                "previous": result.get("previous"),
                                "overridden": waived})
        self._json(result)
