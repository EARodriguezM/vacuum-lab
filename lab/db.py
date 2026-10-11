"""SQLite store for vacuum test runs.

One file, no server, no dependencies beyond the standard library -- which
matters because the lab PC is an offline Ubuntu box and anything needing a
package index is a liability there.

Three tables carry a run: `runs` is the header and the computed result,
`samples` is the recorded curve, and `events` is the narrative (phase changes,
interlock refusals, operator actions). Keeping the narrative separate from the
curve means a run's log survives even when sampling was patchy.

Four more hold the accounts (`users`, `roles`, `sessions`, `role_requests`).
The rules about them live in lab/auth.py; this module only stores them. They
never leave the machine: `snapshot` drops them from the downloadable copy.

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

SCHEMA_VERSION = 4

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
    created_at  REAL    NOT NULL,
    -- The account that started it. `operator` above is free text typed on
    -- the form (a student running a run for somebody else); this is who was
    -- signed in. NULL for runs recorded before accounts existed.
    started_by  TEXT,
    -- Which chamber recorded it, so runs from two rigs can be told apart
    -- once their records meet.
    chamber     TEXT
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
    motor       INTEGER,
    -- The drive's over-temperature flags, 1 while raised. Only the full-vacuum
    -- run acted on them before; every run records them now, so a curve can
    -- show when the pump started cooking rather than only that it stopped.
    ov_temp_pump INTEGER,
    ov_temp_elec INTEGER,
    -- Process hardware, as far as it is known: a driven actuator's reading,
    -- or a manual one's value once the operator confirmed it. NULL means
    -- not known -- between "please open the valve" and the Done press.
    gas_sccm    REAL,
    gate_pct    REAL,
    magnetron_w REAL,
    shutter     INTEGER,
    -- The fractional pumping rate, %/min, that the base-pressure judgement
    -- was looking at, so a reader can see why a run called base when it did.
    rate_pct_min REAL
);

CREATE INDEX IF NOT EXISTS samples_by_run ON samples(run_id, t);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(id) ON DELETE CASCADE,
    at          REAL    NOT NULL,
    level       TEXT    NOT NULL,
    message     TEXT    NOT NULL,
    detail      TEXT,
    -- The username behind an action, NULL for what the rig did by itself.
    user        TEXT
);

CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id, at);

-- Accounts. A person signs in with a password; a paired tablet ("device")
-- has no password at all and only ever watches. `status` is pending until
-- an admin approves a sign-up, and disabled rather than deleted afterwards,
-- so the name behind old events still means somebody.
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    uuid          TEXT    NOT NULL UNIQUE,
    username      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name  TEXT    NOT NULL DEFAULT '',
    kind          TEXT    NOT NULL DEFAULT 'person',
    password_hash TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL DEFAULT 'pending',
    must_change   INTEGER NOT NULL DEFAULT 0,
    note          TEXT    DEFAULT '',
    created_at    REAL    NOT NULL,
    updated_at    REAL    NOT NULL,
    last_login_at REAL
);

-- One role per account per chamber: owner, admin, operator or viewer.
CREATE TABLE IF NOT EXISTS roles (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chamber     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    granted_by  TEXT,
    granted_at  REAL    NOT NULL,
    PRIMARY KEY (user_id, chamber)
);

-- Only a hash of each session token is kept: the token itself exists in the
-- browser that holds it, so a copy of this file signs nobody in.
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL    NOT NULL,
    seen_at     REAL    NOT NULL,
    active_at   REAL    NOT NULL,
    expires_at  REAL,
    address     TEXT    DEFAULT '',
    agent       TEXT    DEFAULT ''
);

CREATE INDEX IF NOT EXISTS sessions_by_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS role_requests (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chamber     TEXT    NOT NULL,
    role        TEXT    NOT NULL,
    note        TEXT    DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'open',
    created_at  REAL    NOT NULL,
    decided_by  TEXT,
    decided_at  REAL
);

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
    "ov_temp_pump", "ov_temp_elec",
    "gas_sccm", "gate_pct", "magnetron_w", "shutter", "rate_pct_min",
)

#: Columns added after schema version 1. `CREATE TABLE IF NOT EXISTS` does
#: nothing to a table that already exists, so an older database needs them
#: named explicitly -- otherwise the first insert fails on a column the code
#: believes is there.
MIGRATIONS = (
    ("samples", "p_best", "REAL"),
    ("samples", "ov_temp_pump", "INTEGER"),
    ("samples", "ov_temp_elec", "INTEGER"),
    ("samples", "gas_sccm", "REAL"),
    ("samples", "gate_pct", "REAL"),
    ("samples", "magnetron_w", "REAL"),
    ("samples", "shutter", "INTEGER"),
    ("samples", "rate_pct_min", "REAL"),
    ("runs", "started_by", "TEXT"),
    ("runs", "chamber", "TEXT"),
    ("events", "user", "TEXT"),
)

#: Tables holding credentials. Dropped from every copy that leaves the box.
ACCOUNT_TABLES = ("sessions", "role_requests", "roles", "users")


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
                   operator: str = "", notes: str = "",
                   started_by: str | None = None,
                   chamber: str | None = None) -> dict:
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO runs (uuid, procedure, name, operator, notes, "
                "config, status, started_at, created_at, started_by, chamber) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), procedure, name, operator, notes,
                 json.dumps(config), RUNNING, now, now, started_by, chamber),
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
                  detail: dict | None = None, user: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (run_id, at, level, message, detail, user) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, time.time(), level, message,
                 json.dumps(detail) if detail else None, user),
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
        proper online backup instead. The accounts are dropped from the copy
        and the file vacuumed, so the freed pages do not still hold them: a
        viewer may download the runs, never the password hashes.
        """
        with self._lock:
            target = sqlite3.connect(destination)
            try:
                self._db.backup(target)
                for table in ACCOUNT_TABLES:
                    target.execute(f"DROP TABLE IF EXISTS {table}")
                target.commit()
                target.execute("VACUUM")
            finally:
                target.close()
        return destination

    # --- accounts ---------------------------------------------------------
    #
    # Storage only. Who may do what, and what a password must look like, is
    # lab/auth.py's business; nothing here checks a permission.

    def create_user(self, username: str, password_hash: str, *,
                    display_name: str = "", kind: str = "person",
                    status: str = "pending", must_change: bool = False,
                    note: str = "") -> dict:
        """Insert an account. Raises sqlite3.IntegrityError if the name is taken."""
        now = time.time()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO users (uuid, username, display_name, kind, "
                "password_hash, status, must_change, note, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), username, display_name or username, kind,
                 password_hash, status, int(bool(must_change)), note, now, now),
            )
            self._db.commit()
            return self.user(cursor.lastrowid)  # type: ignore[arg-type]

    def user(self, user_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def user_by_name(self, username: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return dict(row) if row else None

    def users(self, chamber: str) -> list[dict]:
        """Every account, with its role in `chamber` (None if it has none)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT u.*, r.role AS role FROM users u "
                "LEFT JOIN roles r ON r.user_id = u.id AND r.chamber = ? "
                "ORDER BY u.kind, u.status, u.username", (chamber,)).fetchall()
        return [dict(row) for row in rows]

    def update_user(self, user_id: int, **fields) -> dict | None:
        allowed = {"display_name", "password_hash", "status", "must_change",
                   "note", "last_login_at"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if changes:
            changes["updated_at"] = time.time()
            assignments = ", ".join(f"{k}=?" for k in changes)
            with self._lock:
                self._db.execute(f"UPDATE users SET {assignments} WHERE id=?",
                                 (*changes.values(), user_id))
                self._db.commit()
        return self.user(user_id)

    def delete_user(self, user_id: int) -> bool:
        with self._lock:
            cursor = self._db.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._db.commit()
            return cursor.rowcount > 0

    def role(self, user_id: int, chamber: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT role FROM roles WHERE user_id=? AND chamber=?",
                (user_id, chamber)).fetchone()
        return row["role"] if row else None

    def set_role(self, user_id: int, chamber: str, role: str | None,
                 granted_by: str | None = None) -> None:
        """Give an account a role in a chamber, or take it away with None."""
        with self._lock:
            if role is None:
                self._db.execute(
                    "DELETE FROM roles WHERE user_id=? AND chamber=?",
                    (user_id, chamber))
            else:
                self._db.execute(
                    "INSERT INTO roles (user_id, chamber, role, granted_by, "
                    "granted_at) VALUES (?,?,?,?,?) "
                    "ON CONFLICT(user_id, chamber) DO UPDATE SET "
                    "role=excluded.role, granted_by=excluded.granted_by, "
                    "granted_at=excluded.granted_at",
                    (user_id, chamber, role, granted_by, time.time()))
            self._db.commit()

    def holders(self, chamber: str, role: str) -> list[dict]:
        """The accounts holding `role` in `chamber` -- one, for the owner."""
        with self._lock:
            rows = self._db.execute(
                "SELECT u.* FROM users u JOIN roles r ON r.user_id = u.id "
                "WHERE r.chamber=? AND r.role=?", (chamber, role)).fetchall()
        return [dict(row) for row in rows]

    def create_session(self, token_hash: str, user_id: int,
                       expires_at: float | None, address: str = "",
                       agent: str = "") -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO sessions (token_hash, user_id, created_at, "
                "seen_at, active_at, expires_at, address, agent) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (token_hash, user_id, now, now, now, expires_at, address,
                 agent[:200]))
            self._db.commit()

    def session(self, token_hash: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE token_hash=?",
                (token_hash,)).fetchone()
        return dict(row) if row else None

    def sessions(self, user_id: int | None = None) -> list[dict]:
        where, args = ("WHERE user_id=?", (user_id,)) if user_id is not None \
            else ("", ())
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM sessions {where} ORDER BY seen_at DESC",
                args).fetchall()
        return [dict(row) for row in rows]

    def touch_session(self, token_hash: str, *, seen_at: float | None = None,
                      active_at: float | None = None,
                      address: str | None = None) -> None:
        changes = {k: v for k, v in (("seen_at", seen_at),
                                     ("active_at", active_at),
                                     ("address", address)) if v is not None}
        if not changes:
            return
        assignments = ", ".join(f"{k}=?" for k in changes)
        with self._lock:
            self._db.execute(
                f"UPDATE sessions SET {assignments} WHERE token_hash=?",
                (*changes.values(), token_hash))
            self._db.commit()

    def delete_session(self, token_hash: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM sessions WHERE token_hash=?",
                             (token_hash,))
            self._db.commit()

    def delete_sessions(self, user_id: int, keep: str | None = None) -> int:
        """End every session of an account, except `keep` if given."""
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE user_id=? AND token_hash IS NOT ?",
                (user_id, keep))
            self._db.commit()
            return cursor.rowcount

    def purge_sessions(self, now: float | None = None) -> int:
        """Forget sessions past their expiry."""
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM sessions WHERE expires_at IS NOT NULL "
                "AND expires_at < ?", (now or time.time(),))
            self._db.commit()
            return cursor.rowcount

    def create_role_request(self, user_id: int, chamber: str, role: str,
                            note: str = "") -> dict:
        with self._lock:
            # One open request per person and chamber: a new one replaces it.
            self._db.execute(
                "DELETE FROM role_requests WHERE user_id=? AND chamber=? "
                "AND status='open'", (user_id, chamber))
            cursor = self._db.execute(
                "INSERT INTO role_requests (user_id, chamber, role, note, "
                "created_at) VALUES (?,?,?,?,?)",
                (user_id, chamber, role, note, time.time()))
            self._db.commit()
            return self.role_request(cursor.lastrowid)  # type: ignore[arg-type]

    def role_request(self, request_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM role_requests WHERE id=?",
                (request_id,)).fetchone()
        return dict(row) if row else None

    def role_requests(self, chamber: str, status: str | None = "open",
                      user_id: int | None = None) -> list[dict]:
        clauses, args = ["q.chamber=?"], [chamber]
        if status:
            clauses.append("q.status=?")
            args.append(status)
        if user_id is not None:
            clauses.append("q.user_id=?")
            args.append(user_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT q.*, u.username, u.display_name FROM role_requests q "
                "JOIN users u ON u.id = q.user_id "
                f"WHERE {' AND '.join(clauses)} ORDER BY q.created_at",
                args).fetchall()
        return [dict(row) for row in rows]

    def decide_role_request(self, request_id: int, status: str,
                            decided_by: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE role_requests SET status=?, decided_by=?, decided_at=? "
                "WHERE id=?", (status, decided_by, time.time(), request_id))
            self._db.commit()


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
