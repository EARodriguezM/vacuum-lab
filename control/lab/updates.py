"""New versions of the panel: what the updater on this machine has found,
and an admin's request to install one.

The updater is `tools/autoupdate.py`, run by systemd as the panel's own
user: it finds a signed version on GitHub, verifies and tests it, and keeps
its account of that in its own folder, with a copy, `update.json`, beside
the database. Nothing here reaches the network or touches a release. The
panel only

* reads that copy (cached on its modification time) for the poll, so
  Settings -> Server can show what is ready and everybody can see that an
  install is under way;
* writes an admin's request, `update-request.json`, which
  `vacuum-lab-install.path` turns into `autoupdate.py install`. That
  restarts the panel; the installer, not the panel, waits for it to come
  back healthy and goes back to the version before if it does not.

From the request until the installer has its verdict, the run engine
refuses new runs and power-downs (`RunEngine.hold_for_update`), and the
status file names the request the hold is for: the installer acts only
while it does, so no run can begin between its last look and the restart.
The hold is taken again by the panel that comes up while the install still
reads "started" -- the installer may yet go back, and that restart would end
a run begun meanwhile. It ends with the installer's verdict, when it says
it did not go ahead, after `HOLD_S` whatever happens, or -- if the installer
never takes the request -- after `PICKUP_S`, the request withdrawn.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time

from . import logs

#: Beside the database; tools/autoupdate.py names the same two files.
STATE_FILE = "update.json"
REQUEST_FILE = "update-request.json"

#: How long the installer has to take a request before the panel withdraws
#: it: the path unit starts it within a second when it is installed.
PICKUP_S = 60.0
#: The longest new runs are held back for one install: a backup, the switch,
#: the restart and a minute's health check, perhaps twice.
HOLD_S = 600.0
#: How many refused versions the panel lists.
REFUSED_SHOWN = 5
#: The installer's verdicts.
DONE = ("installed", "rolled_back", "refused", "failed")
#: How long a panel that comes up during an install holds runs back, from
#: its own start: the health check (a minute), the wait before going back
#: (two), and a margin. Its verdict normally ends the hold long before.
ADOPT_S = 300.0


class Updates:
    """The updater's state for the panel, and the one request at a time."""

    def __init__(self, folder: str | None, engine, database=None,
                 supervisor=None):
        #: The panel's data folder (beside the database); None: no updater.
        self.folder = folder
        self.engine = engine
        self.db = database
        #: Whose automatic recovery after a drive fault also refuses a request.
        self.supervisor = supervisor
        self._lock = threading.Lock()
        self._asking = False
        #: Writes the status file now (the Application sets it), so the hold
        #: is there before the request is.
        self.publish = None
        self._cache: tuple | None = None          # (mtime_ns, size, state)
        #: The install this panel waits on -- asked for here, or under way
        #: when it started -- or None.
        self.pending: dict | None = None
        #: Why the last request came to nothing, when the installer could
        #: not say so itself (it never started).
        self.problem: str | None = None
        self._adopt()

    def _path(self, name: str) -> str | None:
        return os.path.join(self.folder, name) if self.folder else None

    def read(self) -> dict | None:
        """update.json, or None when this machine has no updater."""
        path = self._path(STATE_FILE)
        if path is None:
            return None
        try:
            stat = os.stat(path)
        except OSError:
            return None
        cached = self._cache
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return cached[2]
        try:
            with open(path, encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            state = None
        if not isinstance(state, dict):
            state = None
        self._cache = (stat.st_mtime_ns, stat.st_size, state)
        return state

    def _install(self) -> dict:
        install = (self.read() or {}).get("install")
        return install if isinstance(install, dict) else {}

    def describe(self) -> dict | None:
        """What the poll carries: None when there is no updater here."""
        state = self.read()
        if state is None and self.pending is None and self.problem is None:
            return None
        state = state or {}
        latest = state.get("latest") if isinstance(state.get("latest"), dict) else None
        refused = state.get("refused") if isinstance(state.get("refused"), dict) else {}
        pending = self.pending
        return {
            "checked_at": state.get("checked_at"),
            "check_error": state.get("check_error"),
            "running": state.get("running"),
            "latest": latest and {key: latest.get(key) for key in (
                "tag", "release", "commit", "state", "detail", "title", "notes",
                "tagged_at", "signed_by", "tested_at", "older_than_running")},
            "refused": [{"tag": tag, "reason": reason}
                        for tag, reason in sorted(refused.items(),
                                                  reverse=True)[:REFUSED_SHOWN]],
            "install": state.get("install"),
            "pending": pending and {key: pending.get(key) for key in (
                "tag", "release", "name", "asked_at")},
            "problem": self.problem,
        }

    def hold_id(self) -> str | None:
        """The request runs are held back for now, for the status file: the
        installer acts only while it reads this."""
        pending = self.pending
        if pending is None or not self.engine.held():
            return None
        return pending["id"]

    def request(self, release: str, principal) -> dict:
        """An admin asks for the version that is ready to be installed."""
        with self._lock:
            if self.folder is None:
                return {"ok": False, "code": 404,
                        "error": "this panel has no updater"}
            if self.pending is not None or self._asking:
                who = (self.pending or {}).get("name") or "someone"
                return {"ok": False, "code": 409,
                        "error": f"an install has already been asked for, by {who}"}
            latest = (self.read() or {}).get("latest") or {}
            if latest.get("state") != "ready":
                return {"ok": False, "code": 409,
                        "error": "no version is ready to install"}
            if latest.get("release") != release:
                return {"ok": False, "code": 409,
                        "error": f"the version ready to install is now "
                                 f"{latest.get('tag')}: look again"}
            recovering = getattr(self.supervisor, "recovering", None) or {}
            if recovering.get("active"):
                return {"ok": False, "code": 409, "error": "not now: the pump is "
                        "being made safe after a fault"}
            self._asking = True
        # Not under this lock: the engine's may be held a while by a run
        # starting, and the supervisor's tick takes this one.
        try:
            tag = latest.get("tag")
            name = getattr(principal, "name", "") or "someone"
            refusal = self.engine.hold_for_update(
                f"version {tag} is being installed (asked for by {name})", HOLD_S)
            if refusal:
                return {"ok": False, "code": 409, "error": f"not now: {refusal}"}
            pending = {"id": secrets.token_hex(8), "release": release,
                       "tag": tag, "by": getattr(principal, "username", None),
                       "name": name, "asked_at": time.time(),
                       "since": time.monotonic(), "taken": False}
            with self._lock:
                self.pending = pending
                self.problem = None
            try:
                if self.publish is not None:
                    self.publish()
                self._write_request(pending)
            except Exception as exc:
                # Whatever failed, nothing was asked: no hold may outlive it,
                # in the engine or in the status file.
                with self._lock:
                    self.pending = None
                self.engine.unhold()
                try:
                    if self.publish is not None:
                        self.publish()
                except Exception as again:
                    logs.error("updates", again)
                return {"ok": False, "code": 500,
                        "error": f"the request could not be written: {exc}"}
            return {"ok": True, "tag": tag, "release": release}
        finally:
            self._asking = False

    def _write_request(self, pending: dict) -> None:
        path = self._path(REQUEST_FILE)
        temporary = path + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                             0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as fh:
            json.dump({"id": pending["id"], "release": pending["release"],
                       "tag": pending["tag"], "by": pending["by"],
                       "name": pending["name"], "at": pending["asked_at"]}, fh)
        # The path unit starts the installer on the final name only.
        os.replace(temporary, path)

    def _adopt(self) -> None:
        """At start: an install still "started" is one whose verdict is not
        in -- this panel may be the version being judged, or the one gone
        back to -- so runs are held back until it is. One already decided is
        put in the rig's record, once."""
        install = self._install()
        if install.get("state") in DONE:
            self._record(install)
            return
        at = install.get("at")
        if install.get("state") != "started" or not isinstance(at, (int, float)) \
                or not 0 <= time.time() - at < HOLD_S:
            return
        # Whatever the engine is doing: refusing new runs is always safe, and
        # an owed clean-up tried again at this start must not cost the hold.
        self.engine.hold_for_update(
            f"version {install.get('tag')} is being checked after its install",
            ADOPT_S, force=True)
        self.pending = {"id": install.get("request"), "tag": install.get("tag"),
                        "release": install.get("release"),
                        "name": install.get("name"), "by": install.get("by"),
                        "asked_at": install.get("asked_at"),
                        "since": time.monotonic() - HOLD_S + ADOPT_S,
                        "taken": True}

    def watch(self) -> None:
        """From the supervisor's tick: let runs start again once the install
        this panel waits on has its verdict, or will not happen. Never
        waits: a request being made holds the lock, and this skips a tick."""
        if self.pending is None or not self._lock.acquire(blocking=False):
            return
        try:
            pending = self.pending
            if pending is None:
                return
            install = self._install()
            mine = install.get("request") == pending["id"]
            age = time.monotonic() - pending["since"]
            if mine and install.get("state") in DONE:
                self._end(None)
                self._record(install)
                return
            if age > HOLD_S:
                self._end("the install did not say how it went in time; runs "
                          "may start again. Look at journalctl -u "
                          "vacuum-lab-install")
                return
            if not mine and not pending["taken"] and age > PICKUP_S:
                # Withdrawn -- unless the installer took it this instant,
                # in which case it is under way and the hold stays.
                try:
                    os.unlink(self._path(REQUEST_FILE))
                except FileNotFoundError:
                    pending["taken"] = True
                    return
                except OSError as exc:
                    logs.error("updates", exc)
                self._end("the installer did not take the request within a "
                          "minute, so nothing was changed. Is "
                          "vacuum-lab-install.path installed and running?")
        finally:
            self._lock.release()

    def _end(self, problem: str | None) -> None:
        self.pending = None
        self.problem = problem
        self.engine.unhold()

    def _record(self, install: dict) -> None:
        """An install's verdict in the rig's record, under who asked, once
        -- through the run engine's writer, so the database never holds up
        the supervisor's tick."""
        if self.db is None or not install.get("request"):
            return
        try:
            if self.db.meta("update_recorded") == str(install["request"]):
                return
        except Exception as exc:
            logs.error("updates", exc)
            return
        name = install.get("name") or "someone"
        tag = install.get("tag")
        if install.get("state") == "installed":
            level, said = "action", f"{name}: installed {tag}"
        elif install.get("state") == "refused":
            level, said = "warning", (f"{name}: installing {tag} did not go "
                                      f"ahead: {install.get('detail')}")
        else:
            level, said = "warning", (f"{name}: installing {tag} did not work: "
                                      f"{install.get('detail')}")
        at, user, request = time.time(), install.get("by"), str(install["request"])

        def write():
            self.db.add_event(None, level, said, {"install": install},
                              user=user, at=at)
            self.db.set_meta("update_recorded", request)
        queue = getattr(self.engine, "_write", None)
        if queue is not None:
            queue(write, f"{level}: {said}")
            return
        try:
            write()
        except Exception as exc:
            logs.error("updates", exc)
