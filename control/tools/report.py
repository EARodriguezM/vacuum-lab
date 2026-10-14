#!/usr/bin/env python3
"""What happened on the rig since a given time, for whoever looks first.

    ./tools/report.py                    the last 24 hours
    ./tools/report.py --since 7d         the last week
    ./tools/report.py --since 2026-10-12 since that day (or a time, 12:30)

Read straight from the database, read-only, beside the running panel: the
runs that started or ended, the alarms raised (critical first), interlock
overrides, the actions people took, clean-ups still owed, and the backups.
Plain text, so it can go to a terminal, a cron mail or a chat. Exits 1 when
something needs a person -- a critical alarm, an owed clean-up, a run cut
off, or no good backup in 48 h -- and 0 otherwise.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

DEFAULT_DB = os.path.expanduser("~/.local/share/vacuum-lab/lab.db")
BACKUP_MAX_AGE_H = 48.0


def parse_since(text: str, now: float) -> float:
    """'24h', '7d', '90m', a date, a date and time, or a time today."""
    text = text.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([mhd])", text)
    if match:
        unit = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
        return now - float(match.group(1)) * unit
    if re.fullmatch(r"\d{1,2}:\d{2}", text):
        hour, minute = map(int, text.split(":"))
        today = dt.datetime.fromtimestamp(now).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if today.timestamp() > now:
            today -= dt.timedelta(days=1)
        return today.timestamp()
    for pattern in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, pattern).timestamp()
        except ValueError:
            continue
    raise ValueError(f"cannot read {text!r} as a time: try 24h, 7d, "
                     f"2026-10-12 or 08:00")


def _open(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro",
                                 uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _json(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {}


def _when(at: float | None) -> str:
    if not at:
        return "?"
    return time.strftime("%d %b %H:%M", time.localtime(at))


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"


def _result_line(procedure: str, result: dict) -> str:
    """The one number each kind of run is for."""
    parts = []
    if isinstance(result.get("base_mbar"), (int, float)):
        parts.append(f"base {result['base_mbar']:.2E} mbar")
    if isinstance(result.get("leak_rate_mbar_l_s"), (int, float)):
        parts.append(f"leak {result['leak_rate_mbar_l_s']:.2E} mbar·L/s")
    if isinstance(result.get("ultimate_mbar"), (int, float)):
        parts.append(f"ultimate {result['ultimate_mbar']:.2E} mbar")
    if result.get("pump_trouble"):
        parts.append("ended for the pump's sake")
    if result.get("cleanup_pending"):
        parts.append("CLEAN-UP OWED")
    if result.get("cut_off"):
        parts.append("cut off by a crash or power cut")
    if result.get("caveat"):
        parts.append(str(result["caveat"])[:90])
    if result.get("message") and not parts:
        parts.append(str(result["message"])[:90])
    return "; ".join(parts)


def report(db: Path, since: float, now: float | None = None) -> tuple[str, bool]:
    """(the report's text, whether something needs a person)."""
    now = time.time() if now is None else now
    lines: list[str] = []
    attention = False
    say = lines.append
    say(f"Vacuum lab -- {db}")
    say(f"From {_when(since)} to {_when(now)}")

    connection = _open(db)
    try:
        runs = connection.execute(
            "SELECT * FROM runs WHERE started_at >= ? OR ended_at >= ? "
            "OR status = 'running' ORDER BY started_at", (since, since)).fetchall()
        events = connection.execute(
            "SELECT * FROM events WHERE at >= ? ORDER BY at", (since,)).fetchall()
        owed = [r for r in connection.execute(
            "SELECT * FROM runs WHERE result LIKE '%cleanup_pending%' ORDER BY id")
            if _json(r["result"]).get("cleanup_pending") is True]
    finally:
        connection.close()

    say("")
    say(f"Runs ({len(runs)})")
    if not runs:
        say("  none")
    for run in runs:
        result = _json(run["result"])
        took = (run["ended_at"] or now) - run["started_at"]
        say(f"  #{run['id']} {run['name'] or run['procedure']} "
            f"[{run['procedure']}] {run['status']}, {_when(run['started_at'])}, "
            f"{_duration(took)}" + (f" -- by {run['started_by']}"
                                    if run["started_by"] else ""))
        detail = _result_line(run["procedure"], result)
        if detail:
            say(f"      {detail}")
        if result.get("cut_off"):
            attention = True

    alarms = [e for e in events if e["message"].startswith("ALARM:")]
    critical = [e for e in alarms if e["level"] == "error"]
    say("")
    say(f"Alarms ({len(alarms)}, {len(critical)} critical)")
    if not alarms:
        say("  none")
    for event in sorted(alarms, key=lambda e: (e["level"] != "error", e["at"])):
        mark = "CRITICAL" if event["level"] == "error" else "warning "
        say(f"  {mark} {_when(event['at'])}  {event['message'][7:][:140]}")
    attention = attention or bool(critical)

    overrides = [e for e in events if "interlock overridden" in e["message"]]
    say("")
    say(f"Interlock overrides ({len(overrides)})")
    if not overrides:
        say("  none")
    for event in overrides:
        detail = _json(event["detail"])
        say(f"  {_when(event['at'])}  {event['message'][:120]}")
        if detail.get("reason"):
            say(f"      reason: {str(detail['reason'])[:120]}")

    actions = [e for e in events if e["level"] == "action"
               and "interlock overridden" not in e["message"]]
    say("")
    say(f"Actions by people and the panel ({len(actions)})")
    for event in actions[-15:]:
        who = f" ({event['user']})" if event["user"] else ""
        say(f"  {_when(event['at'])}  {event['message'][:120]}{who}")
    if len(actions) > 15:
        say(f"  ... and {len(actions) - 15} earlier")
    if not actions:
        say("  none")

    say("")
    say(f"Clean-ups still owed ({len(owed)})")
    if not owed:
        say("  none")
    for run in owed:
        say(f"  #{run['id']} {run['name'] or run['procedure']} "
            f"[{run['procedure']}], ended {_when(run['ended_at'])}")
    attention = attention or bool(owed)

    say("")
    say("Backups")
    status_file = db.parent / "backup-status.json"
    try:
        status = json.loads(status_file.read_text())
    except (OSError, ValueError):
        status = None
    if not status:
        say("  no backup has been recorded for this database")
        attention = True
    else:
        success = status.get("last_success")
        say("  last attempt " + _when(status.get("last_attempt")) + ": "
            + ("worked" if status.get("last_ok") else
               f"FAILED -- {status.get('last_error')}"))
        say("  last good backup: " + (f"{_when(success)} "
                                      f"({_duration(now - success)} ago)"
                                      if success else "never"))
        if not success or now - success > BACKUP_MAX_AGE_H * 3600 \
                or not status.get("last_ok"):
            attention = True

    say("")
    say("Needs a person: " + ("YES -- see above" if attention else "no"))
    return "\n".join(lines), attention


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--since", default="24h")
    args = parser.parse_args(argv)
    now = time.time()
    try:
        since = parse_since(args.since, now)
    except ValueError as exc:
        parser.error(str(exc))
    db = Path(args.db).expanduser()
    if not db.exists():
        print(f"report: no database at {db}", file=sys.stderr)
        return 2
    try:
        text, attention = report(db, since, now)
    except sqlite3.Error as exc:
        print(f"report: could not read {db}: {exc}", file=sys.stderr)
        return 2
    print(text)
    return 1 if attention else 0


if __name__ == "__main__":
    raise SystemExit(main())
