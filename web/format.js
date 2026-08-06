// Formatting shared by every view.
//
// Pressure is the awkward one: it spans fourteen decades, so it is always
// rendered in scientific notation with a fixed mantissa width. A chamber at
// 3.2e-7 mbar and one at 3.2e-3 mbar must never look alike at a glance.

export function pressure(mbar, digits = 2) {
  if (mbar === null || mbar === undefined || Number.isNaN(mbar)) return "—";
  if (mbar === 0) return "0";
  return Number(mbar).toExponential(digits).replace("e", "e");
}

export function pressureParts(mbar, digits = 2) {
  if (mbar === null || mbar === undefined || Number.isNaN(mbar)) {
    return { mantissa: "—", exponent: null };
  }
  const text = Number(mbar).toExponential(digits);
  const [mantissa, exponent] = text.split("e");
  return { mantissa, exponent: Number(exponent) };
}

export function number(value, digits = 1, fallback = "—") {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return Number(value).toFixed(digits);
}

export function integer(value, fallback = "—") {
  if (value === null || value === undefined || Number.isNaN(value)) return fallback;
  return Math.round(Number(value)).toString();
}

/** Compact duration: 45s, 12m 30s, 3h 07m, 2d 04h. */
export function duration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total}s`;
  const m = Math.floor(total / 60), s = total % 60;
  if (m < 60) return `${m}m ${String(s).padStart(2, "0")}s`;
  const h = Math.floor(m / 60), rm = m % 60;
  if (h < 24) return `${h}h ${String(rm).padStart(2, "0")}m`;
  return `${Math.floor(h / 24)}d ${String(h % 24).padStart(2, "0")}h`;
}

/** Axis-friendly elapsed label: 0, 30s, 5m, 1h20. */
export function elapsed(seconds) {
  if (seconds < 1) return "0";
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const m = seconds / 60;
  if (m < 90) return `${Math.round(m)}m`;
  const h = Math.floor(m / 60), rm = Math.round(m % 60);
  return rm ? `${h}h${String(rm).padStart(2, "0")}` : `${h}h`;
}

export function clock(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleTimeString([], {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export function dateTime(epochSeconds) {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString([], {
    year: "numeric", month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  });
}

export function bytes(count) {
  if (!count) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = count, index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index++; }
  return `${value.toFixed(index ? 1 : 0)} ${units[index]}`;
}

export function titleCase(text) {
  return String(text || "").replace(/[_-]+/g, " ")
    .replace(/^\w/, (c) => c.toUpperCase());
}

/** Parse "1e-5", "1E-5", "0.00001" alike; returns null if unparseable. */
export function parseExponent(text) {
  if (typeof text === "number") return text;
  const value = Number(String(text).trim().replace(",", "."));
  return Number.isFinite(value) ? value : null;
}
