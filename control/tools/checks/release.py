"""Releases: what is shipped, what stops a deploy, and how one is undone --
against a stand-in for the target machine, so no SSH is needed."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from tools import release as rel
from tools.checks.common import ROOT, attempt, check

QUIET = lambda *a, **k: None                                       # noqa: E731

#: What the pre-flight would hand a deploy -- given, so the deploy checks
#: need no git (the lab PC has none) and copy only what they use.
INFO = {"files": ["webserver.py", "lab/judgement.py",
                  *rel.UNITS.values()],
        "commit": "d" * 40, "short": "dddddddd", "branch": "test",
        "dirty": False}


class LocalTarget(rel.Target):
    """The target, played in a scratch folder: its commands run here, while
    `sudo`, systemctl, its clock and the panel's status file are the test's.
    `before_command` may change the rig as a command arrives -- somebody
    starting a run half way through a deploy, say."""

    def __init__(self, base: Path, units: Path | None = None):
        super().__init__("local", str(base))
        self.sudo = "FAKESUDO "
        self.etc = units
        #: Units systemd is not running; everything else answers "active".
        self.inactive: set[str] = set()
        #: Whether starting one panel unit stops the other (their Conflicts=).
        self.conflicts_work = True
        if units is not None:
            units.mkdir(parents=True, exist_ok=True)
            self.units = {str(units / Path(i).name): s
                          for i, s in rel.UNITS.items()}
            self.old_units = [str(units / Path(i).name) for i in rel.OLD_UNITS]
        self.active = True
        self.status: dict | None = None
        self.skew = 0.0                          # target clock minus ours
        self.restarts = 0
        self.restarted: list[str] = []           # which unit, each time
        self.on_restart = None                   # what the panel does then
        self.before_command = None
        #: A command this returns True for fails, as a refused sudo would.
        self.fail_on = None
        self.commands: list[str] = []

    def clock(self) -> float:
        return time.time() + self.skew

    def run(self, command: str, timeout: float = 120.0) -> tuple[int, str]:
        self.commands.append(command)
        if self.before_command:
            self.before_command(self, command)
        if self.fail_on and self.fail_on(command):
            return 1, "refused (selftest)"
        if command.startswith("date +%s.%N; cat "):
            return 0, f"{self.clock()}\n" + (json.dumps(self.status)
                                             if self.status else "")
        if command.startswith("systemctl is-active"):
            unit = command.split()[-1].removesuffix(".service")
            return 0, ("active" if self.active and unit not in self.inactive
                       else "inactive")
        if command.startswith("systemctl cat"):
            unit = command.split()[2]
            known = self.etc is not None and (self.etc / unit).exists()
            return (0, "") if known else (1, "")
        if command.startswith(self.sudo):
            command = command[len(self.sudo):]
            if command.startswith("systemctl restart"):
                unit = command.split()[-1].removesuffix(".service")
                self.restarts += 1
                self.restarted.append(unit)
                self.inactive.discard(unit)
                other = {rel.SERVICE: rel.OLD_SERVICE,
                         rel.OLD_SERVICE: rel.SERVICE}.get(unit)
                if other and self.conflicts_work:
                    self.inactive.add(other)
                if self.on_restart:
                    self.on_restart(self)
                return 0, ""
            if command.startswith(("systemctl stop", "systemctl disable --now")):
                self.inactive.add(command.split()[-1].removesuffix(".service"))
                return 0, ""
            if command.startswith(("systemctl", "udevadm")):
                return 0, ""
        result = subprocess.run(["bash", "-c", command], capture_output=True,
                                text=True, timeout=timeout)
        return result.returncode, (result.stdout + result.stderr).strip()

    def copy(self, folder: Path, release: str) -> None:
        shutil.copytree(folder, Path(self.base) / "releases" / release)


def deploy_args(**changes) -> SimpleNamespace:
    args = dict(to="lab", base="", status="/run/status.json",
                force_unknown_state=False, skip_tests=True,
                skip_remote_selftest=True, install_units=True,
                backup_dest=[rel.DEFAULT_BACKUP_DEST], no_backup=True)
    args.update(changes)
    return SimpleNamespace(**args)


def reports(release_of):
    """A panel that, once restarted, reports the release `release_of` names."""
    def restarted(target):
        target.status = {"written": target.clock(), "run": None,
                         "release": release_of(target), "ports": {},
                         "critical": 0}
    return restarted


def current_name(target) -> str | None:
    return rel.current_release(target)


def test_release() -> None:
    print("\nReleases")
    folder = Path(tempfile.mkdtemp(prefix="selftest-release-"))
    try:
        _sections(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _sections(folder: Path) -> None:
    # --- what only Python 3.12 reads is caught before it reaches a 3.11 Pi ----
    cases = {
        'x = f"{d["k"]}"\n': True,
        'x = f"{chr(10).join(a)}" + f"{\'\\n\'}"\n': True,
        'x = f"""{a  # why\n}"""\n': True,
        'x = f"{a +\n b}"\n': True,
        'x = f"{d[\'k\']}"\n': False,
        'x = f"""{"k"}"""\n': False,
        'x = f"{v:>{w}.2f} {u!r}"\n': False,
    }
    caught = {source: bool(rel.newer_fstrings(source)) for source in cases}
    check("f-strings only 3.12 reads are caught, and no others",
          caught == cases,
          ", ".join(repr(s[:16]) for s, c in caught.items() if c != cases[s])
          or "all as expected")

    def tree_parses():
        problems = rel.parse_problems(rel.code_files(ROOT), ROOT)
        return not problems, (problems[0] if problems else
                              "every file parses as 3.11 to 3.14")
    attempt("this tree parses as every Python the rig runs", tree_parses)

    def without_git():
        # A release folder, as on the lab PC: no .git beside it.
        copy = folder / "nogit"
        for name in ("webserver.py", "lab/judgement.py"):
            (copy / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(Path(ROOT) / name, copy / name)
        files = rel.code_files(copy)
        return (files == ["lab/judgement.py", "webserver.py"]
                and not rel.parse_problems(files, copy), str(files))
    attempt("the check runs where there is no git -- the lab PC, a release "
            "folder -- on the files on disk", without_git)

    (folder / "old.py").write_text("type Alias = int\n")
    check("a 3.12 statement is caught as not 3.11",
          any("not Python 3.11" in p for p in rel.parse_problems(["old.py"], folder)))

    # --- what goes into a release ------------------------------------------------
    def what_travels():
        repo = folder / "repo"
        (repo / "lab").mkdir(parents=True)
        (repo / "lab" / "kept.py").write_text("x = 1\n")
        (repo / "top.py").write_text("y = 2\n")
        for command in (["init", "-q"], ["add", "."],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit",
                         "-qm", "start"]):
            subprocess.run(["git", *command], cwd=repo, check=True,
                           capture_output=True)
        (repo / "lab" / "new.py").write_text("z = 3\n")       # new code
        (repo / "notes.md").write_text("mine\n")             # a person's own
        (repo / "scratch").mkdir()
        (repo / "scratch" / "try.py").write_text("w = 4\n")  # unknown folder
        files = rel.tracked_files(repo)
        state = rel.git_state(repo)
        return (files == ["lab/kept.py", "lab/new.py", "top.py"]
                and state["dirty"], f"{files}, dirty {state['dirty']}")
    def from_its_folder():
        # The panel is one folder of the repository, beside the tablet and
        # the manuals: a release made there carries that folder and no other.
        repo = folder / "mono"
        (repo / "control" / "lab").mkdir(parents=True)
        (repo / "control" / "lab" / "kept.py").write_text("x = 1\n")
        (repo / "tablet").mkdir()
        (repo / "tablet" / "build.sh").write_text("true\n")
        for command in (["init", "-q"], ["add", "."],
                        ["-c", "user.name=t", "-c", "user.email=t@t", "commit",
                         "-qm", "start"]):
            subprocess.run(["git", *command], cwd=repo, check=True,
                           capture_output=True)
        (repo / "control" / "lab" / "new.py").write_text("z = 3\n")
        (repo / "tablet" / "probe.py").write_text("w = 4\n")
        (repo / "README.md").write_text("mine\n")
        control = repo / "control"
        files = rel.tracked_files(control)
        return (rel.has_git(control)
                and files == ["lab/kept.py", "lab/new.py"],
                f"{files}, git {rel.has_git(control)}")
    if rel.has_git(ROOT):
        attempt("a release carries what git tracks and new code beside it, "
                "never a person's own files", what_travels)
        attempt("a release made in the panel's folder carries that folder "
                "only, not the tablet's or the top of the tree", from_its_folder)
    else:
        # A release on the lab PC: no git there, and nothing to ship from.
        print("  note  no git here: what a release carries is checked where "
              "releases are made")
    out = folder / "staged"
    state = {"commit": "c" * 40, "short": "cccccccc", "branch": "test",
             "dirty": True}
    release = rel.stage(ROOT, ["webserver.py", "lab/judgement.py"], state, out)
    version = json.loads((out / release / "VERSION.json").read_text())
    check("a release says what it is, dirty included",
          release.endswith("-cccccccc-dirty") and version["dirty"] is True
          and (out / release / "lab" / "judgement.py").exists(), release)

    parsed = rel.parse(["deploy", "--to", "lab", "--backup-dest", "far:~/bk"])
    default = rel.parse(["deploy", "--to", "lab"])
    check("--backup-dest replaces the default stick, and the stick is the "
          "default", parsed.backup_dest == ["far:~/bk"]
          and default.backup_dest == [rel.DEFAULT_BACKUP_DEST],
          f"{parsed.backup_dest} / {default.backup_dest}")

    # --- what stops a deploy -------------------------------------------------------
    now = time.time()
    idle = {"written": now - 2, "run": None, "shutdown": None}
    busy = {**idle, "run": {"id": 7, "procedure": "bakeout"}}
    refusals = {
        "no status": rel.idle_problem(None, now),
        "stale": rel.idle_problem({**idle, "written": now - 120}, now),
        "a run": rel.idle_problem(busy, now),
        "a power-down": rel.idle_problem({**idle, "shutdown": {"active": True}},
                                         now),
    }
    check("a deploy is refused with a run going, a power-down under way, or "
          "the rig's state unknown", all(refusals.values()),
          "; ".join(f"{k}: {(v or 'ALLOWED')[:30]}" for k, v in refusals.items()))
    check("... and allowed when the rig is idle",
          rel.idle_problem(idle, now) is None)
    check("forcing past an unknown state never forces past a run going",
          rel.idle_problem(busy, now, force=True)
          and rel.idle_problem(None, now, force=True) is None)

    # --- switching, health, rollback, pruning ------------------------------------
    base = folder / "target"
    target = LocalTarget(base)
    for n in range(7):
        (base / "releases" / f"2026010{n}-0000-aaaa").mkdir(parents=True)
    names = sorted(p.name for p in (base / "releases").iterdir())
    rel.switch(target, names[5])
    check("current is switched to the release",
          rel.current_release(target) == names[5])
    rel.switch(target, names[6])
    check("... and switched again in one step",
          (base / "current").resolve().name == names[6])

    def health():
        target.status = {"written": time.time(), "release": names[6],
                         "ports": {"pump": "/dev/tc600"}, "critical": 0}
        well = rel.healthy(target, names[6], {"ports": {"pump": "/dev/tc600"},
                                              "critical": 0},
                           "/x", wait=5, sleep=lambda s: None)
        target.status["ports"] = {"pump": "/dev/ttyUSB3"}
        moved = rel.healthy(target, names[6], {"ports": {"pump": "/dev/tc600"},
                                               "critical": 0},
                            "/x", wait=0.2, sleep=lambda s: None)
        target.status.update({"ports": {"pump": "/dev/tc600"}, "critical": 1})
        alarm = rel.healthy(target, names[6], {"ports": {"pump": "/dev/tc600"},
                                               "critical": 0},
                            "/x", wait=0.2, sleep=lambda s: None)
        target.status["release"] = names[5]
        old = rel.healthy(target, names[6], None, "/x", wait=0.2,
                          sleep=lambda s: None)
        return (well is None and moved and alarm and old,
                f"moved: {moved}; alarm: {alarm}; old: {old}")
    attempt("the health check wants the new release, the same ports and no "
            "new critical alarm", health)

    rel.prune(target, keep=3)
    left = sorted(p.name for p in (base / "releases").iterdir())
    check("old releases are pruned, never current or the one before it",
          left == names[4:] and names[6] == rel.current_release(target),
          f"kept {left}")

    # --- whole deploys, on the stand-in ----------------------------------------------
    def fresh(name, previous: bool = True, old_unit: str | None = None):
        place = folder / name
        stand_in = LocalTarget(place / "base", units=place / "etc")
        (place / "base" / "releases").mkdir(parents=True)
        if previous:
            (place / "base" / "releases" / "20250101-0000-old").mkdir()
            rel.switch(stand_in, "20250101-0000-old")
        if old_unit is not None:
            (place / "etc" / "vacuum-lab-web.service").write_text(old_unit)
        stand_in.status = {"written": stand_in.clock(), "run": None,
                           "release": "20250101-0000-old" if previous else None,
                           "ports": {}, "critical": 0}
        return stand_in, deploy_args(base=str(place / "base"))

    def deploy(stand_in, args, wait=0.3):
        try:
            return rel.deploy(args, log=QUIET, target=stand_in, wait=wait,
                              sleep=lambda s: None, info=INFO), None
        except rel.ReleaseError as exc:
            return None, str(exc)

    def no_git_needed():
        stand_in, args = fresh("nogit-deploy")
        stand_in.on_restart = reports(current_name)

        def refuse(*a, **k):
            raise AssertionError("git was called")
        saved = rel._git
        rel._git = refuse
        try:
            made, error = deploy(stand_in, args)
        finally:
            rel._git = saved
        return made is not None, error or f"deployed {made} without git"
    attempt("a deploy, given what to ship, never needs git on either side",
            no_git_needed)

    def install_fails_halfway():
        stand_in, args = fresh("halfway", old_unit="OLD UNIT\n")
        installs = []

        def second_install(command):
            if command.startswith("FAKESUDO install") and "units-before" not in command:
                installs.append(command)
                return len(installs) == 2
            return False
        stand_in.fail_on = second_install
        made, error = deploy(stand_in, args)
        unit = (folder / "halfway" / "etc" / "vacuum-lab-web.service").read_text()
        return (made is None and unit == "OLD UNIT\n"
                and "put back as they were" in (error or "")
                and stand_in.restarts == 0,
                (error or "deployed")[-80:])
    attempt("a unit install that fails half way puts back what it had "
            "installed, and nothing is restarted", install_fails_halfway)

    def healthy_deploy():
        stand_in, args = fresh("healthy")
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        return (made and current_name(stand_in) == made
                and stand_in.restarts == 1, error or f"running {made}")
    attempt("a deploy switches, restarts once and finds its release running",
            healthy_deploy)

    def run_started_meanwhile():
        stand_in, args = fresh("meanwhile")

        def somebody_starts(target, command):
            if command.startswith("test -e") and not target.status.get("run"):
                target.status = {**target.status, "written": target.clock(),
                                 "run": {"id": 9, "procedure": "deposition"}}
        stand_in.before_command = somebody_starts
        made, error = deploy(stand_in, args)
        return (made is None and "run 9" in (error or "")
                and stand_in.restarts == 0
                and current_name(stand_in) == "20250101-0000-old",
                (error or "deployed anyway")[:80])
    attempt("a run started while a deploy runs its checks stops it before "
            "anything is switched", run_started_meanwhile)

    def clocks_apart():
        stand_in, args = fresh("skew")
        stand_in.skew = -300.0                  # the target five minutes behind
        stand_in.status["written"] = stand_in.clock()
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        dead, _ = fresh("dead")
        dead.status["written"] = time.time() - 300   # stale by its own clock
        dead.skew = 0.0
        refused = rel.idle_problem(*rel.rig_state(dead, "/x"))
        return (made is not None and refused,
                f"behind: {error or 'deployed'}; dead panel: {refused}")
    attempt("the status file's age is read by the target's own clock",
            clocks_apart)

    def first_release_undone():
        stand_in, args = fresh("first", previous=False, old_unit="OLD UNIT\n")
        stand_in.on_restart = reports(lambda t: None)   # never comes up right
        made, error = deploy(stand_in, args)
        unit = (folder / "first" / "etc" / "vacuum-lab-web.service").read_text()
        added = (folder / "first" / "etc" / "vacuum-lab-backup.timer").exists()
        return (made is None and "rolled back to the copy" in (error or "")
                and unit == "OLD UNIT\n" and not added,
                f"{(error or 'deployed')[:70]}; unit restored "
                f"{unit == 'OLD UNIT' + chr(10)}; added timer removed {not added}")
    attempt("a first release that does not come up is undone: the units it "
            "replaced put back, the ones it added removed", first_release_undone)

    def nothing_to_go_back_to():
        stand_in, args = fresh("bare", previous=False)
        args.install_units = True
        for installed, source in stand_in.units.items():   # units already right
            shutil.copy2(Path(ROOT) / source, installed)
        stand_in.on_restart = reports(lambda t: None)
        made, error = deploy(stand_in, args)
        return (made is None and "NOT rolled back" in (error or ""),
                (error or "deployed")[:80])
    attempt("a release with nothing to go back to says it was not rolled back",
            nothing_to_go_back_to)

    def bad_release_rolled_back():
        stand_in, args = fresh("rollback")
        stand_in.on_restart = reports(
            lambda t: "20250101-0000-old" if current_name(t).endswith("-old")
            else "something else")
        made, error = deploy(stand_in, args)
        return (made is None and current_name(stand_in) == "20250101-0000-old"
                and "rolled back to 20250101-0000-old" in (error or ""),
                (error or "deployed")[:80])
    attempt("a release that does not come up is switched back, and the one "
            "before checked running again", bad_release_rolled_back)

    # --- from the units' old names (tc600-web, tc600-backup) ------------------------
    def old_names(name):
        stand_in, args = fresh(name)
        for installed in stand_in.old_units:
            Path(installed).write_text(f"OLD {Path(installed).name}\n")
        return stand_in, args

    def systemctl(stand_in) -> list[str]:
        return [c[len(stand_in.sudo):] for c in stand_in.commands
                if c.startswith(stand_in.sudo + "systemctl")]

    def moved_over():
        stand_in, args = old_names("moved")
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        done = systemctl(stand_in)
        etc = folder / "moved" / "etc"
        kept = folder / "moved" / "base" / "units-before" / str(made)
        return (made is not None
                and all((etc / Path(i).name).exists() for i in rel.UNITS)
                and not any((etc / Path(i).name).exists() for i in rel.OLD_UNITS)
                and all((kept / Path(i).name).exists() for i in rel.OLD_UNITS)
                and "systemctl enable vacuum-lab-web" in done
                and "systemctl disable tc600-web" in done
                and "systemctl disable --now tc600-backup.timer" in done
                and "systemctl enable --now vacuum-lab-backup.timer" in done
                and "systemctl stop tc600-web" not in done
                and stand_in.restarted == ["vacuum-lab-web"]
                and "tc600-web" in stand_in.inactive,
                error or f"systemctl: {done}")
    attempt("a machine under the old unit names is moved over: new units "
            "enabled, one restart of the new panel that stops the old one, the "
            "old unit files removed once it is healthy and kept aside",
            moved_over)

    def both_running():
        stand_in, args = old_names("both")
        stand_in.conflicts_work = False      # the old panel does not stop
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        return (made is None and "still running beside" in (error or "")
                and stand_in.restarted[-1] == "tc600-web",
                (error or "deployed")[:120])
    attempt("a move is not healthy while the old panel still runs beside the "
            "new one, and is undone", both_running)

    def restart_refused():
        stand_in, args = old_names("refused")
        stand_in.fail_on = lambda c: c == "FAKESUDO systemctl restart vacuum-lab-web"
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        etc = folder / "refused" / "etc"
        return (made is None and "rolled back" in (error or "")
                and all((etc / Path(i).name).exists() for i in rel.OLD_UNITS)
                and not (etc / "vacuum-lab-web.service").exists()
                and stand_in.restarted == ["tc600-web"],
                f"{(error or 'deployed')[:80]}; restarts {stand_in.restarted}")
    attempt("a move whose new panel will not start goes back to the old one",
            restart_refused)

    def new_panel_will_not_stop():
        stand_in, args = old_names("stuck")
        stand_in.on_restart = lambda t: reports(
            lambda u: "something else" if t.restarted[-1] == "vacuum-lab-web"
            else current_name(u))(t)
        stand_in.fail_on = lambda c: c == ("FAKESUDO systemctl disable --now "
                                           "vacuum-lab-web.service")
        made, error = deploy(stand_in, args)
        kept = (folder / "stuck" / "etc" / "vacuum-lab-web.service").exists()
        return (made is None and "NOT work" in (error or "") and kept,
                f"{(error or 'deployed')[:90]}; unit kept {kept}")
    attempt("... and if the new panel cannot be stopped in going back, its unit "
            "stays -- its Conflicts= keeps the two apart -- and the deploy says "
            "so", new_panel_will_not_stop)

    def only_backups_left():
        stand_in, args = fresh("leftover")
        etc = folder / "leftover" / "etc"
        for installed, source in stand_in.units.items():
            shutil.copy2(Path(ROOT) / source, installed)
        for name in ("tc600-backup.service", "tc600-backup.timer"):
            (etc / name).write_text(f"OLD {name}\n")
        stand_in.on_restart = reports(current_name)
        made, error = deploy(stand_in, args)
        return (made is not None and stand_in.restarted == ["vacuum-lab-web"]
                and not (etc / "tc600-backup.timer").exists(),
                error or f"restarts {stand_in.restarted}")
    attempt("old backup units left behind are removed, and do not make a "
            "deploy treat the panel as still under its old name",
            only_backups_left)

    def dropins_refused():
        stand_in, args = old_names("dropins")
        (folder / "dropins" / "etc" / "tc600-web.service.d").mkdir()
        made, error = deploy(stand_in, args)
        return (made is None and "tc600-web.service.d" in (error or "")
                and stand_in.restarts == 0
                and current_name(stand_in) == "20250101-0000-old",
                (error or "deployed")[:100])
    attempt("a move that would leave local changes (drop-ins) behind is "
            "refused before anything is switched", dropins_refused)

    def backup_through_the_unit_there():
        chosen = []
        for name, setup in (("bk-old", old_names), ("bk-new", fresh)):
            stand_in, args = setup(name)
            if setup is fresh:
                for installed, source in stand_in.units.items():
                    shutil.copy2(Path(ROOT) / source, installed)
            args.no_backup = False
            stand_in.on_restart = reports(current_name)
            deploy(stand_in, args)
            chosen.append(next((c.split()[-1] for c in systemctl(stand_in)
                                if c.startswith("systemctl start")), None))
        return (chosen == ["tc600-backup.service", "vacuum-lab-backup.service"],
                str(chosen))
    attempt("the backup before a deploy runs through the backup unit the "
            "machine has, old name or new", backup_through_the_unit_there)

    def not_without_asking():
        stand_in, args = old_names("asked")
        args.install_units = False
        made, error = deploy(stand_in, args)
        return (made is None and "tc600-web.service is the unit's old name"
                in (error or "") and stand_in.restarts == 0
                and current_name(stand_in) == "20250101-0000-old",
                (error or "deployed")[:120])
    attempt("... but only with --install-units", not_without_asking)

    def move_undone():
        stand_in, args = old_names("unmoved")

        def comes_up(target):
            # Under the new name it never comes right; under the old name the
            # release before is running again.
            back = target.restarted[-1] == "tc600-web"
            reports(lambda t: current_name(t) if back else "something else")(target)
        stand_in.on_restart = comes_up
        made, error = deploy(stand_in, args)
        done = systemctl(stand_in)
        etc = folder / "unmoved" / "etc"
        restored = all((etc / Path(i).name).read_text()
                       == f"OLD {Path(i).name}\n" for i in rel.OLD_UNITS)
        added = [Path(i).name for i in rel.UNITS
                 if Path(i).name != "99-vacuum-lab.rules"
                 and (etc / Path(i).name).exists()]
        return (made is None and "rolled back to 20250101-0000-old" in (error or "")
                and restored and not added
                and "systemctl disable --now vacuum-lab-web.service" in done
                and "systemctl enable tc600-web" in done
                and "systemctl enable --now tc600-backup.timer" in done
                and stand_in.restarted == ["vacuum-lab-web", "tc600-web"]
                and current_name(stand_in) == "20250101-0000-old",
                f"{(error or 'deployed')[:60]}; old back {restored}; "
                f"new left {added}; restarts {stand_in.restarted}")
    attempt("a move that does not come up healthy is undone: the old units "
            "back, enabled and restarted, the new ones removed", move_undone)

    def unit_never_beside_old():
        unit = (Path(ROOT) / "deploy" / "vacuum-lab-web.service").read_text()
        lines = [line.strip() for line in unit.splitlines()]
        conflicts = "Conflicts=tc600-web.service" in lines
        ordered = any(line.startswith("After=") and "tc600-web.service" in line
                      for line in lines)
        return conflicts and ordered, f"Conflicts {conflicts}, ordered {ordered}"
    attempt("the panel's unit never runs beside its old name, and stops it "
            "before starting", unit_never_beside_old)

    def which_unit():
        stand_in, _ = old_names("which")
        before = rel.panel_service(stand_in)
        Path(next(i for i in stand_in.units
                  if i.endswith("vacuum-lab-web.service"))).write_text("NEW\n")
        return (before == "tc600-web" and rel.panel_service(stand_in)
                == "vacuum-lab-web", f"{before} -> {rel.panel_service(stand_in)}")
    attempt("rollback restarts the unit the panel runs under, old name or new",
            which_unit)
