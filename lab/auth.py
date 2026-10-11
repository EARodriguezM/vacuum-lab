"""Who may watch the rig, who may drive it, and who is driving it right now.

The panel used to trust anyone who could reach its port. On a campus network
that is anyone at all, so every request now carries a session, and what the
session may do depends on its role in this chamber:

    viewer     sees everything: live state, history, runs, exports
    operator   drives the rig -- while holding control
    admin      changes the rig's set-up, approves accounts, and is the only
               role that may override an interlock
    owner      exactly one per chamber; an admin who can also appoint admins

Roles are per chamber because a lab with two rigs gives one person different
rights on each. Every account is a viewer at most until somebody with the
right to says otherwise: a sign-up waits for an admin, and the owner is made
on the machine itself (tools/users.py), so nobody on the network can claim
the rig first.

**Control is not the same as permission.** Two operators on two tablets must
not both be driving one chamber, so whoever acts first takes *control*: a
lease that the holder's own polling renews every couple of seconds and that
lapses LEASE_S after the polling stops, so a tablet that dies frees the rig
by itself. Anything that moves hardware needs it. What makes the rig *safer*
does not: making the pump safe, stopping a run without powering down,
cancelling a power-down and acknowledging alarms are open to every operator,
the way an emergency stop does not wait its turn (lab/api.py, POST_RULES).

**Walking away.** A session left open on a shared tablet is somebody else's
hands on the rig. After IDLE_S without a touch, a person's session keeps
watching but may not act until the password is typed again; on a paired
tablet the panel signs them out altogether and falls back to the tablet's
own session. The server enforces the first part, so a panel that never
signs anyone out still cannot act on a stale session. The person whose run
is going gets RUN_IDLE_S instead: a deposition is mostly watching, and its
guided steps must not each start with a password.

**Clocks.** Everything kept in memory -- control, the idle timer, password
throttling, pairing codes -- is timed with `time.monotonic()`. A Raspberry Pi
has no clock battery and boots with the wrong time until the network
corrects it; measured on the wall clock, that correction could make control
outlive its holder by hours. The wall clock is used only for what must
survive a restart: session expiry and the last-touch time stored with each
session.

**Paired tablets.** A tablet on the wall must keep showing the rig -- and keep
sounding its alarms -- with nobody signed in. An admin pairs it once with a
one-time code; it gets a viewer session that never expires and can never be
given a higher role.

Passwords are scrypt hashes from hashlib, so there is no new dependency.
Sessions are random tokens of which only a SHA-256 is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass

ROLES = ("viewer", "operator", "admin", "owner")
RANK = {role: index for index, role in enumerate(ROLES)}

#: A person's session may act only this long after their last touch.
IDLE_S = 15 * 60
#: ...and this long for the person whose run is going (decided 2026-10-11).
RUN_IDLE_S = 60 * 60
#: How long a person stays signed in at all, watching, from signing in.
SESSION_S = 7 * 24 * 3600
#: Control lapses this long after the holder's panel stops polling.
LEASE_S = 15.0
#: A tablet pairing code is good for this long, and only once.
PAIRING_S = 15 * 60
#: active_at is written back at most this often, not on every click.
ACTIVE_WRITE_S = 30.0

#: scrypt cost: 16 MB of memory per guess, whoever is guessing.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1

MIN_PASSWORD = 8
MAX_PASSWORD = 128
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
#: No I, O, 0 or 1: a code read off one screen and typed into another.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Sign-ups an address may make per hour, and pending ones the rig will hold.
SIGNUPS_PER_HOUR = 5
MAX_PENDING = 50


# --- passwords and tokens --------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode("utf-8"),
                                salt=base64.b64decode(salt), n=int(n), r=int(r),
                                p=int(p), dklen=len(expected))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_code(groups: int = 3, size: int = 3) -> str:
    """A short code to read off one screen and type into another."""
    return "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(size))
                    for _ in range(groups))


def normalise_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(code or "").upper())


def check_username(username: str) -> str | None:
    if not USERNAME.match(username or ""):
        return ("A username is 3 to 32 characters: lower-case letters, digits, "
                "dots, dashes or underscores, starting with a letter or digit.")
    return None


def check_password(password: str, username: str = "") -> str | None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"A password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"A password may have at most {MAX_PASSWORD} characters."
    if username and password.strip().lower() == username.lower():
        return "The password cannot be the username."
    return None


# --- who is asking ---------------------------------------------------------

@dataclass
class Principal:
    """The account behind one request, with its role in this chamber."""

    user_id: int
    username: str
    name: str
    kind: str                   # person | device | local
    role: str
    session: str | None         # the token's hash; None with accounts off
    #: Seconds since this session's last deliberate touch.
    inactive_s: float = 0.0
    #: How long it may go untouched and still act (IDLE_S or RUN_IDLE_S).
    idle_limit: float = IDLE_S
    must_change: bool = False

    @property
    def rank(self) -> int:
        return RANK.get(self.role, -1)

    def at_least(self, role: str) -> bool:
        return self.rank >= RANK[role]

    def idle(self) -> bool:
        """Whether this session must give its password again before acting."""
        return self.kind == "person" and self.inactive_s > self.idle_limit

    def may_operate(self) -> bool:
        return (self.kind in ("person", "local") and self.at_least("operator")
                and not self.idle() and not self.must_change)


#: Every request, when the server runs with --no-auth. It is the owner, never
#: idle, and has nobody to share control with.
LOCAL = Principal(0, "local", "Operator", "local", "owner", None)


class Throttle:
    """Makes guessing passwords slow.

    The first FREE failures for a name, or from an address, cost nothing;
    after that each attempt must wait a doubling delay, up to MAX_S. A
    success clears the name's count. An address's count is left to expire
    on its own, so signing in to one's own account does not reset the clock
    for guessing at somebody else's.
    """

    FREE = 5
    BASE_S = 30.0
    MAX_S = 15 * 60.0

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}

    def wait(self, *keys: str) -> float:
        now = time.monotonic()
        longest = 0.0
        with self._lock:
            for key in keys:
                count, last = self._failures.get(key, (0, 0.0))
                if count < self.FREE:
                    continue
                delay = min(self.BASE_S * 2 ** (count - self.FREE), self.MAX_S)
                longest = max(longest, last + delay - now)
        return max(0.0, longest)

    def fail(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in keys:
                count, last = self._failures.get(key, (0, 0.0))
                if now - last > 2 * self.MAX_S:
                    count = 0
                self._failures[key] = (count + 1, now)
            if len(self._failures) > 5000:        # bounded under a flood
                cutoff = now - 2 * self.MAX_S
                self._failures = {k: v for k, v in self._failures.items()
                                  if v[1] > cutoff}

    def clear(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)


def refusal(error: str, status: int, **extra) -> dict:
    """A refused request: the HTTP status travels in the dict to the handler."""
    return {"ok": False, "error": error, "status": status, **extra}


# --- the rules -------------------------------------------------------------

class Auth:
    """Accounts, sessions, roles and control for one chamber."""

    def __init__(self, database, chamber: str, enabled: bool = True,
                 idle_s: float = IDLE_S, run_idle_s: float = RUN_IDLE_S,
                 runner=None):
        self.db = database
        self.chamber = chamber
        self.enabled = enabled
        #: How long a person may go untouched and still act, and how long
        #: when the run that is going is theirs (webserver.py --idle-minutes,
        #: --run-idle-minutes).
        self.idle_s = idle_s
        self.run_idle_s = run_idle_s
        #: Returns the username that started the run in progress, or None.
        self.runner = runner
        self.throttle = Throttle()
        self._lock = threading.RLock()
        self._control: dict | None = None
        self._pairing: dict[str, tuple[int, float]] = {}
        self._signups: dict[str, list[float]] = {}
        #: Session hash -> monotonic time of its last deliberate touch, and of
        #: the last time that touch was also written to the database.
        self._touched: dict[str, float] = {}
        self._written: dict[str, float] = {}
        self._dummy: str | None = None

    # --- sessions -----------------------------------------------------------

    def idle_limit(self, username: str) -> float:
        """How long `username` may go untouched: longer while their run goes."""
        try:
            running = self.runner() if self.runner else None
        except Exception:
            running = None
        return self.run_idle_s if running and running == username else self.idle_s

    def resolve(self, token: str | None, address: str = "") -> Principal | None:
        """The principal behind a session token, or None if it is not valid."""
        if not self.enabled:
            return LOCAL
        if not token:
            return None
        hashed = token_hash(token)
        row = self.db.session(hashed)
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] is not None and row["expires_at"] < now:
            self.db.delete_session(hashed)
            return None
        user = self.db.user(row["user_id"])
        if not user or user["status"] != "active":
            return None
        if user["kind"] == "device":
            role = "viewer"                 # a tablet only ever watches
        else:
            role = self.db.role(user["id"], self.chamber)
            if role is None:
                return None
        if now - row["seen_at"] > 60:
            self.db.touch_session(hashed, seen_at=now, address=address or None)
        # Since the last touch: from memory when this process saw it, which
        # no clock correction can stretch; from the database only after a
        # restart, and never negative.
        touched = self._touched.get(hashed)
        inactive = (time.monotonic() - touched if touched is not None
                    else max(0.0, now - row["active_at"]))
        return Principal(user["id"], user["username"],
                         user["display_name"] or user["username"], user["kind"],
                         role, hashed, inactive,
                         self.idle_limit(user["username"]),
                         bool(user["must_change"]))

    def _touch(self, session: str, write: bool = False) -> None:
        """Record a deliberate touch: in memory always, in the database at
        most every ACTIVE_WRITE_S (or at once, with `write`)."""
        mono = time.monotonic()
        self._touched[session] = mono
        if write or mono - self._written.get(session, 0.0) > ACTIVE_WRITE_S:
            self._written[session] = mono
            self.db.touch_session(session, active_at=time.time())

    def _forget(self, session: str | None) -> None:
        self._touched.pop(session, None)
        self._written.pop(session, None)

    def mark_active(self, principal: Principal) -> bool:
        """Count a deliberate action as a touch, unless the session is idle.

        An idle session stays idle until the password is typed again: a
        stranger tapping a tablet somebody walked away from must not wake
        that person's rights back up.
        """
        if principal.kind != "person" or principal.idle():
            return False
        self._touch(principal.session)
        principal.inactive_s = 0.0
        self._refresh_holder(principal)
        return True

    def login(self, username: str, password: str, address: str = "",
              agent: str = "") -> dict:
        if not self.enabled:
            return {"ok": True, "token": None, "me": self.me(LOCAL)}
        name = str(username or "").strip().lower()
        keys = (f"user:{name}", f"addr:{address}")
        wait = self.throttle.wait(*keys)
        if wait > 0:
            seconds = math.ceil(wait)
            return refusal(f"Too many failed sign-ins. Try again in {seconds} s.",
                           429, retry_after=seconds)
        user = self.db.user_by_name(name) if name else None
        if user is None or user["kind"] != "person" or not user["password_hash"]:
            verify_password(str(password), self._dummy_hash())   # same cost
            self.throttle.fail(*keys)
            return refusal("Wrong username or password.", 401)
        if not verify_password(str(password), user["password_hash"]):
            self.throttle.fail(*keys)
            sys.stderr.write(f"  failed sign-in for {name!r} from {address}\n")
            return refusal("Wrong username or password.", 401)
        self.throttle.clear(keys[0])
        if user["status"] == "pending":
            return refusal("This account is waiting for an admin to approve it.",
                           403)
        if user["status"] != "active":
            return refusal("This account is disabled. Ask an admin.", 403)
        if self.db.role(user["id"], self.chamber) is None:
            return refusal("This account has no role in this chamber. Ask an "
                           "admin.", 403)
        token = new_token()
        hashed = token_hash(token)
        self.db.create_session(hashed, user["id"], time.time() + SESSION_S,
                               address, agent)
        self.db.update_user(user["id"], last_login_at=time.time())
        # Signing in is a touch; the new row already carries the time.
        self._touched[hashed] = self._written[hashed] = time.monotonic()
        return {"ok": True, "token": token, "me": self.me(self.resolve(token))}

    def logout(self, principal: Principal) -> dict:
        if principal.session:
            self.release(principal, quiet=True)
            self.db.delete_session(principal.session)
            self._forget(principal.session)
        return {"ok": True}

    def reauth(self, principal: Principal, password: str) -> dict:
        """Type the password again to act after an idle spell."""
        if principal.kind != "person":
            return refusal("Only a person signs in with a password.", 403)
        user = self.db.user(principal.user_id)
        key = f"user:{principal.username}"
        wait = self.throttle.wait(key)
        if wait > 0:
            return refusal(f"Too many failed attempts. Try again in "
                           f"{math.ceil(wait)} s.", 429)
        if not user or not verify_password(str(password), user["password_hash"]):
            self.throttle.fail(key)
            return refusal("Wrong password.", 401)
        self.throttle.clear(key)
        self._touch(principal.session, write=True)
        principal.inactive_s = 0.0
        self._refresh_holder(principal)
        return {"ok": True, "me": self.me(principal)}

    def change_password(self, principal: Principal, current: str,
                        new: str) -> dict:
        if principal.kind != "person":
            return refusal("Only a person has a password.", 403)
        user = self.db.user(principal.user_id)
        key = f"user:{principal.username}"
        if self.throttle.wait(key) > 0:
            return refusal("Too many failed attempts. Try again later.", 429)
        if not user or not verify_password(str(current), user["password_hash"]):
            self.throttle.fail(key)
            return refusal("The current password is wrong.", 401)
        problem = check_password(new, user["username"])
        if problem:
            return refusal(problem, 400)
        if verify_password(new, user["password_hash"]):
            return refusal("Choose a password different from the current one.",
                           400)
        self.db.update_user(user["id"], password_hash=hash_password(new),
                            must_change=0)
        # Every other session of this account ends: a password is changed
        # because somebody else might know it.
        self.db.delete_sessions(user["id"], keep=principal.session)
        principal.must_change = False
        return {"ok": True, "me": self.me(principal)}

    def signup(self, username: str, display_name: str, password: str,
               note: str = "", address: str = "") -> dict:
        """Ask for an account. It does nothing until an admin approves it."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._signups.get(address, []) if now - t < 3600]
            if len(recent) >= SIGNUPS_PER_HOUR:
                return refusal("Too many requests from this address. Try again "
                               "later.", 429)
            recent.append(now)
            self._signups[address] = recent
        name = str(username or "").strip().lower()
        problem = check_username(name) or check_password(password, name)
        if problem:
            return refusal(problem, 400)
        display = " ".join(str(display_name or "").split())[:60] or name
        pending = [u for u in self.db.users(self.chamber)
                   if u["status"] == "pending"]
        if len(pending) >= MAX_PENDING:
            return refusal("Too many requests are already waiting. Ask an "
                           "admin.", 429)
        try:
            self.db.create_user(name, hash_password(password),
                                display_name=display, status="pending",
                                note=str(note or "")[:300])
        except sqlite3.IntegrityError:
            return refusal("That username is taken.", 409)
        self.db.add_event(None, "action",
                          f"Account requested: {display} ({name}). An admin "
                          "approves it in Settings -> People.", user=name)
        return {"ok": True, "pending": True}

    def me(self, principal: Principal) -> dict:
        """What the panel needs to know about whoever is looking at it."""
        person = principal.kind == "person"
        described = {
            "username": principal.username, "name": principal.name,
            "kind": principal.kind, "role": principal.role,
            "rank": principal.rank, "must_change": principal.must_change,
            "idle": principal.idle(),
            # None, never infinity, for a tablet or with accounts off: JSON
            # has no infinity and the panel would fail to parse the reply.
            "idle_after_s": round(principal.idle_limit) if person else None,
            "idle_in_s": (max(0, round(principal.idle_limit - principal.inactive_s))
                          if person else None),
            "can": {
                "operate": principal.may_operate(),
                "admin": principal.at_least("admin")
                and principal.kind in ("person", "local"),
                "override": principal.at_least("admin")
                and principal.kind in ("person", "local"),
            },
            "accounts": self.enabled,
        }
        if self.enabled and principal.at_least("admin") \
                and principal.kind == "person":
            described["waiting"] = (
                sum(1 for u in self.db.users(self.chamber)
                    if u["status"] == "pending")
                + len(self.db.role_requests(self.chamber)))
        return described

    # --- control ------------------------------------------------------------

    def _holder(self, now: float | None = None) -> dict | None:
        """The current controller, or None once the lease has lapsed.

        Monotonic throughout: the lease lapses LEASE_S after the holder's
        polling stops, or when their session goes idle, whatever the wall
        clock does meanwhile.
        """
        now = now or time.monotonic()
        holder = self._control
        if holder is None:
            return None
        if now - holder["renewed"] > LEASE_S or now > holder["idle_at"]:
            self._control = None
            return None
        return holder

    @staticmethod
    def _idle_at(principal: Principal, now: float) -> float:
        """When this session will go idle, on the monotonic clock."""
        return now + max(0.0, principal.idle_limit - principal.inactive_s)

    def _refresh_holder(self, principal: Principal) -> None:
        with self._lock:
            holder = self._holder()
            if holder and holder["session"] == principal.session:
                now = time.monotonic()
                holder["renewed"] = now
                holder["idle_at"] = self._idle_at(principal, now)

    def renew(self, principal: Principal) -> None:
        """Called on every request: the holder's own polling keeps control."""
        if self.enabled and principal.session:
            self._refresh_holder(principal)

    def control(self, principal: Principal | None = None) -> dict:
        if not self.enabled:
            return {"enabled": False, "held": False, "mine": True}
        with self._lock:
            holder = self._holder()
            if holder is None:
                return {"enabled": True, "held": False, "mine": False,
                        "lease_s": LEASE_S}
            mine = bool(principal and principal.session
                        and holder["session"] == principal.session)
            return {"enabled": True, "held": True, "holder": holder["name"],
                    "username": holder["username"], "since": holder["since"],
                    "mine": mine,
                    # The same account, on another screen: "Take control
                    # here" moves it without needing an admin.
                    "same_user": bool(principal and not mine
                                      and holder["user_id"] == principal.user_id),
                    "lease_s": LEASE_S}

    def take(self, principal: Principal, force: bool = False,
             explicit: bool = True) -> dict:
        """Take control of the rig: free, lapsed, already ours, or by force.

        `explicit` is a person pressing Take control. An action that only
        *needs* control (ensure_control) takes it when it is free, never
        from another screen -- not even one signed in to the same account.
        Otherwise two tablets sharing a login would hand control back and
        forth on every press, which is two controllers in all but name.
        """
        if not self.enabled:
            return {"ok": True, "control": self.control(principal)}
        if principal.kind != "person":
            return refusal("This tablet only watches. Sign in to take control.",
                           403)
        if not principal.at_least("operator"):
            return refusal(f"Taking control needs the operator role; you are "
                           f"{principal.role}.", 403)
        if principal.idle():
            return refusal("Your session has been idle. Enter your password to "
                           "act again.", 401, reauth=True)
        now = time.monotonic()
        with self._lock:
            holder = self._holder(now)
            previous = None
            if (holder and holder["session"] != principal.session
                    and holder["user_id"] == principal.user_id and not explicit):
                return refusal("This account has control on another screen. "
                               "Press Take control here first.", 409,
                               holder=holder["name"], same_user=True)
            if holder and holder["user_id"] != principal.user_id:
                if not (force and principal.at_least("admin")):
                    return refusal(
                        f"{holder['name']} has control of the rig. They can "
                        "release it, or it frees itself 15 s after their panel "
                        "closes" + (" -- or take it over." if
                                    principal.at_least("admin") else "."),
                        409, holder=holder["name"])
                previous = holder["name"]
            changed = holder is None or holder["user_id"] != principal.user_id
            self._control = {
                "session": principal.session, "user_id": principal.user_id,
                "username": principal.username, "name": principal.name,
                # Wall time, only to show "since 11:12 PM".
                "since": time.time() if changed else holder["since"],
                "renewed": now, "idle_at": self._idle_at(principal, now),
            }
        if previous:
            self._log(principal, "warning",
                      f"{principal.name} took control from {previous}")
        elif changed:
            self._log(principal, "action", f"{principal.name} took control")
        return {"ok": True, "control": self.control(principal)}

    def ensure_control(self, principal: Principal) -> dict | None:
        """None if this principal may move hardware now; else the refusal."""
        if not self.enabled:
            return None
        result = self.take(principal, explicit=False)
        return None if result.get("ok") else result

    def release(self, principal: Principal, quiet: bool = False) -> dict:
        with self._lock:
            holder = self._holder()
            mine = holder is not None and holder["session"] == principal.session
            if mine:
                self._control = None
        if mine and not quiet:
            self._log(principal, "action", f"{principal.name} released control")
        return {"ok": True, "released": mine,
                "control": self.control(principal)}

    def _drop_control_of(self, user_id: int) -> None:
        with self._lock:
            holder = self._holder()
            if holder and holder["user_id"] == user_id:
                self._control = None

    # --- tablets ------------------------------------------------------------

    def create_device(self, actor: Principal, label: str) -> dict:
        """An account for one tablet, and the code that pairs it."""
        if not self._admin(actor):
            return refusal("Pairing a tablet needs the admin role.", 403)
        label = " ".join(str(label or "").split())[:40] or "Tablet"
        taken = {u["username"] for u in self.db.users(self.chamber)}
        number = 1
        while f"tablet-{number}" in taken:
            number += 1
        user = self.db.create_user(f"tablet-{number}", "", display_name=label,
                                   kind="device", status="active")
        self.db.set_role(user["id"], self.chamber, "viewer", actor.username)
        code = self._issue_code(user["id"])
        self._log(actor, "action", f"{actor.name} added the tablet {label!r}; "
                                   "waiting to be paired")
        return {"ok": True, "code": code, "expires_in_s": PAIRING_S,
                "device": self._public(self.db.user(user["id"]))}

    def renew_code(self, actor: Principal, user_id: int) -> dict:
        if not self._admin(actor):
            return refusal("Pairing a tablet needs the admin role.", 403)
        user = self.db.user(user_id)
        if not user or user["kind"] != "device":
            return refusal("No such tablet.", 404)
        return {"ok": True, "code": self._issue_code(user_id),
                "expires_in_s": PAIRING_S}

    def _issue_code(self, user_id: int) -> str:
        code = new_code()
        now = time.monotonic()
        with self._lock:
            self._pairing = {k: v for k, v in self._pairing.items()
                             if v[1] > now and v[0] != user_id}
            self._pairing[token_hash(normalise_code(code))] = \
                (user_id, now + PAIRING_S)
        return code

    def pair(self, code: str, address: str = "", agent: str = "") -> dict:
        """Turn a pairing code typed on a tablet into its watching session."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        key = f"pair:{address}"
        wait = self.throttle.wait(key)
        if wait > 0:
            return refusal(f"Too many wrong codes. Try again in "
                           f"{math.ceil(wait)} s.", 429)
        hashed = token_hash(normalise_code(code))
        with self._lock:
            entry = self._pairing.pop(hashed, None)
        valid = entry is not None and entry[1] > time.monotonic()
        user = self.db.user(entry[0]) if valid else None
        if not user or user["kind"] != "device" or user["status"] != "active":
            self.throttle.fail(key)
            return refusal("That code is wrong or has expired. Ask an admin "
                           "for a new one.", 401)
        token = new_token()
        self.db.create_session(token_hash(token), user["id"], None, address,
                               agent)
        self.db.add_event(None, "action", f"Tablet {user['display_name']!r} "
                                          f"paired from {address or 'unknown'}",
                          user=user["username"])
        return {"ok": True, "token": token, "me": self.me(self.resolve(token))}

    # --- administration -----------------------------------------------------

    def _admin(self, actor: Principal) -> bool:
        return (actor.kind in ("person", "local") and actor.at_least("admin")
                and not actor.must_change)

    def _public(self, user: dict | None) -> dict | None:
        if not user:
            return None
        hidden = {"password_hash", "uuid"}
        return {k: v for k, v in user.items() if k not in hidden}

    def users_report(self, actor: Principal) -> dict:
        if not self._admin(actor):
            return refusal("Managing accounts needs the admin role.", 403)
        now = time.monotonic()
        with self._lock:
            # Shown as a wall-clock time; kept on the monotonic one.
            pairing = {v[0]: time.time() + (v[1] - now)
                       for v in self._pairing.values() if v[1] > now}
        people, tablets = [], []
        for user in self.db.users(self.chamber):
            entry = self._public(user)
            sessions = self.db.sessions(user["id"])
            entry["last_seen_at"] = max((s["seen_at"] for s in sessions),
                                        default=None)
            if user["kind"] == "device":
                entry["paired"] = bool(sessions)
                entry["address"] = sessions[0]["address"] if sessions else ""
                entry["code_expires_at"] = pairing.get(user["id"])
                tablets.append(entry)
            else:
                entry["you"] = user["username"] == actor.username
                people.append(entry)
        return {"ok": True, "people": people, "tablets": tablets,
                "requests": self.db.role_requests(self.chamber),
                "you": {"username": actor.username, "role": actor.role}}

    def _target(self, actor: Principal, user_id: int,
                person: bool = True) -> tuple[dict | None, dict | None]:
        """The account an admin action is about, or the reason it is refused."""
        if not self._admin(actor):
            return None, refusal("Managing accounts needs the admin role.", 403)
        user = self.db.user(int(user_id))
        if not user or (person and user["kind"] != "person"):
            return None, refusal("No such account.", 404)
        if user["id"] == actor.user_id:
            return None, refusal("You cannot change your own account here.", 403)
        role = self.db.role(user["id"], self.chamber)
        if role == "owner":
            return None, refusal("The owner's account is changed by the owner, "
                                 "or on the machine with tools/users.py.", 403)
        if role == "admin" and not actor.at_least("owner"):
            return None, refusal("Only the owner changes an admin's account.",
                                 403)
        return {**user, "role": role}, None

    def _grantable(self, actor: Principal, role: str) -> dict | None:
        if role not in ("viewer", "operator", "admin"):
            return refusal("The role is viewer, operator or admin.", 400)
        if role == "admin" and not actor.at_least("owner"):
            return refusal("Only the owner appoints admins.", 403)
        return None

    def approve(self, actor: Principal, user_id: int, role: str) -> dict:
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        if user["status"] != "pending":
            return refusal("That account is not waiting for approval.", 409)
        problem = self._grantable(actor, role)
        if problem:
            return problem
        self.db.update_user(user["id"], status="active")
        self.db.set_role(user["id"], self.chamber, role, actor.username)
        self._log(actor, "action", f"{actor.name} approved {user['display_name']} "
                                   f"({user['username']}) as {role}")
        return {"ok": True}

    def decline(self, actor: Principal, user_id: int) -> dict:
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        if user["status"] != "pending":
            return refusal("Only a request still waiting can be declined; "
                           "disable an account instead.", 409)
        self.db.delete_user(user["id"])
        self._log(actor, "action", f"{actor.name} declined the account request "
                                   f"of {user['display_name']} "
                                   f"({user['username']})")
        return {"ok": True}

    def set_role(self, actor: Principal, user_id: int, role: str) -> dict:
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        if user["status"] != "active":
            return refusal("Approve or enable the account first.", 409)
        problem = self._grantable(actor, role)
        if problem:
            return problem
        self.db.set_role(user["id"], self.chamber, role, actor.username)
        if RANK[role] < RANK["operator"]:
            self._drop_control_of(user["id"])
        self._log(actor, "action", f"{actor.name} made {user['display_name']} "
                                   f"({user['username']}) {role}")
        return {"ok": True}

    def set_status(self, actor: Principal, user_id: int, enabled: bool) -> dict:
        user, problem = self._target(actor, user_id, person=False)
        if problem:
            return problem
        if user["status"] == "pending":
            return refusal("Approve or decline the request instead.", 409)
        if enabled:
            self.db.update_user(user["id"], status="active")
        else:
            self.db.update_user(user["id"], status="disabled")
            self.db.delete_sessions(user["id"])
            self._drop_control_of(user["id"])
        self._log(actor, "action",
                  f"{actor.name} {'enabled' if enabled else 'disabled'} the "
                  f"account of {user['display_name']} ({user['username']})")
        return {"ok": True}

    def remove_device(self, actor: Principal, user_id: int) -> dict:
        user, problem = self._target(actor, user_id, person=False)
        if problem:
            return problem
        if user["kind"] != "device":
            return refusal("Disable a person's account instead; their name is "
                           "on the rig's record.", 409)
        self.db.delete_user(user["id"])
        self._log(actor, "action", f"{actor.name} removed the tablet "
                                   f"{user['display_name']!r}")
        return {"ok": True}

    def reset_password(self, actor: Principal, user_id: int) -> dict:
        """A temporary password, shown once, that must be changed on first use."""
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        temporary = new_code(groups=3, size=4).lower()
        self.db.update_user(user["id"], password_hash=hash_password(temporary),
                            must_change=1)
        self.db.delete_sessions(user["id"])
        self._drop_control_of(user["id"])
        self._log(actor, "action", f"{actor.name} reset the password of "
                                   f"{user['display_name']} ({user['username']})")
        return {"ok": True, "temporary": temporary}

    def transfer_owner(self, actor: Principal, user_id: int,
                       password: str) -> dict:
        if actor.kind != "person" or not actor.at_least("owner"):
            return refusal("Only the owner hands the chamber over.", 403)
        me = self.db.user(actor.user_id)
        if not me or not verify_password(str(password), me["password_hash"]):
            return refusal("Wrong password.", 401)
        user = self.db.user(int(user_id))
        if not user or user["kind"] != "person" or user["status"] != "active" \
                or user["id"] == actor.user_id:
            return refusal("Choose another active person.", 400)
        self.db.set_role(user["id"], self.chamber, "owner", actor.username)
        self.db.set_role(actor.user_id, self.chamber, "admin", actor.username)
        self._log(actor, "warning", f"{actor.name} handed ownership of the "
                                    f"chamber to {user['display_name']} "
                                    f"({user['username']})")
        return {"ok": True}

    def request_role(self, actor: Principal, role: str, note: str = "") -> dict:
        if actor.kind != "person":
            return refusal("Only a person asks for a role.", 403)
        if role not in ("operator", "admin") or RANK[role] <= actor.rank:
            return refusal("Ask for a role above the one you have.", 400)
        self.db.create_role_request(actor.user_id, self.chamber, role,
                                    str(note or "")[:300])
        self._log(actor, "action", f"{actor.name} asked to become {role}")
        return {"ok": True}

    def decide(self, actor: Principal, request_id: int, approve: bool) -> dict:
        request = self.db.role_request(int(request_id))
        if not request or request["chamber"] != self.chamber \
                or request["status"] != "open":
            return refusal("No such open request.", 404)
        user, problem = self._target(actor, request["user_id"])
        if problem:
            return problem
        if approve:
            problem = self._grantable(actor, request["role"])
            if problem:
                return problem
            self.db.set_role(user["id"], self.chamber, request["role"],
                             actor.username)
        self.db.decide_role_request(request["id"],
                                    "approved" if approve else "declined",
                                    actor.username)
        self._log(actor, "action",
                  f"{actor.name} {'approved' if approve else 'declined'} "
                  f"{user['display_name']}'s request to become "
                  f"{request['role']}")
        return {"ok": True}

    # --- helpers ------------------------------------------------------------

    def _log(self, actor: Principal, level: str, message: str) -> None:
        try:
            self.db.add_event(None, level, message, user=actor.username)
        except Exception:
            pass

    def _dummy_hash(self) -> str:
        if self._dummy is None:
            self._dummy = hash_password(secrets.token_urlsafe(12))
        return self._dummy

    def status(self) -> dict:
        """For /api/ping: enough for a sign-in page, nothing about anyone."""
        if not self.enabled:
            return {"enabled": False}
        return {"enabled": True,
                "owner": bool(self.db.holders(self.chamber, "owner"))}


def bootstrap_owner(database, chamber: str, username: str, password: str,
                    display_name: str = "", replace: bool = False) -> str:
    """Create the chamber's owner, from the machine itself. Returns what it did.

    Run with no owner: creates the account (or promotes an existing one).
    Run again for the same name: sets a new password -- the way back in for
    an owner who forgot it. Run for a different name: refused unless
    `replace`, in which case the previous owner becomes an admin.
    """
    name = username.strip().lower()
    problem = check_username(name) or check_password(password, name)
    if problem:
        raise ValueError(problem)
    owners = database.holders(chamber, "owner")
    current = owners[0] if owners else None
    if current and current["username"] != name and not replace:
        raise ValueError(f"{current['username']} already owns {chamber}; pass "
                         "--replace to hand it over")
    user = database.user_by_name(name)
    if user is None:
        user = database.create_user(name, hash_password(password),
                                    display_name=display_name or name,
                                    status="active")
        action = "created"
    else:
        if user["kind"] != "person":
            raise ValueError(f"{name} is a tablet, not a person")
        database.update_user(user["id"], password_hash=hash_password(password),
                             status="active", must_change=0,
                             **({"display_name": display_name}
                                if display_name else {}))
        database.delete_sessions(user["id"])
        action = "updated"
    if current and current["id"] != user["id"]:
        database.set_role(current["id"], chamber, "admin", "tools/users.py")
    database.set_role(user["id"], chamber, "owner", "tools/users.py")
    database.add_event(None, "action", f"Owner of {chamber} {action} on the "
                                       f"machine: {name}", user=name)
    return action
