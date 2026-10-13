#!/usr/bin/env python3
"""Ship the panel to the machine that runs the rig -- checked, reversible.

    ./tools/release.py check                     pre-flight only, here
    ./tools/release.py deploy --to USER@HOST     the whole release
    ./tools/release.py status --to USER@HOST     what runs there, what is kept
    ./tools/release.py rollback --to USER@HOST   back to the release before

A release is the tracked files (`git ls-files`) and a `VERSION.json` saying
which commit they are, copied to `<base>/releases/<id>/` on the target; the
service runs `<base>/current`, a symlink switched in one step. A file git
does not track travels only if it is new in a folder git tracks (a release
stamped "dirty" is what is on disk); anything else -- a person's notes at
the top of the tree, a folder git knows nothing of -- never does.

`deploy`, in order -- stopping at the first thing that fails:

 1. pre-flight here: every file parses as Python 3.11 to 3.14 (and no
    f-string uses what only 3.12 reads), the selftest and the browser test
    pass, the tree is clean -- or the release is stamped "dirty";
 2. the rig is idle: no run going and no power-down under way, read from
    the panel's own status file (`/run/vacuum-lab/status.json`);
 3. the release is copied over and its selftest run on the target's own
    Python (there is no hardware in it, so it costs nothing but time);
 4. a backup is made (`tools/backup.py`, through the tc600-backup unit when
    it is installed);
 5. the installed systemd units and udev rules are compared with the
    release's `deploy/`: a difference stops the deploy, unless
    `--install-units` copies them in;
 6. `current` is switched, the service restarted, and within 60 s it must be
    running the new release, with both instruments on the ports they were
    on and no new critical alarm -- otherwise `current` is switched back and
    the service restarted on the release before;
 7. all but the five newest releases are removed (never `current`, never
    the one before it).

Nothing here needs a password on the target but `sudo` for systemctl,
which is passwordless on the lab PC. The first release onto a machine that
still runs from a plain copy (`~/tc600-control`) needs the new units, which
point at `<base>/current`: run it with `--install-units`.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Python versions every file must parse as: the lab PC runs 3.12, the Pis
#: will run their OS's, the laptop runs 3.14.
VERSIONS = ((3, 11), (3, 12), (3, 13), (3, 14))
KEEP_RELEASES = 5
HEALTH_WAIT_S = 60.0
DEFAULT_BASE = "~/vacuum-lab"
DEFAULT_STATUS = "/run/vacuum-lab/status.json"
DEFAULT_BACKUP_DEST = "/mnt/vacuum-backup/vacuum-lab"
SERVICE = "tc600-web"
#: Installed file -> its source in a release, compared before switching.
UNITS = {
    "/etc/systemd/system/tc600-web.service": "deploy/tc600-web.service",
    "/etc/systemd/system/tc600-backup.service": "deploy/tc600-backup.service",
    "/etc/systemd/system/tc600-backup.timer": "deploy/tc600-backup.timer",
    "/etc/udev/rules.d/99-vacuum-lab.rules": "deploy/99-vacuum-lab.rules",
}


class ReleaseError(Exception):
    pass


# --- pre-flight ---------------------------------------------------------------


def newer_fstrings(source: str) -> list[tuple[int, str]]:
    """F-string syntax only Python 3.12 reads (PEP 701), as (line, what).

    `ast.parse(feature_version=(3, 11))` on a newer Python accepts it, so a
    file can pass that check and still be a SyntaxError on a 3.11 Pi: a
    quote reused inside a replacement field, a backslash or a comment in
    one, or a line break inside one of a single-quoted f-string.
    """
    if not hasattr(tokenize, "FSTRING_START"):
        return []                        # 3.11 itself: ast.parse decides
    found = []
    stack: list[dict] = []               # the f-strings we are inside
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return []
    for token in tokens:
        kind, text, (line, _) = token.type, token.string, token.start
        inside = stack and stack[-1]["depth"] > 0
        if inside and kind in (tokenize.STRING, tokenize.FSTRING_START):
            quote = text.lstrip("rRbBuUfF")
            outer = stack[-1]["quote"]
            if quote.startswith(outer):
                found.append((line, f"the f-string's own quote {outer} reused "
                                    f"inside it"))
            if kind == tokenize.STRING and "\\" in text:
                found.append((line, "a backslash inside an f-string's {}"))
        if inside and kind == tokenize.COMMENT:
            found.append((line, "a comment inside an f-string's {}"))
        if inside and kind == tokenize.NL and len(stack[-1]["quote"]) == 1:
            found.append((line, "a line break inside a single-quoted f-string"))
        if kind == tokenize.FSTRING_START:
            stack.append({"quote": text.lstrip("rRbBuUfF"), "depth": 0})
        elif kind == tokenize.FSTRING_END and stack:
            stack.pop()
        elif kind == tokenize.OP and stack and text in "{}":
            stack[-1]["depth"] += 1 if text == "{" else -1
    return found


def _git(root: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True,
                          text=True, check=True).stdout


def untracked_code(root: Path = ROOT) -> list[str]:
    """New files, not ignored, inside the folders git already tracks."""
    root = Path(root)
    tracked = [n for n in _git(root, "ls-files", "-z").split("\0") if n]
    folders = {name.split("/")[0] for name in tracked if "/" in name}
    others = [n for n in _git(root, "ls-files", "-z", "--others",
                              "--exclude-standard").split("\0") if n]
    return sorted(n for n in others
                  if "/" in n and n.split("/")[0] in folders)


def tracked_files(root: Path = ROOT) -> list[str]:
    """What a release carries: what git tracks, as it is on disk, and the new
    files in the folders it tracks."""
    root = Path(root)
    tracked = [n for n in _git(root, "ls-files", "-z").split("\0") if n]
    return sorted({n for n in tracked + untracked_code(root)
                   if (root / n).is_file()})


def has_git(root: Path = ROOT) -> bool:
    return shutil.which("git") is not None and (Path(root) / ".git").exists()


def code_files(root: Path = ROOT) -> list[str]:
    """The files to check: what a release carries where git is at hand; on
    a machine without it -- the lab PC, a release folder -- the Python
    files on disk."""
    root = Path(root)
    if has_git(root):
        return tracked_files(root)
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        found += [os.path.relpath(os.path.join(folder, n), root)
                  for n in names if n.endswith(".py")]
    return sorted(found)


def parse_problems(files: list[str], root: Path = ROOT) -> list[str]:
    """Every Python file must parse as each supported version."""
    root = Path(root)
    problems = []
    for name in files:
        if not name.endswith(".py"):
            continue
        source = (root / name).read_text(encoding="utf-8")
        for version in VERSIONS:
            try:
                ast.parse(source, name, feature_version=version)
            except SyntaxError as exc:
                problems.append(f"{name}:{exc.lineno}: not Python "
                                f"{version[0]}.{version[1]}: {exc.msg}")
                break
        for line, what in newer_fstrings(source):
            problems.append(f"{name}:{line}: not Python 3.11: {what}")
    return problems


def git_state(root: Path = ROOT) -> dict:
    root = Path(root)
    changed = _git(root, "status", "--porcelain", "--untracked-files=no").strip()
    return {"commit": _git(root, "rev-parse", "HEAD").strip(),
            "short": _git(root, "rev-parse", "--short=8", "HEAD").strip(),
            "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "dirty": bool(changed) or bool(untracked_code(root))}


def run_tests(root: Path, log=print) -> list[str]:
    """The selftest and the browser test, judged by their exit status."""
    failed = []
    for command in (["tools/selftest.py"], ["tools/uitest.py"]):
        log(f"  running {' '.join(command)} ...")
        started = time.monotonic()
        result = subprocess.run([sys.executable, *command], cwd=root,
                                capture_output=True, text=True)
        tail = (result.stdout.strip().splitlines() or ["(no output)"])[-1]
        log(f"    {tail} ({time.monotonic() - started:.0f} s)")
        if result.returncode != 0:
            failed.append(f"{command[0]} failed: {tail}")
    return failed


def preflight(root: Path = ROOT, tests: bool = True, log=print) -> dict:
    files = tracked_files(root)
    state = git_state(root)
    log(f"Release from {state['branch']} at {state['short']}"
        + (" (with uncommitted changes: stamped dirty)" if state["dirty"] else ""))
    problems = parse_problems(files, root)
    log(f"  {sum(n.endswith('.py') for n in files)} Python files parse as "
        f"3.11-3.14" if not problems else f"  {len(problems)} parse problems")
    if tests:
        problems += run_tests(root, log)
    else:
        log("  tests skipped (--skip-tests)")
    if problems:
        raise ReleaseError("pre-flight failed:\n  " + "\n  ".join(problems))
    return {"files": files, **state}


def stage(root: Path, files: list[str], state: dict, out: Path) -> str:
    """The release's files and VERSION.json in `out/<id>`; returns the id."""
    root = Path(root)
    release = (time.strftime("%Y%m%d-%H%M", time.gmtime()) + "-" + state["short"]
               + ("-dirty" if state["dirty"] else ""))
    folder = out / release
    folder.mkdir(parents=True)
    for name in files:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, target)
    (folder / "VERSION.json").write_text(json.dumps({
        "release": release, "commit": state["commit"], "branch": state["branch"],
        "dirty": state["dirty"], "built_utc": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": len(files),
    }, indent=1) + "\n")
    return release


# --- the target -------------------------------------------------------------------


class Target:
    """A machine reached over SSH; `run` returns (status, output).

    `units` maps each installed unit or rule file to its source in a
    release, and `sudo` prefixes what needs root -- both replaceable, so the
    selftest can stand a folder in for the machine.
    """

    def __init__(self, host: str, base: str):
        self.host, self.base = host, base.rstrip("/")
        self.units = dict(UNITS)
        self.sudo = "sudo -n "

    def run(self, command: str, timeout: float = 120.0) -> tuple[int, str]:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             self.host, command], capture_output=True, text=True,
            timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()

    def copy(self, folder: Path, release: str) -> None:
        destination = f"{self.host}:{self.base}/releases/{release}/"
        result = subprocess.run(
            ["rsync", "-a", "--mkpath", f"{folder}/", destination],
            capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise ReleaseError(f"copying the release: {result.stderr.strip()}")


def _q(path: str) -> str:
    """A remote path, quoted, with a leading ~ left for the shell."""
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def rig_state(target: Target, status_path: str) -> tuple[dict | None, float | None]:
    """The panel's status file and the target's own clock, read together:
    the file's age is the target's business, not this machine's -- two
    clocks a minute apart would call a live panel stale, or a dead one
    fresh."""
    code, out = target.run(f"date +%s.%N; cat {_q(status_path)} 2>/dev/null")
    lines = out.split("\n", 1)
    try:
        now = float(lines[0].strip())
    except (ValueError, IndexError):
        now = None
    try:
        state = json.loads(lines[1]) if len(lines) > 1 and lines[1].strip() else None
    except ValueError:
        state = None
    return (state if isinstance(state, dict) else None), now


def idle_problem(state: dict | None, now: float | None, max_age: float = 30.0,
                 force: bool = False) -> str | None:
    """Why the rig must not be restarted now, or None.

    A run, a power-down or a make-safe the panel reports always refuses.
    Not knowing -- no status, or a stale one -- refuses unless `force`,
    which is for the first release onto a machine whose panel writes none.
    """
    if state is not None:
        if state.get("run"):
            run = state["run"]
            return (f"run {run.get('id')} ({run.get('procedure')}) is going: "
                    f"a restart would end it")
        if state.get("shutdown"):
            return "a power-down is under way: a restart would leave it unfinished"
        if state.get("securing"):
            return "the pump is being made safe after a fault"
    if force:
        return None
    if state is None:
        return ("the panel's status file cannot be read, so whether a run "
                "is going is unknown")
    if now is None:
        return "the target's clock could not be read"
    age = now - float(state.get("written") or 0)
    if age > max_age:
        return (f"the panel's status is {age:.0f} s old: the panel is not "
                f"running or not writing it")
    return None


def current_release(target: Target) -> str | None:
    code, out = target.run(f"readlink {_q(target.base + '/current')}")
    return Path(out).name if code == 0 and out else None


def releases_on(target: Target) -> list[str]:
    code, out = target.run(f"ls -1 {_q(target.base + '/releases')} 2>/dev/null")
    return sorted(out.split()) if code == 0 else []


def switch(target: Target, release: str) -> None:
    """Point `current` at `release` in one rename."""
    base = target.base
    code, out = target.run(
        f"cd {_q(base)} && ln -sfn releases/{shlex.quote(release)} current.new "
        f"&& mv -T current.new current")
    if code != 0:
        raise ReleaseError(f"switching current: {out}")


def unit_differences(target: Target, release: str) -> list[str]:
    folder = f"{target.base}/releases/{release}"
    differ = []
    for installed, source in target.units.items():
        code, _ = target.run(f"test -e {shlex.quote(installed)}")
        if code != 0:
            if source.endswith(("web.service", ".rules")):
                differ.append(f"{installed} is not installed")
            continue
        code, out = target.run(f"cmp -s {shlex.quote(installed)} "
                               f"{_q(folder + '/' + source)}")
        if code != 0:
            differ.append(f"{installed} differs from the release's {source}")
    return differ


def _reload(target: Target) -> None:
    for command in ("systemctl daemon-reload", "udevadm control --reload-rules"):
        code, out = target.run(target.sudo + command)
        if code != 0:
            raise ReleaseError(f"{command}: {out}")


def units_saved(target: Target, release: str) -> str:
    """Where the units a release replaces are kept until it proves itself."""
    return f"{target.base}/units-before/{release}"


def install_units(target: Target, release: str) -> str:
    """Install the release's units and rules, keeping the ones they replace
    in `units_saved()` first; returns that folder. If any step fails, what
    was already installed is put back before the error is raised: a unit
    pointing at a release that never went live would fail the panel's next
    start, and leave nothing watching the rig."""
    saved = units_saved(target, release)
    try:
        return _install_units(target, release, saved)
    except ReleaseError as exc:
        unrestored = restore_units(target, saved)
        raise ReleaseError(f"{exc}; the units were "
                           + ("put back as they were" if not unrestored else
                              "NOT all put back: " + "; ".join(unrestored)))


def _install_units(target: Target, release: str, saved: str) -> str:
    folder = f"{target.base}/releases/{release}"
    code, out = target.run(f"mkdir -p -m 700 {_q(saved)}")
    if code != 0:
        raise ReleaseError(f"keeping the installed units: {out}")
    for installed in target.units:
        code, out = target.run(f"if test -e {shlex.quote(installed)}; then "
                               f"cp -p {shlex.quote(installed)} {_q(saved)}/; fi")
        if code != 0:
            raise ReleaseError(f"keeping {installed}: {out}")
    for installed, source in target.units.items():
        code, out = target.run(f"{target.sudo}install -m 644 "
                               f"{_q(folder + '/' + source)} "
                               f"{shlex.quote(installed)}")
        if code != 0:
            raise ReleaseError(f"installing {installed}: {out}")
    _reload(target)
    code, out = target.run(f"{target.sudo}systemctl enable --now "
                           f"tc600-backup.timer")
    if code != 0:
        raise ReleaseError(f"enabling the backup timer: {out}")
    return saved


def restore_units(target: Target, saved: str) -> list[str]:
    """Undo `install_units`: put back each unit it replaced, and remove each
    it added (the backup timer stopped first). What could not be done."""
    failed = []
    for installed in target.units:
        name = Path(installed).name
        code, _ = target.run(f"test -e {_q(saved + '/' + name)}")
        if code == 0:
            command = (f"{target.sudo}install -m 644 {_q(saved + '/' + name)} "
                       f"{shlex.quote(installed)}")
        else:
            if name == "tc600-backup.timer":
                target.run(f"{target.sudo}systemctl disable --now {name}")
            command = f"{target.sudo}rm -f {shlex.quote(installed)}"
        code, out = target.run(command)
        if code != 0:
            failed.append(f"{installed}: {out}")
    try:
        _reload(target)
    except ReleaseError as exc:
        failed.append(str(exc))
    return failed


def healthy(target: Target, release: str | None, before: dict | None,
            status_path: str, wait: float = HEALTH_WAIT_S,
            sleep=time.sleep) -> str | None:
    """None once the service is up running `release` and well, else what is
    wrong. With `release` None (a panel from before releases), only that
    the service is active."""
    deadline = time.monotonic() + wait
    problem = "no status yet"
    while time.monotonic() < deadline:
        sleep(3)
        code, out = target.run(f"systemctl is-active {SERVICE}")
        if out.strip() != "active":
            problem = f"the service is {out.strip() or 'not running'}"
            continue
        if release is None:
            return None
        state, _ = rig_state(target, status_path)
        if not state or state.get("release") != release:
            problem = "the panel has not reported the new release yet"
            continue
        if before:
            moved = {key: (port, state.get("ports", {}).get(key))
                     for key, port in (before.get("ports") or {}).items()
                     if state.get("ports", {}).get(key) != port}
            if moved:
                problem = "instruments are not on the ports they were on: " \
                    + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in moved.items())
                continue
            if (state.get("critical") or 0) > (before.get("critical") or 0):
                problem = "a new critical alarm is up"
                continue
        return None
    return problem


def prune(target: Target, keep: int = KEEP_RELEASES) -> list[str]:
    releases = releases_on(target)
    current = current_release(target)
    protected = {current}
    if current in releases:
        index = releases.index(current)
        if index > 0:
            protected.add(releases[index - 1])
    removed = []
    for release in releases[:-keep]:
        if release in protected:
            continue
        code, _ = target.run(f"rm -rf {_q(target.base + '/releases/' + release)}")
        if code == 0:
            removed.append(release)
    return removed


def check_idle(target: Target, args, log=print) -> dict | None:
    state, now = rig_state(target, args.status)
    problem = idle_problem(state, now, force=args.force_unknown_state)
    if problem:
        raise ReleaseError(f"not now: {problem}")
    unknown = idle_problem(state, now)
    if unknown:
        log(f"  going ahead (--force-unknown-state): {unknown}")
    return state


def deploy(args, log=print, target: Target | None = None,
           wait: float = HEALTH_WAIT_S, sleep=time.sleep,
           info: dict | None = None) -> str:
    """The whole release; returns its id. Raises ReleaseError, having put
    back what it changed, when it cannot finish. `info` stands in for the
    pre-flight (the files and the commit) in the selftest, which also runs
    where there is no git."""
    target = target or Target(args.to, args.base)
    info = info or preflight(ROOT, tests=not args.skip_tests, log=log)

    log(f"Checking {args.to} is idle")
    before = check_idle(target, args, log)

    with tempfile.TemporaryDirectory(prefix="release-") as scratch:
        release = stage(ROOT, info["files"], info, Path(scratch))
        log(f"Copying release {release}")
        target.copy(Path(scratch) / release, release)
    folder = f"{target.base}/releases/{release}"

    if not args.skip_remote_selftest:
        log("Running the selftest on the target's own Python (about 11 min)")
        code, out = target.run(f"cd {_q(folder)} && python3 tools/selftest.py "
                               f"> selftest.log 2>&1; echo exit=$?; "
                               f"tail -1 selftest.log", timeout=1800)
        log(f"  {out.splitlines()[-1] if out else '?'}")
        if "exit=0" not in out:
            raise ReleaseError(f"the selftest failed on {args.to}; the log is "
                               f"{folder}/selftest.log")

    if args.no_backup:
        log("No backup (--no-backup)")
    else:
        log("Backing up first")
        code, _ = target.run("systemctl cat tc600-backup.service >/dev/null 2>&1")
        if code == 0:
            code, out = target.run(f"{target.sudo}systemctl start "
                                   f"tc600-backup.service", timeout=1800)
        else:
            code, out = target.run(
                f"cd {_q(folder)} && python3 tools/backup.py make "
                + " ".join(f"--dest {shlex.quote(d)}" for d in args.backup_dest),
                timeout=1800)
        if code != 0:
            raise ReleaseError(f"the backup failed (pass --no-backup to deploy "
                               f"without one): {out[-300:]}")

    differ = unit_differences(target, release)
    if differ and not args.install_units:
        raise ReleaseError("the installed units are not the release's:\n  "
                           + "\n  ".join(differ)
                           + "\nreview them, then pass --install-units")

    # Minutes have passed -- a selftest, a backup: somebody may have
    # started a run meanwhile. Asked again, just before anything changes.
    log("Checking again that it is idle")
    check_idle(target, args, log)

    # Switched before the units are installed: a unit naming `current` must
    # never be in place without it -- the panel crashing in between would be
    # restarted into a folder that does not exist, and nothing would watch
    # the rig. The running panel is not touched by either until the restart.
    previous = current_release(target)
    log(f"Switching {previous or '(a plain copy)'} -> {release}")
    switch(target, release)
    saved = None
    if differ:
        log("Installing the release's units and udev rules")
        try:
            saved = install_units(target, release)
        except ReleaseError:
            if previous:
                switch(target, previous)
            else:
                target.run(f"rm -f {_q(target.base + '/current')}")
            raise
    log("Restarting")
    code, out = target.run(f"{target.sudo}systemctl restart {SERVICE}", timeout=150)
    problem = (f"restart failed: {out}" if code != 0 else
               healthy(target, release, before, args.status, wait, sleep))
    if not problem:
        log(f"  {release} is running")
        for removed in prune(target):
            log(f"  removed old release {removed}")
        return release

    log(f"  NOT HEALTHY: {problem}")
    if not previous and not saved:
        raise ReleaseError(f"release {release} is NOT healthy and was NOT rolled "
                           f"back: there is nothing to go back to. {problem}")
    if previous:
        log(f"  switching back to {previous}")
        switch(target, previous)
    unrestored = restore_units(target, saved) if saved else []
    code, out = target.run(f"{target.sudo}systemctl restart {SERVICE}",
                           timeout=150)
    back = (f"restart failed: {out}" if code != 0 else
            healthy(target, previous, before if previous else None,
                    args.status, wait, sleep))
    what = previous or "the copy the previous units run"
    if back or unrestored:
        raise ReleaseError(
            f"release {release} was not healthy ({problem}); going back to "
            f"{what} did NOT work either: "
            + "; ".join(filter(None, [back] + unrestored))
            + ". Look at the machine now: journalctl -u tc600-web")
    raise ReleaseError(f"release {release} was rolled back to {what}: {problem}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="pre-flight here, without deploying")
    p.add_argument("--skip-tests", action="store_true")
    for name in ("deploy", "status", "rollback"):
        p = sub.add_parser(name)
        p.add_argument("--to", required=True, help="USER@HOST")
        p.add_argument("--base", default=DEFAULT_BASE)
        p.add_argument("--status", default=DEFAULT_STATUS)
        p.add_argument("--force-unknown-state", action="store_true",
                       help="go ahead when the panel's state cannot be read "
                            "(the first release onto a machine); a run, a "
                            "power-down or a make-safe still refuses")
        if name == "deploy":
            p.add_argument("--skip-tests", action="store_true")
            p.add_argument("--skip-remote-selftest", action="store_true")
            p.add_argument("--install-units", action="store_true")
            p.add_argument("--backup-dest", action="append", default=None,
                           help=f"where the backup before the switch goes "
                                f"(repeatable; default {DEFAULT_BACKUP_DEST})")
            p.add_argument("--no-backup", action="store_true")
    return parser


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    args = build_parser().parse_args(argv)
    if getattr(args, "backup_dest", "absent") is None:
        args.backup_dest = [DEFAULT_BACKUP_DEST]
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    sys.stdout.reconfigure(line_buffering=True)
    try:
        if args.command == "check":
            preflight(ROOT, tests=not args.skip_tests)
            print("Pre-flight passed")
            return 0
        if args.command == "deploy":
            deploy(args)
            return 0
        target = Target(args.to, args.base)
        if args.command == "status":
            current = current_release(target)
            for release in releases_on(target):
                print(("* " if release == current else "  ") + release)
            print(json.dumps(rig_state(target, args.status)[0], indent=1))
            return 0
        releases, current = releases_on(target), current_release(target)
        if current not in releases or releases.index(current) == 0:
            raise ReleaseError("there is no release before the current one")
        previous = releases[releases.index(current) - 1]
        check_idle(target, args)
        switch(target, previous)
        code, out = target.run(f"{target.sudo}systemctl restart {SERVICE}")
        if code != 0:
            raise ReleaseError(f"restart failed: {out}")
        problem = healthy(target, previous, None, args.status)
        if problem:
            raise ReleaseError(f"switched back to {previous}, but: {problem}")
        print(f"Rolled back to {previous}")
        return 0
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
