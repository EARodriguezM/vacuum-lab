// Where the panel points, how often it polls, and what the server is running.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill } from "../ui.js";
import { api, normaliseHost, settings, updateSettings } from "../api.js";
import * as fmt from "../format.js";

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
    <${Card} title="Instrument ports"
             hint="Filter by how the adapter attaches, then pick a port. Changing one drops the link and reopens it, so it is refused while a run is recording."
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
          <input value=${draft.host} placeholder="e.g. 172.16.2.62:8600"
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
        <div class="row">
          <button class="primary" onClick=${apply}>Save</button>
          <span class="small muted">Stored in this browser only.</span>
        </div>
      <//>

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
