"""The device framework: what every instrument in this rig has in common.

Adding an instrument means subclassing `DeviceService`, implementing three
methods, and registering it. Nothing else in the application needs to change:
the API exposes whatever is registered, the run engine samples whatever is
registered, and the UI renders whatever the API reports.

The shape every device shares:

    _open()            establish the link, return a driver object
    _read()            one poll -> a plain dict of readings
    sample_fields()    map that dict onto the run-sample columns

Each service owns its serial port and polls it on its own thread. That is the
important structural decision: a serial port tolerates exactly one owner, so
the port is held by one long-lived sampler and every reader -- HTTP handler,
run engine, CLI -- takes the cached snapshot instead. It also means the run
engine records what was *observed continuously*, not what happened to be true
the instant a browser asked.
"""

from __future__ import annotations

import threading
import time


class DeviceError(Exception):
    """Raised when a device command cannot be carried out."""


class DeviceService:
    """Base class: owns a link, polls it, publishes a cached snapshot."""

    #: Seconds between polls. Overridden per device -- a gauge is cheap to
    #: read, a pump with a dozen parameters is not.
    interval = 2.0

    #: Consecutive failures tolerated before the link is torn down and
    #: reopened. A serial handle can wedge open: writes stop reaching the UART
    #: while the descriptor still looks healthy, so nothing ever raises.
    reconnect_after = 3

    kind = "device"
    label = "Device"

    def __init__(self, key: str, port: str, **options):
        self.key = key
        self.port = port
        self.options = options
        self.lock = threading.RLock()          # guards the link itself
        self._driver = None
        self._identity: dict = {}
        self._failures = 0

        self._snap: dict = self.offline("starting up")
        self._snap_lock = threading.Lock()

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"{key}-sampler", daemon=True
        )
        self._thread.start()

    # --- to implement in a subclass ---------------------------------------

    def _open(self):
        """Open the link and return the driver object."""
        raise NotImplementedError

    def _read(self) -> dict:
        """One poll. Return the readings; raise to signal a lost link."""
        raise NotImplementedError

    def sample_fields(self, snap: dict) -> dict:
        """Map a snapshot onto columns of the run-sample row."""
        return {}

    def command(self, action: str, value=None, override: bool = False) -> dict:
        """Carry out a named action. Devices with no controls need not."""
        return {"ok": False, "error": f"{self.label} has no action {action!r}"}

    # --- link management ---------------------------------------------------

    def driver(self):
        """The live driver object, opening the link if it is not up yet."""
        if self._driver is None:
            self._driver = self._open()
        return self._driver

    def drop(self) -> None:
        if self._driver is not None:
            try:
                self._driver.close()
            except Exception:
                pass
            self._driver = None
        self._identity = {}

    def note_failure(self) -> None:
        self._failures += 1
        if self._failures >= self.reconnect_after:
            self.drop()
            self._failures = 0

    def note_success(self) -> None:
        self._failures = 0

    def rebind(self, port: str, **options) -> dict:
        """Move this instrument to a different serial port, live.

        Which port an instrument is on is not a compile-time fact on this rig:
        USB adapters renumber on replug, and the gauge had to be moved from a
        USB adapter to an onboard UART before it would talk at all. Making
        that a field in the UI rather than a command-line flag is the
        difference between a two-second fix and an SSH session.

        The old link is dropped first, so the previous port is released even
        if the new one turns out to be wrong; the sampler thread then reopens
        against whatever is configured and reports the outcome as usual.
        """
        port = (port or "").strip()
        if not port:
            return {"ok": False, "error": "no port given"}

        previous = self.port
        with self.lock:
            self.drop()
            self.port = port
            for name, value in options.items():
                if value is not None and hasattr(self, name):
                    setattr(self, name, value)
            self._failures = 0

        # Let the sampler take one cycle against the new port so the caller
        # learns whether it worked, rather than getting an optimistic "ok".
        deadline = time.monotonic() + max(4.0, self.interval * 2 + 2)
        while time.monotonic() < deadline:
            snap = self.snapshot()
            if snap.get("timestamp", 0) > 0 and snap.get("age", 99) < self.interval + 1:
                if snap.get("connected"):
                    return {"ok": True, "port": port, "previous": previous,
                            "connected": True}
            time.sleep(0.2)

        snap = self.snapshot()
        return {
            "ok": bool(snap.get("connected")),
            "port": port,
            "previous": previous,
            "connected": bool(snap.get("connected")),
            "error": None if snap.get("connected") else
            f"nothing answered on {port}: {snap.get('error')}",
        }

    def close(self) -> None:
        self._stop.set()
        with self.lock:
            self.drop()

    # --- sampling ---------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                snap = self._read()
            except Exception as exc:              # never kill the thread
                snap = self.offline(str(exc))
            self.observe(snap)
            with self._snap_lock:
                self._snap = snap
            self._stop.wait(self.interval)

    def observe(self, snap: dict) -> None:
        """Hook for state that must be tracked continuously, e.g. timers."""

    def offline(self, message: str) -> dict:
        return {
            "key": self.key, "kind": self.kind, "label": self.label,
            "connected": False, "port": self.port, "error": message,
            "identity": {}, "timestamp": time.time(),
        }

    def online(self, **payload) -> dict:
        return {
            "key": self.key, "kind": self.kind, "label": self.label,
            "connected": True, "port": self.port, "error": None,
            "identity": self._identity, "timestamp": time.time(),
            **payload,
        }

    def snapshot(self) -> dict:
        with self._snap_lock:
            snap = dict(self._snap)
        snap["age"] = round(time.time() - snap.get("timestamp", 0), 1)
        return snap


class Registry:
    """Every instrument this process is driving, keyed by name."""

    def __init__(self):
        self._devices: dict[str, DeviceService] = {}

    def add(self, service: DeviceService) -> DeviceService:
        self._devices[service.key] = service
        return service

    def get(self, key: str) -> DeviceService:
        try:
            return self._devices[key]
        except KeyError:
            raise DeviceError(f"no device named {key!r}") from None

    def __contains__(self, key: str) -> bool:
        return key in self._devices

    def __iter__(self):
        return iter(self._devices.values())

    def keys(self) -> list[str]:
        return list(self._devices)

    def snapshot(self) -> dict:
        return {key: service.snapshot() for key, service in self._devices.items()}

    def descriptors(self) -> list[dict]:
        """What the UI needs to render a device list without polling each one."""
        return [
            {"key": s.key, "kind": s.kind, "label": s.label, "port": s.port}
            for s in self._devices.values()
        ]

    def sample_row(self) -> dict:
        """Merge every device's contribution into one run-sample row."""
        row: dict = {}
        for service in self._devices.values():
            try:
                row.update(service.sample_fields(service.snapshot()))
            except Exception:
                continue
        return row

    def close(self) -> None:
        for service in self._devices.values():
            service.close()
