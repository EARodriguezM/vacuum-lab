// The charts for one run -- in progress on the Process page, or read back from
// the database on the History page. Same component for both, so a run looks
// the same live as it does a month later.
//
// Each procedure gets the stack plus the one or two charts that answer its own
// question: a deposition, whether the working pressure held; a base-pressure
// search, why the engine called base when it did; a rate-of-rise, whether the
// rise was straight enough to be a leak rate; any pump-down, which gas source
// limits it.

import { html } from "./html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { ChartPanel, DataTable, Legend, Plot } from "./chart.js";
import { LoadCurve, PumpdownChart, RateChart, RigStack, useStored } from "./rigcharts.js";
import { api } from "./api.js";
import * as A from "./analysis.js";
import * as fmt from "./format.js";

const finite = (value) => typeof value === "number" && Number.isFinite(value);
const p = (v) => fmt.pressure(v, 2);

const VACUUM_SEQUENCES = new Set(["deposition", "base_calibration", "full_vacuum"]);
const PROCESS_PHASES = new Set(["backfill", "presputter", "deposit", "recover"]);

/** Pressure limits a procedure was configured with, as chart references. */
export function pressureRefs(procedure, config, pump) {
  const refs = [];
  const limit = pump?.interlocks?.motor_on?.limit_mbar;
  if (VACUUM_SEQUENCES.has(procedure)) {
    const crossover = Math.min(config.crossover_mbar ?? limit ?? 0.8, limit ?? Infinity);
    if (finite(crossover)) {
      refs.push({ axis: "y", value: crossover, tone: "muted",
                  label: `crossover ${fmt.pressure(crossover, 1)}` });
    }
  }
  if (procedure === "deposition") {
    if (finite(config.working_mbar)) {
      refs.push({ axis: "y", value: config.working_mbar, tone: "accent",
                  label: `working ${fmt.pressure(config.working_mbar, 1)}` });
    }
    if (finite(config.max_inlet_mbar)) {
      refs.push({ axis: "y", value: config.max_inlet_mbar, tone: "crit",
                  label: `abort above ${fmt.pressure(config.max_inlet_mbar, 1)}` });
    }
  }
  if (procedure === "pumpdown" && finite(config.target_mbar)) {
    refs.push({ axis: "y", value: config.target_mbar, tone: "accent",
                label: `target ${fmt.pressure(config.target_mbar, 1)}` });
  }
  if (procedure === "bakeout" && finite(config.max_pressure_mbar)) {
    refs.push({ axis: "y", value: config.max_pressure_mbar, tone: "crit",
                label: `abort above ${fmt.pressure(config.max_pressure_mbar, 1)}` });
  }
  return refs;
}

function panelsFor(procedure, samples) {
  if (procedure === "bakeout") return ["pressure", "temperature", "power"];
  const base = ["pressure", "speed", "current", "power", "voltage"];
  const process = ["magnetron", "gas", "gate"];
  const hasProcess = samples.some((s) => finite(s.magnetron_w) || finite(s.gas_sccm)
                                         || finite(s.gate_pct));
  return procedure === "deposition" || hasProcess ? [...base, ...process] : base;
}

/**
 * Everything charted for one run.
 *
 * samples  rows with `t` in seconds since the run started
 * events   the run log, for markers
 */
/** The procedure catalogue, fetched once: its field defaults are the values
 *  the engine used for anything a run's config left out. */
let catalogue = null;
export function useCatalogue() {
  const [value, setValue] = useState(catalogue);
  useEffect(() => {
    if (catalogue) return;
    api.procedures().then((data) => {
      catalogue = data.procedures || [];
      setValue(catalogue);
    }).catch(() => {});
  }, []);
  return value || [];
}

/**
 * The configuration a run actually ran with.
 *
 * A missing or null value means the engine fell back to its default, and the
 * selftest holds every published field default equal to the code's own
 * fallback -- so the published default *is* what was used, and the charts
 * can draw the setpoint and the ceiling for a run started with an empty
 * config (run 21 was).
 */
export function effectiveConfig(procedure, config, procedures) {
  const defaults = {};
  for (const field of procedures.find((p) => p.name === procedure)?.fields || []) {
    defaults[field.key] = field.default;
  }
  const given = Object.fromEntries(Object.entries(config || {})
    .filter(([, value]) => value !== null && value !== undefined && value !== ""));
  return { ...defaults, ...given };
}

export function RunCharts({ run, procedure, config: given = {}, samples, events = [],
                            startedAt, pump, gauge, live = false }) {
  const procedures = useCatalogue();
  const config = effectiveConfig(procedure, given, procedures);
  const [cursor, setCursor] = useState(null);
  const [metric, setMetric] = useStored("vacuum-lab-load-metric", "current");

  const phases = useMemo(() => A.phaseSpans(samples, fmt.phaseLabel), [samples]);
  const markers = useMemo(() => A.eventMarkers(events, startedAt), [events, startedAt]);
  const gaugeNames = Object.fromEntries((gauge?.identity?.gauges || [])
    .filter((g) => g.present !== false).map((g) => [g.channel, g.identifier]));
  const interval = useMemo(() => {
    const gaps = [];
    for (let i = 1; i < samples.length && gaps.length < 50; i++) gaps.push(samples[i].t - samples[i - 1].t);
    gaps.sort((a, b) => a - b);
    return gaps.length ? gaps[gaps.length >> 1] : 2;
  }, [samples]);

  if (!samples.length) {
    return html`<div class="chart-empty">${live ? "Waiting for the first sample…" : "This run recorded no samples."}</div>`;
  }

  const showRate = VACUUM_SEQUENCES.has(procedure) || procedure === "pumpdown"
    || procedure === "hold";
  const showProcess = procedure === "deposition"
    && samples.some((s) => PROCESS_PHASES.has(s.phase));
  const showLoad = samples.some((s) => s.motor && s.power > 0);
  const nominal = Number(pump?.identity?.NominalSpd) || null;
  const showPumpdown = !live && procedure !== "rate_of_rise" && procedure !== "bakeout"
    && A.turboStart(samples, nominal) !== null;

  return html`
    <div class="grid" style="gap:16px">
      <section class="card">
        <header>
          <div>
            <h2>Run charts</h2>
            <p class="hint">One time axis for every chart: hover any of them, or focus one and
              use the arrow keys, to read all of them at that moment. Shaded bands are the
              run's phases; dotted markers are entries from the run log.</p>
          </div>
        </header>
        <${RigStack} samples=${samples} x="elapsed" pump=${pump} gaugeNames=${gaugeNames}
                     panels=${panelsFor(procedure, samples)} phases=${phases} events=${markers}
                     pressureRefs=${pressureRefs(procedure, config, pump)}
                     cursor=${cursor} onCursor=${setCursor} />
        ${showRate && html`
          <${RateChart} samples=${samples} x="elapsed" windowS=${120}
                        threshold=${finite(config.base_rate_pct) ? config.base_rate_pct : 2}
                        phases=${phases} events=${markers}
                        cursor=${cursor} onCursor=${setCursor} />`}
      </section>

      ${showProcess && html`
        <section class="card">
          <${WorkingPressure} samples=${samples} config=${config} phases=${phases}
                              cursor=${cursor} onCursor=${setCursor} />
        </section>`}

      ${procedure === "rate_of_rise" && html`
        <section class="card">
          <${RateOfRiseChart} samples=${samples} run=${run} config=${config} />
        </section>`}

      ${showLoad && html`
        <section class="card">
          <${LoadCurve} samples=${samples} pump=${pump} metric=${metric} onMetric=${setMetric}
                        cursor=${cursor} onCursor=${setCursor} interval=${interval} />
        </section>`}

      ${showPumpdown && html`
        <section class="card">
          <${PumpdownChart} samples=${samples} nominal=${nominal} />
        </section>`}
    </div>`;
}

/**
 * The argon working pressure, close up, against what the run asked for.
 *
 * This is the chart for whoever is turning the needle valve: the setpoint,
 * the factor-of-two window the backfill accepts, and the ceiling that aborts
 * the run -- with the deviation from the setpoint as a number beside it.
 */
function WorkingPressure({ samples, config, phases, cursor, onCursor }) {
  const working = config.working_mbar;
  const ceiling = config.max_inlet_mbar;
  const inProcess = useMemo(() => samples.filter((s) => PROCESS_PHASES.has(s.phase)),
                            [samples]);
  // Everything from the backfill on, framed to always show a decade below the
  // setpoint and the ceiling: run 21 sat at 4e-5 mbar for nineteen minutes
  // waiting for the argon, then overshot straight past the ceiling, and that
  // wait is as much the story as the overshoot.
  const series = useMemo(() => [{
    key: "p", label: "Chamber pressure", color: "--series-1",
    points: A.column(inProcess, "p_best", { positive: true }),
  }], [inProcess]);
  const values = inProcess.map((s) => s.p_best).filter((v) => finite(v) && v > 0);
  const latest = values.length ? values[values.length - 1] : null;
  const deviation = latest && working ? (latest / working - 1) * 100 : null;
  const deposit = inProcess.filter((s) => s.phase === "deposit").map((s) => s.p_best)
    .filter((v) => finite(v) && v > 0);
  const spread = deposit.length && working
    ? ((Math.max(...deposit) - Math.min(...deposit)) / working) * 100 : null;
  const frame = working ? [working / 10, Math.max(ceiling || 0, working * 10) * 1.5] : [];

  return html`
    <${ChartPanel} title="Working pressure"
      subtitle=${spread != null
        ? `varied by ${fmt.integer(spread)} % of the setpoint while depositing`
        : "argon backfill to the end of the run"}
      readout=${latest ? `${p(latest)} mbar${deviation != null
        ? ` (${deviation >= 0 ? "+" : ""}${fmt.integer(deviation)} %)` : ""}` : "—"}
      hint="Gauge readings are nitrogen-equivalent unless the gauge's gas correction is set; argon reads differently on Pirani and cold-cathode gauges, so compare working pressures between systems only with the correction applied."
      table=${() => html`<${DataTable} series=${series} x=${{ kind: "elapsed" }} unit="mbar"
                                       formatValue=${p} />`}>
      <${Plot} series=${series} height=${200} x=${{ kind: "elapsed" }}
               y=${{ log: true, include: frame }}
               unit="mbar" formatValue=${p}
               phases=${phases.filter((ph) => PROCESS_PHASES.has(ph.phase))}
               bands=${working ? [{ axis: "y", from: working / 2, to: working * 2,
                                    label: "accepted: ½× to 2× the setpoint" }] : []}
               refs=${[
                 working && { axis: "y", value: working, tone: "accent",
                              label: `setpoint ${fmt.pressure(working, 1)}` },
                 ceiling && { axis: "y", value: ceiling, tone: "crit",
                              label: `abort above ${fmt.pressure(ceiling, 1)}` },
               ].filter(Boolean)}
               cursor=${cursor} onCursor=${onCursor}
               emptyText="The run has not reached the argon backfill." />
    <//>`;
}

/** What a rate-of-rise curve says, by the engine's verdict codes. */
const RISE_VERDICTS = {
  linear: "straight enough to be a leak rate",
  bent: "bent: an outgassing transient, not a leak rate",
  falling: "falling: something is still pumping the chamber — is the gate shut? Not a leak rate",
  frozen: "no rise at all: the gauge has stopped measuring — not a leak-tight chamber",
};

/** The same verdict as RateOfRise.summarise, while the run is still measuring. */
function riseVerdict(measuring, fit) {
  if (!fit) return null;
  // Not one reading changed: a gauge that stopped measuring. Its fit is
  // "R² 1, no rise" -- a perfect leak-tight chamber, which it is not.
  if (measuring.every((s) => s.p_best === measuring[0].p_best)) return "frozen";
  if (fit.slope < 0) return "falling";
  return fit.r2 >= A.JUDGEMENT.linear_r2 ? "linear" : "bent";
}

/**
 * The rise, on linear axes, with the straight line fitted to it.
 *
 * A leak rate is only a leak rate if the rise is straight: a curve that bends
 * over is a surface still giving up gas, and its slope means nothing. The
 * residual of the fit is the evidence either way, so it is drawn.
 */
function RateOfRiseChart({ samples, run, config }) {
  const measuring = useMemo(() => samples.filter((s) => s.phase === "measuring"
                                                         && finite(s.p_best)), [samples]);
  const fit = useMemo(() => A.linearFit(measuring.map((s) => [s.t, s.p_best])), [measuring]);
  const series = useMemo(() => {
    const out = [{ key: "p", label: "Measured", color: "--series-1",
                   points: measuring.map((s) => [s.t, s.p_best]) }];
    if (fit && measuring.length) {
      const first = measuring[0].t;
      const last = measuring[measuring.length - 1].t;
      out.push({ key: "fit", label: "Straight-line fit", color: "--series-2", dash: "6 4",
                 endLabel: false,
                 points: [[first, fit.intercept + fit.slope * first],
                          [last, fit.intercept + fit.slope * last]] });
    }
    return out;
  }, [measuring, fit]);
  const residuals = useMemo(() => [{
    key: "r", label: "Residual", color: "--series-1",
    points: fit ? measuring.map((s) => [s.t, s.p_best - (fit.intercept + fit.slope * s.t)]) : [],
  }], [measuring, fit]);
  const volume = config.volume_l;
  // Once the run has ended, its verdict is the engine's (lab/runs.py), not
  // a second fit made here; while it runs, the same maths on the same rows.
  const result = run?.result || {};
  const judged = "r_squared" in result;
  const r2 = judged ? result.r_squared : fit?.r2;
  const verdict = judged && result.verdict ? result.verdict
    : judged ? (r2 === null ? "frozen" : r2 >= A.JUDGEMENT.linear_r2 ? "linear" : "bent")
      : riseVerdict(measuring, fit);
  const leak = judged ? result.leak_rate_mbar_l_s
    : (fit && volume && verdict !== "frozen" && verdict !== "falling"
      ? fit.slope * volume : null);
  const said = verdict ? RISE_VERDICTS[verdict] : null;

  return html`
    <div class="chart-stack">
      <${ChartPanel} title="Rate of rise"
        subtitle=${!said ? "needs the measuring phase"
          : finite(r2) && verdict !== "frozen" ? `R² = ${fmt.number(r2, 4)} — ${said}` : said}
        readout=${leak != null ? `${fmt.pressure(leak, 2)} mbar·L/s` : null}
        legend=${html`<${Legend} series=${series} />`}
        table=${() => html`<${DataTable} series=${series} x=${{ kind: "elapsed" }} unit="mbar"
                                         formatValue=${p} />`}>
        <${Plot} series=${series} height=${210} x=${{ kind: "elapsed" }} unit="mbar"
                 formatValue=${p} y=${{}} xLabels=${false}
                 emptyText="No samples in the measuring phase yet." />
      <//>
      <${ChartPanel} title="Residual from the straight line"
        subtitle="a leak scatters evenly about zero; outgassing bows">
        <${Plot} series=${residuals} height=${120} x=${{ kind: "elapsed" }} unit="mbar"
                 formatValue=${p} y=${{}} refs=${[{ axis: "y", value: 0, tone: "muted" }]} />
      <//>
    </div>`;
}

// --- comparing runs ---------------------------------------------------------------

/**
 * This run's pump-down against others, aligned at the turbopump start.
 *
 * Emphasis rather than a rainbow: this run in the accent colour, the others
 * in grey, each named at its end. The question is "is this chamber behaving
 * like it used to", and that is one curve against a background.
 */
export function CompareRuns({ run, samples, runs }) {
  const candidates = runs.filter((r) => r.id !== run.id && (r.sample_count ?? 0) > 20
    && ["base_calibration", "full_vacuum", "deposition", "pumpdown"].includes(r.procedure));
  const [chosen, setChosen] = useState(() => candidates.slice(0, 3).map((r) => r.id));
  const [others, setOthers] = useState({});
  const [cursor, setCursor] = useState(null);

  useEffect(() => {
    let cancelled = false;
    for (const id of chosen) {
      if (others[id]) continue;
      api.runSamples(id, 1500).then((data) => {
        if (!cancelled) setOthers((current) => ({ ...current, [id]: data.samples || [] }));
      }).catch(() => {});
    }
    return () => { cancelled = true; };
  }, [chosen.join(",")]);

  const align = (rows) => A.pumpdownCurve(rows).points;

  const series = useMemo(() => {
    const out = chosen.filter((id) => others[id] && align(others[id]).length).map((id) => {
      const record = runs.find((r) => r.id === id);
      return { key: `r${id}`, label: `Run ${id} · ${fmt.dateTime(record?.started_at)}`,
               color: "--series-2", faint: true, points: align(others[id]) };
    });
    out.push({ key: "this", label: `This run (${run.id})`, color: "--series-1",
               points: align(samples) });
    return out;
  }, [chosen.join(","), others, samples]);

  const toggle = (id) => setChosen((current) => (current.includes(id)
    ? current.filter((x) => x !== id) : [...current, id].slice(-6)));

  if (!candidates.length) return null;
  return html`
    <section class="card">
      <${ChartPanel} title="Compared with other pump-downs"
        subtitle=${`aligned at the moment each turbopump started from rest; pump-down part only${
          series.find((s) => s.key === "this")?.points.length ? ""
            : " — this run has no start from rest, so only the others are drawn"}`}
        legend=${html`<${Legend} series=${series} />`}
        table=${() => html`<${DataTable} series=${series} x=${{ kind: "logtime", title: "Time" }}
                                         unit="mbar" formatValue=${p} />`}>
        <div class="row tight chart-tools">
          ${candidates.slice(0, 12).map((r) => html`
            <button class="small" aria-pressed=${chosen.includes(r.id) ? "true" : "false"}
                    onClick=${() => toggle(r.id)} title=${r.name}>
              ${r.id} · ${fmt.titleCase(r.procedure)}</button>`)}
        </div>
        <${Plot} series=${series} height=${240} x=${{ log: true, kind: "logtime", floor: 1 }}
                 y=${{ log: true }} unit="mbar" formatValue=${p}
                 cursor=${cursor} onCursor=${setCursor}
                 emptyText="None of these runs started its turbopump while recording." />
      <//>
    </section>`;
}

// --- the rig across months ----------------------------------------------------------

/**
 * Health indicators run by run: is the chamber, or the pump, getting worse?
 *
 * Each is a dot per run on a date axis, joined in order. The reference lines
 * are the pump's ratings, so a drift is read against what it should be, not
 * only against last week.
 */
export function RunTrends({ pump }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => {
    api.trends().then((data) => setRows(data.runs || []))
      .catch((problem) => setError(problem.message));
  }, []);

  const ratings = pump?.ratings || {};
  const floor = (ratings.supply_v ?? 24) * (1 - (ratings.supply_tolerance ?? 0.05));

  const charts = useMemo(() => {
    if (!rows) return [];
    const point = (r, value, extra = []) => [r.started_at, value, {
      head: `Run ${r.id} · ${fmt.dateTime(r.started_at)}`,
      rows: [{ label: fmt.titleCase(r.procedure), value: r.status }, ...extra],
    }];
    const pick = (fn) => rows.map((r) => fn(r)).filter((pt) => pt && finite(pt[1]));
    return [
      {
        title: "Base pressure", unit: "mbar", log: true, formatValue: p,
        hint: "Completed calibrations and runs that reached base with the rotor at full speed.",
        points: pick((r) => r.base_mbar && r.base_valid !== false
          && (r.status === "completed" || r.procedure === "deposition")
          ? point(r, r.base_mbar) : null),
      },
      {
        title: "Time to base pressure", unit: "min", formatValue: (v) => fmt.number(v, 0),
        points: pick((r) => (r.base_mbar && r.base_valid !== false && finite(r.time_to_base_s)
          ? point(r, r.time_to_base_s / 60) : null)),
      },
      {
        title: "Turbopump run-up, rest to full speed", unit: "s",
        formatValue: (v) => fmt.integer(v),
        refs: ratings.startup_s ? [{ axis: "y", value: ratings.startup_s, tone: "muted",
                                     label: `rated ${fmt.duration(ratings.startup_s)}` }] : [],
        include: ratings.startup_s ? [0, ratings.startup_s * 1.3] : [0],
        points: pick((r) => (finite(r.runup_s) ? point(r, r.runup_s) : null)),
      },
      {
        title: "Friction power at full speed", unit: "W", formatValue: (v) => fmt.integer(v),
        hint: "Drive power with nothing to pump (below 1e-4 mbar). A steady rise over months is bearing wear.",
        include: [0],
        points: pick((r) => (finite(r.friction_w) ? point(r, r.friction_w) : null)),
      },
      {
        title: "Lowest supply voltage under load", unit: "V", formatValue: (v) => fmt.number(v, 2),
        refs: [{ axis: "y", value: floor, tone: "warn", label: `rated floor ${fmt.number(floor, 1)} V` }],
        include: [floor - 1, floor + 1.5],
        points: pick((r) => (finite(r.min_voltage) ? point(r, r.min_voltage,
          finite(r.sag_v) ? [{ label: "sag from idle", value: `${fmt.number(r.sag_v, 2)} V` }] : [])
          : null)),
      },
      {
        title: "Time held below full speed by gas load", unit: "min",
        formatValue: (v) => fmt.number(v, 1), include: [0],
        points: pick((r) => (finite(r.held_s) ? point(r, r.held_s / 60) : null)),
      },
      {
        title: "Leak rate (rate of rise)", unit: "mbar·L/s", log: true, formatValue: p,
        points: pick((r) => (finite(r.leak_rate_mbar_l_s) && r.leak_rate_mbar_l_s > 0
          ? point(r, r.leak_rate_mbar_l_s) : null)),
      },
    ].filter((chart) => chart.points.length);
  }, [rows, floor, ratings.startup_s]);

  return html`
    <section class="card">
      <header>
        <div>
          <h2>Trends across runs</h2>
          <p class="hint">One dot per run, measured from its stored curve. These answer what
            a single run cannot: whether the chamber or the pump is getting worse.</p>
        </div>
      </header>
      ${error && html`<div class="banner warn">${error}</div>`}
      ${!rows && !error && html`<div class="chart-empty">Measuring every run…</div>`}
      ${rows && !charts.length && html`<div class="chart-empty">Not enough runs yet.</div>`}
      <div class="grid two">
        ${charts.map((chart) => {
          const series = [{ key: "v", label: chart.title, color: "--series-1",
                            points: chart.points, dots: true, endLabel: false }];
          return html`
            <${ChartPanel} key=${chart.title} title=${chart.title}
              readout=${`${chart.formatValue(chart.points[chart.points.length - 1][1])} ${chart.unit}`}
              hint=${chart.hint}
              table=${() => html`<${DataTable} series=${series} unit=${chart.unit}
                                               x=${{ kind: "date", title: "Run started" }}
                                               formatValue=${chart.formatValue} />`}>
              <${Plot} series=${series} height=${160} x=${{ kind: "date" }}
                       y=${{ log: chart.log, include: chart.include }} unit=${chart.unit}
                       formatValue=${chart.formatValue} refs=${chart.refs || []}
                       gapFactor=${Infinity} />
            <//>`;
        })}
      </div>
    </section>`;
}
