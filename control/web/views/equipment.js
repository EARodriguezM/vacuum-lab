// Everything on the rig, one sub-tab each: the instruments the server has
// registered, then each piece of process hardware marked installed.
//
// The list is built from what the server reports, not written down here. A
// chamber with another pump or another gauge controller gets its own tabs
// without a change to this file: a known kind gets its full page (PumpView,
// GaugeView), anything else a plain one showing what its service reports.

import { html } from "../html.js";
import { useMemo } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill } from "../ui.js";
import { ChartPanel, DataTable, Plot } from "../chart.js";
import { WindowPicker, useHistory, useStored } from "../rigcharts.js";
import { PumpView } from "./pump.js";
import { GaugeView } from "./gauge.js";
import * as A from "../analysis.js";
import * as fmt from "../format.js";

/** Each actuator's column in the rolling history and in run samples. */
const ACTUATOR_COLUMN = {
  gas: "gas_sccm", gate: "gate_pct", magnetron: "magnetron_w", shutter: "shutter",
};

const ACTUATOR_ABOUT = {
  gas: "Admits argon to the chamber. A mass flow controller once wired; until then a needle valve set by hand.",
  gate: "Throttles between the chamber and the turbopump, so the chamber can sit at a working pressure while the pump runs flat out.",
  magnetron: "The sputter source and its power supply.",
  shutter: "The flap between target and substrate: closed while pre-sputtering cleans the target, open to deposit.",
};

/** The sub-tabs for this rig, in the order the server lists its devices. */
export function equipmentTabs(state) {
  const devices = state?.devices || {};
  const order = state?.order || Object.keys(devices);
  // Each with a mark and a word, never a colour alone.
  const instruments = order.filter((key) => devices[key]).map((key) => ({
    key, label: devices[key].label || key, kind: devices[key].kind,
    type: "device", tone: devices[key].connected ? "good" : "crit",
    mark: devices[key].connected ? "✓" : "✕",
    state: devices[key].connected ? "connected" : "no contact",
  }));
  const actuators = (state?.actuators || [])
    .filter((actuator) => actuator.installed !== false)
    .map((actuator) => ({
      key: actuator.key, label: actuator.label, type: "actuator", actuator,
      tone: actuator.available ? "good" : "idle",
      mark: actuator.available ? "✓" : "○",
      state: actuator.available ? "driven" : "manual",
    }));
  return [...instruments, ...actuators];
}

function ActuatorView({ actuator }) {
  const [seconds, setSeconds] = useStored("vacuum-lab-actuator-window", 3600);
  const history = useHistory(seconds);
  const column = ACTUATOR_COLUMN[actuator.key];
  const points = useMemo(
    () => (column ? A.column(history.samples, column) : []),
    [history.samples, column]);
  const series = useMemo(() => [{
    key: actuator.key, label: actuator.label, color: "--series-1", points,
    mark: actuator.key === "shutter" ? "step" : "line",
  }], [points, actuator.key, actuator.label]);
  const known = points.some(([, value]) => Number.isFinite(value));
  const format = actuator.key === "shutter"
    ? (value) => (value ? "open" : "closed")
    : (value) => fmt.number(value, 1);

  return html`
    <div class="grid" style="gap:16px">
      <${Card} title=${actuator.label} hint=${ACTUATOR_ABOUT[actuator.key] || ""}>
        ${!actuator.available && html`
          <${Banner} tone="warn">
            <strong>Manual.</strong> Nothing is wired to it yet, so the software
            cannot move it. A guided run stops and asks you to set it, and waits
            for Done; automatic mode stays off for any procedure that needs it.
          <//>`}
        <${Pairs} items=${[
          ["Control", actuator.available ? "driven by the software" : "by hand"],
          ["Now", actuator.display],
          actuator.pending
            ? ["Asked for", html`${actuator.pending} — <strong>not confirmed</strong>`]
            : null,
          ["Last change", actuator.changed_at ? fmt.dateTime(actuator.changed_at) : "—"],
          actuator.error ? ["Error", actuator.error] : null,
        ]} />
        <p class="hint">
          A manual actuator shows a value only once somebody confirms the step:
          showing what the software asked for as if it had happened is how a
          sample gets coated during a pre-sputter.
        </p>
      <//>

      <${Card} title="Recorded"
        actions=${html`<${WindowPicker} value=${seconds} onChange=${setSeconds} />`}>
        <${ChartPanel} title=${`${actuator.label}${actuator.unit ? ` (${actuator.unit})` : ""}`}
          readout=${known ? null : "not known"}
          table=${known ? () => html`<${DataTable} series=${series} unit=${actuator.unit}
                                                   x=${{ kind: "clock" }}
                                                   formatValue=${format} />` : null}>
          ${known
            ? html`<${Plot} series=${series} height=${150} x=${{ kind: "clock" }}
                            unit=${actuator.unit} formatValue=${format}
                            y=${{ zero: true }} />`
            : html`<p class="hint">
                No value has been recorded in this window. A manual actuator is
                only recorded once its step is confirmed during a run; a driven
                one, continuously.</p>`}
        <//>
      <//>
    </div>`;
}

/** Whatever a service reports, for an instrument with no page of its own. */
function DeviceView({ device }) {
  const identity = device.identity || {};
  return html`
    <${Card} title=${device.label || device.key}
      hint="This instrument has no page of its own yet; this is what its service reports.">
      ${!device.connected && html`
        <${Banner} tone="crit"><strong>No contact.</strong> ${device.error}<//>`}
      <${Pairs} items=${[
        ["Kind", device.kind],
        ["Port", html`<span class="mono">${device.port || "—"}</span>`],
        ["State", device.connected ? "connected" : "offline"],
        ...Object.entries(identity).slice(0, 12)
          .map(([key, value]) => [key, typeof value === "object"
            ? JSON.stringify(value) : String(value)]),
      ]} />
    <//>`;
}

/**
 * The Equipment tab. `sub` is the selected sub-tab's key (the part of the
 * hash after "#equipment/"); `pumpProps` and `gaugeProps` are handed to the
 * full pages unchanged.
 */
export function EquipmentView({ state, sub, onSelect, pumpProps, gaugeProps }) {
  const tabs = equipmentTabs(state);
  if (!tabs.length) {
    return html`<${Empty}>The server reports no instruments and no process hardware.<//>`;
  }
  const active = tabs.find((tab) => tab.key === sub) || tabs[0];

  let page;
  if (active.type === "actuator") {
    page = html`<${ActuatorView} actuator=${active.actuator} />`;
  } else if (active.kind === "tc600") {
    page = html`<${PumpView} ...${pumpProps} />`;
  } else if (active.kind === "tpg") {
    page = html`<${GaugeView} ...${gaugeProps} />`;
  } else {
    page = html`<${DeviceView} device=${{ key: active.key, ...state.devices[active.key] }} />`;
  }

  const hidden = (state?.actuators || []).filter((a) => a.installed === false);
  return html`
    <div class="grid" style="gap:12px">
      <nav class="tabs subtabs" aria-label="Equipment">
        ${tabs.map((tab) => html`
          <button key=${tab.key} aria-current=${tab.key === active.key ? "page" : null}
                  data-test=${`subtab-${tab.key}`} onClick=${() => onSelect(tab.key)}>
            <span class=${`dot-mark ${tab.tone}`} aria-hidden="true">${tab.mark}</span>${tab.label}
            ${tab.type === "actuator" && !tab.actuator.available
              ? html`<span class="off-tag">manual</span>`
              : tab.tone === "crit" ? html`<span class="off-tag">no contact</span>`
              : html`<span class="sr-only">, ${tab.state}</span>`}
          </button>`)}
      </nav>
      ${page}
      ${hidden.length > 0 && html`
        <p class="small muted">
          Not on the rig, so not listed: ${hidden.map((a) => a.label).join(", ")}.
          An admin marks equipment installed under${" "}
          <a href="#settings/rig">Settings → Rig</a>.
        </p>`}
    </div>`;
}
