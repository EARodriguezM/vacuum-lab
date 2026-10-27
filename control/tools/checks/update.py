"""New versions: found on GitHub, verified by their signature and their
files, tested here and installed only when an admin asks -- against a fake
GitHub on localhost and the release stand-in, so no network is needed."""

from __future__ import annotations

import collections
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from lab import db as dbmod
from lab import registry, runs
from lab import updates as updatesmod
from tools import autoupdate as up
from tools import release as rel
from tools.checks.common import attempt, check
from tools.checks.release import LocalTarget, current_name, reports

QUIET = lambda *a, **k: None                                       # noqa: E731
REPO = "lab/vacuum-lab"
RUNNING = "20261001-0000-aaaaaaaa"
#: A version's files: enough to unpack, parse and compare units.
FILES = {
    "control/webserver.py": (b"print('panel')\n", True),
    "control/lab/thing.py": (b"VALUE = 1\n", False),
    "control/deploy/vacuum-lab-web.service": (b"[Unit]\nDescription=panel\n", False),
    "control/deploy/99-vacuum-lab.rules": (b"# rules\n", False),
    "README.md": (b"outside control/, not part of a release\n", False),
}
PASSES = [sys.executable, "-c", "print('Passed 3, failed 0')"]
FAILS = [sys.executable, "-c", "import sys; print('FAILED 1'); sys.exit(1)"]
SLOW = [sys.executable, "-c", "import time; time.sleep(30)"]


def make_tar(files: dict, prefix: str = "", gz: bool = False) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz" if gz else "w") as tar:
        folders = sorted({prefix + "/".join(n.split("/")[:i]) + "/"
                          for n in files for i in range(1, n.count("/") + 1)})
        for folder in ([prefix] if prefix else []) + folders:
            info = tarfile.TarInfo(folder.rstrip("/"))
            info.type, info.mode = tarfile.DIRTYPE, 0o775
            tar.addfile(info)
        for name, (data, executable) in files.items():
            info = tarfile.TarInfo(prefix + name)
            info.size, info.mode = len(data), 0o775 if executable else 0o664
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def digest_of(files: dict) -> str:
    with tarfile.open(fileobj=io.BytesIO(make_tar(files))) as tar:
        return up.digest(tar)[0]


def keygen(path: Path) -> Path:
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test",
                    "-f", str(path)], check=True)
    return path


def sign(payload: bytes, key: Path, namespace: str = "git") -> bytes:
    with tempfile.TemporaryDirectory(prefix="selftest-sign-") as scratch:
        text = Path(scratch) / "payload"
        text.write_bytes(payload)
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n",
                        namespace, str(text)], check=True, capture_output=True)
        return (Path(scratch) / "payload.sig").read_bytes()


def tag_text(name: str, commit: str, digest: str, notes: str = "What changed.",
             named: str | None = None) -> bytes:
    return (f"object {commit}\ntype commit\ntag {named or name}\n"
            f"tagger Lab <lab@example.org> {int(time.time())} +0000\n\n"
            f"Version {name}\n\n{notes}\n\n{up.DIGEST_LINE} {digest}\n").encode()


class FakeGitHub:
    """The three addresses the updater asks, on one local port."""

    def __init__(self):
        self.tags: dict[str, dict] = {}
        self.tarballs: dict[str, bytes] = {}
        self.down = False
        self.hits: collections.Counter = collections.Counter()
        #: Which tag objects were asked for, in order.
        self.asked: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = urlparse(self.path).path
                if fake.down:
                    return self._send(503, b"down")
                if path == f"/{REPO}.git/info/refs":
                    fake.hits["refs"] += 1
                    return self._send(200, fake.advertisement())
                if path.startswith(f"/repos/{REPO}/git/tags/"):
                    fake.hits["tag"] += 1
                    sha = path.rsplit("/", 1)[-1]
                    fake.asked.append(sha)
                    tag = next((t for t in fake.tags.values()
                                if t["object"] == sha), None)
                    if tag is None:
                        return self._send(404, b"{}")
                    return self._send(200, json.dumps({"verification": {
                        "payload": tag["payload"], "signature": tag["signature"],
                    }}).encode())
                if path.startswith(f"/{REPO}/tar.gz/"):
                    fake.hits["tarball"] += 1
                    data = fake.tarballs.get(path.rsplit("/", 1)[-1])
                    return self._send(200 if data else 404, data or b"")
                self._send(404, b"")

            def _send(self, code, body):
                self.send_response(code)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def advertisement(self) -> bytes:
        def pkt(line: bytes) -> bytes:
            return b"%04x" % (len(line) + 4) + line
        refs = [("f" * 40, "HEAD"), ("e" * 40, "refs/heads/main"),
                # A tag that is not a tag object cannot be signed: ignored,
                # however new its name.
                ("d" * 40, "refs/tags/v2099.01.01")]
        for name, tag in sorted(self.tags.items()):
            refs += [(tag["object"], f"refs/tags/{name}"),
                     (tag["commit"], f"refs/tags/{name}^{{}}")]
        out = pkt(b"# service=git-upload-pack\n") + b"0000"
        for index, (sha, name) in enumerate(refs):
            line = f"{sha} {name}".encode()
            if index == 0:
                line += b"\0multi_ack side-band-64k agent=git/fake"
            out += pkt(line + b"\n")
        return out + b"0000"

    def publish(self, name: str, key: Path | None, files: dict = FILES, *,
                tamper: bool = False, named: str | None = None,
                namespace: str = "git") -> str:
        """A version on the fake GitHub; returns its commit."""
        commit = secrets.token_hex(20)
        payload = tag_text(name, commit, digest_of(files), named=named)
        signature = sign(payload, key, namespace) if key else None
        self.tags[name] = {"object": secrets.token_hex(20), "commit": commit,
                           "payload": payload.decode(),
                           "signature": signature.decode() if signature else None}
        served = dict(files)
        if tamper:
            served["control/lab/thing.py"] = (b"VALUE = 2\n", False)
        self.tarballs[commit] = make_tar(served, f"lab-vacuum-lab-{commit[:7]}/",
                                         gz=True)
        return commit

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Holder:
    """Just enough run engine for lab/updates.py."""

    def __init__(self):
        self.busy: str | None = None
        self.hold: str | None = None

    def hold_for_update(self, reason, seconds, force=False):
        if self.busy and not force:
            return self.busy
        self.hold = reason
        return None

    def unhold(self):
        self.hold = None

    def held(self):
        return self.hold


def write_status(path: Path, run: bool = False, release: str = RUNNING) -> None:
    path.write_text(json.dumps({
        "written": time.time(), "release": release, "ports": {}, "critical": 0,
        "run": {"id": 7, "procedure": "deposition"} if run else None}))


def test_update() -> None:
    print("\nNew versions: signed, checked, tested, installed when asked")
    folder = Path(tempfile.mkdtemp(prefix="selftest-update-"))
    try:
        _sections(folder)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _sections(folder: Path) -> None:
    check("the panel and the updater name the same two files",
          (updatesmod.STATE_FILE, updatesmod.REQUEST_FILE)
          == (up.STATE_FILE, up.REQUEST_FILE))

    # --- reading GitHub's list of tags ----------------------------------------------
    def tags_listed():
        fake = FakeGitHub()
        try:
            fake.tags["v2026.10.27"] = {"object": "1" * 40, "commit": "2" * 40}
            refs = up.parse_refs(fake.advertisement())
        finally:
            fake.close()
        tags = up.annotated_tags(refs)
        return (tags == {"v2026.10.27": {"object": "1" * 40, "commit": "2" * 40}}
                and refs.get("HEAD") == "f" * 40, f"{tags}")
    attempt("the list of tags is read as git reads it; only tag objects count",
            tags_listed)
    order = sorted(["v2026.10.27.2", "v2026.10.27", "v2026.9.30", "v2026.10.28",
                    "v2026.10.27.10", "2026.10.29", "v2026.10.27.1", "v2026.10.27.x"],
                   key=lambda n: up.version_key(n) or (0,))
    check("versions order by day, then by count; anything else is no version",
          [n for n in order if up.version_key(n)]
          == ["v2026.10.27", "v2026.10.27.2", "v2026.10.27.10", "v2026.10.28"],
          str(order))

    # --- the hash over control/ -------------------------------------------------------
    def same_both_ways():
        plain = up.digest(tarfile.open(fileobj=io.BytesIO(make_tar(FILES))))
        github = up.digest(tarfile.open(fileobj=io.BytesIO(
            make_tar(FILES, "lab-vacuum-lab-1234567/", gz=True))), strip=1)
        changed = dict(FILES)
        changed["control/lab/thing.py"] = (b"VALUE = 2\n", False)
        flipped = dict(FILES)
        flipped["control/lab/thing.py"] = (b"VALUE = 1\n", True)
        outside = dict(FILES)
        outside["docs/new.md"] = (b"more", False)
        return (plain == github and plain[1] == 4
                and digest_of(changed) != plain[0]
                and digest_of(flipped) != plain[0]
                and digest_of(outside) == plain[0],
                f"{plain[1]} files; GitHub's form {'the same' if plain == github else 'DIFFERENT'}")
    attempt("the hash is the same from git archive and from GitHub's tarball; "
            "a byte, an execute bit changes it, a file outside control/ not",
            same_both_ways)

    def odd_members_refused():
        refused = []
        for kind in ("link", "escape"):
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as tar:
                if kind == "link":
                    info = tarfile.TarInfo("control/lab/x.py")
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    tar.addfile(info)
                else:
                    data = b"x"
                    info = tarfile.TarInfo("control/../../outside.py")
                    info.size = 1
                    tar.addfile(info, io.BytesIO(data))
            try:
                up.digest(tarfile.open(fileobj=io.BytesIO(buffer.getvalue())),
                          into=folder / "odd")
            except up.Refused:
                refused.append(kind)
        return (refused == ["link", "escape"]
                and not (folder / "outside.py").exists(), f"refused: {refused}")
    attempt("a link or a path leaving control/ refuses the whole release",
            odd_members_refused)

    def notes_and_hash():
        text = tag_text("v2026.10.27", "a" * 40, "b" * 64, "Line one.\nLine two.")
        fields = up.parse_tag(text)
        problems = []
        for bad in (text.replace(up.DIGEST_LINE.encode(), b"other:"),
                    text + f"{up.DIGEST_LINE} {'c' * 64}\n".encode()):
            try:
                up.parse_tag(bad)
                problems.append("accepted")
            except up.Refused:
                pass
        return (fields["digest"] == "b" * 64 and fields["notes"]
                == "Line one.\nLine two." and fields["tag"] == "v2026.10.27"
                and fields["tagged_at"] and not problems, f"{fields['notes']!r}")
    attempt("a tag gives its notes and exactly one hash, or is refused",
            notes_and_hash)

    # --- signatures -----------------------------------------------------------------
    if shutil.which("ssh-keygen") is None:
        check("ssh-keygen is installed (the updater verifies with it)", False)
        return
    keys = folder / "keys"
    keys.mkdir()
    lab = keygen(keys / "lab")
    stranger = keygen(keys / "stranger")
    signers = folder / "allowed_signers"
    up.trust("lab", Path(str(lab) + ".pub"), signers)
    payload = tag_text("v2026.10.27", "a" * 40, "b" * 64)

    def verdict(text, signature, where=signers):
        try:
            return up.verify(text, signature, where)
        except up.Refused as exc:
            return f"refused: {exc}"
        except up.UpdateError as exc:
            return f"refused for now: {exc}"
    good = verdict(payload, sign(payload, lab))
    check("a tag signed with the trusted key verifies, under its name", good == "lab",
          good)
    for label, outcome in (
            ("a tag whose text was changed after signing is refused",
             verdict(payload.replace(b"What", b"Who"), sign(payload, lab))),
            ("a tag signed with any other key is refused",
             verdict(payload, sign(payload, stranger))),
            ("a signature made for something other than git is refused",
             verdict(payload, sign(payload, lab, "file"))),
            ("with no trusted key, every tag is refused -- for now: trusting "
             "one later lets it through",
             verdict(payload, sign(payload, lab), folder / "none").replace(
                 "refused for now", "refused") if "for now" in verdict(
                 payload, sign(payload, lab), folder / "none") else "accepted")):
        check(label, outcome.startswith("refused"), outcome[:90])

    # --- check: finding, verifying, testing -----------------------------------------
    base, data = folder / "base", folder / "data"
    (base / "releases" / RUNNING).mkdir(parents=True)
    (base / "releases" / RUNNING / "VERSION.json").write_text(json.dumps(
        {"release": RUNNING, "tag": "v2026.10.01", "commit": "a" * 40}))
    os.symlink(f"releases/{RUNNING}", base / "current")
    data.mkdir()
    shutil.copy(signers, base / up.SIGNERS)
    status = folder / "status.json"
    write_status(status)
    fake = FakeGitHub()
    config = up.Config(str(base), str(data), str(status), repository=REPO,
                       github=fake.url, api=fake.url, codeload=fake.url,
                       selftest=PASSES, selftest_poll=0.1)
    state = lambda: up.load_state(data)                            # noqa: E731
    try:
        _checks(folder, fake, config, lab, stranger, state, status)
        _installs(folder, fake, config, lab, state, status)
    finally:
        fake.close()
    _engine_hold(folder)
    _over_http(folder)
    _tagging(folder, lab)


def _checks(folder, fake, config, lab, stranger, state, status):
    base = config.base
    said = up.check(config, log=QUIET)
    check("with no newer version: up to date, nothing fetched",
          said == "up to date" and state().get("latest") is None
          and fake.hits["tarball"] == 0, said)

    commit = fake.publish("v2026.10.27", lab)
    said = up.check(config, log=QUIET)
    latest = state().get("latest") or {}
    unpacked = base / "releases" / str(latest.get("release"))
    version = json.loads((unpacked / "VERSION.json").read_text()) \
        if unpacked.is_dir() else {}
    check("a new signed version is verified, unpacked and tested: ready",
          latest.get("state") == "ready" and version.get("tag") == "v2026.10.27"
          and version.get("commit") == commit
          and (unpacked / "webserver.py").read_bytes() == FILES["control/webserver.py"][0]
          and os.access(unpacked / "webserver.py", os.X_OK)
          and not (unpacked / "README.md").exists()
          and latest.get("notes") == "What changed." and latest.get("signed_by") == "lab",
          f"{said}; {latest.get('detail')}")
    fetched = fake.hits["tarball"]
    said = up.check(config, log=QUIET)
    check("a version already ready is not fetched or tested again",
          fake.hits["tarball"] == fetched and state()["latest"]["state"] == "ready",
          said)

    # A newer one while the rig is busy: it waits, and is not tested.
    fake.publish("v2026.10.28", lab)
    write_status(status, run=True)
    said = up.check(config, log=QUIET)
    waiting = state().get("latest") or {}
    check("a version found during a run waits to be tested; the older one "
          "unpacked is removed",
          waiting.get("tag") == "v2026.10.28" and waiting.get("state") == "waiting"
          and "run 7" in waiting.get("detail", "") and not unpacked.exists(), said)
    write_status(status)
    said = up.check(config, log=QUIET)
    check("...and is tested once the rig is idle",
          (state().get("latest") or {}).get("state") == "ready", said)

    # A selftest stopped by a run starting.
    fake.publish("v2026.10.29", lab)
    config.selftest = SLOW
    timer = threading.Timer(1.0, write_status, (status,), {"run": True})
    timer.start()
    started = time.monotonic()
    said = up.check(config, log=QUIET)
    took = time.monotonic() - started
    timer.join()
    latest = state().get("latest") or {}
    check("a selftest is stopped as soon as a run starts, to run again later",
          latest.get("state") == "waiting" and took < 10
          and "stopped" in latest.get("detail", ""), f"{took:.1f} s; {said}")
    write_status(status)
    config.selftest = FAILS
    said = up.check(config, log=QUIET)
    refused = state().get("refused", {})
    check("a version whose selftest fails here is refused, and its folder removed",
          "v2026.10.29" in refused and "FAILED 1" in refused["v2026.10.29"]
          and not (base / "releases" / latest.get("release", "x")).exists(), said)
    config.selftest = PASSES
    asked = len(fake.asked)
    said = up.check(config, log=QUIET)
    check("a refused version is never tried again; the newest one before it is "
          "offered instead",
          fake.tags["v2026.10.29"]["object"] not in fake.asked[asked:]
          and (state().get("latest") or {}).get("tag") == "v2026.10.28"
          and state()["latest"]["state"] == "ready", said)

    for name, how, expect in (
            ("v2026.10.30", {"key": None}, "not signed"),
            ("v2026.10.31", {"key": stranger}, "not trusted"),
            ("v2026.11.01", {"key": lab, "tamper": True}, "not the ones"),
            ("v2026.11.02", {"key": lab, "named": "v2026.11.09"}, "published as")):
        key = how.pop("key")
        fake.publish(name, key, **how)
        said = up.check(config, log=QUIET)
        reason = state().get("refused", {}).get(name, "")
        check(f"refused: {expect}", expect in reason
              and not any(p.name.endswith(".partial") for p in (base / "releases").iterdir()),
              reason[:90] or said)

    fake.publish("v2026.11.03", lab)
    fake.down = True
    said = up.check(config, log=QUIET)
    check("GitHub not answering is said, not held against any version",
          "503" in (state().get("check_error") or "")
          and "v2026.11.03" not in state().get("refused", {}), said)
    fake.down = False
    said = up.check(config, log=QUIET)
    check("...and the next check carries on",
          (state().get("latest") or {}).get("tag") == "v2026.11.03"
          and state().get("check_error") is None, said)


def _installs(folder, fake, config, lab, state, status):
    base, data = config.base, config.data
    etc = folder / "etc"
    target = LocalTarget(base, units=etc)
    for name in ("vacuum-lab-web.service", "99-vacuum-lab.rules"):
        (etc / name).write_bytes(FILES[f"control/deploy/{name}"][0])
    target.status = {"written": target.clock(), "run": None, "release": RUNNING,
                     "ports": {}, "critical": 0}
    target.on_restart = reports(current_name)
    database = dbmod.Database(str(data / "lab.db"))
    holder = Holder()
    olga = SimpleNamespace(name="Olga", username="olga")

    def go(**kwargs):
        return up.install(config, target=target, wait=1.0,
                          sleep=lambda s: time.sleep(0.05), log=QUIET, **kwargs)

    def ask(updates):
        """An admin's request, and the panel's status file saying it holds
        runs back for it -- as the route writes it at once."""
        asked = updates.request(state()["latest"]["release"], olga)
        target.status = {**target.status, "written": target.clock(),
                         "update_hold": updates.hold_id()}
        return asked

    def detail():
        return (state().get("install") or {}).get("detail", "")

    check("with no request, install does nothing", go() == "" and
          target.restarts == 0)
    saved = (updatesmod.PICKUP_S, updatesmod.HOLD_S, up.ROLLBACK_WAIT_S)
    try:
        updates = updatesmod.Updates(str(data), holder, database)
        ready = state()["latest"]
        asked = ask(updates)
        # The panel that comes up during the health check: the install
        # still reads "started", so it holds runs back until the verdict.
        newcomer = {}

        def comes_up(t):
            reports(current_name)(t)
            newcomer["holder"] = Holder()
            newcomer["updates"] = updatesmod.Updates(str(data), newcomer["holder"],
                                                     database)
        target.on_restart = comes_up
        outcome = go()
        record = state().get("install") or {}
        backups = list((base / "backups").glob("*/lab-*/MANIFEST.json"))
        check("an asked-for version is backed up, switched to and restarted into",
              asked.get("ok") and outcome == "installed"
              and current_name(target) == ready["release"]
              and target.restarts == 1 and record.get("state") == "installed"
              and record.get("by") == "olga" and state().get("latest") is None
              and len(backups) == 1,
              f"{outcome}: {record.get('detail')}; {len(backups)} backup(s)")
        check("the copy made before an install does not count as the lab's backup",
              not (data / "backup-status.json").exists())
        recorded = lambda: [e for e in database.events(limit=50)     # noqa: E731
                            if "installed v2026.11.03" in e["message"]]
        held = newcomer["holder"].hold
        before = len(recorded())
        newcomer["updates"].watch()
        updatesmod.Updates(str(data), Holder(), database)
        check("the panel that comes up holds runs back until the verdict, then "
              "records it once, under who asked",
              held and "being checked" in held and newcomer["holder"].hold is None
              and before == 0 and len(recorded()) == 1
              and recorded()[0].get("user") == "olga",
              f"held: {held}; {len(recorded())} record(s)")
        installed = ready["release"]

        def new_ready(name):
            fake.publish(name, lab)
            up.check(config, log=QUIET)
            return state()["latest"]

        # Not healthy: the panel keeps reporting the release before.
        ready = new_ready("v2026.11.04")
        target.on_restart = reports(lambda t: installed)
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        check("a version that does not come up healthy is switched back by itself, "
              "and never offered again",
              outcome == "rolled_back" and current_name(target) == installed
              and target.restarts == 3 and "v2026.11.04" in state()["refused"]
              and not (base / "releases" / ready["release"]).exists(),
              f"{outcome}: {detail()[:80]}")
        updates.watch()

        # Up, reporting the new release -- but the pump does not answer.
        ready = new_ready("v2026.11.05")
        target.status = {**target.status, "connected": {"pump": True}}

        def pump_silent(t):
            ours = current_name(t)
            t.status = {"written": t.clock(), "run": None, "release": ours,
                        "ports": {}, "critical": 0,
                        "connected": {"pump": ours == installed}}
        target.on_restart = pump_silent
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        check("a version that cannot reach an instrument that answered before is "
              "not healthy, and is switched back",
              outcome == "rolled_back" and current_name(target) == installed
              and "pump" in detail(), f"{outcome}: {detail()[:80]}")
        updates.watch()

        # Connected, but its readings stopped: a sampler that hangs.
        ready = new_ready("v2026.11.07")
        target.status = {**target.status, "fresh": {"pump": True}}

        def pump_hung(t):
            ours = current_name(t)
            t.status = {"written": t.clock(), "run": None, "release": ours,
                        "ports": {}, "critical": 0, "connected": {"pump": True},
                        "fresh": {"pump": ours == installed}}
        target.on_restart = pump_hung
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        check("...nor one whose instrument stays connected but stops reading",
              outcome == "rolled_back" and "pump" in detail(),
              f"{outcome}: {detail()[:80]}")
        updates.watch()
        target.status = {**target.status, "connected": {}, "fresh": {}}

        # Well at first sight, then a critical alarm: the check waits.
        ready = new_ready("v2026.11.08")
        late = {"reads": 0}

        def alarm_later(t, command):
            if command.startswith("date +%s.%N; cat") and current_name(t) == \
                    ready["release"] and t.status.get("release") == ready["release"]:
                late["reads"] += 1
                if late["reads"] == 2:
                    t.status = {**t.status, "critical": 1}
        target.on_restart = reports(current_name)
        target.before_command = alarm_later
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        target.before_command = None
        check("a release is judged only after it has run a while: an alarm "
              "after the first good look still sends it back",
              outcome == "rolled_back" and "critical" in detail(),
              f"{outcome}: {detail()[:80]}")
        updates.watch()

        # Not healthy, and something going on the new panel all the same.
        ready = new_ready("v2026.11.09")
        up.ROLLBACK_WAIT_S = 0.3

        def busy_and_wrong(t):
            ours = current_name(t)
            t.status = {"written": t.clock(), "release": installed, "ports": {},
                        "critical": 0,
                        "run": {"id": 5, "procedure": "hold"} if ours != installed
                        else None}
        target.on_restart = busy_and_wrong
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        check("going back waits a while for a run to end, then goes, and says so",
              outcome == "rolled_back" and "ended what was going" in detail()
              and current_name(target) == installed, f"{outcome}: {detail()[:90]}")
        updates.watch()
        up.ROLLBACK_WAIT_S = saved[2]
        target.on_restart = reports(current_name)

        # A restart that could not be made says nothing about the version.
        ready = new_ready("v2026.11.10")
        once = {"done": False}

        def refuse_restart_once(command):
            if "systemctl restart" in command and not once["done"]:
                once["done"] = True
                return True
            return False
        target.fail_on = refuse_restart_once
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        target.fail_on = None
        latest = state().get("latest") or {}
        check("a restart that fails is gone back from, and the version may be "
              "asked for again",
              outcome == "rolled_back" and "v2026.11.10" not in state()["refused"]
              and latest.get("state") == "ready"
              and (base / "releases" / ready["release"]).is_dir(),
              f"{outcome}: {detail()[:80]}")
        updates.watch()
        target.status = {**target.status, "connected": {}}

        ready = new_ready("v2026.11.06")
        restarts = target.restarts
        target.status = {**target.status, "run": {"id": 9, "procedure": "hold"}}
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        outcome = go(backup=False)
        updates.watch()
        check("a run going refuses the install; nothing is switched, runs may "
              "start again, and the refusal is recorded",
              outcome == "refused" and current_name(target) == installed
              and target.restarts == restarts and holder.hold is None
              and updates.pending is None
              and any("did not go ahead" in e["message"]
                      for e in database.events(limit=50)), detail())
        target.status = {**target.status, "run": None}

        ask(updates)
        target.status = {**target.status, "update_hold": None}
        outcome = go(backup=False)
        updates.watch()
        check("the install goes ahead only while the panel's status file shows it "
              "holding runs back for that very request",
              outcome == "refused" and "not holding" in detail()
              and target.restarts == restarts, detail()[:80])

        ask(updates)
        request = json.loads((data / up.REQUEST_FILE).read_text())
        request["at"] -= 600
        (data / up.REQUEST_FILE).write_text(json.dumps(request))
        outcome = go(backup=False)
        updates.watch()
        check("a request left from long ago is not acted on",
              outcome == "refused" and "too old" in detail(), detail()[:80])

        (etc / "vacuum-lab-web.service").write_bytes(b"[Unit]\nchanged\n")
        ask(updates)
        outcome = go(backup=False)
        check("a version that would change the system units is left to "
              "release.py deploy --install-units",
              outcome == "refused" and "--install-units" in detail()
              and target.restarts == restarts, outcome)
        (etc / "vacuum-lab-web.service").write_bytes(
            FILES["control/deploy/vacuum-lab-web.service"][0])
        updates.watch()

        (data / up.REQUEST_FILE).write_text(json.dumps(
            {"id": "x1", "release": "20200101-0000-00000000"}))
        outcome = go(backup=False)
        check("a request for anything but the version ready is refused",
              outcome == "refused" and "no longer" in detail(), outcome)

        # The panel's folder is one a panel gone wrong could write: the
        # installer believes only its own state, and only its own names.
        forged = up.load_state(data)
        forged["latest"] = {**forged["latest"], "release": "../../escape"}
        up.save_state(data, forged)
        (data / up.REQUEST_FILE).write_text(json.dumps(
            {"id": "x2", "release": "../../escape", "at": time.time()}))
        outcome = go(backup=False)
        first = detail()
        own = config.load()
        own["latest"] = {**own["latest"], "release": "../../escape"}
        up.save_state(base, own)
        (data / up.REQUEST_FILE).write_text(json.dumps(
            {"id": "x3", "release": "../../escape", "at": time.time()}))
        second = go(backup=False)
        check("the installer ignores the panel's copy of its state, and never "
              "switches to a folder it did not make",
              outcome == "refused" and "no longer" in first and second == "refused"
              and "not a release" in detail()
              and current_name(target) == installed, detail()[:80])
        own["latest"] = {**own["latest"], "release": ready["release"]}
        up.save_state(base, own)
        up.save_state(data, own)

        # Nobody takes the request: withdrawn, runs released.
        updatesmod.PICKUP_S = 0.2
        updates = updatesmod.Updates(str(data), holder, database)
        ask(updates)
        time.sleep(0.3)
        updates.watch()
        check("a request nobody takes within the minute is withdrawn, and runs "
              "may start again",
              not (data / up.REQUEST_FILE).exists() and holder.hold is None
              and "vacuum-lab-install.path" in (updates.problem or ""),
              updates.problem or "still waiting")
        # Taken the instant before it would be withdrawn: the hold stays.
        ask(updates)
        taken = up.claim_request(data)
        time.sleep(0.3)
        updates.watch()
        kept = holder.hold is not None and updates.pending is not None
        current = up.load_state(data)
        current["install"] = {"request": taken["id"], "state": "refused",
                              "detail": "busy", "at": time.time()}
        up.save_state(data, current)
        updates.watch()
        check("a request the installer has taken is not withdrawn under it; its "
              "refusal ends the hold",
              kept and holder.hold is None and updates.pending is None)
        # Taken, and then nothing: the installer died, or its record was lost.
        updatesmod.HOLD_S = 0.4
        ask(updates)
        up.claim_request(data)
        time.sleep(0.5)
        updates.watch()
        check("a hold whose install never reports ends after its time all the same",
              holder.hold is None and updates.pending is None
              and "in time" in (updates.problem or ""), updates.problem or "")
        # The status file cannot be written: nothing is asked, nothing held.
        def broken():
            raise ValueError("status could not be built")
        updates.publish = broken
        failed = ask(updates)
        updates.publish = None
        check("a request that cannot be published leaves no hold and no request",
              not failed.get("ok") and holder.hold is None
              and updates.pending is None and not (data / up.REQUEST_FILE).exists(),
              failed.get("error", ""))
        updatesmod.HOLD_S = saved[1]
        holder.busy = "run 3 is going: a restart would end it"
        refused = updates.request(ready["release"], olga)
        check("the panel refuses a request while the rig is busy, and writes none",
              not refused.get("ok") and refused.get("code") == 409
              and "run 3" in refused.get("error", "")
              and not (data / up.REQUEST_FILE).exists(), refused.get("error", ""))
        holder.busy = None
        said = up.main(["--base", str(base), "--data", str(data), "retry",
                        "v2026.10.29"])
        check("a refused version can be tried again on purpose (retry)",
              said == 0 and "v2026.10.29" not in config.load()["refused"])
        check("a laptop deploy does not go ahead while an install is held for",
              "install" in (rel.idle_problem(
                  {"written": time.time(), "run": None, "update_hold": "x"},
                  time.time()) or ""))
        recovering = updatesmod.Updates(
            str(data), holder, database,
            supervisor=SimpleNamespace(recovering={"active": True}))
        refused = recovering.request(ready["release"], olga)
        check("...or while the pump is being made safe after a fault",
              not refused.get("ok") and "made safe" in refused.get("error", "")
              and holder.hold is None, refused.get("error", ""))
    finally:
        updatesmod.PICKUP_S, updatesmod.HOLD_S, up.ROLLBACK_WAIT_S = saved
        database.close()


def _engine_hold(folder):
    database = dbmod.Database(str(folder / "engine.db"))
    engine = runs.RunEngine(database, registry.Registry(), roles=())
    try:
        first = engine.hold_for_update("version v2026.10.27 is being installed", 60)
        start = engine.start("hold", {})
        down = engine.shutdown_pump()
        engine.unhold()
        after = engine.start("hold", {})
        check("while an install is under way, runs and power-downs are refused",
              first is None and "v2026.10.27" in start.get("error", "")
              and "v2026.10.27" in down.get("error", "")
              and "v2026.10.27" not in after.get("error", ""),
              start.get("error", "")[:80])
        engine.hold_for_update("short", 0.2)
        time.sleep(0.3)
        check("a hold ends by itself when its time is up", engine.held() is None)
        engine._checklist.append({"actuator": "magnetron", "id": 1, "run_id": 1,
                                  "instruction": "Switch the magnetron off.",
                                  "since": time.time()})
        refused = engine.hold_for_update("x", 60)
        engine._checklist.clear()
        check("an install is not held for while a run's steps by hand are owed",
              refused and "by hand" in refused and engine.held() is None, refused)
        stop = threading.Event()
        engine._shutdown_thread = threading.Thread(target=stop.wait, daemon=True)
        engine._shutdown_thread.start()
        try:
            refused = engine.hold_for_update("x", 60)
        finally:
            stop.set()
            engine._shutdown_thread.join(5)
        check("...nor while a power-down is under way",
              refused and "power-down" in refused and engine.held() is None, refused)

        # The panel that comes up mid-install holds runs back whatever it is
        # doing -- here an owed step by hand would refuse an ordinary hold.
        adopt = folder / "adopt"
        adopt.mkdir()
        up.save_state(adopt, {"refused": {}, "install": {
            "request": "r1", "state": "started", "tag": "v2026.10.27",
            "release": "20261027-1200-abcdef12", "name": "Olga", "by": "olga",
            "at": time.time() - 30}})
        engine._checklist.append({"actuator": "magnetron", "id": 2, "run_id": 1,
                                  "instruction": "Switch the magnetron off.",
                                  "since": time.time()})
        adopted = updatesmod.Updates(str(adopt), engine, database)
        held = engine.held()
        engine._checklist.clear()
        check("a panel that comes up during an install holds runs back until "
              "the verdict, even with a step by hand owed",
              held and "being checked" in held and adopted.pending is not None,
              str(held))
        engine.unhold()
    finally:
        engine.close()
        database.close()


def _over_http(folder):
    import urllib.error
    import urllib.request
    from lab import api as apimod
    from lab import auth as authmod

    data = folder / "http"
    data.mkdir()
    up.save_state(data, {"refused": {}, "latest": {
        "tag": "v2026.10.27", "release": "20261027-1200-abcdef12",
        "state": "ready", "commit": "c" * 40}})
    database = dbmod.Database(str(data / "lab.db"))
    accounts = authmod.Auth(database, "chamber-1")
    engine = runs.RunEngine(database, registry.Registry(), roles=())
    updates = updatesmod.Updates(str(data), engine, database)
    status_file = data / "run" / "status.json"
    application = apimod.Application(registry.Registry(), database, engine,
                                     auth=accounts, updates=updates,
                                     chamber={"id": "chamber-1", "name": "C1"},
                                     status_file=str(status_file))
    order = {}
    published = updates.publish

    def spy():
        order["request_there"] = (data / up.REQUEST_FILE).exists()
        published()
    updates.publish = spy
    handler = type("UpdateHandler", (apimod.Handler,),
                   {"app": application, "log_message": lambda *a: None})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"

    def call(route, body=None, token=None):
        request = urllib.request.Request(
            url + route, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, (json.loads(raw) if raw[:1] == b"{" else {})

    try:
        authmod.bootstrap_owner(database, "chamber-1", "olga", "olga-pass-1", "Olga")
        owner = call("/api/login", {"username": "olga",
                                    "password": "olga-pass-1"})[1]["token"]
        call("/api/signup", {"username": "ana", "name": "Ana",
                             "password": "ana-pass-1"})
        ana_id = next(p["id"] for p in call("/api/users", token=owner)[1]["people"]
                      if p["username"] == "ana")
        call("/api/users/approve", {"id": ana_id, "role": "operator"}, owner)
        ana = call("/api/login", {"username": "ana",
                                  "password": "ana-pass-1"})[1]["token"]
        release = "20261027-1200-abcdef12"
        body = {"release": release, "confirm": True}
        operator = call("/api/update/install", body, ana)
        bare = call("/api/update/install", {"release": release}, owner)
        stale = call("/api/update/install", {**body, "release": "x"}, owner)
        check("only an admin may install a version, confirmed, naming the one ready",
              operator[0] == 403 and bare[0] == 400 and stale[0] == 409
              and not (data / up.REQUEST_FILE).exists(),
              f"operator {operator[0]}, unconfirmed {bare[0]}, stale {stale[0]}")
        asked = call("/api/update/install", body, owner)
        hold = application.status().get("update_hold")
        again = call("/api/update/install", body, owner)
        seen = call("/api/state", token=ana)[1].get("update") or {}
        started = engine.start("hold", {})
        check("the admin's request is written, held runs back, shown to everyone, "
              "and asked once",
              asked[0] == 200 and (data / up.REQUEST_FILE).exists()
              and again[0] == 409 and (seen.get("pending") or {}).get("tag")
              == "v2026.10.27" and "being installed" in started.get("error", "")
              and any("asked for version v2026.10.27" in e["message"]
                      for e in database.events(limit=20)),
              f"{asked[0]} {asked[1].get('error', '')}; again {again[0]}")
        written = json.loads(status_file.read_text()) if status_file.exists() else {}
        check("the status file names the request runs are held back for, "
              "written before the request exists",
              hold and hold == (updates.pending or {}).get("id")
              and written.get("update_hold") == hold
              and order.get("request_there") is False, str(hold))
        engine._checklist.append({"actuator": "gas", "id": 7, "run_id": 1,
                                  "instruction": "Shut the argon.",
                                  "since": time.time()})
        done = call("/api/run/checklist", {"id": 7}, ana)
        check("confirming a step left by hand is never held back by an install",
              done[0] == 200 and done[1].get("ok") and not engine.checklist(),
              f"{done[0]} {done[1].get('error', '')}")
    finally:
        server.shutdown()
        server.server_close()
        engine.close()
        database.close()


def _tagging(folder, lab):
    """tools/release.py tag on the laptop and tools/autoupdate.py on the lab PC
    must agree on the signature and the hash: one signs what the other checks."""
    if shutil.which("git") is None:
        check("release.py tag (skipped: no git on this machine)", True)
        return
    repo = folder / "repo"
    (repo / "control" / "tools").mkdir(parents=True)
    (repo / "control" / "webserver.py").write_text("print('panel')\n")
    (repo / "control" / "tools" / "run.sh").write_text("#!/bin/sh\n")
    os.chmod(repo / "control" / "tools" / "run.sh", 0o755)
    (repo / "README.md").write_text("not in a release\n")

    def git(*args):
        return subprocess.run(["git", "-c", "commit.gpgsign=false", *args],
                              cwd=repo, capture_output=True, text=True, check=True).stdout
    git("init", "-q", "-b", "main")
    for key, value in (("user.name", "Lab"), ("user.email", "lab@example.org"),
                       ("gpg.format", "ssh"), ("user.signingkey", str(lab))):
        git("config", key, value)
    git("add", "-A")
    git("commit", "-q", "-m", "first")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    args = SimpleNamespace(commit="HEAD", name="v2026.10.27", message="Notes here.",
                           notes_file=None, not_main=False)

    def signed_and_agreed():
        name = rel.tag(args, log=QUIET, root=repo / "control")
        raw = subprocess.run(["git", "cat-file", "tag", name], cwd=repo,
                             capture_output=True, check=True).stdout
        cut = raw.find(b"-----BEGIN SSH SIGNATURE-----")
        signer = up.verify(raw[:cut], raw[cut:], folder / "allowed_signers")
        fields = up.parse_tag(raw[:cut])
        archive = subprocess.run(["git", "archive", "--format=tar",
                                  "--prefix=lab-vacuum-lab-1234567/", "HEAD"],
                                 cwd=repo, capture_output=True, check=True).stdout
        found = up.digest(tarfile.open(fileobj=io.BytesIO(archive)), strip=1)
        return (signer == "lab" and fields["digest"] == found[0] and found[1] == 2
                and fields["notes"] == "Notes here.", f"{signer}, {found[1]} files")
    attempt("a tag made by release.py verifies and hashes the same on the lab "
            "PC's side", signed_and_agreed)

    def refuses(label, expect, **changes):
        def test():
            try:
                rel.tag(SimpleNamespace(**{**vars(args), **changes}), log=QUIET,
                        root=repo / "control")
                return False, "signed"
            except rel.ReleaseError as exc:
                return expect in str(exc), str(exc)[:90]
        attempt(label, test)
    (repo / "control" / "webserver.py").write_text("print('later')\n")
    git("commit", "-q", "-am", "second")
    refuses("a commit not on origin/main is not signed", "not on origin/main",
            name="v2026.10.28")
    git("config", "gpg.format", "openpgp")
    refuses("without SSH signing set up, release.py says how", "not set up to sign",
            name="v2026.10.28", not_main=True)
