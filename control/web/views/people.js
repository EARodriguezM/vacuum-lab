// Accounts, from the Settings page: your own, and -- for admins -- everyone
// else's and the paired tablets.
//
// Every rule here is enforced by the server (lab/auth.py). The page only
// hides what the server would refuse, so that an operator is not offered a
// button that can only say no: who may approve whom, that only the owner
// appoints admins, that nobody edits their own role.

import { html } from "../html.js";
import { useEffect, useState } from "preact/hooks";
import { Banner, Card, Confirm, Empty, Pairs, Pill } from "../ui.js";
import { api } from "../api.js";
import { ChangePassword } from "./login.js";
import * as fmt from "../format.js";

const ROLE_TONE = { owner: "accent", admin: "accent", operator: "good", viewer: "idle" };

const ROLE_HELP = {
  viewer: "watches everything, changes nothing",
  operator: "drives the rig while holding control",
  admin: "sets the rig up, manages people, may override interlocks",
  owner: "one per chamber; also appoints admins",
};

export function RolePill({ role }) {
  return html`<${Pill} tone=${ROLE_TONE[role] || "idle"}>${role || "no role"}<//>`;
}

/** Your own account: role, password, asking for more. */
export function MyAccount({ me, notify, onSignOut, onChanged }) {
  const [asking, setAsking] = useState("");
  const [note, setNote] = useState("");
  if (!me || me.kind !== "person") return null;
  const next = me.role === "viewer" ? "operator" : me.role === "operator" ? "admin" : null;

  const request = async () => {
    try {
      await api.requestRole(asking, note);
      notify(`Asked to become ${asking}. An admin will see it under People.`, "ok");
      setAsking("");
      setNote("");
    } catch (problem) {
      notify(problem.message, "error");
    }
  };

  return html`
    <${Card} title="My account"
      actions=${html`<button onClick=${onSignOut}>Sign out</button>`}>
      <${Pairs} items=${[
        ["Name", me.name],
        ["Username", html`<span class="mono">${me.username}</span>`],
        ["Role here", html`<${RolePill} role=${me.role} />`],
        ["What it allows", ROLE_HELP[me.role] || "—"],
      ]} />
      ${next && html`
        <div class="row">
          ${asking
            ? html`
              <label class="field" style="flex:2 1 240px">
                <span>Why (optional)</span>
                <input value=${note} placeholder="e.g. I run the depositions for the hydrogen project"
                       onInput=${(e) => setNote(e.target.value)} />
              </label>
              <button class="primary" onClick=${request}>Send request</button>
              <button onClick=${() => setAsking("")}>Cancel</button>`
            : html`<button onClick=${() => setAsking(next)}>Ask to become ${next}</button>`}
        </div>`}
      <details>
        <summary class="small">Change my password</summary>
        <${ChangePassword} notify=${notify} onDone=${onChanged} />
      </details>
    <//>`;
}

function TemporaryPassword({ shown, onClose }) {
  if (!shown) return null;
  return html`
    <${Confirm} open=${true} title=${`Temporary password for ${shown.name}`}
      confirmLabel="Done" onCancel=${onClose} onConfirm=${onClose}
      body=${html`
        <p>Give this to ${shown.name} in person. It works once: they must
          choose their own password when they sign in. It is not shown again.</p>
        <p class="mono temp-password">${shown.password}</p>`} />`;
}

/** Everyone with an account here, for an admin. */
export function People({ me, notify }) {
  const [report, setReport] = useState(null);
  const [error, setError] = useState(null);
  const [shown, setShown] = useState(null);
  const [handing, setHanding] = useState(null);
  const [password, setPassword] = useState("");

  const allowed = !!me?.can?.admin && me?.kind === "person";
  const load = () => api.users()
    .then((data) => { setReport(data); setError(null); })
    .catch((problem) => setError(problem.message));
  useEffect(() => { if (allowed) load(); }, [allowed]);

  if (!allowed) return null;

  const isOwner = me.role === "owner";
  const grantable = isOwner ? ["viewer", "operator", "admin"] : ["viewer", "operator"];
  const act = async (promise, message) => {
    try {
      const result = await promise;
      if (message) notify(message, "ok");
      load();
      return result;
    } catch (problem) {
      notify(problem.message, "error");
      return null;
    }
  };

  const people = report?.people || [];
  // Whether requests prove their institutional address by a mailed code.
  const verifying = !!report?.email?.verifying;
  const pending = people.filter((p) => p.status === "pending");
  const others = people.filter((p) => p.status !== "pending");
  const requests = report?.requests || [];
  // What this admin may change: never themselves, never the owner, and an
  // admin only if they are the owner.
  const editable = (person) => !person.you && person.role !== "owner"
    && (person.role !== "admin" || isOwner);

  return html`
    <${Card} title="People"
      hint="Everyone who can sign in to this chamber. Watching needs an account too; nobody gets in until an admin approves them."
      actions=${html`<button class="small" onClick=${load}>Refresh</button>`}>
      ${error && html`<${Banner} tone="crit">${error}<//>`}

      ${pending.length > 0 && !verifying && html`
        <${Banner} tone="warn">
          This server cannot send mail, so nobody's institutional address is
          checked: make sure each request is the person it names before
          approving it. To check them by a mailed code, set up a mail account on
          the machine: <span class="mono">./tools/users.py mail set</span>
        <//>`}
      ${pending.length > 0 && html`
        <h3>Waiting for approval</h3>
        <div class="scroll-x">
          <table>
            <thead><tr><th>Name</th><th>Institutional address</th><th>Asked</th>
                       <th>Note</th><th /></tr></thead>
            <tbody>
              ${pending.map((person) => {
                const unconfirmed = verifying && !person.email_verified_at;
                return html`
                <tr key=${person.id}>
                  <td>${person.display_name}</td>
                  <td><span class="mono">${person.email || person.username}</span>
                    <div class="small" data-test=${`address-${person.username}`}>
                      ${person.email_verified_at
                        ? `✓ confirmed ${fmt.dateTime(person.email_verified_at)}`
                        : verifying ? "✗ not confirmed yet" : "not checked"}</div></td>
                  <td>${fmt.dateTime(person.created_at)}</td>
                  <td class="wrap-cell small">${person.note || "—"}</td>
                  <td>
                    <div class="row">
                      ${grantable.map((role) => html`
                        <button key=${role} class=${`small${role === "viewer" ? "" : " primary"}`}
                                disabled=${unconfirmed}
                                title=${unconfirmed ? "Waits until the code mailed to the address is typed back" : ""}
                                onClick=${() => act(api.approveUser(person.id, role),
                                  `${person.display_name} approved as ${role}.`)}>
                          Approve as ${role}</button>`)}
                      <button class="small danger"
                              onClick=${() => act(api.declineUser(person.id),
                                `Request from ${person.display_name} declined.`)}>
                        Decline</button>
                    </div>
                  </td>
                </tr>`;
              })}
            </tbody>
          </table>
        </div>`}

      ${requests.length > 0 && html`
        <h3>Asking for a higher role</h3>
        ${requests.map((request) => html`
          <div class="row" key=${request.id}>
            <span><strong>${request.display_name}</strong>${" "}
              (<span class="mono">${request.username}</span>) asks to become${" "}
              <strong>${request.role}</strong>${request.note ? ` — “${request.note}”` : ""}.</span>
            <button class="small primary"
                    disabled=${request.role === "admin" && !isOwner}
                    title=${request.role === "admin" && !isOwner ? "Only the owner appoints admins" : ""}
                    onClick=${() => act(api.decideRequest(request.id, true),
                      `${request.display_name} is now ${request.role}.`)}>Approve</button>
            <button class="small"
                    onClick=${() => act(api.decideRequest(request.id, false),
                      "Request declined.")}>Decline</button>
          </div>`)}`}

      ${!report && !error
        ? html`<${Empty}>Loading…<//>`
        : html`
          <div class="scroll-x">
            <table>
              <thead><tr><th>Name</th><th>Username</th><th>Role</th><th>Status</th>
                         <th>Last sign-in</th><th /></tr></thead>
              <tbody>
                ${others.map((person) => html`
                  <tr key=${person.id}>
                    <td>${person.display_name}${person.you ? html` <span class="muted">(you)</span>` : null}</td>
                    <td class="mono">${person.username}</td>
                    <td>
                      ${editable(person) && person.status === "active"
                        ? html`
                          <select value=${person.role || ""} aria-label=${`Role of ${person.display_name}`}
                                  onChange=${(e) => act(api.setRole(person.id, e.target.value),
                                    `${person.display_name} is now ${e.target.value}.`)}>
                            ${grantable.map((role) => html`<option value=${role}>${role}</option>`)}
                          </select>`
                        : html`<${RolePill} role=${person.role} />`}
                    </td>
                    <td><${Pill} tone=${person.status === "active" ? "good" : "crit"}>
                      ${person.status}<//></td>
                    <td>${person.last_login_at ? fmt.dateTime(person.last_login_at) : "never"}</td>
                    <td>
                      ${editable(person) && html`
                        <div class="row">
                          <button class="small"
                                  onClick=${async () => {
                                    const result = await act(api.resetPassword(person.id));
                                    if (result?.temporary) {
                                      setShown({ name: person.display_name, password: result.temporary });
                                    }
                                  }}>Reset password</button>
                          <button class=${`small${person.status === "active" ? " danger" : ""}`}
                                  onClick=${() => act(api.setEnabled(person.id, person.status !== "active"),
                                    `${person.display_name} ${person.status === "active" ? "disabled" : "enabled"}.`)}>
                            ${person.status === "active" ? "Disable" : "Enable"}</button>
                        </div>`}
                      ${isOwner && !person.you && person.status === "active" && html`
                        <button class="small ghost" onClick=${() => { setHanding(person); setPassword(""); }}>
                          Make owner…</button>`}
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}
      <${TemporaryPassword} shown=${shown} onClose=${() => setShown(null)} />
      <${Confirm} open=${!!handing} tone="danger" confirmLabel="Hand over"
        title=${`Make ${handing?.display_name} the owner?`}
        body=${html`<p>There is one owner per chamber. ${handing?.display_name} becomes
          it, and you become an admin. Type your password to confirm.</p>`}
        extra=${html`<label class="field"><span>Your password</span>
          <input type="password" value=${password} autocomplete="current-password"
                 onInput=${(e) => setPassword(e.target.value)} /></label>`}
        onCancel=${() => setHanding(null)}
        onConfirm=${async () => {
          const person = handing;
          setHanding(null);
          await act(api.transferOwner(person.id, password),
                    `${person.display_name} now owns this chamber.`);
        }} />
    <//>`;
}

/** Paired tablets, for an admin: add one, see it, remove it. */
export function Tablets({ me, notify }) {
  const [report, setReport] = useState(null);
  const [label, setLabel] = useState("");
  const [code, setCode] = useState(null);

  const allowed = !!me?.can?.admin && me?.kind === "person";
  const load = () => api.users().then(setReport).catch(() => setReport(null));
  useEffect(() => { if (allowed) load(); }, [allowed]);
  if (!allowed) return null;

  const add = async () => {
    try {
      const result = await api.addTablet(label || "Tablet");
      setCode({ code: result.code, label: result.device?.display_name,
                until: Date.now() / 1000 + result.expires_in_s });
      setLabel("");
      load();
    } catch (problem) {
      notify(problem.message, "error");
    }
  };
  const renew = async (tablet) => {
    try {
      const result = await api.tabletCode(tablet.id);
      setCode({ code: result.code, label: tablet.display_name,
                until: Date.now() / 1000 + result.expires_in_s });
      load();
    } catch (problem) {
      notify(problem.message, "error");
    }
  };
  const remove = async (tablet) => {
    try {
      await api.removeTablet(tablet.id);
      notify(`${tablet.display_name} removed. It shows the sign-in page from now on.`, "ok");
      load();
    } catch (problem) {
      notify(problem.message, "error");
    }
  };

  const tablets = report?.tablets || [];
  return html`
    <${Card} title="Tablets"
      hint="A paired tablet keeps showing the rig and sounding alarms with nobody signed in. It can only watch; a person signs in on it to act.">
      ${code && html`
        <${Banner} tone="good">
          On ${code.label || "the tablet"}: open the panel, choose${" "}
          <strong>Pair this tablet</strong> and type${" "}
          <span class="mono pair-code">${code.code}</span>
          ${" "}— it works once, until ${fmt.clock(code.until)}.
        <//>`}
      ${tablets.length === 0
        ? html`<${Empty}>No tablet is paired yet.<//>`
        : html`
          <div class="scroll-x">
            <table>
              <thead><tr><th>Tablet</th><th>State</th><th>Last seen</th><th>From</th><th /></tr></thead>
              <tbody>
                ${tablets.map((tablet) => html`
                  <tr key=${tablet.id}>
                    <td>${tablet.display_name} <span class="muted mono small">${tablet.username}</span></td>
                    <td><${Pill} tone=${tablet.paired ? "good" : "warn"}>
                      ${tablet.paired ? "paired" : tablet.code_expires_at ? "code waiting" : "not paired"}<//></td>
                    <td>${tablet.last_seen_at ? fmt.dateTime(tablet.last_seen_at) : "—"}</td>
                    <td class="mono">${tablet.address || "—"}</td>
                    <td>
                      <div class="row">
                        ${!tablet.paired && html`
                          <button class="small" onClick=${() => renew(tablet)}>New code</button>`}
                        <button class="small danger" onClick=${() => remove(tablet)}>Remove</button>
                      </div>
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}
      <div class="row">
        <label class="field">
          <span>Name for a new tablet</span>
          <input value=${label} placeholder="e.g. Wall tablet, chamber 1"
                 onInput=${(e) => setLabel(e.target.value)} />
        </label>
        <button class="primary" onClick=${add}>Pair a tablet</button>
      </div>
    <//>`;
}
