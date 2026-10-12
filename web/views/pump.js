// Full control of the TC 600 drive unit and its accessory connectors.

import { html } from "../html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill, Tile } from "../ui.js";
import { ChartPanel, DataTable, Plot } from "../chart.js";
import { LoadCurve, RigStack, WindowPicker, downloadCsv, pumpLimits, useHistory,
         useStored } from "../rigcharts.js";
import { api } from "../api.js";
import { ActionList } from "../alarms.js";
import * as A from "../analysis.js";
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

/**
 * A button that refuses itself, with the interlock's own words as the reason.
 *
 * For an admin (`onOverride` given) a blocked button has a second one beside
 * it: the deliberate way past the rule, through a dialog that quotes the rule
 * and asks them to say they have checked the rig themselves. Nobody else is
 * offered it, and the server refuses an override from anyone else anyway.
 */
function Guarded({ action, label, interlocks, onRun, tone = "", disabled, onOverride }) {
  const rule = interlocks?.[action];
  const blocked = rule && !rule.allowed;
  const reason = blocked
    ? `${rule.reason}${rule.wait ? ` (about ${rule.wait}s)` : ""}`
    : "";
  return html`
    <span class="guarded">
      <button class=${tone} disabled=${disabled || blocked} title=${reason}
              onClick=${() => onRun(action, label, reason)}>
        ${label}${blocked && rule.wait ? html` <span class="muted">${rule.wait}s</span>` : null}
      </button>
      ${blocked && onOverride && !disabled && rule.rule && html`
        <button class="small danger" title="Admins only: waive this one rule and send it"
                onClick=${() => onOverride(action, label, reason, rule.rule)}>Override…</button>`}
    </span>`;
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
          ["Actual temperature", values.TMSActTemp > 0
            ? `${fmt.integer(values.TMSActTemp)} °C` : "not measured (reads 0)"],
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

/** What acknowledging does, said wherever it can be pressed. */
const ACK_NOTE = "Switches the turbopump motor off first, then clears the drive's "
  + "fault, so the rotor cannot restart into whatever tripped it. Make safe does "
  + "the same and also keeps the forevacuum pump running.";

const GAS_MODES = { 0: "heavy gases (argon)", 1: "light gases", 2: "helium" };
const VENT_MODE_NAMES = { 0: "controlled venting (delayed)", 1: "no venting", 2: "venting on" };
const BKP_MODE_NAMES = { 0: "continuous", 1: "intermittent", 2: "delayed" };

/**
 * Faults, and what happens after one.
 *
 * The policy, stated where the operator acts on it: after a drive fault the
 * panel makes the pump safe by itself -- motor off, fault acknowledged,
 * forevacuum pump kept on -- and never switches the forevacuum pump off or
 * restarts the turbopump. The drive's own protective settings sit beside it,
 * because they decide when a dragged rotor becomes an Err006.
 */
// `safeBusy` locks only Make safe, which every operator may press whoever
// holds control; switching automatic recovery is an admin's (`canAdmin`).
function FaultsCard({ pump, alarms, busy, safeBusy, canAdmin = true, onCommand,
                      onSecure, notify }) {
  const [diagnostics, setDiagnostics] = useState(null);
  const [loading, setLoading] = useState(false);
  const [showCodes, setShowCodes] = useState(false);

  const load = (fresh = false) => {
    setLoading(true);
    api.pumpDiagnostics("pump", fresh)
      .then(setDiagnostics)
      .catch((error) => setDiagnostics({ ok: false, error: error.message }))
      .finally(() => setLoading(false));
  };
  // Read again on a new fault code: the history is what it is about.
  useEffect(() => { if (pump?.connected) load(); },
            [pump?.connected, pump?.fault?.code]);

  const fault = pump?.fault?.code ? pump.fault : null;
  const auto = alarms?.auto_recover;
  const recovery = alarms?.recovering;
  const values = diagnostics?.values || {};
  const has = (name) => Object.prototype.hasOwnProperty.call(values, name);
  const toggleAuto = async () => {
    try {
      const result = await api.setAutoRecovery(!auto);
      notify(result.ok ? `Automatic recovery ${result.auto_recover ? "on" : "off"}.`
                       : result.error, result.ok ? "ok" : "error");
    } catch (problem) {
      notify(problem.message, "error");
    }
  };

  return html`
    <${Card} title="Faults and recovery"
      hint="After a drive fault the panel makes the pump safe by itself: turbopump motor off, fault acknowledged, pumping station — the forevacuum pump — kept on. It never switches the forevacuum pump off, and it never restarts the turbopump: that is your decision, through the crossover interlock."
      actions=${html`
        <button class="small" disabled=${loading} onClick=${() => load(true)}>
          ${loading ? "Reading…" : "Read the drive"}</button>`}>

      ${fault
        ? html`
          <div class="banner crit">
            <strong>Fault ${fault.code}: ${fault.title}.</strong> ${fault.cause}
            <div style="margin-top:6px"><strong>What to do:</strong> ${fault.remedy}</div>
          </div>`
        : html`<div class="small muted">No fault on the drive now.</div>`}

      <div class="row">
        <button class="primary" disabled=${(safeBusy ?? busy) || !pump?.connected}
                onClick=${onSecure}
                title="Motor off, fault acknowledged, forevacuum pump on">Make safe now</button>
        <button disabled=${busy || !fault}
                onClick=${() => onCommand("ack", "Acknowledge fault", ACK_NOTE)}>
          Acknowledge only</button>
        <span class=${`pill ${auto ? "good" : "warn"}`}>
          <span class="dot" />automatic recovery ${auto ? "on" : "off"}</span>
        <button class="small ghost" onClick=${toggleAuto} disabled=${!canAdmin}
                title=${canAdmin ? "" : "Switching automatic recovery is an admin's"}>
          ${auto ? "Switch off" : "Switch on"}</button>
      </div>

      ${recovery && html`
        <div class="small">
          <strong>${recovery.active ? "Recovering from" : "Last automatic recovery:"}</strong>
          fault ${recovery.code} at ${fmt.clock(recovery.started_at)}
          ${!recovery.active && html`<${Pill} tone=${recovery.ok ? "good" : "crit"}>
            ${recovery.ok ? "confirmed" : "not confirmed"}<//>`}
          <${ActionList} actions=${recovery.actions} />
          ${recovery.error && html`<span class="muted">${recovery.error}</span>`}
        </div>`}

      ${diagnostics?.ok === false && html`<${Banner} tone="warn">${diagnostics.error}<//>`}
      ${diagnostics?.ok && html`
        <div class="grid two">
          <div>
            <h3 class="sub-head">The drive's own protection</h3>
            <${Pairs} items=${[
              has("RUTimeCtrl") && ["Run-up time monitoring", values.RUTimeCtrl ? "on" : "off"],
              has("RUTimeSVal") && ["Run-up time limit", `${fmt.integer(values.RUTimeSVal)} min (factory 15)`],
              has("SpdSwPt") && ["Speed switchpoint", `${fmt.integer(values.SpdSwPt)} % (factory 80)`],
              has("GasMode") && ["Gas mode", GAS_MODES[values.GasMode] || values.GasMode],
              has("PwrSVal") && ["Power limit", `${fmt.integer(values.PwrSVal)} %`],
              has("VentMode") && ["Venting", VENT_MODE_NAMES[values.VentMode] || values.VentMode],
              has("EnableVent") && ["Venting valve", values.EnableVent ? "released" : "locked"],
              has("OpModeBKP") && ["Backing pump mode", BKP_MODE_NAMES[values.OpModeBKP] || values.OpModeBKP],
              has("PumpCycles") && ["Start/stop cycles", fmt.integer(values.PumpCycles)],
            ]} />
            <p class="hint">
              Err006 comes from these: with monitoring on, a rotor below the switchpoint
              once the run-up time has run out (counted from motor start) trips the drive.
              ${values.VentMode === 0 && values.EnableVent
                ? " With this venting setting, switching the pumping station off vents the chamber once the rotor is below 50 % — which is why a fault never switches it off."
                : ""}
            </p>
            ${diagnostics.missing?.length > 0 && html`
              <p class="hint">Not implemented by this firmware: ${diagnostics.missing.join(", ")}.</p>`}
          </div>
          <div>
            <h3 class="sub-head">The drive's last faults</h3>
            ${diagnostics.read_s_ago > 0 && html`
              <p class="small muted">As read ${Math.round(diagnostics.read_s_ago)} s ago.</p>`}
            ${diagnostics.history?.length
              ? html`<ol class="fault-history">
                  ${diagnostics.history.map((entry) => html`
                    <li key=${entry.position}><strong class="mono">${entry.code}</strong>
                      ${entry.title}</li>`)}
                </ol>`
              : html`<p class="small muted">${has("ErrHist1") ? "No faults recorded."
                : "This firmware does not report its error history."}</p>`}
            <button class="small ghost" onClick=${() => setShowCodes(!showCodes)}>
              ${showCodes ? "Hide" : "Show"} what each error code means</button>
          </div>
        </div>
        ${showCodes && html`
          <div class="scroll-x">
            <table>
              <thead><tr><th>Code</th><th>Meaning</th><th>Usual cause</th><th>What to do</th></tr></thead>
              <tbody>
                ${Object.values(diagnostics.codes || {}).map((entry) => html`
                  <tr key=${entry.code}>
                    <td class="mono">${entry.code}</td><td>${entry.title}</td>
                    <td class="wrap-cell">${entry.cause}</td>
                    <td class="wrap-cell">${entry.remedy}</td>
                  </tr>`)}
              </tbody>
            </table>
            <p class="hint">Err006 is pinned down by this pump's manual (PM 0504 BE, in the
              repository). The others follow Pfeiffer's error list for its TC drives; the
              TC 600's own list is in PM 0547 BN.</p>
          </div>`}`}
    <//>`;
}

/**
 * What the pump has been doing, whether or not a run was recording.
 *
 * The stack answers "what happened, and in what order"; the tiles above it
 * answer the questions a pump's health is judged by, so they can be read
 * without hovering anything: how long the last run-up took against the
 * rated 2.5 min, what the bearings cost at full speed with nothing to pump,
 * how far the supply sagged, and how long the gas load held the rotor down.
 */
function PumpTrends({ pump, gauge }) {
  const [seconds, setSeconds] = useStored("vacuum-lab-pump-window", 3600);
  const [metric, setMetric] = useStored("vacuum-lab-load-metric", "current");
  const [paused, setPaused] = useState(null);
  const [cursor, setCursor] = useState(null);
  const history = useHistory(seconds);
  const samples = paused || history.samples;
  const limits = pumpLimits(pump);
  const now = history.now;
  const domain = now ? [now - seconds, now] : null;

  const health = useMemo(() => {
    const runup = A.runupTime(samples, limits.nominal);
    const states = A.loadState(samples, limits.nominal);
    let held = 0;
    states.forEach((state, i) => {
      if (state === "held" && i > 0) held += Math.min(30, samples[i].t - samples[i - 1].t);
    });
    const quiet = samples.filter((s) => limits.nominal && s.speed >= 0.95 * limits.nominal
      && s.p_best > 0 && s.p_best < 1e-4 && s.power > 0).map((s) => s.power);
    quiet.sort((a, b) => a - b);
    const loaded = samples.filter((s) => s.power > 0 && Number.isFinite(s.voltage))
      .map((s) => s.voltage);
    return {
      runup,
      held,
      friction: quiet.length >= 5 ? quiet[quiet.length >> 1] : null,
      lowest: loaded.length ? Math.min(...loaded) : null,
    };
  }, [samples, limits.nominal]);

  const gaugeNames = Object.fromEntries((gauge?.identity?.gauges || [])
    .filter((g) => g.present !== false).map((g) => [g.channel, g.identifier]));
  const crossover = pump?.interlocks?.motor_on?.limit_mbar;

  return html`
    <${Card} title="Pump trends"
      hint=${`Recorded by the server every ${history.interval} s, whether or not a run is going — the last 24 hours are kept.`
             + (history.oldest && now && history.oldest > now - seconds + 60
               ? ` Recording since ${fmt.clock(history.oldest)}, when the server started.` : "")}
      actions=${html`
        <${WindowPicker} value=${seconds} onChange=${(v) => { setSeconds(v); setPaused(null); }} />
        <button class="small" aria-pressed=${paused ? "true" : "false"}
                onClick=${() => setPaused(paused ? null : history.samples)}>
          ${paused ? "Resume" : "Pause"}</button>
        <button class="small" disabled=${!samples.length}
                onClick=${() => downloadCsv(samples, `pump-history-${new Date().toISOString().slice(0, 16)}.csv`)}>
          CSV</button>`}>
      ${history.error && html`<${Banner} tone="warn">${history.error}<//>`}

      <div class="tiles">
        <${Tile} label="Last run-up"
                 value=${health.runup ? fmt.duration(health.runup.seconds) : "—"}
                 tone=${health.runup && limits.startup && health.runup.seconds > limits.startup * 1.1 ? "warn" : null}
                 foot=${health.runup
                   ? `rest to full speed · rated ${fmt.duration(limits.startup)}`
                   : "no run-up from rest in this window"} />
        <${Tile} label="Friction at full speed"
                 value=${health.friction != null ? fmt.integer(health.friction) : "—"}
                 unit=${health.friction != null ? "W" : ""}
                 foot=${health.friction != null
                   ? "below 1e-4 mbar · creeps up as bearings wear"
                   : "needs full speed below 1e-4 mbar"} />
        <${Tile} label="Lowest supply under load"
                 value=${health.lowest != null ? fmt.number(health.lowest, 2) : "—"}
                 unit=${health.lowest != null ? "V" : ""}
                 tone=${health.lowest != null && health.lowest < limits.supplyLow ? "warn" : null}
                 foot=${`rated floor ${fmt.number(limits.supplyLow, 1)} V (${limits.supply} V − ${limits.tolerancePct} %)`} />
        <${Tile} label="Held below full speed"
                 value=${health.held ? fmt.duration(health.held) : "none"}
                 tone=${health.held > 30 ? "warn" : null}
                 foot="gas load beyond the drive's power line" />
      </div>

      <${RigStack} samples=${samples} x="clock" domain=${domain} pump=${pump}
                   gaugeNames=${gaugeNames} cursor=${cursor} onCursor=${setCursor}
                   panels=${["pressure", "speed", "current", "power", "voltage"]}
                   pressureRefs=${crossover ? [{ axis: "y", value: crossover, tone: "warn",
                     label: `turbopump start ≤ ${fmt.pressure(crossover, 0)}` }] : []} />

      <${LoadCurve} samples=${samples} pump=${pump} metric=${metric} onMetric=${setMetric}
                    cursor=${cursor} onCursor=${setCursor} interval=${history.interval} />

      <${TemperaturePanel} samples=${samples} pump=${pump} />
    <//>`;
}

/**
 * Pump temperature, honestly.
 *
 * Firmware 017500 answers NO_DEF for every temperature sensor it documents
 * (P326 electronics, P330 pump bottom, P342 bearing, P346 motor), and the TMS
 * reads 0 while its heater is off. So on this rig there is usually no pump
 * temperature to draw -- and a line at 0 °C would be worse than none. What is
 * measured is the heat going in (drive power, charted above) and the drive's
 * own over-temperature flags, marked on the power chart.
 */
function TemperaturePanel({ samples, pump }) {
  const points = useMemo(() => A.temperaturePoints(samples), [samples]);
  const series = useMemo(() => [{ key: "tms", label: "TMS temperature", color: "--series-2",
                                  points }], [points]);
  const measured = points.some(([, v]) => Number.isFinite(v));
  const setpoint = Number(pump?.status?.TMSHeatSVal) || null;
  const heating = pump?.status?.Heating === true;
  const hot = pump?.status?.OvTempPump || pump?.status?.OvTempElec;
  return html`
    <${ChartPanel} title="Pump temperature (TMS)"
      readout=${hot ? "over temperature" : measured ? null : "not measured"}
      table=${measured ? () => html`<${DataTable} series=${series} unit="°C" x=${{ kind: "clock" }}
                                                   formatValue=${(v) => fmt.integer(v)} />` : null}>
      ${measured
        ? html`<${Plot} series=${series} height=${140} x=${{ kind: "clock" }} unit="°C"
                        formatValue=${(v) => fmt.integer(v)} y=${{ zero: true, include: [setpoint || 40] }}
                        refs=${setpoint && heating ? [{ axis: "y", value: setpoint, tone: "muted",
                                                       label: `TMS setpoint ${setpoint} °C` }] : []} />`
        : html`<p class="hint">
            No temperature is measured on this pump right now. The TC 600 (firmware ${pump?.identity?.FwVersion || "017500"})
            has no electronics, motor, bearing or
            pump-bottom sensor — it answers NO_DEF for all four — and the TMS reads 0 while
            its heater is off. The heat going into the pump is the drive power above; the
            drive's own over-temperature flag is marked on that chart when it trips.
            ${heating ? "" : " Switching the TMS heater on (Accessory connectors, below) makes this chart fill in."}
          </p>`}
    <//>`;
}

export function PumpView({ state, onCommand, onWriteParam, busy, safeBusy,
                           onShutdown, onSecure, notify, shutdown, onOverride }) {
  const pump = state?.devices?.pump;
  const [accessories, setAccessories] = useState(null);
  const [speed, setSpeed] = useState("");

  const reload = (fresh = false) => api.accessories("pump", fresh).then(setAccessories)
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
          <strong>Fault ${s.ErrorCode}${pump.fault?.title ? ` — ${pump.fault.title}` : ""}.</strong>
          See Faults and recovery below: Make safe switches the motor off, clears
          the fault and keeps the forevacuum pump running.
        <//>`}

      <${Card} title="Station and turbopump" hint=${stationHint(interlocks)}>
        <div class="row">
          <${Guarded} action="on" label="Start pumping station" tone="primary"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy}
                      onOverride=${onOverride} />
          <${Guarded} action="off" label="Stop pumping station" tone="danger"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy}
                      onOverride=${onOverride} />
          <${Guarded} action="motor_on" label="Start turbopump"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy}
                      onOverride=${onOverride} />
          <${Guarded} action="motor_off" label="Stop turbopump"
                      interlocks=${interlocks} onRun=${onCommand} disabled=${busy}
                      onOverride=${onOverride} />
        </div>
        <div class="row">
          <button disabled=${busy}
                  onClick=${() => onCommand(setpoints.Standby ? "standby_off" : "standby_on",
                                            setpoints.Standby ? "Leave standby" : "Enter standby")}>
            ${setpoints.Standby ? "Leave standby" : `Enter standby (${
              fmt.integer(setpoints.StdbySVal)}%)`}
          </button>
          <button disabled=${busy || !fault}
                  onClick=${() => onCommand("ack", "Acknowledge fault", ACK_NOTE)}>
            Acknowledge fault
          </button>
          <button class="danger"
                  disabled=${busy || !!shutdown?.active
                             || setpoints.PumpgStatn !== true}
                  onClick=${onShutdown}>
            ${shutdown?.active ? "Powering down…" : "Power down the pump"}
          </button>
        </div>
        <div class="small muted">
          Power down runs the whole stop sequence in order — motor off, rotor
          to rest with the backing pump still running, then the station. Use
          it instead of the two buttons above when you are finished for the
          day.
        </div>
        <${CrossoverNote} interlocks=${interlocks}
                          running=${setpoints.MotorPump === true} />
        <${InterlockNotes} interlocks=${interlocks} />
      <//>

      <${FaultsCard} pump=${pump} alarms=${state?.alarms} busy=${busy}
                     safeBusy=${safeBusy} canAdmin=${state?.me?.can?.admin !== false}
                     onCommand=${onCommand} onSecure=${onSecure} notify=${notify} />

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

      <${PumpTrends} pump=${pump} gauge=${state?.devices?.gauge} />

      <${Card} title="Accessory connectors"
               actions=${html`<button class="small" onClick=${() => reload(true)}>Refresh</button>`}
               hint="Read on demand rather than in the status poll: fourteen extra round trips per refresh.">
        <${Accessories} data=${accessories} busy=${busy} onWrite=${writeParam}
                        reload=${reload} />
      <//>
    </div>`;
}
