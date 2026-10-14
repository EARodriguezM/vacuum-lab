// Turning instrument readings into a sentence somebody can act on.
//
// The panel's first job is to answer "what is the rig doing right now?"
// without the reader decoding flags. Everything here is derived from a state
// snapshot; nothing talks to the network.

import * as fmt from "./format.js";
import { JUDGEMENT } from "./analysis.js";

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
    const meaning = pump.fault?.title ? ` — ${pump.fault.title.toLowerCase()}` : "";
    return {
      tone: "crit",
      headline: `The pump has stopped with fault ${status.ErrorCode}${meaning}`,
      detail: pump.fault?.cause
        ? `${pump.fault.cause} ${pump.fault.remedy || ""}`
        : "Clear the cause, then acknowledge the fault to restart.",
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
  // Motor off with the rotor still turning is a coast-down -- after a stop,
  // or after the panel made the pump safe. It must never read as a run-up,
  // and with the station off it is the one state that must never read as
  // "everything is off": a coasting rotor needs its backing pump.
  if (!motor && speed > 0) {
    return station
      ? {
        tone: "warn",
        headline: `Coasting down — ${fmt.integer(speed)} Hz`,
        detail: "The turbopump motor is off and the rotor is slowing, backed by the "
          + "forevacuum pump. Leave the station on until it is at rest.",
      }
      : {
        tone: "crit",
        headline: `Coasting without backing — ${fmt.integer(speed)} Hz`,
        detail: "The pumping station — the forevacuum pump — is off while the rotor "
          + "still turns. A coasting rotor needs backing: switch the station on.",
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
      // The drive's station flag, not proof the backing pump runs: it read
      // on with the backing pump off and the chamber open (2026-10-09).
      headline: "Station on, turbopump off",
      detail: "The pumping station is on and the turbopump motor is off.",
    };
  }
  // Below full speed with the motor on means one of two very different
  // things. Accelerating (the drive's own P307 flag) is a run-up. Not
  // accelerating is a rotor held down: the gas load needs more power than
  // the drive may give, and it is minutes or seconds from tripping. Calling
  // that "spinning up" is how an overload reads as progress.
  const switchpoint = nominal ? Math.round(nominal * 0.8) : null;
  // The speed the drive was actually asked for: standby and the speed
  // setting mode both put a healthy rotor below nominal on purpose.
  let expected = target;
  if (nominal && setpoints.Standby === true) {
    expected = nominal * (Number(setpoints.StdbySVal) || 66) / 100;
  } else if (nominal && Number(setpoints.OpModeTMP) === 1) {
    expected = nominal * (Number(setpoints.SpdSVal) || 100) / 100;
  }
  if (motor && expected && speed > 0 && speed < expected * JUDGEMENT.full_speed_fraction && !status.PumpAccel) {
    return {
      tone: "crit",
      headline: `Held down by gas load — ${fmt.integer(speed)} Hz`,
      detail: `The rotor is below full speed and not accelerating: there is more gas than `
        + `the drive may carry. Cut the gas flow or throttle the gate valve now`
        + (switchpoint ? ` — below ${switchpoint} Hz the drive trips (Err006).` : "."),
      dragged: true,
    };
  }
  if (motor && (status.PumpAccel || (target && speed < target * JUDGEMENT.full_speed_fraction))) {
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
    // Overrange is what this rig's only gauge says whenever the chamber is
    // at or near atmosphere -- vented, or before roughing starts. That is
    // the normal idle state, not a fault, and must not read like one.
    if (readings.some((r) => r.status === 2)) {
      return {
        tone: "idle",
        headline: "Chamber at atmosphere",
        detail: "The gauge reads above its range, as it does with the chamber "
          + "vented or before roughing has started. It shows a number again "
          + "once the backing pump has taken the chamber below about 1000 mbar.",
        atmosphere: true,
      };
    }
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
