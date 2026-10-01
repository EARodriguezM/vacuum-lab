// Root of the panel: polling, navigation, and the confirmation gate.
//
// One poll drives everything. /api/state carries every instrument's cached
// snapshot plus the run engine's status, so the views never fetch on their
// own except for the things that are expensive or rarely change (accessory
// parameters, run history, the capability probe).
//
// Every command that moves hardware goes through `ask()`, which puts the
// action in front of the operator in plain words before it is sent. The
// server independently refuses anything arriving without `confirm`, so this
// dialog is the courtesy, not the safety.

import { html } from "./html.js";
import { render } from "preact";
import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import { api, settings, updateSettings } from "./api.js";
import { Banner, Choose, Confirm, Toasts, useToasts } from "./ui.js";
import { DashboardView } from "./views/dashboard.js";
import { Overview } from "./views/overview.js";
import { PumpView } from "./views/pump.js";
import { GaugeView } from "./views/gauge.js";
import { TestsView } from "./views/tests.js";
import { HistoryView } from "./views/history.js";
import { SettingsView } from "./views/settings.js";
import * as fmt from "./format.js";

const TABS = [
  { key: "dashboard", label: "Dashboard" },
  { key: "overview", label: "Overview" },
  { key: "pump", label: "Pump" },
  { key: "gauge", label: "Gauge" },
  { key: "tests", label: "Process" },
  { key: "history", label: "History" },
  { key: "settings", label: "Settings" },
];

function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "auto") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

function App() {
  const [tab, setTab] = useState(
    () => location.hash.replace("#", "") || "dashboard");
  const [state, setState] = useState(null);
  const [live, setLive] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [poweringDown, setPoweringDown] = useState(false);
  const [toasts, notify, dismiss] = useToasts();
  const [tick, setTick] = useState(0);

  // A ref, not state: the poll loop reads it without needing to be rebuilt
  // every time a run starts or stops.
  const runningRef = useRef(false);

  useEffect(() => { applyTheme(settings().theme); }, [tick]);

  useEffect(() => {
    const onHash = () => setTab(location.hash.replace("#", "") || "dashboard");
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const navigate = useCallback((key) => {
    location.hash = key;
    setTab(key);
  }, []);

  // --- polling ------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    let timer = null;

    const poll = async () => {
      try {
        const next = await api.state();
        if (cancelled) return;
        setState(next);
        setError(null);
        runningRef.current = !!next.run?.running;

        // The live sample buffer is only worth fetching while something is
        // recording; otherwise it is an empty array every two seconds.
        if (runningRef.current) {
          try {
            const samples = await api.live(900);
            if (!cancelled) setLive(samples);
          } catch { /* the state poll already reported any outage */ }
        } else if (!cancelled) {
          setLive(null);
        }
      } catch (problem) {
        if (!cancelled) setError(problem.message);
      } finally {
        if (!cancelled) {
          timer = setTimeout(poll, Math.max(500, settings().pollMs));
        }
      }
    };

    poll();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [tick]);

  // --- commands -----------------------------------------------------------

  /** Put a hardware action in front of the operator before sending it. */
  const ask = useCallback((device, action, label, reason, value, options = {}) => {
    if (options.silent) {
      return send(device, action, value, false);
    }
    return new Promise((resolve) => {
      setPending({ device, action, label, reason, value, resolve, override: false });
    });
  }, []);

  const send = async (device, action, value, override) => {
    setBusy(true);
    try {
      const result = await api.command(device, action, value, { override });
      if (result.ok) {
        notify(`${fmt.titleCase(action)} sent.`, "ok");
      } else if (result.interlock) {
        notify(`Refused: ${result.error}`, "error");
      } else {
        notify(result.error || "The command failed.", "error");
      }
      return result;
    } catch (problem) {
      notify(problem.message, "error");
      return { ok: false, error: problem.message };
    } finally {
      setBusy(false);
    }
  };

  const confirmPending = async () => {
    const request = pending;
    setPending(null);
    const result = await send(request.device, request.action, request.value,
                              request.override);
    request.resolve?.(result);
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

  const startRun = async (body) => {
    setBusy(true);
    try {
      const result = await api.startRun(body);
      if (result.ok) notify(`Started run ${result.run.id}.`, "ok");
      else notify(result.error, "error");
    } catch (problem) {
      notify(problem.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const acknowledgeStep = async () => {
    setBusy(true);
    try {
      const result = await api.acknowledgeStep();
      if (result.ok) notify("Step confirmed — the run continues.", "ok");
      else notify(result.error, "error");
    } catch (problem) {
      notify(problem.message, "error");
    } finally {
      setBusy(false);
    }
  };

  // A full-vacuum run has no ending of its own: stopping it *is* how it
  // finishes, and powering down is the expected answer. Every other
  // procedure runs to its own end, so stopping one early usually means
  // "leave the chamber as it is and let me look at it".
  const stopOptions = (procedure) => {
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
        + "the station. Takes several minutes; the chamber only comes back "
        + "up to air pressure if you vent it.",
    };
    return procedure === "full_vacuum" ? [down, keep] : [keep, down];
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
    setBusy(true);
    try {
      const result = await api.stopRun(
        shutdown ? "stopped by the operator, powering the pump down"
                 : "stopped by the operator, chamber left under vacuum",
        shutdown);
      if (!result.ok) notify(result.error, "error");
      else if (shutdown) notify("Stopping the run, then powering down…", "ok");
      else notify("Stopping the run. The pump keeps running.", "ok");
    } catch (problem) {
      notify(problem.message, "error");
    } finally {
      setBusy(false);
    }
  };

  // --- render -------------------------------------------------------------

  const theme = settings().theme;
  const cycleTheme = () => {
    const next = theme === "auto" ? "light" : theme === "light" ? "dark" : "auto";
    updateSettings({ theme: next });
    applyTheme(next);
    setTick((t) => t + 1);
  };

  // The guided power-down, reachable without a run to stop: switch the
  // motor off, wait for the rotor to reach rest with the backing pump still
  // running, then stop the station. The server owns the sequence.
  const powerDown = async () => {
    setPoweringDown(false);
    setBusy(true);
    try {
      const result = await api.shutdownPump("requested from the pump page");
      if (result.ok) notify("Powering the pump down…", "ok");
      else notify(result.error, "error");
    } catch (problem) {
      notify(problem.message, "error");
    } finally {
      setBusy(false);
    }
  };

  const run = state?.run;
  // Reported whether or not a run is live: a power-down outlives the run
  // that asked for it, and the rotor takes minutes to stop. A *finished*
  // one is only worth showing while its outcome is still true -- once the
  // station is running again, "rotor at rest, station off" is a lie sitting
  // at the top of the page.
  const shutdown = run?.shutdown;
  const stationOn = state?.devices?.pump?.setpoints?.PumpgStatn === true;
  const showShutdown = shutdown && (shutdown.active || !stationOn);
  const body = () => {
    if (!state && error) {
      return html`
        <${Banner} tone="crit">
          <strong>Not connected.</strong> ${error}
          <div class="small" style="margin-top:8px">
            Set the controller address under
            <a href="#settings" onClick=${() => navigate("settings")}>Settings</a>
            if the server is on another machine.
          </div>
        <//>`;
    }
    if (!state) return html`<div class="boot">Connecting…</div>`;

    switch (tab) {
      case "dashboard":
        return html`<${DashboardView} state=${state} live=${live} />`;
      case "pump":
        return html`<${PumpView} state=${state} onCommand=${pumpCommand}
                                 onWriteParam=${writeParam} busy=${busy}
                                 onShutdown=${() => setPoweringDown(true)}
                                 shutdown=${shutdown} />`;
      case "gauge":
        return html`<${GaugeView} state=${state} onCommand=${gaugeCommand}
                                  busy=${busy} />`;
      case "tests":
        return html`<${TestsView} state=${state} live=${live} onStart=${startRun}
                                  onStop=${stopRun} busy=${busy}
                                  onAcknowledge=${acknowledgeStep} />`;
      case "history":
        return html`<${HistoryView} notify=${notify} />`;
      case "settings":
        return html`<${SettingsView} state=${state} notify=${notify}
                                     onSettingsChanged=${() => setTick((t) => t + 1)} />`;
      default:
        return html`<${Overview} state=${state} live=${live} onStop=${stopRun}
                                 onNavigate=${navigate} />`;
    }
  };

  return html`
    <div class="wrap">
      <header class="top">
        <div class="brand">
          <h1>Vacuum Lab Control</h1>
          <span class="sub">
            TC 600 turbopump · TPG gauge controller
            ${state ? ` · up ${fmt.duration(state.uptime_s)}` : ""}
          </span>
        </div>
        <div class="top-actions">
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
          </button>`)}
      </nav>

      ${error && state && html`
        <${Banner} tone="warn">Lost contact with the server: ${error}<//>`}

      ${showShutdown && html`
        <${Banner} tone=${shutdown.active ? "warn"
                          : shutdown.ok ? "good" : "crit"}>
          <strong>${shutdown.active ? "Powering down:"
                    : shutdown.ok ? "Power-down complete:"
                    : "Power-down stopped:"}</strong> ${shutdown.message}
          ${shutdown.active && shutdown.rotor_hz != null
            ? ` Rotor at ${Math.round(shutdown.rotor_hz)} Hz.` : ""}
        <//>`}

      ${body()}

      <${Choose} open=${stopping}
        title="Stop the run"
        body=${html`<p>The recording ends either way and everything measured
          so far is kept. What should the pump do?</p>`}
        options=${stopOptions(run?.procedure)}
        onChoose=${finishStop} onCancel=${() => setStopping(false)} />

      <${Confirm} open=${poweringDown}
        title="Power the pump down"
        tone="danger" confirmLabel="Power down"
        body=${html`<p>The turbopump motor goes off, the rotor coasts to a
          stop with the backing pump still running, and the pumping station
          stops only once it is at rest. This takes several minutes and the
          chamber is left under whatever vacuum it holds.</p>`}
        onCancel=${() => setPoweringDown(false)} onConfirm=${powerDown} />

      <${Confirm} open=${!!pending}
        title=${pending?.label || "Confirm"}
        tone=${/off|stop|delete/i.test(pending?.action || "") ? "danger" : "primary"}
        confirmLabel="Do it"
        body=${html`
          <p>${pending?.reason ||
            "This changes what the hardware is doing right now."}</p>`}
        extra=${pending?.reason ? html`
          <label class="check">
            <input type="checkbox" checked=${pending.override}
                   onChange=${(e) => setPending({ ...pending,
                                                  override: e.target.checked })} />
            <span>Override the interlock
              <div class="help">Only when you know why the rule does not apply.
                Overrides are logged.</div></span>
          </label>` : null}
        onCancel=${cancelPending} onConfirm=${confirmPending} />

      <${Toasts} items=${toasts} onDismiss=${dismiss} />
    </div>`;
}

render(html`<${App} />`, document.getElementById("root"));
