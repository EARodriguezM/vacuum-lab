"""SQLite store for vacuum test runs.

One file, no server, no dependencies beyond the standard library -- which
matters because the lab PC is an offline Ubuntu box and anything needing a
package index is a liability there.

Three tables carry a run: `runs` is the header and the computed result,
`samples` is the recorded curve, and `events` is the narrative (phase changes,
interlock refusals, operator actions). Keeping the narrative separate from the
curve means a run's log survives even when sampling was patchy.

Concurrency: one connection guarded by a lock, in WAL mode. The alternative --
a connection per thread -- buys nothing here, because the writers are a single
run engine and the readers are short HTTP handlers, and it would cost the
"one obvious place the schema lives" property.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA_VERSION = 2

#: Statuses a run can be in. `running` is the only one the engine writes live;
#: everything else is terminal.
RUNNING = "running"
COMPLETED = "completed"
ABORTED = "aborted"
FAILED = "failed"
TERMINAL = (COMPLETED, ABORTED, FAILED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    uuid        TEXT    NOT NULL UNIQUE,
    procedure   TEXT    NOT NULL,
    name        TEXT    NOT NULL,
    operator    TEXT    DEFAULT '',
    notes       TEXT    DEFAULT '',
    config      TEXT    NOT NULL DEFAULT '{}',
    status      TEXT    NOT NULL,
    phase       TEXT    DEFAULT '',
    started_at  REAL    NOT NULL,
    ended_at    REAL,
    result      TEXT,
    created_at  REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    t           REAL    NOT NULL,
    at          REAL    NOT NULL,
    phase       TEXT    DEFAULT '',
    p1          REAL,
    p1_status   INTEGER,
    p2          REAL,
    p2_status   INTEGER,
    -- The pressure the run engine actually judged on: the lower of the valid
    -- channels, in mbar, or NULL when no channel was reading one. Stored
    -- rather than recomputed, because "which channel was trustworthy" is a
    -- decision made at sampling time and a reader cannot reconstruct it.
    p_best      REAL,
    speed       REAL,
    set_speed   REAL,
    current     REAL,
    voltage     REAL,
    power       REAL,
    tms_temp    REAL,
    error_code  TEXT,
    station     INTEGER,
    motor       INTEGER
);

CREATE INDEX IF NOT EXISTS samples_by_run ON samples(run_id, t);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    at          REAL    NOT NULL,
    level       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, at);

CREATE TABLE IF NOT EXISTS devices (
    key         TEXT    PRIMARY KEY,
    kind        TEXT    NOT NULL,
    port        TEXT    DEFAULT '',
    identity    TEXT    DEFAULT '{}',
    first_seen  REAL    NOT NULL,
    last_seen   REAL    NOT NULL
);
"""

#: Sample columns in the order they are written and exported. Kept in one
#: place so the schema, the insert and the CSV header cannot drift apart.
SAMPLE_COLUMNS = (
    "t", "at", "phase", "p1", "p1_status", "p2", "p2_status", "p_best",
    "speed", "set_speed", "current", "voltage", "power",
    "tms_temp", "error_code", "station", "motor",
)

#: Columns added after schema version 1. `CREATE TABLE IF NOT EXISTS` does
#: nothing to a table that already exists, so an older database needs them
#: named explicitly -- otherwise the first insert fails on a column the code
#: believes is there.
MIGRATIONS = (
    ("samples", "p_best", "REAL"),
)


def default_path() -> str:
    """Where the database lives unless told otherwise."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "vacuum-lab", "lab.db")


class Database:
    """The run store. Thread-safe; every public method takes the lock."""

    def __init__(self, path: str | None = None):
        self.path = path or default_path()
        directory = os.path.dirname(os.path.abspath(self.path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            self._db.commit()

    def _migrate(self) -> None:
        """Add columns an older database is missing. Caller holds the lock."""
        for table, column, kind in MIGRATIONS:
            present = {row["name"] for row in
                       self._db.execute(f"PRAGMA table_info({table})")}
            if column not in present:
                self._db.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def close(self) -> None:
        with self._lock:
            # Fold the write-ahead log back into the database file before
            # letting go. SQLite recovers from a live WAL perfectly well on
            # the next open, so this is not about correctness -- it is about
            # everything that copies the file. A plain `cp lab.db` of a
            # database with a 4 MB WAL silently loses the most recent runs,
            # which is exactly what happened taking a backup on 2026-09-02.
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._db.close()

    # --- runs -------------------------------------------------------------

    def create_run(self, procedure: str, name: str, config: dict,
                   operator: str = "", notes: str = "") -> dict:
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO runs (uuid, procedure, name, operator, notes, "
                "config, status, started_at, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), procedure, name, operator, notes,
                 json.dumps(config), RUNNING, now, now),
            )
            self._db.commit()
            return self.run(cursor.lastrowid)  # type: ignore[arg-type]

    def finish_run(self, run_id: int, status: str, result: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE runs SET status=?, ended_at=?, result=?, phase='' "
                "WHERE id=?",
                (status, time.time(), json.dumps(result), run_id),
            )
            self._db.commit()

    def set_phase(self, run_id: int, phase: str) -> None:
        with self._lock:
            self._db.execute("UPDATE runs SET phase=? WHERE id=?", (phase, run_id))
            self._db.commit()

    def update_run(self, run_id: int, **fields) -> dict | None:
        """Edit the operator-supplied metadata of an existing run."""
        allowed = {"name", "operator", "notes"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return self.run(run_id)
        assignments = ", ".join(f"{k}=?" for k in changes)
        with self._lock:
            self._db.execute(
                f"UPDATE runs SET {assignments} WHERE id=?",
                (*changes.values(), run_id),
            )
            self._db.commit()
        return self.run(run_id)

    def run(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        return _run_row(row) if row else None

    def runs(self, limit: int = 200, offset: int = 0,
             procedure: str | None = None, status: str | None = None,
             search: str | None = None) -> list[dict]:
        clauses, args = [], []
        if procedure:
            clauses.append("procedure = ?")
            args.append(procedure)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if search:
            clauses.append("(name LIKE ? OR operator LIKE ? OR notes LIKE ?)")
            args.extend([f"%{search}%"] * 3)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT r.*, "
                f"  (SELECT COUNT(*) FROM samples s WHERE s.run_id = r.id) "
                f"    AS sample_count "
                f"FROM runs r {where} "
                f"ORDER BY r.started_at DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return [_run_row(row) for row in rows]

    def count_runs(self, procedure: str | None = None,
                   status: str | None = None) -> int:
        clauses, args = [], []
        if procedure:
            clauses.append("procedure = ?")
            args.append(procedure)
        if status:
            clauses.append("status = ?")
            args.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            row = self._db.execute(
                f"SELECT COUNT(*) AS n FROM runs {where}", args
            ).fetchone()
        return int(row["n"])

    def active_run(self) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM runs WHERE status=? ORDER BY started_at DESC "
                "LIMIT 1", (RUNNING,)
            ).fetchone()
        return _run_row(row) if row else None

    def delete_run(self, run_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM runs WHERE id=?", (run_id,))
            self._db.commit()
            return cursor.rowcount > 0

    def orphaned_runs(self) -> list[dict]:
        """Runs left `running` by a crash or a restart.

        The engine only ever holds one run open, so on startup anything still
        marked running belongs to a dead process and must be closed out --
        otherwise the UI shows a phantom test that nothing is driving.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM runs WHERE status=?", (RUNNING,)
            ).fetchall()
        return [_run_row(row) for row in rows]

    # --- samples ----------------------------------------------------------

    def add_sample(self, run_id: int, sample: dict) -> None:
        columns = ", ".join(SAMPLE_COLUMNS)
        placeholders = ", ".join("?" * len(SAMPLE_COLUMNS))
        values = [sample.get(name) for name in SAMPLE_COLUMNS]
        with self._lock:
            self._db.execute(
                f"INSERT INTO samples (run_id, {columns}) "
                f"VALUES (?, {placeholders})",
                (run_id, *values),
            )
            self._db.commit()

    def samples(self, run_id: int, limit: int | None = None,
                every: int = 1) -> list[dict]:
        """Read a run's curve.

        `every` decimates by taking one row in N, which is how a six-hour
        bakeout stays plottable without shipping 20 000 points to a browser.
        """
        query = ("SELECT * FROM samples WHERE run_id=? "
                 "ORDER BY t ASC")
        args: list = [run_id]
        if every > 1:
            query = ("SELECT * FROM (SELECT *, ROW_NUMBER() OVER "
                     "(ORDER BY t ASC) AS rn FROM samples WHERE run_id=?) "
                     "WHERE rn % ? = 1 ORDER BY t ASC")
            args.append(every)
        if limit:
            query += " LIMIT ?"
            args.append(limit)
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def sample_count(self, run_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM samples WHERE run_id=?", (run_id,)
            ).fetchone()
        return int(row["n"])

    def latest_sample(self, run_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM samples WHERE run_id=? ORDER BY t DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    # --- events -----------------------------------------------------------

    def add_event(self, run_id: int | None, level: str, message: str,
                  detail: dict | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (run_id, at, level, message, detail) "
                "VALUES (?,?,?,?,?)",
                (run_id, time.time(), level, message,
                 json.dumps(detail) if detail else None),
            )
            self._db.commit()

    def events(self, run_id: int | None = None, limit: int = 500) -> list[dict]:
        where = "WHERE run_id=?" if run_id is not None else ""
        args: list = [run_id] if run_id is not None else []
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM events {where} ORDER BY at DESC LIMIT ?",
                (*args, limit),
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            if entry.get("detail"):
                try:
                    entry["detail"] = json.loads(entry["detail"])
                except (ValueError, TypeError):
                    pass
            out.append(entry)
        return out

    # --- devices ----------------------------------------------------------

    def record_device(self, key: str, kind: str, port: str,
                      identity: dict) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO devices (key, kind, port, identity, first_seen, "
                "last_seen) VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET port=excluded.port, "
                "identity=excluded.identity, last_seen=excluded.last_seen",
                (key, kind, port, json.dumps(identity), now, now),
            )
            self._db.commit()

    def devices(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM devices ORDER BY key"
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            try:
                entry["identity"] = json.loads(entry["identity"])
            except (ValueError, TypeError):
                entry["identity"] = {}
            out.append(entry)
        return out

    # --- housekeeping -----------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            runs = self._db.execute(
                "SELECT COUNT(*) AS n, "
                "COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0) "
                "  AS completed "
                "FROM runs"
            ).fetchone()
            samples = self._db.execute(
                "SELECT COUNT(*) AS n FROM samples"
            ).fetchone()
            by_procedure = self._db.execute(
                "SELECT procedure, COUNT(*) AS n FROM runs GROUP BY procedure"
            ).fetchall()
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {
            "runs": int(runs["n"]),
            "completed": int(runs["completed"]),
            "samples": int(samples["n"]),
            "by_procedure": {r["procedure"]: int(r["n"]) for r in by_procedure},
            "path": self.path,
            "bytes": size,
        }

    def snapshot(self, destination: str) -> str:
        """Write a consistent copy of the database, safe to download.

        Copying the file directly would race the WAL; `sqlite3.backup` takes a
        proper online backup instead.
        """
        with self._lock:
            target = sqlite3.connect(destination)
            try:
                self._db.backup(target)
            finally:
                target.close()
        return destination


def _run_row(row: sqlite3.Row) -> dict:
    """Turn a run row into a dict with its JSON columns already decoded."""
    entry = dict(row)
    for column in ("config", "result"):
        raw = entry.get(column)
        if raw:
            try:
                entry[column] = json.loads(raw)
            except (ValueError, TypeError):
                entry[column] = {}
        else:
            entry[column] = {} if column == "config" else None
    if entry.get("ended_at") and entry.get("started_at"):
        entry["duration"] = entry["ended_at"] - entry["started_at"]
    else:
        entry["duration"] = None
    return entry
