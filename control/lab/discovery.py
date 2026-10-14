"""Finding the instruments: which serial port each one is on, by asking it.

Which port an instrument sits on is not a fixed fact on this rig. USB
adapters are numbered in the order the kernel finds them, so a replug swaps
/dev/ttyUSB0 and /dev/ttyUSB1 -- and the panel then speaks the TC 600's
RS-485 telegrams at the gauge. The onboard UARTs died on 2026-08-31. Adapters
get replaced. The udev rules in deploy/ pin a name to an adapter's serial
number, which is good, but only for that adapter and only once installed.

So this asks each candidate port what is on the other end:

* **TC 600** -- opened at 9600 8N1 with DTR and RTS asserted (the HXSP-485
  draws its power from them), then parameter 349, ElecName, is *read* at the
  configured RS-485 address. A reply with a valid checksum naming a drive
  unit is unambiguous.
* **TPG** -- the mnemonic handshake at each rate the family supports, reading
  the firmware version (PNR), the family's own read-only discriminator.

Neither probe writes anything. A TC 600 telegram reaching a TPG is an unknown
mnemonic and is answered with a NAK; TPG mnemonics reaching the RS-485 bus
fail the TC 600's checksum and are ignored. A port already held by one of
this panel's own instruments is never probed -- one owner per port.

Each instrument found is opened by the most stable name its port has: the
udev name (/dev/tc600, /dev/tpg252), else /dev/serial/by-id/..., else the bare
device. The adapter's serial number is remembered in a small JSON file next
to the database, so the next start tries that adapter first and a replug
costs one probe, not a sweep.
"""

from __future__ import annotations

import glob
import json
import os
import threading
import time

#: The udev names from deploy/99-vacuum-lab.rules, per kind of instrument.
UDEV_NAMES = {"tc600": "/dev/tc600", "tpg": "/dev/tpg252"}

#: What each kind of instrument is called on screen.
LABELS = {"tc600": "TC 600 turbopump", "tpg": "TPG gauge controller"}


def classify_port(port) -> str:
    """"usb" for an adapter (it has a vendor:product id), "serial" for a UART."""
    hwid = (getattr(port, "hwid", "") or "").upper()
    device = getattr(port, "device", "") or ""
    if "USB" in hwid or "USB" in device.upper():
        return "usb"
    if "/dev/ttyS" in device or "COM" in device.upper():
        return "serial"
    return "other"


def uart_present(device: str) -> bool:
    """Whether an onboard serial node is backed by a real UART.

    Linux creates /dev/ttyS0 through ttyS31 whether or not the hardware is
    there. The sysfs `type` attribute reads 0 (PORT_UNKNOWN) for the phantoms
    and non-zero for a real chip. Anything without it -- every USB adapter --
    is assumed real.
    """
    name = os.path.basename(device)
    try:
        with open(f"/sys/class/tty/{name}/type") as handle:
            return int(handle.read().strip()) != 0
    except (OSError, ValueError):
        return True


def real(path: str) -> str:
    """The device node behind a symlink, so two names for one port compare equal."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def stable_name(device: str) -> str:
    """The most stable name this port has: udev name, by-id path, or itself."""
    target = real(device)
    for name in UDEV_NAMES.values():
        if os.path.exists(name) and real(name) == target:
            return name
    for path in sorted(glob.glob("/dev/serial/by-id/*")):
        if real(path) == target:
            return path
    return device


def candidate_ports(exclude=()) -> list[dict]:
    """Every serial port an instrument could be on, minus the ones in use.

    USB adapters and real UARTs only: phantom /dev/ttyS nodes are skipped,
    and so is anything in `exclude` (compared by the node behind any alias).
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    excluded = {real(path) for path in exclude if path}
    found = []
    for port in sorted(list_ports.comports(), key=lambda p: p.device):
        kind = classify_port(port)
        if kind == "other" or not uart_present(port.device):
            continue
        if real(port.device) in excluded:
            continue
        found.append({
            "device": port.device,
            "kind": kind,
            "serial_number": getattr(port, "serial_number", "") or "",
            "description": port.description or "",
            "hwid": port.hwid or "",
        })
    return found


# --- probes -----------------------------------------------------------------


def probe_tc600(port: str, address: int = 1, timeout: float = 0.5) -> dict | None:
    """Ask `port` whether a TC 600 is on it. Read-only. None if not."""
    from tc600 import device as tc600_device

    try:
        with tc600_device.TC600(port, address=address, timeout=timeout,
                                retries=1) as pump:
            # End any partial frame another probe left in the receiver --
            # TPG mnemonics end in CR LF and an ENQ, and a stray LF or ENQ
            # in front of the next telegram spoils its checksum. A lone CR is
            # an empty telegram, which the drive ignores.
            pump.serial.write(b"\r")
            pump.serial.flush()
            time.sleep(0.05)
            pump.serial.reset_input_buffer()
            name = str(pump.read("ElecName")).strip()
            identity = {"ElecName": name}
            try:
                identity["FwVersion"] = str(pump.read("FwVersion")).strip()
            except Exception:
                pass
    except Exception:
        return None
    if not name:
        return None
    return {"kind": "tc600", "address": address, "identity": identity,
            "says": f"{name}" + (f", firmware {identity['FwVersion']}"
                                 if identity.get("FwVersion") else "")}


def probe_tpg(port: str, timeout: float = 0.5) -> dict | None:
    """Ask `port` whether a TPG is on it, at each supported rate. Read-only."""
    import tpg

    unit = None
    try:
        unit = tpg.autodetect(port, timeout=timeout)
        if unit is None:
            return None
        firmware = str(unit.firmware()).strip()
        rate = unit.baudrate
    except Exception:
        return None
    finally:
        if unit is not None:
            try:
                unit.close()
            except Exception:
                pass
    return {"kind": "tpg", "baudrate": rate, "identity": {"firmware": firmware},
            "says": f"firmware {firmware} at {rate} baud"}


PROBES = {"tc600": probe_tc600, "tpg": probe_tpg}


# --- memory -----------------------------------------------------------------


class PortMemory:
    """Which USB adapter carried each instrument last time, by serial number."""

    def __init__(self, path: str | None):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict = {}
        if path:
            try:
                with open(path) as handle:
                    self.data = json.load(handle)
            except (OSError, ValueError):
                self.data = {}

    def remember(self, kind: str, port: str, serial_number: str = "") -> None:
        with self._lock:
            self.data[kind] = {"port": port, "serial_number": serial_number,
                               "seen_at": time.time()}
            if not self.path:
                return
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)),
                            exist_ok=True)
                with open(self.path, "w") as handle:
                    json.dump(self.data, handle, indent=2)
            except OSError:
                pass

    def preferred(self, kind: str, candidates: list[dict]) -> list[str]:
        """The ports to try first for `kind`: the remembered adapter's, then
        the remembered port name."""
        entry = self.data.get(kind) or {}
        first = [c["device"] for c in candidates
                 if entry.get("serial_number")
                 and c.get("serial_number") == entry["serial_number"]]
        if entry.get("port"):
            first.append(entry["port"])
        return first


# --- finding ------------------------------------------------------------------


def find(kind: str, candidates: list[str], address: int = 1,
         timeout: float = 0.5, tried: list | None = None) -> dict | None:
    """Probe `candidates` in order for an instrument of `kind`.

    Returns the first answer, with the port and its stable name. `tried`, if
    given, collects what each port said, for the panel's report.
    """
    probe = PROBES[kind]
    seen = set()
    for port in candidates:
        node = real(port)
        if not port or node in seen or not os.path.exists(port):
            continue
        seen.add(node)
        started = time.monotonic()
        answer = (probe(port, address=address, timeout=timeout)
                  if kind == "tc600" else probe(port, timeout=timeout))
        if tried is not None:
            tried.append({"port": port, "kind": kind,
                          "answered": answer is not None,
                          "says": answer["says"] if answer else "nothing answered",
                          "seconds": round(time.monotonic() - started, 2)})
        if answer:
            return {**answer, "device": node, "port": stable_name(port)}
    return None


class Finder:
    """Searches the free ports for an instrument; shared by every service.

    One search at a time: two instruments probing the same free port at once
    would each read the other's replies.
    """

    def __init__(self, registry, memory: PortMemory | None = None,
                 enabled: bool = True, ports=candidate_ports, udev_names=None):
        self.registry = registry
        self.memory = memory or PortMemory(None)
        self.enabled = enabled
        # Where to look. The selftest narrows both to its simulators' ports:
        # on the lab PC the defaults reach the real instruments, and a test
        # finding the real pump by itself is a test talking to the rig.
        self.ports = ports
        self.udev_names = UDEV_NAMES if udev_names is None else udev_names
        self._lock = threading.Lock()
        #: The last report: what each port said, when.
        self.last_report: dict | None = None

    def owned_ports(self, except_service=None) -> list[str]:
        return [service.port for service in self.registry
                if service is not except_service and service.port]

    def search(self, service) -> dict | None:
        """Find `service`'s instrument on its own port or any free one."""
        kind = getattr(service, "probe_kind", None)
        if kind not in PROBES:
            return None
        with self._lock:
            exclude = self.owned_ports(except_service=service)
            candidates = self.ports(exclude=exclude)
            order = ([service.port, self.udev_names.get(kind, "")]
                     + self.memory.preferred(kind, candidates)
                     + [c["device"] for c in candidates])
            excluded = {real(p) for p in exclude}
            order = [p for p in order if p and real(p) not in excluded]
            tried: list = []
            found = find(kind, order, address=getattr(service, "address", 1),
                         tried=tried)
            self.last_report = {"at": time.time(), "for": kind, "tried": tried,
                                "found": found}
        if found:
            serial_number = next((c["serial_number"] for c in candidates
                                  if real(c["device"]) == found["device"]), "")
            self.memory.remember(kind, found["port"], serial_number)
        return found

    def survey(self) -> dict:
        """Report what every free port answers to -- for the Settings page.

        Ports held by this panel's instruments are listed as such and not
        probed: they already have an owner, and it is talking to them.
        """
        with self._lock:
            owned = {real(service.port): service for service in self.registry
                     if service.port}
            ports = self.ports()
            rows = []
            for port in ports:
                owner = owned.get(real(port["device"]))
                if owner is not None:
                    snap = owner.snapshot()
                    rows.append({**port, "stable": stable_name(port["device"]),
                                 "status": "in use",
                                 "says": f"{owner.label}"
                                 + (" (connected)" if snap.get("connected")
                                    else " (not answering)")})
                    continue
                answer = None
                for kind in ("tc600", "tpg"):
                    tried: list = []
                    answer = find(kind, [port["device"]], tried=tried)
                    if answer:
                        break
                rows.append({**port, "stable": stable_name(port["device"]),
                             "status": "found" if answer else "silent",
                             "found_kind": answer["kind"] if answer else None,
                             "says": (f"{LABELS[answer['kind']]}: {answer['says']}"
                                      if answer else "nothing answered")})
            self.last_report = {"at": time.time(), "survey": rows}
            return {"ok": True, "ports": rows, "at": time.time()}


def startup(kind: str, given: str, address: int, memory: PortMemory,
            search: bool = True, log=print, exclude=()) -> dict:
    """Decide which port an instrument opens at startup.

    `given` is the --serial / --gauge value: a port, or "auto". A given port
    is tried first; if nothing of the right kind answers there (or the value
    is "auto"), every free port is probed. Returns {"port", "found",
    "tried"} -- with nothing found, the given port (or the udev name) is
    returned anyway so the service starts, reports itself offline, and keeps
    looking in the background.
    """
    automatic = (given or "auto").lower() == "auto"
    candidates = candidate_ports(exclude=exclude)
    order = ([] if automatic else [given]) + [UDEV_NAMES.get(kind, "")] \
        + memory.preferred(kind, candidates) + [c["device"] for c in candidates]
    excluded = {real(p) for p in exclude if p}
    order = [p for p in order if p and real(p) not in excluded]
    if not search and not automatic:
        order = [given]
    tried: list = []
    found = find(kind, order, address=address, tried=tried)
    if found:
        serial_number = next((c["serial_number"] for c in candidates
                              if real(c["device"]) == found["device"]), "")
        memory.remember(kind, found["port"], serial_number)
        log(f"  {LABELS[kind]} found on {found['port']}"
            + (f" ({found['device']})" if found["port"] != found["device"] else "")
            + f": {found['says']}")
        return {"port": found["port"], "found": found, "tried": tried}
    fallback = given if not automatic else (
        UDEV_NAMES.get(kind) if os.path.exists(UDEV_NAMES.get(kind, ""))
        else (memory.data.get(kind) or {}).get("port") or "auto")
    log(f"  {LABELS[kind]} not found on any port"
        + (f"; starting on {fallback} and searching in the background"
           if search else f"; starting on {fallback}"))
    return {"port": fallback, "found": None, "tried": tried}
