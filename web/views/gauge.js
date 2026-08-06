// Full control of the TPG gauge controller.
//
// Everything the mnemonic protocol exposes on a two-channel unit is reachable
// here: pressures and their status codes, which gauge is fitted to each
// channel, gauge on/off, degas, the measurement filter, the display unit, the
// four switching functions, and the error register. The capability probe is
// here too, because on a model whose manual is not in the repository, asking
// the firmware what it implements beats trusting a table.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill, Tile } from "../ui.js";
import { api } from "../api.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

const UNITS = [
  { value: 0, label: "mbar" }, { value: 1, label: "Torr" },
  { value: 2, label: "Pa" }, { value: 3, label: "micron" },
  { value: 4, label: "hPa" }, { value: 5, label: "Volt" },
];
const FILTERS = [
  { value: 0, label: "fast" }, { value: 1, label: "normal" },
  { value: 2, label: "slow" },
];

/** Status codes 1–6 are the gauge explaining why its number means nothing. */
function statusTone(reading) {
  if (reading.valid) return "good";
  if (reading.status === 1 || reading.status === 4) return "idle";
  if (reading.status === 2) return "warn";
  return "crit";
}

function ChannelCard({ reading, gauge, sensor, filter, busy, onCommand,
                       supports }) {
  const channel = reading.channel;
  // The server decides what "on" means: the real TPG 252 A reports 3 for a
  // live gauge, not the 2 the family documentation shows.
  const isOn = sensor?.on === true;
  const switchable = sensor?.code !== 0;
  const hasDegas = supports?.degas !== false;
  const absent = gauge && gauge.present === false;

  return html`
    <${Card} title=${`Channel ${channel}`}
             hint=${absent
               ? "No gauge is plugged into this channel."
               : gauge ? `${gauge.identifier} — ${gauge.description}` : ""}>
      <div class="tiles">
        <${Tile} label="Pressure" accent=${reading.valid}
                 value=${reading.valid ? fmt.pressure(reading.mbar, 2) : "—"}
                 unit=${reading.valid ? "mbar" : ""}
                 foot=${reading.valid
                   ? status.regime(reading.mbar)
                   : reading.status_text} />
        <${Tile} label="Raw reading"
                 value=${reading.valid ? fmt.pressure(reading.value, 3) : "—"}
                 unit=${reading.unit}
                 foot=${`status ${reading.status}`} />
      </div>

      <div class="row">
        <${Pill} tone=${statusTone(reading)}>${reading.status_name}<//>
        ${sensor && html`<${Pill} tone=${isOn ? "good" : "idle"}>
          gauge ${sensor.state}<//>`}
        ${filter && html`<${Pill} tone="idle">filter ${filter.state}<//>`}
      </div>

      <div class="row">
        <button class="small" disabled=${busy || !switchable}
                title=${switchable ? "" : "This gauge cannot be switched over the bus."}
                onClick=${() => onCommand(
                  isOn ? "sensor_off" : "sensor_on",
                  `${isOn ? "Switch off" : "Switch on"} the gauge on channel ${channel}`,
                  "", { channel })}>
          ${isOn ? "Switch gauge off" : "Switch gauge on"}
        </button>
        ${hasDegas && html`
          <button class="small" disabled=${busy}
                  onClick=${() => onCommand("degas_on",
                    `Start degassing the gauge on channel ${channel}`,
                    "Degassing heats the gauge to drive off adsorbed gas. Only do it under high vacuum.",
                    { channel })}>Degas on</button>`}
        ${hasDegas && html`
          <button class="small" disabled=${busy}
                  onClick=${() => onCommand("degas_off",
                    `Stop degassing on channel ${channel}`, "", { channel })}>
            Degas off</button>`}
      </div>
      ${supports?.degas === false && html`
        <p class="hint">This controller does not implement degas — it refuses
          the command.</p>`}

      <label class="field">
        <span>Measurement filter</span>
        <select value=${String(filter?.code ?? 1)} disabled=${busy}
                onChange=${(e) => onCommand("filter",
                  `Set the channel ${channel} filter to ${
                    FILTERS.find((f) => f.value === Number(e.target.value))?.label}`,
                  "", { channel, value: Number(e.target.value) })}>
          ${FILTERS.map((option) => html`
            <option value=${String(option.value)}>${option.label}</option>`)}
        </select>
        <span class="help">Slower filtering steadies a noisy reading at the
          cost of response time.</span>
      </label>
    <//>`;
}

function Setpoints({ setpoints, statuses, busy, onCommand }) {
  const [draft, setDraft] = useState({});
  if (!setpoints?.length) {
    return html`<${Empty}>This unit reported no switching functions.<//>`;
  }
  const edit = (number, key, value) =>
    setDraft((d) => ({ ...d, [number]: { ...(d[number] || {}), [key]: value } }));

  // Thresholds are shown in exponential form whatever their magnitude.
  // JavaScript only switches to exponential below 1e-6 on its own, so a
  // column of raw numbers reads "0.001, 0.00001, 1e-7" -- three formats for
  // four rows, in a domain where miscounting zeros sets the wrong threshold.
  const merged = (entry) => ({
    ...entry,
    lower: Number(entry.lower).toExponential(2),
    upper: Number(entry.upper).toExponential(2),
    ...(draft[entry.setpoint] || {}),
  });

  // Models differ: some tie each switching function to a measurement channel,
  // the real TPG 252 A does not. Drop the column when it means nothing here.
  const hasChannel = setpoints.some((entry) => entry.channel != null);

  return html`
    <div class="scroll-x">
      <table>
        <thead>
          <tr><th>#</th><th>State</th>${hasChannel ? html`<th>Channel</th>` : null}
              <th>Lower</th><th>Upper</th><th></th></tr>
        </thead>
        <tbody>
          ${setpoints.map((entry) => {
            const value = merged(entry);
            const state = statuses?.find((s) => s.setpoint === entry.setpoint);
            const dirty = !!draft[entry.setpoint];
            return html`
              <tr key=${entry.setpoint}>
                <td>SP${entry.setpoint}</td>
                <td><${Pill} tone=${state?.closed ? "good" : "idle"}>
                  ${state?.closed ? "closed" : "open"}<//></td>
                ${hasChannel ? html`
                  <td>
                    <select value=${String(value.channel)} disabled=${busy}
                            onChange=${(e) => edit(entry.setpoint, "channel",
                                                   Number(e.target.value))}>
                      <option value="1">1</option><option value="2">2</option>
                    </select>
                  </td>` : null}
                <td><input type="text" style="width:110px" value=${value.lower}
                           disabled=${busy}
                           onInput=${(e) => edit(entry.setpoint, "lower",
                                                 e.target.value)} /></td>
                <td><input type="text" style="width:110px" value=${value.upper}
                           disabled=${busy}
                           onInput=${(e) => edit(entry.setpoint, "upper",
                                                 e.target.value)} /></td>
                <td>
                  <button class="small" disabled=${busy || !dirty}
                          onClick=${() => onCommand("setpoint",
                            `Set switching function ${entry.setpoint}`,
                            "A switching function drives a relay; check what it is wired to.",
                            {
                              setpoint: entry.setpoint,
                              // Omitted entirely when the model has no
                              // channel field; sending one would shift every
                              // parameter by a position.
                              ...(hasChannel
                                ? { channel: Number(value.channel) } : {}),
                              lower: fmt.parseExponent(value.lower),
                              upper: fmt.parseExponent(value.upper),
                            })}>Save</button>
                </td>
              </tr>`;
          })}
        </tbody>
      </table>
    </div>`;
}

function ProbeResult({ result }) {
  if (!result) return null;
  return html`
    <div class="grid" style="gap:10px">
      <p class="hint">Sent the bare query form of every safe mnemonic and
        recorded what came back. Trust this over the built-in table.</p>
      <div class="scroll-x">
        <table>
          <thead><tr><th>Mnemonic</th><th>Reply</th></tr></thead>
          <tbody>
            ${Object.entries(result.supported || {}).map(([mnemonic, data]) => html`
              <tr key=${mnemonic}><td class="mono">${mnemonic}</td>
                  <td class="mono">${data}</td></tr>`)}
          </tbody>
        </table>
      </div>
      ${result.refused?.length > 0 && html`
        <div class="small muted">
          <strong>Not implemented</strong> (answered NAK):
          <span class="mono">${result.refused.join(" ")}</span>
        </div>`}
      ${result.silent?.length > 0 && html`
        <div class="small muted">
          <strong>No answer</strong>:
          <span class="mono">${result.silent.join(" ")}</span>
        </div>`}
    </div>`;
}

export function GaugeView({ state, onCommand, busy }) {
  const gauge = state?.devices?.gauge;
  const [details, setDetails] = useState(null);
  const [probe, setProbe] = useState(null);
  const [probing, setProbing] = useState(false);

  const reload = () => api.gaugeDetails("gauge").then(setDetails)
    .catch((error) => setDetails({ ok: false, error: error.message }));

  useEffect(() => { if (gauge?.connected) reload(); }, [gauge?.connected]);

  if (!gauge) return html`<${Empty}>No gauge is configured on this server.<//>`;

  if (!gauge.connected) {
    return html`
      <div class="grid" style="gap:16px">
        <${Banner} tone="crit">
          <strong>No contact with the gauge controller.</strong> ${gauge.error}
        <//>
        <${Card} title="Bringing the link up"
                 hint="Nothing answered on the port, so work down this list.">
          <ol class="small" style="margin:0; padding-left:20px; line-height:1.7">
            <li>Confirm the TPG is powered and its RS-232 socket is the one cabled.</li>
            <li>Check the adapter is a <strong>true RS-232</strong> one. Many
                CH340 dongles sold as "USB to RS232" are TTL level and cannot
                talk to the instrument at all.</li>
            <li>Try a null-modem (crossed) cable: TX and RX must meet.</li>
            <li>Run <span class="mono">./tpgctl.py -p PORT diagnose</span> on
                the lab PC — it separates a dead adapter from a wrong cable
                from a wrong baud rate.</li>
          </ol>
        <//>
      </div>`;
  }

  const readings = gauge.readings || [];
  const sensors = gauge.sensors || [];
  const gauges = gauge.identity?.gauges || [];
  const filters = Array.isArray(details?.filter) ? details.filter : [];
  const errors = details?.errors;
  const summary = status.gaugeSummary(gauge);

  const runProbe = async () => {
    setProbing(true);
    try {
      const result = await onCommand("probe", "Probe the gauge's command set",
                                     "", null, { silent: true });
      if (result?.ok) setProbe(result.result);
    } finally {
      setProbing(false);
    }
  };

  return html`
    <div class="grid" style="gap:16px">
      <div class=${`summary ${summary.tone}`}>
        <div class="spread">
          <span class="headline">${summary.headline}</span>
          <${Pill} tone=${status.connectionTone(gauge)}>
            ${gauge.port} @ ${gauge.baudrate || "?"} baud<//>
        </div>
        <div class="small muted">${summary.detail}</div>
      </div>

      ${errors && errors.ok === false && html`
        <${Banner} tone="crit">
          <strong>Error register ${errors.word}:</strong>
          ${(errors.faults || []).join("; ")}
          <button class="small" style="margin-left:10px"
                  onClick=${() => onCommand("reset_errors", "Clear the latched errors")}>
            Clear</button>
        <//>`}

      <div class="grid two">
        ${readings.map((reading) => html`
          <${ChannelCard} key=${reading.channel} reading=${reading}
            gauge=${gauges.find((g) => g.channel === reading.channel)}
            sensor=${sensors.find((s) => s.channel === reading.channel)}
            filter=${filters.find((f) => f.channel === reading.channel)}
            supports=${details?.supports}
            busy=${busy} onCommand=${onCommand} />`)}
      </div>

      <div class="grid two">
        <${Card} title="Controller">
          <${Pairs} items=${[
            ["Firmware", gauge.identity?.firmware || "—"],
            ["Channels", gauge.identity?.channels ?? "—"],
            ["Port", gauge.port],
            ["Baud rate", gauge.baudrate || "—"],
            ["Display unit", gauge.unit || "—"],
            ["Error register", errors?.word || "—"],
          ]} />
          <label class="field">
            <span>Pressure unit</span>
            <select value=${String(gauge.identity?.unit_code ?? 0)} disabled=${busy}
                    onChange=${(e) => onCommand("unit",
                      `Change the display unit to ${
                        UNITS.find((u) => u.value === Number(e.target.value))?.label}`,
                      "Readings are always stored in millibar regardless, so past runs stay comparable.",
                      { value: Number(e.target.value) })}>
              ${UNITS.map((option) => html`
                <option value=${String(option.value)}>${option.label}</option>`)}
            </select>
            <span class="help">Affects the front panel and the raw bus value.
              This software converts to millibar for storage either way.</span>
          </label>
        <//>

        <${Card} title="Command set"
                 hint="No TPG 252 A manual is in this repository, so the table this software ships is the published family set. This asks the firmware itself."
                 actions=${html`
                   <button class="small" disabled=${busy || probing}
                           onClick=${runProbe}>
                     ${probing ? "Probing…" : "Probe the unit"}</button>`}>
          ${probe
            ? html`<${ProbeResult} result=${probe} />`
            : html`<${Empty}>Not probed yet. It sends only read-only queries
                   and changes nothing.<//>`}
        <//>
      </div>

      <${Card} title="Switching functions"
               hint="Each drives a relay that closes below its lower threshold and opens above the upper one."
               actions=${html`<button class="small" onClick=${reload}>Refresh</button>`}>
        <${Setpoints} setpoints=${details?.setpoints} statuses=${gauge.setpoints}
                      busy=${busy} onCommand=${onCommand} />
      <//>
    </div>`;
}
