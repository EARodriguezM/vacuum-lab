// The run database: browse, inspect, annotate, export, delete.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Confirm, Empty, Pairs, Pill } from "../ui.js";
import { CompareRuns, RunCharts, RunTrends } from "../runcharts.js";
import { ResultSummary, EventLog } from "./tests.js";
import { api, download } from "../api.js";
import * as fmt from "../format.js";

/** Save a server export. Fetched with the session, so no token is in a URL. */
function save(path, notify) {
  download(path).catch((error) => notify(error.message, "error"));
}

const STATUS_TONE = {
  completed: "good", running: "accent", aborted: "warn", failed: "crit",
};

function RunDetail({ run, onClose, onChanged, notify, devices, runs,
                    canEdit = true, canDelete = true }) {
  const [samples, setSamples] = useState([]);
  const [events, setEvents] = useState([]);
  const [meta, setMeta] = useState({
    name: run.name, operator: run.operator || "", notes: run.notes || "",
  });
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setMeta({ name: run.name, operator: run.operator || "", notes: run.notes || "" });
    Promise.all([api.runSamples(run.id, 2000), api.runEvents(run.id)])
      .then(([s, e]) => {
        if (cancelled) return;
        setSamples(s.samples || []);
        setEvents(e.events || []);
      })
      .catch((error) => notify(error.message, "error"))
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [run.id]);

  const saveMeta = async () => {
    setSaving(true);
    try {
      await api.updateRun(run.id, meta);
      notify("Saved.", "ok");
      onChanged();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    setConfirmDelete(false);
    try {
      await api.deleteRun(run.id);
      notify(`Deleted run ${run.id}.`, "ok");
      onChanged();
      onClose();
    } catch (error) {
      notify(error.message, "error");
    }
  };

  return html`
    <div class="grid" style="gap:16px">
      <${Card} title=${run.name}
        hint=${`${fmt.titleCase(run.procedure)} · ${fmt.dateTime(run.started_at)}` +
               `${run.operator ? ` · ${run.operator}` : ""}`}
        actions=${html`
          <${Pill} tone=${STATUS_TONE[run.status] || "idle"}>${run.status}<//>
          <button onClick=${() => save(`/api/runs/${run.id}/export.csv`, notify)}>CSV</button>
          <button onClick=${() => save(`/api/runs/${run.id}/export.json`, notify)}>JSON</button>
          <button onClick=${onClose}>Close</button>`}>
        <${Pairs} items=${[
          ["Started", fmt.dateTime(run.started_at)],
          ["Ended", run.ended_at ? fmt.dateTime(run.ended_at) : "—"],
          ["Duration", fmt.duration(run.duration)],
          ["Samples", fmt.integer(run.sample_count ?? samples.length)],
        ]} />
      <//>

      <div class="grid two">
        <${Card} title="Result">
          <${ResultSummary} procedure=${run.procedure} result=${run.result} />
        <//>
        <${Card} title="Configuration">
          <${Pairs} items=${Object.entries(run.config || {}).map(([key, value]) =>
            [fmt.titleCase(key), typeof value === "number" && Math.abs(value) < 0.01
              && value !== 0 ? fmt.pressure(value, 2) : String(value)])} />
        <//>
      </div>

      ${loading
        ? html`<${Card}><${Empty}>Loading the curves…<//><//>`
        : html`
          <${RunCharts} run=${run} procedure=${run.procedure} config=${run.config || {}}
                        samples=${samples} events=${events} startedAt=${run.started_at}
                        pump=${devices?.pump} gauge=${devices?.gauge} />
          <${CompareRuns} run=${run} samples=${samples} runs=${runs} />`}

      <div class="grid two">
        <${Card} title="Run log">
          <${EventLog} events=${events} />
        <//>

        <${Card} title="Notes"
                 hint="Editable after the fact — the curve is immutable, the story around it is not.">
          <label class="field"><span>Name</span>
            <input value=${meta.name}
                   onInput=${(e) => setMeta({ ...meta, name: e.target.value })} />
          </label>
          <label class="field"><span>Operator</span>
            <input value=${meta.operator}
                   onInput=${(e) => setMeta({ ...meta, operator: e.target.value })} />
          </label>
          <label class="field"><span>Notes</span>
            <textarea value=${meta.notes}
                      onInput=${(e) => setMeta({ ...meta, notes: e.target.value })} />
          </label>
          <div class="row">
            <button class="primary" disabled=${saving || !canEdit} onClick=${saveMeta}
                    title=${canEdit ? "" : "Editing a run needs the operator role"}>Save</button>
            <button class="danger right" disabled=${!canDelete}
                    title=${canDelete ? "" : "Deleting a run is an admin's"}
                    onClick=${() => setConfirmDelete(true)}>Delete run</button>
          </div>
        <//>
      </div>

      <${Confirm} open=${confirmDelete} title=${`Delete run ${run.id}?`}
        tone="danger" confirmLabel="Delete permanently"
        body=${html`<p>This removes <strong>${run.name}</strong> and all
          ${fmt.integer(run.sample_count ?? samples.length)} of its samples.
          It cannot be undone. Export it first if you might want it.</p>`}
        onCancel=${() => setConfirmDelete(false)} onConfirm=${remove} />
    </div>`;
}

export function HistoryView({ notify, state }) {
  const [runs, setRuns] = useState([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState(null);
  const [selected, setSelectedState] = useState(null);

  // A selected run lives in the address too, as #history/<id>, so it can be
  // bookmarked or sent to someone and opens straight on that run.
  const setSelected = (run) => {
    setSelectedState(run);
    const want = run ? `#history/${run.id}` : "#history";
    if (location.hash !== want) history.replaceState(null, "", want);
  };
  useEffect(() => {
    const open = () => {
      const id = Number(location.hash.split("/")[1]);
      if (!id) { setSelectedState(null); return; }
      api.run(id).then((data) => data.run && setSelectedState(
        { ...data.run, sample_count: data.sample_count }))
        .catch(() => notify(`No run ${id}.`, "error"));
    };
    open();
    window.addEventListener("hashchange", open);
    return () => window.removeEventListener("hashchange", open);
  }, []);
  const [filters, setFilters] = useState({ procedure: "", status: "", search: "" });
  const [loading, setLoading] = useState(true);

  const load = () => {
    setLoading(true);
    api.runs({ ...filters, limit: 200 })
      .then((data) => { setRuns(data.runs || []); setTotal(data.total || 0); })
      .catch((error) => notify(error.message, "error"))
      .finally(() => setLoading(false));
    api.stats().then(setStats).catch(() => {});
  };

  useEffect(load, [filters.procedure, filters.status, filters.search]);

  if (selected) {
    return html`<${RunDetail} run=${selected} onClose=${() => setSelected(null)}
                              onChanged=${() => { load(); setSelected(null); }}
                              notify=${notify} devices=${state?.devices} runs=${runs}
                              canEdit=${(state?.me?.rank ?? 3) >= 1}
                              canDelete=${state?.me?.can?.admin !== false} />`;
  }

  const procedures = [...new Set(runs.map((r) => r.procedure))];

  return html`
    <div class="grid" style="gap:16px">
      <${Card} title="Test database"
        hint=${stats?.stats
          ? `${stats.stats.runs} ${stats.stats.runs === 1 ? "run" : "runs"} · ` +
            `${fmt.integer(stats.stats.samples)} samples · ` +
            `${fmt.bytes(stats.stats.bytes)}` +
            (stats.stats.path ? ` · ${stats.stats.path}` : "")
          : ""}
        actions=${html`
          <button onClick=${() => save("/api/export/runs.csv", notify)}>All runs (CSV)</button>
          <button onClick=${() => save("/api/export/runs.json", notify)}>Index (JSON)</button>
          <button onClick=${() => save("/api/export/archive.json", notify)}>Full archive</button>
          <button onClick=${() => save("/api/export/database.db", notify)}>SQLite file</button>`}>
        <p class="hint">
          <strong>All runs (CSV)</strong> is one row per test with its result —
          the spreadsheet view. <strong>Full archive</strong> and the${" "}
          <strong>SQLite file</strong> both include every recorded curve; the
          SQLite file is the database itself, taken as a consistent online backup —
          without the accounts, which never leave the machine.
        </p>
        <div class="row">
          <label class="field" style="flex:2 1 220px">
            <span>Search</span>
            <input value=${filters.search} placeholder="name, operator or notes"
                   onInput=${(e) => setFilters({ ...filters, search: e.target.value })} />
          </label>
          <label class="field">
            <span>Procedure</span>
            <select value=${filters.procedure}
                    onChange=${(e) => setFilters({ ...filters, procedure: e.target.value })}>
              <option value="">all</option>
              ${procedures.map((p) => html`
                <option value=${p}>${fmt.titleCase(p)}</option>`)}
            </select>
          </label>
          <label class="field">
            <span>Status</span>
            <select value=${filters.status}
                    onChange=${(e) => setFilters({ ...filters, status: e.target.value })}>
              <option value="">all</option>
              <option value="completed">completed</option>
              <option value="aborted">aborted</option>
              <option value="failed">failed</option>
              <option value="running">running</option>
            </select>
          </label>
        </div>
      <//>

      <${RunTrends} pump=${state?.devices?.pump} />

      <${Card} title=${`Runs${total ? ` (${runs.length} of ${total})` : ""}`}
               actions=${html`<button class="small" onClick=${load}>Refresh</button>`}>
        ${loading
          ? html`<${Empty}>Loading…<//>`
          : !runs.length
          ? html`<${Empty}>No runs recorded yet. Start one from the Process tab.<//>`
          : html`
            <div class="scroll-x">
              <table>
                <thead>
                  <tr><th>#</th><th>Started</th><th>Name</th><th>Procedure</th>
                      <th>Status</th><th>Duration</th><th>Result</th>
                      <th>Operator</th><th></th></tr>
                </thead>
                <tbody>
                  ${runs.map((run) => html`
                    <tr key=${run.id} style="cursor:pointer"
                        onClick=${() => setSelected(run)}>
                      <td class="num">${run.id}</td>
                      <td>${fmt.dateTime(run.started_at)}</td>
                      <td class="wrap-cell">${run.name}</td>
                      <td>${fmt.titleCase(run.procedure)}</td>
                      <td><${Pill} tone=${STATUS_TONE[run.status] || "idle"}>
                          ${run.status}<//></td>
                      <td class="num">${fmt.duration(run.duration)}</td>
                      <td class="num">${headline(run)}</td>
                      <td>${run.operator || "—"}</td>
                      <td><button class="small ghost">Open</button></td>
                    </tr>`)}
                </tbody>
              </table>
            </div>`}
      <//>
    </div>`;
}

/** The one number that matters for each procedure, for the table's Result column. */
export function headline(run) {
  const result = run.result || {};
  if (run.procedure === "pumpdown") {
    return result.ultimate_mbar ? `${fmt.pressure(result.ultimate_mbar, 1)} mbar` : "—";
  }
  if (run.procedure === "rate_of_rise") {
    return result.leak_rate_mbar_l_s !== undefined && result.leak_rate_mbar_l_s !== null
      ? `${fmt.pressure(result.leak_rate_mbar_l_s, 1)} mbar·L/s` : "—";
  }
  if (run.procedure === "hold") {
    return result.ultimate_mbar ? `${fmt.pressure(result.ultimate_mbar, 1)} mbar` : "—";
  }
  if (run.procedure === "bakeout") {
    return result.peak_temperature_c ? `${fmt.integer(result.peak_temperature_c)} °C` : "—";
  }
  if (run.procedure === "base_calibration" || run.procedure === "full_vacuum") {
    // Only a completed run's base pressure is quoted here. Run 26 aborted
    // after recording "base 3.4e-2 mbar" with its rotor dragged down by gas
    // load; its best pressure is the honest number for the table.
    if (result.base_mbar && run.status === "completed") {
      return `base ${fmt.pressure(result.base_mbar, 1)} mbar`;
    }
    return result.ultimate_mbar ? `best ${fmt.pressure(result.ultimate_mbar, 1)} mbar` : "—";
  }
  if (run.procedure === "deposition") {
    if (result.deposition_s) return `${fmt.duration(result.deposition_s)} deposited`;
    return result.base_mbar ? `base ${fmt.pressure(result.base_mbar, 1)} mbar` : "—";
  }
  return "—";
}
