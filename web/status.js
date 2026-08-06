// Turning instrument readings into a sentence somebody can act on.
//
// The panel's first job is to answer "what is the rig doing right now?"
// without the reader decoding flags. Everything here is derived from a state
// snapshot; nothing talks to the network.

import * as fmt from "./format.js";

const NO_ERROR = new Set(["000000", "000", "0", ""]);

export function pumpSummary(pump) {
  if (!pump) return { tone: "idle", headline: "No pump configured", detail: "" };
  if (!pump.connected) {
    return {
      tone: "crit",
      headline: "No contact with the pump",
      detail: pump.error || "The drive unit is not answering on its serial port.",
    };
  }

  const status = pump.status || {};
  const setpoints = pump.setpoints || {};
  const speed = Number(status.ActualSpd) || 0;
  const target = Number(status.SetRotSpd) || 0;
  const nominal = Number(pump.identity?.NominalSpd) || 0;
  const station = setpoints.PumpgStatn === true;
  const motor = setpoints.MotorPump === true;
  const fault = status.ErrorCode && !NO_ERROR.has(String(status.ErrorCode).trim());

  if (fault) {
    return {
      tone: "crit",
      headline: `The pump has stopped with fault ${status.ErrorCode}`,
      detail: "Clear the cause, then acknowledge the fault to restart.",
    };
  }
  if (status.OvTempPump || status.OvTempElec) {
    return {
      tone: "crit",
      headline: "Over temperature",
      detail: status.OvTempPump
        ? "The pump body is too hot. Check the cooling."
        : "The drive electronics are too hot. Check the fan and the airflow.",
    };
  }
  if (!station) {
    return {
      tone: "idle",
      headline: "Everything is off",
      detail: "The pumping station is not running.",
    };
  }
  if (station && !motor && speed === 0) {
    return {
      tone: "good",
      headline: "Backing pump only",
      detail: "The station is roughing. The turbopump motor is off.",
    };
  }
  if (status.PumpAccel || (target && speed < target * 0.95)) {
    const share = nominal ? Math.round((speed / nominal) * 100) : null;
    return {
      tone: "warn",
      headline: `Spinning up — ${fmt.integer(speed)} Hz`,
      detail: share !== null
        ? `${share}% of the pump's nominal ${nominal} Hz. It is still accelerating.`
        : "The rotor is still accelerating.",
    };
  }
  if (speed > 0) {
    return {
      tone: "good",
      headline: `Running at ${fmt.integer(speed)} Hz`,
      detail: status.SetSpdAtt
        ? "The rotor is at its setpoint."
        : "The rotor is turning.",
    };
  }
  return {
    tone: "warn",
    headline: "Slowing down",
    detail: "The station is on but the rotor has stopped or is coasting.",
  };
}

export function gaugeSummary(gauge) {
  if (!gauge) return { tone: "idle", headline: "No gauge configured", detail: "" };
  if (!gauge.connected) {
    return {
      tone: "crit",
      headline: "No contact with the gauge controller",
      detail: gauge.error ||
        "The TPG is not answering. Check its power and the RS-232 link.",
    };
  }
  const readings = gauge.readings || [];
  const valid = readings.filter((r) => r.valid);
  if (!valid.length) {
    const reasons = readings.map((r) => `channel ${r.channel}: ${r.status_text}`);
    return {
      tone: "warn",
      headline: "No usable pressure reading",
      detail: reasons.join("; ") || "Both channels are out of range or switched off.",
    };
  }
  const best = Math.min(...valid.map((r) => r.mbar));
  return {
    tone: "good",
    headline: `${fmt.pressure(best, 2)} mbar`,
    detail: `From channel ${valid.find((r) => r.mbar === best).channel}.`,
  };
}

/** The lowest trustworthy pressure across channels, in millibar. */
export function bestPressure(gauge) {
  const valid = (gauge?.readings || []).filter((r) => r.valid);
  return valid.length ? Math.min(...valid.map((r) => r.mbar)) : null;
}

/** How the rig as a whole should be described, worst state first. */
export function overallSummary(devices) {
  const pump = pumpSummary(devices.pump);
  const gauge = gaugeSummary(devices.gauge);
  const rank = { crit: 3, warn: 2, good: 1, idle: 0 };
  return rank[gauge.tone] > rank[pump.tone] ? gauge : pump;
}

export function connectionTone(device) {
  if (!device) return "idle";
  if (!device.connected) return "crit";
  if (device.age > 10) return "warn";
  return "good";
}

/** Human-readable vacuum regime, so a number gets a meaning attached. */
export function regime(mbar) {
  if (mbar === null || mbar === undefined) return "";
  if (mbar > 300) return "atmosphere";
  if (mbar > 1) return "rough vacuum";
  if (mbar > 1e-3) return "medium vacuum";
  if (mbar > 1e-7) return "high vacuum";
  return "ultra-high vacuum";
}
