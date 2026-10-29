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
//
// What a person must do -- a step a guided run waits for, a step a run's
// ending left by hand, an alarm -- is on every page, not only the one where
// it happens to be answered. And the things that make the rig safer stay
// pressable on an idle session: they ask for the password, then act.

import { html } from "./html.js";
import { render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { AuthError, api, onAuthProblem, onSessionChange, pollParticipants,
         sessionTokens, setGuestSession, setPersonSession, settings,
         updateSettings } from "./api.js";
import { Banner, Choose, Confirm, Pill, Toasts, useDialog, useToasts } from "./ui.js";
import { AlarmBar, useAlarmSignals } from "./alarms.js";
import { ChecklistBar, PromptBar } from "./guided.js";
import { DashboardView } from "./views/dashboard.js";
import { EquipmentView } from "./views/equipment.js";
import { TestsView } from "./views/tests.js";
import { HistoryView } from "./views/history.js";
import { SettingsView } from "./views/settings.js";
import { ChangePassword, LoginView, ReauthDialog } from "./views/login.js";
import * as fmt from "./format.js";
import { setJudgement } from "./analysis.js";

const TABS = [
  { key: "dashboard", label: "Dashboard" },
  { key: "equipment", label: "Equipment" },
  { key: "tests", label: "Process" },
  { key: "history", label: "History" },
  { key: "settings", label: "Settings" },
];

// The pump and the gauge had tabs of their own, and the Overview was folded
// into the Dashboard. Old links and bookmarks land where those went.
const MOVED = { pump: "equipment/pump", gauge: "equipment/gauge", overview: "dashboard" };

// A deliberate touch is reported at most this often: often enough that a
// person using the panel never meets the idle prompt (lab/auth.py IDLE_S).
const TOUCH_EVERY_MS = 60000;

// Polls that fail in a row before the panel says it has lost the lab PC.
// One is a hiccup -- a restart, a busy Wi-Fi -- and says nothing.
const LOST_AFTER = 2;

/** What a power-down does, said the same way wherever it is offered. */
const POWER_DOWN = "Switches the turbopump motor off, waits for the rotor to "
  + "coast to a stop with the backing pump still running, then stops the "
  + "pumping station. Takes several minutes. Afterwards nothing pumps the "
  + "chamber, so it slowly leaks back up towards air pressure.";

/** What each hardware command did, once the drive took it. */
const DONE = {
  on: "Pumping station switched on.",
  off: "Pumping station switched off.",
  motor_on: "Turbopump motor switched on — it is spinning up.",
  motor_off: "Turbopump motor switched off — the rotor coasts down, backed.",
  standby_on: "Standby on.",
  standby_off: "Standby off: back to full speed.",
  ack: "Fault acknowledged, with the motor off.",
  speed: "Speed setpoint changed.",
  param: "Setting changed.",
  vent: "Vent command sent.",
  sensor_on: "Gauge switched on.",
  sensor_off: "Gauge switched off.",
  setpoint: "Switching point changed.",
  unit: "Display unit changed.",
};

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
 * How this person may answer something that makes the rig safer -- an
 * alarm, a step left by hand, Make safe, Stop. "yes"; "reauth", an idle
 * session, which types the password and then it is done; "signin", a paired
 * tablet; "no", a viewer.
 */
function safetyMay(me) {
  if (!me) return "no";
  if (me.kind === "local") return "yes";
  if (me.kind === "device") return "signin";
  if (me.must_change || (me.rank ?? 0) < 1) return "no";
  return me.idle ? "reauth" : "yes";
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
  // Fresh for every request: App mounts one per request (its `key`), so a
  // reason typed for one is never carried to the next -- and nothing resets
  // the form after its first frame, under somebody already typing.
  const [sure, setSure] = useState(false);
  const [why, setWhy] = useState("");
  const box = useDialog(!!request, onCancel);
  if (!request) return null;
  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="override-title"
           data-test="override-dialog" tabIndex="-1" ref=${box}>
        <h2 id="override-title">Override: ${request.label}</h2>
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
                 data-test="override-reason" onInput=${(e) => setWhy(e.target.value)} />
        </label>
        <label class="check">
          <input type="checkbox" checked=${sure} data-test="override-checked"
                 onChange=${(e) => setSure(e.target.checked)} />
          <span>I have checked the chamber and the pump myself.</span>
        </label>
        <div class="actions">
          <button data-test="override-cancel" onClick=${onCancel}>Cancel</button>
          <button class="danger" disabled=${!sure || !request.rule || why.trim().length < 3}
                  data-test="override-confirm" onClick=${() => onConfirm(why.trim())}>
            Override and send</button>
        </div>
      </div>
    </div>`;
}

/** Who holds control of the rig, and the one button that changes it. */
function ControlChip({ control, me, onTake, onRelease, onTakeOver }) {
  if (!control?.enabled || !me || me.kind === "device" || me.kind === "guest") return null;
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

/** Who is looking: a person, a paired tablet, a guest, or nobody (accounts
 *  off). */
function UserChip({ me, onSignIn, onSignOut, onAccount, onLeaveGuest }) {
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
  if (me.kind === "guest") {
    return html`
      <span class="chip-group">
        <span class="small muted">Guest · watching</span>
        <button class="small primary" data-test="guest-signin" onClick=${onLeaveGuest}>
          Sign in</button>
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
function WatchingNotice({ me, control, tab, onReauth, onTakeOver }) {
  if (!me || me.kind === "local" || me.must_change) return null;
  if (me.kind === "device") {
    // The wall display's own page says it in its header; elsewhere, where
    // the controls are, it says why they do nothing.
    if (tab === "dashboard") return null;
    return html`<${Banner}>This tablet only watches. A person signs in on it to
      act, and is signed out again after a quarter of an hour without a touch.<//>`;
  }
  if (me.kind === "guest") {
    // On every page, the Dashboard too: a screen that never sounds an alarm
    // must say so wherever somebody may be relying on it.
    return html`<div data-test="guest-notice"><${Banner}>You are watching as a
      guest: the rig live, its runs and its log. Nothing can be changed or
      downloaded, and <strong>alarms are silent on this screen</strong>. To do
      more, sign in with an account.<//></div>`;
  }
  if (me.role === "viewer") {
    return html`<${Banner}>You are a viewer here: everything is visible, nothing
      can be changed. To operate the rig, ask for it under${" "}
      <a href="#settings/account">Settings → My account</a>.<//>`;
  }
  if (me.idle) {
    return html`
      <${Banner} tone="warn">
        <strong>Watching only.</strong> Nothing was touched for a while, so the
        controls wait for your password. Make safe, Stop, and answering alarms
        ask for it and then go ahead.
        <button data-test="reauth-open" class="small" style="margin-left:10px" onClick=${onReauth}>
          Enter password</button>
      <//>`;
  }
  if (control?.enabled && control.held && !control.mine) {
    return html`
      <${Banner}>
        <strong>${control.holder} has control of the rig</strong> since${" "}
        ${fmt.clock(control.since)}. Make safe, stopping a run without powering
        down, and answering alarms stay open to you.
        ${me.can?.admin && html`
          <button class="small" style="margin-left:10px" onClick=${onTakeOver}>
            Take over</button>`}
      <//>`;
  }
  return null;
}

/**
 * Two polls running without a good answer. Everything below is the last
 * thing the lab PC said, greyed out; this says since when, and what to look
 * at -- the line (`offline`: no answer at all), or the panel there answering
 * with an error.
 */
function LostContact({ lost, error, kiosk, silence }) {
  const ago = fmt.duration(Date.now() / 1000 - lost.since);
  return html`
    <${Banner} tone="crit">
      <div class="spread">
        <strong data-test="lost-contact">${lost.offline
          ? "No contact with the lab PC" : "The panel on the lab PC is not answering properly"}
          ${" "}since ${fmt.clock(lost.since)} (${ago}).</strong>
        ${silence}
      </div>
      <div class="small" style="margin-top:6px">
        The figures below are from then, and greyed out. The rig keeps doing
        what it was last told — and if only this screen has lost its network,
        the lab PC still watches it. Check:
        <ul class="plain-list">
          ${lost.offline && kiosk && html`<li>the tablet's USB cable to the lab PC, or its Wi-Fi;</li>`}
          ${lost.offline && !kiosk && html`<li>that this device is on the lab's network;</li>`}
          ${lost.offline && html`<li>that the lab PC is on and its screen answers;</li>`}
          <li>that the panel's service runs there:${" "}
            <span class="mono">systemctl status vacuum-lab-web</span>, and its
            journal:${" "}<span class="mono">journalctl -u vacuum-lab-web -p err</span>.</li>
        </ul>
        ${!lost.offline && html`<span class="muted">${error}</span>`}
      </div>
    <//>`;
}

function App() {
  const [where, setWhere] = useState(parseHash);
  const tab = where.tab;
  const [state, setState] = useState(null);
  const [live, setLive] = useState(null);
  const [error, setError] = useState(null);
  // No good answer for LOST_AFTER polls: {since, failures, offline}. A new
  // object each failed poll, so the "since 14:02 (3m)" keeps counting.
  const [lost, setLost] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [poweringDown, setPoweringDown] = useState(false);
  const [securing, setSecuring] = useState(false);
  const [takingOver, setTakingOver] = useState(false);
  // A command an interlock refused, which an admin may choose to override.
  // Each gets its own number: the dialog is mounted afresh for each.
  const [overriding, setOverridingState] = useState(null);
  const overrides = useRef(0);
  const setOverriding = (request) => setOverridingState(
    request ? { ...request, seq: (overrides.current += 1) } : null);
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
  // The last poll that came back, and the failures since it.
  const contactRef = useRef({ okAt: Date.now() / 1000, failures: 0 });
  // A safety action waiting for the password: it goes ahead once typed.
  const afterReauth = useRef(null);
  // A guest whose session lapsed joins again by itself -- at most every
  // 10 s, so a refusal ends at the sign-in page instead of a loop. `turn`
  // grows when the person leaves: a rejoin still under way then does nothing.
  const rejoin = useRef({ at: 0, busy: false, turn: 0 });
  // Whether the tab strip has more to the right than fits (phones).
  const tabsRef = useRef(null);
  const [tabsMore, setTabsMore] = useState(false);
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

  // A new page starts at its top, not at whatever height the last one was
  // scrolled to -- a link from low on the Dashboard opened History halfway.
  useEffect(() => { window.scrollTo(0, 0); }, [where.tab, where.sub]);

  // On a phone the tabs scroll sideways: a fade at the edge says there are
  // more, until the strip is scrolled to its end.
  useEffect(() => {
    const strip = tabsRef.current;
    if (!strip) return undefined;
    const measure = () => setTabsMore(
      strip.scrollLeft + strip.clientWidth < strip.scrollWidth - 4);
    measure();
    strip.addEventListener("scroll", measure, { passive: true });
    window.addEventListener("resize", measure);
    return () => {
      strip.removeEventListener("scroll", measure);
      window.removeEventListener("resize", measure);
    };
  }, [!!state]);

  // --- accounts -------------------------------------------------------------

  useEffect(() => { api.ping().then(setPing).catch(() => {}); }, [held]);

  useEffect(() => onSessionChange((next, reason) => {
    // A guest's session is in the server's memory only: a restart, or a page
    // hidden for a couple of minutes, ends it. Join again while guests are
    // still allowed; the sign-in page only if that is refused.
    if (reason === "guest-ended") {
      const now = Date.now();
      if (!rejoin.current.busy && now - rejoin.current.at > 10000) {
        const turn = rejoin.current.turn;
        rejoin.current = { at: now, busy: true, turn };
        const mine = () => rejoin.current.turn === turn;
        api.guest()
          .then((result) => { if (mine()) setGuestSession(result.token); })
          .catch(() => {
            if (!mine()) return;
            setHeld({ ...sessionTokens() });
            setNeedLogin(true);
            setState(null);
          })
          .finally(() => { if (mine()) rejoin.current.busy = false; });
        return;
      }
    }
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
    // Whoever is here now is not the person the password dialog asked.
    setReauth(false);
    afterReauth.current = null;
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

  // A guest who wants to sign in stops watching first: the sign-in page then
  // has every choice, "Watch as a guest" among them, to go back.
  const leaveGuest = useCallback(async () => {
    // A session that lapses just now must not be joined again under the
    // person on their way to signing in -- nor one already being joined.
    rejoin.current = { at: Date.now(), busy: false, turn: rejoin.current.turn + 1 };
    try { await api.logout(); } catch { /* it may already be gone */ }
    setGuestSession(null);
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
        contactRef.current = { okAt: Date.now() / 1000, failures: 0 };
        setJudgement(next.judgement);
        setState(next);
        setError(null);
        setLost(null);
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
          if (tokens.person || tokens.device || tokens.guest) {
            schedule(0);
          } else if (rejoin.current.busy) {
            // A guest joining again: until that answers, nothing is new --
            // counted like any poll without an answer, so a slow one greys
            // the page out instead of leaving old numbers looking live.
            const contact = contactRef.current;
            contact.failures += 1;
            if (contact.failures >= LOST_AFTER) {
              setLost({ since: contact.okAt, failures: contact.failures, offline: false });
            }
            schedule(Math.max(500, settings().pollMs));
          } else {
            setNeedLogin(true);
            setState(null);
          }
          return;
        }
        const contact = contactRef.current;
        contact.failures += 1;
        setError(problem.message);
        if (contact.failures >= LOST_AFTER) {
          setLost({ since: contact.okAt, failures: contact.failures,
                    offline: !!problem.offline });
        }
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

  const me = state?.me;
  const may = safetyMay(me);

  /**
   * Something that makes the rig safer, pressed by whoever is looking: done
   * now, after the password on an idle session, or after signing in on a
   * paired tablet (where the person presses again).
   */
  const safely = (action) => {
    const how = safetyMay(stateRef.current?.me);
    if (how === "reauth") {
      afterReauth.current = action;
      setReauth(true);
    } else if (how === "signin") {
      setShowLogin(true);
    } else if (how === "yes") {
      action();
    }
  };

  /** Put a hardware action in front of the operator before sending it. */
  const ask = useCallback((device, action, label, reason, value, options = {}) => {
    if (options.silent) {
      return send(device, action, value, false, label);
    }
    return new Promise((resolve) => {
      setPending({ device, action, label, reason, value, resolve });
    });
  }, []);

  /**
   * Send one command. A refusal is toasted -- or, with `quiet`, left to the
   * dialog that sent it to show. An admin refused by an interlock is offered
   * the override straight away, unless the dialog offers it itself.
   */
  const send = async (device, action, value, override, label, quiet = false) => {
    setBusy(true);
    try {
      const result = await api.command(device, action, value, { override });
      if (result.ok) {
        notify(`${DONE[action] || `${label || fmt.titleCase(action)} — done.`}${
          override ? " The interlock was overridden." : ""}`, "ok");
      } else if (result.interlock) {
        if (!quiet) notify(`Refused: ${result.error}`, "error");
        // An admin is offered the deliberate way past that one rule; nobody
        // else is. After an override, the refusal is a different rule.
        if (!quiet && result.rule && stateRef.current?.me?.can?.override) {
          setOverriding({ device, action, value, reason: result.error,
                          rule: result.rule, waived: override?.rules || [],
                          label: label || fmt.titleCase(action) });
        }
      } else if (!quiet) {
        notify(result.error || "The command failed.", "error");
      }
      return result;
    } catch (problem) {
      if (!quiet || problem instanceof AuthError) fail(problem);
      return { ok: false, error: problem.message };
    } finally {
      setBusy(false);
    }
  };

  // The dialog stays open while the command goes, and shows a refusal
  // itself: the person is looking at it, not at a toast in the corner.
  const confirmPending = async () => {
    const request = pending;
    setPending({ ...request, sending: true });
    const result = await send(request.device, request.action, request.value,
                              false, request.label, true);
    if (!request.answered) {
      request.answered = true;
      request.resolve?.(result);
    }
    // Only if the dialog still shows this request: never brought back for
    // one that was closed meanwhile.
    setPending((shown) => {
      if (!shown || shown.resolve !== request.resolve) return shown;
      if (result.ok || result.cancelled) return null;
      return { ...request, answered: true, sending: false,
               error: result.error || "the command failed",
               rule: result.interlock ? result.rule : null };
    });
  };

  const cancelPending = () => {
    if (pending && !pending.answered) pending.resolve?.({ ok: false, cancelled: true });
    setPending(null);
  };

  const overrideFromPending = () => {
    const request = pending;
    setPending(null);
    setOverriding({ device: request.device, action: request.action,
                    value: request.value, reason: request.error, rule: request.rule,
                    waived: [], label: request.label || fmt.titleCase(request.action) });
  };

  const confirmOverride = async (why) => {
    const request = overriding;
    setOverriding(null);
    await send(request.device, request.action, request.value,
               { rules: [...(request.waived || []), request.rule], reason: why,
                 refusal: request.reason, checked: true },
               request.label);
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

  /** From the start dialog, which shows a refusal itself. */
  const startRun = async (body) => {
    setBusy(true);
    try {
      const result = await api.startRun(body);
      if (result.ok) notify(`Run ${result.run.id} started.`, "ok");
      return result;
    } catch (problem) {
      if (problem instanceof AuthError) fail(problem);
      return { ok: false, error: problem.message };
    } finally {
      setBusy(false);
    }
  };

  const acknowledgeStep = (id) => perform(() => api.acknowledgeStep(id),
                                          "Step confirmed — the run continues.");

  const confirmStep = (id) => perform(() => api.confirmStep(id),
                                      "Confirmed. It is in the rig's log under your name.");

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
    // A chamber whose file has no turbopump: the run's recording ends and
    // nothing else changes -- there is no pump here to power down.
    if (!state?.devices?.pump) {
      return [{
        key: "keep", label: "Stop the run",
        description: "Ends the recording. This chamber has no turbopump the "
          + "panel drives, so nothing else changes.",
      }];
    }
    const keep = {
      key: "keep", label: "Stop the run, keep the vacuum",
      description: "The turbopump and the backing pump carry on exactly as "
        + "they are. The chamber stays under vacuum, ready for the next run.",
    };
    const down = {
      key: "shutdown", tone: "danger",
      label: "Stop the run and power the pump down",
      description: POWER_DOWN,
    };
    return [keep, down];
  };

  // Stopping a run is two decisions, not one: end the recording, and decide
  // what the hardware should be left doing. Ending a deposition usually
  // means leaving the chamber under vacuum for the next run; finishing for
  // the day means the rotor should be at rest and the backing pump off. The
  // dialog asks, rather than picking one silently.
  const stopRun = () => safely(() => setStopping(true));

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

  const acknowledgeAlarms = (id) => safely(async () => {
    try {
      await api.acknowledgeAlarms(id);
      setTick((t) => t + 1);          // poll again now rather than in 2 s
    } catch (problem) {
      fail(problem);
    }
  });

  // Make safe: motor off, fault acknowledged, forevacuum pump on. The same
  // sequence the supervisor runs by itself after a fault, for when that is
  // switched off or a person wants it now.
  const makeSafe = async () => {
    setSecuring(false);
    await perform(() => api.securePump("Make safe, from the panel"),
                  "Making the pump safe: motor off, fault cleared, forevacuum pump on…");
  };

  const cancelPowerDown = () => safely(() => perform(
    () => api.cancelShutdown(), "Power-down cancelled. The backing pump keeps running."));

  const run = state?.run;
  const kiosk = !!held.device;
  const [sounding, silenceSounds, silenceLost] = useAlarmSignals({
    alarms: state?.alarms, prompt: run?.prompt, checklist: run?.checklist,
    lost: kiosk && lost ? lost.since : null,
    // A guest can answer nothing, nor silence it: its screen stays quiet.
    mute: me?.kind === "guest",
  });

  // The guided power-down, reachable without a run to stop: switch the
  // motor off, wait for the rotor to reach rest with the backing pump still
  // running, then stop the station. The server owns the sequence.
  const powerDown = async () => {
    setPoweringDown(false);
    await perform(() => api.shutdownPump("requested from Equipment → Pump"),
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

  const control = state?.control;
  // What the operator may press. Anything that moves hardware needs an
  // operator who is not idle and either holds control or finds it free (the
  // first action takes it). What makes the rig safer is open to any
  // operator: an idle one is asked for the password first (`safely`).
  const canOperate = !!me?.can?.operate;
  const controlFree = !control?.enabled || !control.held || control.mine;
  const locked = busy || !canOperate || !controlFree;
  const safeLocked = busy || may === "no";
  // Confirming a guided step moves the run on: it needs control of the rig.
  const heldBy = !control?.enabled || !control.held || control.mine ? null
    : control.same_user
      ? "You have control on another screen: confirm the step there, or take control here."
      : `${control.holder} has control of the rig: they confirm this step.`;

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
  const canSilence = !!me && (me.kind === "local"
                              || (me.kind === "person" && (me.rank ?? 0) >= 1));
  const silence = sounding && canSilence && html`
    <button class="small" data-test="silence" onClick=${silenceSounds}
            title="Quietens this screen only, until something new sounds">
      Silence here</button>`;

  const body = () => {
    if (!state && error) {
      // Where this screen points can be changed with nothing to point at.
      if (tab === "settings") {
        return html`<${SettingsView} state=${null} notify=${notify} sub="screen"
                                     onSelect=${(key) => navigate(`settings/${key}`)}
                                     onSignOut=${() => signOut("Signed out.")}
                                     onSettingsChanged=${() => setTick((t) => t + 1)} />`;
      }
      return html`
        <${Banner} tone="crit">
          <strong>No contact with the lab PC.</strong>
          ${!/^no answer/.test(error) && html` ${error}.`}
          <div class="small" style="margin-top:8px">
            Check that this device is on the lab's network and that the lab PC
            is on. If the panel is served from another machine, set its address
            under${" "}
            <a href="#settings/screen" onClick=${() => navigate("settings/screen")}>Settings → This screen</a>.
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
      case "equipment":
        return html`<${EquipmentView} state=${state} sub=${where.sub}
          onSelect=${(key) => navigate(`equipment/${key}`)}
          pumpProps=${{ state, onCommand: pumpCommand, onWriteParam: writeParam,
                        busy: locked, safeBusy: safeLocked, notify, shutdown, may,
                        onShutdown: () => setPoweringDown(true),
                        onSecure: () => safely(() => setSecuring(true)),
                        onOverride: me?.can?.override
                          ? (action, label, reason, rule) => setOverriding(
                            { device: "pump", action, label, reason, rule })
                          : null }}
          gaugeProps=${{ state, onCommand: gaugeCommand, busy: locked }} />`;
      case "tests":
        return html`<${TestsView} state=${state} live=${live} onStart=${startRun}
                                  onStop=${stopRun} busy=${locked} stopBusy=${safeLocked}
                                  may=${may} />`;
      case "history":
        return html`<${HistoryView} notify=${notify} state=${state} />`;
      case "settings":
        return html`<${SettingsView} state=${state} notify=${notify} sub=${where.sub}
                                     onSelect=${(key) => navigate(`settings/${key}`)}
                                     onSignOut=${() => signOut("Signed out.")}
                                     onSettingsChanged=${() => setTick((t) => t + 1)} />`;
      default:
        return html`<${DashboardView} state=${state} live=${live} kiosk=${kiosk}
                                      onStop=${may !== "no" ? stopRun : null}
                                      stopBusy=${safeLocked} may=${may} />`;
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
                       onLeaveGuest=${leaveGuest}
                       onAccount=${() => navigate("settings/account")} />
          <button class="ghost small" onClick=${cycleTheme}
                  title=${`Theme: ${theme}`}>
            ${theme === "auto" ? "Auto" : theme === "light" ? "Light" : "Dark"}
          </button>
        </div>
      </header>

      <nav class=${`tabs${tabsMore ? " more" : ""}`} ref=${tabsRef} aria-label="Pages">
        ${TABS.map((entry) => html`
          <button key=${entry.key} aria-current=${tab === entry.key ? "page" : null}
                  data-test=${`tab-${entry.key}`} onClick=${() => navigate(entry.key)}>
            ${entry.label}
            ${entry.key === "tests" && run?.running
              ? html`<span class="badge pill accent"><span class="dot" />live</span>`
              : null}
            ${entry.key === "settings" && me?.waiting > 0
              ? html`<span class="badge pill warn" title="Waiting for an admin">${me.waiting}<span class="sr-only"> waiting</span></span>`
              : null}
          </button>`)}
      </nav>

      ${lost && state && html`
        <${LostContact} lost=${lost} error=${error} kiosk=${kiosk}
                        silence=${silenceLost && html`
                          <button class="small" data-test="silence-lost" onClick=${silenceLost}
                                  title="Quietens this screen only; anyone here may">
                            Silence here</button>`} />`}

      <div class=${`wrap-inner${lost ? " stale" : ""}`} aria-busy=${lost ? "true" : null}>
          <${ChecklistBar} steps=${run?.checklist} now=${state?.server_time} may=${may}
                           busy=${safeLocked}
                           onConfirm=${(id) => safely(() => confirmStep(id))}
                           silence=${silence} />

          <${AlarmBar} alarms=${state?.alarms} may=${may} busy=${safeLocked}
                       onAcknowledge=${acknowledgeAlarms}
                       silence=${run?.checklist?.length ? null : silence} />
          ${state?.health && !(Number.isFinite(state.health.supervisor_s)
                                && state.health.supervisor_s < 10) && html`
            <${Banner} tone="crit">
              <strong>Nothing is raising alarms.</strong> The panel's watcher has
              ${!Number.isFinite(state.health.supervisor_s) ? "not started"
                : `not looked at the rig for ${fmt.integer(state.health.supervisor_s)} s`}:
              faults, a lost link or a dragged rotor will not be announced, and
              a drive fault will not be made safe. Restart the panel, and see
              <span class="mono">journalctl -u vacuum-lab-web -p err</span> for why.
            <//>`}
          ${state?.update?.pending && html`
            <div data-test="update-banner"><${Banner} tone="warn">
              <strong>Installing version ${state.update.pending.tag}</strong>, asked for
              by ${state.update.pending.name}. The panel restarts in a moment and is
              back within about a minute; this page then reloads by itself. New runs
              wait until it is back. The pump keeps doing what it was told.
            <//></div>`}

          <${PromptBar} prompt=${run?.running ? run.prompt : null} now=${state?.server_time}
                        may=${may} busy=${busy} heldBy=${heldBy}
                        onDone=${(id) => safely(() => acknowledgeStep(id))}
                        silence=${state?.alarms?.alarms?.length || run?.checklist?.length
                          ? null : silence} />

          ${showShutdown && html`
            <${Banner} tone=${shutdown.active ? "warn"
                              : shutdown.ok ? "good" : "crit"}>
              <strong>${shutdown.active ? "Powering down:"
                        : shutdown.ok ? "Power-down complete:"
                        : "Power-down stopped:"}</strong> ${shutdown.message}
              ${shutdown.active && shutdown.rotor_hz != null
                ? ` Rotor at ${Math.round(shutdown.rotor_hz)} Hz.` : ""}
              ${shutdown.active && may !== "no" && html`
                <button class="small" style="margin-left:10px" onClick=${cancelPowerDown}>
                  ${may === "signin" ? "Sign in to cancel it"
                    : "Cancel — keep the backing pump running"}</button>`}
            <//>`}

          <${WatchingNotice} me=${me} control=${control} tab=${tab}
                             onReauth=${() => setReauth(true)}
                             onTakeOver=${() => setTakingOver(true)} />

          ${body()}
      </div>

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
        body=${html`<p>${POWER_DOWN}</p>`}
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
        confirmLabel=${pending?.label || "Send"}
        busy=${!!pending?.sending} error=${pending?.error}
        actions=${pending?.error && pending?.rule && me?.can?.override && html`
          <button class="danger" data-test="override-open" onClick=${overrideFromPending}>
            Override…</button>`}
        body=${html`
          <p>${pending?.reason ||
            "This changes what the hardware is doing right now."}</p>`}
        onCancel=${cancelPending} onConfirm=${confirmPending} />

      <${OverrideDialog} key=${overriding?.seq || 0} request=${overriding}
        onCancel=${() => setOverriding(null)} onConfirm=${confirmOverride} />

      ${reauth && html`<${ReauthDialog} open=${true} me=${me}
        onDone=${() => {
          setReauth(false);
          const next = afterReauth.current;
          afterReauth.current = null;
          notify(next ? "Welcome back." : "Welcome back. The controls are yours again.", "ok");
          setTick((t) => t + 1);
          next?.();
        }}
        onCancel=${() => { afterReauth.current = null; setReauth(false); }} />`}

      <${Toasts} items=${toasts} onDismiss=${dismiss} />
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));
