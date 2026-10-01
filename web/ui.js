// Small shared components: tiles, pills, dialogs, toasts, forms.

import { html } from "./html.js";
import { useEffect, useState } from "preact/hooks";
import * as fmt from "./format.js";

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

/** A confirmation dialog. Anything that moves hardware goes through one. */
export function Confirm({ open, title, body, confirmLabel = "Confirm",
                          tone = "primary", onConfirm, onCancel, extra }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (event) => { if (event.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true">
        <h2>${title}</h2>
        <div class="small">${body}</div>
        ${extra}
        <div class="actions">
          <button onClick=${onCancel}>Cancel</button>
          <button class=${tone} onClick=${onConfirm}>${confirmLabel}</button>
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
  useEffect(() => {
    if (!open) return;
    const onKey = (event) => { if (event.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;
  return html`
    <div class="overlay" onClick=${(e) => e.target === e.currentTarget && onCancel()}>
      <div class="dialog" role="dialog" aria-modal="true">
        <h2>${title}</h2>
        ${body && html`<div class="small">${body}</div>`}
        <div class="choices">
          ${options.map((option) => html`
            <button key=${option.key} class=${`choice ${option.tone || ""}`}
                    onClick=${() => onChoose(option.key)}>
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

export function Toasts({ items, onDismiss }) {
  return html`
    <div class="toast-stack">
      ${items.map((item) => html`
        <div class=${`toast ${item.tone || ""}`} key=${item.id}
             onClick=${() => onDismiss(item.id)}>${item.message}</div>`)}
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

/** Turn a procedure's field list into a config object of the right types. */
export function coerceConfig(fields, values) {
  const out = {};
  for (const field of fields) {
    const raw = values[field.key];
    if (field.type === "boolean") out[field.key] = !!raw;
    else if (field.type === "exponent") out[field.key] = fmt.parseExponent(raw);
    else if (field.type === "number") out[field.key] = Number(raw);
    else out[field.key] = raw;
  }
  return out;
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
    setItems((current) => [...current, { id, message, tone }]);
    setTimeout(() => setItems((c) => c.filter((i) => i.id !== id)),
               tone === "error" ? 9000 : 4500);
  };
  const dismiss = (id) => setItems((c) => c.filter((i) => i.id !== id));
  return [items, push, dismiss];
}
