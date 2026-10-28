// The wall display: what the rig is doing, readable from across the lab.
//
// Three audiences read this screen, and it is laid out by the questions they
// ask, in the order they ask them:
//
//   * **An undergraduate** wants to know what the machine is doing, in words,
//     what happens next, and whether anything is wrong. Hence the guide card
//     -- the current step, why it exists, what ends it and about how long that
//     takes -- and the vacuum scale, which puts the pressure on one line from
//     atmosphere to ultra-high vacuum with the rig's own landmarks on it. The
//     "Explain" switch adds a sentence to every figure; it remembers its
//     setting per browser.
//   * **A PhD student** running a deposition wants the run's progress: the
//     phase, the countdown to the shutter opening or closing, the working
//     pressure against its setpoint, the rotor and the drive load -- and the
//     curves, with the run's phases and limits drawn on them.
//   * **A professor** walking past wants to know whether the rig is healthy
//     and what happened since yesterday: the health checklist and the
//     activity feed, with every run one click from its record.
//
// The rules the first version set still hold: bigger type than the rest of
// the panel, every colour paired with a word, one measure per chart. It is
// also the landing page -- the Overview it replaced redirects here -- and on
// the kiosk's 1024×600 screen the guide and the four figures sit side by
// side, so nothing that matters is below the fold.

import { html } from "../html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { Empty, Pill } from "../ui.js";
import { Sparkline, useWidth } from "../chart.js";
import { RigStack, WindowPicker, pumpLimits, useHistory, useStored } from "../rigcharts.js";
import { effectiveConfig, pressureRefs, useCatalogue } from "../runcharts.js";
import { api } from "../api.js";
import { headline } from "./history.js";
import * as A from "../analysis.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

const finite = (value) => typeof value === "number" && Number.isFinite(value);

/** One sentence per phase: what it is for, for somebody seeing it first. */
const PHASE_HELP = {
  rough: "The backing (forevacuum) pump empties the chamber from atmosphere. "
    + "The turbopump waits: it may only start once the chamber is rough enough.",
  crossover: "The chamber is rough enough, so the turbopump motor is being started.",
  spinup: "The turbopump accelerates to its full 1500 Hz (90 000 rpm). The drive "
    + "runs at maximum power until it gets there — that is normal.",
  basepressure: "Pumping until the pressure stops improving. The chamber decides "
    + "how long: water on the walls is the usual reason it is slow.",
  backfill: "Argon is let in until the chamber holds the working pressure, "
    + "where a plasma can burn.",
  presputter: "Plasma on with the shutter closed: the target's surface is sputtered "
    + "clean before it can reach the sample.",
  deposit: "Shutter open: the film is growing on the sample.",
  recover: "Plasma and argon off; the chamber is pumped back down.",
  holding: "Holding the chamber under vacuum and recording until somebody stops the run.",
  roughing: "The backing pump alone, from atmosphere.",
  turbo: "Starting the turbopump.",
  pumping: "Pumping down towards the target pressure.",
  settle: "Letting the isolated chamber settle before measuring.",
  measuring: "Measuring how fast the pressure rises in the closed chamber.",
  heating: "Heating the pump to its setpoint.",
  soak: "Holding the temperature to drive gas off the surfaces.",
  cooling: "Letting the pump cool down.",
};

/** The deposition route, shown when idle so the sequence is always in view. */
const ROUTE = ["rough", "crossover", "spinup", "basepressure", "backfill",
               "presputter", "deposit", "recover"];
/** The steps only a deposition takes -- the ones its hardware is for. */
const DEPOSITION_ONLY = ["backfill", "presputter", "deposit", "recover"];

function countdown(seconds) {
  if (!finite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}

function about(seconds) {
  if (!finite(seconds)) return null;
  if (seconds < 5) return "any moment now";
  return `about ${fmt.duration(seconds)}`;
}

/** Vacuum regime, and how the pressure tile is tinted for it. */
function regimeTone(mbar) {
  if (mbar === null || mbar === undefined) return "idle";
  if (mbar > 1) return "warn";
  if (mbar > 1e-4) return "accent";
  return "good";
}

/** What the rotor is doing, in one word, from the drive's own flags. */
function rotorState(pump) {
  const s = pump?.status || {};
  const sp = pump?.setpoints || {};
  const speed = Number(s.ActualSpd) || 0;
  const nominal = Number(pump?.identity?.NominalSpd) || 0;
  if (!pump?.connected) return { word: "no contact", tone: "crit" };
  if (pump.fault?.code) return { word: `stopped by fault ${pump.fault.code}`, tone: "crit" };
  if (sp.MotorPump !== true) {
    return speed > 0 ? { word: "coasting down, motor off", tone: "warn" }
                     : { word: "stopped", tone: "idle" };
  }
  if (s.PumpAccel) return { word: "accelerating", tone: "accent" };
  if (A.atFullSpeed(speed, nominal)) return { word: "at full speed", tone: "good" };
  if (sp.Standby) return { word: "in standby", tone: "idle" };
  return { word: "held down by gas load", tone: "crit" };
}

function Panel({ title, subtitle, children, wide, tone, className = "", actions }) {
  return html`
    <section class=${`panel${wide ? " wide" : ""}${tone ? ` tone-${tone}` : ""} ${className}`}>
      <header>
        <h3>${title}</h3>
        ${subtitle && html`<span class="panel-sub">${subtitle}</span>`}
        ${actions && html`<div class="panel-actions">${actions}</div>`}
      </header>
      ${children}
    </section>`;
}

/**
 * A level against its limits: the fill carries the severity, the track is a
 * lighter step of the same colour, and the ticks are labelled.
 */
function Meter({ value, max, ticks = [], tone = "accent", label }) {
  const share = finite(value) && max > 0 ? Math.max(0, Math.min(1, value / max)) : 0;
  return html`
    <div class="meter" role="meter" aria-valuemin="0" aria-valuemax=${max}
         aria-valuenow=${finite(value) ? value : 0} aria-label=${label}>
      <div class="meter-track">
        <span class=${`meter-fill ${tone}`} style=${`width:${(share * 100).toFixed(1)}%`} />
        ${ticks.map((tick) => html`
          <i class="meter-tick" style=${`left:${Math.min(100, (tick.value / max) * 100)}%`}
             title=${tick.label} />`)}
      </div>
      ${ticks.length > 0 && html`
        <div class="meter-labels">
          ${ticks.map((tick) => html`
            <span style=${`left:${Math.min(100, (tick.value / max) * 100)}%`}>${tick.label}</span>`)}
        </div>`}
    </div>`;
}

function Explain({ on, children }) {
  return on ? html`<p class="explain">${children}</p>` : null;
}

// --- the guide: what is happening, what happens next -------------------------

function Stepper({ phases, current, title, off = [] }) {
  const index = phases.indexOf(current);
  return html`
    <ol class="stepper" aria-label=${title}>
      ${phases.map((phase, position) => html`
        <li key=${phase} title=${off.includes(phase) ? "needs equipment not installed yet" : null}
            class=${off.includes(phase) ? "off" : position < index ? "done" : position === index ? "now"
                    : position === index + 1 ? "next" : ""}>
          <span class="step-mark" aria-hidden="true">
            ${position < index ? "✓" : position + 1}</span>
          <span class="step-name">${fmt.phaseLabel(phase)}</span>
        </li>`)}
    </ol>`;
}

/** The rate of change the run engine judged on its last sample, %/min, or null. */
function engineRate(run) {
  const rate = run?.latest?.rate_pct_min;
  return finite(rate) ? rate : null;
}

/** What ends the current phase, and about when -- phase by phase. */
function nextStep(run, config, samples, pump) {
  const phase = run.phase;
  const inPhase = run.in_phase_s || 0;
  const limits = pumpLimits(pump);
  const pressure = [...samples].reverse().find((s) => finite(s.p_best))?.p_best;
  // The engine's own process timer, which starts when the step took effect
  // -- the plasma struck, the shutter opened -- not when the phase began.
  const timer = run.timer;
  if (timer) {
    if (!finite(timer.elapsed_s)) {
      return { text: `${timer.label} ${countdown(timer.total_s)} after the step above is confirmed.` };
    }
    const left = Math.max(0, timer.total_s - timer.elapsed_s);
    return { text: `${timer.label} in ${countdown(left)}`,
             progress: timer.total_s ? timer.elapsed_s / timer.total_s : null, countdown: left };
  }
  switch (phase) {
    case "rough": {
      const limit = pump?.interlocks?.motor_on?.limit_mbar ?? 0.8;
      const target = Math.min(Number(config.crossover_mbar) || limit, limit);
      const eta = A.etaToPressure(samples, target);
      return { text: `The turbopump starts at ${fmt.pressure(target, 1)} mbar`
        + (finite(pressure) ? ` — the chamber is at ${fmt.pressure(pressure, 2)} mbar` : "")
        + (eta !== null ? `, ${about(eta)} at the current rate.` : ".") };
    }
    case "crossover":
      return { text: "Waiting for the drive to accept the turbopump start." };
    case "spinup": {
      const target = limits.nominal ? A.JUDGEMENT.full_speed_fraction * limits.nominal : null;
      const eta = target ? A.etaToSpeed(samples, target) : null;
      return { text: target ? `Full speed (${fmt.integer(target)} Hz)`
        + (eta !== null ? ` in ${about(eta)}.` : " soon.") : "Waiting for full speed." };
    }
    case "basepressure": {
      // The engine's own rate -- the number it calls base pressure on --
      // never a second fit made here from the same samples.
      const rate = engineRate(run);
      const limit = Number(config.base_rate_pct) || 2;
      return { text: `Base pressure is called once the pressure improves by less than `
        + `${fmt.number(limit, 1)} %/min for ${fmt.number(Number(config.base_settle_min) || 3, 1)} min`
        + (rate !== null ? ` — now ${rate < 0 ? "falling" : "rising"} ${fmt.number(Math.abs(rate), Math.abs(rate) < 10 ? 1 : 0)} %/min.`
          : " — not enough readings yet to judge the rate.") };
    }
    case "backfill": {
      const working = Number(config.working_mbar);
      const deviation = finite(pressure) && working ? (pressure / working - 1) * 100 : null;
      return { text: `Holding ${fmt.pressure(working, 1)} mbar of argon`
        + (deviation !== null ? ` — now ${fmt.pressure(pressure, 2)} (${deviation >= 0 ? "+" : ""}${fmt.integer(deviation)} %)` : "")
        + `; it settles for ${fmt.number(Number(config.settle_min) || 2, 1)} min inside ½× to 2× before the plasma strikes.` };
    }
    case "presputter":
    case "deposit":
    case "recover":
      return { text: "Waiting for the step above to be confirmed." };
    case "holding":
      return { text: `Holding for ${fmt.duration(inPhase)}, until somebody stops the run.` };
    default:
      return { text: `${fmt.phaseLabel(phase)} for ${fmt.duration(inPhase)}.` };
  }
}

/**
 * Where the rig physically is, when no run is driving it.
 *
 * "Station on" is the drive's flag, not proof of roughing: on 2026-10-09 the
 * station read on with the backing pump off and the chamber open. So the
 * pressure has to be seen falling before this says the chamber is roughing.
 */
function idleStage(pump, gaugeState, pressure, rate) {
  const s = pump?.status || {};
  const sp = pump?.setpoints || {};
  const speed = Number(s.ActualSpd) || 0;
  const nominal = Number(pump?.identity?.NominalSpd) || 0;
  const limit = pump?.interlocks?.motor_on?.limit_mbar ?? 0.8;
  if (!pump?.connected) return { stage: null, text: "The panel has no contact with the turbopump drive." };
  if (sp.PumpgStatn !== true) {
    return { stage: null, text: gaugeState.atmosphere
      ? "Everything is off and the chamber is at atmosphere."
      : `Everything is off. The chamber is at ${finite(pressure) ? `${fmt.pressure(pressure, 2)} mbar` : "an unknown pressure"}.` };
  }
  if (sp.MotorPump !== true) {
    if (speed > 0) return { stage: "recover", text: `The turbopump is coasting down (${fmt.integer(speed)} Hz), backed by the forevacuum pump.` };
    if (finite(pressure) && pressure <= limit) {
      return { stage: "crossover", text: `Roughed to ${fmt.pressure(pressure, 2)} mbar: the turbopump may be started (Equipment → Pump), or a recipe can take over.` };
    }
    if (finite(rate) && rate < -0.5) {
      return { stage: "rough", text: "The backing pump is roughing the chamber; the turbopump is off." };
    }
    return { stage: null, text: finite(rate)
      ? "The station is on, but the pressure is not falling: is the backing pump running, and the chamber closed?"
      : "The station is on and the turbopump is off." };
  }
  if (A.atFullSpeed(speed, nominal)) {
    return { stage: "basepressure", text: `The turbopump is at full speed and the chamber at ${finite(pressure) ? `${fmt.pressure(pressure, 2)} mbar` : "an unknown pressure"}.` };
  }
  return { stage: "spinup", text: `The turbopump is spinning up (${fmt.integer(speed)} Hz).` };
}

function Guide({ state, samples, config, explain, onStop, stopBusy, may }) {
  const run = state?.run;
  const pump = state?.devices?.pump;
  const gaugeState = status.gaugeSummary(state?.devices?.gauge);
  const pressure = status.bestPressure(state?.devices?.gauge);

  if (run?.running) {
    const phases = (run.phases || []).filter((p) => p !== "done");
    const next = nextStep(run, config, samples, pump);
    return html`
      <section class="guide card">
        <div class="guide-head">
          <div>
            <div class="guide-kicker">${run.label} · ${run.mode === "auto" ? "automatic" : "guided"}</div>
            <div class="guide-title">${run.run?.name}</div>
          </div>
          <div class="guide-time">
            <span class="num">${fmt.duration(run.elapsed_s)}</span>
            <span class="muted small">elapsed · started ${fmt.clock(run.run?.started_at)}</span>
            ${onStop && html`
              <button class="danger small" data-test="dash-stop" disabled=${stopBusy}
                      onClick=${onStop}>${run.stopping ? "Stopping…"
                        : may === "signin" ? "Sign in to stop" : "Stop run"}</button>`}
          </div>
        </div>
        <${Stepper} phases=${phases} current=${run.phase} title="Run phases" />
        <div class="guide-now">
          <div>
            <div class="guide-label">Now: ${fmt.phaseLabel(run.phase)}
              <span class="muted"> · ${fmt.duration(run.in_phase_s)} in this step</span></div>
            ${explain && PHASE_HELP[run.phase] && html`<p class="explain">${PHASE_HELP[run.phase]}</p>`}
          </div>
          <div>
            <div class="guide-label">Next</div>
            <div class="guide-next">${next.text}</div>
            ${finite(next.progress) && html`
              <div class="bar big"><span style=${`width:${Math.min(100, next.progress * 100).toFixed(1)}%`} /></div>`}
          </div>
        </div>
      </section>`;
  }

  const last = run?.last;
  const where = idleStage(pump, gaugeState, pressure, A.latestRate(samples));
  const depositionOff = state?.unavailable?.deposition;
  const recent = last && last.ended_at && (state?.server_time || Date.now() / 1000) - last.ended_at < 12 * 3600;
  const lastSaid = last && fmt.runStatus({ status: last.status,
                                           result: { stopped: last.by_operator, message: last.message } });
  return html`
    <section class="guide card">
      <div class="guide-head">
        <div>
          <div class="guide-kicker">No run is recording</div>
          <div class="guide-title">${where.text}</div>
        </div>
      </div>
      ${recent && html`
        <div class=${`guide-last ${lastSaid === "completed" ? "good" : lastSaid === "failed" ? "crit" : lastSaid === "stopped" ? "idle" : "warn"}`}>
          <strong>Last run ${lastSaid}</strong> at ${fmt.clock(last.ended_at)}: ${last.name || last.label}.
          ${last.message && html`<span class="muted"> ${last.message}</span>`}
          <a href=${`#history/${last.id}`}>Open its record</a>
        </div>`}
      <div class="guide-label">The deposition route${where.stage ? " — the rig is here" : ""}</div>
      <${Stepper} phases=${ROUTE} current=${where.stage} title="The deposition route"
                  off=${depositionOff ? DEPOSITION_ONLY : []} />
      ${depositionOff && html`
        <p class="guide-off">From argon backfill on, the route needs hardware that is not on
          the rig yet — ${depositionOff.map((name) => name.toLowerCase()).join(", ")} — so deposition
          is switched off. Pumping to base pressure works today. <a href="#settings/rig">Rig equipment</a></p>`}
      ${explain && html`<p class="explain">Each step needs the one before it: the turbopump
        cannot start until the backing pump has roughed the chamber, and a plasma needs argon
        at the working pressure. A recipe walks the rig through them and checks every limit on
        the way.</p>`}
      <div class="guide-actions">
        <a class="btn primary" href="#tests">Start a run — Process tab</a>
        <a class="btn" href="#equipment/pump">Drive the pump by hand — Equipment tab</a>
      </div>
    </section>`;
}

// --- the vacuum scale ------------------------------------------------------------

const SCALE_MAX = 1e3;
const SCALE_MIN = 1e-8;
const REGIMES = [
  { from: 1e3, to: 1, label: "rough vacuum", short: "rough", shade: 1 },
  { from: 1, to: 1e-3, label: "medium vacuum", short: "medium", shade: 2 },
  { from: 1e-3, to: 1e-7, label: "high vacuum", short: "high", shade: 3 },
  { from: 1e-7, to: 1e-8, label: "UHV", short: "UHV", shade: 4 },
];
const LABEL_ROW_PX = 20;
const LABEL_GAP_PX = 8;

let measurer = null;
let family = null;

/** The rendered width of a line of text, so labels are placed, not hoped for. */
function textWidth(text, size, weight = 400) {
  try {
    if (!measurer) measurer = document.createElement("canvas").getContext("2d");
    if (!family) family = getComputedStyle(document.body).fontFamily || "sans-serif";
    measurer.font = `${weight} ${size}px ${family}`;
    return measurer.measureText(text).width;
  } catch {
    return text.length * size * 0.6;
  }
}

/**
 * Put each label over its x, shifted inward at the edges, in the lowest row
 * where it does not touch its neighbour. Returns the labels and the rows used.
 */
function stackLabels(items, width) {
  const ends = [];
  const placed = [...items].sort((a, b) => a.x - b.x).map((item) => {
    const left = Math.min(Math.max(0, item.x - item.w / 2), Math.max(0, width - item.w));
    let row = ends.findIndex((end) => left >= end + LABEL_GAP_PX);
    if (row < 0) { row = ends.length; ends.push(0); }
    ends[row] = left + item.w;
    return { ...item, left, row };
  });
  return { placed, rows: ends.length };
}

/**
 * The pressure on one line, from atmosphere (left) to ultra-high vacuum
 * (right) -- the direction a pump-down travels -- with the rig's own
 * landmarks on it: where the turbopump may start, where its gas limit is,
 * the working pressure, the last base pressure.
 *
 * The landmarks crowd each other: the gas limit and the working pressure are
 * a third of a decade apart. Their labels are measured and stacked; when
 * that would take more than two rows -- a phone -- each landmark gets a
 * number on the bar and a line in a key underneath instead.
 */
function VacuumScale({ pressure, atmosphere, marks, explain }) {
  const [ref, width] = useWidth(900);
  const fraction = (value) => {
    const clamped = Math.min(SCALE_MAX, Math.max(SCALE_MIN, value));
    return (Math.log10(SCALE_MAX) - Math.log10(clamped))
           / (Math.log10(SCALE_MAX) - Math.log10(SCALE_MIN));
  };
  const position = (value) => fraction(value) * 100;
  const known = finite(pressure) && pressure > 0;
  const nowAt = known ? fraction(pressure) : atmosphere ? 0 : null;
  const nowText = known ? `${fmt.pressure(pressure, 2)} mbar` : "atmosphere";

  const sorted = [...marks].filter((m) => finite(m.value) && m.value > 0)
    .sort((a, b) => b.value - a.value)
    .map((mark, index) => ({ ...mark, number: index + 1, x: fraction(mark.value) * width,
                             valueText: fmt.pressure(mark.value, 0) }));
  const full = stackLabels(sorted.map((mark) => ({
    ...mark, w: textWidth(`${mark.label} `, 12) + textWidth(mark.valueText, 12, 650) + 8,
  })), width);
  const compact = full.rows > 2;
  const labels = compact ? stackLabels(sorted.map((mark) => ({ ...mark, w: 18 })), width) : full;

  // Every other decade once a decade is narrower than its label.
  const stride = width / 11 < 44 ? 2 : 1;
  const decades = [];
  for (let power = 3; power >= -8; power -= stride) decades.push(power);

  const nowWidth = textWidth(nowText, 14, 700) + 18;
  const nowLeft = nowAt === null ? 0
    : Math.min(Math.max(0, nowAt * width - nowWidth / 2), Math.max(0, width - nowWidth));

  return html`
    <div class="vscale" ref=${ref} role="img"
         aria-label=${`Chamber pressure ${known ? nowText : atmosphere ? "at atmosphere" : "unknown"}, on a scale from atmosphere to ultra-high vacuum`}>
      <div class="vscale-marks" style=${`height:${labels.rows * LABEL_ROW_PX + 6}px`}>
        ${labels.placed.map((mark) => html`
          <div class=${`vscale-mark ${compact ? "badge" : ""}`} key=${mark.label}
               style=${`left:${mark.left}px; bottom:${mark.row * LABEL_ROW_PX}px; width:${mark.w}px`}>
            ${compact ? html`<span class="num">${mark.number}</span>`
              : html`<span>${mark.label} <b class="num">${mark.valueText}</b></span>`}
          </div>`)}
      </div>
      <div class="vscale-bar">
        ${REGIMES.map((regime) => {
          const span = (fraction(regime.to) - fraction(regime.from)) * width - 8;
          const text = textWidth(regime.label, 12.5) <= span ? regime.label
            : textWidth(regime.short, 12.5) <= span ? regime.short : "";
          return html`
            <div class=${`vscale-regime shade-${regime.shade}`} title=${regime.label}
                 style=${`left:${position(regime.from)}%; width:${position(regime.to) - position(regime.from)}%`}>
              <span>${text}</span>
            </div>`;
        })}
        ${sorted.map((mark) => html`<i class="vscale-line" style=${`left:${position(mark.value)}%`} />`)}
        ${nowAt !== null && html`<div class="vscale-now" style=${`left:${nowAt * 100}%`} />`}
      </div>
      <div class="vscale-ticks">
        ${nowAt !== null && html`
          <span class="vscale-now-label num" style=${`left:${nowLeft}px`}>${nowText}</span>`}
        ${decades.map((power) => html`
          <span class=${`vscale-tick ${power === 3 ? "first" : power === -8 ? "last" : ""}`}
                style=${`left:${position(10 ** power)}%`}>${power === 3 ? "1000" : `1e${power}`}</span>`)}
      </div>
      ${compact && html`
        <ol class="vscale-key">
          ${sorted.map((mark) => html`
            <li key=${mark.label}><span class="vscale-key-number num">${mark.number}</span>
              ${mark.label} <b class="num">${mark.valueText} mbar</b></li>`)}
        </ol>`}
      ${explain && html`<p class="explain">Each step to the right is ten times less gas. The
        backing pump alone reaches the rough range; the turbopump may only start below the
        crossover and takes the chamber into high vacuum. Sputtering then lets argon back in
        to the working pressure — four decades up from base — and the turbopump must not be
        asked to carry much more gas than its limit.</p>`}
    </div>`;
}

// --- health and activity -----------------------------------------------------------

function HealthItem({ tone, label, value, hint }) {
  const mark = tone === "good" ? "✓" : tone === "crit" ? "✕" : tone === "warn" ? "!" : "·";
  return html`
    <li class=${`health-item ${tone}`}>
      <span class="health-mark" aria-hidden="true">${mark}</span>
      <span class="health-label">${label}</span>
      <span class="health-value">${value}${hint && html`<span class="health-hint">${hint}</span>`}</span>
    </li>`;
}

function Health({ state, samples, calibration }) {
  const devices = state?.devices || {};
  const pump = devices.pump;
  const gauge = devices.gauge;
  const s = pump?.status || {};
  const limits = pumpLimits(pump);
  const loaded = samples.filter((x) => x.power > 0 && finite(x.voltage)).map((x) => x.voltage);
  const lowest = loaded.length ? Math.min(...loaded) : null;
  const alarms = state?.alarms?.alarms || [];
  const critical = alarms.filter((a) => a.level === "critical").length;
  const gaugeLine = (gauge?.readings || []).map((r) => {
    const id = (gauge?.identity?.gauges || []).find((g) => g.channel === r.channel);
    if (id && id.present === false) return null;
    return `ch ${r.channel}${id?.identifier ? ` ${id.identifier}` : ""}: ${r.valid ? "reading" : r.status_name}`;
  }).filter(Boolean).join(" · ");
  const age = calibration?.started_at
    ? ((state?.server_time || Date.now() / 1000) - calibration.started_at) / 86400 : null;

  return html`
    <ul class="health">
      <${HealthItem} tone=${!pump?.connected ? "crit" : pump.fault?.code ? "crit" : "good"}
        label="Turbopump drive"
        value=${!pump?.connected ? "no contact" : pump.fault?.code
          ? `fault ${pump.fault.code} — ${pump.fault.title}` : "no fault"} />
      <${HealthItem} tone=${s.OvTempPump || s.OvTempElec ? "crit" : pump?.connected ? "good" : "idle"}
        label="Temperature"
        value=${s.OvTempPump ? "pump over temperature" : s.OvTempElec
          ? "electronics over temperature" : "no over-temperature flag"}
        hint=" (the drive reports a verdict, not a value)" />
      <${HealthItem} tone=${lowest !== null && lowest < limits.supplyLow ? "warn" : pump?.connected ? "good" : "idle"}
        label="24 V supply"
        value=${`${fmt.number(s.DrvVoltage, 2)} V now`}
        hint=${lowest !== null ? ` · lowest under load ${fmt.number(lowest, 2)} V (rated ≥ ${fmt.number(limits.supplyLow, 1)})` : ""} />
      <${HealthItem} tone=${!gauge?.connected ? "crit" : "good"}
        label="Gauge" value=${gauge?.connected ? gaugeLine || "connected" : "no contact"} />
      <${HealthItem} tone=${critical ? "crit" : alarms.length ? "warn" : "good"}
        label="Alarms" value=${alarms.length ? `${alarms.length} to acknowledge` : "none waiting"} />
      <${HealthItem} tone=${state?.alarms?.auto_recover ? "good" : "warn"}
        label="Fault recovery" value=${state?.alarms?.auto_recover
          ? "automatic — the forevacuum pump is kept running" : "switched off"} />
      <${HealthItem} tone=${age === null ? "warn" : age > 30 ? "warn" : "good"}
        label="Base calibration"
        value=${calibration ? `${fmt.pressure(calibration.result?.base_mbar, 1)} mbar, ${fmt.duration(age * 86400)} ago`
          : "never run"}
        hint=${age !== null && age > 30 ? " — worth repeating" : ""} />
      <${HealthItem} tone="idle" label="Pump hours"
        value=${`${fmt.integer(s.OpHrsPump)} h`} />
    </ul>`;
}

const ACTIVITY = /run (completed|aborted|failed)|alarm|turbopump started|power-down|made safe|recovery|shutter (open|closed)|plasma on|operator|confirmed|by hand|found on another port/i;

/** Every alarm the supervisor has raised since the server started. */
function AlarmLog({ alarms }) {
  const recent = alarms?.recent || [];
  if (!recent.length) {
    return html`<p class="small muted">No alarms since the server started.</p>`;
  }
  return html`
    <details class="alarm-log-box">
      <summary>Alarm log — ${recent.length} since the server started · automatic
        recovery ${alarms?.auto_recover ? "on" : "off"}</summary>
      <div class="alarm-log">
        ${recent.map((alarm) => html`
          <div class="entry" key=${alarm.id}>
            <span class="mono small muted">${fmt.clock(alarm.raised_at)}</span>
            <${Pill} tone=${alarm.level === "critical" ? "crit" : "warn"}>
              ${alarm.active ? alarm.level : "cleared"}<//>
            <span><strong>${alarm.title}</strong>
              ${alarm.acknowledged ? html` <span class="small muted">· acknowledged</span>` : null}
              <div class="small muted">${alarm.detail}</div></span>
          </div>`)}
      </div>
    </details>`;
}

function Activity({ events, recent, alarms }) {
  // One line per happening: an abort is logged by the run as an error and
  // again as its outcome, and the supervisor raises an alarm for it too --
  // "Run 9 aborted: <recipe>. <reason>" against "Run aborted: <reason>".
  // Keyed on the run and the reason, so the three collapse into the first.
  const seen = new Set();
  const shown = events.filter((e) => e.level === "error" || e.level === "warning"
                                     || ACTIVITY.test(e.message))
    .filter((e) => {
      const reason = e.message.replace(/^ALARM:\s*/i, "")
        .replace(/^Run \d+ (aborted|failed): [^.]*\.\s*/i, "$1: ")
        .replace(/^Run (aborted|failed):\s*/i, "$1: ");
      const key = `${e.run_id ?? ""}|${reason.slice(0, 60).toLowerCase()}`;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, 8);
  return html`
    <div class="activity">
      ${shown.length
        ? html`<ul class="feed">
            ${shown.map((e) => html`
              <li key=${e.id} class=${e.level}>
                <span class="mono small muted">${fmt.clock(e.at)}</span>
                <span><span class="feed-text" title=${e.message}>${e.message.replace(/^ALARM:\s*/i, "")}</span>
                  ${e.run_id ? html` <a class="small" href=${`#history/${e.run_id}`}>run ${e.run_id}</a>` : null}</span>
              </li>`)}
          </ul>`
        : html`<${Empty}>Nothing has happened yet today.<//>`}
      <${AlarmLog} alarms=${alarms} />
      ${recent.length > 0 && html`
        <div class="scroll-x">
          <table class="runs-mini">
            <thead><tr><th>Run</th><th>When</th><th>Type</th><th>Status</th><th>Result</th></tr></thead>
            <tbody>
              ${recent.map((r) => html`
                <tr key=${r.id} onClick=${() => { location.hash = `history/${r.id}`; }}>
                  <td class="num">${r.id}</td>
                  <td>${fmt.dateTime(r.started_at)}</td>
                  <td>${fmt.titleCase(r.procedure)}</td>
                  <td><${Pill} tone=${fmt.STATUS_TONE[fmt.runStatus(r)] || "idle"}>${fmt.runStatus(r)}<//></td>
                  <td class="num">${headline(r)}</td>
                </tr>`)}
            </tbody>
          </table>
        </div>`}
    </div>`;
}

// --- the view ---------------------------------------------------------------------

export function DashboardView({ state, live, kiosk = false, onStop = null, stopBusy = false,
                               may = "yes" }) {
  const [recent, setRecent] = useState([]);
  const [calibration, setCalibration] = useState(null);
  const [events, setEvents] = useState([]);
  // Off by default on a kiosk: the wall screen is 600 px tall, and its
  // readers have seen the explanations before. Anyone may switch them on.
  const [explain, setExplain] = useStored("vacuum-lab-explain", !kiosk);
  const [windowS, setWindowS] = useStored("vacuum-lab-dash-window", 3600);
  const [wall, setWall] = useState(false);
  const procedures = useCatalogue();

  useEffect(() => {
    api.runs({ limit: 6 })
      .then((data) => setRecent(data.runs || []))
      .catch(() => setRecent([]));
    api.runs({ procedure: "base_calibration", status: "completed", limit: 1 })
      .then((data) => setCalibration((data.runs || [])[0] || null))
      .catch(() => setCalibration(null));
  }, [state?.run?.running, state?.run?.last?.id]);

  useEffect(() => {
    let cancelled = false;
    const load = () => api.events(80)
      .then((data) => { if (!cancelled) setEvents(data.events || []); })
      .catch(() => {});
    load();
    const timer = setInterval(load, 10000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  // Full screen for a wall display: the whole page, so the alarm bar stays.
  useEffect(() => {
    const onChange = () => {
      const on = !!document.fullscreenElement;
      setWall(on);
      document.body.classList.toggle("wall", on);
    };
    document.addEventListener("fullscreenchange", onChange);
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      document.body.classList.remove("wall");
    };
  }, []);
  const toggleWall = () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else document.documentElement.requestFullscreen?.().catch(() => {});
  };

  const devices = state?.devices || {};
  const pump = devices.pump;
  const gauge = devices.gauge;
  const run = state?.run;
  const history = useHistory(windowS);
  const recording = !!(run?.running && live?.samples?.length);
  const samples = recording ? live.samples : history.samples;
  const domain = !recording && history.now ? [history.now - windowS, history.now] : null;
  const gaugeNames = Object.fromEntries((gauge?.identity?.gauges || [])
    .filter((g) => g.present !== false).map((g) => [g.channel, g.identifier]));
  const actuators = state?.actuators || [];
  const actuator = (key) => actuators.find((a) => a.key === key);
  const config = useMemo(
    () => (run?.running ? effectiveConfig(run.procedure, run.run?.config, procedures) : {}),
    [run?.running, run?.procedure, run?.run?.id, procedures]);

  const pressure = status.bestPressure(gauge);
  const gaugeState = status.gaugeSummary(gauge);
  // Problems first; otherwise, while a run is going, the step it is on --
  // "Deposition" says more on a wall than "Running at 1499 Hz".
  const rigSummary = status.overallSummary(devices);
  const summary = run?.running && !["warn", "crit"].includes(rigSummary.tone)
    ? { tone: "good", headline: `${fmt.phaseLabel(run.phase)} — ${run.run?.name || run.label}`,
        detail: `${run.label}. ${rigSummary.headline}.` }
    : rigSummary;
  const s = pump?.status || {};
  const limits = pumpLimits(pump);
  const rotor = rotorState(pump);
  const speed = Number(s.ActualSpd) || 0;
  // While a run judges the curve, quote its number; otherwise the history's.
  const judged = run?.running ? engineRate(run) : null;
  const rate = useMemo(() => judged ?? A.latestRate(samples), [samples, judged]);
  const spinEta = rotor.word === "accelerating" && limits.nominal
    ? A.etaToSpeed(samples, A.JUDGEMENT.full_speed_fraction * limits.nominal) : null;

  const pressureTrend = samples.map((x) => x.p_best).filter(Boolean);
  const speedTrend = samples.map((x) => x.speed).filter(Number.isFinite);
  const powerTrend = samples.map((x) => x.power).filter(Number.isFinite);
  const parts = fmt.pressureParts(pressure, 2);
  const power = Number(s.DrvPower) || 0;
  const loadText = rotor.word === "accelerating" ? "Run-up uses the drive's maximum power — normal."
    : rotor.word === "held down by gas load" ? "At the drive's limit: more gas than the pump can carry."
    : rotor.word === "at full speed"
      ? (power >= limits.powerFull * 0.8 ? "Heavy gas load — close to the limit at full speed."
         : power >= 30 ? "Carrying a gas load." : "Light load: bearing friction and a little gas.")
    : "The motor is not being driven.";
  const reference = calibration?.result?.base_mbar;
  // Against base pressure only when no gas is being let in on purpose: at the
  // argon working pressure the chamber is meant to be 200 times above it.
  const processGas = (run?.running && ["backfill", "presputter", "deposit"].includes(run.phase))
    || (actuator("gas")?.actual || 0) > 0;
  const ratio = finite(pressure) && finite(reference) && rotor.word === "at full speed"
    && !processGas ? pressure / reference : null;

  // Landmarks on the vacuum scale, from the rig's own limits and this run.
  const crossover = pump?.interlocks?.motor_on?.limit_mbar ?? 0.8;
  const marks = [
    { value: Math.min(Number(config.crossover_mbar) || crossover, crossover),
      label: "turbopump may start", tone: "muted" },
    { value: 1e-2, label: "turbopump's gas limit", tone: "warn" },
    { value: run?.procedure === "deposition" ? Number(config.working_mbar) : 5e-3,
      label: run?.procedure === "deposition" ? "this run's working pressure" : "typical working pressure",
      tone: "accent" },
    finite(reference) && { value: reference, label: "last base pressure", tone: "good" },
  ].filter(Boolean);

  const refs = run?.running
    ? pressureRefs(run.procedure, config, pump)
    : [{ axis: "y", value: crossover, tone: "muted", label: `turbopump may start ≤ ${fmt.pressure(crossover, 0)}` },
       finite(reference) && { axis: "y", value: reference, tone: "good", label: `last base ${fmt.pressure(reference, 1)}` }]
      .filter(Boolean);
  const phases = useMemo(() => (recording ? A.phaseSpans(samples, fmt.phaseLabel) : []),
                         [recording, samples]);

  const gas = actuator("gas");
  const magnetron = actuator("magnetron");
  const gate = actuator("gate");
  const shutter = actuator("shutter");
  const plasmaOn = (magnetron?.actual || 0) > 0;
  const shown = (a) => (!a ? "—" : a.installed === false ? html`<span class="muted">not installed</span>`
    : a.available || a.confirmed ? a.display : a.pending ? `asked: ${a.pending}` : "—");
  const notInstalled = actuators.filter((a) => a.installed === false).length;
  const manualCount = actuators.filter((a) => a.installed !== false && !a.available).length;

  return html`
    <div class=${`dashboard${wall ? " is-wall" : ""}`}>
      <div class=${`dash-headline ${summary.tone}`}>
        <div>
          <div class="dash-state">${summary.headline}</div>
          <div class="dash-detail">${summary.detail}</div>
        </div>
        <div class="dash-side">
          <div class="dash-clock num">${fmt.clock(state?.server_time)}</div>
          ${run?.running
            ? html`<span class="pill accent"><span class="dot" />${fmt.phaseLabel(run.phase)}</span>`
            : html`<span class="pill idle"><span class="dot" />No run</span>`}
          <div class="dash-tools">
            <button class="small" aria-pressed=${explain ? "true" : "false"}
                    onClick=${() => setExplain(!explain)}
                    title="Add a sentence to every figure, for whoever is new to the rig">
              ${explain ? "Explanations on" : "Explain"}</button>
            <button class="small" onClick=${toggleWall}
                    title="Fill the screen — for a monitor on the lab wall">
              ${wall ? "Exit full screen" : "Full screen"}</button>
          </div>
        </div>
      </div>

      <div class="dash-top">
      <${Guide} state=${state} samples=${samples} config=${config} explain=${explain}
                onStop=${onStop} stopBusy=${stopBusy} may=${may} />

      <div class="dash-grid four">
        <${Panel} title="Chamber pressure" tone=${regimeTone(pressure)}
                  subtitle=${gauge?.connected ? "" : "gauge offline"}>
          <div class="stat-body">
            <div class="stat-figure pressure">
              ${finite(pressure)
                ? html`
                  <span class="stat-value">${parts.mantissa}</span>
                  <span class="stat-exp">×10<sup>${parts.exponent}</sup></span>
                  <span class="stat-unit">mbar</span>`
                : html`<span class="stat-value small-value">${gaugeState.atmosphere ? "atmosphere" : "—"}</span>`}
            </div>
            ${pressureTrend.length > 1 && html`
              <${Sparkline} values=${pressureTrend} log=${true} color="--series-1" height=${38} />`}
          </div>
          <div class="stat-foot">
            ${finite(pressure) ? status.regime(pressure) : gaugeState.atmosphere ? "gauge above its range" : "no reading"}
            ${rate !== null && finite(pressure) && html` · ${Math.abs(rate) < 0.5 ? "steady"
              : `${rate < 0 ? "falling" : "rising"} ${fmt.number(Math.abs(rate), Math.abs(rate) < 10 ? 1 : 0)} %/min`}`}
            ${ratio !== null && html`<br /><span class=${ratio > 3 ? "warn-text" : ""}>
              ${ratio < 0.67 ? "below" : ratio < 1.5 ? "at"
                : `${fmt.number(ratio, ratio < 10 ? 1 : 0)}×`} the last base pressure
              (${fmt.pressure(reference, 1)})</span>`}
          </div>
          <${Explain} on=${explain}>Lower is emptier: atmosphere is about 1000 mbar${finite(reference)
            ? `, and the last base calibration reached ${fmt.pressure(reference, 0)} mbar` : ""}.<//>
        <//>

        <${Panel} title="Turbopump rotor" tone=${rotor.tone === "crit" ? "crit" : null}>
          <div class="stat-body">
            <div class="stat-figure">
              <span class="stat-value">${fmt.integer(speed)}</span>
              <span class="stat-unit">Hz</span>
            </div>
            ${speedTrend.length > 1 && html`
              <${Sparkline} values=${speedTrend} color="--series-3" height=${38} />`}
          </div>
          ${limits.nominal && html`
            <${Meter} value=${speed} max=${limits.nominal} label="Rotor speed"
                      tone=${rotor.tone === "crit" ? "crit" : rotor.tone === "good" ? "good" : "accent"}
                      ticks=${[{ value: limits.switchpoint, label: "80 %" }]} />`}
          <div class="stat-foot">${rotor.word}${limits.nominal ? ` · ${fmt.integer((speed / limits.nominal) * 100)} % of ${fmt.integer(limits.nominal)} Hz` : ""}
            ${spinEta !== null && ` · full speed ${about(spinEta)}`}</div>
          <${Explain} on=${explain}>The turbopump only pumps well at full speed. Held below
            the 80 % mark by too much gas, the drive trips (Err006).<//>
        <//>

        <${Panel} title="Drive load" tone=${rotor.word === "held down by gas load" ? "crit" : null}>
          <div class="stat-body">
            <div class="stat-figure">
              <span class="stat-value">${fmt.integer(power)}</span>
              <span class="stat-unit">W</span>
            </div>
            ${powerTrend.length > 1 && html`
              <${Sparkline} values=${powerTrend} color="--series-2" height=${38} />`}
          </div>
          <${Meter} value=${power} max=${limits.powerReduced + 10} label="Drive power"
                    tone=${power >= limits.powerFull && rotor.word !== "accelerating" ? "crit"
                      : power >= limits.powerFull * 0.8 && rotor.word !== "accelerating" ? "warn" : "accent"}
                    ticks=${[{ value: limits.powerFull, label: `${limits.powerFull} W` },
                             { value: limits.powerReduced, label: `${limits.powerReduced} W` }]} />
          <div class="stat-foot">${fmt.number(s.DrvCurrent, 2)} A at ${fmt.number(s.DrvVoltage, 2)} V · ${loadText}</div>
          <${Explain} on=${explain}>The power the drive puts into the rotor — the heat in the
            pump. At full speed it rises with the gas load; ${limits.powerFull} W is the limit
            for argon.<//>
        <//>

        <${Panel} title="Plasma and gas" tone=${plasmaOn ? "accent" : null}>
          <dl class="process-list">
            <dt>Magnetron</dt>
            <dd class="num">${plasmaOn ? `${fmt.integer(magnetron.actual)} W — plasma on` : shown(magnetron)}</dd>
            <dt>Shutter</dt><dd>${shown(shutter)}</dd>
            <dt>Argon</dt><dd class="num">${shown(gas)}</dd>
            <dt>Gate valve</dt><dd class="num">${shown(gate)}</dd>
          </dl>
          <div class="stat-foot">${notInstalled
            ? html`${notInstalled === actuators.length ? "Not on the rig yet" : `${notInstalled} not installed`} — deposition is switched off. <a href="#settings/rig">Rig equipment</a>`
            : manualCount ? `${manualCount} manual — a guided run asks you to do them`
            : "all under software control"}</div>
          <${Explain} on=${explain}>Argon is let in, the magnetron strikes a plasma that
            sputters the target, and the shutter decides whether the sample is being coated.<//>
        <//>
      </div>
      </div>

      <${Panel} title="Where the chamber is" wide=${true}
                subtitle="from atmosphere to ultra-high vacuum">
        <${VacuumScale} pressure=${pressure} atmosphere=${gaugeState.atmosphere}
                        marks=${marks} explain=${explain} />
      <//>

      <${Panel} title=${recording ? "This run" : "The rig"} wide=${true}
                subtitle=${recording ? "live, with the run's phases and limits"
                  : "recorded whether or not a run is going"}
                actions=${!recording && html`<${WindowPicker} value=${windowS} onChange=${setWindowS} />`}>
        ${samples.length
          ? html`<${RigStack} samples=${samples} x=${recording ? "elapsed" : "clock"}
                             domain=${domain} pump=${pump} gaugeNames=${gaugeNames}
                             panels=${["pressure", "speed", "power"]} phases=${phases}
                             pressureRefs=${refs} pressureHeight=${wall ? 340 : 280}
                             height=${wall ? 160 : 130} />`
          : html`<${Empty}>Nothing recorded yet.<//>`}
      <//>

      <div class="dash-grid two-up">
        <${Panel} title="Rig health">
          <${Health} state=${state} samples=${history.samples} calibration=${calibration} />
        <//>
        <${Panel} title="Recent activity">
          <${Activity} events=${events} recent=${recent} alarms=${state?.alarms} />
        <//>
      </div>
    </div>`;
}
