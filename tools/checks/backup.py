"""Backups: a consistent, private, checked copy that keeps the accounts, kept
by the calendar, restorable -- and refused or reported when it is not."""

from __future__ import annotations

import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from lab import db as dbmod
from tools import backup as backupmod
from tools.checks.common import attempt, check

DAY = 86400.0


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _database(folder: Path, runs: int = 3) -> Path:
    path = folder / "lab.db"
    database = dbmod.Database(str(path))
    try:
        for n in range(runs):
            run = database.create_run("pumpdown", f"run {n}", {}, operator="me")
            database.add_sample(run["id"], {"t": 0.0, "at": 0.0, "p1": 1.0e-3})
        database._db.execute(
            "INSERT INTO users (uuid, username, password_hash, status, "
            "created_at, updated_at) "
            "VALUES ('u-1', 'someone', 'scrypt$x', 'active', 0, 0)")
        database._db.commit()
    finally:
        database.close()
    return path


def test_backup() -> None:
    print("\nBackups")
    root = Path(tempfile.mkdtemp(prefix="selftest-backup-"))
    try:
        _sections(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _sections(root: Path) -> None:
    data = root / "data"
    data.mkdir()
    db = _database(data)
    (data / "equipment.json").write_text('{"gate": true}\n')
    quiet = lambda *a, **k: None                                  # noqa: E731
    for stick in ("usb1", "usb2", "usb3", "usb4", "busy"):
        backupmod.init(root / stick, log=quiet)

    # --- one backup, two sticks ----------------------------------------------
    def two_sticks():
        record = backupmod.make(db, [str(root / "usb1"), str(root / "usb2")],
                                "chamber-1", log=quiet)
        landed = [Path(d["path"]) for d in record["destinations"] if d["ok"]]
        good = [b for b in landed if not backupmod.verify_backup(b)]
        # The copy is made on the first stick, never in /tmp (RAM on a Pi),
        # and nothing of the making is left there.
        left = [p.name for d in ("usb1", "usb2")
                for p in (root / d / "chamber-1").glob(".*")]
        return (record["ok"] and len(good) == 2 and not left,
                f"{len(good)} of 2 verified; left behind: {left or 'nothing'}; "
                f"{record.get('error')}")
    attempt("a backup reaches every destination and verifies there", two_sticks)

    first = backupmod.backups_in(root / "usb1" / "chamber-1")[0]
    tables = backupmod.read_manifest(first)["tables"]
    check("a backup keeps the accounts, unlike the download",
          tables.get("users") == 1 and tables.get("runs") == 3, str(tables))
    modes = [_mode(root / "usb1" / "chamber-1"), _mode(first)] + \
        [_mode(p) for p in first.iterdir()]
    check("it is as private as the database: folders 0700, files 0600",
          modes[:2] == [0o700, 0o700] and set(modes[2:]) == {0o600},
          " ".join(oct(m) for m in modes))
    check("the rig's equipment file goes with it",
          (first / "equipment.json").read_text() == '{"gate": true}\n')
    status = backupmod.read_status(db)
    check("no status is written by make() itself, only by the command",
          status is None)

    # --- consistent while the panel writes -------------------------------------
    def while_writing():
        stop = threading.Event()
        written = []

        def writer():
            database = dbmod.Database(str(db))
            run = database.create_run("hold", "busy", {}, operator="me")
            while not stop.is_set():
                database.add_sample(run["id"], {"t": float(len(written)), "at": 0.0,
                                                "p1": 1.0e-4})
                written.append(1)
            database.close()
        thread = threading.Thread(target=writer, daemon=True)
        thread.start()
        time.sleep(0.2)
        record = backupmod.make(db, [str(root / "busy")], "chamber-1",
                                log=quiet)
        stop.set()
        thread.join(timeout=10)
        made = Path(record["destinations"][0]["path"])
        return (record["ok"] and not backupmod.verify_backup(made)
                and len(written) > 0,
                f"{len(written)} samples written during it; "
                f"{record.get('error')}")
    attempt("a backup taken while the panel writes is consistent", while_writing)

    def wal_included():
        database = dbmod.Database(str(db))
        run = database.create_run("hold", "uncheckpointed", {}, operator="me")
        target = Path(tempfile.mkdtemp(prefix="wal-", dir=root))
        try:
            made = backupmod.snapshot(db, target, "chamber-1", time.time())
        finally:
            database.close()
        copy = sqlite3.connect(made / "lab.db")
        try:
            found = copy.execute("SELECT COUNT(*) FROM runs WHERE id=?",
                                 (run["id"],)).fetchone()[0]
        finally:
            copy.close()
        return found == 1, "the run still only in the write-ahead log is " \
            + ("in the copy" if found else "missing from the copy")
    attempt("what is still in the write-ahead log is in the backup", wal_included)

    # --- what goes wrong is reported, never passed off as a backup -------------
    def one_destination_fails():
        blocked = root / "not-a-folder"
        blocked.write_text("a file where the stick should be")
        record = backupmod.make(db, [str(blocked), str(root / "usb3")],
                                "chamber-1", log=quiet)
        results = [d["ok"] for d in record["destinations"]]
        return (not record["ok"] and results == [False, True]
                and "not-a-folder" in (record.get("error") or ""),
                f"{results}: {(record.get('error') or '')[:70]}")
    attempt("a destination that cannot be written fails the backup, and the "
            "others still get theirs", one_destination_fails)

    def unmounted_stick():
        # A mount point with nothing mounted on it: a plain, empty folder.
        bare = root / "mnt" / "vacuum-backup"
        bare.mkdir(parents=True)
        record = backupmod.make(db, [str(bare / "vacuum-lab")], "chamber-1",
                                log=quiet)
        written = [str(p) for p in (root / "mnt").rglob("*")
                   if p != bare] + [p.name for p in db.parent.glob(".work-*")]
        return (not record["ok"] and not written
                and "mounted" in (record.get("error") or ""),
                (record.get("error") or "")[-70:]
                + (f"; wrote {written}" if written else ""))
    attempt("an unmounted stick is refused, and nothing is written in its "
            "place on the system disk", unmounted_stick)

    def status_keeps_last_success():
        good = backupmod.make(db, [str(root / "usb4")], "chamber-1",
                              now=time.time() - 3600, log=quiet)
        backupmod.write_status(db, good)
        bad = backupmod.make(db, [], "chamber-1", log=quiet)
        backupmod.write_status(db, bad)
        status = backupmod.read_status(db)
        return (status["last_ok"] is False
                and status["last_success"] == good["attempted"]
                and _mode(backupmod.status_path(db)) == 0o600,
                f"last ok {status['last_ok']}, last success kept "
                f"{status.get('last_success') == good['attempted']}")
    attempt("the status records a failure without losing the last success",
            status_keeps_last_success)

    def damage_found():
        copy = backupmod.backups_in(root / "usb2" / "chamber-1")[0]
        with open(copy / "lab.db", "r+b") as fh:
            fh.seek(200)
            byte = fh.read(1)
            fh.seek(200)
            fh.write(bytes([byte[0] ^ 0xFF]))
        problems = backupmod.verify_backup(copy)
        try:
            backupmod.restore(copy, root / "restored.db")
            restored = True
        except backupmod.BackupError:
            restored = False
        return (problems and not restored and not (root / "restored.db").exists(),
                "; ".join(problems)[:80])
    attempt("a damaged backup fails verification and is never restored",
            damage_found)

    def unfinished_ignored():
        folder = root / "usb1" / "chamber-1"
        half = folder / "lab-20990101-000000"
        half.mkdir()
        (half / "lab.db").write_bytes(b"half a copy")
        listed = half in backupmod.backups_in(folder)
        return not listed, "listed" if listed else "not a backup"
    attempt("a copy without its manifest is never counted as a backup",
            unfinished_ignored)

    def restore_guards():
        good = backupmod.backups_in(root / "usb1" / "chamber-1")[0]
        target = root / "live" / "lab.db"
        target.parent.mkdir()
        target.write_bytes(b"the panel's database")
        try:
            backupmod.restore(good, target)
            over_live = True
        except backupmod.BackupError:
            over_live = False
        fresh = root / "fresh" / "lab.db"
        backupmod.restore(good, fresh)
        return (not over_live and target.read_bytes() == b"the panel's database"
                and _mode(fresh) == 0o600
                and not backupmod.verify_backup(good),
                "refused over an existing database; restored elsewhere 0600")
    attempt("a restore never overwrites a database unasked", restore_guards)

    def drill():
        good = backupmod.backups_in(root / "usb1" / "chamber-1")[0]
        opened = backupmod.drill(good, log=quiet)
        left = [p.name for p in good.parent.glob(".drill-*")]
        return opened and not left, good.name + (f"; left {left}" if left else "")
    attempt("the restore drill opens a backup with the panel's own code, on "
            "the stick, and tidies up", drill)

    def restore_after_stopped_panel():
        # The panel stopped, then backed up: the read-only look leaves an
        # empty log and its index beside the database.
        stopped = root / "stopped"
        stopped.mkdir()
        live = _database(stopped, runs=1)
        backupmod.init(root / "usb5", log=quiet)
        record = backupmod.make(live, [str(root / "usb5")], "chamber-1",
                                log=quiet)
        leftovers = [p.name for p in stopped.iterdir() if p.name.endswith(
            ("-wal", "-shm"))]
        made = Path(record["destinations"][0]["path"])
        try:
            backupmod.restore(made, live, force=True)
            restored = True
        except backupmod.BackupError as exc:
            restored = str(exc)
        return restored is True, (f"left {leftovers}; restore: "
                                  f"{'worked' if restored is True else restored}")
    attempt("a backup of a stopped panel's database can be restored over it",
            restore_after_stopped_panel)

    # --- keeping by the calendar -----------------------------------------------
    now = time.mktime((2026, 10, 12, 18, 0, 0, 0, 0, -1))
    stamps = {Path(f"b{n}"): now - n * 6 * 3600 for n in range(4 * 400)}
    kept = backupmod.to_keep(stamps, now)
    days = {int((now - stamps[p]) // DAY) for p in kept}
    recent = sum(1 for p in kept if now - stamps[p] <= 48 * 3600)
    check("retention keeps everything from the last 48 h",
          recent == 9, f"{recent} kept")
    check("... one a day for 14 days, and no more than 14 + 8 + 12 + 48 h",
          all(d in days for d in range(14)) and len(kept) <= 9 + 14 + 8 + 12,
          f"{len(kept)} kept")
    oldest = max(now - stamps[p] for p in kept) / DAY
    check("... and nothing past twelve months", 300 < oldest < 366,
          f"oldest kept {oldest:.0f} days")

    def prune_for_real():
        folder = root / "cal" / "chamber-1"
        folder.mkdir(parents=True)
        source = backupmod.backups_in(root / "usb1" / "chamber-1")[0]
        day = 86400.0
        now = time.time()
        made = {}
        for age_days in (0, 1, 2, 3, 20, 21, 22, 200, 400, 401):
            stamp = now - age_days * day - 3600
            name = "lab-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(stamp))
            copy = folder / name
            shutil.copytree(source, copy)
            manifest = backupmod.read_manifest(copy)
            manifest["created"] = stamp
            backupmod._write_json(copy / backupmod.MANIFEST, manifest)
            made[age_days] = copy
        # One damaged: its database cut short. Left for a person, never
        # judged by the calendar.
        with open(made[400] / "lab.db", "r+b") as fh:
            fh.truncate(100)
        removed = backupmod.prune(folder, now)
        gone = sorted(age for age, path in made.items() if path in removed)
        group = [age for age in (20, 21, 22) if made[age].exists()]
        return (not set(gone) & {0, 1, 2, 3, 200} and 401 in gone
                and group == [20] and made[400].exists(),
                f"removed the backups {gone} days old; of 20-22 kept {group}; "
                f"kept the damaged one: {made[400].exists()}")
    attempt("pruning removes what the calendar does not keep, and nothing it "
            "cannot judge", prune_for_real)

    def remote_destination():
        # ssh and rsync stand-ins on PATH: the "remote" is a folder here.
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "ssh").write_text(
            "#!/bin/sh\nwhile [ \"$1\" = -o ]; do shift 2; done\nshift\n"
            "exec sh -c \"$*\"\n")
        real_rsync = shutil.which("rsync")
        (bin_dir / "rsync").write_text(
            f"#!{sys.executable}\nimport os, sys\n"
            f"args = [a.split(':', 1)[1] if a.startswith('far:') else a "
            f"for a in sys.argv[1:]]\n"
            f"os.execv({real_rsync!r}, [{real_rsync!r}] + args)\n")
        for tool in ("ssh", "rsync"):
            (bin_dir / tool).chmod(0o755)
        far = root / "far" / "vacuum-lab"
        backupmod.init(far, log=quiet)
        saved = os.environ["PATH"]
        os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved}"
        try:
            record = backupmod.make(db, [f"far:{far}"], "chamber-1", log=quiet)
        finally:
            os.environ["PATH"] = saved
        landed = backupmod.backups_in(far / "chamber-1")
        partial = list((far / "chamber-1").glob(".partial-*"))
        return (record["ok"] and len(landed) == 1 and not partial
                and not backupmod.verify_backup(landed[0])
                and _mode(far / "chamber-1") == 0o700,
                f"{record.get('error') or 'delivered'}; {len(landed)} there")
    if shutil.which("rsync"):
        attempt("a backup to another machine lands complete, or not at all",
                remote_destination)
    else:
        print("  note  rsync is not installed here: a backup to another "
              "machine is not checked")

    def clock_behind():
        folder = root / "usb1" / "chamber-1"
        before = backupmod.backups_in(folder)
        notes = []
        removed = backupmod.prune(folder, time.time() - 400 * DAY,
                                  log=notes.append)
        after = backupmod.backups_in(folder)
        return (not removed and before == after and notes,
                notes[0][:70] if notes else "pruned anyway")
    attempt("with the clock behind the newest backup nothing is pruned",
            clock_behind)
