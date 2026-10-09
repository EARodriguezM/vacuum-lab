"""Turning stored runs into files somebody can take away.

Four shapes, because they answer different questions:

* **CSV** -- one run's curve, for a spreadsheet or a plotting script.
* **JSON** -- one run complete: header, configuration, result, events, curve.
  This is the archival form; it round-trips everything the database holds.
* **Index CSV** -- every run as one row, for "which test was that again?".
* **SQLite** -- the whole database, taken as a proper online backup.

Numbers are written at full precision rather than display precision. A
pump-down curve re-analysed later must not be limited by how many digits the
web page happened to show.
"""

from __future__ import annotations

import csv
import io
import json
import time

from . import db as dbmod

#: Columns of a per-run CSV, in order.
SAMPLE_HEADER = (
    "t_s", "timestamp", "iso_time", "phase",
    "p1_mbar", "p1_status", "p2_mbar", "p2_status", "p_best_mbar",
    "speed_hz", "set_speed_hz", "current_a", "voltage_v", "power_w",
    "tms_temp_c", "error_code", "station", "motor",
    "ov_temp_pump", "ov_temp_elec",
    "argon_sccm", "gate_pct", "magnetron_w", "shutter_open", "rate_pct_min",
)

#: Columns of the all-runs index CSV.
INDEX_HEADER = (
    "id", "uuid", "procedure", "name", "operator", "status",
    "started_iso", "ended_iso", "duration_s", "samples", "result", "notes",
)


def _iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def _slug(text: str) -> str:
    keep = [c if c.isalnum() or c in "-_" else "-" for c in text.strip()]
    return "".join(keep).strip("-").lower() or "run"


def filename(run: dict, extension: str) -> str:
    """A filename that sorts chronologically and says what it holds."""
    stamp = time.strftime("%Y%m%d-%H%M", time.localtime(run["started_at"]))
    return f"{stamp}-{run['procedure']}-{_slug(run['name'])}-{run['id']}.{extension}"


def run_csv(database: "dbmod.Database", run_id: int) -> tuple[str, str] | None:
    """One run's curve as CSV. Returns (filename, text)."""
    run = database.run(run_id)
    if not run:
        return None
    buffer = io.StringIO()

    # A comment block keeps the file self-describing once it has been emailed
    # around and separated from the database it came from. These lines are
    # written straight to the buffer rather than through the csv writer: the
    # config and result are JSON, and the writer would see the quotes in them
    # as needing escaping and wrap the whole line in quotes of its own,
    # putting a `"` before the `#` and breaking the comment convention.
    comments = [
        f"# {run['name']} -- {run['procedure']}",
        f"# run {run['id']} ({run['uuid']})",
        f"# started {_iso(run['started_at'])}"
        f"  ended {_iso(run.get('ended_at'))}"
        f"  status {run['status']}",
    ]
    if run.get("operator"):
        comments.append(f"# operator {run['operator']}")
    comments.append(f"# config {json.dumps(run['config'], sort_keys=True)}")
    if run.get("result"):
        comments.append(f"# result {json.dumps(run['result'], sort_keys=True)}")
    if run.get("notes"):
        # Newlines in a note would otherwise become uncommented lines.
        comments.append(f"# notes {run['notes'].replace(chr(10), ' ')}")
    buffer.write("\n".join(comments) + "\n\n")

    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SAMPLE_HEADER)

    for sample in database.samples(run_id):
        writer.writerow([
            sample.get("t"), sample.get("at"), _iso(sample.get("at")),
            sample.get("phase") or "",
            sample.get("p1"), sample.get("p1_status"),
            sample.get("p2"), sample.get("p2_status"),
            sample.get("p_best"),
            sample.get("speed"), sample.get("set_speed"),
            sample.get("current"), sample.get("voltage"), sample.get("power"),
            sample.get("tms_temp"), sample.get("error_code") or "",
            sample.get("station"), sample.get("motor"),
            sample.get("ov_temp_pump"), sample.get("ov_temp_elec"),
            sample.get("gas_sccm"), sample.get("gate_pct"),
            sample.get("magnetron_w"), sample.get("shutter"),
            sample.get("rate_pct_min"),
        ])
    return filename(run, "csv"), buffer.getvalue()


def run_json(database: "dbmod.Database", run_id: int) -> tuple[str, str] | None:
    """One run, complete, as JSON."""
    run = database.run(run_id)
    if not run:
        return None
    payload = {
        "run": run,
        "started_iso": _iso(run["started_at"]),
        "ended_iso": _iso(run.get("ended_at")),
        "samples": database.samples(run_id),
        "events": list(reversed(database.events(run_id, limit=10000))),
        "exported_at": time.time(),
        "exported_iso": _iso(time.time()),
        "schema_version": dbmod.SCHEMA_VERSION,
    }
    return filename(run, "json"), json.dumps(payload, indent=2)


def index_csv(database: "dbmod.Database") -> tuple[str, str]:
    """Every run as one row."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(INDEX_HEADER)
    for run in database.runs(limit=100000):
        writer.writerow([
            run["id"], run["uuid"], run["procedure"], run["name"],
            run.get("operator", ""), run["status"],
            _iso(run["started_at"]), _iso(run.get("ended_at")),
            round(run["duration"], 1) if run.get("duration") else "",
            run.get("sample_count", ""),
            json.dumps(run.get("result") or {}, sort_keys=True),
            (run.get("notes") or "").replace("\n", " "),
        ])
    stamp = time.strftime("%Y%m%d-%H%M")
    return f"{stamp}-all-runs.csv", buffer.getvalue()


def index_json(database: "dbmod.Database") -> tuple[str, str]:
    """Every run's header and result, without the curves."""
    stamp = time.strftime("%Y%m%d-%H%M")
    payload = {
        "runs": database.runs(limit=100000),
        "stats": database.stats(),
        "exported_at": time.time(),
        "exported_iso": _iso(time.time()),
        "schema_version": dbmod.SCHEMA_VERSION,
    }
    return f"{stamp}-all-runs.json", json.dumps(payload, indent=2)


def bundle_json(database: "dbmod.Database") -> tuple[str, str]:
    """Every run *with* its curve. Large; the honest full-archive option."""
    runs = []
    for run in database.runs(limit=100000):
        runs.append({
            **run,
            "samples": database.samples(run["id"]),
            "events": list(reversed(database.events(run["id"], limit=10000))),
        })
    stamp = time.strftime("%Y%m%d-%H%M")
    payload = {
        "runs": runs,
        "stats": database.stats(),
        "exported_at": time.time(),
        "exported_iso": _iso(time.time()),
        "schema_version": dbmod.SCHEMA_VERSION,
    }
    return f"{stamp}-vacuum-lab-archive.json", json.dumps(payload, indent=2)


def database_snapshot(database: "dbmod.Database", directory: str) -> tuple[str, str]:
    """A consistent copy of the .db file. Returns (filename, path on disk)."""
    import os

    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-vacuum-lab.db"
    path = os.path.join(directory, name)
    database.snapshot(path)
    return name, path
