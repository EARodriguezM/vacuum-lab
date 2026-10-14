"""The analysis maths the run engine judges a curve by -- and the panel draws
with: both run the same vectors (analysis_vectors.json), and the panel's
copy of the thresholds must be the server's."""

from __future__ import annotations

import json
import math
import os
import re

from lab import judgement, runs, trends
from tools.checks.common import (
    ROOT, check)

VECTORS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "analysis_vectors.json")


def load_vectors() -> dict:
    with open(VECTORS, encoding="utf-8") as fh:
        return json.load(fh)


def _close(value, expected, tolerance) -> bool:
    if expected is None or value is None:
        return value is None and expected is None
    return abs(value - expected) <= tolerance * max(1.0, abs(expected))


def panel_defaults() -> dict:
    """`JUDGEMENT` as written in web/analysis.js, read from its source."""
    with open(os.path.join(ROOT, "web", "analysis.js"), encoding="utf-8") as fh:
        source = fh.read()
    block = re.search(r"export const JUDGEMENT = \{(.*?)\};", source, re.S)
    if not block:
        return {}
    return {key: float(value) for key, value in
            re.findall(r"(\w+):\s*([0-9.eE+-]+)", block.group(1))}


def test_analysis() -> None:
    print("\nAnalysis")
    # A perfect straight line: slope must come back exactly, R² must be 1.
    points = [(t, 1.0e-6 + 2.0e-8 * t) for t in range(0, 100, 5)]
    fit = runs.linear_fit(points)
    check("linear_fit recovers the slope",
          abs(fit["slope"] - 2.0e-8) < 1e-14, f"{fit['slope']:.3E}")
    check("linear_fit reports R2 = 1", abs(fit["r_squared"] - 1.0) < 1e-9)
    check("linear_fit refuses too few points", runs.linear_fit([(0, 1)]) is None)

    # Fractional rate: the base-pressure detector's whole basis. An
    # exponential decay with time constant tau has a constant relative rate
    # of -100/tau percent per second, whatever the absolute pressure.
    decay = [(t, 1.0e-6 * math.exp(-t / 400.0)) for t in range(0, 300, 10)]
    rate = runs.relative_rate(decay)
    check("relative_rate reads a decade-independent rate",
          abs(rate - (-60.0 * 100.0 / 400.0)) < 1e-6, f"{rate:.3f} %/min")
    deep = [(t, 1.0e-9 * math.exp(-t / 400.0)) for t in range(0, 300, 10)]
    check("the same shape three decades lower gives the same rate",
          abs(runs.relative_rate(deep) - rate) < 1e-6)
    check("a flat curve reads as zero rate",
          abs(runs.relative_rate([(t, 1e-6) for t in range(0, 300, 10)])) < 1e-9)

    stats = runs.statistics([3.0, 1.0, 2.0])
    check("statistics min/max/mean",
          stats["min"] == 1.0 and stats["max"] == 3.0 and stats["mean"] == 2.0)
    check("statistics keeps order for first/last",
          stats["first"] == 3.0 and stats["last"] == 2.0)

    # --- one copy of the thresholds, and one set of answers ------------------
    vectors = load_vectors()
    tolerance = vectors["tolerance"]
    for case in vectors["rate"]:
        points = [(t, p) for t, p in case["points"]]
        got = judgement.relative_rate(points, case["window_s"])
        check(f"rate vector: {case['name']}",
              _close(got, case["expected"], tolerance),
              f"{got} against {case['expected']}")
    full = [judgement.at_full_speed(c["speed"], c["nominal"])
            for c in vectors["full_speed"]]
    check("full-speed vectors: 95 % is full, a missing speed is unknown",
          full == [c["expected"] for c in vectors["full_speed"]], str(full))
    for case in vectors["linear_fit"]:
        got = judgement.linear_fit([tuple(p) for p in case["points"]])
        ok = (got is None if case["slope"] is None else
              got is not None and _close(got["slope"], case["slope"], tolerance)
              and _close(got["intercept"], case["intercept"], tolerance)
              and _close(got["r_squared"], case["r_squared"], tolerance))
        check(f"fit vector: {case['name']}", ok, str(got)[:80])

    published = {k: float(v) for k, v in judgement.published().items()}
    defaults = panel_defaults()
    check("the panel's thresholds before the first poll are the server's",
          defaults == published,
          "same" if defaults == published else f"panel {defaults}")
    check("the engine judges with the shared thresholds",
          runs.VacuumSequence.FULL_SPEED_FRACTION == judgement.FULL_SPEED_FRACTION
          and runs.VacuumSequence.PROTECT_FRACTION == judgement.PROTECT_FRACTION
          and runs.VacuumSequence.RATE_WINDOW_S == judgement.RATE_WINDOW_S
          and trends.FULL_SPEED == judgement.FULL_SPEED_FRACTION)

    # --- a speed that did not come back is not 0 Hz, in the trends either ----
    held = [{"t": float(t), "speed": 1500.0, "motor": 1, "power": 20.0}
            for t in range(0, 60, 10)]
    held += [{"t": float(t), "speed": None, "motor": 1, "power": 20.0}
             for t in range(60, 300, 10)]
    held += [{"t": 300.0, "speed": 1500.0, "motor": 1, "power": 20.0}]
    measured = trends.metrics(held, 1500.0)
    check("lost speed readings are not counted as time held down",
          measured["held_s"] == 0.0, f"held {measured['held_s']} s")
    lost = [{"t": 0.0, "speed": 1500.0, "motor": 1},
            {"t": 100.0, "speed": None, "motor": 1},
            {"t": 130.0, "speed": 1490.0, "motor": 1}]
    check("a base pressure is judged by the nearest speed that was read",
          trends.base_valid(lost, {"time_to_base_s": 100.0}, 1500.0) is True)
    start = [{"t": 0.0, "speed": 300.0, "motor": 0},
             {"t": 2.0, "speed": None, "motor": 1},
             {"t": 60.0, "speed": 1450.0, "motor": 1}]
    check("an unread speed at motor-on is not a run-up from rest",
          trends.metrics(start, 1500.0)["runup_s"] is None,
          f"{trends.metrics(start, 1500.0)['runup_s']}")
