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

/** The username in what was typed: the name, or the whole address. */
function bareUsername(typed, domain) {
  const name = typed.trim().toLowerCase();
  return domain && name.endsWith(`@${domain}`) ? name.slice(0, -domain.length - 1) : name;
}

/**
 * Asking for an account. With Google sign-in set up on the server, the
 * person chooses a password and writes the note first, then proves their
 * institutional account with Google's device flow -- a code typed at
 * google.com/device on their own phone or computer -- and the request is
 * made the moment Google says whose account it was, with the username and
 * name Google gives (lab/auth.py, lab/google.py). No verified identity ever
 * waits on this screen for somebody else to put a password to. Without
 * Google, they type the username, and an admin checks who they are.
 */
function SignUp({ domain, google }) {
  const [form, setForm] = useState({ username: "", name: "", password: "",
                                     repeat: "", note: "" });
  const [error, setError] = useState(null);
  const [flow, setFlow] = useState(null);   // the Google sign-in under way
  const [sent, setSent] = useState(null);   // {username, name?, verified}
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
      if (google) {
        setFlow(await api.googleStart(form.password, form.note.trim()));
      } else {
        const username = bareUsername(form.username, domain);
        const result = await api.signup({ username, name: form.name.trim(),
                                          password: form.password,
                                          note: form.note.trim() });
        setSent({ username: result.username || username, verified: false });
      }
    } catch (problem) {
      setError(problem.message);
    } finally {
      setWorking(false);
    }
  };

  if (sent) {
    return html`
      <${Banner} tone="good">
        <strong>Request sent${sent.verified ? `, as ${sent.name}` : ""}.</strong>
        ${sent.verified ? " Google confirmed your institutional account." : ""} An
        admin approves it and chooses what you may do — watch, or operate the
        rig. Then sign in with${" "}<span class="mono">${sent.username}</span>${" "}and
        the password you chose.
      <//>`;
  }
  if (flow) {
    return html`<${GoogleStep} domain=${domain} flow=${flow}
      onDone=${(answer) => setSent({ username: answer.username,
                                     name: answer.identity?.name, verified: true })}
      onEnd=${(problem) => { setFlow(null); setError(problem); }} />`;
  }
  return html`
    <form class="grid" style="gap:12px" onSubmit=${submit}>
      ${google ? html`
        <p class="small">Accounts are for people with an institutional account.
          Choose a password for this panel, then sign in with your${" "}
          <strong>@${domain}</strong>${" "}Google account: the request is made
          with the username and name Google gives.</p>` : html`
        <label class="field"><span>Institutional username</span>
          <span class="suffix">
            <input value=${form.username} autocomplete="username" autocapitalize="none"
                   spellcheck="false" placeholder="e.g. anperez" data-test="signup-username"
                   onInput=${set("username")} />
            <span class="unit">@${domain}</span></span>
          <span class="help">Your institutional email before the @: for
            anperez@${domain}, type anperez.</span>
        </label>
        <label class="field"><span>Your name</span>
          <input value=${form.name} autocomplete="name" onInput=${set("name")} />
          <span class="help">Shown next to everything you do on the rig.</span>
        </label>`}
      <div class="row form-row">
        <label class="field"><span>Password for this panel</span>
          <input type="password" value=${form.password} autocomplete="new-password"
                 data-test="signup-password" onInput=${set("password")} />
          <span class="help">At least 8 characters. Not your institutional
            account's password.</span>
        </label>
        <label class="field"><span>Password again</span>
          <input type="password" value=${form.repeat} autocomplete="new-password"
                 data-test="signup-repeat" onInput=${set("repeat")} />
        </label>
      </div>
      <label class="field"><span>Note for the admin (optional)</span>
        <input value=${form.note} placeholder="Who you are, which project"
               data-test="signup-note" onInput=${set("note")} />
      </label>
      ${error && html`<${Banner} tone="crit">${error}<//>`}
      <button class="primary" type="submit" data-test="signup-submit"
              disabled=${working || !form.password || (!google && !form.username)}>
        ${working ? (google ? "Asking Google…" : "Sending…")
          : google ? "Continue with Google" : "Request the account"}</button>
    </form>`;
}

/**
 * The Google sign-in under way: the code to type at Google, shown here, and
 * the server asked at the pace Google set until the request is made (or the
 * sign-in ends). Polled only while this step is on screen -- the sign-in
 * page has no state poll to join.
 */
function GoogleStep({ domain, flow, onDone, onEnd }) {
  // Set while the panel or the network does not answer: polling goes on.
  const [hiccup, setHiccup] = useState(null);
  useEffect(() => {
    let stopped = false;
    let timer = null;
    const wait = Math.max(2, Number(flow.interval) || 5) * 1000;
    const tick = async () => {
      try {
        const answer = await api.googlePoll(flow.flow);
        if (stopped) return;
        setHiccup(null);
        if (answer.state === "done") {
          onDone(answer);
          return;
        }
      } catch (problem) {
        if (stopped) return;
        // A refusal that ended the sign-in on the server ends it here too,
        // with its own reason; the panel busy or the network gone a moment
        // does not -- the sign-in is still there to ask about.
        if (problem.payload?.ended || [400, 403, 409, 410].includes(problem.status)) {
          onEnd(problem.message);
          return;
        }
        setHiccup(problem.message);
      }
      timer = setTimeout(tick, wait);
    };
    timer = setTimeout(tick, wait);
    return () => { stopped = true; clearTimeout(timer); };
  }, [flow.flow]);

  return html`
    <div class="grid" style="gap:12px">
      <p class="small">On your phone or computer, open${" "}
        <a href=${flow.url} target="_blank" rel="noopener noreferrer">
          ${flow.url.replace(/^https:\/\//, "")}</a>, sign in with your${" "}
        <strong>@${domain}</strong>${" "}account, and type this code:</p>
      <div class="device-code mono" data-test="signup-code">${flow.user_code}</div>
      <p class="small muted">Waiting for Google… the request is sent by itself
        once you accept: keep this page open until it says so. Type this code
        only on your own account; never type a code somebody else gives you. It
        works for${" "}${Math.round((flow.expires_in || 1800) / 60)}${" "}minutes.</p>
      ${hiccup && html`<${Banner} tone="warn">Still trying: ${hiccup}<//>`}
      <button type="button" class="ghost" onClick=${() => onEnd(null)}>Cancel</button>
    </div>`;
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
              <button key=${entry.key} type="button" data-test=${`signin-mode-${entry.key}`}
                      aria-current=${mode === entry.key ? "page" : null}
                      onClick=${() => setMode(entry.key)}>${entry.label}</button>`)}
          </nav>`}
        ${mode === "signin" && html`<${SignIn} onSignedIn=${onSignedIn} />`}
        ${mode === "signup" && html`<${SignUp} domain=${ping?.auth?.email_domain || "unal.edu.co"}
                                                google=${!!ping?.auth?.google_signin} />`}
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
