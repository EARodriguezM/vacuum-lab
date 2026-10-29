// Accounts, where the panel points, how often it polls, what is on the rig,
// and what the server is running -- one sub-tab each, so nobody scrolls past
// the port survey to change the theme.
//
// What changes the rig's set-up -- equipment, ports, the port search -- is an
// admin's (lab/api.py POST_RULES); for everyone else those controls are
// shown, so the set-up can be read, but greyed out.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Confirm, Empty, Pairs, Pill } from "../ui.js";
import { POLL_MAX_MS, api, normaliseHost, settings, updateSettings } from "../api.js";
import { Guests, MyAccount, People, Tablets } from "./people.js";
import { beep } from "../alarms.js";
import * as fmt from "../format.js";

/**
 * Finding the instruments without being told where they are.
 *
 * The server asks every free serial port what is on it -- a read of the
 * TC 600's type, the TPG's firmware query -- so a replugged or replaced USB
 * adapter does not matter: the instrument is found wherever it answers, and
 * an instrument that goes quiet is looked for on the other ports.
 */
function AutoPorts({ state, notify, admin = true }) {
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
        <button class="small" disabled=${surveying || !admin} onClick=${detect}>
          ${surveying ? "Asking every port…" : "Detect instruments now"}</button>`}>
      <div class="row">
        <span class=${`pill ${ports.search ? "good" : "idle"}`}><span class="dot" />
          automatic search ${ports.search ? "on" : "off"}</span>
        <button class="small ghost" disabled=${!admin} onClick=${toggle}>
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
function RigEquipment({ state, notify, admin = true }) {
  const [rows, setRows] = useState(null);
  const [busy, setBusy] = useState("");
  // The question being asked, in the panel's own dialog: a browser's
  // confirm() blocks the page and looks foreign on the kiosk tablet.
  const [asking, setAsking] = useState(null);
  const unavailable = state?.unavailable || {};

  const load = () => api.equipment().then((data) => setRows(data.equipment || []))
    .catch((error) => notify(error.message, "error"));
  useEffect(() => { load(); }, []);

  const toggle = (row) => {
    const next = !row.installed;
    const uses = row.used_by.length ? ` It is used by: ${row.used_by.join(", ")}.` : "";
    setAsking({
      row, next,
      title: next ? `Mark ${row.label} installed?` : `Mark ${row.label} not installed?`,
      question: next
        ? `It counts as on the rig from now on.${uses}`
        : `Procedures that need it will be switched off.${uses}`,
    });
  };

  const apply = async ({ row, next }) => {
    setAsking(null);
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
                    <button class="small" disabled=${busy === row.key || !admin}
                            title=${admin ? "" : "Marking equipment is an admin's"}
                            onClick=${() => toggle(row)}>
                      ${row.installed ? "Mark not installed" : "Mark installed"}</button>`}</td>
                </tr>`)}
            </tbody>
          </table>
        </div>
        <p class="hint">${off
          ? `Switched off for now: ${[...new Set(rows.filter((r) => !r.installed)
              .flatMap((r) => r.used_by))].join("; ")}. Everything else needs only the pump and the gauge.`
          : "Every procedure can run on this rig."}</p>`}
      <${Confirm} open=${!!asking} title=${asking?.title}
        tone=${asking?.next ? "primary" : "danger"}
        confirmLabel=${asking?.next ? "Mark installed" : "Mark not installed"}
        body=${html`<p>${asking?.question}</p>`}
        onCancel=${() => setAsking(null)} onConfirm=${() => apply(asking)} />
    <//>`;
}

/** Pick which port an instrument is on: filter by attachment, then choose.
 *
 * Worth being a first-class control rather than a command-line flag on this
 * rig: USB adapters renumber on replug, and the gauge had to be moved from a
 * USB adapter to an onboard UART before it would talk at all.
 */
function PortPicker({ devices, notify, onChanged, admin = true }) {
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
          <button disabled=${applying === key || !admin
                    || !chosen[key] || chosen[key] === device.port}
                  onClick=${() => apply(key)}>
            ${applying === key ? "Switching…" : "Move to this port"}
          </button>
        </div>`)}
    <//>`;
}


/**
 * Which version runs, and a newer one the updater on the lab PC has found
 * (tools/autoupdate.py). It looks on GitHub every 15 minutes, accepts only a
 * version signed with a key trusted on that machine, runs its selftest there
 * while the rig is idle, and says so here. An admin installs it: the panel
 * restarts, and goes back to the version before by itself if the new one
 * does not come up healthy.
 */
function SoftwareVersion({ state, notify, admin = false }) {
  const [asking, setAsking] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const update = state?.update;
  const latest = update?.latest;
  const running = update?.running;
  const install = update?.install;
  const control = state?.control;
  const controlFree = !control?.enabled || !control.held || control.mine;
  const ready = latest?.state === "ready";
  const versionOf = (v) => v?.tag ? `${v.tag} (${v.release})` : v?.release || state?.release || "—";

  const send = async () => {
    setSending(true);
    setError("");
    try {
      const result = await api.installUpdate(latest.release);
      if (!result.ok) throw new Error(result.error);
      setAsking(false);
      notify(`Installing ${result.tag}: the panel restarts in a moment.`, "ok");
    } catch (failure) {
      setError(failure.message);
    } finally {
      setSending(false);
    }
  };

  if (!update) {
    return html`
      <${Card} title="Software version">
        <${Pairs} items=${[["Running", state?.release || "—"]]} />
        <p class="hint">This machine has no updater (tools/autoupdate.py): new versions
          are deployed from a laptop with tools/release.py.</p>
      <//>`;
  }
  const outcome = install && !update.pending && {
    started: ["warn", `An install of ${install.tag} began at ${fmt.dateTime(install.at)} and has not said how it went. On the lab PC: journalctl -u vacuum-lab-install`],
    installed: ["good", `Installed ${install.tag} at ${fmt.dateTime(install.at)}, asked for by ${install.name}.`],
    refused: ["warn", `Installing ${install.tag} did not go ahead (${fmt.dateTime(install.at)}): ${install.detail}`],
    rolled_back: ["crit", `${install.tag} was installed and went back by itself (${fmt.dateTime(install.at)}): ${install.detail}`],
    failed: ["crit", `Installing ${install.tag} failed (${fmt.dateTime(install.at)}): ${install.detail}`],
  }[install.state];

  return html`
    <div data-test="update-card"><${Card} title="Software version"
      hint="New versions come from GitHub, signed by the lab. This machine looks every 15 minutes, checks the signature and the files, and runs the selftest on the new version while the rig is idle. Only an admin installs it: the panel restarts, and goes back to the version before by itself if the new one does not come up healthy.">
      <${Pairs} items=${[
        ["Running", versionOf(running)],
        ["Last looked", update.checked_at ? fmt.dateTime(update.checked_at) : "not yet"],
      ]} />
      ${update.check_error && html`
        <${Banner} tone="warn">Could not ask GitHub: ${update.check_error}. It tries again
          in 15 minutes.<//>`}
      ${update.problem && html`<${Banner} tone="crit">${update.problem}<//>`}
      ${outcome && html`<${Banner} tone=${outcome[0]}>${outcome[1]}<//>`}
      ${!latest && !update.check_error && update.checked_at && html`
        <p class="small">This is the newest version.</p>`}
      ${latest && html`
        <div class="grid" style="gap:8px">
          <div class="row">
            <strong>${latest.title || `Version ${latest.tag}`}</strong>
            <${Pill} tone=${ready ? "good" : latest.state === "refused" ? "crit" : "accent"}>
              ${{ ready: "ready to install", waiting: "waiting to be tested",
                  testing: "being tested", refused: "refused" }[latest.state] || latest.state}<//>
          </div>
          <p class="small">${ready
            ? `Signed by ${latest.signed_by}, tagged ${fmt.dateTime(latest.tagged_at)}; the selftest passed here at ${fmt.dateTime(latest.tested_at)}.`
            : latest.detail}</p>
          ${latest.older_than_running && html`
            <${Banner} tone="warn">The panel runs a release deployed by hand after this
              version was made: installing it may go back in time.<//>`}
          ${latest.notes && html`<p class="small" style="white-space:pre-wrap">${latest.notes}</p>`}
          ${ready && html`
            <div class="row">
              <button class="primary" data-test="update-install"
                      disabled=${!admin || !controlFree || !!update.pending}
                      title=${!admin ? "Installing a version is an admin's"
                              : !controlFree ? `${control.holder} has control of the rig` : ""}
                      onClick=${() => { setError(""); setAsking(true); }}>
                Install ${latest.tag}…</button>
              ${!admin && html`<span class="small muted">An admin installs it.</span>`}
              ${admin && !controlFree && html`<span class="small muted">${control.holder} has
                control of the rig: take control first.</span>`}
            </div>`}
        </div>`}
      ${(update.refused || []).length > 0 && html`
        <details>
          <summary class="small">Versions not installed (${update.refused.length})</summary>
          <ul class="small">${update.refused.map((r) => html`
            <li key=${r.tag}><span class="mono">${r.tag}</span>: ${r.reason}</li>`)}</ul>
        </details>`}
      <${Confirm} open=${asking} title=${`Install version ${latest?.tag}?`}
        confirmLabel="Install and restart" busy=${sending} error=${error}
        onCancel=${() => setAsking(false)} onConfirm=${send}
        body=${html`
          <p>The panel restarts to run ${latest?.tag}. For about a minute nothing watches
            the rig: the pump and the gauge keep doing what they were told, but no
            interlock, alarm, recording or automatic recovery runs. Every open screen
            reloads once it is back.</p>
          <p>From now until then, new runs and power-downs are refused. A backup of the
            database is made first. If the new version is not running healthy within a
            minute, the panel goes back to ${versionOf(running)} by itself.</p>`} />
    <//></div>`;
}

/** The sub-tabs this person sees, in order. */
function settingsTabs(me, connected = true) {
  // Before the panel has ever answered, only this screen's own settings --
  // where it points -- mean anything.
  if (!connected) return [{ key: "screen", label: "This screen" }];
  return [
    me?.kind === "person" && { key: "account", label: "My account" },
    me?.kind === "person" && me?.can?.admin && { key: "people", label: "People" },
    { key: "rig", label: "Rig" },
    { key: "screen", label: "This screen" },
    { key: "server", label: "Server" },
  ].filter(Boolean);
}

export function SettingsView({ state, onSettingsChanged, notify, onSignOut, sub = "",
                               onSelect = () => {} }) {
  const current = settings();
  const [draft, setDraft] = useState({ ...current });
  const [stats, setStats] = useState(null);
  const [ping, setPing] = useState(null);

  useEffect(() => {
    // A count over every stored sample: not for a guest (lab/api.py).
    if (state?.me?.kind !== "guest") api.stats().then(setStats).catch(() => setStats(null));
    api.ping().then(setPing).catch((error) => setPing({ error: error.message }));
  }, []);

  const apply = () => {
    updateSettings({
      host: draft.host.trim(),
      pollMs: Math.min(POLL_MAX_MS, Math.max(500, Number(draft.pollMs) || 2000)),
      theme: draft.theme,
      alarmSound: draft.alarmSound !== false,
    });
    onSettingsChanged();
    notify("Settings saved.", "ok");
  };

  const devices = state?.devices || {};
  const me = state?.me;
  const admin = !!me?.can?.admin;
  const tabs = settingsTabs(me, !!state);
  const active = tabs.find((tab) => tab.key === sub) || tabs[0];
  const page = active.key;

  return html`
    <div class="grid" style="gap:16px">
      <nav class="tabs subtabs" aria-label="Settings">
        ${tabs.map((tab) => html`
          <button key=${tab.key} aria-current=${tab.key === page ? "page" : null}
                  data-test=${`subtab-${tab.key}`} onClick=${() => onSelect(tab.key)}>
            ${tab.label}
            ${tab.key === "people" && me?.waiting > 0
              ? html`<span class="badge pill warn">${me.waiting}<span class="sr-only"> waiting</span></span>`
              : null}
            ${tab.key === "server" && admin && state?.update?.latest?.state === "ready"
              ? html`<span class="badge pill good">new<span class="sr-only"> version ready</span></span>`
              : null}
          </button>`)}
      </nav>

      ${page === "account" && html`
        <${MyAccount} me=${me} notify=${notify} onSignOut=${onSignOut}
                      onChanged=${onSettingsChanged} />`}
      ${page === "people" && html`
        <${People} me=${me} notify=${notify} />
        <${Guests} me=${me} notify=${notify} />
        <${Tablets} me=${me} notify=${notify} />`}

      ${page === "screen" && html`<${Card} title="Connection"
               hint="Leave the address empty when this page is served by the lab PC itself. To drive that machine from a page served elsewhere, fill it in — and start that server with --allow-origin naming this page's origin, or it refuses the requests.">
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
        <div class="row">
          <label class="field">
            <span>Poll interval</span>
            <span class="suffix">
              <input type="number" min="500" max=${POLL_MAX_MS} step="500" value=${draft.pollMs}
                     onInput=${(e) => setDraft({ ...draft, pollMs: e.target.value })} />
              <span class="unit">ms</span>
            </span>
            <span class="help">0.5 to 10 s. Holding control needs a poll at least
              every 15 s.</span>
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
          <button onClick=${() => { if (!beep()) notify("Tap the page once first: the browser only allows sound after a touch.", "error"); }}>
            Test the alarm sound</button>
          <span class="small muted">Plays the alarm's two tones on this device, to check its speaker.</span>
        </div>
        <div class="row">
          <button class="primary" onClick=${apply}>Save</button>
          <span class="small muted">Stored in this browser only.</span>
        </div>
      <//>`}

      ${page === "rig" && html`
      <${RigEquipment} state=${state} notify=${notify} admin=${admin} />

      <${AutoPorts} state=${state} notify=${notify} admin=${admin} />

      ${me?.kind !== "guest" && html`
        <${PortPicker} devices=${devices} notify=${notify} admin=${admin}
                       onChanged=${onSettingsChanged} />`}

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
      <//>`}

      ${page === "server" && html`
      <${SoftwareVersion} state=${state} notify=${notify} admin=${admin} />
      <div class="grid two">
        <${Card} title="Server">
          <${Pairs} items=${[
            ["Service", ping?.service || "—"],
            ["Uptime", fmt.duration(state?.uptime_s)],
            ["Chamber", ping?.chamber ? `${ping.chamber.name} (${ping.chamber.id})` : "—"],
            ["Accounts", ping
              ? (ping.auth?.enabled ? "everyone signs in" : "off — open to anyone")
              : "—"],
            ["Server time", fmt.dateTime(state?.server_time)],
          ]} />
          ${ping?.error && html`<${Banner} tone="crit">${ping.error}<//>`}
          ${ping && ping.auth && !ping.auth.enabled && html`
            <${Banner} tone="crit">
              Accounts are switched off on this server (<span class="mono">--no-auth</span>).
              Anyone who can reach its port can start and stop the pump. That
              is for the simulator on a laptop, never for the lab.
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
              ${stats.stats.path && html`<div class="small muted mono">${stats.stats.path}</div>`}`
            : html`<${Empty}>Not available.<//>`}
        <//>
      </div>`}
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
