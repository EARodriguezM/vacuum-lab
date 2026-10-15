"""Errors the panel's own threads run into: logged, once each, never lost.

The supervisor, the chart recorder and the audit records must not die of an
exception -- a dead watcher protects nothing -- so each catches everything.
They used to catch it and say nothing, which hid a bug for as long as it
kept happening. Now the first time each kind of error happens it goes to
stderr with its traceback; later ones of the same kind are counted, and
said again at the 10th, 100th and 1000th time, so a failure that repeats
every second does not flood the journal and one that repeats is not lost.

Each line starts with a syslog priority (`<3>` error, `<4>` warning), which
journald reads as the entry's priority: `journalctl -u vacuum-lab-web -p err`
shows them and nothing else.
"""

from __future__ import annotations

import sys
import threading
import traceback

ERROR, WARNING = 3, 4

_lock = threading.Lock()
_counts: dict[str, int] = {}
#: Where each kind was first met, for the health block of the state poll.
_first: dict[str, str] = {}


def _key(where: str, exc: BaseException) -> str:
    """The kind of error: where it was met, its type and the line that
    raised it -- not its message, which may differ every time (a value, a
    time) and would make every occurrence look new."""
    frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
    origin = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else "?"
    return f"{where}: {type(exc).__name__} at {origin}"


def _describe(exc: BaseException) -> str:
    try:
        return str(exc)
    except Exception:                                       # noqa: BLE001
        return "(its message could not be read)"


def error(where: str, exc: BaseException, level: int = ERROR) -> int:
    """Log `exc`, met in `where`, the first time; returns how many times.
    Never raises: it is what the watching threads call when all else fails."""
    try:
        key = _key(where, exc)
    except Exception:                                       # noqa: BLE001
        key = f"{where}: {type(exc).__name__}"
    with _lock:
        count = _counts[key] = _counts.get(key, 0) + 1
        _first.setdefault(key, where)
    try:
        message = _describe(exc)
        if count == 1:
            text = "".join(traceback.format_exception(exc)).rstrip()
            lines = [f"{where}: {type(exc).__name__}: {message}"] + \
                [f"  {line}" for line in text.splitlines()]
        elif count in (10, 100, 1000) or count % 10000 == 0:
            lines = [f"{where}: {type(exc).__name__}: {message} -- "
                     f"{count} times now"]
        else:
            return count
        sys.stderr.write("".join(f"<{level}>{line}\n" for line in lines))
        sys.stderr.flush()
    except Exception:                                       # noqa: BLE001
        pass                         # nowhere to say it; never raise from here
    return count


def counts() -> dict[str, int]:
    """Every kind of error met so far, with how often."""
    with _lock:
        return dict(_counts)


def total() -> int:
    with _lock:
        return sum(_counts.values())
