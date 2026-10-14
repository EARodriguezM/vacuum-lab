"""Health indicators per run, for watching the rig age across months.

A single run says how the chamber and the pump behaved that day. Laid side
by side, the same few numbers say whether either is wearing out -- which is
the question nobody can answer from one curve:

    base_mbar      how good the chamber gets            (chamber: leaks, water)
    time_to_base_s how long it takes to get there
    runup_s        rest to full speed, motor on         (pump: bearings, supply)
    friction_w     drive power at full speed with no
                   gas to pump -- bearing and windage
                   loss, which creeps up as the
                   bearings wear
    sag_v          supply voltage lost under the run-up
                   load (the supply and its cable)
    held_s         time the gas load held the rotor
                   below full speed (process: too much
                   gas for the pump)

Every one is measured from the stored curve rather than taken from the run's
summary, so runs recorded before the summary carried it still have it, and a
summary that was wrong does not propagate: run 26 of 2026-10-08 recorded a
"base pressure" of 3.4e-2 mbar with the rotor dragged down to 1160 Hz, and
`base_valid` is how that reading is kept off the trend.

Finished runs never change, so each is computed once and cached by its id
and sample count. A speed that did not come back is unknown, never 0 Hz: it
neither starts a run-up from rest nor counts as time held down.
"""

from __future__ import annotations

import statistics
import threading

from . import judgement

#: Below this pressure the gas load on the rotor is negligible, so the
#: drive's power at full speed is its own friction and windage.
NO_GAS_MBAR = 1.0e-4

#: The rotor counts as at full speed from this fraction of nominal: the run
#: engine's own threshold (lab/judgement.py).
FULL_SPEED = judgement.FULL_SPEED_FRACTION

_cache: dict[tuple[int, int], dict] = {}
_lock = threading.Lock()


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def metrics(samples: list[dict], nominal: float | None) -> dict:
    """The health indicators of one run, from its samples."""
    out: dict = {"runup_s": None, "friction_w": None, "sag_v": None,
                 "min_voltage": None, "held_s": None, "peak_power_w": None}
    if not samples:
        return out
    if not nominal:
        speeds = [v for s in samples if (v := _number(s.get("speed"))) is not None]
        nominal = max(speeds) if max(speeds, default=0) >= 300 else None

    # Run-up: motor switched on with the rotor at rest, to full speed.
    if nominal:
        for index in range(1, len(samples)):
            before, sample = samples[index - 1], samples[index]
            speed = _number(sample.get("speed"))
            if (before.get("motor") or not sample.get("motor") or speed is None
                    or speed > 0.05 * nominal):
                continue
            for later in samples[index:]:
                if not later.get("motor"):
                    break
                if judgement.at_full_speed(_number(later.get("speed")), nominal):
                    out["runup_s"] = round(later["t"] - sample["t"], 1)
                    break
            if out["runup_s"] is not None:
                break

    # Friction: power at full speed with nothing to pump.
    if nominal:
        quiet = [_number(s.get("power")) for s in samples
                 if judgement.at_full_speed(_number(s.get("speed")), nominal)
                 and isinstance(s.get("p_best"), (int, float))
                 and 0 < s["p_best"] < NO_GAS_MBAR
                 and (_number(s.get("power")) or 0) > 0]
        if len(quiet) >= 5:
            out["friction_w"] = round(statistics.median(quiet), 1)

    # Supply: idle voltage minus the lowest under load.
    idle = [_number(s.get("voltage")) for s in samples
            if (_number(s.get("power")) or 0) == 0 and _number(s.get("voltage"))]
    loaded = [_number(s.get("voltage")) for s in samples
              if (_number(s.get("power")) or 0) > 0 and _number(s.get("voltage"))]
    if loaded:
        out["min_voltage"] = min(loaded)
    if idle and loaded:
        out["sag_v"] = round(statistics.median(idle) - min(loaded), 2)

    powers = [_number(s.get("power")) for s in samples if _number(s.get("power"))]
    if powers:
        out["peak_power_w"] = max(powers)

    # Held down: motor on, below full speed, after full speed was reached.
    if nominal:
        reached = False
        held = 0.0
        for before, sample in zip(samples, samples[1:]):
            speed = _number(sample.get("speed"))
            motor = sample.get("motor")
            if speed is None or motor is None:
                continue                    # not read: neither held nor not
            if not motor:
                reached = False
                continue
            if judgement.at_full_speed(speed, nominal):
                reached = True
            elif reached and (_number(sample.get("power")) or 0) > 0:
                held += max(0.0, sample["t"] - before["t"])
        out["held_s"] = round(held, 1)
    return out


def base_valid(samples: list[dict], result: dict, nominal: float | None) -> bool | None:
    """Whether a recorded base pressure was taken with the rotor at full speed."""
    at = (result or {}).get("time_to_base_s")
    if not isinstance(at, (int, float)) or not samples:
        return None
    read = [s for s in samples if _number(s.get("speed")) is not None]
    if not nominal or not read:
        return None
    nearest = min(read, key=lambda s: abs(s["t"] - at))
    return judgement.at_full_speed(_number(nearest.get("speed")), nominal)


def trends(database, nominal: float | None = None, limit: int = 500) -> list[dict]:
    """One row per run, oldest first, with its result and its health."""
    rows = []
    for run in reversed(database.runs(limit=limit)):
        if run["status"] == "running":
            continue
        count = run.get("sample_count") or 0
        key = (run["id"], count)
        with _lock:
            cached = _cache.get(key)
        if cached is None:
            samples = database.samples(run["id"])
            result = run.get("result") or {}
            cached = metrics(samples, nominal)
            cached["base_valid"] = base_valid(samples, result, nominal)
            with _lock:
                _cache[key] = cached
        result = run.get("result") or {}
        rows.append({
            "id": run["id"], "name": run["name"], "procedure": run["procedure"],
            "status": run["status"], "started_at": run["started_at"],
            "samples": count,
            "base_mbar": result.get("base_mbar"),
            "time_to_base_s": result.get("time_to_base_s"),
            "time_to_crossover_s": result.get("time_to_crossover_s"),
            "ultimate_mbar": result.get("ultimate_mbar"),
            "leak_rate_mbar_l_s": result.get("leak_rate_mbar_l_s"),
            **cached,
        })
    return rows
