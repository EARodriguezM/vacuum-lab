// Setting up and watching a vacuum test run.
//
// The procedure forms are not hard-coded: the server publishes each
// procedure's fields at /api/procedures, and this builds the form from that.
// Adding a procedure on the Python side makes it appear here with no change
// to this file.
//
// The step a guided run waits for is not shown here but at the top of every
// page (guided.js): somebody watching another tab must see it too.

import { html } from "../html.js";
import { useEffect, useMemo, useState } from "preact/hooks";
import { Banner, Card, Confirm, Empty, Field, Pairs, Pill, coerceConfig, configProblems,
         defaultConfig }
  from "../ui.js";
import { RunCharts } from "../runcharts.js";
import { useStored } from "../rigcharts.js";
import { ActionList } from "../alarms.js";
import * as A from "../analysis.js";
import { api, joinPoll } from "../api.js";
import * as fmt from "../format.js";
import * as status from "../status.js";

/** An ended run stays on this page this long, then the form comes back. */
const AFTER_RUN_S = 12 * 3600;

function ResultSummary({ procedure, result }) {
  if (!result) return null;
  const rows = [];
  const p = (value, digits = 2) =>
    value === null || value === undefined ? "—" : fmt.pressure(value, digits);
  const base = () => {
    rows.push(["Base pressure", result.base_mbar != null ? `${p(result.base_mbar)} mbar` : "not reached"]);
    if (result.time_to_base_s != null) rows.push(["Time to base", fmt.duration(result.time_to_base_s)]);
    if (result.time_to_full_speed_s != null) {
      rows.push(["Time to full speed", fmt.duration(result.time_to_full_speed_s)]);
    }
  };

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
  } else if (procedure === "base_calibration" || procedure === "full_vacuum") {
    base();
    if (result.ultimate_mbar != null) rows.push(["Best pressure", `${p(result.ultimate_mbar)} mbar`]);
  } else if (procedure === "deposition") {
    base();
    rows.push(["Working pressure set", `${p(result.working_mbar_setpoint, 1)} mbar`]);
    if (result.working_mbar_actual?.mean != null) {
      rows.push(["Working pressure held", `${p(result.working_mbar_actual.mean)} mbar (mean)`]);
    }
    rows.push(["Deposited", result.deposition_s != null
      ? `${fmt.duration(result.deposition_s)}${result.deposition_set_s
        ? ` of ${fmt.duration(result.deposition_set_s)} set` : ""}` : "not started"]);
    // Never "yes" by default: an older run has no such record, and a
    // shutter that never opened was never closed by anybody either.
    rows.push(["Shutter closed at the end", result.deposition_s == null ? "never opened"
      : result.shutter_closed === true ? "confirmed" : result.shutter_closed === false
        ? "NOT confirmed" : "—"]);
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

/** The phases, the current one marked, the finished ones ticked -- not only coloured. */
function Phases({ phases, current }) {
  const index = phases.indexOf(current);
  return html`
    <div class="phases">
      ${phases.map((phase, position) => html`
        <span key=${phase} class=${`step${phase === current ? " now"
                      : position < index ? " done" : ""}`}
              aria-current=${phase === current ? "step" : null}>
          ${position < index ? html`<span aria-hidden="true">✓ </span>` : null}${fmt.phaseLabel(phase)}</span>`)}
    </div>`;
}

/** What happens first when this procedure starts, in plain words. */
function firstMove(procedure) {
  const phases = procedure?.phases || [];
  if (phases.includes("rough") || phases.includes("roughing")) {
    return "It switches the pumping station on at once, so the backing pump "
      + "starts roughing. The turbopump starts only once the gauge reads the "
      + "chamber below the crossover pressure.";
  }
  if (procedure?.name === "bakeout") {
    return "It switches the pump's heater on at once, to the setpoint below.";
  }
  if (procedure?.name === "rate_of_rise") {
    return "It closes the gate valve to isolate the chamber, then measures "
      + "how fast the pressure rises.";
  }
  return "It records the instruments; it switches nothing on.";
}

/**
 * The start dialog: the recipe repeated before it runs, because a run's
 * first step moves hardware -- the server wants `confirm` for it like any
 * other command. A refusal is shown here, where the person is looking.
 */
function StartDialog({ request, busy, error, onCancel, onConfirm }) {
  if (!request) return null;
  const { procedure, body, manual, noGauge } = request;
  const config = body.config || {};
  const shown = (field) => {
    const value = config[field.key];
    if (field.type === "boolean") return value ? "yes" : "no";
    if (field.type === "select") {
      return (field.options || []).find((o) => String(o.value) === String(value))?.label
        ?.split(" — ")[0] || String(value);
    }
    if (value === null || value === undefined) return "—";
    const number = field.type === "exponent" ? fmt.pressure(value, 1)
      : String(Number(value));
    return `${number}${field.unit ? ` ${field.unit}` : ""}`;
  };
  const guided = String(config.mode || "guided") !== "auto";
  return html`
    <${Confirm} open=${true} title=${`Start: ${body.name || procedure.label}`}
      confirmLabel="Start the run" busy=${busy} error=${error}
      onCancel=${onCancel} onConfirm=${onConfirm}
      body=${html`
        <div class="grid" style="gap:10px" data-test="start-dialog">
          <p><strong>${procedure.label}</strong>${procedure.fields.some((f) => f.key === "mode")
            ? `, ${guided ? "guided" : "automatic"}` : ""}. ${firstMove(procedure)}</p>
          <${Pairs} items=${procedure.fields.filter((f) => f.key !== "mode")
            .map((field) => [String(field.label).replace(/^…/, ""), shown(field)])} />
          ${manual.length > 0 && guided && html`
            <p>It will stop and ask you to do these by hand, and wait for your
              Done each time: ${manual.join(", ")}.</p>`}
          ${noGauge && html`<p class="warn-text">The gauge is offline: the curve
            will have no pressure.</p>`}
          <p class="muted">It drives the hardware through the same interlocks as
            the manual controls, and holds no override.</p>
        </div>`} />`;
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

/** The process timer a recipe is running, as a countdown. */
function TimerLine({ timer }) {
  if (!timer) return null;
  if (timer.elapsed_s === null || timer.elapsed_s === undefined) {
    return html`<div class="small"><strong>${timer.label}</strong>
      ${fmt.duration(timer.total_s)} after the step above is confirmed.</div>`;
  }
  const left = Math.max(0, timer.total_s - timer.elapsed_s);
  return html`
    <div class="small"><strong>${timer.label} in ${fmt.duration(left)}</strong>
      <span class="muted"> · ${fmt.duration(timer.elapsed_s)} of ${fmt.duration(timer.total_s)}</span>
    </div>`;
}

// `stopBusy` locks the stop button only: every operator may press it,
// whoever holds control.
function LiveRun({ run, samples, events, onStop, stopBusy, devices, may }) {
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

      <${Card} title=${run.run?.name || run.label}
               hint=${`${run.label} · ${run.mode === "auto" ? "automatic" : "guided"}` +
                      ` · started ${fmt.dateTime(run.run?.started_at)}`}
               actions=${html`
                 <button class="danger" disabled=${stopBusy} onClick=${onStop}
                         data-test="run-stop">
                   ${run.stopping ? "Stopping…" : may === "signin" ? "Sign in to stop"
                     : "Stop run"}</button>`}>
        <${Phases} phases=${phases} current=${run.phase} />
        <div class="bar"><span style=${`width:${(share * 100).toFixed(0)}%`} /></div>
        <${TimerLine} timer=${run.timer} />
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
                 unconfigured = {}, me = null }) {
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState("");
  const [notes, setNotes] = useState("");
  const [config, setConfig] = useState({});
  // The start dialog: what is about to run, and a refusal if it is refused.
  const [starting, setStarting] = useState(null);

  // Opens on the first procedure this rig can run.
  const procedure = procedures.find((p) => p.name === chosen)
    || procedures.find((p) => !unavailable[p.name]) || procedures[0];
  // What the chamber has no instrument for at all, and -- of the rest --
  // the equipment not installed yet: two different things to tell someone.
  const noInstrument = unconfigured[procedure?.name] || [];
  const absent = (unavailable[procedure?.name] || [])
    .filter((label) => !noInstrument.includes(label));
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
  const review = () => setStarting({
    procedure, manual, noGauge: !!blocked,
    body: {
      procedure: procedure.name,
      name: name || procedure.label,
      notes,
      config: coerceConfig(procedure.fields, config),
    },
  });
  const start = async () => {
    const request = starting;
    setStarting({ ...request, sending: true, error: null });
    const result = await onStart(request.body);
    if (result?.ok) setStarting(null);
    else setStarting({ ...request, sending: false, error: result?.error || "the run did not start" });
  };

  return html`
    <div class="grid" style="gap:16px">
      ${blocked && html`<${Banner} tone="warn">${blocked}<//>`}

      ${noInstrument.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Switched off — this chamber has no ${noInstrument.join(" or ")}.</strong>
          ${" "}You can read what this procedure does, but it cannot run on this
          chamber: its instruments are listed in its file in <code>chambers/</code>.
        <//>`}

      ${noInstrument.length === 0 && absent.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Switched off — not installed on the rig yet:</strong>
          ${" "}${absent.join(", ")}. You can read what this procedure does, but it
          cannot run until that is in place. Then mark it installed in
          ${" "}<a href="#settings/rig">Settings → Rig</a>.
        <//>`}

      ${noInstrument.length === 0 && absent.length === 0 && manual.length > 0 && html`
        <${Banner} tone="warn">
          <strong>Not wired up yet:</strong> ${manual.join(", ")}.
          A guided run will stop and ask you to do each of these by hand.
          Automatic mode stays unavailable until they are connected.
        <//>`}

      <${Card} title="Choose a procedure">
        <div class="row">
          ${procedures.map((entry) => {
            const off = unavailable[entry.name];
            const tag = unconfigured[entry.name] ? "not on this chamber" : "not installed";
            return html`
              <button key=${entry.name}
                      class=${`${entry.name === procedure.name ? "primary" : ""}${off ? " unavailable" : ""}`}
                      aria-pressed=${entry.name === procedure.name ? "true" : "false"}
                      data-test=${`procedure-${entry.name}`}
                      title=${off ? `Switched off: needs ${off.join(", ")}` : entry.description}
                      onClick=${() => setChosen(entry.name)}>
                ${entry.label}${off ? html` <span class="off-tag">${tag}</span>` : null}</button>`;
          })}
        </div>
        <p class="hint">${procedure.description}</p>
        <${Phases} phases=${procedure.phases} current=${null} />
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
          <div class="field">
            <span>Operator</span>
            <div class="fixed-value" data-test="run-operator">
              ${me && me.kind !== "device" ? me.name : "—"}</div>
            <span class="help">${me && me.kind !== "device"
              ? "Whoever is signed in and starts the run. To run under your own name, sign in as yourself."
              : "A paired tablet only watches: sign in to start a run under your name."}</span>
          </div>
          <label class="field">
            <span>Notes</span>
            <textarea value=${notes} onInput=${(e) => setNotes(e.target.value)}
                      placeholder="Chamber, sample, what changed since last time…" />
          </label>
        <//>
      </div>

      <${Card}>
        <div class="row">
          <button class="primary"
                  disabled=${busy || noInstrument.length > 0 || absent.length > 0
                             || problems.length > 0 || !ready}
                  data-test="run-start" onClick=${review}>
            Start: ${procedure.label}…
          </button>
          <span class="small muted">
            ${noInstrument.length > 0
              ? `Switched off: this chamber has no ${noInstrument.join(" or ")}.`
              : absent.length > 0
              ? "Switched off until its equipment is installed."
              : problems.length > 0
                ? "Fix the settings marked below first."
                : "The next step shows the recipe again before anything moves."}
          </span>
        </div>
        ${problems.length > 0 && html`
          <div class="banner warn small" role="alert">
            ${problems.map((problem) => html`<div key=${problem}>${problem}</div>`)}
          </div>`}
      <//>

      <${StartDialog} request=${starting} busy=${!!starting?.sending}
                      error=${starting?.error} onCancel=${() => setStarting(null)}
                      onConfirm=${start} />
    </div>`;
}

/**
 * A run that has ended -- and the rig carrying on after it.
 *
 * When a run aborted, this page used to snap straight back to an empty form,
 * and the curves vanished at exactly the moment somebody needed them: what
 * the rotor did after the fault, whether the pressure recovered. Now the run
 * stays on screen with its result, and its charts continue live from the
 * server's rolling history in an "After the run" phase, until a new run is
 * started -- or for half a day, after which the form is what is wanted.
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

  const said = fmt.runStatus({ status: last.status,
                               result: { stopped: last.by_operator, message: last.message } });
  const tone = said === "completed" ? "good" : said === "failed" ? "crit"
    : said === "stopped" ? "idle" : "warn";
  const secure = state?.run?.secure;
  const recent = secure && secure.started_at >= last.ended_at - 5;

  return html`
    <div class="grid" style="gap:16px">
      <section class=${`summary ${tone}`}>
        <div class="spread">
          <span class="headline">
            Run ${last.id} ${said}: ${last.name || last.label}</span>
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

      ${record?.result && html`
        <${Card} title="Result">
          <${ResultSummary} procedure=${last.procedure} result=${record.result} />
        <//>`}

      <${RunCharts} run=${record || { id: last.id }} procedure=${last.procedure}
                    config=${record?.config || {}} samples=${samples} events=${events}
                    startedAt=${startedAt} pump=${state?.devices?.pump}
                    gauge=${state?.devices?.gauge} live=${true} />

      <${Card} title="Run log">
        <${EventLog} events=${events} />
      <//>
    </div>`;
}

export function TestsView({ state, live, onStart, onStop, busy, stopBusy, may }) {
  // null while they load: "the server published none" is a different thing.
  const [procedures, setProcedures] = useState(null);
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
                            events=${events} onStop=${onStop} may=${may}
                            stopBusy=${stopBusy} devices=${state?.devices} />`;
  }

  // The run that just ended stays on screen, live, until it is dismissed.
  const last = run?.last;
  const now = state?.server_time || Date.now() / 1000;
  if (last && last.id !== dismissed && now - (last.ended_at || 0) < AFTER_RUN_S) {
    return html`<${AfterRun} last=${last} state=${state}
                             onDismiss=${() => setDismissed(last.id)} />`;
  }

  if (procedures === null) return html`<${Empty}>Loading the procedures…<//>`;

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
                        unconfigured=${state?.unconfigured || {}}
                        me=${state?.me} />`;
}

export { ResultSummary, EventLog };
