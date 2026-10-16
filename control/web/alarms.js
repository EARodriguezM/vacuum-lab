// Alarms: what went wrong, what the panel did about it, until somebody has
// seen it.
//
// The server's supervisor raises them (lab/alarms.py) and keeps each until it
// is acknowledged, even once its condition has cleared -- a fault at night is
// still on screen in the morning. This module shows them on every page, puts
// the worst one in the browser tab's title, and sounds -- for the critical
// ones, for a step a run's ending left to a person, and with a chime of its
// own for a guided run waiting on somebody.
//
// Every alarm says what was already done -- "motor off, fault acknowledged,
// forevacuum pump kept on" -- because an alarm that only says "fault" leaves
// the reader to guess whether the chamber is still under vacuum.

import { html } from "./html.js";
import { useEffect, useRef, useState } from "preact/hooks";
import { settings } from "./api.js";
import * as fmt from "./format.js";

function since(epoch) {
  return fmt.duration(Math.max(0, Date.now() / 1000 - epoch));
}

/** The steps an automatic or manual recovery took, as one readable line. */
export function ActionList({ actions }) {
  if (!actions?.length) return null;
  return html`
    <ul class="alarm-actions">
      ${actions.map((a) => html`
        <li class=${a.ok ? "done" : "failed"}>
          <span class="mark" aria-hidden="true">${a.ok ? "✓" : "✗"}</span>
          ${a.step}${!a.ok && a.error ? html` — <span class="muted">${a.error}</span>` : null}
        </li>`)}
    </ul>`;
}

/**
 * The button that answers an alarm or a step, for whoever is looking.
 *
 * `may`: "yes" -- press it; "reauth" -- an idle session, which types the
 * password and then the action goes ahead; "signin" -- a paired tablet,
 * where a person signs in first; "no" -- a viewer. Never a dead grey button
 * with no reason on a screen where something is wrong.
 */
export function SafetyButton({ may, busy, onPress, label, test, tone = "", small = true,
                              signin = null }) {
  if (may === "no") {
    return html`<span class="small muted" title="Needs the operator role">
      An operator answers this</span>`;
  }
  const text = may === "signin" ? (signin || `Sign in to ${label.toLowerCase()}`) : label;
  return html`
    <button class=${`${small ? "small " : ""}${tone}`} disabled=${busy}
            data-test=${test} onClick=${onPress}
            title=${may === "reauth" ? "Asks for your password, then does it" : ""}>
      ${text}</button>`;
}

export function AlarmBar({ alarms, onAcknowledge, busy, may = "yes", silence = null }) {
  const list = alarms?.alarms || [];
  if (!list.length) return null;
  const critical = list.filter((a) => a.level === "critical").length;
  // What a screen reader announces: only when the alarms themselves change,
  // never because "12 s ago" became "14 s ago".
  const spoken = list.slice(0, 4).map((a) => `${a.level}: ${a.title}`).join(". ");
  return html`
    <section class=${`alarm-bar ${critical ? "crit" : "warn"}`} aria-label="Alarms"
             data-test="alarm-bar">
      <div class="sr-only" role="alert">${spoken}</div>
      <header>
        <strong>${critical ? `${critical} critical alarm${critical > 1 ? "s" : ""}` : "Warning"}
          ${list.length > critical ? ` · ${list.length - critical} warning${list.length - critical > 1 ? "s" : ""}` : ""}</strong>
        <span class="row tight">
          ${silence}
          ${list.length > 1 && html`
            <${SafetyButton} may=${may} busy=${busy} label="Acknowledge all"
                             test="alarm-ack-all" onPress=${() => onAcknowledge("all")} />`}
        </span>
      </header>
      ${list.slice(0, 4).map((alarm) => html`
        <div class=${`alarm ${alarm.level}`} key=${alarm.id}>
          <div class="alarm-head">
            <span class=${`pill ${alarm.level === "critical" ? "crit" : "warn"}`}>
              <span class="dot" />${alarm.active ? alarm.level : "cleared"}</span>
            <strong class="alarm-title">${alarm.title}</strong>
            <span class="small muted" aria-hidden="true">${fmt.clock(alarm.raised_at)} · ${since(alarm.raised_at)} ago
              ${!alarm.active && alarm.cleared_at ? ` · cleared ${fmt.clock(alarm.cleared_at)}` : ""}</span>
            <span class="right">
              <${SafetyButton} may=${may} busy=${busy} label="Acknowledge" test="alarm-ack"
                               onPress=${() => onAcknowledge(alarm.id)} />
            </span>
          </div>
          <p class="alarm-detail">${alarm.detail}</p>
          ${alarm.actions?.length > 0 && html`
            <div class="small"><span class="muted">What the panel did:</span>
              <${ActionList} actions=${alarm.actions} /></div>`}
        </div>`)}
      ${list.length > 4 && html`<div class="small muted">and ${list.length - 4} more — see the
        alarm log on the Dashboard, under Recent activity.</div>`}
    </section>`;
}

// --- sound and title ----------------------------------------------------------

let audio = null;
let quiet = null;

/**
 * Browsers only allow sound after a user gesture: unlock on the first one.
 *
 * The context is suspended as soon as it exists, and again after every beep.
 * A running AudioContext renders silence without pause, and on the kiosk
 * tablet -- which taps itself once after every load to unlock sound -- that
 * silence was most of the panel's CPU: 0.39 of a core on an idle dashboard
 * against 0.01 for a blank page (measured 2026-10-10).
 */
function unlockAudio() {
  if (audio || typeof window === "undefined") return;
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  audio = new Context();
  audio.suspend().catch(() => {});
}
if (typeof window !== "undefined") {
  window.addEventListener("pointerdown", unlockAudio, { once: true });
  window.addEventListener("keydown", unlockAudio, { once: true });
}

/** Tones as [frequency, start offset], played and then back to silence. */
function play(tones) {
  if (!audio) return false;
  clearTimeout(quiet);
  const sound = () => {
    const now = audio.currentTime;
    tones.forEach(([frequency, offset]) => {
      const oscillator = audio.createOscillator();
      const gain = audio.createGain();
      oscillator.frequency.value = frequency;
      gain.gain.setValueAtTime(0.0001, now + offset);
      gain.gain.exponentialRampToValueAtTime(0.25, now + offset + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, now + offset + 0.18);
      oscillator.connect(gain).connect(audio.destination);
      oscillator.start(now + offset);
      oscillator.stop(now + offset + 0.2);
    });
    // Back to silence once the tones have played.
    quiet = setTimeout(() => audio.suspend().catch(() => {}), 1200);
  };
  // Always through resume(): it queues behind a suspend still in flight --
  // the one unlockAudio sends when the same tap both unlocks and beeps.
  audio.resume().then(sound, () => {});
  return true;
}

/** Two falling tones: an alarm, distinct from anything a browser plays. */
export function beep() {
  return play([[880, 0], [660, 0.22]]);
}

/** Three rising tones: a guided run is waiting for somebody -- not a fault. */
export function chime() {
  return play([[523, 0], [659, 0.18], [784, 0.36]]);
}

/** Everything sounding now, by key, that "Silence here" has not quietened. */
const silenced = new Set();

/**
 * What sounds and what the tab's title says, for everything that wants a
 * person: unacknowledged critical alarms (every 4 s), steps a run's ending
 * left by hand (every 4 s), a guided run waiting on somebody (a chime, then
 * again each minute), and a kiosk that has lost the lab PC (every 10 s for
 * two minutes, then every five: nobody can sign in to silence it while the
 * lab PC is gone, and it may be off for the night).
 *
 * "Silence here" (operators and above, this browser only) quietens what is
 * sounding now; anything new sounds again. The server never hears of it.
 * Returns [what is sounding, the silence function].
 */
export function useAlarmSignals({ alarms, prompt, checklist, lost }) {
  const base = useRef(typeof document !== "undefined" ? document.title : "");
  const [, setTick] = useState(0);
  const list = alarms?.alarms || [];
  const critical = list.find((a) => a.level === "critical");
  const worst = critical || list[0];
  const steps = checklist || [];

  const title = worst ? `⚠ ${worst.title}`
    : steps.length ? `⚠ To do by hand: ${steps[0].label}`
    : prompt ? `▶ Waiting for you: ${prompt.label}`
    : lost ? "⚠ No contact with the lab PC" : null;
  useEffect(() => {
    document.title = title ? `${title} — Vacuum Lab` : base.current;
  }, [title]);

  const urgent = [
    ...list.filter((a) => a.level === "critical").map((a) => `alarm:${a.id}`),
    ...steps.map((s) => `step:${s.id}`),
  ].filter((key) => !silenced.has(key));
  const promptKey = prompt && !silenced.has(`prompt:${prompt.id}`) ? `prompt:${prompt.id}` : null;
  const lostKey = lost && !silenced.has(`lost:${lost}`) ? `lost:${lost}` : null;
  const on = settings().alarmSound !== false;

  useEffect(() => {
    if (!on || !urgent.length) return undefined;
    beep();
    const timer = setInterval(beep, 4000);
    return () => clearInterval(timer);
  }, [on, urgent.join(",")]);
  useEffect(() => {
    if (!on || !promptKey || urgent.length) return undefined;
    chime();
    const timer = setInterval(chime, 60000);
    return () => clearInterval(timer);
  }, [on, promptKey, urgent.length > 0]);
  useEffect(() => {
    if (!on || !lostKey) return undefined;
    let count = 0;
    let timer = null;
    const next = () => {
      count += 1;
      timer = setTimeout(() => { beep(); next(); }, count <= 12 ? 10000 : 300000);
    };
    next();
    return () => clearTimeout(timer);
  }, [on, lostKey]);

  // Only what can actually be heard: before the first touch unlocks sound,
  // there is nothing to silence.
  const sounding = on && !!audio && (urgent.length > 0 || !!promptKey || !!lostKey);
  const silence = () => {
    urgent.forEach((key) => silenced.add(key));
    if (promptKey) silenced.add(promptKey);
    if (lostKey) silenced.add(lostKey);
    setTick((t) => t + 1);
  };
  return [sounding, silence];
}
