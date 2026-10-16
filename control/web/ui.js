// Small shared components: tiles, pills, dialogs, toasts, forms.

import { html } from "./html.js";
import { useEffect, useRef, useState } from "preact/hooks";
import * as fmt from "./format.js";

/**
 * Focus into a dialog when it opens, and back where it was when it closes.
 *
 * Without it a keyboard or a screen reader stays on the page behind the
 * overlay -- on the button that opened it, which can be pressed again -- and
 * after the dialog the focus is lost at the top of the document. Escape
 * cancels. Returns the ref for the dialog's element.
 */
export function useDialog(open, onCancel) {
  const box = useRef(null);
  // The latest: a dialog that is sending passes none, and Escape does nothing.
  const cancel = useRef(onCancel);
  cancel.current = onCancel;
  useEffect(() => {
    if (!open) return undefined;
    const before = document.activeElement;
    const first = box.current?.querySelector(
      "input:not([disabled]), textarea:not([disabled]), select:not([disabled])")
      || box.current;
    first?.focus?.({ preventScroll: true });
    const onKey = (event) => { if (event.key === "Escape") cancel.current?.(); };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      if (before && document.contains(before)) before.focus?.({ preventScroll: true });
    };
  }, [open]);
  return box;
}

export function Pill({ tone = "idle", children, title }) {
  return html`<span class=${`pill ${tone}`} title=${title}>
    <span class="dot" />${children}</span>`;
}

export function Tile({ label, value, unit, foot, tone, accent }) {
  return html`
    <div class=${`tile${accent ? " accent" : ""}`}>
      <span class="label">${label}</span>
      <span class=${`value num${tone ? ` ${tone}` : ""}`}
            style=${tone ? `color:var(--${tone})` : null}>
        ${value}${unit ? html`<span class="unit"> ${unit}</span>` : null}
      </span>
      ${foot ? html`<span class="foot">${foot}</span>` : null}
    </div>`;
}

export function Card({ title, hint, actions, children, className = "" }) {
  return html`
    <section class=${`card ${className}`}>
      ${(title || actions) && html`
        <header>
          <div>
            ${title && html`<h2>${title}</h2>`}
            ${hint && html`<p class="hint">${hint}</p>`}
          </div>
          ${actions && html`<div class="row">${actions}</div>`}
        </header>`}
      ${children}
    </section>`;
}

export function Banner({ tone = "", children }) {
  if (!children) return null;
  return html`<div class=${`banner ${tone}`}>${children}</div>`;
}

export function Empty({ children }) {
  return html`<div class="empty">${children}</div>`;
}

/**
 * A confirmation dialog. Anything that moves hardware goes through one.
 *
 * The button says what it does ("Start turbopump"), never "Do it". A
 * refusal (`error`) is shown here, in the dialog that sent the request,
 * where the person is looking -- not in a toast that fades -- and `actions`
 * may offer the way on from it (an admin's Override…).
 */
export function Confirm({ open, title, body, confirmLabel = "Confirm",
                          tone = "primary", onConfirm, onCancel, extra, error,
                          busy = false, actions = null, disabled = false,
                          cancelLabel = "Cancel" }) {
  // While it is sending, it cannot be cancelled: the request has gone, and
  // its answer is shown here.
  const box = useDialog(open, busy ? null : onCancel);
  if (!open) return null;
  return html`
    <div class="overlay" onClick=${(e) => !busy && e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="confirm-title"
           tabIndex="-1" ref=${box}>
        <h2 id="confirm-title">${title}</h2>
        <div class="small">${body}</div>
        ${extra}
        ${error && html`
          <div class="banner crit small" role="alert" data-test="dialog-error">
            <strong>Refused:</strong> ${error}</div>`}
        <div class="actions">
          ${actions}
          <button data-test="confirm-cancel" disabled=${busy} onClick=${onCancel}>
            ${error ? "Close" : cancelLabel}</button>
          ${!error && html`
            <button class=${tone} data-test="confirm-ok" disabled=${busy || disabled}
                    onClick=${onConfirm}>${busy ? "Sending…" : confirmLabel}</button>`}
        </div>
      </div>
    </div>`;
}

/** A dialog that asks *which* action to take, not merely whether to proceed.
 *
 * Distinct from `Confirm` on purpose. When the choices differ in what they
 * leave behind — a chamber still under vacuum, or a pump brought to rest —
 * a single "are you sure?" hides the decision that actually matters, and the
 * operator answers the wrong question.
 */
export function Choose({ open, title, body, options = [], onChoose, onCancel }) {
  const box = useDialog(open, onCancel);
  if (!open) return null;
  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="choose-title"
           tabIndex="-1" ref=${box}>
        <h2 id="choose-title">${title}</h2>
        ${body && html`<div class="small">${body}</div>`}
        <div class="choices">
          ${options.map((option) => html`
            <button key=${option.key} class=${`choice ${option.tone || ""}`}
                    data-test=${`choice-${option.key}`} onClick=${() => onChoose(option.key)}>
              <strong>${option.label}</strong>
              <span class="help">${option.description}</span>
            </button>`)}
        </div>
        <div class="actions">
          <button onClick=${onCancel}>Cancel</button>
        </div>
      </div>
    </div>`;
}

/**
 * What just happened. A success fades by itself; an error stays until it is
 * dismissed -- on a tablet across the room nobody reads nine seconds of
 * text in time. Announced to a screen reader as it appears.
 */
export function Toasts({ items, onDismiss }) {
  return html`
    <div class="toast-stack" aria-live="polite">
      ${items.map((item) => html`
        <div class=${`toast ${item.tone || ""}`} key=${item.id}
             role=${item.tone === "error" ? "alert" : "status"}>
          <span>${item.message}</span>
          <button class="toast-close ghost" aria-label="Dismiss"
                  onClick=${() => onDismiss(item.id)}>×</button>
        </div>`)}
    </div>`;
}

/** Build one input from a procedure field descriptor served by the API. */
export function Field({ field, value, onChange }) {
  const id = `field-${field.key}`;

  if (field.type === "boolean") {
    return html`
      <label class="check" for=${id}>
        <input id=${id} type="checkbox" checked=${!!value}
               onChange=${(e) => onChange(e.target.checked)} />
        <span>
          ${field.label}
          ${field.help && html`<div class="help">${field.help}</div>`}
        </span>
      </label>`;
  }

  if (field.type === "select") {
    return html`
      <label class="field" for=${id}>
        <span>${field.label}</span>
        <select id=${id} value=${String(value)}
                onChange=${(e) => onChange(e.target.value)}>
          ${(field.options || []).map((option) => html`
            <option value=${String(option.value)}>${option.label}</option>`)}
        </select>
        ${field.help && html`<span class="help">${field.help}</span>`}
      </label>`;
  }

  // "exponent" fields accept 1e-5 as text, because typing 0.00001 into a
  // number input and counting the zeros is how people set the wrong target.
  const isExponent = field.type === "exponent";
  return html`
    <label class="field" for=${id}>
      <span>${field.label}</span>
      <span class="suffix">
        <input id=${id} type=${isExponent ? "text" : "number"}
               data-test=${`recipe-field-${field.key}`}
               inputmode=${isExponent ? "text" : "decimal"}
               value=${value ?? ""}
               min=${field.min} max=${field.max} step=${field.step || "any"}
               onInput=${(e) => onChange(
                 isExponent ? e.target.value : e.target.value)} />
        ${field.unit && html`<span class="unit">${field.unit}</span>`}
      </span>
      ${field.help && html`<span class="help">${field.help}</span>`}
    </label>`;
}

/** One numeric field's value, or null when the box is empty or not a number. */
function fieldNumber(field, raw) {
  if (field.type === "exponent") return fmt.parseExponent(raw);
  const text = String(raw ?? "").trim();
  if (!text) return null;
  const value = Number(text);
  return Number.isFinite(value) ? value : null;
}

/** Turn a procedure's field list into a config object of the right types.
 *  Only for a form `configProblems` passed: an empty box is never sent. */
export function coerceConfig(fields, values) {
  const out = {};
  for (const field of fields) {
    const raw = values[field.key];
    if (field.type === "boolean") out[field.key] = !!raw;
    else if (field.type === "exponent" || field.type === "number") {
      out[field.key] = fieldNumber(field, raw);
    } else out[field.key] = raw;
  }
  return out;
}

/** What is wrong with a filled-in form, one sentence per field. The server
 *  refuses the same things; saying so here keeps Start from being pressed. */
export function configProblems(fields, values) {
  const problems = [];
  for (const field of fields) {
    if (field.type !== "exponent" && field.type !== "number") continue;
    const label = String(field.label || field.key).replace(/^…/, "");
    const value = fieldNumber(field, values[field.key]);
    const unit = field.unit ? ` ${field.unit}` : "";
    if (value === null) {
      problems.push(`"${label}" needs a number.`);
    } else if ((field.min != null && value < field.min)
               || (field.max != null && value > field.max)) {
      problems.push(`"${label}" must be between ${field.min} and ${field.max}${unit}.`);
    }
  }
  return problems;
}

export function defaultConfig(fields) {
  const out = {};
  for (const field of fields) out[field.key] = field.default;
  return out;
}

/** A labelled key/value list. */
export function Pairs({ items }) {
  return html`
    <dl class="pairs">
      ${items.filter(Boolean).map(([key, value]) => html`
        <dt>${key}</dt><dd class="num">${value}</dd>`)}
    </dl>`;
}

export function useToasts() {
  const [items, setItems] = useState([]);
  const push = (message, tone = "") => {
    const id = Math.random().toString(36).slice(2);
    // The same error twice is one toast; at most four on screen.
    setItems((current) => [...current.filter((i) => i.message !== message),
                           { id, message, tone }].slice(-4));
    if (tone !== "error") {
      setTimeout(() => setItems((c) => c.filter((i) => i.id !== id)), 4500);
    }
  };
  const dismiss = (id) => setItems((c) => c.filter((i) => i.id !== id));
  return [items, push, dismiss];
}
