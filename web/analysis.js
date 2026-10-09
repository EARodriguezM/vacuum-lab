// Turning recorded samples into the quantities a vacuum scientist reads.
//
// Everything here is pure: samples in, numbers out, no network, no DOM. The
// charts draw what this computes, and the same functions serve the rolling
// history, a live run and a stored one, because all three hand over rows with
// the same columns.

const finite = (value) => typeof value === "number" && Number.isFinite(value);

/** History rows ({fields, rows}) into sample objects with `t` = wall clock. */
export function historySamples(payload) {
  if (!payload?.fields || !payload?.rows) return [];
  const { fields, rows } = payload;
  return rows.map((row) => {
    const sample = {};
    fields.forEach((name, index) => { sample[name] = row[index]; });
    sample.t = sample.at;
    return sample;
  });
}

/** Points for one column: [x, y] pairs, nulls kept so lines break there. */
export function column(samples, key, { positive = false, xKey = "t" } = {}) {
  return samples.map((s) => {
    const value = s[key];
    const usable = finite(value) && (!positive || value > 0);
    return [s[xKey], usable ? value : null];
  });
}

/**
 * The TMS temperature as points, with 0 read as "not measured".
 *
 * The drive reports 0 while the TMS heater is off, and firmware 017500 has
 * no other temperature sensor -- so on this rig a 0 is the absence of a
 * reading, never a pump at freezing point.
 */
export function temperaturePoints(samples) {
  return samples.map((s) => [s.t, finite(s.tms_temp) && s.tms_temp > 0 ? s.tms_temp : null]);
}

/** Least-squares p = a + b t, with R². Same maths as lab/runs.py. */
export function linearFit(points) {
  const usable = points.filter(([a, b]) => finite(a) && finite(b));
  const n = usable.length;
  if (n < 3) return null;
  let sumX = 0;
  let sumY = 0;
  for (const [a, b] of usable) { sumX += a; sumY += b; }
  const meanX = sumX / n;
  const meanY = sumY / n;
  let variance = 0;
  let covariance = 0;
  for (const [a, b] of usable) {
    variance += (a - meanX) ** 2;
    covariance += (a - meanX) * (b - meanY);
  }
  if (variance === 0) return null;
  const slope = covariance / variance;
  const intercept = meanY - slope * meanX;
  let total = 0;
  let residual = 0;
  for (const [a, b] of usable) {
    total += (b - meanY) ** 2;
    residual += (b - (intercept + slope * a)) ** 2;
  }
  return { slope, intercept, r2: total > 0 ? 1 - residual / total : 1, n };
}

/**
 * Fractional rate of change of pressure, %/min, over a trailing window.
 *
 * A fit of ln(p) against t, exactly what the run engine's base-pressure
 * judgement uses (`relative_rate` in lab/runs.py): d(ln p)/dt is (dp/dt)/p,
 * a rate that means the same thing at 1e-3 mbar and at 1e-7. Negative is
 * falling. Running sums keep it linear in the number of samples.
 */
export function fractionalRate(samples, windowS = 120, key = "p_best") {
  const out = [];
  const window = [];
  let head = 0;
  for (const sample of samples) {
    const t = sample.t;
    const p = sample[key];
    const usable = finite(p) && p > 0 && finite(t);
    if (usable) window.push([t, Math.log(p)]);
    while (head < window.length && t - window[head][0] > windowS) head++;
    let rate = null;
    const n = window.length - head;
    // Times are taken relative to the window's first sample: epoch seconds
    // squared would lose every significant digit a two-minute window has.
    if (usable && n >= 5 && window[window.length - 1][0] - window[head][0] >= windowS * 0.4) {
      const t0 = window[head][0];
      let sx = 0;
      let sy = 0;
      let sxx = 0;
      let sxy = 0;
      for (let i = head; i < window.length; i++) {
        const dt = window[i][0] - t0;
        sx += dt; sy += window[i][1]; sxx += dt * dt; sxy += dt * window[i][1];
      }
      const variance = sxx - (sx * sx) / n;
      if (variance > 0) rate = ((sxy - (sx * sy) / n) / variance) * 60 * 100;
    }
    out.push({ t, rate });
  }
  return out;
}

/** The fractional rate split by direction, for a log-magnitude chart. */
export function rateSeries(rates) {
  const falling = [];
  const rising = [];
  for (const { t, rate } of rates) {
    if (!finite(rate) || rate === 0) {
      falling.push([t, null]);
      rising.push([t, null]);
    } else if (rate < 0) {
      falling.push([t, -rate]);
      rising.push([t, null]);
    } else {
      falling.push([t, null]);
      rising.push([t, rate]);
    }
  }
  return { falling, rising };
}

/**
 * Local slope n of a pump-down on log-log axes: p ∝ t^-n.
 *
 * The exponent is the diagnosis. Once the volume gas is gone, n ≈ 1 is
 * surface desorption -- water, in an unbaked chamber -- n ≈ 0.5 is gas
 * diffusing out of the bulk (elastomer seals, polymers), and n → 0 is a
 * floor: a leak, permeation, or the pump's own limit. Fitted over a sliding
 * window of a fixed width in log-time so early and late points weigh alike.
 */
export function loglogSlope(points, halfWidth = 0.12) {
  const usable = points.filter(([t, p]) => finite(t) && t > 0 && finite(p) && p > 0)
    .map(([t, p]) => [Math.log10(t), Math.log10(p), t]);
  const out = [];
  let start = 0;
  let end = 0;
  for (let i = 0; i < usable.length; i++) {
    const centre = usable[i][0];
    while (usable[start][0] < centre - halfWidth) start++;
    while (end < usable.length && usable[end][0] <= centre + halfWidth) end++;
    const window = usable.slice(start, end);
    if (window.length < 5) { out.push([usable[i][2], null]); continue; }
    const fit = linearFit(window.map(([a, b]) => [a, b]));
    out.push([usable[i][2], fit ? -fit.slope : null]);
  }
  return out;
}

/**
 * The exponent n of p ∝ t^-n fitted over the last decade of time.
 *
 * One straight-line fit in log-log space from t_end/10 to t_end. A median of
 * local slopes would be dominated by the final minutes -- samples are evenly
 * spaced in time, so most of them sit in the last decade's last stretch --
 * and the last decade is the regime the chamber has settled into.
 */
export function lastDecadeSlope(points) {
  const usable = points.filter(([t, p]) => finite(t) && t > 0 && finite(p) && p > 0);
  if (usable.length < 10) return null;
  const end = usable[usable.length - 1][0];
  const decade = usable.filter(([t]) => t >= end / 10)
    .map(([t, p]) => [Math.log10(t), Math.log10(p)]);
  if (decade.length < 8) return null;
  const fit = linearFit(decade);
  return fit ? { n: -fit.slope, r2: fit.r2, from: end / 10, to: end } : null;
}

/** min / max / geometric mean / spread for a pressure window. */
export function pressureStats(values) {
  const usable = values.filter((v) => finite(v) && v > 0);
  if (!usable.length) return null;
  const logs = usable.map(Math.log10);
  const mean = logs.reduce((a, b) => a + b, 0) / logs.length;
  const variance = logs.reduce((a, b) => a + (b - mean) ** 2, 0) / logs.length;
  return {
    count: usable.length,
    min: Math.min(...usable),
    max: Math.max(...usable),
    last: usable[usable.length - 1],
    geomean: 10 ** mean,
    // One standard deviation of log10 p, as a percentage: the scatter of
    // the reading around its own trend, i.e. the gauge's noise when flat.
    spreadPct: (10 ** Math.sqrt(variance) - 1) * 100,
    decades: Math.log10(Math.max(...usable) / Math.min(...usable)),
  };
}

export function stats(values) {
  const usable = values.filter(finite);
  if (!usable.length) return null;
  const mean = usable.reduce((a, b) => a + b, 0) / usable.length;
  return { count: usable.length, min: Math.min(...usable), max: Math.max(...usable),
           mean, last: usable[usable.length - 1] };
}

/** Consecutive runs of the same phase, as x-bands. */
export function phaseSpans(samples, label = (phase) => phase) {
  const spans = [];
  for (const sample of samples) {
    const phase = sample.phase || "";
    if (!phase) continue;
    const last = spans[spans.length - 1];
    if (last && last.phase === phase) {
      last.to = sample.t;
    } else {
      if (last) last.to = sample.t;
      spans.push({ phase, from: sample.t, to: sample.t, label: label(phase) });
    }
  }
  return spans;
}

/** Spans where a predicate holds, for a state strip. */
export function spansWhere(samples, classify) {
  const spans = [];
  let current = null;
  for (let i = 0; i < samples.length; i++) {
    const sample = samples[i];
    const state = classify(sample);
    const next = samples[i + 1];
    const end = next ? next.t : sample.t;
    if (!state) { current = null; continue; }
    if (current && current.key === state.key) {
      current.to = end;
    } else {
      current = { ...state, from: sample.t, to: end };
      spans.push(current);
    }
  }
  return spans;
}

/** Status codes 1-6: the gauge saying why its number means nothing. */
export const GAUGE_STATES = {
  1: { key: "under", label: "underrange", tone: "warn" },
  2: { key: "over", label: "overrange", tone: "warn" },
  3: { key: "error", label: "sensor error", tone: "crit" },
  4: { key: "off", label: "sensor off", tone: "idle" },
  5: { key: "none", label: "no sensor", tone: "idle" },
  6: { key: "ident", label: "identification error", tone: "crit" },
};

export function gaugeStrip(samples, channel) {
  return spansWhere(samples, (s) => {
    const status = s[`p${channel}_status`];
    if (!finite(status)) return { key: "nodata", label: "no contact", tone: "idle" };
    if (status === 0) return null;
    return GAUGE_STATES[status] || { key: `s${status}`, label: `status ${status}`, tone: "crit" };
  });
}

/**
 * The pump's operating state for every sample, for the load curve.
 *
 * Three states, because a point's meaning depends on which it is in:
 *   full speed      the drive is holding the rotor where it was asked to;
 *                   its power is friction plus whatever gas it is pumping
 *   running up      the drive is applying its maximum to accelerate; the
 *                   power says nothing about gas load
 *   held down       below full speed and not gaining: the gas load needs more
 *                   power than the drive may give, so the rotor sits where
 *                   the two balance -- the state that tripped the pump on
 *                   2026-10-08
 */
export function loadState(samples, nominal) {
  const states = [];
  for (let i = 0; i < samples.length; i++) {
    const s = samples[i];
    if (!finite(s.speed) || !finite(s.power) || s.power <= 0 || !s.motor) {
      states.push(null);
      continue;
    }
    if (nominal && s.speed >= 0.95 * nominal) { states.push("full"); continue; }
    // Rising over the last ~10 s means running up; otherwise held down.
    let j = i - 1;
    while (j > 0 && finite(samples[j].t) && s.t - samples[j].t < 8) j--;
    const before = j >= 0 ? samples[j] : null;
    const gaining = before && finite(before.speed) && s.speed > before.speed + 2;
    states.push(gaining ? "runup" : "held");
  }
  return states;
}

export const LOAD_STATES = {
  full: { label: "at full speed", color: "--series-1" },
  held: { label: "held below full speed by gas load", color: "--series-2" },
  runup: { label: "running up", color: "--series-3" },
};

/** Run-up time, from motor-on with the rotor at rest to 95 % of nominal. */
export function runupTime(samples, nominal) {
  if (!nominal) return null;
  for (let i = 1; i < samples.length; i++) {
    const before = samples[i - 1];
    const s = samples[i];
    const start = !before.motor && s.motor;
    if (!start || !finite(s.speed) || s.speed > 0.05 * nominal) continue;
    for (let j = i; j < samples.length; j++) {
      if (!samples[j].motor) break;
      if (finite(samples[j].speed) && samples[j].speed >= 0.95 * nominal) {
        return { seconds: samples[j].t - s.t, from: s.t, to: samples[j].t };
      }
    }
  }
  return null;
}

/** Run-log entries as chart markers, at seconds since the run started. */
export function eventMarkers(events, startedAt, { relative = true } = {}) {
  const wanted = /turbopump started|shutter|plasma|deposition|aborted|fault|over temperature|base pressure|working pressure|power-down|operator|made safe|recovery|rotor has fallen/i;
  return (events || [])
    .filter((e) => e.level === "error" || e.level === "warning" || wanted.test(e.message))
    .filter((e) => !/^Phase:/.test(e.message))
    .map((e) => ({
      x: relative ? e.at - startedAt : e.at,
      label: e.message,
      level: e.level,
    }));
}

/**
 * When the turbopump was started from rest during these samples, or null.
 *
 * Only a real start counts: a run that begins with the rotor already
 * spinning (run 26 began at 851 Hz) has no moment to align a pump-down on,
 * and pretending the first sample is one compares it with nothing.
 */
export function turboStart(samples, nominal = 1500) {
  for (let i = 1; i < samples.length; i++) {
    const s = samples[i];
    if (s.motor && !samples[i - 1].motor
        && (!finite(s.speed) || s.speed <= 0.05 * (nominal || 1500))) {
      return s.t;
    }
  }
  return null;
}

const NOT_PUMPING = new Set(["backfill", "presputter", "deposit", "recover"]);

/**
 * The pump-down part of a run, as [seconds since turbo start, pressure].
 *
 * Ends where the chamber stopped being pumped down: the argon backfill of a
 * deposition, or any rise to three times the lowest pressure seen so far --
 * gas admitted by hand, a burst, a valve -- because after that the curve
 * describes the gas coming in, not the chamber giving it up.
 */
export function pumpdownCurve(samples, nominal) {
  const start = turboStart(samples, nominal);
  if (start === null) return { start: null, points: [] };
  const points = [];
  let lowest = Infinity;
  for (const s of samples) {
    if (s.t <= start) continue;
    if (NOT_PUMPING.has(s.phase)) break;
    const p = s.p_best;
    if (!finite(p) || p <= 0) continue;
    if (p > lowest * 3 && points.length > 10) break;
    lowest = Math.min(lowest, p);
    points.push([s.t - start, p]);
  }
  return { start, points };
}

/**
 * Scatter of log p about its own straight-line trend over the last
 * `seconds`, as a percentage: what the gauge adds to a steady reading.
 * A drifting or falling pressure does not count as noise.
 */
export function pressureNoise(samples, seconds = 300) {
  const end = samples.length ? samples[samples.length - 1].t : null;
  if (end === null) return null;
  const recent = samples.filter((s) => s.t >= end - seconds && finite(s.p_best)
                                       && s.p_best > 0)
    .map((s) => [s.t - end, Math.log10(s.p_best)]);
  const fit = linearFit(recent);
  if (!fit || recent.length < 10) return null;
  const residuals = recent.map(([t, y]) => y - (fit.intercept + fit.slope * t));
  const variance = residuals.reduce((a, r) => a + r * r, 0) / residuals.length;
  return (10 ** Math.sqrt(variance) - 1) * 100;
}
