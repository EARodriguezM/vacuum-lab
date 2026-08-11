// Setting up and watching a vacuum test run.
//
// The procedure forms are not hard-coded: the server publishes each
// procedure's fields at /api/procedures, and this builds the form from that.
// Adding a procedure on the Python side makes it appear here with no change
// to this file.

import { html } from "../html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { Banner, Card, Empty, Field, Pairs, Pill, coerceConfig, defaultConfig }
  from "../ui.js";
import { PressureChart, SpeedChart, TemperatureChart } from "../chart.js";
import { api } from "../api.js";
import * as fmt from "../format.js";

function ResultSummary({ procedure, result }) {
  if (!result) return null;
  const rows = [];
  const p = (value, digits = 2) =>
    value === null || value === undefined ? "—" : fmt.pressure(value, digits);

  if (procedure === "pumpdown") {
    rows.push(["Target", p(result.target_mbar) + " mbar"]);
    rows.push(["Reached", result.target_reached ? "yes" : "no"]);
    rows.push(["Time to target", fmt.duration(result.time_to_target_s)]);
    rows.push(["Ultimate pressure", p(result.ultimate_mbar) + " mbar"]);
    rows.push(["Final pressure", p(result.final_mbar) + " mbar"]);
  } else if (procedure === "rate_of_rise") {
    rows.push(["Leak rate", result.leak_rate_mbar_l_s !== undefined
      ? `${p(result.leak_rate_mbar_l_s, 3)} mbar·L/s` : "—"]);
    rows.push(["Rise", result.rise_mbar_per_h !== undefined
      ? `${p(result.rise_mbar_per_h, 3)} mbar/h` : "—"]);
    rows.push(["Chamber volume", `${fmt.number(result.volume_l, 2)} L`]);
    rows.push(["Fit quality R²", fmt.number(result.r_squared, 4)]);
    rows.push(["Linear", result.linear ? "yes" : "no"]);
  } else if (procedure === "hold") {
    rows.push(["Ultimate pressure", p(result.ultimate_mbar) + " mbar"]);
    rows.push(["Final pressure", p(result.final_mbar) + " mbar"]);
    rows.push(["Drift", p(result.drift_mbar, 2) + " mbar"]);
    rows.push(["Settled", result.settled ? "yes" : "still falling"]);
  } else if (procedure === "bakeout") {
    rows.push(["Setpoint", `${fmt.integer(result.setpoint_c)} °C`]);
    rows.push(["Peak temperature", `${fmt.integer(result.peak_temperature_c)} °C`]);
    rows.push(["Soak", `${fmt.number(result.soak_hours, 1)} h`]);
    rows.push(["Peak pressure", p(result.peak_pressure_mbar) + " mbar"]);
    rows.push(["Final pressure", p(result.final_pressure_mbar) + " mbar"]);
  }
  rows.push(["Duration", fmt.duration(result.duration_s)]);
  rows.push(["Samples", fmt.integer(result.samples)]);

  return html`
    <div class="grid" style="gap:10px">
      <${Pairs} items=${rows} />
      ${result.caveat && html`<${Banner} tone="warn">${result.caveat}<//>`}
      ${result.message && html`<div class="small muted">${result.message}</div>`}
    </div>`;
}

function EventLog({ events }) {
  if (!events?.length) return html`<${Empty}>Nothing logged yet.<//>`;
  return html`
    <div class="log">
      ${events.map((entry) => html`
        <div class=${`entry ${entry.level}`} key=${entry.id}>
          <span class="when mono">${fmt.clock(entry.at)}</span>
          <span>${entry.message}</span>
        </div>`)}
    </div>`;
}

/** The manual step currently blocking the recipe. The loudest thing on screen. */
function OperatorPrompt({ prompt, onAcknowledge, busy }) {
  if (!prompt) return null;
  const waiting = prompt.since ? (Date.now() / 1000) - prompt.since : 0;
  return html`
    <section class="card prompt-card">
      <div class="prompt-head">
        <span class="pill warn"><span class="dot" />Waiting for you</span>
        <span class="small muted">${fmt.duration(waiting)}</span>
      </div>
      <p class="prompt-text">${prompt.instruction}</p>
      ${prompt.note && html`<p class="hint">${prompt.note}</p>`}
      <div class="row">
        <button class="primary" disabled=${busy} onClick=${onAcknowledge}>
          Done — continue the run
        </button>
        <span class="small muted">
          ${prompt.label} → ${prompt.value}. The run keeps recording while you
          do this; nothing is unmonitored.
        </span>
      </div>
    </section>`;
}

/** What the process hardware is doing, and which of it is real. */
function Actuators({ actuators }) {
  if (!actuators?.length) return null;
  return html`
    <div class="tiles">
      ${actuators.map((a) => html`
        <div class=${`tile${a.available ? "" : " manual"}`} key=${a.key}>
          <span class="label">${a.label}</span>
          <span class="value num">${a.display}</span>
          <span class="foot">
            ${a.error ? a.error
              : a.pending ? `asked for ${a.pending} — not confirmed`
              : a.available ? "under software control"
              : "manual — not wired yet"}
          </span>
        </div>`)}
    </div>`;
}

function LiveRun({ run, samples, events, onStop, onAcknowledge, busy }) {
  const phases = run.phases || [];
  const index = phases.indexOf(run.phase);
  const share = phases.length > 1
    ? Math.max(0, Math.min(1, (index + 0.5) / phases.length)) : 0;
  const bakeout = run.procedure === "bakeout";

  return html`
    <div class="grid" style="gap:16px">
      <${OperatorPrompt} prompt=${run.prompt} busy=${busy}
                         onAcknowledge=${onAcknowledge} />

      <${Card} title=${run.run?.name || run.label}
               hint=${`${run.label} · ${run.mode === "auto" ? "automatic" : "guided"}` +
                      ` · started ${fmt.dateTime(run.run?.started_at)}`}
               actions=${html`
                 <button class="danger" disabled=${busy} onClick=${onStop}>
                   ${run.stopping ? "Stopping…" : "Stop run"}</button>`}>
        <div class="phases">
          ${phases.map((phase, position) => html`
            <span class=${`step${phase === run.phase ? " now"
                          : position < index ? " done" : ""}`}>
              ${fmt.titleCase(phase)}</span>`)}
        </div>
        <div class="bar"><span style=${`width:${(share * 100).toFixed(0)}%`} /></div>
        <div class="spread small muted">
          <span>Elapsed ${fmt.duration(run.elapsed_s)}${" "}
            · ${fmt.duration(run.in_phase_s)} in this phase</span>
          <span>${run.samples} samples · every ${fmt.number(run.interval_s, 0)}s</span>
        </div>
      <//>

      ${run.actuators?.length > 0 && html`
        <${Card} title="Process hardware">
          <${Actuators} actuators=${run.actuators} />
        <//>`}

      <${Card} title="Pressure">
        <${PressureChart} samples=${samples} height=${250} />
      <//>

      <${Card} title=${bakeout ? "TMS temperature" : "Rotor speed"}>
        ${bakeout
          ? html`<${TemperatureChart} samples=${samples} height=${170} />`
          : html`<${SpeedChart} samples=${samples} height=${170} />`}
      <//>

      <${Card} title="Run log">
        <${EventLog} events=${events} />
      <//>
    </div>`;
}

function Setup({ procedures, onStart, busy, blocked, actuators = [] }) {
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState(procedures[0]?.name || "");
  const [operator, setOperator] = useState(
    () => localStorage.getItem("vacuum-lab-operator") || "");
  const [notes, setNotes] = useState("");
  const [config, setConfig] = useState({});

  const procedure = procedures.find((p) => p.name === chosen) || procedures[0];
  const manual = actuators.filter((a) => !a.available).map((a) => a.label);

  useEffect(() => {
    if (procedure) setConfig(defaultConfig(procedure.fields));
  }, [chosen]);

  if (!procedure) return html`<${Empty}>The server published no procedures.<//>`;

  const start = () => {
    localStorage.setItem("vacuum-lab-operator", operator);
    onStart({
      procedure: procedure.name,
      name: name || procedure.label,
      operator,
      notes,
      config: coerceConfig(procedure.fields, config),
    });
  };

  return html`
    <div class="grid" style="gap:16px">
      ${blocked && html`<${Banner} tone="warn">${blocked}<//>`}

      ${manual.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Not wired up yet:</strong> ${manual.join(", ")}.
          A guided run will stop and ask you to do each of these by hand.
          Automatic mode stays unavailable until they are connected.
        <//>`}

      <${Card} title="Choose a procedure">
        <div class="row">
          ${procedures.map((entry) => html`
            <button key=${entry.name}
                    class=${entry.name === chosen ? "primary" : ""}
                    onClick=${() => setChosen(entry.name)}>${entry.label}</button>`)}
        </div>
        <p class="hint">${procedure.description}</p>
        <div class="phases">
          ${procedure.phases.map((phase) => html`
            <span class="step">${fmt.titleCase(phase)}</span>`)}
        </div>
      <//>

      <div class="grid two">
        <${Card} title="Settings">
          <div class="grid" style="gap:12px">
            ${procedure.fields.map((field) => html`
              <${Field} key=${field.key} field=${field} value=${config[field.key]}
                        onChange=${(value) =>
                          setConfig((c) => ({ ...c, [field.key]: value }))} />`)}
          </div>
        <//>

        <${Card} title="Record keeping"
                 hint="These go into the database with the curve, so a run can be found again months later.">
          <label class="field">
            <span>Run name</span>
            <input value=${name} placeholder=${procedure.label}
                   onInput=${(e) => setName(e.target.value)} />
          </label>
          <label class="field">
            <span>Operator</span>
            <input value=${operator} onInput=${(e) => setOperator(e.target.value)} />
          </label>
          <label class="field">
            <span>Notes</span>
            <textarea value=${notes} onInput=${(e) => setNotes(e.target.value)}
                      placeholder="Chamber, sample, what changed since last time…" />
          </label>
        <//>
      </div>

      <${Card}>
        <div class="row">
          <button class="primary" disabled=${busy} onClick=${start}>
            Start ${procedure.label.toLowerCase()}
          </button>
          <span class="small muted">
            The run drives the hardware through the same interlocks as the
            manual controls — it holds no override.
          </span>
        </div>
      <//>
    </div>`;
}

export function TestsView({ state, live, onStart, onStop, onAcknowledge, busy }) {
  const [procedures, setProcedures] = useState([]);
  const [events, setEvents] = useState([]);
  const run = state?.run;

  useEffect(() => {
    api.procedures().then((data) => setProcedures(data.procedures || []))
      .catch(() => setProcedures([]));
  }, []);

  const runId = run?.run?.id;
  useEffect(() => {
    if (!runId) { setEvents([]); return; }
    let cancelled = false;
    const load = () => api.runEvents(runId)
      .then((data) => { if (!cancelled) setEvents(data.events || []); })
      .catch(() => {});
    load();
    const timer = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [runId]);

  if (run?.running) {
    return html`<${LiveRun} run=${run} samples=${live?.samples || []}
                            events=${events} onStop=${onStop}
                            onAcknowledge=${onAcknowledge} busy=${busy} />`;
  }

  const devices = state?.devices || {};
  const blocked = !devices.gauge?.connected
    ? "The gauge controller is offline, so a run would record no pressure. " +
      "You can still start one — the pump data will be recorded — but the " +
      "curve will be empty."
    : null;

  return html`<${Setup} procedures=${procedures} onStart=${onStart}
                        busy=${busy} blocked=${blocked}
                        actuators=${state?.actuators || []} />`;
}

export { ResultSummary, EventLog };
