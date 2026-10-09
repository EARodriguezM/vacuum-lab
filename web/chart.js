// SVG charts, hand-rolled.
//
// No chart library: the lab PC is offline, and what these views need is a
// small set of forms done properly -- time series stacked on a shared time
// axis, a scatter for the pump's load curve, state strips for the things that
// are on or off. Everything goes through one component, `Plot`.
//
// The rules this file keeps, and why:
//
//   * **Never two y-axes.** Pressure spans fourteen decades, rotor speed one,
//     power two. Sharing an axis between any two invents a correlation. Each
//     measure gets its own chart; charts that belong together are stacked on
//     one time axis and share a crosshair, which is how the eye compares them.
//   * **Aqua is below 3:1 contrast on the light surface**, so a line drawn in
//     it carries a direct end-label, and a scatter that uses it has a legend
//     and a table view. That is the required relief, not decoration.
//   * **Text never wears a series colour.** Labels, values and annotations use
//     the ink tokens; identity comes from the coloured key beside them.
//   * **Thresholds are dashed, gridlines are not.** A dashed line on these
//     charts always means a limit somebody wrote down -- the 80 % switchpoint,
//     the crossover pressure, the supply tolerance -- never a grid.
//   * **A gap is a gap.** An underrange gauge or a server that was down has
//     measured nothing; the line breaks there rather than drawing a smooth
//     trend through a period when nothing was known.
//
// Pressure is drawn on a log scale, because a pump-down that is a featureless
// cliff on a linear axis is a straight line with a knee on a log one, and the
// knee is the whole diagnostic.

import { html } from "./html.js";
import { useLayoutEffect, useMemo, useRef, useState } from "preact/hooks";
import * as fmt from "./format.js";

const PAD = { top: 10, right: 66, bottom: 22, left: 54 };
const PHASE_ROW = 15;          // headroom for phase labels
const STRIP_ROW = 13;          // height of one state-strip row
const STRIP_GAP = 4;

const log10 = (value) => Math.log(value) / Math.LN10;
const finite = (value) => typeof value === "number" && Number.isFinite(value);

/** Track a container's pixel width so text stays crisp instead of scaling. */
export function useWidth(initial = 680) {
  const ref = useRef(null);
  const [width, setWidth] = useState(initial);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return undefined;
    const measure = () => setWidth(Math.max(220, node.clientWidth));
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  return [ref, width];
}

// --- domains and ticks ------------------------------------------------------

/** Decade ticks spanning the data, so a log axis reads as powers of ten. */
function decadeTicks(low, high, most = 8) {
  const first = Math.floor(log10(low) + 1e-9);
  const last = Math.ceil(log10(high) - 1e-9);
  const stride = Math.max(1, Math.ceil((last - first) / most));
  const ticks = [];
  for (let power = first; power <= last; power += stride) ticks.push(10 ** power);
  return ticks;
}

function niceStep(span, count) {
  const raw = span / Math.max(1, count);
  const magnitude = 10 ** Math.floor(log10(raw));
  // The round step nearest the raw one, judged on a log scale: 530 becomes
  // 500, not 1000, so a 0-1590 Hz axis gets four ticks rather than two.
  let best = magnitude;
  for (const factor of [1, 2, 2.5, 5, 10]) {
    if (Math.abs(log10(factor * magnitude / raw)) < Math.abs(log10(best / raw))) {
      best = factor * magnitude;
    }
  }
  return best;
}

function niceTicks(low, high, count = 5) {
  if (!(high > low)) return [low];
  const step = niceStep(high - low, count);
  const ticks = [];
  for (let value = Math.ceil(low / step - 1e-9) * step; value <= high + step * 1e-9;
       value += step) {
    ticks.push(Number(value.toFixed(10)));
  }
  return ticks;
}

/** Durations a time axis may step by: round numbers in human units. */
const TIME_STEPS = [
  1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
  3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000,
];

function timeTicks(low, high, count, local) {
  const span = high - low;
  if (!(span > 0)) return [low];
  const step = TIME_STEPS.find((s) => span / s <= count) || TIME_STEPS.at(-1);
  // Clock and date ticks land on round local times (14:00, not 13:47); the
  // timezone offset shifts the grid so "every hour" means on the hour here.
  const offset = local ? -new Date(low * 1000).getTimezoneOffset() * 60 : 0;
  const ticks = [];
  for (let value = Math.ceil((low + offset) / step) * step - offset;
       value <= high; value += step) {
    ticks.push(value);
  }
  return { ticks, step };
}

/** Ticks for a log time axis: round human durations, not bare decades. */
function logTimeTicks(low, high) {
  const candidates = [1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 1200, 1800,
                      3600, 7200, 18000, 36000, 86400, 172800, 432000];
  let ticks = candidates.filter((v) => v >= low && v <= high);
  while (ticks.length > 7) ticks = ticks.filter((_, i) => i % 2 === 0);
  return ticks;
}

function formatTick(value, kind, step = 60) {
  switch (kind) {
    case "clock": {
      const date = new Date(value * 1000);
      return step < 60
        ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
        : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    case "date": {
      const date = new Date(value * 1000);
      return step < 86400
        ? date.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })
        : date.toLocaleDateString([], { month: "short", day: "numeric" });
    }
    case "elapsed":
      return elapsedTick(value, step);
    case "logtime":
      return fmt.elapsed(value);
    case "pressure":
      return `1e${Math.round(log10(value))}`;
    default:
      return fmt.number(value, Math.abs(value) >= 10 || value === 0 ? 0 : 1);
  }
}

/** Run-time tick labels that stay distinct at every step: 1:30, 4 min, 1 h 15. */
function elapsedTick(value, step) {
  const total = Math.round(value);
  if (step < 60) {
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  }
  if (total < 3600) return `${Math.round(total / 60)} min`;
  const hours = Math.floor(total / 3600);
  const minutes = Math.round((total % 3600) / 60);
  return minutes ? `${hours} h ${String(minutes).padStart(2, "0")}` : `${hours} h`;
}

/** How a hovered x reads in a tooltip header. */
export function formatX(value, kind) {
  switch (kind) {
    case "clock":
      return new Date(value * 1000).toLocaleTimeString([], {
        hour: "2-digit", minute: "2-digit", second: "2-digit" });
    case "date":
      return fmt.dateTime(value);
    case "elapsed":
      return `t + ${fmt.duration(value)}`;
    case "logtime":
      return `${fmt.duration(value)} after start`;
    case "pressure":
      return `${fmt.pressure(value, 2)} mbar`;
    default:
      return fmt.number(value, 2);
  }
}

function yRange(values, axis) {
  let low = axis.min ?? null;
  let high = axis.max ?? null;
  const usable = values.filter((v) => finite(v) && (!axis.log || v > 0));
  if (axis.log) {
    const lo = usable.length ? Math.min(...usable) : 1e-6;
    const hi = usable.length ? Math.max(...usable) : 1e-3;
    // `include` widens a log axis too: a close-up that must always show a
    // setpoint and a ceiling, whatever the data did.
    const extra = [].concat(axis.include ?? []).filter((v) => finite(v) && v > 0);
    const lowest = Math.min(lo, ...extra);
    const highest = Math.max(hi, ...extra);
    low = low ?? 10 ** Math.floor(log10(lowest) + 1e-9);
    high = high ?? 10 ** Math.ceil(log10(highest) - 1e-9);
    if (axis.min == null && axis.floor != null) low = Math.max(low, axis.floor);
    if (high <= low) high = low * 10;
    return [low, high];
  }
  if (usable.length) {
    const lo = Math.min(...usable);
    const hi = Math.max(...usable);
    if (low == null) low = lo;
    if (high == null) high = hi;
    if (axis.min == null && axis.max == null) {
      const span = high - low;
      const margin = span === 0 ? Math.max(1, Math.abs(high) * 0.1) : span * 0.08;
      low -= margin;
      high += margin;
    }
  } else {
    low = low ?? 0;
    high = high ?? 1;
  }
  if (axis.include != null) {
    low = Math.min(low, ...[].concat(axis.include));
    high = Math.max(high, ...[].concat(axis.include));
  }
  if (axis.zero && low > 0) low = 0;
  if (axis.minSpan && high - low < axis.minSpan) {
    const middle = (high + low) / 2;
    low = middle - axis.minSpan / 2;
    high = middle + axis.minSpan / 2;
  }
  if (high <= low) high = low + 1;
  return [low, high];
}

/** Index of the point whose x is nearest `value`, in an x-sorted list. */
function nearestIndex(points, value) {
  let low = 0;
  let high = points.length - 1;
  if (high < 0) return -1;
  while (high - low > 1) {
    const middle = (low + high) >> 1;
    if (points[middle][0] < value) low = middle;
    else high = middle;
  }
  return Math.abs(points[low][0] - value) <= Math.abs(points[high][0] - value)
    ? low : high;
}

/** The value a series holds at x: nearest sample, or the step it is on. */
function valueAt(entry, value, tolerance) {
  const points = entry.points;
  if (!points.length) return null;
  if (entry.mark === "step") {
    let found = null;
    for (const point of points) {
      if (point[0] > value) break;
      found = point;
    }
    return found && finite(found[1]) ? found[1] : null;
  }
  const index = nearestIndex(points, value);
  const point = points[index];
  if (!point || !finite(point[1])) return null;
  return Math.abs(point[0] - value) <= tolerance ? point[1] : null;
}

/** Median spacing of x, used to tell a gap in the data from its cadence. */
function medianSpacing(points) {
  const gaps = [];
  for (let i = 1; i < points.length && gaps.length < 400; i++) {
    const gap = points[i][0] - points[i - 1][0];
    if (gap > 0) gaps.push(gap);
  }
  if (!gaps.length) return 0;
  gaps.sort((a, b) => a - b);
  return gaps[gaps.length >> 1];
}

// --- the plot -----------------------------------------------------------------

/**
 * One chart. Every series shares one x and one y axis.
 *
 * series: [{ key, label, color: "--series-1", points: [[x, y, meta?], ...],
 *            mark: "line" | "step" | "scatter", faint, dash, endLabel }]
 * x / y:  { log, min, max, kind ("clock"|"elapsed"|"date"|"pressure"|
 *           "logtime"|"number"), zero, minSpan, include, floor }
 * refs:   [{ axis: "y"|"x", value, label, tone: "warn"|"crit"|"muted" }]
 * bands:  [{ axis: "y"|"x", from, to, label }]
 * phases: [{ from, to, label }]         labelled bands along x
 * events: [{ x, label, level }]         markers from the run log
 * strips: [{ label, segments: [{ from, to, tone, label }] }]
 * cursor / onCursor: a shared crosshair, in the units of `cursorOf`
 */
export function Plot({
  series = [], height = 170, x = {}, y = {}, refs = [], bands = [],
  phases = [], events = [], strips = [], cursor = null, onCursor = null,
  formatValue = (v) => fmt.number(v, 1), unit = "", emptyText = "No data yet.",
  xLabels = true, ariaLabel = "", gapFactor = Infinity, title = "",
}) {
  const [ref, width] = useWidth();
  // A synced chart's crosshair lives in its parent, so every chart in a
  // stack draws the same moment; a lone chart keeps its own.
  const synced = typeof onCursor === "function";
  const [localCursor, setLocalCursor] = useState(null);
  const [pointer, setPointer] = useState(null);          // scatter only
  const activeCursor = synced ? cursor : localCursor;

  const live = useMemo(
    () => series.filter((s) => s.points && s.points.some((p) => finite(p[1]))),
    [series]);
  const scatter = live.length > 0 && live.every((s) => s.mark === "scatter");

  const stripHeight = strips.length ? strips.length * (STRIP_ROW + STRIP_GAP) + 2 : 0;
  const top = PAD.top + (phases.some((phase) => phase.label) ? PHASE_ROW : 0);
  // A chart in the middle of a stack drops its time labels -- the bottom
  // chart carries them for all -- and gives the space back to its plot.
  const bottom = xLabels ? PAD.bottom : 6;

  // Geometry is memoised: a shared crosshair re-renders every chart in a
  // stack on each pointer move, and rebuilding a few thousand path segments
  // per chart per move is what makes a stack feel sticky on the lab PC.
  const geometry = useMemo(() => {
    if (!live.length) return null;
    const plotW = Math.max(40, width - PAD.left - PAD.right);
    const plotH = Math.max(36, height - top - bottom - stripHeight);

    const xs = live.flatMap((s) => s.points.map((p) => p[0])).filter(finite);
    let xLow = x.min ?? Math.min(...xs);
    let xHigh = x.max ?? Math.max(...xs);
    if (x.log) {
      xLow = Math.max(xLow, x.floor ?? 1e-12);
      if (x.kind === "pressure") {
        xLow = 10 ** Math.floor(log10(xLow) + 1e-9);
        xHigh = 10 ** Math.ceil(log10(xHigh) - 1e-9);
      }
    }
    if (!(xHigh > xLow)) xHigh = xLow + (x.log ? xLow * 9 : 1);

    const ys = live.flatMap((s) => s.points.map((p) => p[1]));
    const [yLow, yHigh] = yRange(ys, y);

    const sx = x.log
      ? (v) => PAD.left + ((log10(v) - log10(xLow)) / (log10(xHigh) - log10(xLow))) * plotW
      : (v) => PAD.left + ((v - xLow) / (xHigh - xLow)) * plotW;
    const invX = x.log
      ? (px) => 10 ** (log10(xLow) + ((px - PAD.left) / plotW) * (log10(xHigh) - log10(xLow)))
      : (px) => xLow + ((px - PAD.left) / plotW) * (xHigh - xLow);
    const syRaw = y.log
      ? (v) => (log10(v) - log10(yLow)) / (log10(yHigh) - log10(yLow))
      : (v) => (v - yLow) / (yHigh - yLow);
    const sy = (v) => top + plotH - Math.min(1.02, Math.max(-0.02, syRaw(v))) * plotH;
    const usableY = (v) => finite(v) && (!y.log || v > 0);
    const usableX = (v) => finite(v) && (!x.log || v > 0);

    let yTicks = y.log ? decadeTicks(yLow, yHigh, Math.max(3, Math.floor(plotH / 26)))
                       : niceTicks(yLow, yHigh, Math.max(3, Math.floor(plotH / 26)));
    yTicks = yTicks.filter((v) => v >= yLow - 1e-12 && v <= yHigh * (1 + 1e-9));

    const count = Math.max(2, Math.floor(plotW / 92));
    let xTicks;
    let xStep = 60;
    if (x.kind === "clock" || x.kind === "date" || x.kind === "elapsed") {
      const result = timeTicks(xLow, xHigh, count, x.kind !== "elapsed");
      xTicks = result.ticks;
      xStep = result.step;
    } else if (x.log && x.kind === "logtime") {
      xTicks = logTimeTicks(xLow, xHigh);
    } else if (x.log) {
      xTicks = decadeTicks(xLow, xHigh, count + 2);
    } else {
      xTicks = niceTicks(xLow, xHigh, count);
    }

    // Paths, breaking at missing values -- a gauge out of range, a link that
    // was down. `gapFactor` can also break at a gap in time wider than that
    // many median spacings, but it is off by default: decimated data is
    // irregularly spaced on purpose, and reading that as missing data drew
    // every long run as a dashed line.
    const paths = [];
    const dots = [];
    for (const entry of live) {
      if (entry.mark === "scatter") {
        for (const point of entry.points) {
          if (!usableX(point[0]) || !usableY(point[1])) continue;
          dots.push({ cx: sx(point[0]), cy: sy(point[1]), color: entry.color,
                      key: entry.key });
        }
        continue;
      }
      if (entry.dots) {
        for (const point of entry.points) {
          if (!usableX(point[0]) || !usableY(point[1])) continue;
          dots.push({ cx: sx(point[0]), cy: sy(point[1]), color: entry.color,
                      key: entry.key });
        }
      }
      const spacing = entry.dots ? 0 : medianSpacing(entry.points);
      const limit = spacing > 0 ? spacing * gapFactor : Infinity;
      let d = "";
      let pen = false;
      let lastX = null;
      let lastY = null;
      for (const [px, py] of entry.points) {
        if (!usableX(px) || !usableY(py)) { pen = false; continue; }
        if (pen && lastX !== null && px - lastX > limit) pen = false;
        const X = sx(px).toFixed(1);
        const Y = sy(py).toFixed(1);
        if (entry.mark === "step" && pen) {
          d += `H${X} V${Y} `;
        } else {
          d += `${pen ? "L" : "M"}${X},${Y} `;
        }
        pen = true;
        lastX = px;
        lastY = py;
      }
      if (entry.mark === "step" && pen && entry.extendTo != null && lastY !== null) {
        d += `H${sx(Math.min(entry.extendTo, xHigh)).toFixed(1)} `;
      }
      paths.push({ key: entry.key, d: d.trim(), color: entry.color,
                   faint: entry.faint, dash: entry.dash, width: entry.width });
    }

    // Direct end-labels: the latest value, at the right edge. Labels that
    // would collide are dropped rather than nudged off their lines -- the
    // legend and the tooltip still carry the series.
    const labels = [];
    for (const entry of live) {
      if (entry.mark === "scatter" || entry.endLabel === false || entry.faint) continue;
      const last = [...entry.points].reverse().find((p) => usableY(p[1]) && usableX(p[0]));
      if (!last) continue;
      // Only a series still reading at the right edge gets a label there: a
      // channel that went overrange ten minutes ago must not have its last
      // number quoted beside the axis as though it were current.
      const recent = Math.max(medianSpacing(entry.points) * 4, (xHigh - xLow) * 0.015);
      if (entry.mark !== "step" && !x.log && xHigh - last[0] > recent) continue;
      const yPos = Math.min(Math.max(sy(last[1]), top + 7), top + plotH);
      if (labels.some((l) => Math.abs(l.y - yPos) < 11)) continue;
      labels.push({ y: yPos, text: entry.endText ? entry.endText(last[1])
                                                 : formatValue(last[1]) });
    }

    const spacing = Math.max(...live.map((s) => medianSpacing(s.points) * 2), 0);

    return { plotW, plotH, xLow, xHigh, yLow, yHigh, sx, sy, invX, usableY,
             usableX, yTicks, xTicks, xStep, paths, dots, labels, spacing };
  }, [live, width, height, top, bottom, stripHeight, x.log, x.min, x.max, x.kind, x.floor,
      y.log, y.min, y.max, y.zero, y.minSpan, String(y.include), y.floor, gapFactor]);

  if (!geometry) {
    return html`<div ref=${ref}><div class="chart-empty">${emptyText}</div></div>`;
  }

  const g = geometry;
  const inside = (value) => value >= g.xLow && value <= g.xHigh;
  const span = g.xHigh - g.xLow;

  // --- the hover / shared-cursor overlay -------------------------------------

  /** The sample nearest a screen x, across every series: never a gap. */
  const snap = (px) => {
    const value = g.invX(px);
    let best = null;
    let target = null;
    for (const entry of live) {
      if (!entry.points.length) continue;
      const point = entry.points[nearestIndex(entry.points, value)];
      if (!point || !finite(point[1])) continue;
      const distance = Math.abs(point[0] - value);
      if (best === null || distance < best) { best = distance; target = point[0]; }
    }
    return target;
  };

  /** The scatter point nearest the pointer, within a generous radius. */
  const pick = (px, py) => {
    let best = null;
    for (const entry of live) {
      for (const point of entry.points) {
        if (!g.usableX(point[0]) || !g.usableY(point[1])) continue;
        const dx = g.sx(point[0]) - px;
        const dy = g.sy(point[1]) - py;
        const distance = dx * dx + dy * dy;
        if (distance < 28 * 28 && (!best || distance < best.distance)) {
          best = { distance, entry, point };
        }
      }
    }
    return best;
  };

  let hoverX = null;
  let tooltip = null;
  const marked = [];

  if (scatter) {
    let best = pointer ? pick(pointer.x, pointer.y) : null;
    if (!best && activeCursor !== null) {
      // Linked from a time chart: ring the sample taken at that moment.
      for (const entry of live) {
        for (const point of entry.points) {
          const t = point[2]?.t;
          if (!finite(t)) continue;
          const distance = Math.abs(t - activeCursor);
          if (!best || distance < best.distance) best = { distance, entry, point };
        }
      }
      if (best && best.distance > (best.point[2]?.tolerance ?? 30)) best = null;
    }
    if (best) {
      marked.push({ cx: g.sx(best.point[0]), cy: g.sy(best.point[1]),
                    color: best.entry.color });
      tooltip = {
        x: g.sx(best.point[0]),
        head: best.point[2]?.head || formatX(best.point[0], x.kind),
        rows: [{ color: best.entry.color, label: best.entry.label,
                 value: `${formatValue(best.point[1])}${unit ? ` ${unit}` : ""}` },
               ...(best.point[2]?.rows || [])],
      };
    }
  } else if (activeCursor !== null && inside(activeCursor)) {
    hoverX = activeCursor;
    const tolerance = Math.max(span / 60, g.spacing);
    const rows = live.map((entry) => {
      const value = valueAt(entry, hoverX, tolerance);
      if (g.usableY(value)) {
        marked.push({ cx: g.sx(hoverX), cy: g.sy(value), color: entry.color,
                      faint: entry.faint });
      }
      return {
        color: entry.color, label: entry.label, faint: entry.faint,
        value: g.usableY(value)
          ? `${formatValue(value)}${unit ? ` ${unit}` : ""}` : "no reading",
      };
    });
    const near = events.filter((e) => inside(e.x)
                                      && Math.abs(g.sx(e.x) - g.sx(hoverX)) < 5);
    // A point may carry its own description -- a run's number and kind on a
    // trend chart -- and that beats the bare x value as a heading.
    let meta = null;
    for (const entry of live) {
      const point = entry.points[nearestIndex(entry.points, hoverX)];
      if (point && point[0] === hoverX && point[2]) { meta = point[2]; break; }
    }
    tooltip = {
      x: g.sx(hoverX),
      head: meta?.head || formatX(hoverX, x.kind),
      rows: [...rows.filter((r) => !r.faint || r.value !== "no reading"),
             ...(meta?.rows || []),
             ...near.map((e) => ({ event: true, level: e.level, value: e.label }))],
    };
  }

  const setCursor = (next) => {
    if (synced) {
      if (next !== cursor) onCursor(next);
    } else {
      setLocalCursor(next);
    }
  };

  const onMove = (event) => {
    const box = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - box.left;
    const py = event.clientY - box.top;
    if (px < PAD.left - 4 || px > PAD.left + g.plotW + 4) return;
    if (scatter) {
      setPointer({ x: px, y: py });
      const best = pick(px, py);
      if (synced && best?.point[2]?.t != null) setCursor(best.point[2].t);
      return;
    }
    setCursor(snap(px));
  };

  const onLeave = () => {
    setPointer(null);
    setCursor(null);
  };

  // Keyboard: arrows walk the crosshair through the samples, so everything a
  // pointer can read is reachable without one.
  const onKey = (event) => {
    if (scatter || !live.length) return;
    const points = live[0].points.filter((p) => finite(p[1]));
    if (!points.length) return;
    let index = activeCursor === null ? points.length - 1
      : nearestIndex(points, activeCursor);
    if (event.key === "ArrowLeft") index = Math.max(0, index - 1);
    else if (event.key === "ArrowRight") index = Math.min(points.length - 1, index + 1);
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = points.length - 1;
    else if (event.key === "Escape") { setCursor(null); return; }
    else return;
    event.preventDefault();
    setCursor(points[index][0]);
  };

  const tooltipLeft = tooltip
    ? (tooltip.x + 14 + 190 > width ? Math.max(4, tooltip.x - 14 - 190)
                                    : tooltip.x + 14)
    : 0;
  const plotBottom = top + g.plotH;
  const tone = (t) => (t === "crit" ? "var(--crit)" : t === "warn" ? "var(--warn)"
    : t === "good" ? "var(--good)" : t === "accent" ? "var(--accent)" : "var(--ink-faint)");

  return html`
    <div class="chart-holder" ref=${ref}>
      <svg class="chart" width=${width} height=${height} tabindex="0"
           role="img"
           aria-label=${ariaLabel || title || `Chart: ${live.map((s) => s.label).join(", ")}`}
           onPointerMove=${onMove} onPointerLeave=${onLeave} onKeyDown=${onKey}>

        ${bands.filter((b) => b.axis === "y").map((band) => {
          const y1 = g.sy(Math.min(band.to, g.yHigh));
          const y2 = g.sy(Math.max(band.from, g.yLow));
          if (!(y2 - y1 > 0.5)) return null;
          return html`
            <g>
              <rect class="band" x=${PAD.left} y=${y1} width=${g.plotW} height=${y2 - y1} />
              ${band.label && html`<text class="band-label" x=${PAD.left + 6}
                                        y=${y1 + 11}>${band.label}</text>`}
            </g>`;
        })}

        ${phases.map((phase, index) => {
          const start = g.sx(Math.max(phase.from, g.xLow));
          const end = g.sx(Math.min(phase.to ?? g.xHigh, g.xHigh));
          if (!(end - start >= 1)) return null;
          const wide = end - start >= 34;
          return html`
            <g>
              ${index % 2 === 0 && html`<rect class="phase-band" x=${start} y=${top}
                                              width=${end - start} height=${g.plotH} />`}
              <line class="phase-edge" x1=${start} x2=${start} y1=${top - PHASE_ROW + 3}
                    y2=${plotBottom} />
              ${wide && html`<text class="phase-label" x=${start + 4} y=${top - 4}>
                ${truncate(phase.label, Math.floor((end - start - 6) / 6))}</text>`}
            </g>`;
        })}

        ${bands.filter((b) => b.axis === "x").map((band) => {
          const start = g.sx(Math.max(band.from, g.xLow));
          const end = g.sx(Math.min(band.to ?? g.xHigh, g.xHigh));
          if (!(end - start >= 1)) return null;
          return html`<rect class="band" x=${start} y=${top} width=${end - start}
                            height=${g.plotH} />`;
        })}

        ${g.yTicks.map((tick) => html`
          <g>
            <line class="grid-line" x1=${PAD.left} x2=${PAD.left + g.plotW}
                  y1=${g.sy(tick)} y2=${g.sy(tick)} />
            <text class="tick" x=${PAD.left - 7} y=${g.sy(tick) + 3.5} text-anchor="end">
              ${y.log ? `1e${Math.round(log10(tick))}` : (y.tickFormat || ((v) => formatTick(v)))(tick)}
            </text>
          </g>`)}

        ${g.xTicks.map((tick) => html`
          <g>
            <line class="grid-line v" x1=${g.sx(tick)} x2=${g.sx(tick)} y1=${top}
                  y2=${plotBottom} />
            ${xLabels && html`<text class="tick" x=${g.sx(tick)} y=${height - 7}
                                    text-anchor="middle">${formatTick(tick, x.kind || (x.log ? "pressure" : "number"), g.xStep)}</text>`}
          </g>`)}

        <line class="axis-line" x1=${PAD.left} x2=${PAD.left + g.plotW}
              y1=${plotBottom} y2=${plotBottom} />

        ${placeLabels(refs, g, inside, y.log).map((rule) => {
          if (rule.axis === "x") {
            if (!inside(rule.value)) return null;
            const px = g.sx(rule.value);
            return html`
              <g>
                <line class="ref-line" x1=${px} x2=${px} y1=${top} y2=${plotBottom}
                      stroke=${tone(rule.tone)} />
                ${rule.label && html`<text class="ref-label" x=${px + 4} y=${top + 10}>
                  ${rule.label}</text>`}
              </g>`;
          }
          const value = rule.value;
          if (!finite(value) || (y.log && value <= 0)) return null;
          if (value < g.yLow || value > g.yHigh) return null;
          const py = g.sy(value);
          const left = rule.place?.startsWith("left");
          const below = rule.place?.endsWith("below");
          return html`
            <g>
              <line class="ref-line" x1=${PAD.left} x2=${PAD.left + g.plotW} y1=${py}
                    y2=${py} stroke=${tone(rule.tone)} />
              ${rule.label && rule.place && html`
                <text class="ref-label" x=${left ? PAD.left + 5 : PAD.left + g.plotW - 4}
                      y=${below ? py + 11 : py - 4}
                      text-anchor=${left ? "start" : "end"}>${rule.label}</text>`}
            </g>`;
        })}

        ${g.paths.map((path) => html`
          <path class=${`series${path.faint ? " faint" : ""}`} d=${path.d}
                stroke=${path.faint ? "var(--context)" : `var(${path.color})`}
                stroke-dasharray=${path.dash || null}
                stroke-width=${path.width || null} />`)}

        ${g.dots.map((dot) => html`
          <circle class="dot" cx=${dot.cx} cy=${dot.cy} r="3.5"
                  fill=${`var(${dot.color})`} />`)}

        ${events.map((entry) => {
          if (!inside(entry.x)) return null;
          const px = g.sx(entry.x);
          const colour = entry.level === "error" ? "var(--crit)"
            : entry.level === "warning" ? "var(--warn)" : "var(--ink-faint)";
          return html`
            <g class="event">
              <line x1=${px} x2=${px} y1=${top} y2=${plotBottom} stroke=${colour} />
              <path d=${`M${px - 4},${top} L${px + 4},${top} L${px},${top + 6} Z`}
                    fill=${colour} />
            </g>`;
        })}

        ${g.labels.map((label) => html`
          <text class="end-label" x=${PAD.left + g.plotW + 6} y=${label.y + 3.5}>
            ${label.text}</text>`)}

        ${strips.map((strip, row) => {
          const y0 = plotBottom + 6 + row * (STRIP_ROW + STRIP_GAP);
          return html`
            <g class="strip">
              <text class="strip-label" x=${PAD.left - 7} y=${y0 + STRIP_ROW - 3}
                    text-anchor="end">${strip.label}</text>
              <rect class="strip-track" x=${PAD.left} y=${y0} width=${g.plotW}
                    height=${STRIP_ROW} />
              ${strip.segments.map((segment) => {
                const start = g.sx(Math.max(segment.from, g.xLow));
                const end = g.sx(Math.min(segment.to, g.xHigh));
                if (!(end - start > 0.3)) return null;
                return html`<rect class=${`strip-seg ${segment.tone || "idle"}`}
                                  x=${start} y=${y0} width=${Math.max(1, end - start)}
                                  height=${STRIP_ROW} />`;
              })}
            </g>`;
        })}

        ${hoverX !== null && html`
          <line class="crosshair" x1=${g.sx(hoverX)} x2=${g.sx(hoverX)} y1=${top}
                y2=${plotBottom + stripHeight} />`}

        ${marked.map((mark) => html`
          <circle class="marker" cx=${mark.cx} cy=${mark.cy} r=${scatter ? 6 : 4}
                  fill=${mark.faint ? "var(--context)" : `var(${mark.color})`} />`)}
      </svg>

      ${tooltip && html`
        <div class="tooltip" style=${`left:${tooltipLeft}px; top:${top}px`}>
          <div class="t-time">${tooltip.head}</div>
          ${tooltip.rows.map((row) => row.event
            ? html`<div class=${`t-event ${row.level || ""}`}>${row.value}</div>`
            : html`
              <div class="t-row">
                ${row.color
                  ? html`<span class=${`t-key${row.faint ? " faint" : ""}`}
                               style=${row.faint ? "" : `background:var(${row.color})`} />`
                  : html`<span class="t-key none" />`}
                <strong class="num">${row.value}</strong>
                <span class="t-label">${row.label}</span>
              </div>`)}
          ${hoverX !== null && strips.map((strip) => {
            const segment = strip.segments.find((s) => hoverX >= s.from && hoverX <= s.to);
            return segment ? html`<div class="t-row">
              <span class=${`t-key strip-key ${segment.tone || "idle"}`} />
              <strong>${segment.label}</strong>
              <span class="t-label">${strip.label}</span></div>` : null;
          })}
        </div>`}
    </div>`;
}

/**
 * Where each horizontal reference's label goes so no two overlap: above the
 * line at the right, then at the left, then below. A label with nowhere left
 * is dropped -- its line stays, and the chart's subtitle or hint names it.
 */
function placeLabels(refs, g, inside, log) {
  const taken = [];
  const clash = (side, y) => taken.some((t) => t.side === side && Math.abs(t.y - y) < 12);
  return refs.map((rule) => {
    if (rule.axis === "x" || !rule.label || !finite(rule.value)) return rule;
    if (log && rule.value <= 0) return rule;
    if (rule.value < g.yLow || rule.value > g.yHigh) return rule;
    const py = g.sy(rule.value);
    for (const place of ["right-above", "left-above", "right-below", "left-below"]) {
      const side = place.split("-")[0];
      const labelY = place.endsWith("below") ? py + 11 : py - 4;
      if (!clash(side, labelY)) {
        taken.push({ side, y: labelY });
        return { ...rule, place };
      }
    }
    return { ...rule, place: null };
  });
}

function truncate(text, characters) {
  const value = String(text || "");
  if (characters <= 1) return "";
  return value.length > characters ? `${value.slice(0, Math.max(1, characters - 1))}…` : value;
}

/**
 * A legend: line keys for lines, dots for scatter.
 * Present whenever two or more series share a chart, absent for one.
 */
export function Legend({ series, note = null }) {
  const live = series.filter((s) => s.points && s.points.some((p) => finite(p[1])));
  if (live.length < 2 && !note) return null;
  return html`
    <div class="legend">
      ${live.length >= 2 && live.map((entry) => html`
        <span class="item">
          <span class=${`key ${entry.mark === "scatter" ? "dot" : "line"}${entry.faint ? " faint" : ""}`}
                style=${entry.faint ? "" : `background:var(${entry.color})`} />
          ${entry.label}${entry.count != null ? html` <span class="muted">(${entry.count})</span>` : null}
        </span>`)}
      ${note && html`<span class="item muted">${note}</span>`}
    </div>`;
}

/**
 * The same numbers as the chart, as a table: the view that needs no colour
 * vision, no pointer and no hovering. Long series are thinned evenly.
 */
export function DataTable({ series, x = {}, unit = "", formatValue = (v) => fmt.number(v, 2),
                            limit = 240 }) {
  const live = series.filter((s) => s.points && s.points.some((p) => finite(p[1])));
  if (!live.length) return html`<div class="chart-empty">Nothing to tabulate.</div>`;
  const scatter = live.every((s) => s.mark === "scatter");
  if (scatter) {
    const rows = live.flatMap((entry) => entry.points
      .filter((p) => finite(p[1]))
      .map((p) => ({ entry, p })));
    const every = Math.max(1, Math.ceil(rows.length / limit));
    return html`
      <div class="table-view scroll-x">
        <table>
          <thead><tr><th>${x.title || "x"}</th><th>${unit || "value"}</th><th>Group</th>
                     <th>When</th></tr></thead>
          <tbody>
            ${rows.filter((_, i) => i % every === 0).map(({ entry, p }) => html`
              <tr>
                <td class="num">${formatX(p[0], x.kind)}</td>
                <td class="num">${formatValue(p[1])}</td>
                <td>${entry.label}</td>
                <td class="num">${p[2]?.t ? fmt.clock(p[2].t) : "—"}</td>
              </tr>`)}
          </tbody>
        </table>
        ${every > 1 && html`<p class="hint">Every ${every}th of ${rows.length} points.</p>`}
      </div>`;
  }
  const xs = [...new Set(live.flatMap((s) => s.points.map((p) => p[0])))]
    .filter(finite).sort((a, b) => a - b);
  const every = Math.max(1, Math.ceil(xs.length / limit));
  const picked = xs.filter((_, i) => i % every === 0 || i === xs.length - 1);
  const lookup = live.map((entry) => new Map(entry.points.map((p) => [p[0], p[1]])));
  return html`
    <div class="table-view scroll-x">
      <table>
        <thead><tr><th>${x.title || "Time"}</th>
          ${live.map((entry) => html`<th>${entry.label}${unit ? ` (${unit})` : ""}</th>`)}
        </tr></thead>
        <tbody>
          ${picked.map((value) => html`
            <tr>
              <td class="num">${formatX(value, x.kind)}</td>
              ${lookup.map((map) => {
                const v = map.get(value);
                return html`<td class="num">${finite(v) ? formatValue(v) : "—"}</td>`;
              })}
            </tr>`)}
        </tbody>
      </table>
      ${every > 1 && html`<p class="hint">Every ${every}th of ${xs.length} rows; the CSV
        export has them all.</p>`}
    </div>`;
}

/**
 * A titled chart with its table view behind a toggle. `readout` is the
 * figure shown beside the title -- usually the latest value -- so a chart in
 * a stack still answers "what is it now?" at a glance.
 */
export function ChartPanel({ title, subtitle, readout, children, table, legend, hint }) {
  const [showTable, setShowTable] = useState(false);
  return html`
    <section class="chart-panel">
      <header>
        <div class="chart-title">
          <h3>${title}</h3>
          ${subtitle && html`<span class="chart-sub">${subtitle}</span>`}
        </div>
        <div class="row tight">
          ${readout != null && html`<span class="readout num">${readout}</span>`}
          ${table && html`
            <button class="ghost tiny" aria-pressed=${showTable ? "true" : "false"}
                    onClick=${() => setShowTable(!showTable)}>
              ${showTable ? "Chart" : "Table"}</button>`}
        </div>
      </header>
      ${showTable && table ? table() : children}
      ${!showTable && legend}
      ${hint && html`<p class="hint">${hint}</p>`}
    </section>`;
}

/**
 * A sparkline: shape only, no axes, no labels.
 *
 * It sits behind a stat panel's number to answer "and which way is it going?"
 * -- the one question a bare number cannot. It is never the only place a
 * value appears, so it carries no ticks and needs none.
 */
export function Sparkline({ values, color = "--series-1", log = false, height = 40 }) {
  const clean = values.filter((v) => Number.isFinite(v) && (!log || v > 0));
  if (clean.length < 2) return null;

  const scaled = log ? clean.map(log10) : clean;
  const low = Math.min(...scaled);
  const high = Math.max(...scaled);
  const spread = high - low || 1;
  const width = 100;                       // viewBox units; CSS sizes it

  const points = scaled.map((value, index) => {
    const px = (index / (scaled.length - 1)) * width;
    const py = height - ((value - low) / spread) * (height - 4) - 2;
    return `${px.toFixed(2)},${py.toFixed(2)}`;
  });

  return html`
    <svg class="sparkline" viewBox=${`0 0 ${width} ${height}`}
         preserveAspectRatio="none" aria-hidden="true">
      <polyline points=${points.join(" ")} fill="none"
                stroke=${`var(${color})`} stroke-width="1.5"
                vector-effect="non-scaling-stroke"
                stroke-linejoin="round" stroke-linecap="round" />
    </svg>`;
}
