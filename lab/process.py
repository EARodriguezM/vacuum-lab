"""Process hardware: the things that make a deposition happen.

The pump and the gauge measure and pump. These four move the process:

    gas       argon admission -- a mass flow controller, or a needle valve
    gate      the valve between chamber and turbopump, open/closed/throttled
    magnetron the sputter source: enable plus a power setpoint
    shutter   the flap over the substrate, so the target can be cleaned first

None of them is wired yet, and that is the point of this module. Every
actuator exists from day one with `driver = None`, which means *manual*: the
software knows the step is needed, knows what it should say, and knows it
cannot do it itself. In guided mode the run engine stops and asks the
operator; in automatic mode it refuses to start a recipe whose steps it
cannot actually perform.

Wiring one up later is a matter of writing a driver object with `set` and
`read` and handing it to the actuator. No recipe changes: `Deposition` in
runs.py never asks whether the hardware is real.

Why this shape rather than a `DeviceService` each: a service owns a serial
port and a polling thread. A gate valve driven from a relay on the TPG's
switching output has no port of its own, and a needle valve has no electrical
existence at all. What they share is "a thing the recipe sets and reads",
which is all this abstracts.
"""

from __future__ import annotations

import threading
import time


class ActuatorError(Exception):
    """Raised when an actuator is present but the command failed."""


class OperatorRequired(Exception):
    """Raised when a step needs a human, because nothing is wired to do it.

    Carries the instruction to put in front of the operator. The run engine
    turns this into a prompt in guided mode and into a refusal in automatic
    mode -- a recipe that claims to be automatic must not silently depend on
    somebody noticing a message.
    """

    def __init__(self, instruction: str, actuator: str = "", value=None):
        super().__init__(instruction)
        self.instruction = instruction
        self.actuator = actuator
        self.value = value


class Actuator:
    """One thing the process sets and reads.

    `driver`, when present, must offer `set(value)` and `read()`. Until then
    the actuator is manual: it records what the recipe asked for, so the run
    log still says what the process state was meant to be, and raises
    `OperatorRequired` so somebody does it.
    """

    kind = "actuator"
    unit = ""

    def __init__(self, key: str, label: str, driver=None):
        self.key = key
        self.label = label
        self.driver = driver
        self._lock = threading.Lock()
        #: Last value the recipe asked for, whether or not anything carried it
        #: out. This is what the run log and the dashboard display.
        self.requested = None
        self.changed_at: float | None = None
        #: Whether that request is known to have taken effect. A driven
        #: actuator confirms itself; a manual one stays False until a human
        #: says they have done it. The distinction matters on screen: showing
        #: a shutter as "closed" when all that happened is that the software
        #: asked for it is exactly the kind of display that gets a sample
        #: coated during a pre-sputter.
        self.confirmed = False

    @property
    def available(self) -> bool:
        """Whether software can actually move this."""
        return self.driver is not None

    def instruction(self, value) -> str:
        """What to tell the operator when this has to be done by hand."""
        return f"Set {self.label} to {self.format(value)}."

    def format(self, value) -> str:
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, (int, float)) and self.unit:
            return f"{value:g} {self.unit}"
        return str(value)

    def set(self, value):
        """Ask for a new state. Raises OperatorRequired when unwired."""
        with self._lock:
            self.requested = value
            self.changed_at = time.time()
            self.confirmed = self.driver is not None
            if self.driver is None:
                raise OperatorRequired(self.instruction(value), self.key, value)
            try:
                return self.driver.set(value)
            except OperatorRequired:
                raise
            except Exception as exc:
                raise ActuatorError(f"{self.label}: {exc}") from exc

    def confirm(self) -> None:
        """An operator says the manual step is done."""
        self.confirmed = True

    def read(self):
        """Current state as the hardware reports it, or what was requested."""
        if self.driver is None:
            return self.requested
        try:
            return self.driver.read()
        except Exception as exc:
            raise ActuatorError(f"{self.label}: {exc}") from exc

    def describe(self) -> dict:
        try:
            actual = self.read()
        except ActuatorError as exc:
            actual, error = None, str(exc)
        else:
            error = None
        shown = actual if actual is not None else self.requested
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "unit": self.unit,
            "available": self.available,
            "requested": self.requested,
            "actual": actual,
            "confirmed": self.confirmed,
            "display": self.format(shown) if (self.confirmed or actual is not None)
            else "—",
            "pending": None if self.confirmed or self.requested is None
            else self.format(self.requested),
            "changed_at": self.changed_at,
            "error": error,
        }


class GasFlow(Actuator):
    """Argon admission. A mass flow controller, or a needle valve by hand."""

    kind = "gas"
    unit = "sccm"

    def instruction(self, value) -> str:
        if not value:
            return "Close the argon valve completely."
        return (f"Open the argon needle valve until the flow reads "
                f"{value:g} sccm — or until the chamber settles at the "
                f"working pressure below.")


class GateValve(Actuator):
    """The valve between chamber and turbopump.

    Values are a percentage: 100 fully open, 0 shut. Throttling it is how a
    turbopump at full speed is made to hold 1e-2 mbar of argon instead of
    pumping it straight away.
    """

    kind = "valve"
    unit = "%"

    def instruction(self, value) -> str:
        if value is None:
            return f"Set {self.label}."
        if value >= 99:
            return "Open the gate valve to the turbopump fully."
        if value <= 1:
            return "Close the gate valve to the turbopump."
        return (f"Throttle the gate valve to roughly {value:g}% open, then "
                "adjust until the working pressure holds steady.")


class Magnetron(Actuator):
    """The sputter source. Value is power in watts; 0 or False means off."""

    kind = "source"
    unit = "W"

    def instruction(self, value) -> str:
        if not value:
            return "Switch the magnetron power supply OFF."
        return (f"Set the magnetron supply to {value:g} W and strike the "
                "plasma. Confirm the discharge is stable before continuing.")


class Shutter(Actuator):
    """The flap between target and substrate. True is open."""

    kind = "shutter"

    def format(self, value) -> str:
        if value is None:
            return "—"
        return "open" if value else "closed"

    def instruction(self, value) -> str:
        return ("OPEN the substrate shutter — deposition starts now."
                if value else
                "CLOSE the substrate shutter, so the target is sputtered "
                "clean without coating the sample.")


class ProcessHardware:
    """The four actuators, whether or not any of them is wired yet."""

    def __init__(self, drivers: dict | None = None):
        drivers = drivers or {}
        self.gas = GasFlow("gas", "Argon flow", drivers.get("gas"))
        self.gate = GateValve("gate", "Gate valve", drivers.get("gate"))
        self.magnetron = Magnetron("magnetron", "Magnetron",
                                   drivers.get("magnetron"))
        self.shutter = Shutter("shutter", "Substrate shutter",
                               drivers.get("shutter"))
        self._all = {a.key: a for a in
                     (self.gas, self.gate, self.magnetron, self.shutter)}

    def __iter__(self):
        return iter(self._all.values())

    def get(self, key: str) -> Actuator:
        try:
            return self._all[key]
        except KeyError:
            raise ActuatorError(f"no actuator named {key!r}") from None

    def attach(self, key: str, driver) -> Actuator:
        """Give an actuator a real driver, once the hardware is wired."""
        actuator = self.get(key)
        actuator.driver = driver
        return actuator

    @property
    def automatic_ready(self) -> bool:
        """Whether a recipe could run with no human in the loop."""
        return all(a.available for a in self._all.values())

    def missing(self) -> list[str]:
        """Actuators a fully automatic run would need and does not have."""
        return [a.label for a in self._all.values() if not a.available]

    def describe(self) -> list[dict]:
        return [a.describe() for a in self._all.values()]

    def safe_shutdown(self, log=None) -> None:
        """Put the process hardware in its safe state, best effort.

        Called when a run ends for any reason. Order matters: the plasma goes
        first, then the gas, then the shutter closes. Every step is attempted
        even if an earlier one failed, because "the magnetron would not switch
        off" must not also leave the gas running.
        """
        for actuator, value in ((self.magnetron, 0), (self.gas, 0),
                                (self.shutter, False)):
            try:
                actuator.set(value)
            except OperatorRequired as prompt:
                if log:
                    log("warning", f"MANUAL STEP: {prompt.instruction}")
            except ActuatorError as exc:
                if log:
                    log("error", f"Could not make {actuator.label} safe: {exc}")
