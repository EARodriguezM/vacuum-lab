#!/usr/bin/env python3
"""End-to-end test of the whole stack against the simulators, no hardware needed.

Runs the fake pump and the fake gauge in background threads on pseudo-terminals
and drives the real client code against them: `./tools/selftest.py`

The checks live in `tools/checks/`, one module per area. Sections run in
dependency order -- the drivers, the analysis maths, the process hardware,
the store, accounts, the chart recorder, a whole recorded run -- then five
that make things go wrong on purpose: `faults` (lost, late and refused
replies; the interlocks over every combination of readings), `timing` (how
fresh the readings are, how soon a run reacts, the clock set an hour either
way), `lifecycle` (a run's start, end and clean-up when the database, the
service or the process fails), `authority` (overrides and who may change
the drive) and `security` (idle sessions, bursts of sign-ins and
connections, wrong clocks, stray hosts and origins, downloads that must not
hold up the rig) -- and last port discovery, what the panel says about
itself (`observability`), backups and releases.

Each section starts from the same place: both simulators reset, and
whatever the one before changed -- a module's constants, the clock -- put
back, and whatever it left running -- a sampler, a run engine, a
supervisor, a chart recorder -- closed. Either is a FAIL of the section
that left it. A section that raises is a FAIL, and the sections after it
still run.

    --only NAME     run one section (repeatable); see SECTIONS
    --coverage      also report which lines of lab/, tc600/, tpg/ never ran
"""

from __future__ import annotations

import argparse
import copy
import gc
import os
import re
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: Every section, by the name `--only` takes, in the order they run.
SECTIONS = ("pump", "gauge", "cold_cathode", "analysis", "process", "equipment",
            "deposition", "database", "accounts", "history", "engine", "faults",
            "timing", "lifecycle", "authority", "security", "discovery",
            "observability", "backup", "release")

#: Where a section's code lives, and how it is called: (module, function,
#: which simulator ports it takes).
WHERE = {
    "pump": ("drivers", "test_pump", ("pump",)),
    "gauge": ("drivers", "test_gauge", ("gauge",)),
    "cold_cathode": ("drivers", "test_cold_cathode_interlock", ()),
    "analysis": ("analysis", "test_analysis", ()),
    "process": ("process", "test_process", ()),
    "equipment": ("process", "test_equipment", ()),
    "deposition": ("process", "test_deposition_engine", ()),
    "database": ("store", "test_database", ()),
    "accounts": ("accounts", "test_accounts", ()),
    "history": ("store", "test_history_and_charts", ()),
    "engine": ("engine", "test_run_engine", ("pump", "gauge")),
    "faults": ("faults", "test_faults", ("pump", "gauge")),
    "timing": ("timing", "test_timing", ("pump", "gauge")),
    "lifecycle": ("lifecycle", "test_lifecycle", ("pump", "gauge")),
    "authority": ("authority", "test_authority", ("pump",)),
    "security": ("security", "test_security", ("pump",)),
    "discovery": ("discovery", "test_discovery", ("pump", "gauge")),
    "observability": ("observability", "test_observability", ()),
    "backup": ("backup", "test_backup", ()),
    "release": ("release", "test_release", ()),
}

#: Module names whose constants a section may change and must not keep.
WATCHED = ("lab.", "tc600", "tpg", "tools.fake_")
CONSTANT = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
#: The simulators' own state, put back by their reset() before each section.
RESET_BY_SIMULATOR = {"STATE", "FAULTS", "CHAMBER"}
_CONTAINERS = (dict, list, set)


# --- keeping one section's changes from reaching the next ------------------


def _watched_modules():
    return [m for name, m in list(sys.modules.items())
            if m is not None and name.startswith(WATCHED)]


def _constants() -> dict:
    """Every UPPER_CASE attribute of the rig's modules and of the classes
    they define, with a copy of each plain container's contents."""
    seen = {}
    for module in _watched_modules():
        owners = [module] + [value for value in vars(module).values()
                             if isinstance(value, type)
                             and value.__module__ == module.__name__]
        for owner in owners:
            for name, value in list(vars(owner).items()):
                if not CONSTANT.match(name):
                    continue
                contents = None
                if type(value) in _CONTAINERS and name not in RESET_BY_SIMULATOR:
                    try:
                        contents = copy.deepcopy(value)
                    except Exception:                       # noqa: BLE001
                        contents = None
                seen[(owner, name)] = (value, contents)
    return seen


def _restore(before: dict) -> list[str]:
    """Put back what a section changed; name each thing that needed it."""
    changed = []
    after = _constants()
    for key, (value, contents) in before.items():
        owner, name = key
        label = f"{getattr(owner, '__name__', owner)}.{name}"
        now = after.get(key, (None, None))[0]
        if now is not value:
            setattr(owner, name, value)
            changed.append(label)
        elif contents is not None and value != contents:
            value.clear()
            if isinstance(value, dict):
                value.update(contents)
            elif isinstance(value, list):
                value.extend(contents)
            else:
                value.update(contents)
            changed.append(label + " (contents)")
    # Only on modules and classes that were there before: a module first
    # imported during the section brings constants of its own.
    owners = {id(owner) for owner, _ in before}
    for key in after.keys() - before.keys():
        owner, name = key
        if id(owner) in owners:
            try:
                delattr(owner, name)
                changed.append(f"{getattr(owner, '__name__', owner)}.{name} (added)")
            except AttributeError:
                pass
    return changed


_CLOCKS = {name: getattr(time, name) for name in ("time", "monotonic", "sleep")}


def _restore_clocks() -> list[str]:
    changed = []
    for name, real in _CLOCKS.items():
        if getattr(time, name) is not real:
            setattr(time, name, real)
            changed.append(f"time.{name}")
    return changed


def _close_leftovers() -> list[str]:
    """Stop every sampler, run engine, supervisor and chart recorder still
    running. A sampler left behind would keep reading the simulator's port
    and take the next section's replies; an engine or supervisor would go
    on commanding the simulated pump under the next section's feet."""
    from lab import alarms, history, registry, runs
    closed = []
    gc.collect()
    for obj in gc.get_objects():
        try:
            thread = getattr(obj, "_thread", None)
            if not isinstance(obj, (runs.RunEngine, alarms.Supervisor,
                                    history.History, registry.DeviceService)):
                continue
        except Exception:                                   # noqa: BLE001
            continue
        helpers = [getattr(obj, name, None) for name in (
            "_shutdown_thread", "_secure_thread", "_orphan_thread",
            "_recovery_thread")]
        alive = [t for t in [thread, *helpers]
                 if isinstance(t, threading.Thread) and t.is_alive()]
        if not alive:
            continue
        names = ", ".join(t.name for t in alive)
        try:
            if isinstance(obj, runs.RunEngine):
                obj.close(timeout=20)
                closed.append(f"a run engine's threads ({names})")
            elif isinstance(obj, alarms.Supervisor):
                obj.close(timeout=5)
                closed.append(f"a supervisor's threads ({names})")
            elif isinstance(obj, history.History):
                obj.close()
                closed.append("a chart recorder")
            elif isinstance(obj, registry.DeviceService):
                obj.close()
                if thread is not None:
                    thread.join(timeout=5)
                closed.append(f"the {obj.key} sampler on {obj.port}")
        except Exception as exc:                            # noqa: BLE001
            closed.append(f"{type(obj).__name__} (closing it failed: {exc})")
    return closed


def run_section(name: str, ports: dict, tally: dict) -> None:
    import importlib
    from tools import fake_tc600, fake_tpg
    from tools.checks import common

    module_name, function, wants = WHERE[name]

    fake_tc600.reset()
    fake_tpg.reset()
    before = _constants()
    passed, failed = common.PASSED, common.FAILED
    common.SECTION = name
    started = time.monotonic()
    raised = False
    try:
        test = getattr(importlib.import_module(f"tools.checks.{module_name}"),
                       function)
        test(*(ports[w] for w in wants))
    except Exception as exc:                                # noqa: BLE001
        raised = True
        traceback.print_exc()
        common.check(f"the {name} section ran to its end", False,
                     f"{type(exc).__name__}: {exc}"[:150])
    finally:
        notes = _restore_clocks() + _restore(before)
        leftovers = _close_leftovers()
    # After an exception, what it left half-done is no surprise; otherwise
    # a section that leaves the rig's code changed, or something running,
    # can make a later section pass or fail for the wrong reason.
    if raised:
        for what, items in (("changed", notes), ("running", leftovers)):
            if items:
                print(f"  note  the {name} section left {what}, put back: "
                      + ", ".join(items))
    else:
        common.check(f"the {name} section put back every constant and clock "
                     f"it changed", not notes, ", ".join(notes)[:150])
        common.check(f"the {name} section left nothing running", not leftovers,
                     ", ".join(leftovers)[:150])
    common.SECTION = ""
    tally[name] = (common.PASSED - passed, common.FAILED - failed,
                   time.monotonic() - started)


def spawn_simulators():
    from tools import fake_tc600, fake_tpg
    from tools.checks.common import spawn

    pump_master, pump_slave = os.openpty()
    gauge_master, gauge_slave = os.openpty()
    stop = threading.Event()
    threads = [spawn(fake_tc600.serve, pump_master, stop, "pump"),
               spawn(fake_tpg.serve, gauge_master, stop, "gauge")]
    time.sleep(0.3)
    ports = {"pump": os.ttyname(pump_slave), "gauge": os.ttyname(gauge_slave)}
    print(f"Simulated TC 600 on {ports['pump']}")
    print(f"Simulated TPG    on {ports['gauge']}\n")

    def close():
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        os.close(pump_slave)
        os.close(gauge_slave)
    return ports, close


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=SECTIONS,
                        help="run only this section (repeatable)")
    parser.add_argument("--coverage", nargs="?", const="", metavar="FILE",
                        help="report the lines of lab/, tc600/ and tpg/ "
                             "that never ran; the full list goes to FILE")
    args = parser.parse_args(argv)
    # In step with the warnings on stderr, and readable while it runs.
    sys.stdout.reconfigure(line_buffering=True)
    chosen = [s for s in SECTIONS if s in set(args.only or SECTIONS)]

    coverage = None
    if args.coverage is not None:
        from tools.checks.coverage import Coverage
        coverage = Coverage(ROOT)
        if not coverage.start():
            coverage = None

    from tools.checks import common

    ports, close = spawn_simulators()
    tally: dict[str, tuple[int, int, float]] = {}
    try:
        for name in chosen:
            run_section(name, ports, tally)
    finally:
        close()
        if coverage:
            coverage.stop()

    print("\nBy section")
    for name in chosen:
        passed, failed, took = tally.get(name, (0, 0, 0.0))
        mark = "FAIL" if failed else "ok  "
        print(f"  {mark}  {name:13s} {passed:4d} passed  {failed:3d} failed"
              f"  {took:6.1f} s")
    if common.FAILURES:
        print("\nFailed checks")
        for label in common.FAILURES:
            print(f"  - {label}")
    if coverage:
        out = args.coverage or os.path.join(tempfile.gettempdir(),
                                            "selftest-coverage.txt")
        coverage.report(out)

    print(f"\n{common.PASSED} passed, {common.FAILED} failed")
    return 1 if common.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
