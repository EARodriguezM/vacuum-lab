// Signing in, asking for an account, pairing a tablet -- and the two prompts
// a signed-in person meets later: the password again after an idle spell,
// and a new password after an admin reset the old one.
//
// The rules behind all of this live on the server (lab/auth.py). Nothing
// here decides what anybody may do; it only collects what the server asks
// for and says plainly why it said no.

import { html } from "../html.js";
import { useEffect, useRef, useState } from "preact/hooks";
import { Banner } from "../ui.js";
import { api, setDeviceSession, setPersonSession } from "../api.js";

const MODES = [
  { key: "signin", label: "Sign in" },
  { key: "signup", label: "Request an account" },
  { key: "pair", label: "Pair this tablet" },
];

function useFocus() {
  const ref = useRef(null);
  useEffect(() => { ref.current?.focus(); }, []);
  return ref;
}

function SignIn({ onSignedIn }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [working, setWorking] = useState(false);
  const first = useFocus();

  const submit = async (event) => {
    event.preventDefault();
    setWorking(true);
    setError(null);
    try {
      const result = await api.login(username.trim(), password);
      setPersonSession(result.token);
      onSignedIn(result.me);
    } catch (problem) {
      setError(problem.message);
      setPassword("");
    } finally {
      setWorking(false);
    }
  };

  return html`
    <form class="grid" style="gap:12px" onSubmit=${submit}>
      <label class="field"><span>Username</span>
        <input ref=${first} value=${username} autocomplete="username"
               autocapitalize="none" spellcheck="false" data-test="signin-username"
               onInput=${(e) => setUsername(e.target.value)} />
      </label>
      <label class="field"><span>Password</span>
        <input type="password" value=${password} autocomplete="current-password"
               data-test="signin-password" onInput=${(e) => setPassword(e.target.value)} />
      </label>
      ${error && html`<${Banner} tone="crit">${error}<//>`}
      <button class="primary" type="submit" data-test="signin-submit"
              disabled=${working || !username.trim() || !password}>
        ${working ? "Signing in…" : "Sign in"}</button>
      <p class="hint">Forgot the password? An admin can set a temporary one
        under Settings → People.</p>
    </form>`;
}

function SignUp() {
  const [form, setForm] = useState({ username: "", name: "", password: "",
                                     repeat: "", note: "" });
  const [error, setError] = useState(null);
  const [sent, setSent] = useState(false);
  const [working, setWorking] = useState(false);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const submit = async (event) => {
    event.preventDefault();
    if (form.password !== form.repeat) {
      setError("The two passwords differ.");
      return;
    }
    setWorking(true);
    setError(null);
    try {
      await api.signup({ username: form.username.trim().toLowerCase(),
                         name: form.name.trim(), password: form.password,
                         note: form.note.trim() });
      setSent(true);
    } catch (problem) {
      setError(problem.message);
    } finally {
      setWorking(false);
    }
  };

  if (sent) {
    return html`
      <${Banner} tone="good">
        <strong>Request sent.</strong> An admin approves it and chooses what you
        may do — watch, or operate the rig. Then sign in with${" "}
        <span class="mono">${form.username.trim().toLowerCase()}</span>${" "}and the
        password you chose.
      <//>`;
  }
  return html`
    <form class="grid" style="gap:12px" onSubmit=${submit}>
      <div class="row form-row">
        <label class="field"><span>Username</span>
          <input value=${form.username} autocomplete="username" autocapitalize="none"
                 spellcheck="false" placeholder="e.g. arodriguez"
                 onInput=${set("username")} />
          <span class="help">3–32 lower-case letters, digits, dots or dashes.</span>
        </label>
        <label class="field"><span>Your name</span>
          <input value=${form.name} autocomplete="name" onInput=${set("name")} />
          <span class="help">Shown next to everything you do on the rig.</span>
        </label>
      </div>
      <div class="row form-row">
        <label class="field"><span>Password</span>
          <input type="password" value=${form.password} autocomplete="new-password"
                 onInput=${set("password")} />
          <span class="help">At least 8 characters.</span>
        </label>
        <label class="field"><span>Password again</span>
          <input type="password" value=${form.repeat} autocomplete="new-password"
                 onInput=${set("repeat")} />
        </label>
      </div>
      <label class="field"><span>Note for the admin (optional)</span>
        <input value=${form.note} placeholder="Who you are, which project"
               onInput=${set("note")} />
      </label>
      ${error && html`<${Banner} tone="crit">${error}<//>`}
      <button class="primary" type="submit"
              disabled=${working || !form.username || !form.password}>
        ${working ? "Sending…" : "Request the account"}</button>
    </form>`;
}

function Pair({ onSignedIn }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState(null);
  const [working, setWorking] = useState(false);

  const submit = async (event) => {
    event.preventDefault();
    setWorking(true);
    setError(null);
    try {
      const result = await api.pair(code);
      setDeviceSession(result.token);
      onSignedIn(result.me);
    } catch (problem) {
      setError(problem.message);
    } finally {
      setWorking(false);
    }
  };

  return html`
    <form class="grid" style="gap:12px" onSubmit=${submit}>
      <p class="small">
        A paired tablet always shows the rig and sounds its alarms, with nobody
        signed in. It can only watch: to act, a person signs in on it, and is
        signed out again after a quarter of an hour without a touch.
      </p>
      <label class="field"><span>Pairing code</span>
        <input value=${code} class="mono code-input" autocapitalize="characters"
               autocomplete="off" spellcheck="false" placeholder="XXX-XXX-XXX"
               onInput=${(e) => setCode(e.target.value.toUpperCase())} />
        <span class="help">An admin makes one under Settings → People →
          Tablets. It works once, for 15 minutes.</span>
      </label>
      ${error && html`<${Banner} tone="crit">${error}<//>`}
      <button class="primary" type="submit" disabled=${working || code.length < 9}>
        ${working ? "Pairing…" : "Pair this tablet"}</button>
    </form>`;
}

/**
 * The sign-in page. `onCancel` is offered on a paired tablet, which has a
 * live view to go back to.
 */
export function LoginView({ ping, onSignedIn, onCancel }) {
  const [mode, setMode] = useState("signin");
  const chamber = ping?.chamber?.name;
  const noOwner = ping?.auth?.enabled && ping?.auth?.owner === false;
  const modes = onCancel ? MODES.filter((m) => m.key === "signin") : MODES;

  return html`
    <div class="login">
      <section class="card login-card">
        <header class="login-head">
          <h1>Vacuum Lab Control</h1>
          <span class="sub">${chamber ? `${chamber} · ` : ""}sign in to continue</span>
        </header>
        ${noOwner && html`
          <${Banner} tone="warn">
            <strong>Nobody owns this chamber yet.</strong> The first account is
            made on the machine itself, so nobody on the network can claim the
            rig: <span class="mono">./tools/users.py owner YOURNAME</span>
          <//>`}
        ${modes.length > 1 && html`
          <nav class="tabs subtabs" aria-label="Sign-in options">
            ${modes.map((entry) => html`
              <button key=${entry.key} type="button"
                      aria-current=${mode === entry.key ? "page" : null}
                      onClick=${() => setMode(entry.key)}>${entry.label}</button>`)}
          </nav>`}
        ${mode === "signin" && html`<${SignIn} onSignedIn=${onSignedIn} />`}
        ${mode === "signup" && html`<${SignUp} />`}
        ${mode === "pair" && html`<${Pair} onSignedIn=${onSignedIn} />`}
        ${onCancel && html`
          <button class="ghost" type="button" onClick=${onCancel}>
            Back to watching</button>`}
      </section>
    </div>`;
}

/** The password again, after a quarter of an hour without a touch. */
export function ReauthDialog({ open, me, onDone, onCancel }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [working, setWorking] = useState(false);
  const input = useRef(null);

  useEffect(() => {
    if (open) {
      setPassword("");
      setError(null);
      setTimeout(() => input.current?.focus(), 0);
    }
  }, [open]);
  if (!open) return null;

  const submit = async (event) => {
    event.preventDefault();
    setWorking(true);
    setError(null);
    try {
      const result = await api.reauth(password);
      onDone(result.me);
    } catch (problem) {
      setError(problem.message);
      setPassword("");
    } finally {
      setWorking(false);
    }
  };

  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <form class="dialog" role="dialog" aria-modal="true" onSubmit=${submit}>
        <h2>Still there, ${me?.name || "operator"}?</h2>
        <div class="small">Nothing was touched for a while, so the panel only
          watches until you type your password again. That keeps somebody else
          from acting in your name on a screen you walked away from.</div>
        <label class="field"><span>Password</span>
          <input ref=${input} type="password" value=${password}
                 autocomplete="current-password" data-test="reauth-password"
                 onInput=${(e) => setPassword(e.target.value)} />
        </label>
        ${error && html`<${Banner} tone="crit">${error}<//>`}
        <div class="actions">
          <button type="button" onClick=${onCancel}>Keep watching</button>
          <button class="primary" type="submit" disabled=${working || !password}
                  data-test="reauth-submit">
            Continue</button>
        </div>
      </form>
    </div>`;
}

/** Choosing a new password: forced after a reset, or from My account. */
export function ChangePassword({ forced = false, onDone, notify }) {
  const [form, setForm] = useState({ current: "", next: "", repeat: "" });
  const [error, setError] = useState(null);
  const [working, setWorking] = useState(false);
  const set = (key) => (e) => setForm({ ...form, [key]: e.target.value });

  const submit = async (event) => {
    event.preventDefault();
    if (form.next !== form.repeat) {
      setError("The two new passwords differ.");
      return;
    }
    setWorking(true);
    setError(null);
    try {
      const result = await api.changePassword(form.current, form.next);
      setForm({ current: "", next: "", repeat: "" });
      notify?.("Password changed. Your other sessions were signed out.", "ok");
      onDone?.(result.me);
    } catch (problem) {
      setError(problem.message);
    } finally {
      setWorking(false);
    }
  };

  return html`
    <form class="grid" style="gap:12px" onSubmit=${submit}>
      ${forced && html`
        <${Banner} tone="warn">An admin set a temporary password for you.
          Choose your own before doing anything else.<//>`}
      <label class="field"><span>${forced ? "Temporary password" : "Current password"}</span>
        <input type="password" value=${form.current} autocomplete="current-password"
               onInput=${set("current")} />
      </label>
      <div class="row form-row">
        <label class="field"><span>New password</span>
          <input type="password" value=${form.next} autocomplete="new-password"
                 onInput=${set("next")} />
          <span class="help">At least 8 characters.</span>
        </label>
        <label class="field"><span>New password again</span>
          <input type="password" value=${form.repeat} autocomplete="new-password"
                 onInput=${set("repeat")} />
        </label>
      </div>
      ${error && html`<${Banner} tone="crit">${error}<//>`}
      <div class="row">
        <button class="primary" type="submit"
                disabled=${working || !form.current || !form.next}>
          ${working ? "Saving…" : "Change password"}</button>
      </div>
    </form>`;
}
