// What a person must do with their own hands, on every page.
//
// A guided run stops and asks for each step nothing drives -- open the argon,
// strike the plasma, open the shutter -- and waits, recording all the while.
// That request used to exist only on the Process tab: somebody watching the
// Dashboard on the wall saw a run that had simply stopped moving. Now it sits
// under the header of every page, chimes, and puts itself in the tab's title.
//
// The same goes for what a run's ending could not do itself: a plasma or an
// argon valve worked by hand is still on until somebody says otherwise, and
// the panel cannot see it. Those steps stay on every page, sounding, until
// each is confirmed -- and no run starts meanwhile (lab/runs.py).

import { html } from "./html.js";
import { SafetyButton } from "./alarms.js";
import * as fmt from "./format.js";

function waited(since, now) {
  return fmt.duration(Math.max(0, (now || Date.now() / 1000) - since));
}

/**
 * The step a guided run is waiting for. Done carries the step's own number:
 * a press for a step the run has since replaced is refused by the server,
 * never taken for the new one.
 *
 * `may` is how this person may answer (see SafetyButton); confirming needs
 * control of the rig, so `heldBy` names whoever has it instead.
 */
export function PromptBar({ prompt, now, may, busy, heldBy, onDone, silence }) {
  if (!prompt) return null;
  return html`
    <section class="prompt-bar" aria-label="Waiting for you" data-test="prompt">
      <div class="prompt-head">
        <span class="pill warn"><span class="dot" />Waiting for you</span>
        <span class="small muted" aria-hidden="true">for ${waited(prompt.since, now)}</span>
        <span class="right">${silence}</span>
      </div>
      <p class="prompt-text" role="alert">${prompt.instruction}</p>
      ${prompt.note && html`<p class="prompt-note">${prompt.note}</p>`}
      <div class="row">
        ${heldBy
          ? html`<span class="small"><strong>${heldBy}</strong></span>`
          : html`<${SafetyButton} may=${may} busy=${busy} small=${false} tone="primary"
                                  label="Done — continue the run" test="prompt-done"
                                  signin="Sign in to confirm"
                                  onPress=${() => onDone(prompt.id)} />`}
        <span class="small muted">
          ${prompt.label} → ${prompt.value}. The run keeps recording while you do
          this; its checks keep running.
        </span>
      </div>
    </section>`;
}

/** What a run's ending left to a person, one confirmation per step. */
export function ChecklistBar({ steps, now, may, busy, onConfirm, silence }) {
  if (!steps?.length) return null;
  return html`
    <section class="checklist-bar" aria-label="To do by hand" data-test="checklist">
      <div class="prompt-head">
        <strong>To do by hand — the run has ended, and the panel cannot see these</strong>
        <span class="right">${silence}</span>
      </div>
      <div class="sr-only" role="alert">
        ${steps.map((step) => step.instruction).join(" ")}</div>
      <ul class="checklist">
        ${steps.map((step) => html`
          <li key=${step.id}>
            <span class="checklist-text">
              <strong>${step.instruction}</strong>
              <span class="small muted" aria-hidden="true">
                ${step.run_id ? ` Run ${step.run_id} · ` : " "}waiting ${waited(step.since, now)}</span>
            </span>
            <${SafetyButton} may=${may} busy=${busy} label="Done" test="checklist-done"
                             signin="Sign in to confirm"
                             onPress=${() => onConfirm(step.id)} />
          </li>`)}
      </ul>
      <div class="small muted">No new run starts until each is confirmed.</div>
    </section>`;
}
