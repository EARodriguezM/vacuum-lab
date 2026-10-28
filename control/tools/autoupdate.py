#!/usr/bin/env python3
"""New versions of the panel from GitHub: found, checked and tested on the
machine that runs the rig, and installed only when an admin asks.

    ./tools/autoupdate.py check                 look for a new version; verify,
                                                unpack and test it
    ./tools/autoupdate.py install               install the version that is
                                                ready (the panel asks for it)
    ./tools/autoupdate.py status                what the updater knows
    ./tools/autoupdate.py trust NAME KEYFILE    accept versions signed with
                                                this public key
    ./tools/autoupdate.py retry TAG             forget that TAG was refused,
                                                to try it again

It runs as the panel's own user: `vacuum-lab-update.timer` runs `check`
every 15 minutes, and `vacuum-lab-install.path` runs `install` when the
panel leaves a request beside its database (Settings -> Server, an admin's
"Install"). Nothing is installed without a person asking, because a restart
leaves the rig unwatched for a minute and the person who asks can see that
nobody is about to start a run.

A version is a signed tag on the repository, `vYYYY.MM.DD` (`.2`, `.3` for
more than one a day), made on the laptop with `tools/release.py tag`: an
annotated tag signed with an SSH key, whose message carries a SHA-256 over
every file under `control/` at its commit. `check`, in order, stopping at
the first thing that fails:

 1. asks GitHub which tags exist -- the list `git fetch` reads, which
    spends none of GitHub's API quota -- and takes the newest version that
    is newer than the one running;
 2. fetches the tag's signed text and verifies its signature against
    `allowed_signers` in the base folder (`trust` writes it). A tag pushed
    by somebody who got into the GitHub account, but has not got the key,
    is refused;
 3. downloads that commit's files and compares their hash with the one the
    signature covers, so other files served under that commit are refused
    too;
 4. unpacks `control/` into `<base>/releases/<id>/` with a VERSION.json, and
    checks every file parses as Python 3.11 to 3.14;
 5. runs the selftest there, at the lowest priority, only while the panel
    reports no run, power-down or make-safe -- and stops it if one begins,
    to try again later;
 6. writes in `update.json`, beside the database, that the version is ready.
    The panel shows it under Settings -> Server.

A version refused -- signature, hash, selftest, or an install that did not
come up healthy -- is not tried again until somebody says so (`retry TAG`,
after a selftest that failed for a reason of its own); otherwise a newer tag
is needed. A network failure, a key not trusted yet or a restart that could
not be made is only said, and tried again.

`install` is the second half of `tools/release.py deploy`: the rig idle, a
backup to `<base>/backups`, the system units unchanged (a version that
changes them is deployed from the laptop, `release.py deploy
--install-units`), `current` switched, the panel restarted, and within 60 s
it must be running the new version with its instruments where they were and
no new critical alarm -- otherwise `current` is switched back and the panel
restarted on the version before.

The tarball GitHub serves is `git archive` of the commit, which is also what
`release.py tag` hashes. A `.gitattributes` with `export-subst` or
`export-ignore` would make the two differ; there is none, keep it so.
"""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Imported here, at the top, never later: `install` switches `current` to
# another release while this process runs, and a module imported after that
# would come from the new release.
from tools import release as rel  # noqa: E402

REPOSITORY = "EARodriguezM/vacuum-lab"
GITHUB = "https://github.com"
API = "https://api.github.com"
CODELOAD = "https://codeload.github.com"
USER_AGENT = "vacuum-lab-updater"

#: A version's tag: the day it was made, and a count for a second that day.
TAG = re.compile(r"v(\d{4})\.(\d{2})\.(\d{2})(?:\.([2-9]|[1-9]\d{1,2}))?")
#: The line of a tag's message that carries the hash of `control/`.
DIGEST_LINE = "control-sha256:"
#: The folder of the repository a release is made of.
FOLDER = "control"
#: The namespace git signs tags in (`ssh-keygen -Y sign -n git`).
NAMESPACE = "git"

DEFAULT_DATA = "~/.local/share/vacuum-lab"
#: The updater's account of itself: kept in the base folder, which the
#: panel's unit cannot write, and copied beside the database for the panel
#: to read. The installer believes only the first: the panel's folder is
#: one a panel gone wrong could write. An admin's request is written there
#: by the panel. lab/updates.py names the same two files.
STATE_FILE = "update.json"
REQUEST_FILE = "update-request.json"
#: How a release folder this updater made is named; nothing else is switched to.
RELEASE = re.compile(r"\d{8}-\d{4}-[0-9a-f]{8}")
SIGNERS = "allowed_signers"
LOCK_FILE = "update.lock"

#: A release is a few megabytes; anything near this is not one.
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_ANSWER = 1024 * 1024
TIMEOUT_S = 30.0
#: The selftest takes about 11 min on the lab PC, longer at the lowest
#: priority; past this it is called failed.
SELFTEST_LIMIT_S = 3600.0
#: How often a running selftest looks whether a run has begun.
SELFTEST_POLL_S = 5.0
#: How long `install` waits for a `check` to let go of the lock.
LOCK_WAIT_S = 10.0
#: How long `install` waits for the panel's status file to show the hold:
#: the panel writes it before the request, but a write can fail.
HOLD_SHOWN_S = 10.0
#: A request older than this is not acted on: the panel withdraws one nobody
#: took within a minute, and lets runs start again.
REQUEST_MAX_AGE_S = 90.0
#: Before going back from a version that is not healthy: how long to wait
#: for a run or a power-down to end first.
ROLLBACK_WAIT_S = 120.0


class UpdateError(Exception):
    """Something that stops this check or install, said in a sentence."""


class Refused(UpdateError):
    """A version that must never be tried again: a bad signature, files that
    do not match it, a failed selftest."""


# --- what GitHub says -----------------------------------------------------------


def fetch(url: str, limit: int = MAX_ANSWER, accept: str | None = None,
          to: Path | None = None) -> bytes:
    """GET `url` over verified HTTPS, at most `limit` bytes -- into `to`
    when given (returning b""). Every failure is an UpdateError."""
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as answer:
            size = 0
            chunks = []
            sink = open(to, "wb") if to is not None else None
            try:
                while True:
                    chunk = answer.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise UpdateError(f"{url}: more than {limit} bytes")
                    if sink is not None:
                        sink.write(chunk)
                    else:
                        chunks.append(chunk)
            finally:
                if sink is not None:
                    sink.close()
            return b"".join(chunks)
    except urllib.error.HTTPError as exc:
        raise UpdateError(f"{url}: GitHub answered {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"cannot reach {url.split('/')[2]}: {reason}") from None


def parse_refs(data: bytes) -> dict[str, str]:
    """The refs in git's smart-HTTP advertisement (pkt-lines): name -> sha.
    A peeled tag's commit is listed as `refs/tags/<name>^{}`."""
    refs: dict[str, str] = {}
    position = 0
    while position < len(data):
        try:
            size = int(data[position:position + 4], 16)
        except ValueError:
            raise UpdateError("GitHub's list of tags is not in the form git "
                              "reads") from None
        if size == 0:                      # a flush between sections
            position += 4
            continue
        if size < 4 or position + size > len(data):
            raise UpdateError("GitHub's list of tags is cut short")
        line = data[position + 4:position + size].rstrip(b"\n")
        position += size
        if line.startswith(b"#"):
            continue
        line = line.split(b"\0", 1)[0]     # the first carries capabilities
        sha, _, name = line.decode("utf-8", "replace").partition(" ")
        if re.fullmatch(r"[0-9a-f]{40}", sha) and name:
            refs[name] = sha
    return refs


def annotated_tags(refs: dict[str, str]) -> dict[str, dict]:
    """Tags that are tag objects -- the only kind that can be signed:
    name -> {"object": the tag's sha, "commit": the commit it names}."""
    tags = {}
    for name, sha in refs.items():
        if name.startswith("refs/tags/") and not name.endswith("^{}"):
            commit = refs.get(name + "^{}")
            if commit:
                tags[name[len("refs/tags/"):]] = {"object": sha, "commit": commit}
    return tags


def version_key(tag: str) -> tuple[int, ...] | None:
    match = TAG.fullmatch(tag or "")
    if not match:
        return None
    year, month, day, count = match.groups()
    return int(year), int(month), int(day), int(count or 1)


def signed_text(api: str, repository: str, sha: str) -> tuple[bytes, bytes]:
    """A tag object's signed text and its signature, as GitHub gives them:
    `payload` is the tag without the signature -- exactly the bytes signed."""
    try:
        answer = json.loads(fetch(f"{api}/repos/{repository}/git/tags/{sha}",
                                  accept="application/vnd.github+json"))
    except ValueError:
        raise UpdateError("GitHub's answer about the tag is not JSON") from None
    found = (answer.get("verification") or {}) if isinstance(answer, dict) else {}
    payload, signature = found.get("payload"), found.get("signature")
    if not isinstance(payload, str) or not isinstance(signature, str) \
            or not payload or not signature:
        raise Refused("the tag is not signed")
    return payload.encode("utf-8"), signature.encode("utf-8")


def parse_tag(payload: bytes) -> dict:
    """The fields of a tag object's text, and the version's notes and hash
    from its message. The message is covered by the signature: everything
    taken from here is what the person who signed it wrote."""
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("the tag's text is not UTF-8") from None
    head, _, message = text.partition("\n\n")
    fields = {}
    for line in head.splitlines():
        key, _, value = line.partition(" ")
        fields.setdefault(key, value)
    digests = re.findall(rf"^{re.escape(DIGEST_LINE)} ([0-9a-f]{{64}})$",
                         message, re.M)
    if len(digests) != 1:
        raise Refused(f"the tag's message has no single `{DIGEST_LINE}` line: "
                      f"it was not made by tools/release.py tag")
    tagged_at = None
    parts = fields.get("tagger", "").rsplit(" ", 2)
    if len(parts) == 3 and parts[1].isdigit():
        tagged_at = int(parts[1])
    lines = message.strip("\n").splitlines()
    notes = "\n".join(line for line in lines[1:]
                      if not line.startswith(DIGEST_LINE)).strip()
    return {"object": fields.get("object"), "type": fields.get("type"),
            "tag": fields.get("tag"), "tagger": fields.get("tagger", ""),
            "tagged_at": tagged_at, "title": lines[0] if lines else "",
            "notes": notes, "digest": digests[0]}


# --- the signature ------------------------------------------------------------------


def verify(payload: bytes, signature: bytes, signers: Path) -> str:
    """Who signed `payload`, by the name `allowed_signers` gives their key;
    Refused if nobody it lists did. A missing file trusts nobody."""
    signers = Path(signers)
    if not signers.is_file():
        # Not the version's fault: it is checked again once a key is trusted.
        raise UpdateError(f"no key is trusted yet ({signers} is missing): "
                          f"./tools/autoupdate.py trust NAME KEYFILE")
    with tempfile.TemporaryDirectory(prefix="vacuum-lab-verify-") as scratch:
        sig = Path(scratch) / "tag.sig"
        sig.write_bytes(signature)
        found = subprocess.run(
            ["ssh-keygen", "-Y", "find-principals", "-f", str(signers),
             "-s", str(sig)], capture_output=True, text=True, timeout=30)
        names = found.stdout.split()
        if found.returncode != 0 or not names:
            raise Refused("the tag is signed with a key that is not trusted "
                          "here")
        checked = subprocess.run(
            ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", names[0],
             "-n", NAMESPACE, "-s", str(sig)], input=payload,
            capture_output=True, timeout=30)
        if checked.returncode != 0:
            raise Refused("the tag's signature does not match its text")
    return names[0]


def trust(name: str, keyfile: Path, signers: Path) -> str:
    """Add a public key to `allowed_signers`, for git tags only."""
    if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", name):
        raise UpdateError("the name: letters, digits, . _ @ - only")
    fields = Path(keyfile).read_text(encoding="utf-8").split()
    if len(fields) < 2 or not fields[0].startswith(("ssh-", "ecdsa-", "sk-")):
        raise UpdateError(f"{keyfile} is not an SSH public key")
    checked = subprocess.run(["ssh-keygen", "-l", "-f", str(keyfile)],
                             capture_output=True, text=True, timeout=30)
    if checked.returncode != 0:
        raise UpdateError(f"{keyfile}: {checked.stderr.strip()}")
    line = f'{name} namespaces="{NAMESPACE}" {fields[0]} {fields[1]}\n'
    signers = Path(signers)
    signers.parent.mkdir(parents=True, exist_ok=True)
    with open(signers, "a", encoding="utf-8") as fh:
        fh.write(line)
    os.chmod(signers, 0o644)
    return checked.stdout.strip()


# --- the files ---------------------------------------------------------------------


def digest(tar: tarfile.TarFile, strip: int = 0,
           into: Path | None = None) -> tuple[str, int]:
    """The hash of every file under `control/` in `tar`, and how many there
    are; with `into`, each is also written there, so what is hashed is what
    is unpacked. `strip` leading folders are dropped first (GitHub's tarball
    has one, `git archive` none).

    One line per file -- `x` or `-` for the execute bit, its SHA-256, its
    path under control/ -- sorted, and hashed together. Only plain files: a
    link, a device or a path leaving the folder refuses the whole release.
    """
    lines = []
    for member in tar:
        parts = [p for p in member.name.split("/")[strip:] if p not in ("", ".")]
        if not parts or parts[0] != FOLDER or len(parts) == 1:
            continue
        name = "/".join(parts[1:])
        if ".." in parts or any(ord(c) < 32 or c == "\\" for c in name):
            raise Refused(f"{member.name}: not a path a release may hold")
        if member.isdir():
            continue
        if not member.isfile():
            raise Refused(f"{member.name} is not a plain file")
        data = tar.extractfile(member).read()
        executable = bool(member.mode & 0o100)
        lines.append(f"{'x' if executable else '-'} "
                     f"{hashlib.sha256(data).hexdigest()} {name}\n")
        if into is not None:
            path = Path(into, *parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            os.chmod(path, 0o755 if executable else 0o644)
    lines.sort()
    return hashlib.sha256("".join(lines).encode()).hexdigest(), len(lines)


# --- this machine ------------------------------------------------------------------


class LocalTarget(rel.Target):
    """`tools/release.py`'s target, for the machine this runs on: the same
    commands, run here instead of over SSH."""

    def __init__(self, base: str):
        super().__init__("localhost", base)

    def run(self, command: str, timeout: float = 120.0) -> tuple[int, str]:
        try:
            result = subprocess.run(["bash", "-c", command], capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"no answer within {timeout:.0f} s"
        return result.returncode, (result.stdout + result.stderr).strip()


def running_version(base: Path) -> dict:
    """The VERSION.json of the release `current` points at."""
    try:
        version = json.loads((base / "current" / "VERSION.json").read_text())
        return version if isinstance(version, dict) else {}
    except (OSError, ValueError):
        return {}


def load_state(data: Path) -> dict:
    try:
        state = json.loads((data / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("refused", {})
    return state


def save_state(data: Path, state: dict) -> None:
    """Replace update.json in one rename, readable by this user only."""
    data.mkdir(parents=True, exist_ok=True)
    temporary = data / (STATE_FILE + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(temporary, data / STATE_FILE)


@contextlib.contextmanager
def locked(base: Path, wait: float = 0.0):
    """One updater at a time: yields True when this one holds the lock,
    False when another held it for longer than `wait`."""
    base.mkdir(parents=True, exist_ok=True)
    with open(base / LOCK_FILE, "w") as fh:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(0.2)
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def idle_problem(status: dict | None, now: float | None,
                 force: bool = False) -> str | None:
    """tools/release.py's judgement, less the install hold: that is this
    updater's own, which a deploy from a laptop must respect and an install
    must not trip over."""
    if isinstance(status, dict):
        status = {k: v for k, v in status.items() if k != "update_hold"}
    return rel.idle_problem(status, now, force=force)


def rig_busy(status_path: str) -> str | None:
    """Why the rig must not be disturbed now, from the panel's status file:
    a run, a power-down, a make-safe, steps by hand. A panel that is not
    running says nothing, and nothing is going on that it watches."""
    try:
        state = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        state = None
    return idle_problem(state if isinstance(state, dict) else None,
                        time.time(), force=True)


def run_selftest(folder: Path, status_path: str, command: list[str],
                 limit: float = SELFTEST_LIMIT_S, poll: float = SELFTEST_POLL_S,
                 log=print) -> tuple[str, str]:
    """("passed" | "failed" | "interrupted", what to say). Stopped, and
    "interrupted", as soon as the panel reports a run or a power-down."""
    started = time.monotonic()
    with open(folder / "selftest.log", "w") as out:
        process = subprocess.Popen(
            command, cwd=folder, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        try:
            while process.poll() is None:
                try:
                    process.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                busy = rig_busy(status_path)
                late = time.monotonic() - started > limit
                if busy or late:
                    _stop(process)
                    if busy:
                        return "interrupted", busy
                    return "failed", f"the selftest took longer than {limit / 60:.0f} min"
        finally:
            if process.poll() is None:
                _stop(process)
    lines = (folder / "selftest.log").read_text(errors="replace").strip().splitlines()
    tail = lines[-1] if lines else "(no output)"
    elapsed = time.monotonic() - started
    log(f"  selftest: {tail} ({elapsed:.0f} s)")
    return ("passed" if process.returncode == 0 else "failed"), tail


def _stop(process: subprocess.Popen) -> None:
    for sig, wait in ((signal.SIGTERM, 10), (signal.SIGKILL, 10)):
        try:
            os.killpg(process.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            process.wait(timeout=wait)
            return
        except subprocess.TimeoutExpired:
            continue


def _remove_release(base: Path, release: str | None, keep: set) -> None:
    """A release that was unpacked but will not be installed."""
    if release and release not in keep and re.fullmatch(r"[\w.-]+", release):
        shutil.rmtree(base / "releases" / release, ignore_errors=True)


# --- check ---------------------------------------------------------------------------


class Config:
    """Where everything is; the selftest replaces the addresses and commands."""

    def __init__(self, base: str = rel.DEFAULT_BASE, data: str = DEFAULT_DATA,
                 status: str = rel.DEFAULT_STATUS, repository: str = REPOSITORY,
                 github: str = GITHUB, api: str = API, codeload: str = CODELOAD,
                 selftest: list[str] | None = None,
                 selftest_limit: float = SELFTEST_LIMIT_S,
                 selftest_poll: float = SELFTEST_POLL_S):
        self.base = Path(os.path.expanduser(base))
        self.data = Path(os.path.expanduser(data))
        self.status = status
        self.repository = repository
        self.github, self.api, self.codeload = github, api, codeload
        self.selftest = selftest or [sys.executable, "tools/selftest.py"]
        self.selftest_limit = selftest_limit
        self.selftest_poll = selftest_poll

    @property
    def signers(self) -> Path:
        return self.base / SIGNERS

    def load(self) -> dict:
        """The updater's own state, from the base folder."""
        return load_state(self.base)

    def save(self, state: dict, keep_install: bool = True) -> None:
        """Write the state, then the panel's copy. `keep_install`: the
        install record on disk wins -- an install refused while a check held
        the lock wrote it meanwhile, and only the installer changes it."""
        if keep_install:
            state["install"] = self.load().get("install")
        save_state(self.base, state)
        save_state(self.data, state)


def newest(tags: dict[str, dict], refused: dict) -> tuple[str, dict] | None:
    """The newest version's tag, of those not refused before."""
    versions = [(version_key(name), name) for name in tags
                if version_key(name) and name not in refused]
    if not versions:
        return None
    name = max(versions)[1]
    return name, tags[name]


def wanted(name: str, tag: dict, running: dict) -> bool:
    """Whether `name` would be a change: newer than the version running, or
    -- for a release deployed by hand, which has no version -- another
    commit."""
    if running.get("tag") and version_key(running["tag"]):
        return version_key(name) > version_key(running["tag"])
    return tag["commit"] != running.get("commit")


def check(config: Config, log=print) -> str:
    """Look for a new version and take it as far as "ready". Returns what
    happened, in a few words."""
    with locked(config.base) as mine:
        if not mine:
            return "another update is under way"
        state = config.load()
        try:
            return _check(config, state, log)
        finally:
            config.save(state)


def _check(config: Config, state: dict, log) -> str:
    running = running_version(config.base)
    state["running"] = {key: running.get(key) for key in
                        ("release", "tag", "commit", "built_utc")}
    try:
        refs = parse_refs(fetch(f"{config.github}/{config.repository}.git/"
                                f"info/refs?service=git-upload-pack"))
    except UpdateError as exc:
        state["check_error"] = str(exc)
        log(f"  {exc}")
        return "GitHub could not be asked"
    state["checked_at"] = time.time()
    state["check_error"] = None

    latest = state.get("latest")
    found = newest(annotated_tags(refs), state["refused"])
    if found is None or not wanted(*found, running):
        if latest and latest.get("release") != running.get("release"):
            _remove_release(config.base, latest.get("release"),
                            {running.get("release")})
        state["latest"] = None
        return "up to date"
    name, tag = found

    if not latest or latest.get("tag") != name or latest.get("object") != tag["object"]:
        if latest:
            _remove_release(config.base, latest.get("release"),
                            {running.get("release")})
        state["latest"] = None
        log(f"New version {name} ({tag['commit'][:8]})")
        try:
            latest = state["latest"] = fetch_version(config, name, tag, running, log)
        except Refused as exc:
            state["refused"][name] = str(exc)
            state["latest"] = {"tag": name, "object": tag["object"],
                               "commit": tag["commit"], "state": "refused",
                               "detail": str(exc), "at": time.time()}
            log(f"  REFUSED: {exc}")
            return f"{name} refused"
        except UpdateError as exc:
            state["check_error"] = str(exc)
            log(f"  {exc}")
            return f"{name} could not be fetched"

    if latest.get("state") == "ready":
        return f"{name} is ready to install"
    if latest.get("state") == "refused":
        return f"{name} refused"
    return test_version(config, state, log)


def fetch_version(config: Config, name: str, tag: dict, running: dict,
                  log) -> dict:
    """Verify, download, compare and unpack one version; its `latest` entry,
    waiting for its selftest."""
    payload, signature = signed_text(config.api, config.repository,
                                     tag["object"])
    signer = verify(payload, signature, config.signers)
    fields = parse_tag(payload)
    if fields["tag"] != name:
        raise Refused(f"the signed tag is {fields['tag']!r}, published as {name!r}")
    if fields["type"] != "commit" or fields["object"] != tag["commit"]:
        raise Refused("the signed tag names another commit than GitHub lists")
    commit = fields["object"]
    log(f"  signed by {signer}")

    release = time.strftime("%Y%m%d-%H%M", time.gmtime()) + "-" + commit[:8]
    folder = config.base / "releases" / release
    downloads = config.base / "downloads"
    # Unpacked beside the downloads, not among the releases: a folder left
    # half-written by a power cut must never be taken for one.
    partial = downloads / (release + ".partial")
    downloads.mkdir(parents=True, exist_ok=True)
    tarball = downloads / f"{commit}.tar.gz"
    shutil.rmtree(partial, ignore_errors=True)
    try:
        fetch(f"{config.codeload}/{config.repository}/tar.gz/{commit}",
              limit=MAX_DOWNLOAD, to=tarball)
        partial.mkdir(parents=True)
        try:
            with tarfile.open(tarball, "r:*") as tar:
                found, files = digest(tar, strip=1, into=partial)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise Refused(f"the download is not a readable tarball: {exc}") from None
        if found != fields["digest"]:
            raise Refused("the files GitHub served are not the ones the tag "
                          "was signed for")
        problems = rel.parse_problems(rel.code_files(partial), partial)
        if problems:
            raise Refused("does not parse here: " + "; ".join(problems[:3]))
        (partial / "VERSION.json").write_text(json.dumps({
            "release": release, "commit": commit, "tag": name, "branch": None,
            "dirty": False, "built_utc": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "files": files,
            "signed_by": signer, "from": "github",
        }, indent=1) + "\n")
        if folder.exists():
            raise UpdateError(f"{folder} already exists")
        partial.rename(folder)
    finally:
        shutil.rmtree(partial, ignore_errors=True)
        tarball.unlink(missing_ok=True)
    log(f"  {files} files unpacked into {folder}")
    older = None
    if not running.get("tag") and running.get("built_utc") and fields["tagged_at"]:
        built = _utc(running["built_utc"])
        older = built is not None and fields["tagged_at"] < built
    return {"tag": name, "object": tag["object"], "commit": commit,
            "release": release, "title": fields["title"], "notes": fields["notes"],
            "tagged_at": fields["tagged_at"], "signed_by": signer,
            "older_than_running": older, "state": "waiting",
            "detail": "the selftest has not run yet", "at": time.time()}


def _utc(text: str) -> float | None:
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def test_version(config: Config, state: dict, log) -> str:
    latest = state["latest"]
    name = latest["tag"]
    folder = config.base / "releases" / latest["release"]
    if not folder.is_dir():
        # Removed by hand, or by a prune: fetched again next time.
        state["latest"] = None
        return f"{name}'s folder is gone"
    busy = rig_busy(config.status)
    if busy:
        latest.update(state="waiting", detail=f"to be tested when the rig is "
                                              f"idle: {busy}")
        return f"{name} waits: {busy}"
    latest.update(state="testing", detail="the selftest is running",
                  at=time.time())
    config.save(state)
    log(f"Testing {name} in {folder}")
    outcome, said = run_selftest(folder, config.status, config.selftest,
                                 config.selftest_limit, config.selftest_poll, log)
    if outcome == "interrupted":
        latest.update(state="waiting", detail=f"the selftest was stopped and "
                                              f"will run again: {said}")
        return f"{name}'s selftest stopped: {said}"
    if outcome == "failed":
        reason = f"the selftest failed here: {said}"
        state["refused"][name] = reason
        latest.update(state="refused", detail=reason, at=time.time())
        _remove_release(config.base, latest["release"],
                        {state.get("running", {}).get("release")})
        return f"{name} refused: {said}"
    latest.update(state="ready", detail=said, tested_at=time.time(),
                  at=time.time())
    return f"{name} is ready to install"


# --- install -------------------------------------------------------------------------


def claim_request(data: Path) -> dict | None:
    """Take the panel's request, if there is one: renamed first, so the panel
    withdrawing it (it unlinks the file) and this taking it cannot both
    succeed. A request that cannot be read is dropped."""
    request = data / REQUEST_FILE
    taken = data / (REQUEST_FILE + f".taken-{os.getpid()}")
    try:
        os.rename(request, taken)
    except FileNotFoundError:
        return None
    try:
        found = json.loads(taken.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        found = None
    finally:
        taken.unlink(missing_ok=True)
    if not isinstance(found, dict) or not isinstance(found.get("id"), str) \
            or not isinstance(found.get("release"), str):
        return None
    return found


def install(config: Config, target: rel.Target | None = None,
            wait: float = rel.HEALTH_WAIT_S, sleep=time.sleep,
            backup: bool = True, log=print) -> str:
    """Install what the panel's request names. Returns the outcome
    ("installed", "refused", "rolled_back", "failed", or "" for no
    request), recorded in update.json under `install` for the panel."""
    request = claim_request(config.data)
    if request is None:
        return ""
    record = {"request": request["id"], "release": request["release"],
              "tag": request.get("tag"), "by": request.get("by"),
              "name": request.get("name"), "asked_at": request.get("at"),
              "state": "started", "at": time.time(), "detail": ""}
    with locked(config.base, wait=LOCK_WAIT_S) as mine:
        state = config.load()
        state["install"] = record
        if not mine:
            record.update(state="refused", detail="the updater is busy testing a "
                          "new version; try again when it says it is ready")
            config.save(state, keep_install=False)
            return "refused"
        try:
            # Said before anything is done: the panel that comes up holds
            # runs back while it reads "started".
            config.save(state, keep_install=False)
        except OSError as exc:
            log(f"Install of {record.get('tag')}: not started, the state could "
                f"not be written: {exc}")
            return "failed"
        try:
            outcome = _install(config, state, record, target or LocalTarget(
                str(config.base)), wait, sleep, backup, log)
        except Exception as exc:          # said in the panel, never lost
            record.update(state="failed", detail=f"{type(exc).__name__}: {exc}")
            outcome = "failed"
        record["at"] = time.time()
        config.save(state, keep_install=False)
        log(f"Install of {record.get('tag')}: {outcome}. {record['detail']}")
        return outcome


def _install(config, state, record, target, wait, sleep, backup, log) -> str:
    latest = state.get("latest") or {}
    release = record["release"]
    if latest.get("state") != "ready" or latest.get("release") != release:
        record.update(state="refused", detail="that version is no longer the "
                      "one ready to install")
        return "refused"
    releases = os.path.realpath(config.base / "releases")
    if not RELEASE.fullmatch(release) or os.path.dirname(os.path.realpath(
            config.base / "releases" / release)) != releases:
        record.update(state="refused", detail=f"{release!r} is not a release "
                      f"this updater made")
        return "refused"
    asked = record.get("asked_at")
    age = time.time() - asked if isinstance(asked, (int, float)) else None
    if age is None or not -5 < age < REQUEST_MAX_AGE_S:
        record.update(state="refused", detail="the request is too old to act "
                      "on: the panel stopped holding runs back for it. Ask again")
        return "refused"

    def not_idle(hold: bool = True) -> tuple[str | None, dict | None]:
        """Why not now, from the panel's status file -- which must also
        show the panel holding runs back for this very request, so no run
        can begin between this look and the restart."""
        status, now = rel.rig_state(target, config.status)
        problem = idle_problem(status, now)
        if not problem and hold and (status or {}).get("update_hold") != record["request"]:
            problem = ("the panel is not holding runs back for this install "
                       "(it restarted, or the hold ran out): ask again")
        return problem, status

    folder = f"{target.base}/releases/{release}"
    if target.run(f"test -f {rel._q(folder + '/VERSION.json')}")[0] != 0:
        record.update(state="refused", detail="that version's folder is gone; it "
                      "is fetched again at the next check")
        state["latest"] = None
        return "refused"
    deadline = time.monotonic() + HOLD_SHOWN_S
    problem, before = not_idle()
    while problem and "not holding" in problem and time.monotonic() < deadline:
        sleep(1)
        problem, before = not_idle()
    if problem:
        record.update(state="refused", detail=f"not now: {problem}")
        return "refused"
    differ = rel.unit_differences(target, release)
    if differ:
        record.update(state="refused", detail=(
            "this version changes the system units (" + "; ".join(differ)
            + "): deploy it from a laptop with tools/release.py deploy "
              "--install-units"))
        return "refused"
    if backup:
        log("Backing up the database first")
        backups = f"{target.base}/backups"
        database = config.data / "lab.db"
        code, out = target.run(
            f"cd {rel._q(str(ROOT))} && "
            f"(test -e {rel._q(backups)}/.vacuum-lab-backup || "
            f"python3 tools/backup.py init {rel._q(backups)} --db "
            f"{rel._q(str(database))}) && python3 tools/backup.py make --db "
            f"{rel._q(str(database))} --dest {rel._q(backups)} --no-status",
            timeout=1800)
        if code != 0:
            record.update(state="refused", detail=f"the backup failed, so "
                          f"nothing was changed: {out[-300:]}")
            return "refused"
    # The backup took a while: somebody may have started something meanwhile.
    problem, _ = not_idle()
    if problem:
        record.update(state="refused", detail=f"not now: {problem}")
        return "refused"

    previous = rel.current_release(target)
    log(f"Switching {previous} -> {release}")
    rel.switch(target, release)
    code, out = target.run(f"{target.sudo}systemctl restart {rel.SERVICE}",
                           timeout=150)
    # A restart that could not be made says nothing about the version (sudo
    # refused, systemd busy): it is gone back from, but may be asked again.
    blameless = code != 0
    problem = (f"restart failed: {out}" if code != 0 else
               rel.healthy(target, release, before, config.status, wait, sleep))
    if not problem:
        record.update(state="installed", detail=f"{latest['tag']} is running")
        state["running"] = {"release": release, "tag": latest["tag"],
                            "commit": latest["commit"],
                            "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime())}
        state["latest"] = None
        rel.prune(target)
        return "installed"

    log(f"  NOT HEALTHY: {problem}")
    if not blameless:
        state["refused"][latest["tag"]] = f"not healthy once installed: {problem}"
        latest.update(state="refused",
                      detail=f"not healthy once installed: {problem}")
    if not previous:
        record.update(state="failed", detail=f"not healthy ({problem}), and "
                      f"there is no release to go back to")
        return "failed"
    # The new panel holds runs back while the install reads "started"; if
    # something is going all the same, it is given a while to end -- then
    # the version that is not healthy goes regardless.
    deadline = time.monotonic() + ROLLBACK_WAIT_S
    while True:
        status, now = rel.rig_state(target, config.status)
        busy = idle_problem(status, now, force=True)
        if not busy or time.monotonic() >= deadline:
            break
        sleep(5)
    rel.switch(target, previous)
    code, out = target.run(f"{target.sudo}systemctl restart {rel.SERVICE}",
                           timeout=150)
    if busy:
        problem += f"; going back ended what was going ({busy})"
    back = (f"restart failed: {out}" if code != 0 else
            rel.healthy(target, previous, before, config.status, wait, sleep))
    if back:
        record.update(state="failed", detail=(
            f"not healthy ({problem}); going back to {previous} did NOT work "
            f"either: {back}. Look at the machine now: journalctl -u "
            f"{rel.SERVICE}"))
        return "failed"
    record.update(state="rolled_back", detail=f"not healthy ({problem}); back "
                  f"on {previous}")
    if not blameless:
        _remove_release(config.base, release, {previous})
    return "rolled_back"


# --- the command line ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default=rel.DEFAULT_BASE,
                        help=f"where the releases are (default {rel.DEFAULT_BASE})")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help=f"the panel's data folder, where its database is "
                             f"(default {DEFAULT_DATA})")
    parser.add_argument("--status", default=rel.DEFAULT_STATUS,
                        help=f"the panel's status file (default {rel.DEFAULT_STATUS})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="look for a new version, verify and test it")
    sub.add_parser("install", help="install the version the panel asked for")
    sub.add_parser("status", help="what the updater knows")
    p = sub.add_parser("trust", help="accept versions signed with this key")
    p.add_argument("name", help="whose key: a name for the record")
    p.add_argument("keyfile", help="their SSH public key (.pub)")
    p = sub.add_parser("retry", help="forget that a version was refused")
    p.add_argument("tag", help="the version, vYYYY.MM.DD[.N]")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    config = Config(args.base, args.data, args.status)
    try:
        if args.command == "check":
            print(check(config))
            return 0
        if args.command == "install":
            outcome = install(config)
            return 0 if outcome in ("", "installed") else 1
        if args.command == "trust":
            print(trust(args.name, Path(args.keyfile), config.signers))
            print(f"Versions signed with it are accepted ({config.signers}).")
            return 0
        if args.command == "retry":
            with locked(config.base, wait=LOCK_WAIT_S) as mine:
                if not mine:
                    raise UpdateError("the updater is busy; try again shortly")
                state = config.load()
                reason = state["refused"].pop(args.tag, None)
                if reason is None:
                    raise UpdateError(f"{args.tag} was not refused")
                if (state.get("latest") or {}).get("tag") == args.tag:
                    state["latest"] = None
                config.save(state)
            print(f"{args.tag} is tried again at the next check (it was: {reason})")
            return 0
        print(json.dumps({"running": running_version(config.base),
                          **config.load()}, indent=1))
        return 0
    except UpdateError as exc:
        print(f"autoupdate: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
