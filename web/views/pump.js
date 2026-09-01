// Full control of the TC 600 drive unit and its accessory connectors.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill } from "../ui.js";
import { api } from "../api.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

const NO_ERROR = new Set(["000000", "000", "0", ""]);

/** Vent modes and backing-pump modes, as the drive unit numbers them. */
const VENT_MODES = [
  { value: 0, label: "Controlled venting (delayed)" },
  { value: 1, label: "No venting" },
  { value: 2, label: "Venting on" },
];
const BKP_MODES = [
  { value: 0, label: "Continuous" },
  { value: 1, label: "Intermittent" },
  { value: 2, label: "Delayed" },
];
const OUT4_MODES = [
  { value: 0, label: "Heating / TMS" },
  { value: 1, label: "Sealing gas valve" },
];

/** A button that refuses itself, with the interlock's own words as the reason. */
function Guarded({ action, label, interlocks, onRun, tone = "", disabled }) {
  const rule = interlocks?.[action];
  const blocked = rule && !rule.allowed;
  const reason = blocked
    ? `${rule.reason}${rule.wait ? ` (about ${rule.wait}s)` : ""}`
    : "";
  return html`
    <button class=${tone} disabled=${disabled || blocked} title=${reason}
            onClick=${() => onRun(action, label, reason)}>
      ${label}${blocked && rule.wait ? html` <span class="muted">${rule.wait}s</span>` : null}
    </button>`;
}

/** The card's standing advice, quoting the server's own crossover pressure. */
function stationHint(interlocks) {
  const limit = interlocks?.motor_on?.limit_mbar;
  const crossover = limit == null
    ? "the crossover pressure"
    : `${fmt.pressure(limit, 0)} mbar`;
  return `Order matters: rough with the backing pump alone until the chamber `
    + `reaches ${crossover}, and only then start the turbopump. Never stop `
    + `the station while the rotor is turning.`;
}

/** Positive confirmation that the chamber is roughed enough to cross over.
 *
 * Only rendered when the start is *allowed*: when it is held, the interlock
 * banner already quotes the pressure and the limit, and saying it twice in
 * two different wordings is how an operator ends up unsure which one is the
 * rule.
 */
function CrossoverNote({ interlocks, running }) {
  const rule = interlocks?.motor_on;
  if (running || !rule?.allowed || rule.limit_mbar == null) return null;
  const now = rule.pressure_mbar;
  if (now == null) return null;
  const text = `Chamber at ${fmt.pressure(now)} mbar, at or below the `
    + `${fmt.pressure(rule.limit_mbar, 0)} mbar crossover — `
    + `the turbopump may be started.`;
  return html`<div class="small muted">${text}</div>`;
}

function InterlockNotes({ interlocks }) {
  const blocked = Object.entries(interlocks || {})
    .filter(([, rule]) => !rule.allowed && rule.reason);
  if (!blocked.length) return null;
  return html`
    <div class="grid" style="gap:8px">
      ${blocked.map(([action, rule]) => html`
        <div class="banner warn small">
          <strong>${fmt.titleCase(action)} is held:</strong> ${rule.reason}
          ${rule.wait ? ` About ${rule.wait}s to go.` : ""}
        </div>`)}
    </div>`;
}

function Accessories({ data, busy, onWrite, reload }) {
  const [draft, setDraft] = useState({});

  useEffect(() => { setDraft({}); }, [data]);
  if (!data) return html`<${Empty}>Loading accessory settings…<//>`;
  if (!data.ok) return html`<${Banner} tone="crit">${data.error}<//>`;

  const values = data.values || {};
  const units = data.units || {};
  const writable = new Set(data.writable || []);
  const value = (name) => (name in draft ? draft[name] : values[name]);
  const set = (name, next) => setDraft((d) => ({ ...d, [name]: next }));
  const commit = (name) => onWrite(name, value(name));

  const NumberRow = ({ name, label, min, max, help }) => html`
    <label class="field">
      <span>${label}</span>
      <span class="suffix">
        <input type="number" min=${min} max=${max} value=${value(name) ?? ""}
               disabled=${!writable.has(name) || busy}
               onInput=${(e) => set(name, e.target.value)} />
        <span class="unit">${units[name] || ""}</span>
        <button class="small" disabled=${!writable.has(name) || busy
                  || !(name in draft)} onClick=${() => commit(name)}>Set</button>
      </span>
      ${help && html`<span class="help">${help}</span>`}
    </label>`;

  const SelectRow = ({ name, label, options, help }) => html`
    <label class="field">
      <span>${label}</span>
      <span class="suffix">
        <select value=${String(value(name) ?? "")} disabled=${!writable.has(name) || busy}
                onChange=${(e) => onWrite(name, Number(e.target.value))}>
          ${options.map((option) => html`
            <option value=${String(option.value)}>${option.label}</option>`)}
        </select>
      </span>
      ${help && html`<span class="help">${help}</span>`}
    </label>`;

  return html`
    <div class="grid two">
      <${Card} title="HEAT / TMS"
               hint=${values.HeatType === 1
                 ? "This unit is a TMS installation: a regulated system with a setpoint."
                 : "Conventional casing heater."}>
        <div class="row">
          <button disabled=${!writable.has("Heating") || busy}
                  class=${values.Heating ? "" : "primary"}
                  onClick=${() => onWrite("Heating", !values.Heating)}>
            ${values.Heating ? "Switch heating off" : "Switch heating on"}
          </button>
          <${Pill} tone=${values.TMSSteady ? "good" : "idle"}>
            ${values.TMSSteady ? "regulator engaged" : "not regulating"}<//>
        </div>
        <${Pairs} items=${[
          ["Actual temperature", `${fmt.integer(values.TMSActTemp)} °C`],
          ["Maximum", `${fmt.integer(values.TMSMaxTemp)} °C`],
        ]} />
        <${NumberRow} name="TMSHeatSVal" label="Setpoint" min=${30} max=${90}
                      help="The drive unit accepts 30–90 °C." />
        <${SelectRow} name="CfgOut4" label="Output use" options=${OUT4_MODES} />
      <//>

      <${Card} title="VENT"
               hint="Venting above 20% of nominal speed is refused: air at speed wrecks the rotor.">
        <div class="row">
          <button disabled=${!writable.has("EnableVent") || busy}
                  onClick=${() => onWrite("EnableVent", !values.EnableVent)}>
            ${values.EnableVent ? "Disable venting valve" : "Enable venting valve"}
          </button>
          <${Pill} tone=${values.EnableVent ? "warn" : "idle"}>
            ${values.EnableVent ? "valve released" : "valve locked"}<//>
        </div>
        <${SelectRow} name="VentMode" label="Mode" options=${VENT_MODES} />
        <${NumberRow} name="VentSpd" label="Venting speed threshold"
                      min=${0} max=${100} />
        <${NumberRow} name="VentTime" label="Valve open time" min=${0} max=${3600} />
      <//>

      <${Card} title="FV PUMP (backing pump)"
               hint="The backing pump has no independent on/off — it is switched by the pumping station. These settings govern how it behaves once running.">
        <${SelectRow} name="OpModeBKP" label="Operating mode" options=${BKP_MODES} />
        <${NumberRow} name="SwOnBKP" label="Switch-on threshold" min=${0} max=${9999} />
        <${NumberRow} name="SwOffBKP" label="Switch-off threshold" min=${0} max=${9999} />
      <//>

      <${Card} title="FAN"
               hint="The air cooling unit plugs into the drive unit's own FAN connector and is driven by the TC 600. A full sweep of parameters 0–999 found nothing for it, so it cannot be controlled over RS-485.">
        <${Empty}>Not controllable from here.<//>
      <//>

      ${data.missing?.length > 0 && html`
        <${Card} title="Not implemented by this firmware">
          <p class="hint">These parameters answered NO_DEF, so the drive unit
             does not have them: ${data.missing.join(", ")}.</p>
        <//>`}
    </div>`;
}

export function PumpView({ state, onCommand, onWriteParam, busy }) {
  const pump = state?.devices?.pump;
  const [accessories, setAccessories] = useState(null);
  const [speed, setSpeed] = useState("");

  const reload = () => api.accessories("pump").then(setAccessories)
    .catch((error) => setAccessories({ ok: false, error: error.message }));

  useEffect(() => { if (pump?.connected) reload(); }, [pump?.connected]);

  if (!pump) return html`<${Empty}>No pump is configured on this server.<//>`;
  if (!pump.connected) {
    return html`
      <${Banner} tone="crit">
        <strong>No contact with the pump.</strong> ${pump.error}
      <//>`;
  }

  const summary = status.pumpSummary(pump);
  const s = pump.status || {};
  const setpoints = pump.setpoints || {};
  const interlocks = pump.interlocks || {};
  const fault = s.ErrorCode && !NO_ERROR.has(String(s.ErrorCode).trim());

  const writeParam = async (name, value) => {
    await onWriteParam(name, value);
    reload();
  };

  return html`
    <div class="grid" style="gap:16px">
      <div class=${`summary ${summary.tone}`}>
        <span class="headline">${summary.headline}</span>
        <div class="small muted">${summary.detail}</div>
      </div>

      ${fault && html`
        <${Banner} tone="crit">
          <strong>Fault ${s.ErrorCode}.</strong> Clear the cause, then
          acknowledge it below.
        <//>`}

      <${Card} title="Station and turbopump" hint=${stationHint(interlocks)}>
        <div class="row">
          <${Guarded} action="on" label="Start pumping station" tone="primary"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy} />
          <${Guarded} action="off" label="Stop pumping station" tone="danger"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy} />
          <${Guarded} action="motor_on" label="Start turbopump"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy} />
          <${Guarded} action="motor_off" label="Stop turbopump"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy} />
        </div>
        <div class="row">
          <button disabled=${busy}
                  onClick=${() => onCommand(setpoints.Standby ? "standby_off" : "standby_on",
                                            setpoints.Standby ? "Leave standby" : "Enter standby")}>
            ${setpoints.Standby ? "Leave standby" : `Enter standby (${
              fmt.integer(setpoints.StdbySVal)}%)`}
          </button>
          <button disabled=${busy || !fault}
                  onClick=${() => onCommand("ack", "Acknowledge fault")}>
            Acknowledge fault
          </button>
        </div>
        <${CrossoverNote} interlocks=${interlocks}
                          running=${setpoints.MotorPump === true} />
        <${InterlockNotes} interlocks=${interlocks} />
      <//>

      <div class="grid two">
        <${Card} title="Speed setpoint"
                 hint="Applies only in rotation-speed setting mode; setting it switches the drive unit into that mode.">
          <div class="row">
            <label class="field">
              <span>Setpoint</span>
              <span class="suffix">
                <input type="number" min="20" max="100" step="1"
                       placeholder=${fmt.number(setpoints.SpdSVal, 0)}
                       value=${speed} onInput=${(e) => setSpeed(e.target.value)} />
                <span class="unit">%</span>
              </span>
              <span class="help">20–100% of nominal.</span>
            </label>
            <button class="primary" disabled=${busy || speed === ""}
                    onClick=${() => onCommand("speed",
                                              `Set the speed setpoint to ${speed}%`,
                                              "", Number(speed))}>Apply</button>
          </div>
          <${Pairs} items=${[
            ["Current setpoint", `${fmt.number(setpoints.SpdSVal, 1)} %`],
            ["Standby setpoint", `${fmt.integer(setpoints.StdbySVal)} %`],
            ["Mode", setpoints.OpModeTMP === 1 ? "speed setting" : "final speed"],
          ]} />
        <//>

        <${Card} title="Readings">
          <${Pairs} items=${[
            ["Actual speed", `${fmt.integer(s.ActualSpd)} Hz`],
            ["Setpoint speed", `${fmt.integer(s.SetRotSpd)} Hz`],
            ["Nominal speed", `${fmt.integer(pump.identity?.NominalSpd)} Hz`],
            ["Drive current", `${fmt.number(s.DrvCurrent, 2)} A`],
            ["Drive voltage", `${fmt.number(s.DrvVoltage, 1)} V`],
            ["Drive power", `${fmt.integer(s.DrvPower)} W`],
            ["Operating hours", `${fmt.integer(s.OpHrsPump)} h`],
            ["Firmware", pump.identity?.FwVersion || "—"],
            ["Drive unit", pump.identity?.ElecName || "—"],
          ]} />
          <div class="row">
            ${s.PumpAccel && html`<${Pill} tone="warn">accelerating<//>`}
            ${s.SetSpdAtt && html`<${Pill} tone="good">at setpoint<//>`}
            ${s.SpdSwPtAtt && html`<${Pill} tone="good">switchpoint reached<//>`}
            ${s.OvTempPump && html`<${Pill} tone="crit">pump over temperature<//>`}
            ${s.OvTempElec && html`<${Pill} tone="crit">electronics over temperature<//>`}
          </div>
        <//>
      </div>

      <${Card} title="Accessory connectors"
               actions=${html`<button class="small" onClick=${reload}>Refresh</button>`}
               hint="Read on demand rather than in the status poll: fourteen extra round trips per refresh.">
        <${Accessories} data=${accessories} busy=${busy} onWrite=${writeParam}
                        reload=${reload} />
      <//>
    </div>`;
}
