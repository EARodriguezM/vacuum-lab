// Setting up and watching a vacuum test run.
//
// The procedure forms are not hard-coded: the server publishes each
// procedure's fields at /api/procedures, and this builds the form from that.
// Adding a procedure on the Python side makes it appear here with no change
// to this file.

import { html } from "../html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { Banner, Card, Empty, Field, Pairs, Pill, coerceConfig, configProblems,
         defaultConfig }
  from "../ui.js";
import { RunCharts } from "../runcharts.js";
import { useStored } from "../rigcharts.js";
import { ActionList } from "../alarms.js";
import * as A from "../analysis.js";
import { api, joinPoll } from "../api.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

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
    rows.push(["Leak rate", result.leak_rate_mbar_l_s != null
      ? `${p(result.leak_rate_mbar_l_s, 3)} mbar·L/s` : "—"]);
    rows.push(["Rise", result.rise_mbar_per_h != null
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
    <section class="card prompt-card" data-test="prompt">
      <div class="prompt-head">
        <span class="pill warn"><span class="dot" />Waiting for you</span>
        <span class="small muted">${fmt.duration(waiting)}</span>
      </div>
      <p class="prompt-text">${prompt.instruction}</p>
      ${prompt.note && html`<p class="hint">${prompt.note}</p>`}
      <div class="row">
        <button class="primary" disabled=${busy} onClick=${onAcknowledge}
                data-test="prompt-done">
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

// `busy` locks what needs control of the rig; `stopBusy` only the stop
// button, which every operator may press whoever holds control.
function LiveRun({ run, samples, events, onStop, onAcknowledge, busy, stopBusy,
                   devices }) {
  const phases = run.phases || [];
  const index = phases.indexOf(run.phase);
  const share = phases.length > 1
    ? Math.max(0, Math.min(1, (index + 0.5) / phases.length)) : 0;

  const pumpState = status.pumpSummary(devices?.pump);

  return html`
    <div class="grid" style="gap:16px">
      ${pumpState.dragged && html`
        <${Banner} tone="crit">
          <strong>${pumpState.headline}.</strong> ${pumpState.detail}
        <//>`}
      <${OperatorPrompt} prompt=${run.prompt} busy=${busy}
                         onAcknowledge=${onAcknowledge} />

      <${Card} title=${run.run?.name || run.label}
               hint=${`${run.label} · ${run.mode === "auto" ? "automatic" : "guided"}` +
                      ` · started ${fmt.dateTime(run.run?.started_at)}`}
               actions=${html`
                 <button class="danger" disabled=${stopBusy ?? busy} onClick=${onStop}
                         data-test="run-stop">
                   ${run.stopping ? "Stopping…" : "Stop run"}</button>`}>
        <div class="phases">
          ${phases.map((phase, position) => html`
            <span class=${`step${phase === run.phase ? " now"
                          : position < index ? " done" : ""}`}>
              ${fmt.phaseLabel(phase)}</span>`)}
        </div>
        <div class="bar"><span style=${`width:${(share * 100).toFixed(0)}%`} /></div>
        <div class="spread small muted">
          <span>Elapsed ${fmt.duration(run.elapsed_s)}${" "}
            · ${fmt.duration(run.in_phase_s)} in this phase</span>
          <span>${run.samples} samples · every ${fmt.number(run.interval_s, 0)}s</span>
        </div>
      <//>

      ${(run.requires || []).length > 0 && html`
        <${Card} title="Process hardware">
          <${Actuators} actuators=${(run.actuators || [])
            .filter((a) => run.requires.includes(a.key))} />
        <//>`}

      <${RunCharts} run=${run.run} procedure=${run.procedure}
                    config=${run.run?.config || {}} samples=${samples} events=${events}
                    startedAt=${run.run?.started_at} pump=${devices?.pump}
                    gauge=${devices?.gauge} live=${true} />

      <${Card} title="Run log">
        <${EventLog} events=${events} />
      <//>
    </div>`;
}

function Setup({ procedures, onStart, busy, blocked, actuators = [], unavailable = {},
                 me = null }) {
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState("");
  // Whoever is signed in, unless they type somebody else's name: a student
  // running a run for a supervisor. The account is recorded either way.
  const [operator, setOperator] = useState(() => me?.name || "");
  const [notes, setNotes] = useState("");
  const [config, setConfig] = useState({});

  // Opens on the first procedure this rig can run.
  const procedure = procedures.find((p) => p.name === chosen)
    || procedures.find((p) => !unavailable[p.name]) || procedures[0];
  const absent = unavailable[procedure?.name] || [];
  // Only the actuators this procedure moves: a pump-down asks for none.
  const manual = actuators.filter((a) => (procedure?.requires || []).includes(a.key)
                                         && a.installed !== false && !a.available)
    .map((a) => a.label);

  useEffect(() => {
    if (procedure) setConfig(defaultConfig(procedure.fields));
  }, [procedure?.name]);

  if (!procedure) return html`<${Empty}>The server published no procedures.<//>`;

  // Until this procedure's defaults are filled in -- the first frame, and
  // the frame after switching procedure, which still holds the previous
  // one's settings -- there is nothing of its own to judge.
  const ready = procedure.fields.every((field) => field.key in config);
  const problems = ready ? configProblems(procedure.fields, config) : [];
  const start = () => {
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

      ${absent.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Switched off — not installed on the rig yet:</strong>
          ${" "}${absent.join(", ")}. You can read what this procedure does, but it
          cannot run until that is in place. Then mark it installed in
          ${" "}<a href="#settings">Settings → Rig equipment</a>.
        <//>`}

      ${absent.length === 0 && manual.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Not wired up yet:</strong> ${manual.join(", ")}.
          A guided run will stop and ask you to do each of these by hand.
          Automatic mode stays unavailable until they are connected.
        <//>`}

      <${Card} title="Choose a procedure">
        <div class="row">
          ${procedures.map((entry) => {
            const off = unavailable[entry.name];
            return html`
              <button key=${entry.name}
                      class=${`${entry.name === procedure.name ? "primary" : ""}${off ? " unavailable" : ""}`}
                      title=${off ? `Switched off: needs ${off.join(", ")}` : entry.description}
                      onClick=${() => setChosen(entry.name)}>
                ${entry.label}${off ? html` <span class="off-tag">not installed</span>` : null}</button>`;
          })}
        </div>
        <p class="hint">${procedure.description}</p>
        <div class="phases">
          ${procedure.phases.map((phase) => html`
            <span class="step">${fmt.phaseLabel(phase)}</span>`)}
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
          <button class="primary" disabled=${busy || absent.length > 0 || problems.length > 0}
                  data-test="run-start" onClick=${start}>
            Start ${procedure.label.toLowerCase()}
          </button>
          <span class="small muted">
            ${absent.length > 0
              ? "Switched off until its equipment is installed."
              : problems.length > 0
                ? "Fix the settings marked below first."
                : "The run drives the hardware through the same interlocks as the manual controls — it holds no override."}
          </span>
        </div>
        ${problems.length > 0 && html`
          <div class="banner warn small" role="alert">
            ${problems.map((problem) => html`<div key=${problem}>${problem}</div>`)}
          </div>`}
      <//>
    </div>`;
}

/**
 * A run that has ended -- and the rig carrying on after it.
 *
 * When a run aborted, this page used to snap straight back to an empty form,
 * and the curves vanished at exactly the moment somebody needed them: what
 * the rotor did after the fault, whether the pressure recovered. Now the run
 * stays on screen, and its charts continue live from the server's rolling
 * history in an "After the run" phase, until you start a new run.
 */
function AfterRun({ last, state, onDismiss }) {
  const [record, setRecord] = useState(null);
  const [stored, setStored] = useState([]);
  const [after, setAfter] = useState([]);
  const [events, setEvents] = useState([]);
  const startedAt = last.started_at;

  useEffect(() => {
    let cancelled = false;
    api.run(last.id).then((data) => { if (!cancelled) setRecord(data.run); }).catch(() => {});
    api.runSamples(last.id, 1500)
      .then((data) => { if (!cancelled) setStored(data.samples || []); })
      .catch(() => {});
    const loadEvents = () => api.runEvents(last.id)
      .then((data) => { if (!cancelled) setEvents(data.events || []); })
      .catch(() => {});
    loadEvents();
    // The recovery after a fault is logged a few seconds after the end.
    const timer = setInterval(loadEvents, 5000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [last.id]);

  // Everything the rig recorded since the run ended, kept up to date -- on
  // the panel's own poll, so it redraws with everything else (api.js).
  useEffect(() => {
    let cancelled = false;
    let newest = null;
    let rows = [];
    let pending = null;
    const load = () => (newest === null
      ? api.history(Math.max(60, (state?.server_time || Date.now() / 1000)
                                 - last.ended_at + 10), 1500)
      : api.history(0, 2000, newest));
    const fetch = () => {
      if (pending) return Promise.resolve({ skip: true });
      pending = load().finally(() => { pending = null; });
      return pending;
    };
    const apply = (result) => {
      // An outage is the main poll's to report.
      if (cancelled || result.status !== "fulfilled") return;
      const payload = result.value;
      if (payload?.skip || !payload?.ok) return;
      const fresh = A.historySamples(payload).filter((r) => r.at > last.ended_at);
      if (fresh.length) {
        rows = rows.concat(fresh.filter((r) => newest === null || r.at > newest));
        newest = rows[rows.length - 1].at;
        setAfter(rows);
      } else if (newest === null) {
        newest = last.ended_at;
      }
    };
    fetch().then((value) => apply({ status: "fulfilled", value }),
                 (reason) => apply({ status: "rejected", reason }));
    const leave = joinPoll({ fetch, apply });
    return () => { cancelled = true; leave(); };
  }, [last.id]);

  const samples = useMemo(() => stored.concat(after.map((r) => ({
    ...r, t: r.at - startedAt, phase: "after",
  }))), [stored, after, startedAt]);

  const tone = last.status === "completed" ? "good"
    : last.status === "failed" ? "crit" : "warn";
  const secure = state?.run?.secure;
  const recent = secure && secure.started_at >= last.ended_at - 5;

  return html`
    <div class="grid" style="gap:16px">
      <section class=${`summary ${tone}`}>
        <div class="spread">
          <span class="headline">
            Run ${last.id} ${last.status}: ${last.name || last.label}</span>
          <div class="row">
            <a class="btn" href=${`#history/${last.id}`}>Open in History</a>
            <button class="primary" data-test="run-new" onClick=${onDismiss}>Start a new run</button>
          </div>
        </div>
        ${last.message && html`<div>${last.message}</div>`}
        <div class="small muted">
          Ended at ${fmt.clock(last.ended_at)}, after ${fmt.duration(last.ended_at - startedAt)}.
          The charts carry on with live data in the "After the run" band, so you can
          watch the rotor and the pressure after the end.
          ${last.pump_trouble ? " The pump was put in its safe state: motor off, forevacuum pump kept running."
            : " The pump has been left as it was."}
        </div>
        ${recent && html`
          <div class="small">
            <strong>${secure.active ? "Making the pump safe…" : secure.ok ? "Pump made safe:" : "Pump not confirmed safe:"}</strong>
            <${ActionList} actions=${secure.actions} />
            ${secure.error && html`<span class="muted">${secure.error}</span>`}
          </div>`}
      </section>

      <${RunCharts} run=${record || { id: last.id }} procedure=${last.procedure}
                    config=${record?.config || {}} samples=${samples} events=${events}
                    startedAt=${startedAt} pump=${state?.devices?.pump}
                    gauge=${state?.devices?.gauge} live=${true} />

      <${Card} title="Run log">
        <${EventLog} events=${events} />
      <//>
    </div>`;
}

export function TestsView({ state, live, onStart, onStop, onAcknowledge, busy,
                            stopBusy }) {
  const [procedures, setProcedures] = useState([]);
  const [events, setEvents] = useState([]);
  const [dismissed, setDismissed] = useStored("vacuum-lab-dismissed-run", null);
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
                            onAcknowledge=${onAcknowledge} busy=${busy}
                            stopBusy=${stopBusy} devices=${state?.devices} />`;
  }

  // The run that just ended stays on screen, live, until it is dismissed.
  const last = run?.last;
  if (last && last.id !== dismissed) {
    return html`<${AfterRun} last=${last} state=${state}
                             onDismiss=${() => setDismissed(last.id)} />`;
  }

  const devices = state?.devices || {};
  const blocked = !devices.gauge?.connected
    ? "The gauge controller is offline, so a run would record no pressure. " +
      "You can still start one — the pump data will be recorded — but the " +
      "curve will be empty."
    : null;

  return html`<${Setup} procedures=${procedures} onStart=${onStart}
                        busy=${busy} blocked=${blocked}
                        actuators=${state?.actuators || []}
                        unavailable=${state?.unavailable || {}}
                        me=${state?.me} />`;
}

export { ResultSummary, EventLog };
