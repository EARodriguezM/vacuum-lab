// The wall display: what the rig is doing, readable from across the lab.
//
// This is the view for somebody who wants the state of the process at a
// glance rather than controls to press. Design decisions follow from that:
//
//   * **Bigger type than the rest of the panel.** The audience is standing,
//     possibly several feet away, possibly wearing reading glasses. Nothing
//     here is smaller than 14px and the headline figures are enormous.
//   * **Every colour is accompanied by a word.** The pressure panel is tinted
//     by vacuum regime and also *says* "high vacuum". Status is never carried
//     by hue alone -- that rule matters more, not less, for an audience that
//     may not distinguish red from green.
//   * **One measure per panel.** Pressure spans fourteen decades, rotor speed
//     one, power two. Sharing an axis between any two of them would draw a
//     relationship that is not there.
//
// The sparkline behind each figure answers "which way is it going?", which is
// the question a bare number cannot.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Empty, Pill } from "../ui.js";
import { PressureChart, SpeedChart, Sparkline, TimeChart } from "../chart.js";
import { api } from "../api.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

/** Vacuum regime, and how the pressure panel should be tinted for it. */
function regimeTone(mbar) {
  if (mbar === null || mbar === undefined) return "idle";
  if (mbar > 1) return "warn";          // roughing
  if (mbar > 1e-4) return "accent";     // medium / working pressure
  return "good";                        // high vacuum
}

function Panel({ title, subtitle, children, wide, tone }) {
  return html`
    <section class=${`panel${wide ? " wide" : ""}${tone ? ` tone-${tone}` : ""}`}>
      <header>
        <h3>${title}</h3>
        ${subtitle && html`<span class="panel-sub">${subtitle}</span>`}
      </header>
      ${children}
    </section>`;
}

/** A headline figure with its trend behind it. */
function Stat({ title, value, unit, foot, tone, spark, sparkColor, log }) {
  return html`
    <${Panel} title=${title} tone=${tone}>
      <div class="stat-body">
        <div class="stat-figure">
          <span class="stat-value num">${value}</span>
          ${unit && html`<span class="stat-unit">${unit}</span>`}
        </div>
        ${spark?.length > 1 && html`
          <${Sparkline} values=${spark} log=${log}
                        color=${sparkColor || "--series-1"} height=${38} />`}
      </div>
      ${foot && html`<div class="stat-foot">${foot}</div>`}
    <//>`;
}

export function DashboardView({ state, live }) {
  const [recent, setRecent] = useState([]);
  const [reference, setReference] = useState(null);

  useEffect(() => {
    api.runs({ limit: 6 })
      .then((data) => setRecent(data.runs || []))
      .catch(() => setRecent([]));
    api.runs({ procedure: "base_calibration", status: "completed", limit: 1 })
      .then((data) => {
        const first = (data.runs || [])[0];
        setReference(first?.result?.base_mbar ?? null);
      })
      .catch(() => setReference(null));
  }, [state?.run?.running]);

  const devices = state?.devices || {};
  const pump = devices.pump;
  const gauge = devices.gauge;
  const run = state?.run;
  const samples = live?.samples || [];
  const actuators = state?.actuators || [];
  const actuator = (key) => actuators.find((a) => a.key === key);

  const pressure = status.bestPressure(gauge);
  const pumpStatus = pump?.status || {};
  const summary = status.overallSummary(devices);

  const pressureTrend = samples.map((s) => s.p_best).filter(Boolean);
  const speedTrend = samples.map((s) => s.speed).filter(Number.isFinite);
  const powerTrend = samples.map((s) => s.power).filter(Number.isFinite);

  const parts = fmt.pressureParts(pressure, 2);

  return html`
    <div class="dashboard">
      <div class=${`dash-headline ${summary.tone}`}>
        <div>
          <div class="dash-state">${summary.headline}</div>
          <div class="dash-detail">${summary.detail}</div>
        </div>
        <div class="dash-run">
          ${run?.running
            ? html`
              <span class="pill accent"><span class="dot" />
                ${fmt.titleCase(run.phase)}</span>
              <div class="dash-run-name">${run.run?.name}</div>
              <div class="dash-run-time">
                ${fmt.duration(run.elapsed_s)} elapsed</div>`
            : html`<span class="pill idle"><span class="dot" />Idle</span>`}
        </div>
      </div>

      ${run?.prompt && html`
        <div class="dash-prompt">
          <strong>Waiting for the operator:</strong> ${run.prompt.instruction}
        </div>`}

      <div class="dash-grid">
        <${Panel} title="Chamber pressure" tone=${regimeTone(pressure)}
                  subtitle=${gauge?.connected ? "" : "gauge offline"}>
          <div class="stat-body">
            <div class="stat-figure pressure">
              <span class="stat-value num">${parts.mantissa}</span>
              ${parts.exponent !== null && html`
                <span class="stat-exp">×10<sup>${parts.exponent}</sup></span>`}
              <span class="stat-unit">mbar</span>
            </div>
            ${pressureTrend.length > 1 && html`
              <${Sparkline} values=${pressureTrend} log=${true}
                            color="--series-1" height=${38} />`}
          </div>
          <div class="stat-foot">${status.regime(pressure) || "no reading"}</div>
        <//>

        <${Stat} title="Rotor speed"
                 value=${fmt.integer(pumpStatus.ActualSpd)} unit="Hz"
                 spark=${speedTrend} sparkColor="--series-3"
                 foot=${pump?.identity?.NominalSpd
                   ? `of ${pump.identity.NominalSpd} Hz nominal`
                   : "turbopump"} />

        <${Stat} title="Drive power"
                 value=${fmt.integer(pumpStatus.DrvPower)} unit="W"
                 spark=${powerTrend} sparkColor="--series-2"
                 foot=${`${fmt.number(pumpStatus.DrvCurrent, 2)} A at ${
                   fmt.number(pumpStatus.DrvVoltage, 1)} V`} />

        <${Stat} title="Argon flow"
                 value=${actuator("gas")?.actual ?? actuator("gas")?.requested ?? "—"}
                 unit="sccm"
                 foot=${actuator("gas")?.available
                   ? "mass flow controller" : "manual needle valve"} />

        <${Stat} title="Magnetron"
                 value=${actuator("magnetron")?.actual
                   ?? actuator("magnetron")?.requested ?? "—"}
                 unit="W"
                 tone=${(actuator("magnetron")?.actual || 0) > 0 ? "accent" : null}
                 foot=${(actuator("magnetron")?.actual || 0) > 0
                   ? "plasma on" : "off"} />

        <${Stat} title="Base pressure reference"
                 value=${reference ? fmt.pressure(reference, 1) : "—"}
                 unit=${reference ? "mbar" : ""}
                 foot=${reference
                   ? "from the last calibration"
                   : "run a base pressure calibration"} />
      </div>

      <${Panel} title="Pressure against time" wide=${true}
                subtitle=${run?.running
                  ? "live, this run" : "start a run to record"}>
        ${samples.length
          ? html`<${PressureChart} samples=${samples} height=${300} />`
          : html`<${Empty}>No run is recording.<//>`}
      <//>

      <div class="dash-grid two-up">
        <${Panel} title="Rotor speed">
          ${samples.length
            ? html`<${SpeedChart} samples=${samples} height=${180} />`
            : html`<${Empty}>No run is recording.<//>`}
        <//>
        <${Panel} title="Process hardware">
          ${actuators.length
            ? html`
              <table class="actuator-table">
                <tbody>
                  ${actuators.map((a) => html`
                    <tr key=${a.key}>
                      <td>${a.label}</td>
                      <td class="num">${a.display}</td>
                      <td>
                        ${a.pending
                          ? html`<${Pill} tone="warn">
                              awaiting ${a.pending}<//>`
                          : html`<${Pill} tone=${a.available ? "good" : "idle"}>
                              ${a.available ? "automatic" : "manual"}<//>`}
                      </td>
                    </tr>`)}
                </tbody>
              </table>`
            : html`<${Empty}>No process hardware configured.<//>`}
        <//>
      </div>

      <${Panel} title="Recent runs" wide=${true}>
        ${recent.length
          ? html`
            <div class="scroll-x">
              <table>
                <thead><tr><th>When</th><th>Run</th><th>Type</th>
                           <th>Status</th><th>Result</th></tr></thead>
                <tbody>
                  ${recent.map((r) => html`
                    <tr key=${r.id}>
                      <td>${fmt.dateTime(r.started_at)}</td>
                      <td class="wrap-cell">${r.name}</td>
                      <td>${fmt.titleCase(r.procedure)}</td>
                      <td><${Pill} tone=${r.status === "completed" ? "good"
                        : r.status === "running" ? "accent"
                        : r.status === "failed" ? "crit" : "warn"}>
                        ${r.status}<//></td>
                      <td class="num">${resultLine(r)}</td>
                    </tr>`)}
                </tbody>
              </table>
            </div>`
          : html`<${Empty}>Nothing recorded yet.<//>`}
      <//>
    </div>`;
}

function resultLine(run) {
  const result = run.result || {};
  if (result.base_mbar) return `base ${fmt.pressure(result.base_mbar, 1)} mbar`;
  if (result.ultimate_mbar) return `${fmt.pressure(result.ultimate_mbar, 1)} mbar`;
  if (result.leak_rate_mbar_l_s !== undefined && result.leak_rate_mbar_l_s !== null) {
    return `${fmt.pressure(result.leak_rate_mbar_l_s, 1)} mbar·L/s`;
  }
  if (result.deposition_s) return `${fmt.duration(result.deposition_s)} deposited`;
  return "—";
}
