// The rig's charts, assembled: the same panels serve the rolling history on
// the pump and gauge pages, a run in progress, and a run read back from the
// database, because all three hand over rows with the same columns.
//
// The central form is the **stack**: chamber pressure, rotor speed, drive
// current, drive power and supply voltage, one chart each, on one time axis,
// with one crosshair. That is how the failure of 2026-10-08 reads at a
// glance -- the pressure steps up, and in the same second the rotor sags, the
// current rises to its limit and the supply voltage dips -- without ever
// putting two measures on one axis.
//
// Every limit drawn here is quoted from somewhere a reader can check: the
// interlocks come from the server, the pump's ratings from its manual
// (lab/pump.py RATINGS, PM 0504 BE section 9).

import { html } from "./html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { ChartPanel, DataTable, Legend, Plot } from "./chart.js";
import { api, joinPoll } from "./api.js";
import * as A from "./analysis.js";
import * as fmt from "./format.js";

const finite = (value) => typeof value === "number" && Number.isFinite(value);

// --- time windows and the rolling history -------------------------------------

export const WINDOWS = [
  { seconds: 900, label: "15 min" },
  { seconds: 3600, label: "1 h" },
  { seconds: 6 * 3600, label: "6 h" },
  { seconds: 24 * 3600, label: "24 h" },
];

/** A remembered choice, per browser. A convenience, so it may fail quietly. */
export function useStored(key, fallback) {
  const [value, setValue] = useState(() => {
    try {
      const stored = localStorage.getItem(key);
      return stored === null ? fallback : JSON.parse(stored);
    } catch {
      return fallback;
    }
  });
  const update = (next) => {
    setValue(next);
    try { localStorage.setItem(key, JSON.stringify(next)); } catch { /* private mode */ }
  };
  return [value, update];
}

/**
 * The rig's recent history, kept current.
 *
 * Loads the whole window once (decimated by the server, every extreme kept),
 * then asks only for rows newer than the last one it holds -- one row per
 * poll -- so watching a day of history costs the same as watching a minute.
 * Long windows are reloaded once a minute to stay decimated.
 *
 * It has no clock of its own: after the first load it rides the panel's
 * state poll (api.js joinPoll), so its rows and the instruments' state
 * arrive in the same redraw instead of two redraws out of step -- and it
 * stops when the poll stops, with the page hidden.
 */
export function useHistory(seconds, points = 1500) {
  const [state, setState] = useState({ samples: [], loading: true, error: null,
                                       now: null, oldest: null, interval: 2 });
  useEffect(() => {
    let cancelled = false;
    let samples = [];
    let newest = null;
    let lastFull = 0;
    let pending = null;

    const load = async () => {
      const reload = newest === null
        || (seconds >= 6 * 3600 && Date.now() - lastFull > 60000);
      const payload = reload
        ? await api.history(seconds, points)
        : await api.history(seconds, 2000, newest);
      return { payload, reload };
    };
    // One request at a time: the first load and the first poll can overlap.
    const fetch = () => {
      if (pending) return Promise.resolve({ skip: true });
      pending = load().finally(() => { pending = null; });
      return pending;
    };
    const apply = (result) => {
      if (cancelled || result.value?.skip) return;
      if (result.status === "rejected") {
        setState((s) => ({ ...s, loading: false,
                           error: result.reason?.message || String(result.reason) }));
        return;
      }
      const { payload, reload } = result.value;
      if (!payload.ok) {
        setState((s) => ({ ...s, loading: false,
                           error: payload.error || "no history on this server" }));
        return;
      }
      const rows = A.historySamples(payload);
      const before = samples;
      if (reload) {
        samples = rows;
        lastFull = Date.now();
      } else if (rows.length) {
        samples = samples.concat(rows);
      }
      if (rows.length) newest = rows[rows.length - 1].at;
      else if (newest === null) newest = payload.server_time;
      const now = payload.server_time ?? Date.now() / 1000;
      const kept = samples.filter((s) => s.t >= now - seconds);
      // Nothing new and nothing aged out: keep the same array, so nothing
      // that depends on it redraws.
      if (!reload && !rows.length && kept.length === before.length) return;
      samples = kept;
      setState({ samples, loading: false, error: null, now,
                 oldest: payload.oldest, interval: payload.interval_s || 2 });
    };

    fetch().then((value) => apply({ status: "fulfilled", value }),
                 (reason) => apply({ status: "rejected", reason }));
    const leave = joinPoll({ fetch, apply });
    return () => { cancelled = true; leave(); };
  }, [seconds, points]);
  return state;
}

export function WindowPicker({ value, onChange, options = WINDOWS }) {
  return html`
    <div class="segmented" role="group" aria-label="Time window">
      ${options.map((option) => html`
        <button key=${option.seconds} aria-pressed=${option.seconds === value ? "true" : "false"}
                onClick=${() => onChange(option.seconds)}>${option.label}</button>`)}
    </div>`;
}

/** Hand the plotted rows over as a CSV file, straight from the browser. */
export function downloadCsv(samples, name, columns = HISTORY_COLUMNS) {
  const head = ["iso_time", ...columns.map((c) => c.header)];
  const lines = [head.join(",")];
  for (const sample of samples) {
    const stamp = finite(sample.at) ? new Date(sample.at * 1000).toISOString() : "";
    lines.push([stamp, ...columns.map((c) => {
      const value = sample[c.key];
      return value === null || value === undefined ? "" : String(value);
    })].join(","));
  }
  const blob = new Blob([`${lines.join("\n")}\n`], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  document.body.appendChild(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(link.href); link.remove(); }, 0);
}

export const HISTORY_COLUMNS = [
  { key: "at", header: "timestamp" },
  { key: "p1", header: "p1_mbar" }, { key: "p1_status", header: "p1_status" },
  { key: "p2", header: "p2_mbar" }, { key: "p2_status", header: "p2_status" },
  { key: "p_best", header: "p_best_mbar" },
  { key: "speed", header: "speed_hz" }, { key: "current", header: "current_a" },
  { key: "voltage", header: "voltage_v" }, { key: "power", header: "power_w" },
  { key: "tms_temp", header: "tms_temp_c" }, { key: "station", header: "station" },
  { key: "motor", header: "motor" }, { key: "ov_temp_pump", header: "ov_temp_pump" },
  { key: "ov_temp_elec", header: "ov_temp_elec" }, { key: "error_code", header: "error_code" },
  { key: "gas_sccm", header: "argon_sccm" }, { key: "gate_pct", header: "gate_pct" },
  { key: "magnetron_w", header: "magnetron_w" }, { key: "shutter", header: "shutter_open" },
];

// --- what the limits are ------------------------------------------------------

/** The pump's limits for drawing, from the server's ratings table. */
export function pumpLimits(pump) {
  const ratings = pump?.ratings || {};
  const nominal = Number(pump?.identity?.NominalSpd) || null;
  const supply = ratings.supply_v ?? 24;
  const tolerance = ratings.supply_tolerance ?? 0.05;
  return {
    nominal,
    switchpoint: nominal ? nominal * (ratings.switchpoint_fraction ?? 0.8) : null,
    supplyLow: supply * (1 - tolerance),
    supplyHigh: supply * (1 + tolerance),
    supply,
    tolerancePct: Math.round(tolerance * 100),
    powerFull: ratings.power_line_full_w ?? 60,
    powerReduced: ratings.power_line_reduced_w ?? 80,
    reducedHz: ratings.power_line_reduced_hz ?? 1300,
    currentRated: ratings.current_continuous_a ?? null,
    startup: ratings.startup_s ?? null,
    model: ratings.model || "the turbopump",
    source: ratings.source || "",
  };
}

// --- the stack ----------------------------------------------------------------

const p = (v) => fmt.pressure(v, 2);

/** Why there is no pressure to draw, in the gauge's own terms. */
function lastStatusText(samples) {
  const last = samples[samples.length - 1];
  if (!last) return "Nothing recorded in this window yet.";
  const reasons = [1, 2].map((channel) => {
    const state = A.GAUGE_STATES[last[`p${channel}_status`]];
    return state ? `channel ${channel} ${state.label}` : null;
  }).filter(Boolean);
  return reasons.length
    ? `No valid pressure reading in this window: ${reasons.join(", ")}. At atmosphere this rig's gauge is overrange.`
    : "No valid pressure reading in this window.";
}

function lastValue(samples, key, test = finite) {
  for (let i = samples.length - 1; i >= 0; i--) {
    if (test(samples[i][key])) return samples[i][key];
  }
  return null;
}

/**
 * Pressure, rotor speed, current, power and voltage on one time axis.
 *
 * `x` says how time reads ("clock" for the rolling history, "elapsed" for a
 * run); `domain` pins the axis so a window does not rescale as it fills.
 * `panels` picks which charts appear; any with nothing to draw is left out
 * rather than shown empty, so the stack is only as tall as the data.
 */
export function RigStack({
  samples, x = "clock", domain = null, pump, gaugeNames = {},
  panels = ["pressure", "speed", "current", "power", "voltage"],
  phases = [], events = [], pressureRefs = [], pressureBands = [],
  cursor, onCursor, pressureHeight = 200, height = 128, bare = false,
}) {
  const limits = pumpLimits(pump);

  const series = useMemo(() => {
    const out = {};
    out.pressure = [1, 2].map((channel) => ({
      key: `p${channel}`,
      label: `Channel ${channel}${gaugeNames[channel] ? ` · ${gaugeNames[channel]}` : ""}`,
      color: channel === 1 ? "--series-1" : "--series-2",
      points: A.column(samples, `p${channel}`, { positive: true }),
    }));
    out.speed = [{ key: "speed", label: "Rotor speed", color: "--series-3",
                   points: A.column(samples, "speed") }];
    out.current = [{ key: "current", label: "Drive current", color: "--series-2",
                     points: A.column(samples, "current") }];
    out.power = [{ key: "power", label: "Drive power", color: "--series-2",
                   points: A.column(samples, "power") }];
    out.voltage = [{ key: "voltage", label: "Supply voltage", color: "--series-1",
                     points: A.column(samples, "voltage") }];
    out.temperature = [{ key: "tms", label: "TMS temperature", color: "--series-2",
                         points: A.temperaturePoints(samples) }];
    out.magnetron = [{ key: "magnetron", label: "Magnetron power", color: "--series-1",
                       mark: "step", points: A.column(samples, "magnetron_w") }];
    out.gas = [{ key: "gas", label: "Argon flow", color: "--series-1", mark: "step",
                 points: A.column(samples, "gas_sccm") }];
    out.gate = [{ key: "gate", label: "Gate valve", color: "--series-1", mark: "step",
                  points: A.column(samples, "gate_pct") }];
    return out;
  }, [samples, gaugeNames[1], gaugeNames[2]]);

  const shutterStrip = useMemo(() => ({
    label: "Shutter",
    segments: A.spansWhere(samples, (s) => (finite(s.shutter)
      ? (s.shutter ? { key: "open", label: "open", tone: "accent" }
                   : { key: "closed", label: "closed", tone: "idle" })
      : null)),
  }), [samples]);

  const overTemp = useMemo(() => ({
    label: "Over-temp",
    segments: A.spansWhere(samples, (s) => (s.ov_temp_pump || s.ov_temp_elec
      ? { key: "hot", label: s.ov_temp_pump ? "pump over temperature"
                                            : "electronics over temperature", tone: "crit" }
      : null)),
  }), [samples]);

  const voltageLow = useMemo(() => {
    const values = samples.map((s) => s.voltage).filter(finite);
    return values.length ? Math.min(...values) : null;
  }, [samples]);

  const xAxis = domain ? { kind: x, min: domain[0], max: domain[1] } : { kind: x };
  const has = (key) => series[key]?.some((s) => s.points.some((pt) => finite(pt[1])));

  const definitions = {
    pressure: () => ({
      title: "Chamber pressure", unit: "mbar", height: pressureHeight,
      readout: lastValue(samples, "p_best") != null
        ? `${p(lastValue(samples, "p_best"))} mbar` : "no reading",
      plot: { series: series.pressure, y: { log: true }, formatValue: p, unit: "mbar",
              refs: pressureRefs, bands: pressureBands,
              emptyText: lastStatusText(samples) },
      legend: html`<${Legend} series=${series.pressure} />`,
    }),
    speed: () => ({
      title: "Rotor speed", unit: "Hz",
      subtitle: limits.switchpoint
        ? `the drive trips if held below ${fmt.integer(limits.switchpoint)} Hz` : "",
      readout: lastValue(samples, "speed") != null
        ? `${fmt.integer(lastValue(samples, "speed"))} Hz` : "—",
      plot: {
        series: series.speed, unit: "Hz", formatValue: (v) => fmt.integer(v),
        y: { min: 0, max: Math.max((limits.nominal || 0) * 1.06,
                                   ...series.speed[0].points.map((pt) => pt[1]).filter(finite), 10) },
        refs: [
          limits.nominal && { axis: "y", value: limits.nominal, tone: "muted",
                              label: `nominal ${fmt.integer(limits.nominal)} Hz` },
          limits.switchpoint && { axis: "y", value: limits.switchpoint, tone: "warn",
                                  label: "80 % switchpoint" },
        ].filter(Boolean),
      },
    }),
    current: () => ({
      title: "Drive current", unit: "A",
      readout: lastValue(samples, "current") != null
        ? `${fmt.number(lastValue(samples, "current"), 2)} A` : "—",
      plot: {
        series: series.current, unit: "A", formatValue: (v) => fmt.number(v, 2),
        y: { zero: true, include: limits.currentRated ? [limits.currentRated * 1.08] : [1] },
        refs: limits.currentRated ? [{ axis: "y", value: limits.currentRated, tone: "muted",
                                       label: `rated ${limits.currentRated} A continuous` }] : [],
      },
    }),
    power: () => ({
      title: "Drive power", unit: "W",
      subtitle: "the heat put into the pump",
      readout: lastValue(samples, "power") != null
        ? `${fmt.integer(lastValue(samples, "power"))} W` : "—",
      plot: {
        series: series.power, unit: "W", formatValue: (v) => fmt.integer(v),
        y: { zero: true, include: [limits.powerReduced + 8] },
        refs: [
          { axis: "y", value: limits.powerFull, tone: "warn",
            label: `${limits.powerFull} W: limit at full speed (argon)` },
          { axis: "y", value: limits.powerReduced, tone: "crit",
            label: `${limits.powerReduced} W: limit at ≤ ${fmt.integer(limits.reducedHz)} Hz` },
        ],
      },
      strips: overTemp.segments.length ? [overTemp] : [],
    }),
    voltage: () => ({
      title: "Supply voltage", unit: "V",
      subtitle: voltageLow != null && voltageLow < limits.supplyLow
        ? `fell to ${fmt.number(voltageLow, 2)} V here — below the ${fmt.number(limits.supplyLow, 1)} V rated floor`
        : `rated ${limits.supply} V ± ${limits.tolerancePct} %`,
      readout: lastValue(samples, "voltage") != null
        ? `${fmt.number(lastValue(samples, "voltage"), 2)} V` : "—",
      plot: {
        series: series.voltage, unit: "V", formatValue: (v) => fmt.number(v, 2),
        y: { include: [limits.supplyLow - 1.2, limits.supplyHigh + 0.4], minSpan: 3 },
        bands: [{ axis: "y", from: limits.supplyLow, to: limits.supplyHigh,
                  label: `rated ${limits.supply} V ± ${limits.tolerancePct} %` }],
      },
    }),
    temperature: () => ({
      title: "Pump temperature (TMS)", unit: "°C",
      readout: lastValue(samples, "tms_temp", (v) => finite(v) && v > 0) != null
        ? `${fmt.integer(lastValue(samples, "tms_temp", (v) => finite(v) && v > 0))} °C` : "—",
      plot: { series: series.temperature, unit: "°C", formatValue: (v) => fmt.integer(v),
              y: { zero: true, include: [40] } },
    }),
    magnetron: () => ({
      title: "Magnetron power", unit: "W", height: 104,
      legend: shutterStrip.segments.length ? html`
        <div class="legend strip-legend">
          <span class="item"><span class="key block accent" />shutter open — depositing</span>
          <span class="item"><span class="key block idle" />shutter closed</span>
        </div>` : null,
      readout: lastValue(samples, "magnetron_w") != null
        ? `${fmt.integer(lastValue(samples, "magnetron_w"))} W` : "—",
      plot: { series: series.magnetron, unit: "W", formatValue: (v) => fmt.integer(v),
              y: { zero: true }, strips: shutterStrip.segments.length ? [shutterStrip] : [] },
    }),
    gas: () => ({
      title: "Argon flow", unit: "sccm", height: 96,
      readout: lastValue(samples, "gas_sccm") != null
        ? `${fmt.number(lastValue(samples, "gas_sccm"), 1)} sccm` : "—",
      plot: { series: series.gas, unit: "sccm", formatValue: (v) => fmt.number(v, 1),
              y: { zero: true } },
    }),
    gate: () => ({
      title: "Gate valve opening", unit: "%", height: 96,
      readout: lastValue(samples, "gate_pct") != null
        ? `${fmt.integer(lastValue(samples, "gate_pct"))} %` : "—",
      plot: { series: series.gate, unit: "%", formatValue: (v) => fmt.integer(v),
              y: { min: 0, max: 105 } },
    }),
  };

  // Pressure stays even when no channel has a valid reading -- it is the
  // context for every other chart, and its empty state says why it is empty.
  // The rest are left out when they have nothing to draw.
  const visible = panels.filter((key) => definitions[key] && (key === "pressure" || has(key)));
  if (!visible.length) {
    return html`<div class="chart-empty">Nothing recorded in this window yet.</div>`;
  }

  return html`
    <div class="chart-stack">
      ${visible.map((key, index) => {
        const definition = definitions[key]();
        const last = index === visible.length - 1;
        if (bare) {
          // Inside a panel that already names the chart (the wall display):
          // the plot and its legend, no second title.
          return html`
            <div key=${key}>
              <${Plot} ...${definition.plot} x=${xAxis}
                       height=${definition.height || height}
                       strips=${definition.strips || definition.plot.strips || []}
                       phases=${phases} events=${events} xLabels=${true}
                       cursor=${cursor} onCursor=${onCursor} title=${definition.title} />
              ${definition.legend}
            </div>`;
        }
        return html`
          <${ChartPanel} key=${key} title=${definition.title} subtitle=${definition.subtitle}
                         readout=${definition.readout} legend=${definition.legend}
                         table=${() => html`<${DataTable} series=${definition.plot.series}
                                                          x=${xAxis} unit=${definition.unit}
                                                          formatValue=${definition.plot.formatValue} />`}>
            <${Plot} ...${definition.plot} x=${xAxis}
                     height=${(definition.height || height) + (last ? 0 : -14)
                              + (definition.strips?.length || definition.plot.strips?.length ? 18 : 0)}
                     strips=${definition.strips || definition.plot.strips || []}
                     phases=${index === 0 ? phases : phases.map((ph) => ({ ...ph, label: "" }))}
                     events=${events} xLabels=${last}
                     cursor=${cursor} onCursor=${onCursor}
                     title=${definition.title} />
          <//>`;
      })}
    </div>`;
}

// --- the load curve -------------------------------------------------------------

/**
 * Drive current (or power) against chamber pressure: the turbopump's load
 * curve, read from what it actually did.
 *
 * At full speed the drive's power is bearing friction plus gas friction, and
 * gas friction grows with the throughput -- so the points rise with pressure
 * along one curve. Where that curve reaches the drive's permitted power the
 * rotor can no longer hold its speed, and points move to the orange group:
 * the pump is past what it can carry. That knee is this rig's real ceiling
 * for working pressure with the gate open, measured rather than looked up.
 */
export function LoadCurve({ samples, pump, metric, onMetric, cursor, onCursor,
                            interval = 2 }) {
  const limits = pumpLimits(pump);
  const key = metric === "power" ? "power" : "current";

  const series = useMemo(() => {
    const states = A.loadState(samples, limits.nominal);
    const groups = { full: [], held: [], runup: [] };
    samples.forEach((s, index) => {
      const state = states[index];
      if (!state || !finite(s.p_best) || s.p_best <= 0 || !finite(s[key])) return;
      groups[state].push([s.p_best, s[key], {
        t: s.t,
        tolerance: interval * 3,
        head: `${p(s.p_best)} mbar${s.at ? ` · ${fmt.clock(s.at)}` : ""}`,
        rows: [
          { label: "rotor", value: `${fmt.integer(s.speed)} Hz` },
          { label: key === "power" ? "current" : "power",
            value: key === "power" ? `${fmt.number(s.current, 2)} A` : `${fmt.integer(s.power)} W` },
        ],
      }]);
    });
    return ["full", "held", "runup"].map((state) => ({
      key: state, label: A.LOAD_STATES[state].label, color: A.LOAD_STATES[state].color,
      mark: "scatter", points: groups[state], count: groups[state].length,
    }));
  }, [samples, key, limits.nominal, interval]);

  const held = series.find((s) => s.key === "held").points;
  const heldSeconds = held.length * interval;
  const worst = held.length ? Math.max(...held.map((pt) => pt[0])) : null;
  const unit = key === "power" ? "W" : "A";
  const formatValue = key === "power" ? (v) => fmt.integer(v) : (v) => fmt.number(v, 2);

  return html`
    <${ChartPanel}
      title=${`Load curve: drive ${key} against chamber pressure`}
      subtitle=${held.length
        ? `held below full speed for about ${fmt.duration(heldSeconds)}, at up to ${p(worst)} mbar`
        : "the rotor held full speed throughout"}
      legend=${html`<${Legend} series=${series} />`}
      table=${() => html`<${DataTable} series=${series} unit=${unit} formatValue=${formatValue}
                                       x=${{ kind: "pressure", title: "Pressure" }} />`}>
      <div class="row tight chart-tools">
        <div class="segmented" role="group" aria-label="Quantity">
          <button aria-pressed=${key === "current" ? "true" : "false"}
                  onClick=${() => onMetric("current")}>Current</button>
          <button aria-pressed=${key === "power" ? "true" : "false"}
                  onClick=${() => onMetric("power")}>Power</button>
        </div>
      </div>
      <${Plot} series=${series} height=${250} unit=${unit} formatValue=${formatValue}
               x=${{ log: true, kind: "pressure" }}
               y=${{ zero: true, include: key === "power" ? [limits.powerReduced + 8]
                                                          : [(limits.currentRated || 4) * 1.02] }}
               refs=${key === "power" ? [
                 { axis: "y", value: limits.powerFull, tone: "warn",
                   label: `${limits.powerFull} W limit at full speed (argon)` },
                 { axis: "y", value: limits.powerReduced, tone: "crit",
                   label: `${limits.powerReduced} W limit` },
               ] : []}
               cursor=${cursor} onCursor=${onCursor}
               emptyText="No samples with the motor driven and a valid pressure in this window."
               title="Load curve" />
    <//>`;
}

// --- how fast the pressure is changing ------------------------------------------

/**
 * The fractional rate of change, |d ln p/dt| in %/min, on a log axis, coloured
 * by direction. This is the quantity the run engine judges base pressure on,
 * so the chart shows exactly why a run did or did not call it.
 */
export function RateChart({ samples, windowS = 120, threshold = 2, x = "clock", domain,
                            phases = [], events = [], cursor, onCursor, height = 150,
                            xLabels = true }) {
  const series = useMemo(() => {
    const rates = A.fractionalRate(samples, windowS);
    const { falling, rising } = A.rateSeries(rates);
    return [
      { key: "falling", label: "pressure falling", color: "--series-1", points: falling },
      { key: "rising", label: "pressure rising", color: "--series-2", points: rising },
    ];
  }, [samples, windowS]);
  const latest = useMemo(() => {
    const rates = A.fractionalRate(samples.slice(-200), windowS);
    for (let i = rates.length - 1; i >= 0; i--) if (finite(rates[i].rate)) return rates[i].rate;
    return null;
  }, [samples, windowS]);
  const xAxis = domain ? { kind: x, min: domain[0], max: domain[1] } : { kind: x };
  return html`
    <${ChartPanel} title="Rate of change"
      subtitle=${`fit of ln p over ${fmt.duration(windowS)}, as the base-pressure test does`}
      readout=${latest == null ? "—"
        : Math.abs(latest) < 0.01 ? "flat"
        : `${latest < 0 ? "falling" : "rising"} ${fmt.number(Math.abs(latest), Math.abs(latest) < 10 ? 2 : 0)} %/min`}
      legend=${html`<${Legend} series=${series} />`}
      table=${() => html`<${DataTable} series=${series} x=${xAxis} unit="%/min" />`}>
      <${Plot} series=${series} height=${height} unit="%/min"
               formatValue=${(v) => fmt.number(v, v < 10 ? 2 : 0)}
               x=${xAxis} y=${{ log: true, min: 0.01, max: 1e4 }}
               refs=${[{ axis: "y", value: threshold, tone: "muted",
                         label: `${threshold} %/min: flat enough to call base` }]}
               phases=${phases.map((ph) => ({ ...ph, label: "" }))} events=${events}
               cursor=${cursor} onCursor=${onCursor} xLabels=${xLabels}
               emptyText="Needs a few minutes of valid pressure readings." />
    <//>`;
}

// --- the pump-down, as a vacuum scientist reads it -------------------------------

/**
 * Pressure against time since the turbopump started, both axes logarithmic,
 * with the local slope n beneath it.
 *
 * On log-log axes a pump-down that is limited by gas coming off the walls is
 * a straight line, and its slope names the source: n ≈ 1 is surface
 * desorption, usually water, and baking is the cure; n ≈ 0.5 is gas
 * diffusing out of the bulk, usually elastomer seals; n → 0 is a floor -- a
 * leak, permeation, or the pump's own limit. The volume phase at the very
 * start is steeper than any of them.
 */
export function PumpdownChart({ samples, nominal, title = "Pump-down on log-log axes" }) {
  const curve = useMemo(() => A.pumpdownCurve(samples, nominal).points,
                        [samples, nominal]);
  const slope = useMemo(() => A.loglogSlope(curve), [curve]);
  const [cursor, setCursor] = useState(null);

  if (curve.length < 10) {
    return html`
      <${ChartPanel} title=${title}>
        <div class="chart-empty">Needs a pump-down after the turbopump starts.</div>
      <//>`;
  }

  const series = [{ key: "p", label: "Chamber pressure", color: "--series-1", points: curve }];
  const slopeSeries = [{ key: "n", label: "Local slope n", color: "--series-1",
                         points: slope.map(([t, n]) => [t, finite(n) ? Math.max(-0.4, Math.min(2.9, n)) : null]) }];
  const settled = A.lastDecadeSlope(curve);
  const median = settled ? settled.n : null;
  const x = { log: true, kind: "logtime", floor: 1 };

  return html`
    <div class="chart-stack">
      <${ChartPanel} title=${title}
        subtitle="from the turbopump start to the end of pumping down — before any gas was let in"
        readout=${`${p(curve[curve.length - 1][1])} mbar`}
        table=${() => html`<${DataTable} series=${series} x=${{ kind: "logtime", title: "Time" }}
                                         unit="mbar" formatValue=${p} />`}>
        <${Plot} series=${series} height=${210} x=${x} y=${{ log: true }} unit="mbar"
                 formatValue=${p} xLabels=${false} cursor=${cursor} onCursor=${setCursor} />
      <//>
      <${ChartPanel} title="Local slope n, where p ∝ t⁻ⁿ"
        subtitle=${median == null ? "" : `n = ${fmt.number(median, 2)} fitted over the last decade of time `
          + `(${fmt.duration(settled.from)} to ${fmt.duration(settled.to)}, R² ${fmt.number(settled.r2, 3)}) — ${diagnose(median)}`}
        readout=${median == null ? "—" : `n = ${fmt.number(median, 2)}`}
        hint="n ≈ 1: surface desorption, usually water — baking helps. n ≈ 0.5: gas diffusing out of the bulk, typically elastomer seals. n → 0: a floor — a leak, permeation, or the pump's own limit. Clipped to −0.4…2.9."
        table=${() => html`<${DataTable} series=${slopeSeries} x=${{ kind: "logtime", title: "Time" }}
                                         formatValue=${(v) => fmt.number(v, 2)} />`}>
        <${Plot} series=${slopeSeries} height=${150} x=${x} y=${{ min: -0.5, max: 3 }}
                 formatValue=${(v) => fmt.number(v, 2)} cursor=${cursor} onCursor=${setCursor}
                 refs=${[
                   { axis: "y", value: 1, tone: "muted", label: "n = 1 desorption (water)" },
                   { axis: "y", value: 0.5, tone: "muted", label: "n = 0.5 diffusion" },
                   { axis: "y", value: 0, tone: "muted", label: "n = 0 floor" },
                 ]} />
      <//>
    </div>`;
}

function diagnose(n) {
  if (n > 1.5) return "still in the volume phase";
  if (n > 0.75) return "desorption-limited, most likely water";
  if (n > 0.3) return "diffusion-limited, most likely the seals";
  return "at a floor: a leak, permeation, or the pump's limit";
}
