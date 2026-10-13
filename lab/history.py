"""A rolling record of the whole rig, kept whether or not a run is recording.

The run engine only samples while a procedure is running, so before this the
pump and gauge pages had nothing to plot: a chamber left pumping overnight, a
rotor coasting down after a trip, a pressure creeping up while nobody had
started a run -- none of it was kept anywhere. This keeps the last day of it.

Rows have the same columns as a run sample, so the panel draws both with the
same charts. Nothing here touches a serial port: like every other reader, the
recorder takes the cached snapshot each device service already publishes.

It lives in memory and is lost on a restart, on purpose. Runs are the durable
record and go to SQLite; this is the instrument's chart recorder, and a day of
it at a 2 s cadence is 43 200 rows -- fine in memory, and not worth growing
the database by forever.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from . import logs

#: Columns kept for every row, in order. `at` is the wall-clock time; the
#: rest match the run-sample columns of the same name.
FIELDS = (
    "at", "p1", "p1_status", "p2", "p2_status", "p_best",
    "speed", "set_speed", "current", "voltage", "power", "tms_temp",
    "station", "motor", "ov_temp_pump", "ov_temp_elec", "error_code",
    "gas_sccm", "gate_pct", "magnetron_w", "shutter",
)

#: The columns whose extremes decimation must never drop. These are the ones
#: a fault shows up in: a pressure step, a rotor dragged down, a drive at its
#: power limit. Everything else rides along on the rows they select.
SHAPE_KEYS = ("p1", "p2", "speed", "power")


def decimate(count: int, points: int, columns: list[list]) -> list[int]:
    """Indices of the rows to keep so a chart of them looks like one of all.

    Taking every Nth row is how a spike disappears: the pressure step that
    tripped the pump lasts two samples, and a stride of thirty steps right
    over it. Instead the rows are split into buckets and each bucket keeps
    its first and last row plus, for every column given, the rows holding
    its minimum and maximum. Lines drawn through those are, to the pixel,
    the lines drawn through everything -- which is the whole requirement.

    Returns about `points` indices, in order; never fewer rows than needed
    to show every extreme, so it can exceed `points` slightly.
    """
    if count <= points or points <= 0:
        return list(range(count))
    per_bucket = 2 + 2 * max(1, len(columns))
    buckets = max(1, points // per_bucket)
    size = count / buckets
    keep: set[int] = set()
    for bucket in range(buckets):
        start = int(bucket * size)
        end = min(count, int((bucket + 1) * size)) or start + 1
        if start >= end:
            continue
        keep.add(start)
        keep.add(end - 1)
        for column in columns:
            low = high = None
            for index in range(start, end):
                value = column[index]
                if not isinstance(value, (int, float)):
                    continue
                if low is None or value < column[low]:
                    low = index
                if high is None or value > column[high]:
                    high = index
            if low is not None:
                keep.add(low)
                keep.add(high)
    keep.add(count - 1)          # the newest row is always the one on screen
    return sorted(keep)


class History:
    """Samples the registry on a fixed cadence into a bounded ring buffer."""

    def __init__(self, registry, hardware=None, gauge_key: str = "gauge",
                 interval: float = 2.0, span_s: float = 86400.0):
        self.registry = registry
        self.hardware = hardware
        self.gauge_key = gauge_key
        self.interval = interval
        self.span_s = span_s
        self._rows: deque = deque(maxlen=int(span_s / interval) + 1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: When the loop last went round (monotonic), for the supervisor.
        self.beat: float | None = None

    # --- recording ----------------------------------------------------------

    def start(self) -> "History":
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="history",
                                            daemon=True)
            self._thread.start()
        return self

    def close(self) -> None:
        self._stop.set()

    def beat_age(self) -> float | None:
        return None if self.beat is None else time.monotonic() - self.beat

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.beat = time.monotonic()
            try:
                self.record()
            except Exception as exc:          # never kill the recorder --
                logs.error("history", exc)    # but say why it is missing rows
            self._stop.wait(self.interval)

    def record(self, now: float | None = None) -> dict:
        """Take one row from the cached snapshots, and keep it."""
        row = self.registry.sample_row()
        if self.hardware is not None:
            row.update(self.hardware.sample_fields())
        gauge = (self.registry.get(self.gauge_key)
                 if self.gauge_key in self.registry else None)
        row["p_best"] = gauge.best_pressure() if gauge else None
        row["at"] = time.time() if now is None else now
        with self._lock:
            self._rows.append(tuple(row.get(name) for name in FIELDS))
        return row

    # --- reading ------------------------------------------------------------

    def __len__(self) -> int:
        with self._lock:
            return len(self._rows)

    def window(self, seconds: float | None = None, points: int = 1500,
               since: float | None = None) -> dict:
        """Rows for the panel: a decimated window, or everything newer.

        `since` is how a chart that already holds a window keeps up: it asks
        for the rows after the last one it has, which at a 2 s poll is one
        row, rather than re-fetching an hour of them every two seconds.
        Those come back undecimated; the window itself is decimated to about
        `points` rows.
        """
        with self._lock:
            rows = list(self._rows)
        now = time.time()
        if since is not None:
            selected = [r for r in rows if r[0] > since]
            if len(selected) > max(points, 1):
                selected = selected[-points:]
        else:
            start = now - seconds if seconds else None
            selected = [r for r in rows if start is None or r[0] >= start]
            if len(selected) > points:
                index = {name: FIELDS.index(name) for name in SHAPE_KEYS}
                columns = [[r[index[name]] for r in selected]
                           for name in SHAPE_KEYS]
                selected = [selected[i] for i in
                            decimate(len(selected), points, columns)]
        return {
            "ok": True,
            "fields": list(FIELDS),
            "rows": [list(r) for r in selected],
            "interval_s": self.interval,
            "span_s": self.span_s,
            "oldest": rows[0][0] if rows else None,
            "newest": rows[-1][0] if rows else None,
            "server_time": now,
        }
