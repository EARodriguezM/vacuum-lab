#!/usr/bin/env python3
"""Backups of the rig's database, checked, kept, and restorable.

The SQLite download in the panel is not a backup: it drops the accounts so a
viewer can have it. This keeps everything -- runs, samples, events, accounts,
the rig's equipment and ports -- so it is as private as the database itself:
directories 0700, files 0600.

    backup.py init    DIR                 make DIR a destination (once)
    backup.py make    --db PATH --dest DIR [--dest HOST:DIR] ...
    backup.py verify  DIR [--max-age-h 48]
    backup.py prune   DIR                 retention, run where DIR is
    backup.py restore BACKUP --to PATH
    backup.py drill   BACKUP
    backup.py pull    HOST:DIR LOCAL_DIR
    backup.py status  [--db PATH]

A destination is a folder `init` has marked (`.vacuum-lab-backup`): a stick
that is not mounted leaves a bare mount point, and a backup must never fill
the system disk under it believing it is the stick.

`make` copies the database with `VACUUM INTO` from a read-only connection of
its own (a consistent snapshot, the write-ahead log included, without
stopping the panel's writers), checks the copy (`PRAGMA integrity_check`,
row counts, SHA-256) and writes it to every destination as
`<dest>/<chamber>/lab-YYYYmmdd-HHMMSS/`, the manifest last: a directory
with no manifest is unfinished and is never counted, restored or kept. A
destination on another machine (`host:dir`) goes by rsync over SSH, into an
unfinished folder renamed when complete; that machine prunes its own copy
(`backup.py prune DIR` there). One destination failing does not stop the
others; the run fails unless every one worked. Each local destination then
keeps, of its complete backups (judged by manifest and file sizes, so the
stick is not read through four times a day),
everything from the last 48 h and the newest of each of the last 14 days,
8 weeks and 12 months -- never pruning when the clock reads earlier than
the newest backup (a Pi that booted without network time), and never the
last good one.

`make` records how it went in `backup-status.json` beside the database,
which the panel's supervisor reads to raise an alarm when the last good
backup is more than 48 h old or the last attempt failed.

The destinations are decided by the lab (2026-10-11): a USB stick on each
Pi, the other Pi once chamber 2 exists, the laptop when it is connected --
never the lab PC alone, which may be off. `pull` is the laptop's side:
rsync a destination's tree here, then verify every backup in it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: What a backup holds besides the database, when it exists beside it.
SIDE_FILES = ("equipment.json", "ports.json")
MANIFEST = "MANIFEST.json"
STATUS = "backup-status.json"
#: What `init` leaves in a destination, and `make` looks for.
MARKER = ".vacuum-lab-backup"
PREFIX = "lab-"
STAMP = "%Y%m%d-%H%M%S"
#: An unfinished directory older than this is a dead attempt; removed.
PARTIAL_MAX_AGE_S = 24 * 3600
#: Every backup this recent is kept, whatever the calendar rules say.
KEEP_ALL_S = 48 * 3600
#: The alarm's threshold, and `verify`'s default.
MAX_AGE_H = 48.0
DEFAULT_KEEP = (14, 8, 12)        # daily, weekly, monthly
DEFAULT_DB = os.path.expanduser("~/.local/share/vacuum-lab/lab.db")


class BackupError(Exception):
    pass


# --- the copy ---------------------------------------------------------------


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_read_only(path: Path, immutable: bool = False) -> sqlite3.Connection:
    flag = "immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"{path.resolve().as_uri()}?{flag}", uri=True,
                           timeout=30)


def table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    names = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: connection.execute(f'SELECT COUNT(*) FROM "{name}"')
            .fetchone()[0] for name in names}


def check_database(path: Path) -> tuple[str, dict[str, int]]:
    """(`integrity_check`'s verdict, rows per table) of a database file."""
    connection = _open_read_only(path, immutable=True)
    try:
        verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
        return str(verdict), table_counts(connection)
    finally:
        connection.close()


def snapshot(db: Path, folder: Path, chamber: str, now: float) -> Path:
    """A complete, checked backup directory inside `folder`."""
    if not db.exists():
        raise BackupError(f"no database at {db}")
    name = PREFIX + time.strftime(STAMP, time.gmtime(now))
    target = folder / name
    _private_dir(target)
    copy = target / "lab.db"
    previous = os.umask(0o077)
    try:
        source = _open_read_only(db)
        try:
            source.execute("VACUUM INTO ?", (str(copy),))
        finally:
            source.close()
    except sqlite3.Error as exc:
        raise BackupError(f"could not copy {db}: {exc}") from exc
    finally:
        os.umask(previous)
    os.chmod(copy, 0o600)
    verdict, counts = check_database(copy)
    if verdict != "ok":
        raise BackupError(f"the copy failed its integrity check: {verdict}")
    files = {"lab.db": copy}
    for side in SIDE_FILES:
        original = db.parent / side
        if original.exists():
            shutil.copyfile(original, target / side)
            os.chmod(target / side, 0o600)
            files[side] = target / side
    manifest = {
        "format": 1,
        "chamber": chamber,
        "created": now,
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "host": socket.gethostname(),
        "source": str(db),
        "sqlite": sqlite3.sqlite_version,
        "integrity": verdict,
        "tables": counts,
        "files": {key: {"bytes": path.stat().st_size, "sha256": _sha256(path)}
                  for key, path in files.items()},
    }
    for path in files.values():
        _fsync(path)
    _write_json(target / MANIFEST, manifest)
    return target


def _write_json(path: Path, data: dict) -> None:
    """Written whole or not at all, 0600."""
    temporary = path.with_name(f".{path.name}.tmp")
    previous = os.umask(0o077)
    try:
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.umask(previous)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


# --- checking one ------------------------------------------------------------


def read_manifest(backup: Path) -> dict | None:
    try:
        with open(backup / MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        return None
    return manifest if isinstance(manifest, dict) else None


def verify_backup(backup: Path) -> list[str]:
    """What is wrong with one backup directory; empty when it is good."""
    manifest = read_manifest(backup)
    if manifest is None:
        return ["no readable manifest: unfinished or damaged"]
    problems = []
    files = manifest.get("files") or {}
    if "lab.db" not in files:
        problems.append("the manifest lists no database")
    for name, expected in files.items():
        path = backup / name
        if not path.exists():
            problems.append(f"{name} is missing")
            continue
        if path.stat().st_size != expected.get("bytes"):
            problems.append(f"{name} is {path.stat().st_size} bytes, "
                            f"the manifest says {expected.get('bytes')}")
            continue
        if _sha256(path) != expected.get("sha256"):
            problems.append(f"{name} does not match its checksum")
    if not problems:
        try:
            verdict, counts = check_database(backup / "lab.db")
        except sqlite3.Error as exc:
            return [f"the database does not open: {exc}"]
        if verdict != "ok":
            problems.append(f"integrity check: {verdict}")
        if counts != manifest.get("tables"):
            problems.append("row counts differ from the manifest")
    return problems


def backups_in(folder: Path) -> list[Path]:
    """Every finished backup directory in `folder`, newest name first."""
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir()
                   if p.is_dir() and p.name.startswith(PREFIX)
                   and (p / MANIFEST).exists()),
                  key=lambda p: p.name, reverse=True)


def created(backup: Path) -> float | None:
    manifest = read_manifest(backup)
    value = (manifest or {}).get("created")
    return float(value) if isinstance(value, (int, float)) else None


# --- keeping ----------------------------------------------------------------


def to_keep(stamps: dict[Path, float], now: float,
            keep: tuple[int, int, int] = DEFAULT_KEEP) -> set[Path]:
    """Which backups the retention rules keep, given their creation times."""
    daily, weekly, monthly = keep
    ordered = sorted(stamps, key=lambda p: stamps[p], reverse=True)
    kept = {p for p in ordered if now - stamps[p] <= KEEP_ALL_S}
    if ordered:
        kept.add(ordered[0])
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    buckets = (
        (daily, lambda d: d.toordinal(), today.toordinal()),
        (weekly, lambda d: d.toordinal() // 7, today.toordinal() // 7),
        (monthly, lambda d: d.year * 12 + d.month - 1,
         today.year * 12 + today.month - 1),
    )
    for count, bucket, current in buckets:
        seen = set()
        for path in ordered:
            day = dt.datetime.fromtimestamp(stamps[path], dt.timezone.utc).date()
            key = bucket(day)
            if current - key >= count or key in seen:
                continue
            seen.add(key)
            kept.add(path)
    return kept


def complete(backup: Path) -> bool:
    """A manifest, and every file it lists at the size it says: enough to
    judge by the calendar. The checksums and the integrity check are for
    `verify` and `drill` -- read four times a day for every backup kept,
    they would wear the stick out reading it."""
    manifest = read_manifest(backup)
    if not manifest or not isinstance(manifest.get("files"), dict):
        return False
    for name, expected in manifest["files"].items():
        try:
            if (backup / name).stat().st_size != expected.get("bytes"):
                return False
        except OSError:
            return False
    return "lab.db" in manifest["files"]


def prune(folder: Path, now: float, keep=DEFAULT_KEEP, log=print) -> list[Path]:
    """Remove what retention does not keep; returns what was removed.

    Only complete backups are judged; anything else -- unfinished, damaged,
    unreadable -- is left for `verify` to report and a person to look at,
    and nothing is removed on a check that failed to run.
    """
    removed = []
    for partial in [*folder.glob(".partial-*"), *folder.glob(".work-*")]:
        try:
            if now - partial.stat().st_mtime > PARTIAL_MAX_AGE_S:
                shutil.rmtree(partial)
                removed.append(partial)
        except OSError:
            pass
    good = {}
    for backup in backups_in(folder):
        try:
            stamp = created(backup)
            if stamp is not None and complete(backup):
                good[backup] = stamp
        except OSError:
            continue
    if not good:
        return removed
    if max(good.values()) > now + 60:
        log(f"not pruning {folder}: the clock reads earlier than its newest "
            f"backup -- is network time set?")
        return removed
    kept = to_keep(good, now, keep)
    for backup in good:
        if backup in kept:
            continue
        try:
            shutil.rmtree(backup)
            removed.append(backup)
        except OSError as exc:
            log(f"  could not remove {backup}: {exc}")
    return removed


# --- destinations ------------------------------------------------------------


def is_remote(dest: str) -> bool:
    head = dest.split("/", 1)[0]
    return ":" in head and not dest.startswith("/")


def _rsync(args: list[str]) -> None:
    command = ["rsync", "-a", "--chmod=D700,F600", *args]
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=3600)
    except FileNotFoundError as exc:
        raise BackupError("rsync is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("rsync took over an hour") from exc
    if result.returncode != 0:
        raise BackupError(f"rsync: {result.stderr.strip()[:300]}")


def _ssh(host: str, command: str, timeout: float = 120) -> tuple[int, str]:
    try:
        result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o",
                                 "ConnectTimeout=10", host, command],
                                capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise BackupError("ssh is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError(f"{host} did not answer") from exc
    return result.returncode, (result.stdout + result.stderr).strip()


def _remote_quote(path: str) -> str:
    """Quoted for the far shell, a leading ~/ left for it to expand."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def _not_prepared(dest: str) -> str:
    return (f"{dest} is not a prepared backup destination (no {MARKER} in "
            f"it): is the stick mounted? A new one is prepared once with "
            f"`backup.py init {dest}`")


def deliver(made: Path, dest: str, chamber: str, keep, now: float,
            log=print) -> str:
    """Put one finished backup at one destination; the path it landed at."""
    if is_remote(dest):
        host, folder = dest.split(":", 1)
        folder = folder.rstrip("/")
        q = _remote_quote
        code, out = _ssh(host, f"test -e {q(folder)}/{MARKER}")
        if code != 0:
            raise BackupError(_not_prepared(dest))
        partial = f"{folder}/{chamber}/.partial-{made.name}"
        final = f"{folder}/{chamber}/{made.name}"
        code, out = _ssh(host, f"mkdir -p -m 700 {q(folder + '/' + chamber)} "
                               f"&& mkdir -m 700 {q(partial)}")
        if code != 0:
            raise BackupError(f"{dest}: {out[:200]}")
        # Into an unfinished folder, renamed once everything is there: a
        # copy cut short is never taken for a backup.
        _rsync([*(str(p) for p in sorted(made.iterdir())), f"{host}:{partial}/"])
        code, out = _ssh(host, f"mv -T {q(partial)} {q(final)}")
        if code != 0:
            raise BackupError(f"{dest}: {out[:200]}")
        return f"{host}:{final}"
    if not (Path(dest) / MARKER).exists():
        raise BackupError(_not_prepared(dest))
    folder = Path(dest) / chamber
    try:
        folder.mkdir(mode=0o700, exist_ok=True)      # never the dest's parents
    except OSError as exc:
        raise BackupError(f"{dest} cannot be written ({exc.strerror})") from exc
    partial = folder / f".partial-{made.name}"
    final = folder / made.name
    if final.exists():
        raise BackupError(f"{final} already exists")
    if partial.exists():
        shutil.rmtree(partial)
    _private_dir(partial)
    for source in sorted(made.iterdir()):
        if source.name == MANIFEST:
            continue
        shutil.copyfile(source, partial / source.name)
        os.chmod(partial / source.name, 0o600)
        _fsync(partial / source.name)
    shutil.copyfile(made / MANIFEST, partial / MANIFEST)
    os.chmod(partial / MANIFEST, 0o600)
    problems = verify_backup(partial)
    if problems:
        shutil.rmtree(partial)
        raise BackupError(f"the copy at {dest} did not verify: "
                          + "; ".join(problems))
    os.replace(partial, final)
    try:
        for removed in prune(folder, now, keep, log):
            log(f"  removed {removed}")
    except Exception as exc:                                # noqa: BLE001
        # The backup is made and verified; tidying up must not undo that.
        log(f"  pruning {folder} failed: {exc}")
    return str(final)


def _work_folder(dests: list[str], chamber: str, db: Path) -> Path:
    """Where the copy is made before it is delivered: on the first prepared
    stick. Never /tmp, which on a Pi is RAM and may not hold the database.
    With only other machines to send to, beside the database itself -- one
    extra write of it on the SD card per backup, rather than none at all."""
    for dest in dests:
        if is_remote(dest) or not (Path(dest) / MARKER).exists():
            continue
        try:
            folder = Path(dest) / chamber
            folder.mkdir(mode=0o700, exist_ok=True)
            return folder
        except OSError:
            continue
    return db.parent


def init(dest: Path, db: Path | None = None, log=print) -> None:
    """Make `dest` a backup destination: the folder, private, and its mark."""
    dest.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    (dest / MARKER).write_text("Backups of the vacuum lab's database "
                               "(tools/backup.py).\n")
    if db is not None and db.exists() and \
            os.stat(dest).st_dev == os.stat(db.parent).st_dev:
        log(f"  note: {dest} is on the same disk as {db}; a backup there is "
            f"lost with that disk. A USB stick or another machine is the point.")


def make(db: Path, dests: list[str], chamber: str, keep=DEFAULT_KEEP,
         now: float | None = None, log=print) -> dict:
    """One backup, to every destination. Returns what `status` records."""
    now = time.time() if now is None else now
    record: dict = {"attempted": now, "ok": False, "destinations": [],
                    "error": None}
    if not dests:
        record["error"] = "no destination given"
        return record
    ready = [d for d in dests if is_remote(d) or (Path(d) / MARKER).exists()]
    if not ready:
        # Not even a copy: it would be written beside the database -- on the
        # SD card -- only to go nowhere.
        record["error"] = "; ".join(_not_prepared(d) for d in dests)
        record["destinations"] = [{"dest": d, "ok": False,
                                   "error": _not_prepared(d)} for d in dests]
        return record
    work = Path(tempfile.mkdtemp(prefix=".work-",
                                 dir=_work_folder(dests, chamber, db)))
    try:
        os.chmod(work, 0o700)
        try:
            made = snapshot(db, work, chamber, now)
        except (BackupError, OSError, sqlite3.Error) as exc:
            record["error"] = str(exc)
            return record
        manifest = read_manifest(made) or {}
        record.update({"name": made.name, "tables": manifest.get("tables"),
                       "bytes": manifest.get("files", {}).get("lab.db", {})
                       .get("bytes")})
        for dest in dests:
            entry = {"dest": dest, "ok": False}
            try:
                entry["path"] = deliver(made, dest, chamber, keep, now, log)
                entry["ok"] = True
                log(f"  {dest}: {entry['path']}")
            except (BackupError, OSError) as exc:
                entry["error"] = str(exc)
                log(f"  {dest}: FAILED -- {exc}")
            record["destinations"].append(entry)
        record["ok"] = all(d["ok"] for d in record["destinations"])
        if not record["ok"]:
            record["error"] = "; ".join(f"{d['dest']}: {d['error']}"
                                        for d in record["destinations"]
                                        if not d["ok"])
        return record
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --- the record the panel reads ----------------------------------------------


def status_path(db: Path) -> Path:
    return db.parent / STATUS


def write_status(db: Path, record: dict) -> None:
    """Fold this attempt into the status beside the database."""
    path = status_path(db)
    status = read_status(db) or {}
    status["last_attempt"] = record["attempted"]
    status["last_ok"] = bool(record["ok"])
    status["last_error"] = record.get("error")
    status["destinations"] = record.get("destinations", [])
    if record["ok"]:
        status["last_success"] = record["attempted"]
        status["last_name"] = record.get("name")
    _write_json(path, status)


def read_status(db: Path) -> dict | None:
    try:
        with open(status_path(db), encoding="utf-8") as fh:
            status = json.load(fh)
    except (OSError, ValueError):
        return None
    return status if isinstance(status, dict) else None


# --- restoring --------------------------------------------------------------


def restore(backup: Path, to: Path, force: bool = False) -> None:
    problems = verify_backup(backup)
    if problems:
        raise BackupError("the backup does not verify: " + "; ".join(problems))
    if to.exists() and not force:
        raise BackupError(f"{to} exists; stop the panel, move it aside, or "
                          f"pass --force")
    wal, shm = Path(str(to) + "-wal"), Path(str(to) + "-shm")
    if wal.exists() and wal.stat().st_size > 0:
        raise BackupError(f"{wal} holds data: the database is in use, or was "
                          f"not closed cleanly -- stop the panel first")
    # An empty log and its index are what a closed database leaves after a
    # read-only look (a backup taken with the panel stopped): stale, and
    # they belong to the file being replaced.
    for leftover in (wal, shm):
        if leftover.exists():
            leftover.unlink()
    if not to.parent.exists():
        to.parent.mkdir(mode=0o700, parents=True)
    temporary = to.with_name(f".{to.name}.restoring")
    shutil.copyfile(backup / "lab.db", temporary)
    os.chmod(temporary, 0o600)
    _fsync(temporary)
    os.replace(temporary, to)
    for side in SIDE_FILES:
        if (backup / side).exists():
            shutil.copyfile(backup / side, to.parent / side)
            os.chmod(to.parent / side, 0o600)


def drill(backup: Path, log=print) -> bool:
    """Restore into a scratch folder and open it as the panel would."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from lab import db as dbmod

    # Beside the backup, on the stick: /tmp is RAM on a Pi.
    scratch = Path(tempfile.mkdtemp(prefix=".drill-", dir=backup.parent))
    try:
        restore(backup, scratch / "lab.db")
        database = dbmod.Database(str(scratch / "lab.db"))
        try:
            runs = database.runs()
            last_id = database.meta("last_run_id")
        finally:
            database.close()
        log(f"  restored {backup.name}: {len(runs)} runs listed, "
            f"last run id {last_id}; opened and migrated by the panel's own code")
        return True
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --- the command ---------------------------------------------------------------


def _age(seconds: float) -> str:
    hours = seconds / 3600
    return f"{hours:.1f} h" if hours < 48 else f"{hours / 24:.1f} days"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="prepare a folder as a destination")
    p.add_argument("folder")
    p.add_argument("--db", default=DEFAULT_DB)

    p = sub.add_parser("prune", help="apply retention to a destination here")
    p.add_argument("folder")
    p.add_argument("--keep", default=",".join(map(str, DEFAULT_KEEP)))

    p = sub.add_parser("make", help="back up the database now")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--dest", action="append", default=[],
                   help="a folder (a USB stick), or host:folder (repeatable)")
    p.add_argument("--chamber-id", default="chamber-1")
    p.add_argument("--keep", default=",".join(map(str, DEFAULT_KEEP)),
                   help="daily,weekly,monthly backups to keep (14,8,12)")

    p = sub.add_parser("verify", help="check every backup in a folder")
    p.add_argument("folder")
    p.add_argument("--max-age-h", type=float, default=MAX_AGE_H)

    p = sub.add_parser("restore", help="restore one backup to a database path")
    p.add_argument("backup")
    p.add_argument("--to", required=True)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("drill", help="restore into scratch and open it")
    p.add_argument("backup", nargs="?")
    p.add_argument("--folder", help="the newest good backup in this folder")

    p = sub.add_parser("pull", help="copy a destination here and verify it")
    p.add_argument("source", help="host:folder")
    p.add_argument("folder")

    p = sub.add_parser("status", help="how the last backup went")
    p.add_argument("--db", default=DEFAULT_DB)

    args = parser.parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except BackupError as exc:
        print(f"backup: {exc}", file=sys.stderr)
        return 1


def _keep(text: str) -> tuple[int, int, int]:
    try:
        keep = tuple(int(x) for x in text.split(","))
    except ValueError:
        keep = ()
    if len(keep) != 3 or min(keep) < 1:
        raise BackupError("--keep is daily,weekly,monthly, each at least 1")
    return keep


def _init(args) -> int:
    folder = Path(args.folder).expanduser()
    init(folder, Path(args.db).expanduser())
    print(f"{folder} is ready for backups")
    return 0


def _prune(args) -> int:
    keep = _keep(args.keep)
    for folder in _folders(Path(args.folder).expanduser()):
        for removed in prune(folder, time.time(), keep):
            print(f"  removed {removed}")
    return 0


def _make(args) -> int:
    keep = _keep(args.keep)
    db = Path(args.db).expanduser()
    dests = [d if is_remote(d) else os.path.expanduser(d) for d in args.dest]
    print(f"Backing up {db}")
    record = make(db, dests, args.chamber_id, keep)
    try:
        write_status(db, record)
    except OSError as exc:
        print(f"backup: could not record the status beside {db}: {exc}",
              file=sys.stderr)
    if record["ok"]:
        print(f"Backed up to {len(record['destinations'])} destination(s): "
              f"{record['name']}")
        return 0
    print(f"backup: FAILED -- {record['error']}", file=sys.stderr)
    return 1


def _folders(root: Path) -> list[Path]:
    """`root` itself, or each chamber folder under it."""
    if backups_in(root):
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []


def _verify(args) -> int:
    root = Path(args.folder).expanduser()
    worst, newest = 0, None
    for folder in _folders(root):
        for backup in backups_in(folder):
            problems = verify_backup(backup)
            stamp = created(backup)
            if problems:
                worst = 1
                print(f"  BAD   {backup}: " + "; ".join(problems))
                continue
            print(f"  ok    {backup}")
            if stamp is not None and (newest is None or stamp > newest):
                newest = stamp
    if newest is None:
        print(f"No good backup in {root}")
        return 1
    age = time.time() - newest
    print(f"Newest good backup: {_age(age)} old")
    if age > args.max_age_h * 3600:
        print(f"That is older than {args.max_age_h:.0f} h")
        return 1
    return worst


def _restore(args) -> int:
    restore(Path(args.backup).expanduser(), Path(args.to).expanduser(),
            args.force)
    print(f"Restored to {args.to}")
    return 0


def _drill(args) -> int:
    if args.backup:
        backup = Path(args.backup).expanduser()
    elif args.folder:
        good = [b for f in _folders(Path(args.folder).expanduser())
                for b in backups_in(f) if not verify_backup(b)]
        if not good:
            raise BackupError(f"no good backup in {args.folder}")
        backup = max(good, key=lambda b: created(b) or 0)
    else:
        raise BackupError("name a backup, or --folder")
    return 0 if drill(backup) else 1


def _pull(args) -> int:
    if not is_remote(args.source):
        raise BackupError("the source is host:folder")
    folder = Path(args.folder).expanduser()
    _private_dir(folder)
    _rsync([args.source.rstrip("/") + "/", str(folder) + "/"])
    print(f"Copied {args.source} to {folder}")
    return _verify(argparse.Namespace(folder=str(folder), max_age_h=MAX_AGE_H))


def _status(args) -> int:
    status = read_status(Path(args.db).expanduser())
    if not status:
        print("No backup has been recorded for this database.")
        return 1
    now = time.time()
    success = status.get("last_success")
    attempt = status.get("last_attempt")
    if not isinstance(attempt, (int, float)) or (
            success is not None and not isinstance(success, (int, float))):
        print("The backup status beside the database is not readable.")
        return 1
    print("Last attempt: " + _age(now - attempt) + " ago, "
          + ("worked" if status.get("last_ok") else
             f"FAILED: {status.get('last_error')}"))
    print("Last good backup: " + (f"{_age(now - success)} ago "
                                  f"({status.get('last_name')})"
                                  if success else "never"))
    return 0 if success and now - success <= MAX_AGE_H * 3600 \
        and status.get("last_ok") else 1


COMMANDS = {"init": _init, "prune": _prune, "make": _make, "verify": _verify,
            "restore": _restore, "drill": _drill, "pull": _pull,
            "status": _status}


if __name__ == "__main__":
    raise SystemExit(main())
