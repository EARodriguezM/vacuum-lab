// Root of the panel: signing in, polling, navigation, and the confirmation gate.
//
// One poll drives everything. /api/state carries every instrument's cached
// snapshot plus the run engine's status -- and who is looking, and who has
// control of the rig -- so the views never fetch on their own except for the
// things that are expensive or rarely change (accessory parameters, run
// history, the capability probe). Charts that need more join the same poll
// (api.js joinPoll), so the page redraws once per cycle, not once per timer.
//
// Every command that moves hardware goes through `ask()`, which puts the
// action in front of the operator in plain words before it is sent. The
// server independently refuses anything arriving without `confirm`, and
// decides who may send it at all (lab/auth.py), so this dialog is the
// courtesy, not the safety.

import { html } from "./html.js";
import { render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { AuthError, api, onAuthProblem, onSessionChange, pollParticipants,
         sessionTokens, setPersonSession, settings, updateSettings } from "./api.js";
import { Banner, Choose, Confirm, Pill, Toasts, useToasts } from "./ui.js";
import { AlarmBar, useAlarmSignals } from "./alarms.js";
import { DashboardView } from "./views/dashboard.js";
import { Overview } from "./views/overview.js";
import { EquipmentView } from "./views/equipment.js";
import { TestsView } from "./views/tests.js";
import { HistoryView } from "./views/history.js";
import { SettingsView } from "./views/settings.js";
import { ChangePassword, LoginView, ReauthDialog } from "./views/login.js";
import * as fmt from "./format.js";

const TABS = [
  { key: "dashboard", label: "Dashboard" },
  { key: "overview", label: "Overview" },
  { key: "equipment", label: "Equipment" },
  { key: "tests", label: "Process" },
  { key: "history", label: "History" },
  { key: "settings", label: "Settings" },
];

// The pump and the gauge had tabs of their own. Old links and bookmarks land
// on their sub-tab of Equipment.
const MOVED = { pump: "equipment/pump", gauge: "equipment/gauge" };

// A deliberate touch is reported at most this often: often enough that a
// person using the panel never meets the idle prompt (lab/auth.py IDLE_S).
const TOUCH_EVERY_MS = 60000;

/** What a paired tablet says when it signs a person out for walking away. */
const idleMessage = (me) =>
  `Signed out after ${Math.round((me?.idle_after_s || 900) / 60)} min without a touch. ` +
  "The tablet keeps watching.";

const NO_LIVE = { runId: null, samples: [], lastT: null, fullAt: 0 };

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

/** The hash is "#tab" or "#tab/detail": "#history/26" opens run 26. */
function parseHash() {
  let [head, sub] = location.hash.replace("#", "").split("/");
  if (MOVED[head]) {
    window.history.replaceState(null, "", `#${MOVED[head]}`);
    [head, sub] = MOVED[head].split("/");
  }
  return { tab: head || "dashboard", sub: sub || "" };
}

/**
 * The run in progress: the whole run once, decimated, then only the rows
 * newer than the last one held -- one or two per poll instead of the whole
 * run again. Reloaded once a minute so a long run stays decimated.
 */
async function fetchLive(held) {
  const reload = held.runId === null || Date.now() - held.fullAt > 60000;
  const payload = reload ? await api.liveRun(1500) : await api.liveSince(held.lastT ?? -1);
  return { payload, reload };
}

function mergeLive(held, { payload, reload }) {
  const rows = payload?.samples || [];
  if (reload) {
    return { runId: payload?.run_id ?? null, samples: rows,
             lastT: rows.length ? rows[rows.length - 1].t : null, fullAt: Date.now() };
  }
  if (payload?.run_id !== held.runId) return NO_LIVE;   // a new run: start over
  if (!rows.length) return held;                         // nothing new: no redraw
  return { ...held, samples: held.samples.concat(rows), lastT: rows[rows.length - 1].t };
}

/**
 * The deliberate way past an interlock, for an admin only.
 *
 * It quotes the rule's own words and will not send until the admin ticks
 * that they have checked the rig themselves. The server refuses an override
 * from anyone below admin whatever this dialog does, and logs every one
 * under the name of who sent it.
 */
function OverrideDialog({ request, onCancel, onConfirm }) {
  // Without the rule's id there is nothing to override: the server waives
  // the named rules, never all of them. A rule that turns up after another
  // was waived is added to it, so a command two rules stand in front of can
  // be sent at all.
  const [sure, setSure] = useState(false);
  const [why, setWhy] = useState("");
  useEffect(() => { setSure(false); setWhy(""); }, [request]);
  useEffect(() => {
    if (!request) return undefined;
    const onKey = (event) => { if (event.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [request, onCancel]);
  if (!request) return null;
  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true">
        <h2>Override: ${request.label}</h2>
        <div class="small">
          <p><strong>The interlock refuses this:</strong> ${request.reason}</p>
          <p>Overriding waives ${(request.waived || []).length ? "these rules" : "this rule"} (${
            [...(request.waived || []), request.rule].map((rule, index) => html`${
              index ? ", " : ""}<span class="mono" key=${rule}>${rule}</span>`)}) and sends
            it; any other rule still applies. Do it only when you have
            looked at the rig yourself and know why the rule does not apply
            right now. It is recorded under your name, with your reason.</p>
        </div>
        <label class="field">
          <span>Why it is safe now</span>
          <input value=${why} maxlength="300" placeholder="What you checked, in a sentence"
                 onInput=${(e) => setWhy(e.target.value)} />
        </label>
        <label class="check">
          <input type="checkbox" checked=${sure} onChange=${(e) => setSure(e.target.checked)} />
          <span>I have checked the chamber and the pump myself.</span>
        </label>
        <div class="actions">
          <button onClick=${onCancel}>Cancel</button>
          <button class="danger" disabled=${!sure || !request.rule || why.trim().length < 3}
                  onClick=${() => onConfirm(why.trim())}>
            Override and send</button>
        </div>
      </div>
    </div>`;
}

/** Who holds control of the rig, and the one button that changes it. */
function ControlChip({ control, me, onTake, onRelease, onTakeOver }) {
  if (!control?.enabled || !me || me.kind === "device") return null;
  if (control.held && control.mine) {
    return html`
      <span class="chip-group">
        <${Pill} tone="good" title="You may move the hardware">You have control<//>
        <button class="small" onClick=${onRelease}>Release</button>
      </span>`;
  }
  if (control.held && control.same_user) {
    // This account, on another screen: moving it is the account's own call.
    return html`
      <span class="chip-group">
        <${Pill} tone="warn" title=${`Since ${fmt.clock(control.since)}`}>
          You have control on another screen<//>
        ${me.can?.operate && html`
          <button class="small" onClick=${onTake}>Take control here</button>`}
      </span>`;
  }
  if (control.held) {
    return html`
      <span class="chip-group">
        <${Pill} tone="warn" title=${`Since ${fmt.clock(control.since)}`}>
          ${control.holder} has control<//>
        ${me.can?.admin && html`
          <button class="small" onClick=${onTakeOver}>Take over</button>`}
      </span>`;
  }
  if (me.can?.operate) {
    return html`<button class="small" onClick=${onTake}
                        title="Nobody is driving the rig. Acting takes control too.">
      Take control</button>`;
  }
  return null;
}

/** Who is looking: a person, a paired tablet, or nobody (accounts off). */
function UserChip({ me, onSignIn, onSignOut, onAccount }) {
  if (!me) return null;
  if (me.kind === "local") {
    return html`<${Pill} tone="crit"
      title="The server was started with --no-auth: anyone who reaches it can drive the rig">
      accounts off<//>`;
  }
  if (me.kind === "device") {
    return html`
      <span class="chip-group">
        <span class="small muted">${me.name} · watching</span>
        <button class="small primary" onClick=${onSignIn}>Sign in</button>
      </span>`;
  }
  return html`
    <span class="chip-group">
      <button class="ghost small user-name" onClick=${onAccount} title="My account">
        <strong>${me.name}</strong>
        <span class="muted">· ${me.role}${me.idle ? " · idle" : ""}</span>
      </button>
      <button class="small" onClick=${onSignOut}>Sign out</button>
    </span>`;
}

/** Why the controls on this page are greyed out, when they are. */
function WatchingNotice({ me, control, onReauth, onTakeOver }) {
  if (!me || me.kind === "local" || me.must_change) return null;
  if (me.kind === "device") {
    return html`<${Banner}>This tablet only watches. A person signs in on it to
      act, and is signed out again after a quarter of an hour without a touch.<//>`;
  }
  if (me.role === "viewer") {
    return html`<${Banner}>You are a viewer here: everything is visible, nothing
      can be changed. To operate the rig, ask for it under${" "}
      <a href="#settings">Settings → My account</a>.<//>`;
  }
  if (me.idle) {
    return html`
      <${Banner} tone="warn">
        <strong>Watching only.</strong> Nothing was touched for a while, so the
        controls wait for your password.
        <button class="small" style="margin-left:10px" onClick=${onReauth}>
          Enter password</button>
      <//>`;
  }
  if (control?.enabled && control.held && !control.mine) {
    return html`
      <${Banner}>
        <strong>${control.holder} has control of the rig</strong> since${" "}
        ${fmt.clock(control.since)}. Make safe, stopping a run without powering
        down, and acknowledging alarms stay open to you.
        ${me.can?.admin && html`
          <button class="small" style="margin-left:10px" onClick=${onTakeOver}>
            Take over</button>`}
      <//>`;
  }
  return null;
}

function App() {
  const [where, setWhere] = useState(parseHash);
  const tab = where.tab;
  const [state, setState] = useState(null);
  const [live, setLive] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [poweringDown, setPoweringDown] = useState(false);
  const [securing, setSecuring] = useState(false);
  const [takingOver, setTakingOver] = useState(false);
  // A command an interlock refused, which an admin may choose to override.
  const [overriding, setOverriding] = useState(null);
  const [toasts, notify, dismiss] = useToasts();
  const [tick, setTick] = useState(0);

  // Accounts: the sessions this browser holds, whether the sign-in page is
  // up, and whether the server wants the password again.
  const [held, setHeld] = useState(sessionTokens);
  const [needLogin, setNeedLogin] = useState(false);
  const [showLogin, setShowLogin] = useState(false);
  const [reauth, setReauth] = useState(false);
  const [ping, setPing] = useState(null);

  // Refs, not state: the poll loop and the touch listener read them without
  // being rebuilt every time they change.
  const runningRef = useRef(false);
  const liveRef = useRef(NO_LIVE);
  const stateRef = useRef(null);
  const touchRef = useRef({ last: Date.now(), sent: 0 });
  // The server's fingerprint of the panel's files, as first seen.
  const buildRef = useRef(null);
  stateRef.current = state;

  useEffect(() => { applyTheme(settings().theme); }, [tick]);

  useEffect(() => {
    const onHash = () => setWhere(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((key) => {
    location.hash = key;
    setWhere(parseHash());
  }, []);

  // --- accounts -------------------------------------------------------------

  useEffect(() => { api.ping().then(setPing).catch(() => {}); }, [held]);

  useEffect(() => onSessionChange((next, reason) => {
    // A person's session ended under them -- whichever request found out
    // first -- on a browser that keeps watching as a paired tablet.
    if (reason === "ended" && next.device) {
      const last = stateRef.current?.me;
      notify(last?.kind === "person" && last.idle_in_s != null && last.idle_in_s <= 10
        ? idleMessage(last) : "Signed out. The tablet keeps watching.", "ok");
    }
    setHeld({ ...next });
    setNeedLogin(false);
    setShowLogin(false);
    setTick((t) => t + 1);              // poll now, as whoever this is
    // The on-screen keyboard leaves the page scrolled to wherever the
    // sign-in form was; whoever arrives starts at the top.
    window.scrollTo(0, 0);
  }), []);

  useEffect(() => onAuthProblem((problem) => {
    if (problem.reauth) setReauth(true);
    if (problem.mustChange) setTick((t) => t + 1);   // `me` says so; the page asks
  }), []);

  const signOut = useCallback(async (message) => {
    try { await api.logout(); } catch { /* the session may already be gone */ }
    setPersonSession(null);
    if (message) notify(message, "ok");
  }, []);

  // A deliberate touch keeps a person's session able to act. Reported at
  // most once a minute, and only while it still can: once idle, only the
  // password wakes it, so a stranger's tap on an abandoned tablet does not.
  useEffect(() => {
    const touched = () => {
      const now = Date.now();
      touchRef.current.last = now;
      const me = stateRef.current?.me;
      if (me?.kind === "person" && !me.idle && now - touchRef.current.sent > TOUCH_EVERY_MS) {
        touchRef.current.sent = now;
        api.touch().catch(() => {});
      }
    };
    // A click, not a pointerdown: the kiosk taps itself after every load
    // to unlock sound -- a press it cancels, which never makes a click --
    // and that must not keep an abandoned session awake.
    window.addEventListener("click", touched, { passive: true, capture: true });
    window.addEventListener("keydown", touched);
    return () => {
      window.removeEventListener("click", touched, { capture: true });
      window.removeEventListener("keydown", touched);
    };
  }, []);

  // On a paired tablet a person who walks away is signed out altogether, and
  // the tablet goes back to watching. On a personal phone the session stays,
  // watching, and asks for the password before acting (the server's rule).
  // The server ends such a session itself once it is idle; this covers one
  // signed in before it did, and goes by the server's idea of idle, not by
  // a clock in this page that every reload would restart.
  const signOutIfAbandoned = (me) => {
    const tokens = sessionTokens();
    if (!tokens.device || !tokens.person || me?.kind !== "person") return;
    if (me.idle) signOut(idleMessage(me));
  };

  // --- polling --------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let timer = null;
    let inFlight = false;
    const schedule = (ms) => {
      if (!cancelled) timer = setTimeout(poll, ms);
    };

    const poll = async () => {
      // A hidden page has nothing to draw, so it fetches nothing either -- a
      // phone in a pocket must not drain its battery polling. Coming back
      // into view polls at once.
      if (cancelled || inFlight || document.hidden) return;
      inFlight = true;
      try {
        const wantLive = runningRef.current;
        const [next, applyOthers, liveResult] = await Promise.all([
          api.state(),
          pollParticipants(),
          wantLive ? fetchLive(liveRef.current).catch(() => null) : null,
        ]);
        if (cancelled) return;
        // New files on the server: this page is out of date. Reload now,
        // unless somebody is in the middle of something -- a dialog open or
        // a field being typed in -- in which case at the next poll.
        if (next.build) {
          if (!buildRef.current) {
            buildRef.current = next.build;
          } else if (next.build !== buildRef.current) {
            const typing = ["INPUT", "TEXTAREA", "SELECT"]
              .includes(document.activeElement?.tagName);
            if (!typing && !document.querySelector(".overlay")) {
              location.reload();
              return;
            }
          }
        }
        // Everything in one synchronous block: Preact redraws once.
        setState(next);
        setError(null);
        applyOthers();
        runningRef.current = !!next.run?.running;
        if (!runningRef.current) {
          if (liveRef.current !== NO_LIVE) {
            liveRef.current = NO_LIVE;
            setLive(null);
          }
        } else if (liveResult) {
          const merged = mergeLive(liveRef.current, liveResult);
          if (merged !== liveRef.current) {
            liveRef.current = merged;
            setLive({ samples: merged.samples });
          }
        }
        signOutIfAbandoned(next.me);
        schedule(Math.max(500, settings().pollMs));
      } catch (problem) {
        if (cancelled) return;
        if (problem instanceof AuthError && problem.login) {
          // The person's session is gone. A tablet falls back to watching
          // at once; with nothing to fall back to, the sign-in page.
          const tokens = sessionTokens();
          if (tokens.person || tokens.device) {
            schedule(0);
          } else {
            setNeedLogin(true);
            setState(null);
          }
          return;
        }
        setError(problem.message);
        schedule(Math.max(500, settings().pollMs));
      } finally {
        inFlight = false;
      }
    };

    const onVisible = () => {
      if (!document.hidden && !cancelled) {
        clearTimeout(timer);
        poll();
      }
    };
    document.addEventListener("visibilitychange", onVisible);
    poll();
    return () => {
      cancelled = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [tick]);

  // --- commands -----------------------------------------------------------

  /** A refused request: the password dialog has already opened if that is why. */
  const fail = (problem) => {
    if (problem instanceof AuthError) {
      if (problem.reauth) notify("Enter your password, then try again.", "error");
      else if (problem.login) notify("Sign in to do that.", "error");
      return;
    }
    notify(problem.message, "error");
  };

  /** Put a hardware action in front of the operator before sending it. */
  const ask = useCallback((device, action, label, reason, value, options = {}) => {
    if (options.silent) {
      return send(device, action, value, false);
    }
    return new Promise((resolve) => {
      setPending({ device, action, label, reason, value, resolve });
    });
  }, []);

  const send = async (device, action, value, override, label) => {
    setBusy(true);
    try {
      const result = await api.command(device, action, value, { override });
      if (result.ok) {
        notify(`${fmt.titleCase(action)} sent${override ? " — interlock overridden" : ""}.`, "ok");
      } else if (result.interlock) {
        notify(`Refused: ${result.error}`, "error");
        // An admin is offered the deliberate way past that one rule; nobody
        // else is. After an override, the refusal is a different rule.
        if (result.rule && stateRef.current?.me?.can?.override) {
          setOverriding({ device, action, value, reason: result.error,
                          rule: result.rule, waived: override?.rules || [],
                          label: label || fmt.titleCase(action) });
        }
      } else {
        notify(result.error || "The command failed.", "error");
      }
      return result;
    } catch (problem) {
      fail(problem);
      return { ok: false, error: problem.message };
    } finally {
      setBusy(false);
    }
  };

  const confirmPending = async () => {
    const request = pending;
    setPending(null);
    const result = await send(request.device, request.action, request.value,
                              false, request.label);
    request.resolve?.(result);
  };

  const confirmOverride = async (why) => {
    const request = overriding;
    setOverriding(null);
    await send(request.device, request.action, request.value,
               { rules: [...(request.waived || []), request.rule], reason: why,
                 refusal: request.reason, checked: true },
               request.label);
  };

  const cancelPending = () => {
    pending?.resolve?.({ ok: false, cancelled: true });
    setPending(null);
  };

  const pumpCommand = useCallback(
    (action, label, reason, value) => ask("pump", action, label, reason, value),
    [ask]);

  const writeParam = useCallback(
    (name, value) => ask("pump", "param", `Set ${name} to ${value}`, "",
                         { name, value }),
    [ask]);

  const gaugeCommand = useCallback(
    (action, label, reason, value, options) =>
      ask("gauge", action, label, reason, value, options),
    [ask]);

  /** One request with the panel busy, a toast for the outcome. */
  const perform = async (call, success) => {
    setBusy(true);
    try {
      const result = await call();
      if (result.ok) {
        if (success) notify(typeof success === "function" ? success(result) : success, "ok");
      } else {
        notify(result.error, "error");
      }
      return result;
    } catch (problem) {
      fail(problem);
      return null;
    } finally {
      setBusy(false);
    }
  };

  const startRun = (body) => perform(() => api.startRun(body),
                                     (result) => `Started run ${result.run.id}.`);

  const acknowledgeStep = () => perform(() => api.acknowledgeStep(),
                                        "Step confirmed — the run continues.");

  const takeControl = async (force = false) => {
    const result = await perform(() => api.takeControl(force),
                                 force ? "You took control of the rig." : "You have control of the rig.");
    if (result?.ok) setTick((t) => t + 1);
  };

  const releaseControl = async () => {
    const result = await perform(() => api.releaseControl(), "Control released.");
    if (result?.ok) setTick((t) => t + 1);
  };

  // Keeping the vacuum is the first answer for every procedure: it leaves
  // the forevacuum pump running, and a power-down switches the station off,
  // after which nothing pumps the chamber and it leaks back up.
  // Powering down stays one click away for the end of the day.
  const stopOptions = () => {
    const keep = {
      key: "keep", label: "Stop the run, keep the vacuum",
      description: "The turbopump and the backing pump carry on exactly as "
        + "they are. The chamber stays under vacuum, ready for the next run.",
    };
    const down = {
      key: "shutdown", tone: "danger",
      label: "Stop the run and power the pump down",
      description: "Switches the turbopump motor off, waits for the rotor to "
        + "coast to a stop with the backing pump still running, then stops "
        + "the station. Takes several minutes. Afterwards nothing pumps the "
        + "chamber, so it slowly leaks back up towards air pressure.",
    };
    return [keep, down];
  };

  // Stopping a run is two decisions, not one: end the recording, and decide
  // what the hardware should be left doing. Ending a deposition usually
  // means leaving the chamber under vacuum for the next run; finishing for
  // the day means the rotor should be at rest and the backing pump off. The
  // dialog asks, rather than picking one silently.
  const stopRun = () => setStopping(true);

  const finishStop = async (choice) => {
    setStopping(false);
    const shutdown = choice === "shutdown";
    await perform(
      () => api.stopRun(
        shutdown ? "stopped by the operator, powering the pump down"
                 : "stopped by the operator, chamber left under vacuum",
        shutdown),
      shutdown ? "Stopping the run, then powering down…"
               : "Stopping the run. The pump keeps running.");
  };

  // --- render -------------------------------------------------------------

  const theme = settings().theme;
  const cycleTheme = () => {
    const next = theme === "auto" ? "light" : theme === "light" ? "dark" : "auto";
    updateSettings({ theme: next });
    applyTheme(next);
    setTick((t) => t + 1);
  };

  const acknowledgeAlarms = async (id) => {
    try {
      await api.acknowledgeAlarms(id);
      setTick((t) => t + 1);          // poll again now rather than in 2 s
    } catch (problem) {
      fail(problem);
    }
  };

  // Make safe: motor off, fault acknowledged, forevacuum pump on. The same
  // sequence the supervisor runs by itself after a fault, for when that is
  // switched off or a person wants it now.
  const makeSafe = async () => {
    setSecuring(false);
    await perform(() => api.securePump("Make safe, from the panel"),
                  "Making the pump safe: motor off, fault cleared, forevacuum pump on…");
  };

  const cancelPowerDown = () => perform(
    () => api.cancelShutdown(), "Power-down cancelled. The backing pump keeps running.");

  useAlarmSignals(state?.alarms);

  // The guided power-down, reachable without a run to stop: switch the
  // motor off, wait for the rotor to reach rest with the backing pump still
  // running, then stop the station. The server owns the sequence.
  const powerDown = async () => {
    setPoweringDown(false);
    await perform(() => api.shutdownPump("requested from the pump page"),
                  "Powering the pump down…");
  };

  // The sign-in page: nobody is signed in, or a person is signing in on a
  // paired tablet (who can go back to watching).
  if (needLogin || showLogin) {
    return html`
      <div class="wrap">
        <${LoginView} ping=${ping}
                      onSignedIn=${() => { setNeedLogin(false); setShowLogin(false); }}
                      onCancel=${showLogin && !needLogin ? () => setShowLogin(false) : null} />
        <${Toasts} items=${toasts} onDismiss=${dismiss} />
      </div>`;
  }

  const me = state?.me;
  const control = state?.control;
  // What the operator may press. Anything that moves hardware needs an
  // operator who is not idle and either holds control or finds it free (the
  // first action takes it). What makes the rig safer needs only the first.
  const canOperate = !!me?.can?.operate;
  const controlFree = !control?.enabled || !control.held || control.mine;
  const locked = busy || !canOperate || !controlFree;
  const safeLocked = busy || !canOperate;

  const run = state?.run;
  // Reported whether or not a run is live: a power-down outlives the run
  // that asked for it, and the rotor takes minutes to stop. A *finished*
  // one is only worth showing while its outcome is still true -- once the
  // station is running again, "rotor at rest, station off" is a lie sitting
  // at the top of the page.
  const shutdown = run?.shutdown;
  const stationOn = state?.devices?.pump?.setpoints?.PumpgStatn === true;
  const showShutdown = shutdown && (shutdown.active || !stationOn);
  const instruments = state
    ? (state.order || Object.keys(state.devices || {}))
      .map((key) => state.devices?.[key]?.label).filter(Boolean).join(" · ")
    : "";
  const chamberName = state?.chamber?.name || ping?.chamber?.name;
  const notice = (tab === "equipment" || tab === "tests") && html`
    <${WatchingNotice} me=${me} control=${control} onReauth=${() => setReauth(true)}
                       onTakeOver=${() => setTakingOver(true)} />`;

  const body = () => {
    if (!state && error) {
      return html`
        <${Banner} tone="crit">
          <strong>Not connected.</strong> ${error}
          <div class="small" style="margin-top:8px">
            Set the controller address under${" "}
            <a href="#settings" onClick=${() => navigate("settings")}>Settings</a>
            ${" "}if the server is on another machine.
          </div>
        <//>`;
    }
    if (!state) return html`<div class="boot">Connecting…</div>`;

    if (me?.must_change) {
      return html`
        <section class="card narrow">
          <h2>Choose your own password</h2>
          <${ChangePassword} forced=${true} notify=${notify}
                             onDone=${() => setTick((t) => t + 1)} />
        </section>`;
    }

    switch (tab) {
      case "dashboard":
        return html`<${DashboardView} state=${state} live=${live} />`;
      case "equipment":
        return html`<${EquipmentView} state=${state} sub=${where.sub}
          onSelect=${(key) => navigate(`equipment/${key}`)}
          pumpProps=${{ state, onCommand: pumpCommand, onWriteParam: writeParam,
                        busy: locked, safeBusy: safeLocked, notify, shutdown,
                        onShutdown: () => setPoweringDown(true),
                        onSecure: () => setSecuring(true),
                        onOverride: me?.can?.override
                          ? (action, label, reason, rule) => setOverriding(
                            { device: "pump", action, label, reason, rule })
                          : null }}
          gaugeProps=${{ state, onCommand: gaugeCommand, busy: locked }} />`;
      case "tests":
        return html`<${TestsView} state=${state} live=${live} onStart=${startRun}
                                  onStop=${stopRun} busy=${locked} stopBusy=${safeLocked}
                                  onAcknowledge=${acknowledgeStep} />`;
      case "history":
        return html`<${HistoryView} notify=${notify} state=${state} />`;
      case "settings":
        return html`<${SettingsView} state=${state} notify=${notify}
                                     onSignOut=${() => signOut("Signed out.")}
                                     onSettingsChanged=${() => setTick((t) => t + 1)} />`;
      default:
        return html`<${Overview} state=${state} live=${live}
                                 onStop=${canOperate ? stopRun : null}
                                 onNavigate=${navigate} />`;
    }
  };

  return html`
    <div class="wrap">
      <header class="top">
        <div class="brand">
          <h1>Vacuum Lab Control</h1>
          <span class="sub">
            ${chamberName ? `${chamberName} · ` : ""}${instruments}
            ${state ? ` · up ${fmt.duration(state.uptime_s)}` : ""}
          </span>
        </div>
        <div class="top-actions">
          <${ControlChip} control=${control} me=${me}
                          onTake=${() => takeControl(false)} onRelease=${releaseControl}
                          onTakeOver=${() => setTakingOver(true)} />
          <${UserChip} me=${me} onSignIn=${() => setShowLogin(true)}
                       onSignOut=${() => signOut("Signed out.")}
                       onAccount=${() => navigate("settings")} />
          <button class="ghost small" onClick=${cycleTheme}
                  title=${`Theme: ${theme}`}>
            ${theme === "auto" ? "Auto" : theme === "light" ? "Light" : "Dark"}
          </button>
        </div>
      </header>

      <nav class="tabs">
        ${TABS.map((entry) => html`
          <button key=${entry.key} aria-current=${tab === entry.key ? "page" : null}
                  onClick=${() => navigate(entry.key)}>
            ${entry.label}
            ${entry.key === "tests" && run?.running
              ? html`<span class="badge pill accent"><span class="dot" />live</span>`
              : null}
            ${entry.key === "settings" && me?.waiting > 0
              ? html`<span class="badge pill warn" title="Waiting for an admin">${me.waiting}</span>`
              : null}
          </button>`)}
      </nav>

      ${error && state && html`
        <${Banner} tone="warn">Lost contact with the server: ${error}<//>`}

      <${AlarmBar} alarms=${state?.alarms} onAcknowledge=${acknowledgeAlarms}
                   busy=${safeLocked} />

      ${showShutdown && html`
        <${Banner} tone=${shutdown.active ? "warn"
                          : shutdown.ok ? "good" : "crit"}>
          <strong>${shutdown.active ? "Powering down:"
                    : shutdown.ok ? "Power-down complete:"
                    : "Power-down stopped:"}</strong> ${shutdown.message}
          ${shutdown.active && shutdown.rotor_hz != null
            ? ` Rotor at ${Math.round(shutdown.rotor_hz)} Hz.` : ""}
          ${shutdown.active && canOperate && html`
            <button class="small" style="margin-left:10px" onClick=${cancelPowerDown}>
              Cancel — keep the backing pump running</button>`}
        <//>`}

      ${notice}

      ${body()}

      <${Choose} open=${stopping}
        title="Stop the run"
        body=${html`<p>The recording ends either way and everything measured
          so far is kept. What should the pump do?</p>
          ${!controlFree && html`<p class="small muted">${control.holder} has
            control, so only keeping the vacuum is open to you.</p>`}`}
        options=${controlFree ? stopOptions() : stopOptions().slice(0, 1)}
        onChoose=${finishStop} onCancel=${() => setStopping(false)} />

      <${Confirm} open=${securing}
        title="Make the pump safe"
        confirmLabel="Make safe"
        body=${html`<p>Switches the turbopump motor off, acknowledges the drive's
          fault, and makes sure the pumping station — which runs the forevacuum
          pump — is on. Nothing that keeps the chamber under vacuum is switched
          off. Restart the turbopump afterwards from the Station card, once the
          cause is dealt with.</p>`}
        onCancel=${() => setSecuring(false)} onConfirm=${makeSafe} />

      <${Confirm} open=${poweringDown}
        title="Power the pump down"
        tone="danger" confirmLabel="Power down"
        body=${html`<p>The turbopump motor goes off, the rotor coasts to a
          stop with the backing pump still running, and the pumping station
          stops only once it is at rest. This takes several minutes and the
          chamber is left under whatever vacuum it holds.</p>`}
        onCancel=${() => setPoweringDown(false)} onConfirm=${powerDown} />

      <${Confirm} open=${takingOver}
        title=${`Take control from ${control?.holder || "the operator"}?`}
        tone="danger" confirmLabel="Take over"
        body=${html`<p>${control?.holder || "They"} can no longer move the
          hardware until they take control back. Make safe and stopping a run
          stay open to them. The takeover is logged under your name.</p>`}
        onCancel=${() => setTakingOver(false)}
        onConfirm=${() => { setTakingOver(false); takeControl(true); }} />

      <${Confirm} open=${!!pending}
        title=${pending?.label || "Confirm"}
        tone=${/off|stop|delete/i.test(pending?.action || "") ? "danger" : "primary"}
        confirmLabel="Do it"
        body=${html`
          <p>${pending?.reason ||
            "This changes what the hardware is doing right now."}</p>`}
        onCancel=${cancelPending} onConfirm=${confirmPending} />

      <${OverrideDialog} request=${overriding}
        onCancel=${() => setOverriding(null)} onConfirm=${confirmOverride} />

      <${ReauthDialog} open=${reauth} me=${me}
        onDone=${() => { setReauth(false); notify("Welcome back. The controls are yours again.", "ok"); setTick((t) => t + 1); }}
        onCancel=${() => setReauth(false)} />

      <${Toasts} items=${toasts} onDismiss=${dismiss} />
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));
