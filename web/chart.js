// SVG charts, hand-rolled.
//
// No chart library: the lab PC is offline, and what these views need is one
// chart form -- a time series -- done properly.
//
// Two rules from the visualisation guidance shape this file:
//
//   * **Never two y-axes.** Pressure spans fourteen decades, rotor speed spans
//     one, temperature spans two. Putting any two of them on shared axes makes
//     a picture that reads as a correlation that is not there. So each measure
//     gets its own chart, stacked, sharing an x-axis by alignment.
//   * **Aqua is below the 3:1 contrast bar on the light surface**, so any
//     chart drawn in it ships a direct end-label with the current value. That
//     label is the required relief, not decoration -- do not remove it.
//
// Pressure is drawn on a log scale, because a pump-down that is a featureless
// cliff on a linear axis is a straight line with a knee on a log one, and the
// knee is the whole diagnostic.

import { html } from "./html.js";
import { useEffect, useLayoutEffect, useRef, useState } from "preact/hooks";
import * as fmt from "./format.js";

const PADDING = { top: 12, right: 54, bottom: 26, left: 52 };

/** Track a container's pixel width so text stays crisp instead of scaling. */
function useWidth() {
  const ref = useRef(null);
  const [width, setWidth] = useState(680);
  useLayoutEffect(() => {
    const node = ref.current;
    if (!node) return;
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

const log10 = (value) => Math.log(value) / Math.LN10;

/** Decade ticks spanning the data, so a log axis reads as powers of ten. */
function decadeTicks(low, high) {
  const first = Math.floor(log10(low));
  const last = Math.ceil(log10(high));
  const ticks = [];
  const stride = Math.max(1, Math.ceil((last - first) / 8));
  for (let power = first; power <= last; power += stride) ticks.push(10 ** power);
  return ticks;
}

function niceTicks(low, high, count = 5) {
  if (!(high > low)) return [low];
  const raw = (high - low) / count;
  const magnitude = 10 ** Math.floor(log10(raw));
  const step = [1, 2, 2.5, 5, 10].find((s) => s * magnitude >= raw) * magnitude;
  const ticks = [];
  for (let value = Math.ceil(low / step) * step; value <= high; value += step) {
    ticks.push(Number(value.toFixed(10)));
  }
  return ticks.length ? ticks : [low, high];
}

/**
 * A time-series chart.
 *
 * series: [{ key, label, color, points: [[x, y], ...] }]
 *   `color` is a CSS custom property name such as "--series-1".
 */
export function TimeChart({
  series = [], height = 200, log = false, unit = "",
  formatValue = (v) => fmt.number(v, 1), emptyText = "No data yet.",
  phases = [], xMax = null,
}) {
  const [ref, width] = useWidth();
  const [hover, setHover] = useState(null);

  const live = series.filter((s) => s.points && s.points.length);
  const points = live.flatMap((s) => s.points);

  if (!points.length) {
    return html`<div ref=${ref}><div class="chart-empty">${emptyText}</div></div>`;
  }

  const plotWidth = Math.max(40, width - PADDING.left - PADDING.right);
  const plotHeight = Math.max(40, height - PADDING.top - PADDING.bottom);

  const xLow = 0;
  const xHigh = Math.max(xMax ?? 0, ...points.map((p) => p[0]), 1);

  let yValues = points.map((p) => p[1]).filter((v) => Number.isFinite(v) && (!log || v > 0));
  if (!yValues.length) {
    return html`<div ref=${ref}><div class="chart-empty">${emptyText}</div></div>`;
  }
  let yLow = Math.min(...yValues);
  let yHigh = Math.max(...yValues);

  if (log) {
    // Round out to whole decades so the gridlines land on powers of ten.
    yLow = 10 ** Math.floor(log10(yLow));
    yHigh = 10 ** Math.ceil(log10(yHigh));
    if (yLow === yHigh) yHigh = yLow * 10;
  } else {
    const span = yHigh - yLow;
    const margin = span === 0 ? Math.max(1, Math.abs(yHigh) * 0.1) : span * 0.1;
    yLow -= margin;
    yHigh += margin;
    if (yLow > 0 && yLow < (yHigh - yLow)) yLow = 0;   // keep a real zero base
  }

  const xScale = (value) =>
    PADDING.left + (xHigh === xLow ? 0 : ((value - xLow) / (xHigh - xLow)) * plotWidth);
  const yScale = (value) => {
    const fraction = log
      ? (log10(value) - log10(yLow)) / (log10(yHigh) - log10(yLow))
      : (value - yLow) / (yHigh - yLow);
    return PADDING.top + plotHeight - Math.min(1, Math.max(0, fraction)) * plotHeight;
  };

  const yTicks = log ? decadeTicks(yLow, yHigh) : niceTicks(yLow, yHigh, 4);
  const xTicks = niceTicks(xLow, xHigh, Math.max(2, Math.floor(plotWidth / 90)));

  const path = (entries) => {
    const usable = entries.filter(
      ([, y]) => Number.isFinite(y) && (!log || y > 0)
    );
    if (!usable.length) return "";
    return usable
      .map(([x, y], index) => `${index ? "L" : "M"}${xScale(x).toFixed(1)},${yScale(y).toFixed(1)}`)
      .join(" ");
  };

  // Hover resolves to the nearest sample of the first series that has one, so
  // the crosshair snaps to real data instead of floating between points.
  const onMove = (event) => {
    const box = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - box.left;
    const value = xLow + ((x - PADDING.left) / plotWidth) * (xHigh - xLow);
    let best = null;
    for (const entry of live) {
      for (const [px, py] of entry.points) {
        const distance = Math.abs(px - value);
        if (!best || distance < best.distance) {
          best = { distance, x: px, rows: [], py };
        }
      }
    }
    if (!best) return;
    best.rows = live.map((entry) => {
      const match = entry.points.reduce(
        (closest, point) =>
          Math.abs(point[0] - best.x) < Math.abs(closest[0] - best.x) ? point : closest,
        entry.points[0]
      );
      return { label: entry.label, color: entry.color, value: match[1] };
    });
    setHover(best);
  };

  const tooltipLeft = hover
    ? Math.min(Math.max(xScale(hover.x) + 12, 8), Math.max(8, width - 168))
    : 0;

  return html`
    <div class="chart-holder" ref=${ref}>
      <svg class="chart" width=${width} height=${height}
           role="img" aria-label=${`Time series: ${live.map((s) => s.label).join(", ")}`}
           onMouseMove=${onMove} onMouseLeave=${() => setHover(null)}>

        ${phases.map((phase) => {
          const start = xScale(phase.from);
          const end = xScale(Math.min(phase.to ?? xHigh, xHigh));
          if (end - start < 2) return null;
          return html`<rect class="phase-band" x=${start} y=${PADDING.top}
                            width=${end - start} height=${plotHeight}
                            fill="var(--ink)" />`;
        })}

        ${yTicks.map((tick) => {
          const y = yScale(tick);
          if (y < PADDING.top - 1 || y > PADDING.top + plotHeight + 1) return null;
          return html`
            <g>
              <line class="grid-line" x1=${PADDING.left} x2=${PADDING.left + plotWidth}
                    y1=${y} y2=${y} />
              <text class="tick" x=${PADDING.left - 7} y=${y + 3.5} text-anchor="end">
                ${log ? `1e${Math.round(log10(tick))}` : fmt.number(tick, tick >= 10 ? 0 : 1)}
              </text>
            </g>`;
        })}

        ${xTicks.map((tick) => html`
          <text class="tick" x=${xScale(tick)} y=${height - 8} text-anchor="middle">
            ${fmt.elapsed(tick)}
          </text>`)}

        <line class="axis-line" x1=${PADDING.left} x2=${PADDING.left + plotWidth}
              y1=${PADDING.top + plotHeight} y2=${PADDING.top + plotHeight} />

        ${live.map((entry) => html`
          <path class="series" d=${path(entry.points)}
                stroke=${`var(${entry.color})`} />`)}

        ${live.map((entry) => {
          // The direct end-label: current value, in the series colour, at the
          // right edge. This is what makes a single-series chart legible
          // without a legend, and is the contrast relief for aqua.
          const last = [...entry.points].reverse()
            .find(([, y]) => Number.isFinite(y) && (!log || y > 0));
          if (!last) return null;
          const y = Math.min(Math.max(yScale(last[1]), PADDING.top + 8),
                             PADDING.top + plotHeight);
          return html`
            <text class="end-label" x=${PADDING.left + plotWidth + 6} y=${y + 3.5}
                  fill=${`var(${entry.color})`}>${formatValue(last[1])}</text>`;
        })}

        ${hover && html`
          <g>
            <line class="crosshair" x1=${xScale(hover.x)} x2=${xScale(hover.x)}
                  y1=${PADDING.top} y2=${PADDING.top + plotHeight} />
            ${hover.rows.map((row) =>
              Number.isFinite(row.value) && (!log || row.value > 0)
                ? html`<circle class="marker" cx=${xScale(hover.x)} cy=${yScale(row.value)}
                               r="4" fill=${`var(${row.color})`} />`
                : null)}
          </g>`}
      </svg>

      ${hover && html`
        <div class="tooltip" style=${`left:${tooltipLeft}px; top:6px`}>
          <div class="t-time">t + ${fmt.duration(hover.x)}</div>
          ${hover.rows.map((row) => html`
            <div class="t-row">
              <span><span class="swatch" style=${
                `display:inline-block;width:9px;height:9px;border-radius:2px;` +
                `margin-right:5px;background:var(${row.color})`} />${row.label}</span>
              <span class="num">${formatValue(row.value)}${unit ? ` ${unit}` : ""}</span>
            </div>`)}
        </div>`}
    </div>`;
}

/** Legend. Present whenever two or more series share a chart, absent for one. */
export function Legend({ series }) {
  const live = series.filter((s) => s.points && s.points.length);
  if (live.length < 2) return null;
  return html`
    <div class="legend">
      ${live.map((entry) => html`
        <span class="item">
          <span class="swatch" style=${`background:var(${entry.color})`} />
          ${entry.label}
        </span>`)}
    </div>`;
}

/**
 * Pressure against time, log scale. The rig's primary chart.
 */
export function PressureChart({ samples, height = 220, xMax = null }) {
  const series = [
    {
      key: "p1", label: "Channel 1", color: "--series-1",
      points: samples.filter((s) => s.p1 > 0).map((s) => [s.t, s.p1]),
    },
    {
      key: "p2", label: "Channel 2", color: "--series-2",
      points: samples.filter((s) => s.p2 > 0).map((s) => [s.t, s.p2]),
    },
  ];
  return html`
    <div>
      <${TimeChart} series=${series} height=${height} log=${true} unit="mbar"
                    xMax=${xMax}
                    formatValue=${(v) => fmt.pressure(v, 2)}
                    emptyText="No valid pressure readings yet. Check that a gauge is switched on and in range." />
      <${Legend} series=${series} />
    </div>`;
}

/** Rotor speed against time. Single series, so no legend -- the title names it. */
export function SpeedChart({ samples, height = 150, xMax = null }) {
  const series = [{
    key: "speed", label: "Rotor speed", color: "--series-3",
    points: samples.filter((s) => Number.isFinite(s.speed)).map((s) => [s.t, s.speed]),
  }];
  return html`<${TimeChart} series=${series} height=${height} unit="Hz" xMax=${xMax}
                            formatValue=${(v) => fmt.integer(v)}
                            emptyText="No speed readings yet." />`;
}

/** TMS temperature against time. Single series, its own chart. */
export function TemperatureChart({ samples, height = 150, xMax = null }) {
  const series = [{
    key: "tms", label: "TMS temperature", color: "--series-3",
    points: samples.filter((s) => Number.isFinite(s.tms_temp)).map((s) => [s.t, s.tms_temp]),
  }];
  return html`<${TimeChart} series=${series} height=${height} unit="°C" xMax=${xMax}
                            formatValue=${(v) => `${fmt.integer(v)} °C`}
                            emptyText="No temperature readings yet." />`;
}
