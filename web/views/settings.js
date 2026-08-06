// Where the panel points, how often it polls, and what the server is running.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Empty, Pairs, Pill } from "../ui.js";
import { api, normaliseHost, settings, updateSettings } from "../api.js";
import * as fmt from "../format.js";

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
