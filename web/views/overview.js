// The landing view: what is the rig doing, and is a test running?

import { html } from "../html.js";
import { Banner, Card, Empty, Pill, Tile } from "../ui.js";
import { PressureChart, SpeedChart, TemperatureChart } from "../chart.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

function RunBanner({ run, onStop, onOpen }) {
  if (!run?.running) return null;
  const phases = run.phases || [];
  const index = phases.indexOf(run.phase);
  return html`
    <${Card} className="run-active"
      title=${`Running: ${run.run?.name || run.label}`}
      hint=${`${run.label} · started ${fmt.dateTime(run.run?.started_at)}`}
      actions=${html`
        <button class="small" onClick=${onOpen}>Open</button>
        <button class="small danger" onClick=${onStop}>Stop run</button>`}>
      <div class="phases">
        ${phases.map((phase, position) => html`
          <span class=${`step${phase === run.phase ? " now"
                        : position < index ? " done" : ""}`}>${fmt.titleCase(phase)}</span>`)}
      </div>
      <div class="spread small muted">
        <span>Elapsed ${fmt.duration(run.elapsed_s)}</span>
        <span>${run.samples} samples · every ${fmt.number(run.interval_s, 0)}s</span>
      </div>
    <//>`;
}

export function Overview({ state, live, onStop, onNavigate }) {
  const devices = state?.devices || {};
  const pump = devices.pump;
  const gauge = devices.gauge;
  const summary = status.overallSummary(devices);
  const pressure = status.bestPressure(gauge);
  const pumpStatus = pump?.status || {};
  const run = state?.run;

  const samples = live?.samples || [];
  const bakeout = run?.running && run.procedure === "bakeout";

  return html`
    <div class="grid" style="gap:16px">
      <div class=${`summary ${summary.tone}`}>
        <div class="spread">
          <span class="headline">${summary.headline}</span>
          <div class="row">
            <${Pill} tone=${status.connectionTone(pump)}
                     title=${pump?.error || ""}>Pump<//>
            <${Pill} tone=${status.connectionTone(gauge)}
                     title=${gauge?.error || ""}>Gauge<//>
          </div>
        </div>
        <div class="small muted">${summary.detail}</div>
      </div>

      ${!pump?.connected && pump && html`
        <${Banner} tone="crit">
          <strong>Pump offline.</strong> ${pump.error}
        <//>`}
      ${!gauge?.connected && gauge && html`
        <${Banner} tone="warn">
          <strong>Gauge offline.</strong> ${gauge.error}
        <//>`}

      <${RunBanner} run=${run} onStop=${onStop}
                    onOpen=${() => onNavigate("tests")} />

      <div class="tiles">
        <${Tile} label="Pressure" accent=${true}
                 value=${pressure !== null ? fmt.pressure(pressure, 2) : "—"}
                 unit=${pressure !== null ? "mbar" : ""}
                 foot=${status.regime(pressure)} />
        <${Tile} label="Rotor speed" value=${fmt.integer(pumpStatus.ActualSpd)}
                 unit="Hz"
                 foot=${pump?.identity?.NominalSpd
                   ? `nominal ${pump.identity.NominalSpd} Hz` : ""} />
        <${Tile} label="Drive power" value=${fmt.integer(pumpStatus.DrvPower)}
                 unit="W"
                 foot=${`${fmt.number(pumpStatus.DrvCurrent, 2)} A · ${
                   fmt.number(pumpStatus.DrvVoltage, 1)} V`} />
        <${Tile} label="TMS temperature"
                 value=${fmt.integer(pumpStatus.TMSActTemp)} unit="°C"
                 foot=${pumpStatus.Heating ? "heating on" : "heating off"} />
        <${Tile} label="Operating hours"
                 value=${fmt.integer(pumpStatus.OpHrsPump)} unit="h" />
        <${Tile} label="Fault"
                 value=${pumpStatus.ErrorCode && pumpStatus.ErrorCode !== "000000"
                   ? pumpStatus.ErrorCode : "none"}
                 tone=${pumpStatus.ErrorCode && pumpStatus.ErrorCode !== "000000"
                   ? "crit" : null} />
      </div>

      <${Card} title="Pressure"
               hint=${run?.running
                 ? "Live, from the run in progress."
                 : "Charts fill in while a test run is recording. Start one from the Process tab."}>
        ${samples.length
          ? html`<${PressureChart} samples=${samples} height=${230} />`
          : html`<${Empty}>No run is recording. Live pressure is shown in the tiles above;
                 start a run to record a curve.<//>`}
      <//>

      ${samples.length > 0 && html`
        <${Card} title=${bakeout ? "TMS temperature" : "Rotor speed"}>
          ${bakeout
            ? html`<${TemperatureChart} samples=${samples} height=${160} />`
            : html`<${SpeedChart} samples=${samples} height=${160} />`}
        <//>`}
    </div>`;
}
