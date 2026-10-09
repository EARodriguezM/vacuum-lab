// The landing view: what is the rig doing, and is a test running?

import { html } from "../html.js";
import { Banner, Card, Empty, Pill, Tile } from "../ui.js";
import { RigStack, useHistory } from "../rigcharts.js";
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
                        : position < index ? " done" : ""}`}>${fmt.phaseLabel(phase)}</span>`)}
      </div>
      <div class="spread small muted">
        <span>Elapsed ${fmt.duration(run.elapsed_s)}</span>
        <span>${run.samples} samples · every ${fmt.number(run.interval_s, 0)}s</span>
      </div>
    <//>`;
}

/** Every alarm the supervisor has raised since the server started. */
function AlarmLog({ alarms }) {
  const recent = alarms?.recent || [];
  return html`
    <${Card} title="Alarm log"
             hint=${`Kept since the server started; every entry is also in the events of the run it happened in. Automatic recovery is ${alarms?.auto_recover ? "on" : "off"}.`}>
      ${recent.length
        ? html`
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
          </div>`
        : html`<${Empty}>No alarms since the server started.<//>`}
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

  const history = useHistory(3600);
  const recording = !!(run?.running && live?.samples?.length);
  const samples = recording ? live.samples : history.samples;
  const domain = !recording && history.now ? [history.now - 3600, history.now] : null;
  const bakeout = run?.running && run.procedure === "bakeout";
  const gaugeNames = Object.fromEntries((gauge?.identity?.gauges || [])
    .filter((g) => g.present !== false).map((g) => [g.channel, g.identifier]));

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
        <${Tile} label="Pump temperature"
                 value=${pumpStatus.TMSActTemp > 0 ? fmt.integer(pumpStatus.TMSActTemp) : "—"}
                 unit=${pumpStatus.TMSActTemp > 0 ? "°C" : ""}
                 foot=${pumpStatus.TMSActTemp > 0
                   ? (pumpStatus.Heating ? "TMS, heating on" : "TMS")
                   : "not measured — the TMS reads 0 with its heater off"} />
        <${Tile} label="Operating hours"
                 value=${fmt.integer(pumpStatus.OpHrsPump)} unit="h" />
        <${Tile} label="Fault"
                 value=${pumpStatus.ErrorCode && pumpStatus.ErrorCode !== "000000"
                   ? pumpStatus.ErrorCode : "none"}
                 tone=${pumpStatus.ErrorCode && pumpStatus.ErrorCode !== "000000"
                   ? "crit" : null} />
      </div>

      <${Card} title=${recording ? "This run" : "The last hour"}
               hint=${recording
                 ? "Live, from the run in progress. The Process tab has every chart for it."
                 : "Recorded by the server whether or not a run is going. The Pump and Gauge tabs go back 24 hours."}>
        ${samples.length
          ? html`<${RigStack} samples=${samples} x=${recording ? "elapsed" : "clock"}
                             domain=${domain} pump=${pump} gaugeNames=${gaugeNames}
                             panels=${bakeout ? ["pressure", "temperature"] : ["pressure", "speed"]}
                             pressureHeight=${230} height=${150} />`
          : html`<${Empty}>Nothing recorded yet. The server records every couple of
                 seconds once it has started.<//>`}
      <//>

      ${state?.alarms && html`<${AlarmLog} alarms=${state.alarms} />`}
    </div>`;
}
