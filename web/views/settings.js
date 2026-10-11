// Where the panel points, how often it polls, and what the server is running.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill } from "../ui.js";
import { api, normaliseHost, settings, updateSettings } from "../api.js";
import * as fmt from "../format.js";

/**
 * Finding the instruments without being told where they are.
 *
 * The server asks every free serial port what is on it -- a read of the
 * TC 600's type, the TPG's firmware query -- so a replugged or replaced USB
 * adapter does not matter: the instrument is found wherever it answers, and
 * an instrument that goes quiet is looked for on the other ports.
 */
function AutoPorts({ state, notify }) {
  const [survey, setSurvey] = useState(null);
  const [surveying, setSurveying] = useState(false);
  const ports = state?.ports || {};
  const devices = state?.devices || {};
  const descriptors = Object.fromEntries((ports.devices || []).map((d) => [d.key, d]));

  const detect = async () => {
    setSurveying(true);
    try {
      setSurvey(await api.portSurvey());
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setSurveying(false);
    }
  };
  const toggle = async () => {
    try {
      const result = await api.setPortSearch(!ports.search);
      notify(`Automatic port search ${result.enabled ? "on" : "off"}.`, "ok");
    } catch (error) {
      notify(error.message, "error");
    }
  };
  const searchNow = async (key) => {
    try {
      const result = await api.searchDevice(key);
      notify(result.ok ? "Searching the other ports…" : result.error,
             result.ok ? "ok" : "error");
    } catch (error) {
      notify(error.message, "error");
    }
  };

  return html`
    <${Card} title="Finding the instruments"
      hint="Each instrument is found by asking every serial port what is on it — a read of the TC 600's type, the TPG's firmware query; nothing is written — so a replugged or replaced USB adapter does not matter. An instrument that stops answering for 10 s is looked for on the other ports, at most once a minute. A port one of the instruments is using is never probed."
      actions=${html`
        <button class="small" disabled=${surveying} onClick=${detect}>
          ${surveying ? "Asking every port…" : "Detect instruments now"}</button>`}>
      <div class="row">
        <span class=${`pill ${ports.search ? "good" : "idle"}`}><span class="dot" />
          automatic search ${ports.search ? "on" : "off"}</span>
        <button class="small ghost" onClick=${toggle}>
          ${ports.search ? "Switch off — keep the ports fixed" : "Switch on"}</button>
      </div>
      <div class="scroll-x">
        <table>
          <thead><tr><th>Instrument</th><th>Port</th><th>State</th><th></th></tr></thead>
          <tbody>
            ${Object.entries(devices).map(([key, device]) => {
              const moved = descriptors[key]?.moved;
              return html`
                <tr key=${key}>
                  <td>${device.label}</td>
                  <td class="mono">${device.port === "auto" ? "not found yet" : device.port}
                    ${moved && html`<div class="small muted">moved here from ${moved.from}
                      at ${fmt.clock(moved.at)}</div>`}</td>
                  <td><${Pill} tone=${device.connected ? "good" : "crit"}>
                    ${device.connected ? "answering" : "not answering"}<//></td>
                  <td>${!device.connected && html`
                    <button class="small" onClick=${() => searchNow(key)}>Search now</button>`}</td>
                </tr>`;
            })}
          </tbody>
        </table>
      </div>
      ${survey?.ok && html`
        <div class="scroll-x">
          <table>
            <thead><tr><th>Port</th><th>Adapter</th><th>Stable name</th><th>What answered</th></tr></thead>
            <tbody>
              ${survey.ports.map((port) => html`
                <tr key=${port.device}>
                  <td class="mono">${port.device}</td>
                  <td class="wrap-cell">${port.description || port.kind}
                    ${port.serial_number && html`<div class="small muted mono">serial ${port.serial_number}</div>`}</td>
                  <td class="mono small">${port.stable !== port.device ? port.stable : "—"}</td>
                  <td class="wrap-cell"><${Pill} tone=${port.status === "silent" ? "idle"
                    : port.status === "in use" ? "accent" : "good"}>${port.status}<//>
                    <div class="small">${port.says}</div></td>
                </tr>`)}
            </tbody>
          </table>
          ${!survey.ports.length && html`<${Empty}>No serial ports found on the server.<//>`}
          <p class="hint">Asked at ${fmt.clock(survey.at)}. Ports in use belong to one of
            the panel's instruments and were not probed.</p>
        </div>`}
    <//>`;
}

/**
 * What is physically on the rig. The software cannot see whether a magnetron
 * is bolted to the chamber, so the lab says so here, and every procedure that
 * needs something not installed is switched off -- listed on the Process tab
 * with what it is waiting for, and refused by the server.
 */
function RigEquipment({ state, notify }) {
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState("");
  const unavailable = state?.unavailable || {};

  const load = () => api.equipment().then((data) => setRows(data.equipment || []))
    .catch((error) => notify(error.message, "error"));
  useEffect(() => { load(); }, []);

  const toggle = async (row) => {
    const next = !row.installed;
    const uses = row.used_by.length ? ` It is used by: ${row.used_by.join(", ")}.` : "";
    const question = next
      ? `Mark "${row.label}" as installed on the rig?${uses}`
      : `Mark "${row.label}" as NOT installed? Procedures that need it will be switched off.${uses}`;
    if (!window.confirm(question)) return;
    setBusy(row.key);
    try {
      const result = await api.setEquipment(row.key, next);
      if (!result.ok) throw new Error(result.error);
      setRows(result.equipment);
      notify(`${row.label}: ${next ? "installed" : "not installed"}.`
             + (result.warning ? ` ${result.warning}` : ""), result.warning ? "error" : "ok");
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setBusy("");
    }
  };

  const off = Object.keys(unavailable).length;
  return html`
    <${Card} title="Rig equipment"
      hint="What is physically on the rig besides the pump and the gauge. A procedure that needs something not installed is switched off — it stays listed on the Process tab with what it is waiting for. Installed but not wired means manual: a guided run asks you to do that step by hand. Kept on the server, so every browser sees the same.">
      ${rows === null ? html`<${Empty}>Loading…<//>` : html`
        <div class="scroll-x">
          <table>
            <thead><tr><th>Equipment</th><th>On the rig</th><th>Needed by</th><th></th></tr></thead>
            <tbody>
              ${rows.map((row) => html`
                <tr key=${row.key}>
                  <td>${row.label}</td>
                  <td><${Pill} tone=${row.installed ? "good" : "idle"}>
                    ${row.driven ? "installed, software-driven" : row.installed ? "installed" : "not installed"}<//></td>
                  <td class="wrap-cell small">${row.used_by.length ? row.used_by.join(", ") : "—"}</td>
                  <td>${!row.driven && html`
                    <button class="small" disabled=${busy === row.key} onClick=${() => toggle(row)}>
                      ${row.installed ? "Mark not installed" : "Mark installed"}</button>`}</td>
                </tr>`)}
            </tbody>
          </table>
        </div>
        <p class="hint">${off
          ? `Switched off for now: ${[...new Set(rows.filter((r) => !r.installed)
              .flatMap((r) => r.used_by))].join("; ")}. Everything else needs only the pump and the gauge.`
          : "Every procedure can run on this rig."}</p>`}
    <//>`;
}

/** Pick which port an instrument is on: filter by attachment, then choose.
 *
 * Worth being a first-class control rather than a command-line flag on this
 * rig: USB adapters renumber on replug, and the gauge had to be moved from a
 * USB adapter to an onboard UART before it would talk at all.
 */
function PortPicker({ devices, notify, onChanged }) {
  const [kind, setKind] = useState("all");
  const [ports, setPorts] = useState([]);
  const [hidden, setHidden] = useState(0);
  const [showAll, setShowAll] = useState(false);
  const [chosen, setChosen] = useState({});
  const [loading, setLoading] = useState(false);
  const [applying, setApplying] = useState("");

  const scan = (which = kind, everything = showAll) => {
    setLoading(true);
    api.ports(which, everything)
      .then((data) => { setPorts(data.ports || []); setHidden(data.hidden || 0); })
      .catch((error) => notify(error.message, "error"))
      .finally(() => setLoading(false));
  };

  useEffect(() => { scan(kind, showAll); }, [kind, showAll]);

  const apply = async (key) => {
    const port = chosen[key];
    if (!port) return;
    setApplying(key);
    try {
      const result = await api.setPort(key, port);
      if (result.ok) {
        notify(`${key} is now on ${port}.`, "ok");
      } else {
        notify(result.error || `Nothing answered on ${port}.`, "error");
      }
      onChanged?.();
    } catch (error) {
      notify(error.message, "error");
    } finally {
      setApplying("");
    }
  };

  const entries = Object.entries(devices);

  return html`
    <${Card} title="Set a port by hand"
             hint="Only needed if the automatic search cannot find an instrument. Filter by how the adapter attaches, then pick a port. Changing one drops the link and reopens it, so it is refused while a run is recording."
             actions=${html`
               <button class="small" onClick=${() => scan()} disabled=${loading}>
                 ${loading ? "Scanning…" : "Rescan"}</button>`}>
      <div class="row">
        ${[["all", "All ports"], ["usb", "USB adapters"],
           ["serial", "Onboard serial"]].map(([value, label]) => html`
          <button key=${value} class=${kind === value ? "primary" : ""}
                  onClick=${() => setKind(value)}>${label}</button>`)}
      </div>

      ${(hidden > 0 || showAll) && html`
        <label class="check">
          <input type="checkbox" checked=${showAll}
                 onChange=${(e) => setShowAll(e.target.checked)} />
          <span>
            Show ports with no hardware behind them
            ${hidden > 0 ? ` (${hidden} hidden)` : ""}
            <div class="help">Linux creates /dev/ttyS0–31 whether or not the
              chips exist. The hidden ones report no UART in sysfs.</div>
          </span>
        </label>`}

      ${!loading && !ports.length
        ? html`<${Empty}>No ${kind === "all" ? "" : kind} ports found.<//>`
        : html`
          <div class="scroll-x">
            <table>
              <thead><tr><th>Port</th><th>Type</th><th>Description</th>
                         <th>Hardware id</th></tr></thead>
              <tbody>
                ${ports.map((port) => html`
                  <tr key=${port.device}>
                    <td class="mono">${port.device}</td>
                    <td><${Pill} tone=${port.kind === "usb" ? "accent" : "idle"}>
                      ${port.kind}<//></td>
                    <td class="wrap-cell">${port.description || "—"}</td>
                    <td class="mono small muted">${port.hwid || "—"}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}

      <div class="sep" />

      ${entries.map(([key, device]) => html`
        <div class="row" key=${key}>
          <label class="field">
            <span>${device.label}</span>
            <select value=${chosen[key] ?? device.port}
                    onChange=${(e) => setChosen({ ...chosen, [key]: e.target.value })}>
              <option value=${device.port}>${device.port} (current)</option>
              ${ports.filter((p) => p.device !== device.port).map((p) => html`
                <option value=${p.device}>
                  ${p.device} — ${p.description || p.kind}</option>`)}
            </select>
            <span class="help">
              ${device.connected
                ? `connected${device.baudrate ? ` at ${device.baudrate} baud` : ""}`
                : device.error}
            </span>
          </label>
          <button disabled=${applying === key
                    || !chosen[key] || chosen[key] === device.port}
                  onClick=${() => apply(key)}>
            ${applying === key ? "Switching…" : "Move to this port"}
          </button>
        </div>`)}
    <//>`;
}

export function SettingsView({ state, onSettingsChanged, notify }) {
  const current = settings();
  const [draft, setDraft] = useState({ ...current });
  const [stats, setStats] = useState(null);
  const [ping, setPing] = useState(null);

  useEffect(() => {
    api.stats().then(setStats).catch(() => setStats(null));
    api.ping().then(setPing).catch((error) => setPing({ error: error.message }));
  }, []);

  const apply = () => {
    updateSettings({
      host: draft.host.trim(),
      token: draft.token.trim(),
      pollMs: Math.max(500, Number(draft.pollMs) || 2000),
      theme: draft.theme,
      alarmSound: draft.alarmSound !== false,
    });
    onSettingsChanged();
    notify("Settings saved.", "ok");
  };

  const devices = state?.devices || {};

  return html`
    <div class="grid" style="gap:16px">
      <${Card} title="Connection"
               hint="Leave the address empty when this page is served by the lab PC itself. Fill it in to drive that machine from another one — the API sends permissive CORS headers so that works.">
        <label class="field">
          <span>Controller address</span>
          <input value=${draft.host} placeholder="e.g. 172.16.2.69:8600"
                 onInput=${(e) => setDraft({ ...draft, host: e.target.value })} />
          <span class="help">
            ${draft.host
              ? `Requests go to ${normaliseHost(draft.host)}`
              : "Requests go to this page's own origin."}
          </span>
        </label>
        <label class="field">
          <span>Shared token</span>
          <input type="password" value=${draft.token} placeholder="only if --token is set"
                 onInput=${(e) => setDraft({ ...draft, token: e.target.value })} />
          <span class="help">Sent as X-Token, and appended to download links.</span>
        </label>
        <div class="row">
          <label class="field">
            <span>Poll interval</span>
            <span class="suffix">
              <input type="number" min="500" step="500" value=${draft.pollMs}
                     onInput=${(e) => setDraft({ ...draft, pollMs: e.target.value })} />
              <span class="unit">ms</span>
            </span>
          </label>
          <label class="field">
            <span>Theme</span>
            <select value=${draft.theme}
                    onChange=${(e) => setDraft({ ...draft, theme: e.target.value })}>
              <option value="auto">follow the system</option>
              <option value="light">light</option>
              <option value="dark">dark</option>
            </select>
          </label>
        </div>
        <label class="check">
          <input type="checkbox" checked=${draft.alarmSound !== false}
                 onChange=${(e) => setDraft({ ...draft, alarmSound: e.target.checked })} />
          <span>Sound an alarm for critical alarms
            <div class="help">Two short tones every few seconds until the alarm is
              acknowledged. The browser only allows sound after the first click on
              the page.</div></span>
        </label>
        <div class="row">
          <button class="primary" onClick=${apply}>Save</button>
          <span class="small muted">Stored in this browser only.</span>
        </div>
      <//>

      <${RigEquipment} state=${state} notify=${notify} />

      <${AutoPorts} state=${state} notify=${notify} />

      <${PortPicker} devices=${devices} notify=${notify}
                     onChanged=${onSettingsChanged} />

      <${Card} title="Instruments"
               hint="Each instrument owns its serial port on the server and is polled on its own thread.">
        ${Object.keys(devices).length === 0
          ? html`<${Empty}>The server reports no devices.<//>`
          : html`
            <div class="scroll-x">
              <table>
                <thead><tr><th>Key</th><th>Instrument</th><th>Port</th>
                           <th>State</th><th>Last read</th><th>Detail</th></tr></thead>
                <tbody>
                  ${Object.entries(devices).map(([key, device]) => html`
                    <tr key=${key}>
                      <td class="mono">${key}</td>
                      <td>${device.label}</td>
                      <td class="mono">${device.port}</td>
                      <td><${Pill} tone=${device.connected ? "good" : "crit"}>
                        ${device.connected ? "connected" : "offline"}<//></td>
                      <td class="num">${device.age}s ago</td>
                      <td class="wrap-cell small muted">
                        ${device.connected
                          ? identityLine(device)
                          : device.error}
                      </td>
                    </tr>`)}
                </tbody>
              </table>
            </div>`}
      <//>

      <div class="grid two">
        <${Card} title="Server">
          <${Pairs} items=${[
            ["Service", ping?.service || "—"],
            ["Uptime", fmt.duration(state?.uptime_s)],
            ["Authentication", ping
              ? (ping.token_required ? "token required" : "open to anyone")
              : "—"],
            ["Server time", fmt.dateTime(state?.server_time)],
          ]} />
          ${ping?.error && html`<${Banner} tone="crit">${ping.error}<//>`}
          ${ping && ping.authenticated !== false && html`
            <${Banner} tone="warn">
              No token is set on the server. Anyone who can reach its port can
              start and stop the pump. Restart it with
              <span class="mono">--token</span> on a shared network.
            <//>`}
        <//>

        <${Card} title="Database">
          ${stats?.stats
            ? html`<${Pairs} items=${[
                ["Runs", fmt.integer(stats.stats.runs)],
                ["Completed", fmt.integer(stats.stats.completed)],
                ["Samples", fmt.integer(stats.stats.samples)],
                ["File size", fmt.bytes(stats.stats.bytes)],
                ...Object.entries(stats.stats.by_procedure || {}).map(
                  ([name, count]) => [fmt.titleCase(name), fmt.integer(count)]),
              ]} />
              <div class="small muted mono">${stats.stats.path}</div>`
            : html`<${Empty}>Not available.<//>`}
        <//>
      </div>
    </div>`;
}

function identityLine(device) {
  const identity = device.identity || {};
  if (device.kind === "tc600") {
    return [identity.ElecName, identity.FwVersion && `fw ${identity.FwVersion}`,
            identity.NominalSpd && `${identity.NominalSpd} Hz nominal`]
      .filter(Boolean).join(" · ");
  }
  if (device.kind === "tpg") {
    const gauges = (identity.gauges || []).map((g) => g.identifier).join(" + ");
    return [identity.firmware && `fw ${identity.firmware}`, gauges,
            identity.unit].filter(Boolean).join(" · ");
  }
  return "";
}
