"""The numbers a pressure curve and a rotor are judged by -- kept once.

The run engine decides with these; the per-run trends measure with them;
the panel draws with them. Each used to keep its own copy, and they had
drifted: 95 % of nominal written out in four places, a rate the engine
fitted from three points where the panel waited for five spanning 40 % of
the window, a missing speed read as 0 Hz by the trends and skipped by the
panel. So the Dashboard could say "still falling" while the engine had
called base pressure, from the same samples.

`published()` is what `/api/state` sends the panel as `judgement`;
`web/analysis.js` starts from the same numbers (`JUDGEMENT`) until the first
poll, and the selftest checks the two copies agree and runs both the Python
and the JavaScript against `tools/checks/analysis_vectors.json`.
"""

from __future__ import annotations

import math

#: The rotor counts as at full speed from this fraction of its nominal speed.
FULL_SPEED_FRACTION = 0.95
#: Below this fraction, after full speed was reached with the motor on, a run
#: ends at once: run 25 tripped Err006 five seconds after crossing 80 %.
PROTECT_FRACTION = 0.85
#: A rate-of-rise curve straighter than this is a leak rate; anything bent is
#: an outgassing transient.
LINEAR_R2 = 0.95
#: The trailing window the fractional rate of change is fitted over.
RATE_WINDOW_S = 120.0
#: A rate needs at least this many readings ...
RATE_MIN_POINTS = 5
#: ... spanning at least this share of the window: three readings two
#: seconds apart are the gauge's noise, not the chamber's rate.
RATE_MIN_SPAN = 0.4


def published() -> dict:
    """The thresholds, as the panel receives them."""
    return {
        "full_speed_fraction": FULL_SPEED_FRACTION,
        "protect_fraction": PROTECT_FRACTION,
        "linear_r2": LINEAR_R2,
        "rate_window_s": RATE_WINDOW_S,
        "rate_min_points": RATE_MIN_POINTS,
        "rate_min_span": RATE_MIN_SPAN,
    }


def at_full_speed(speed, nominal) -> bool | None:
    """Whether the rotor is at full speed; None when either is unknown.

    A speed that did not come back is unknown, never 0 Hz.
    """
    if not isinstance(speed, (int, float)) or not isinstance(nominal, (int, float)) \
            or nominal <= 0:
        return None
    return speed >= FULL_SPEED_FRACTION * nominal


def linear_fit(points: list[tuple[float, float]]) -> dict | None:
    """Least-squares fit of p = a + b*t, with the coefficient of determination.

    R² is reported because a rate-of-rise measurement is only meaningful when
    the rise is linear: a curve that bends is still outgassing or still
    settling, and its slope is not a leak rate.
    """
    n = len(points)
    if n < 3:
        return None
    sum_t = sum(t for t, _ in points)
    sum_p = sum(p for _, p in points)
    mean_t, mean_p = sum_t / n, sum_p / n

    variance = sum((t - mean_t) ** 2 for t, _ in points)
    if variance == 0:
        return None
    covariance = sum((t - mean_t) * (p - mean_p) for t, p in points)
    slope = covariance / variance
    intercept = mean_p - slope * mean_t

    total = sum((p - mean_p) ** 2 for _, p in points)
    residual = sum((p - (intercept + slope * t)) ** 2 for t, p in points)
    r_squared = 1.0 - residual / total if total > 0 else 1.0
    return {"slope": slope, "intercept": intercept, "r_squared": r_squared,
            "points": n}


def relative_rate(points: list[tuple[float, float]],
                  window_s: float | None = None) -> float | None:
    """How fast a pressure is changing, in percent per minute.

    Fits ln(p) against time rather than p, because d(ln p)/dt is exactly
    (dp/dt)/p -- a fractional rate. On a quantity that spans decades that is
    the only rate worth quoting: 1e-4 mbar falling by 1e-5 mbar/min and 1e-6
    falling by 1e-7 mbar/min are the same process at different depths, and an
    absolute threshold would call one "still pumping" and the other "flat".

    Negative means falling. None until there are `RATE_MIN_POINTS` readings
    and, given the window they were taken from, until they span
    `RATE_MIN_SPAN` of it.
    """
    usable = [(t, math.log(p)) for t, p in points
              if isinstance(p, (int, float)) and p > 0]
    if len(usable) < RATE_MIN_POINTS:
        return None
    if window_s and usable[-1][0] - usable[0][0] < RATE_MIN_SPAN * window_s:
        return None
    fit = linear_fit(usable)
    if fit is None:
        return None
    return fit["slope"] * 60.0 * 100.0
