// Alarms: what went wrong, what the panel did about it, until somebody has
// seen it.
//
// The server's supervisor raises them (lab/alarms.py) and keeps each until it
// is acknowledged, even once its condition has cleared -- a fault at night is
// still on screen in the morning. This module shows them on every page, puts
// the worst one in the browser tab's title, and can sound an alarm for the
// critical ones.
//
// Every alarm says what was already done -- "motor off, fault acknowledged,
// forevacuum pump kept on" -- because an alarm that only says "fault" leaves
// the reader to guess whether the chamber is still under vacuum.

import { html } from "./html.js";
import { useEffect, useRef } from "preact/hooks";
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

export function AlarmBar({ alarms, onAcknowledge, busy }) {
  const list = alarms?.alarms || [];
  if (!list.length) return null;
  const critical = list.filter((a) => a.level === "critical").length;
  return html`
    <section class=${`alarm-bar ${critical ? "crit" : "warn"}`} role="alert">
      <header>
        <strong>${critical ? `${critical} critical alarm${critical > 1 ? "s" : ""}` : "Warning"}
          ${list.length > critical ? ` · ${list.length - critical} warning${list.length - critical > 1 ? "s" : ""}` : ""}</strong>
        ${list.length > 1 && html`
          <button class="small" disabled=${busy} onClick=${() => onAcknowledge("all")}>
            Acknowledge all</button>`}
      </header>
      ${list.slice(0, 4).map((alarm) => html`
        <div class=${`alarm ${alarm.level}`} key=${alarm.id}>
          <div class="alarm-head">
            <span class=${`pill ${alarm.level === "critical" ? "crit" : "warn"}`}>
              <span class="dot" />${alarm.active ? alarm.level : "cleared"}</span>
            <strong class="alarm-title">${alarm.title}</strong>
            <span class="small muted">${fmt.clock(alarm.raised_at)} · ${since(alarm.raised_at)} ago
              ${!alarm.active && alarm.cleared_at ? ` · cleared ${fmt.clock(alarm.cleared_at)}` : ""}</span>
            <button class="small right" disabled=${busy}
                    onClick=${() => onAcknowledge(alarm.id)}>Acknowledge</button>
          </div>
          <p class="alarm-detail">${alarm.detail}</p>
          ${alarm.actions?.length > 0 && html`
            <div class="small"><span class="muted">What the panel did:</span>
              <${ActionList} actions=${alarm.actions} /></div>`}
        </div>`)}
      ${list.length > 4 && html`<div class="small muted">and ${list.length - 4} more — see the
        alarm log on the Overview tab.</div>`}
    </section>`;
}

// --- sound and title ----------------------------------------------------------

let audio = null;

/** Browsers only allow sound after a user gesture: unlock on the first one. */
function unlockAudio() {
  if (audio || typeof window === "undefined") return;
  const Context = window.AudioContext || window.webkitAudioContext;
  if (!Context) return;
  audio = new Context();
}
if (typeof window !== "undefined") {
  window.addEventListener("pointerdown", unlockAudio, { once: true });
  window.addEventListener("keydown", unlockAudio, { once: true });
}

/** Two short tones: distinct from anything a browser plays by itself. */
function beep() {
  if (!audio) return;
  if (audio.state === "suspended") audio.resume().catch(() => {});
  const now = audio.currentTime;
  [[880, 0], [660, 0.22]].forEach(([frequency, offset]) => {
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
}

/**
 * Title and sound for unacknowledged alarms. The title carries the worst one,
 * so a panel left in a background tab still says something is wrong; the
 * sound repeats every few seconds while a critical alarm is unacknowledged.
 */
export function useAlarmSignals(alarms) {
  const base = useRef(typeof document !== "undefined" ? document.title : "");
  const list = alarms?.alarms || [];
  const critical = list.find((a) => a.level === "critical");
  const worst = critical || list[0];
  useEffect(() => {
    document.title = worst ? `⚠ ${worst.title} — Vacuum Lab` : base.current;
  }, [worst?.id, worst?.title]);
  useEffect(() => {
    if (!critical || settings().alarmSound === false) return undefined;
    beep();
    const timer = setInterval(beep, 4000);
    return () => clearInterval(timer);
  }, [critical?.id]);
}
