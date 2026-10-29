#!/usr/bin/env python3
"""Ship the panel to the machine that runs the rig -- checked, reversible.

    ./tools/release.py check                     pre-flight only, here
    ./tools/release.py deploy --to USER@HOST     the whole release
    ./tools/release.py status --to USER@HOST     what runs there, what is kept
    ./tools/release.py rollback --to USER@HOST   back to the release before
    ./tools/release.py tag [-m NOTES]            sign a version for the lab PC
                                                 to find (tools/autoupdate.py)

A release is the tracked files (`git ls-files`) and a `VERSION.json` saying
which commit they are, copied to `<base>/releases/<id>/` on the target; the
service runs `<base>/current`, a symlink switched in one step. A file git
does not track travels only if it is new in a folder git tracks (a release
stamped "dirty" is what is on disk); anything else -- a person's notes at
the top of the tree, a folder git knows nothing of -- never does.

`deploy`, in order -- stopping at the first thing that fails:

 1. pre-flight here: every file parses as Python 3.11 to 3.14 (and no
    f-string uses what only 3.12 reads), the selftest and the browser test
    pass, the tree is clean -- or the release is stamped "dirty". What is
    copied must be byte for byte what was checked: a file edited, added or
    removed while the tests ran stops the deploy before anything is sent;
 2. the rig is idle: no run going and no power-down under way, read from
    the panel's own status file (`/run/vacuum-lab/status.json`);
 3. the release is copied over and its selftest run on the target's own
    Python (there is no hardware in it, so it costs nothing but time);
 4. a backup is made (`tools/backup.py`, through the vacuum-lab-backup unit
    -- or its old name, tc600-backup -- when it is installed);
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

Until 2026-10 the units were called tc600-web and tc600-backup. A machine
that still has them is moved over by `--install-units`: the new units are
installed and enabled, the old ones disabled, and one `systemctl restart`
of the new panel stops the old one first -- the new unit `Conflicts=` the
old and is ordered after it, so systemd never runs both, and no moment
passes with neither. The old unit files are removed once the new panel is
healthy (and the old one is not running), or put back and started again if
it is not. Drop-ins beside the old units stop the move: they would be lost.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
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
#: How long after a restart before a release may be called healthy: the
#: instruments are opened and found in the first seconds, and the alarm for
#: one that never answers takes 15 s. A third of the wait, at most this.
HEALTH_SETTLE_S = 20.0
DEFAULT_BASE = "~/vacuum-lab"
DEFAULT_STATUS = "/run/vacuum-lab/status.json"
DEFAULT_BACKUP_DEST = "/mnt/vacuum-backup/vacuum-lab"
SERVICE = "vacuum-lab-web"
BACKUP_TIMER = "vacuum-lab-backup.timer"
#: tools/autoupdate.py: the check every 15 minutes, and the install an
#: admin asks for from the panel.
UPDATE_TIMER = "vacuum-lab-update.timer"
INSTALL_PATH = "vacuum-lab-install.path"
#: Units enabled -- and started -- once installed, besides the panel's own.
ENABLED = (BACKUP_TIMER, UPDATE_TIMER, INSTALL_PATH)
#: Started by those, not enabled: stopped before a rollback removes them.
TRIGGERED = ("vacuum-lab-backup.service", "vacuum-lab-update.service",
             "vacuum-lab-install.service")
#: Installed file -> its source in a release, compared before switching.
UNITS = {
    "/etc/systemd/system/vacuum-lab-web.service": "deploy/vacuum-lab-web.service",
    "/etc/systemd/system/vacuum-lab-backup.service": "deploy/vacuum-lab-backup.service",
    "/etc/systemd/system/vacuum-lab-backup.timer": "deploy/vacuum-lab-backup.timer",
    "/etc/systemd/system/vacuum-lab-update.service": "deploy/vacuum-lab-update.service",
    "/etc/systemd/system/vacuum-lab-update.timer": "deploy/vacuum-lab-update.timer",
    "/etc/systemd/system/vacuum-lab-install.service": "deploy/vacuum-lab-install.service",
    "/etc/systemd/system/vacuum-lab-install.path": "deploy/vacuum-lab-install.path",
    "/etc/udev/rules.d/99-vacuum-lab.rules": "deploy/99-vacuum-lab.rules",
}
#: The units' names until 2026-10, which `--install-units` moves a machine
#: off (see the top of this file).
OLD_SERVICE = "tc600-web"
OLD_BACKUP_TIMER = "tc600-backup.timer"
OLD_UNITS = (
    "/etc/systemd/system/tc600-web.service",
    "/etc/systemd/system/tc600-backup.service",
    "/etc/systemd/system/tc600-backup.timer",
)
#: Every SSH connection to the target, rsync's too. The keep-alive is for the
#: selftest there: 13 minutes without a byte of output, and on 2026-10-28 the
#: campus network dropped the idle connection -- the selftest passed, the
#: deploy never heard it. Two minutes without an answer is a lost link.
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
       "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=6"]


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
    """Git is at hand and `root` is in a work tree: its own, or the one just
    above it -- the panel is the `control/` folder of the repository, and git
    run there sees that folder only."""
    root = Path(root)
    return shutil.which("git") is not None and any(
        (folder / ".git").exists() for folder in (root, root.parent))


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


def fingerprint(root: Path, files: list[str]) -> str:
    """One hash over the release's file names and contents."""
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode() + b"\0")
        digest.update((Path(root) / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def preflight(root: Path = ROOT, tests: bool = True, log=print) -> dict:
    files = tracked_files(root)
    # Taken before anything is checked: the release must be these bytes.
    # The tests take ten minutes, and a tree edited meanwhile would ship
    # what was never tested under a stamp saying it was clean.
    checked = fingerprint(root, files)
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
    return {"files": files, "fingerprint": checked, **state}


def stage(root: Path, files: list[str], state: dict, out: Path) -> str:
    """The release's files and VERSION.json in `out/<id>`; returns the id."""
    root = Path(root)
    release = (time.strftime("%Y%m%d-%H%M", time.gmtime()) + "-" + state["short"]
               + ("-dirty" if state["dirty"] else ""))
    folder = out / release
    folder.mkdir(parents=True)
    changed = ReleaseError("files changed while the pre-flight ran: nothing was "
                           "sent. Deploy again from a tree nobody is editing "
                           "(a separate `git worktree` of the commit).")
    try:
        for name in files:
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / name, target)
    except OSError:
        # A file removed, or made unreadable, since it was checked.
        shutil.rmtree(folder, ignore_errors=True)
        if state.get("fingerprint"):
            raise changed from None
        raise
    # The copy, not the tree, is compared: what goes is what was checked.
    if state.get("fingerprint") and (
            fingerprint(folder, files) != state["fingerprint"]
            or sorted(tracked_files(root)) != sorted(files)):
        shutil.rmtree(folder, ignore_errors=True)
        raise changed
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
        self.old_units = list(OLD_UNITS)
        self.sudo = "sudo -n "

    def run(self, command: str, timeout: float = 120.0) -> tuple[int, str]:
        # A link that hangs is a step that failed -- answered like any other,
        # so the deploy still puts back what it changed -- not a traceback
        # that leaves the machine half way.
        try:
            result = subprocess.run(
                [*SSH, self.host, command], capture_output=True, text=True,
                timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"no answer from {self.host} within {timeout:.0f} s"
        return result.returncode, (result.stdout + result.stderr).strip()

    def copy(self, folder: Path, release: str) -> None:
        destination = f"{self.host}:{self.base}/releases/{release}/"
        try:
            result = subprocess.run(
                ["rsync", "-a", "--mkpath", "-e", shlex.join(SSH),
                 f"{folder}/", destination],
                capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            raise ReleaseError("copying the release: no progress in 600 s")
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
        if state.get("by_hand"):
            return (f"the last run left {state['by_hand']} step(s) to do by hand "
                    f"that nobody has confirmed: a restart would forget them")
        if state.get("update_hold"):
            return ("an install an admin asked for in the panel is under way "
                    "(tools/autoupdate.py)")
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


def old_units_installed(target: Target) -> list[str]:
    """The units still installed under their names before 2026-10."""
    return [installed for installed in target.old_units
            if target.run(f"test -e {shlex.quote(installed)}")[0] == 0]


def old_dropins(target: Target, old: list[str]) -> list[str]:
    """Drop-in folders (`<unit>.d/`) beside the units under their old names:
    local changes a move to the new names would leave behind."""
    return [f"{installed}.d" for installed in old
            if target.run(f"test -d {shlex.quote(installed + '.d')}")[0] == 0]


def panel_service(target: Target) -> str:
    """The unit the panel runs under there: the new name, or the old one on
    a machine not moved over yet."""
    web = next(i for i in target.units if Path(i).name == f"{SERVICE}.service")
    code, _ = target.run(f"test -e {shlex.quote(web)}")
    return SERVICE if code == 0 else OLD_SERVICE


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
    for installed in old_units_installed(target):
        differ.append(f"{installed} is the unit's old name, to be replaced")
    return differ


def units_missing(target: Target) -> list[str]:
    """The release's units that are not installed at all and that
    `unit_differences` lets pass -- the backup and update timers on a
    machine that never had them. `--install-units` installs them; a deploy
    without it, and the lab PC's own installer, go ahead without them.
    Asked in one go, so a link that fails is an error, not "missing"."""
    optional = [installed for installed, source in target.units.items()
                if not source.endswith(("web.service", ".rules"))]
    if not optional:
        return []
    code, out = target.run("for f in " + " ".join(map(shlex.quote, optional))
                           + '; do test -e "$f" || echo "$f"; done')
    if code != 0:
        raise ReleaseError(f"checking which units are installed: {out}")
    absent = set(out.split())
    return [installed for installed in optional if installed in absent]


def _reload(target: Target) -> None:
    for command in ("systemctl daemon-reload", "udevadm control --reload-rules"):
        code, out = target.run(target.sudo + command)
        if code != 0:
            raise ReleaseError(f"{command}: {out}")


def units_saved(target: Target, release: str) -> str:
    """Where the units a release replaces are kept until it proves itself."""
    return f"{target.base}/units-before/{release}"


def install_units(target: Target, release: str, old: list[str] = ()) -> str:
    """Install the release's units and rules, keeping the ones they replace
    in `units_saved()` first; returns that folder. If any step fails, what
    was already installed is put back before the error is raised: a unit
    pointing at a release that never went live would fail the panel's next
    start, and leave nothing watching the rig.

    `old` are the units still installed under their names before 2026-10:
    kept with the others, and disabled -- the old panel goes on running
    until the restart stops it, but would no longer start at boot."""
    saved = units_saved(target, release)
    try:
        return _install_units(target, release, saved, old)
    except ReleaseError as exc:
        unrestored = restore_units(target, saved, old)
        raise ReleaseError(f"{exc}; the units were "
                           + ("put back as they were" if not unrestored else
                              "NOT all put back: " + "; ".join(unrestored)))


def _install_units(target: Target, release: str, saved: str,
                   old: list[str]) -> str:
    folder = f"{target.base}/releases/{release}"
    code, out = target.run(f"mkdir -p -m 700 {_q(saved)}")
    if code != 0:
        raise ReleaseError(f"keeping the installed units: {out}")
    for installed in [*target.units, *old]:
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
    names = {Path(installed).name for installed in old}
    steps = []
    if f"{OLD_SERVICE}.service" in names:
        steps.append((f"systemctl disable {OLD_SERVICE}",
                      f"disabling the old {OLD_SERVICE}"))
    if OLD_BACKUP_TIMER in names:
        steps.append((f"systemctl disable --now {OLD_BACKUP_TIMER}",
                      f"stopping the old {OLD_BACKUP_TIMER}"))
    # Enabled, so the panel comes back after a power cut: a unit installed
    # under a new name starts nowhere until it is.
    steps += [(f"systemctl enable {SERVICE}", "enabling the panel's unit")]
    steps += [(f"systemctl enable --now {name}", f"enabling {name}")
              for name in ENABLED]
    for command, what in steps:
        code, out = target.run(target.sudo + command)
        if code != 0:
            raise ReleaseError(f"{what}: {out}")
    return saved


def restore_units(target: Target, saved: str, old: list[str] = ()) -> list[str]:
    """Undo `install_units`: put back each unit it replaced, and remove each
    it added (stopped first); the units under their old names, `old`, back
    in place and enabled again. What could not be done."""
    failed = []
    for installed in target.units:
        name = Path(installed).name
        code, _ = target.run(f"test -e {_q(saved + '/' + name)}")
        if code == 0:
            command = (f"{target.sudo}install -m 644 {_q(saved + '/' + name)} "
                       f"{shlex.quote(installed)}")
        else:
            if name in TRIGGERED:
                # Its timer or path, enabled with --now, may have started it
                # already: the update check runs within minutes of install.
                code, out = target.run(f"{target.sudo}systemctl stop {name}")
                if code != 0:
                    failed.append(f"stopping {name}: {out}")
                    continue
            if name in (*ENABLED, f"{SERVICE}.service"):
                code, out = target.run(f"{target.sudo}systemctl disable --now {name}")
                if code != 0:
                    # Kept: while the new panel may still run, its unit's
                    # Conflicts= is what stops the old one starting beside it.
                    failed.append(f"stopping {name}: {out}")
                    continue
            command = f"{target.sudo}rm -f {shlex.quote(installed)}"
        code, out = target.run(command)
        if code != 0:
            failed.append(f"{installed}: {out}")
    for installed in old:
        name = Path(installed).name
        code, out = target.run(f"{target.sudo}install -m 644 "
                               f"{_q(saved + '/' + name)} "
                               f"{shlex.quote(installed)}")
        if code != 0:
            failed.append(f"{installed}: {out}")
    try:
        _reload(target)
    except ReleaseError as exc:
        failed.append(str(exc))
    names = {Path(installed).name for installed in old}
    for name, command in ((f"{OLD_SERVICE}.service", f"enable {OLD_SERVICE}"),
                          (OLD_BACKUP_TIMER, f"enable --now {OLD_BACKUP_TIMER}")):
        if name in names:
            code, out = target.run(f"{target.sudo}systemctl {command}")
            if code != 0:
                failed.append(f"systemctl {command}: {out}")
    return failed


def retire_old_units(target: Target, old: list[str]) -> list[str]:
    """Once the panel runs healthy under its new name: the unit files under
    the old names go. Their copies stay in `units_saved()`. What could not
    be done."""
    failed = []
    for installed in old:
        code, out = target.run(f"{target.sudo}rm -f {shlex.quote(installed)}")
        if code != 0:
            failed.append(f"{installed}: {out}")
    try:
        _reload(target)
    except ReleaseError as exc:
        failed.append(str(exc))
    return failed


def healthy(target: Target, release: str | None, before: dict | None,
            status_path: str, wait: float = HEALTH_WAIT_S,
            sleep=time.sleep, service: str = SERVICE,
            gone: str | None = None) -> str | None:
    """None once the service is up running `release` and well, else what is
    wrong. With `release` None (a panel from before releases), only that
    the service is active.

    Well means, `settle` seconds or more after the restart: the new release
    reported, each instrument on the port it was on and answering if it was
    answering before, and no new critical alarm. Judged sooner, a release
    that cannot talk to the pump passed before its first reply was due."""
    started = time.monotonic()
    deadline = started + wait
    settle = min(HEALTH_SETTLE_S, wait / 3)
    problem = "no status yet"
    while time.monotonic() < deadline:
        sleep(3)
        code, out = target.run(f"systemctl is-active {service}")
        if out.strip() != "active":
            problem = f"the service is {out.strip() or 'not running'}"
            continue
        if gone:
            code, out = target.run(f"systemctl is-active {gone}")
            if out.strip() == "active":
                problem = f"{gone} is still running beside {service}"
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
            # Answering *now*, not merely connected: a sampler that hangs after
            # its first replies stays connected while its readings age.
            was = before.get("fresh") or before.get("connected") or {}
            now = state.get("fresh") if "fresh" in before else state.get("connected")
            silent = [key for key, answered in was.items()
                      if answered and not (now or {}).get(key)]
            if silent:
                problem = f"not answering, as it was before: {', '.join(silent)}"
                continue
            if (state.get("critical") or 0) > (before.get("critical") or 0):
                problem = "a new critical alarm is up"
                continue
        if time.monotonic() - started < settle:
            problem = "too soon after the restart to judge"
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
        unit = next((name for name in ("vacuum-lab-backup.service",
                                       "tc600-backup.service")
                     if target.run(f"systemctl cat {name} >/dev/null 2>&1")[0] == 0),
                    None)
        if unit:
            code, out = target.run(f"{target.sudo}systemctl start {unit}",
                                   timeout=1800)
        else:
            code, out = target.run(
                f"cd {_q(folder)} && python3 tools/backup.py make "
                + " ".join(f"--dest {_q(d)}" for d in args.backup_dest),
                timeout=1800)
        if code != 0:
            raise ReleaseError(f"the backup failed (pass --no-backup to deploy "
                               f"without one): {out[-300:]}")

    differ = unit_differences(target, release)
    if differ and not args.install_units:
        raise ReleaseError("the installed units are not the release's:\n  "
                           + "\n  ".join(differ)
                           + "\nreview them, then pass --install-units")
    # A unit that is only missing used to pass unseen, --install-units or
    # not: on 2026-10-28 the four updater units were never installed.
    missing = units_missing(target)
    if missing and args.install_units:
        differ += [f"{installed} is not installed" for installed in missing]
    elif missing:
        log("Not installed, and left so (--install-units adds them): "
            + ", ".join(Path(installed).name for installed in missing))

    # Minutes have passed -- a selftest, a backup: somebody may have
    # started a run meanwhile. Asked again, just before anything changes.
    log("Checking again that it is idle")
    check_idle(target, args, log)

    # Switched before the units are installed: a unit naming `current` must
    # never be in place without it -- the panel crashing in between would be
    # restarted into a folder that does not exist, and nothing would watch
    # the rig. The running panel is not touched by either until the restart.
    previous = current_release(target)
    old = old_units_installed(target)
    # The panel under its old name, as opposed to old backup units left over.
    old_panel = any(Path(o).name == f"{OLD_SERVICE}.service" for o in old)
    dropins = old_dropins(target, old)
    if dropins:
        raise ReleaseError("local changes to the old units would be left "
                           "behind by the move to the new names: "
                           + ", ".join(dropins) + ". Carry them over to the "
                           "new unit names by hand first")
    log(f"Switching {previous or '(a plain copy)'} -> {release}")
    switch(target, release)
    saved = None
    if differ:
        log("Installing the release's units and udev rules"
            + (f", in place of {', '.join(Path(o).name for o in old)}"
               if old else ""))
        try:
            saved = install_units(target, release, old)
        except ReleaseError:
            if previous:
                switch(target, previous)
            else:
                target.run(f"rm -f {_q(target.base + '/current')}")
            raise
    log("Restarting" + (f" under the new name; {OLD_SERVICE} stops first"
                        if old_panel else ""))
    # One systemd transaction even when moving off the old name: the new unit
    # Conflicts= the old and is ordered After= it, so starting it stops the
    # old panel first. Two commands would leave a moment -- a dropped link --
    # with neither running and nothing watching the rig.
    code, out = target.run(f"{target.sudo}systemctl restart {SERVICE}",
                           timeout=150)
    problem = (f"restart failed: {out}" if code != 0 else
               healthy(target, release, before, args.status, wait, sleep,
                       gone=OLD_SERVICE if old_panel else None))
    if not problem:
        log(f"  {release} is running")
        if old:
            left = retire_old_units(target, old)
            log(f"  now {SERVICE}; the old units were removed (copies in "
                f"{saved})" if not left else
                f"  now {SERVICE}, but the old units are NOT all removed: "
                + "; ".join(left))
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
    unrestored = restore_units(target, saved, old) if saved else []
    service = OLD_SERVICE if old_panel else SERVICE
    code, out = target.run(f"{target.sudo}systemctl restart {service}",
                           timeout=150)
    back = (f"restart failed: {out}" if code != 0 else
            healthy(target, previous, before if previous else None,
                    args.status, wait, sleep, service=service))
    what = previous or "the copy the previous units run"
    if back or unrestored:
        raise ReleaseError(
            f"release {release} was not healthy ({problem}); going back to "
            f"{what} did NOT work either: "
            + "; ".join(filter(None, [back] + unrestored))
            + f". Look at the machine now: journalctl -u {service}")
    raise ReleaseError(f"release {release} was rolled back to {what}: {problem}")


# --- a version for the lab PC to find -------------------------------------------------


def signing_key(root: Path = ROOT) -> str | None:
    """The public key git signs tags with (`user.signingkey`, with
    `gpg.format ssh`), as a line of a .pub file; None if git is not set up
    to sign with SSH."""
    def config(key):
        result = subprocess.run(["git", "config", "--get", key], cwd=root,
                                capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else ""
    if config("gpg.format") != "ssh":
        return None
    key = config("user.signingkey")
    if key.startswith("key::"):
        return key[5:].strip()
    path = Path(os.path.expanduser(key)) if key else None
    if path is None or not path.exists():
        return None
    if path.suffix != ".pub" and Path(str(path) + ".pub").exists():
        path = Path(str(path) + ".pub")
    return path.read_text(encoding="utf-8").strip()


def next_tag(root: Path = ROOT, day: str | None = None) -> str:
    """Today's version name: vYYYY.MM.DD, then .2, .3 ... for more that day,
    counting the tags here and on GitHub."""
    day = day or time.strftime("v%Y.%m.%d")
    known = set(_git(root, "tag", "--list", f"{day}*").split())
    remote = subprocess.run(["git", "ls-remote", "--tags", "origin", f"{day}*"],
                            cwd=root, capture_output=True, text=True, timeout=30)
    known |= {line.split("refs/tags/")[-1].removesuffix("^{}")
              for line in remote.stdout.splitlines() if "refs/tags/" in line}
    if day not in known:
        return day
    count = 2
    while f"{day}.{count}" in known:
        count += 1
    return f"{day}.{count}"


def tag(args, log=print, root: Path = ROOT) -> str:
    """Sign a version of `control/` at a commit of main, for the lab PC's
    updater to find once it is pushed; returns its name. Nothing is pushed."""
    # Here, not at the top: it imports this module. Run as a script, this
    # file sees tools/ only, so the folder above goes on the path first.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tools import autoupdate
    repo = Path(root).parent
    commit = _git(root, "rev-parse", "--verify", f"{args.commit}^{{commit}}").strip()
    if not args.not_main:
        main = subprocess.run(["git", "merge-base", "--is-ancestor", commit,
                               "origin/main"], cwd=root)
        if main.returncode != 0:
            raise ReleaseError(f"{commit[:8]} is not on origin/main: a version "
                               f"is made from main, once test has been merged "
                               f"into it (git fetch first; --not-main to sign "
                               f"another commit anyway)")
    key = signing_key(root)
    if not key:
        raise ReleaseError("git is not set up to sign with an SSH key: "
                           "git config gpg.format ssh; git config "
                           "user.signingkey ~/.ssh/YOUR_KEY.pub")
    name = args.name or next_tag(root)
    if not autoupdate.TAG.fullmatch(name):
        raise ReleaseError(f"{name}: a version is named vYYYY.MM.DD[.N]")
    if _git(root, "tag", "--list", name).strip():
        raise ReleaseError(f"{name} exists already")

    archive = subprocess.run(["git", "archive", "--format=tar", commit, "control"],
                             cwd=repo, capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        found, files = autoupdate.digest(tar, strip=0)
    notes = args.message or (Path(args.notes_file).read_text(encoding="utf-8")
                             if args.notes_file else "")
    message = (f"Version {name}\n\n{notes.strip() or 'No notes.'}\n\n"
               f"{autoupdate.DIGEST_LINE} {found}\n")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(message)
    try:
        # Not captured: signing may ask for the key's passphrase.
        made = subprocess.run(["git", "tag", "-s", "-F", fh.name, name, commit],
                              cwd=root)
    finally:
        os.unlink(fh.name)
    if made.returncode != 0:
        raise ReleaseError("git tag -s failed")

    # Checked here exactly as the lab PC will check it.
    raw = subprocess.run(["git", "cat-file", "tag", name], cwd=root,
                         capture_output=True, check=True).stdout
    marker = b"-----BEGIN SSH SIGNATURE-----"
    payload, signature = raw[:raw.find(marker)], raw[raw.find(marker):]
    with tempfile.TemporaryDirectory(prefix="release-tag-") as scratch:
        signers = Path(scratch) / "allowed_signers"
        fields = key.split()
        signers.write_text(f'me namespaces="git" {fields[0]} {fields[1]}\n')
        try:
            autoupdate.verify(payload, signature, signers)
            parsed = autoupdate.parse_tag(payload)
        except autoupdate.UpdateError as exc:
            subprocess.run(["git", "tag", "-d", name], cwd=root,
                           capture_output=True)
            raise ReleaseError(f"the tag did not verify, and was deleted: {exc}")
    if parsed["digest"] != found or parsed["object"] != commit:
        subprocess.run(["git", "tag", "-d", name], cwd=root, capture_output=True)
        raise ReleaseError("the signed tag does not say what was meant; deleted")
    log(f"Signed {name}: {commit[:8]}, {files} files under control/.")
    log(f"Nothing is published yet. To offer it to the lab PC:\n"
        f"  git push origin {name}")
    return name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("check", help="pre-flight here, without deploying")
    p.add_argument("--skip-tests", action="store_true")
    p = sub.add_parser("tag", help="sign a version of main for the lab PC's "
                                   "updater (nothing is pushed)")
    p.add_argument("--commit", default="origin/main",
                   help="what to sign (default origin/main)")
    p.add_argument("--name", default=None,
                   help="the version's name (default today's, vYYYY.MM.DD)")
    p.add_argument("-m", "--message", default="",
                   help="release notes: what changed, for the admin who "
                        "installs it")
    p.add_argument("--notes-file", default=None, help="the notes, from a file")
    p.add_argument("--not-main", action="store_true",
                   help="sign a commit that is not on origin/main")
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
        if args.command == "tag":
            tag(args)
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
        service = panel_service(target)
        switch(target, previous)
        code, out = target.run(f"{target.sudo}systemctl restart {service}")
        if code != 0:
            raise ReleaseError(f"restart failed: {out}")
        problem = healthy(target, previous, None, args.status, service=service)
        if problem:
            raise ReleaseError(f"switched back to {previous}, but: {problem}")
        print(f"Rolled back to {previous}")
        return 0
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
