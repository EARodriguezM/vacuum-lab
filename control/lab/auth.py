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

**Guests.** Somebody without an account -- a class watching a deposition --
may watch, once an admin has allowed it (Settings -> People; off until then).
A guest is a viewer that can never act, take control, sound alarms or
download, and is kept in memory only: nothing a stranger does is written to
the database (the request log in the journal still names its address), and a
restart ends every guest (the panel joins again by itself).
At most GUESTS_MAX at once, each dropped GUEST_IDLE_S after its last poll.

Passwords are scrypt hashes from hashlib, so there is no new dependency.
Sessions are random tokens of which only a SHA-256 is stored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass

from lab import logs

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
#: How many passwords are hashed at once, whoever sent them. Every sign-in
#: costs 16 MB before any password is known to be right; without a bound, a
#: burst of sign-ins from the network takes the memory the rig runs in.
SCRYPT_AT_ONCE = 2
#: How long a sign-in waits for its turn before being told to try again.
SCRYPT_WAIT_S = 5.0
_scrypt_slots = threading.BoundedSemaphore(SCRYPT_AT_ONCE)

#: A person signed in on a paired tablet stays signed in at most this long,
#: and is signed out -- the session deleted -- once it goes idle.
TABLET_SESSION_S = 8 * 3600
#: Password checks (each one scrypt) one address, or one account, may ask
#: for per minute -- right or wrong. Failures are throttled on their own;
#: this keeps one client, even with the right password, from holding the
#: hashing slots and turning everybody else away as busy.
HASHES_PER_MINUTE = 10
#: How long ago a last touch dated in the future counts as: long enough to
#: be idle under any limit, finite so that it still goes into JSON.
FUTURE_TOUCH_S = float(SESSION_S)
#: How often the clock's synchronisation is asked again.
CLOCK_CHECK_S = 30.0
#: The wall clock's high-water mark is written back at most this often.
HIGH_WATER_WRITE_S = 30.0

MIN_PASSWORD = 8
MAX_PASSWORD = 128
USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")
#: No I, O, 0 or 1: a code read off one screen and typed into another.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

#: Sign-ups an address may make per hour, and pending ones the rig will hold.
SIGNUPS_PER_HOUR = 5
MAX_PENDING = 50

#: Accounts are the people of one institution: a username is the part of
#: their institutional address before the @ (webserver.py --email-domain).
EMAIL_DOMAIN = "unal.edu.co"
#: A request proves whose it is by a Google sign-in (lab/google.py): the
#: device flow's code lives as long as Google says, at most this long. The
#: request is made the moment Google says whose account it was.
GOOGLE_FLOW_S = 30 * 60
#: A finished sign-in keeps its answer this long, for a poll that asks again
#: (its reply lost on the way, the panel busy): "done", not "start again".
GOOGLE_DONE_S = 120.0
#: Sign-ins being waited for at once, and started per hour from one address
#: and from everybody: each one is a request to Google.
GOOGLE_FLOWS_MAX = 50
GOOGLE_STARTS_PER_HOUR = 10
GOOGLE_STARTS_PER_HOUR_ALL = 60
#: Google asks to be polled no faster than this (and slower when it says so).
GOOGLE_POLL_MIN_S = 5.0
#: Watching without an account (see the top of this file): at most this many
#: at once -- each polls every two seconds, on a machine that also watches the
#: rig -- each dropped this long after its last poll (a hidden page stops
#: polling; it joins again when shown) and after this long in any case; and
#: this many guest sessions an hour from one address.
GUESTS_MAX = 20
GUEST_IDLE_S = 120.0
GUEST_SESSION_S = 12 * 3600.0
GUEST_STARTS_PER_HOUR = 30
#: Guests from one address at once, so one machine cannot take every place
#: for half a day (loopback -- the lab PC's own screen -- is not held to it);
#: and the requests one guest may make a minute: a page polls a few routes
#: every two seconds -- a few tabs share one guest -- and a script on sixteen
#: connections is held to the same.
GUESTS_PER_ADDRESS = 10
GUEST_REQUESTS_PER_MINUTE = 600


# --- passwords and tokens --------------------------------------------------

def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


class Busy(RuntimeError):
    """Too many passwords are being checked at once; the caller answers 503."""


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int,
            dklen: int) -> bytes:
    """hashlib.scrypt, at most SCRYPT_AT_ONCE at a time; Busy if no turn
    comes within SCRYPT_WAIT_S."""
    if not _scrypt_slots.acquire(timeout=SCRYPT_WAIT_S):
        raise Busy("The server is busy checking other passwords. Try again "
                   "in a moment.")
    try:
        return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r,
                              p=p, dklen=dklen)
    finally:
        _scrypt_slots.release()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, 32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = _scrypt(password, base64.b64decode(salt), int(n), int(r),
                         int(p), len(expected))
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
    # fullmatch, not match: `$` also matches before a final newline.
    if not USERNAME.fullmatch(username or ""):
        return ("A username is 3 to 32 characters: lower-case letters, digits, "
                "dots, dashes or underscores, starting with a letter or digit.")
    return None


def google_identity(claims: dict, client_id, domain: str) -> tuple[dict | None, str | None]:
    """The username and name a Google ID token vouches for, or why it does
    not: meant for this client, unexpired, an address Google verified, and
    an account of the institution's own organization (`hd`) -- an address at
    the domain is not enough alone, a personal Google account can carry any
    address it has verified."""
    from lab import google as googlemod
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    try:
        expires = float(claims.get("exp", 0))
    except (TypeError, ValueError):
        expires = 0.0
    if (claims.get("iss") not in googlemod.ISSUERS or not client_id
            or client_id not in audiences or expires < googlemod.now() - 60):
        return None, "Google's answer was not meant for this panel."
    if claims.get("email_verified") not in (True, "true"):
        return None, "Google has not verified that account's address."
    email = str(claims.get("email") or "").strip().lower()
    if claims.get("hd") != domain or not email.endswith(f"@{domain}"):
        return None, (f"Sign in with your institutional account (@{domain}), "
                      f"not {email[:80] or 'that one'}.")
    username = email[:-len(domain) - 1]
    if check_username(username):
        return None, f"{email[:80]} cannot be a username here; ask an admin."
    name = " ".join(str(claims.get("name") or "").split())[:60] or username
    return {"username": username, "name": name, "email": email}, None


def check_password(password: str, username: str = "") -> str | None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"A password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"A password may have at most {MAX_PASSWORD} characters."
    if username and password.strip().lower() == username.lower():
        return "The password cannot be the username."
    return None


def clock_synchronised() -> bool:
    """Whether the kernel says network time is keeping the wall clock right.

    adjtimex(2) with nothing to change only reads the clock's state, which
    is TIME_ERROR (5) while no time service has disciplined it -- whichever
    one runs, timesyncd or chrony. Anything else that goes wrong (not Linux,
    no libc) counts as not synchronised: the cautious answer.
    """
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        # Larger than struct timex on any architecture, and zeroed, so its
        # `modes` is 0: nothing is set, the state is only read.
        buffer = ctypes.create_string_buffer(1024)
        state = libc.adjtimex(buffer)
    except Exception:
        return False
    return state in (0, 1, 2, 3, 4)        # TIME_OK .. TIME_WAIT


def _loopback(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback


class Budget:
    """At most `count` uses per key in any `window` seconds."""

    def __init__(self, count: int, window: float = 60.0):
        self.count = count
        self.window = window
        self._lock = threading.Lock()
        self._used: dict[str, list[float]] = {}

    def wait(self, *keys: str) -> float:
        """The seconds until every key has a use left; 0 now. Counts nothing."""
        now = time.monotonic()
        with self._lock:
            longest = 0.0
            for key in keys:
                times = [t for t in self._used.get(key, [])
                         if now - t < self.window]
                if len(times) >= self.count:
                    longest = max(longest, self.window - (now - times[0]))
            return max(0.1, longest) if longest > 0 else 0.0

    def give_back(self, *keys: str) -> None:
        """Return the use `take` last counted under each key."""
        with self._lock:
            for key in keys:
                times = self._used.get(key)
                if times:
                    times.pop()

    def take(self, *keys: str) -> float:
        """0, with one use counted under every key; or the seconds to wait,
        with nothing counted."""
        now = time.monotonic()
        with self._lock:
            recent = {key: [t for t in self._used.get(key, [])
                            if now - t < self.window] for key in keys}
            full = [times for times in recent.values()
                    if len(times) >= self.count]
            if full:
                return max(0.1, max(self.window - (now - times[0])
                                    for times in full))
            for key, times in recent.items():
                times.append(now)
                self._used[key] = times
            if len(self._used) > 5000:          # bounded under a flood
                self._used = {k: v for k, v in self._used.items()
                              if v and now - v[-1] < self.window}
            return 0.0


# --- who is asking ---------------------------------------------------------

@dataclass
class Principal:
    """The account behind one request, with its role in this chamber."""

    user_id: int
    username: str
    name: str
    kind: str                   # person | device | guest | local
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
    #: Attempts under one key that may be checked at the same time. A failure
    #: is only counted once its password has been checked, so without this
    #: a burst sent in parallel would all pass `wait` before any counted.
    AT_ONCE = 2
    #: What a caller over AT_ONCE is told to wait.
    BUSY_S = 2.0

    def __init__(self):
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, float]] = {}
        self._flight: dict[str, int] = {}

    def _owed(self, keys, now: float) -> float:
        """The longest wait any of `keys` still owes. Caller holds the lock."""
        longest = 0.0
        for key in keys:
            count, last = self._failures.get(key, (0, 0.0))
            if count < self.FREE:
                continue
            delay = min(self.BASE_S * 2 ** (count - self.FREE), self.MAX_S)
            longest = max(longest, last + delay - now)
        return max(0.0, longest)

    def wait(self, *keys: str) -> float:
        with self._lock:
            return self._owed(keys, time.monotonic())

    def begin(self, *keys: str) -> float:
        """Start one attempt: 0 with it counted as in flight under every key,
        or the seconds to wait first, with nothing started. Every 0 must be
        followed by `end`."""
        with self._lock:
            owed = self._owed(keys, time.monotonic())
            if owed > 0:
                return owed
            if any(self._flight.get(key, 0) >= self.AT_ONCE for key in keys):
                return self.BUSY_S
            for key in keys:
                self._flight[key] = self._flight.get(key, 0) + 1
            return 0.0

    def end(self, *keys: str, failed: bool) -> None:
        """Finish an attempt `begin` started, counting it if it failed --
        both under one lock, so no attempt slips in between the slot being
        freed and the failure being counted."""
        with self._lock:
            for key in keys:
                left = self._flight.get(key, 0) - 1
                if left > 0:
                    self._flight[key] = left
                else:
                    self._flight.pop(key, None)
            if failed:
                self._fail(keys)

    def fail(self, *keys: str) -> None:
        with self._lock:
            self._fail(keys)

    def _fail(self, keys) -> None:
        """Count one failure under each key. Caller holds the lock."""
        now = time.monotonic()
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
                 runner=None, email_domain: str = EMAIL_DOMAIN, google=None):
        self.db = database
        self.chamber = chamber
        self.enabled = enabled
        #: Whose usernames these are, and who proves it (lab/google.py; None
        #: or not set up: nobody's account is checked).
        self.email_domain = str(email_domain or EMAIL_DOMAIN).strip().lower()
        self.google = google
        #: Google sign-ins being waited for, by the hash of the token the
        #: browser holds for each (only it can poll it or use it).
        self._flows: dict[str, dict] = {}
        self._flows_lock = threading.Lock()
        self._google_starts = Budget(GOOGLE_STARTS_PER_HOUR, 3600.0)
        self._google_starts_all = Budget(GOOGLE_STARTS_PER_HOUR_ALL, 3600.0)
        #: How long a person may go untouched and still act, and how long
        #: when the run that is going is theirs (webserver.py --idle-minutes,
        #: --run-idle-minutes).
        self.idle_s = idle_s
        self.run_idle_s = run_idle_s
        #: Returns the username that started the run in progress, or None.
        self.runner = runner
        self.throttle = Throttle()
        #: Password checks per address and per account, right or wrong.
        self.hashes = Budget(HASHES_PER_MINUTE)
        #: Whether network time keeps the wall clock right (replaceable, for
        #: the tests); asked at most every CLOCK_CHECK_S.
        self.clock_check = clock_synchronised
        self._clock_checked: tuple[float, bool] | None = None
        self._lock = threading.RLock()
        self._control: dict | None = None
        self._pairing: dict[str, tuple[int, float]] = {}
        self._signups: dict[str, list[float]] = {}
        #: Session hash -> monotonic time of its last deliberate touch, and of
        #: the last time that touch was also written to the database.
        self._touched: dict[str, float] = {}
        self._written: dict[str, float] = {}
        self._dummy: str | None = None
        #: The latest wall-clock time this rig has seen, kept across restarts
        #: (see _wall).
        try:
            self._high_water = float(database.meta("wall_high_water") or 0)
        except (TypeError, ValueError):
            self._high_water = 0.0
        self._high_water_written = time.monotonic() - HIGH_WATER_WRITE_S
        #: Guests watching, by the hash of their token; memory only. Whether
        #: an admin allows them is kept in the database and read once here.
        self._guests: dict[str, dict] = {}
        self._guest_starts = Budget(GUEST_STARTS_PER_HOUR, 3600.0)
        self._guest_requests = Budget(GUEST_REQUESTS_PER_MINUTE)
        try:
            self._guests_on = database.meta("guests") == "on"
        except Exception:
            self._guests_on = False

    # --- sessions -----------------------------------------------------------

    def idle_limit(self, username: str) -> float:
        """How long `username` may go untouched: longer while their run goes."""
        try:
            running = self.runner() if self.runner else None
        except Exception:
            running = None
        return self.run_idle_s if running and running == username else self.idle_s

    def _wall(self) -> float:
        """The wall clock, never earlier than the latest time already seen.

        A Raspberry Pi boots with whatever time it had when it lost power,
        until the network corrects it. Measured on that clock, a session
        touched just before a reboot would look fresh for as long as the
        clock is behind. Only the idle timer uses this: if the clock was ever
        set ahead, sessions need their password once more after a restart
        until real time catches up -- the safe direction -- while expiry keeps
        the plain clock, which a stuck mark could otherwise end for everyone.
        """
        now = time.time()
        if now > self._high_water:
            self._high_water = now
            mono = time.monotonic()
            if mono - self._high_water_written >= HIGH_WATER_WRITE_S:
                self._high_water_written = mono
                try:
                    self.db.set_meta("wall_high_water", repr(now))
                except Exception:
                    pass
        return self._high_water

    def _clock_trusted(self) -> bool:
        mono = time.monotonic()
        checked = self._clock_checked
        if checked is None or mono - checked[0] > CLOCK_CHECK_S:
            try:
                trusted = bool(self.clock_check())
            except Exception:
                trusted = False
            checked = self._clock_checked = (mono, trusted)
        return checked[1]

    def _write(self, what: str, write) -> None:
        """A session's bookkeeping write. A failure -- a full SD card -- is
        said on stderr and never raised: it must not cost a Stop or a Make
        safe its answer, and every request resolves its session first."""
        try:
            write()
        except Exception as exc:
            sys.stderr.write(f"  !! could not record {what} ({exc})\n")

    def _hash_budget(self, *keys: str) -> dict | None:
        """The refusal once these keys have asked for too many password
        checks this minute, or None (and the check counted). Loopback has
        no address budget: it is the kiosk tablet on its cable, where one
        person after another signs in, and the machine itself."""
        keys = tuple(k for k in keys if not (k.startswith("addr:")
                                             and _loopback(k[5:])))
        if not keys:
            return None
        wait = self.hashes.take(*keys)
        if wait <= 0:
            return None
        seconds = math.ceil(wait)
        return refusal(f"Too many password checks from here. Try again in "
                       f"{seconds} s.", 429, retry_after=seconds)

    def resolve(self, token: str | None, address: str = "") -> Principal | None:
        """The principal behind a session token, or None if it is not valid."""
        if not self.enabled:
            return LOCAL
        if not token:
            return None
        hashed = token_hash(token)
        guest = self._guest(hashed)
        if guest is not None:
            return guest
        row = self.db.session(hashed)
        if row is None:
            return None
        now = time.time()
        if row["expires_at"] is not None and row["expires_at"] < now:
            self._end_session(hashed)
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
            self._write("a session's last poll", lambda: self.db.touch_session(
                hashed, seen_at=now, address=address or None))
        # Since the last touch: from memory when this process saw it, which
        # no clock correction can stretch; from the database only after a
        # restart, and then only when network time vouches for the clock --
        # a box that boots with the time it had when the power went cannot
        # say how long it was off. Even then, against the high-water mark,
        # and a touch dated after it (a clock that was ahead) counts as long
        # ago.
        touched = self._touched.get(hashed)
        if touched is not None:
            inactive = time.monotonic() - touched
        elif not self._clock_trusted():
            inactive = FUTURE_TOUCH_S
        else:
            wall = self._wall()
            inactive = (wall - row["active_at"] if row["active_at"] <= wall
                        else FUTURE_TOUCH_S)
        principal = Principal(user["id"], user["username"],
                              user["display_name"] or user["username"],
                              user["kind"], role, hashed, inactive,
                              self.idle_limit(user["username"]),
                              bool(user["must_change"]))
        # On a paired tablet, a person who walked away is signed out: the
        # next one to pick the tablet up gets the tablet's own session, not
        # theirs. Timed here, not in the browser, which a reload resets.
        if row.get("device_id") is not None and principal.idle():
            self._end_session(hashed)
            return None
        return principal

    def _end_session(self, hashed: str) -> None:
        # Refused from now on whether or not the row goes: if it stays, the
        # next request finds it expired or idle again and tries once more.
        self._write("a session ending", lambda: self.db.delete_session(hashed))
        self._forget(hashed)
        with self._lock:
            holder = self._control
            if holder and holder["session"] == hashed:
                self._control = None

    def _touch(self, session: str, write: bool = False) -> None:
        """Record a deliberate touch: in memory always, in the database at
        most every ACTIVE_WRITE_S (or at once, with `write`)."""
        mono = time.monotonic()
        self._touched[session] = mono
        if write or mono - self._written.get(session, 0.0) > ACTIVE_WRITE_S:
            self._written[session] = mono
            self._write("a touch", lambda: self.db.touch_session(
                session, active_at=time.time()))
            self._wall()

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

    def _paired_device(self, token: str | None) -> int | None:
        """The tablet a paired browser's own session belongs to, or None."""
        if not token:
            return None
        row = self.db.session(token_hash(str(token)))
        user = self.db.user(row["user_id"]) if row else None
        if user and user["kind"] == "device" and user["status"] == "active":
            return user["id"]
        return None

    def login(self, username: str, password: str, address: str = "",
              agent: str = "", device_token: str | None = None) -> dict:
        """Sign a person in. `device_token` is the session of the paired
        tablet this is happening on, if any: the person's session is then
        tied to it, ends when idle, and lasts TABLET_SESSION_S at most."""
        if not self.enabled:
            return {"ok": True, "token": None, "me": self.me(LOCAL)}
        name = str(username or "").strip().lower()
        # The whole institutional address signs in too: it is what sign-up
        # accepts, and what a browser may have saved from it.
        if name.endswith(f"@{self.email_domain}"):
            name = name[:-len(self.email_domain) - 1].strip()
        # Per name *and* address: a wrong guess from elsewhere must not lock
        # the owner out of their own account. The address key still slows
        # one machine trying many names.
        keys = (f"login:{name}@{address}", f"addr:{address}")
        # Per address only: a budget kept per name would let anybody spend
        # the owner's from elsewhere -- the lock-out the keys above avoid.
        busy = self._hash_budget(f"addr:{address}")
        if busy:
            return busy
        wait = self.throttle.begin(*keys)
        if wait > 0:
            seconds = math.ceil(wait)
            return refusal(f"Too many failed sign-ins. Try again in {seconds} s.",
                           429, retry_after=seconds)
        wrong = False
        try:
            user = self.db.user_by_name(name) if name else None
            # Sign-ins that succeed are budgeted per account -- from loopback
            # too, which has no address budget. Only somebody who knows the
            # password can spend it, so it locks nobody else out.
            # Checked before hashing, charged only for a right password: a
            # stranger's guesses -- even ones waiting for a hashing slot --
            # must hold none of it, or a few addresses guessing would lock
            # the person out. Parallel sign-ins can overshoot it by the two
            # per name and address in flight; it matters on loopback only.
            account = f"user:{user['id']}" if user else None
            full = self.hashes.wait(account) if account else 0.0
            if full > 0:
                seconds = math.ceil(full)
                return refusal(f"Too many sign-ins to this account. Try again "
                               f"in {seconds} s.", 429, retry_after=seconds)
            if user is None or user["kind"] != "person" \
                    or not user["password_hash"]:
                verify_password(str(password), self._dummy_hash())  # same cost
                wrong = True
            elif not verify_password(str(password), user["password_hash"]):
                wrong = True
                sys.stderr.write(f"  failed sign-in for {name!r} from "
                                 f"{address}\n")
            else:
                self.hashes.take(account)
        finally:
            self.throttle.end(*keys, failed=wrong)
        if wrong:
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
        device = self._paired_device(device_token)
        self.db.create_session(
            hashed, user["id"],
            time.time() + (TABLET_SESSION_S if device else SESSION_S),
            address, agent, device_id=device)
        self.db.update_user(user["id"], last_login_at=time.time())
        # Signing in is a touch; the new row already carries the time.
        self._touched[hashed] = self._written[hashed] = time.monotonic()
        return {"ok": True, "token": token, "me": self.me(self.resolve(token))}

    def logout(self, principal: Principal) -> dict:
        if principal.kind == "guest":
            with self._lock:
                self._guests.pop(principal.session, None)
            return {"ok": True}
        if principal.session:
            self.release(principal, quiet=True)
            self.db.delete_session(principal.session)
            self._forget(principal.session)
        return {"ok": True}

    def _check_own_password(self, principal: Principal, password: str,
                            address: str) -> dict | None:
        """The refusal if `password` is not this person's, or None.

        Throttled per session -- guesses on one screen must not lock the
        person out of signing in, or out of waking a session elsewhere --
        and budgeted per address and per session, right or wrong. A check
        the server was too busy to make (Busy, raised on) is not a failure.
        """
        key = f"session:{principal.session}"
        wait = self.throttle.begin(key)
        if wait > 0:
            return refusal(f"Too many failed attempts. Try again in "
                           f"{math.ceil(wait)} s.", 429)
        wrong = False
        try:
            # Spent only on a check that will run, and per session: somebody
            # at an abandoned screen must not use up the account's checks on
            # the owner's other devices.
            busy = self._hash_budget(f"addr:{address}", key)
            if busy:
                return busy
            # And per account, charged only for a right password -- which
            # a stranger at the screen cannot spend -- so that sessions
            # opened one after another do not multiply what one account may
            # ask of the hashing slots (loopback has no address budget).
            # Reserved before hashing, so checks sent in parallel cannot all
            # pass first; given back for a wrong password or a check that
            # never ran.
            account = f"user:{principal.user_id}"
            full = self.hashes.take(account)
            if full > 0:
                seconds = math.ceil(full)
                return refusal(f"Too many password checks for this account. "
                               f"Try again in {seconds} s.", 429,
                               retry_after=seconds)
            right = False
            try:
                user = self.db.user(principal.user_id)
                right = bool(user) and verify_password(str(password),
                                                       user["password_hash"])
            finally:
                if not right:
                    self.hashes.give_back(account)
            wrong = not right
        finally:
            self.throttle.end(key, failed=wrong)
        if wrong:
            return refusal("Wrong password.", 401)
        self.throttle.clear(key)
        return None

    def reauth(self, principal: Principal, password: str,
               address: str = "") -> dict:
        """Type the password again to act after an idle spell."""
        if principal.kind != "person":
            return refusal("Only a person signs in with a password.", 403)
        problem = self._check_own_password(principal, password, address)
        if problem:
            return problem
        self._touch(principal.session, write=True)
        principal.inactive_s = 0.0
        self._refresh_holder(principal)
        return {"ok": True, "me": self.me(principal)}

    def change_password(self, principal: Principal, current: str,
                        new: str, address: str = "") -> dict:
        if principal.kind != "person":
            return refusal("Only a person has a password.", 403)
        problem = self._check_own_password(principal, current, address)
        if problem:
            if problem["status"] == 401:
                problem["error"] = "The current password is wrong."
            return problem
        user = self.db.user(principal.user_id)
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
        """Ask for an account with a typed username: only while no Google
        sign-in is set up. It does nothing until an admin approves it."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        if self.verifying:
            return refusal("Requests here start with a Google sign-in of your "
                           "institutional account: use Verify with Google.", 400)
        if self._signup_limited(address):
            return refusal("Too many requests from this address. Try again "
                           "later.", 429)
        name, problem = self.username_from(username)
        problem = problem or check_password(password, name)
        if problem:
            return refusal(problem, 400)
        display = " ".join(str(display_name or "").split())[:60] or name
        if self._too_many_pending():
            return refusal("Too many requests are already waiting. Ask an "
                           "admin.", 429)
        try:
            self.db.create_user(name, hash_password(password), display_name=display,
                                status="pending", note=str(note or "")[:300])
        except sqlite3.IntegrityError:
            return refusal("That username is taken.", 409)
        # Only the username, which check_username has limited to letters,
        # digits and . _ - : the rest was typed by somebody nobody has
        # vouched for yet, and the activity feed is on every screen.
        self.db.add_event(None, "info",
                          f"An account was requested ({name}). An admin "
                          "approves it in Settings -> People.")
        return {"ok": True, "pending": True, "verified": False, "username": name}

    def _signup_limited(self, address: str) -> bool:
        """Counts a request from `address`; True once it has made too many."""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._signups.get(address, []) if now - t < 3600]
            if len(recent) >= SIGNUPS_PER_HOUR:
                return True
            recent.append(now)
            self._signups[address] = recent
            return False

    def _too_many_pending(self) -> bool:
        return len([u for u in self.db.users(self.chamber)
                    if u["status"] == "pending"]) >= MAX_PENDING

    # --- the institutional address -----------------------------------------

    @property
    def verifying(self) -> bool:
        """Whether a request must prove its institutional account with Google
        before it is taken: only while a Google client is set up."""
        return self.google is not None and self.google.configured

    def email_of(self, username: str) -> str:
        return f"{username}@{self.email_domain}"

    def username_from(self, typed: str) -> tuple[str, str | None]:
        """The username in what was typed -- the name, or the whole
        institutional address -- and why it is not one, or None."""
        name = str(typed or "").strip().lower()
        if "@" in name:
            name, _, domain = name.rpartition("@")
            name = name.strip()
            if domain != self.email_domain:
                return name, (f"Use your institutional address: the part before "
                              f"@{self.email_domain}.")
        return name, check_username(name)

    def _identity(self, claims: dict) -> tuple[dict | None, str | None]:
        """The username and name Google vouches for, or why it does not."""
        settings = self.google.settings() or {}
        return google_identity(claims, settings.get("client_id"), self.email_domain)

    def google_start(self, address: str = "", password: str = "",
                     note: str = "") -> dict:
        """A request for an account, waiting for a Google sign-in: the code to
        type at Google's device page, and the token the browser polls with.
        The password and the note are given now and kept (the password
        hashed) with the sign-in; the request is made the moment Google says
        whose account it was -- so no verified identity ever waits on a
        screen for somebody else to finish."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        if not self.verifying:
            return refusal("This server has no Google sign-in set up.", 409)
        from lab import google as googlemod
        problem = check_password(password)
        if problem:
            return refusal(problem, 400)
        with self._flows_lock:
            self._prune_flows()
            crowded = len(self._flows) >= GOOGLE_FLOWS_MAX
        if crowded:
            return refusal("Too many sign-ins are waiting. Try again in a few "
                           "minutes.", 429)
        if self._google_starts.take(f"address:{address}"):
            return refusal("Too many Google sign-ins started from here. Try "
                           "again later.", 429)
        if self._google_starts_all.take("all"):
            self._google_starts.give_back(f"address:{address}")
            return refusal("Too many Google sign-ins this hour. Try again "
                           "later.", 429)

        def give_back():
            self._google_starts.give_back(f"address:{address}")
            self._google_starts_all.give_back("all")
        busy = self._hash_budget(f"addr:{address}")
        if busy:
            give_back()
            return busy
        # For "the password cannot be the username", which is only known once
        # Google says whose account it is: as a digest, in memory, folded the
        # way check_password compares.
        folded = hashlib.sha256(password.strip().lower().encode("utf-8")).hexdigest()
        try:
            hashed = hash_password(password)
            started = self.google.start()
        except googlemod.GoogleError as exc:
            give_back()
            logs.error("google sign-in start", exc, logs.WARNING)
            return refusal("Google could not be reached, or refused this panel. "
                           "Try again later, or ask an admin.", 503)
        except BaseException:
            give_back()
            raise
        token = new_token()
        now = time.monotonic()
        interval = max(GOOGLE_POLL_MIN_S, started["interval"])
        with self._flows_lock:
            self._flows[token_hash(token)] = {
                "device_code": started["device_code"], "interval": interval,
                "expires": now + min(started["expires_in"], GOOGLE_FLOW_S),
                "next": now + interval, "busy": False, "identity": None,
                "hash": hashed, "folded": folded, "note": str(note or "")[:300],
                "result": None}
        return {"ok": True, "flow": token, "user_code": started["user_code"],
                "url": started["url"],
                "expires_in": round(min(started["expires_in"], GOOGLE_FLOW_S)),
                "interval": interval}

    def google_poll(self, flow: str, address: str = "") -> dict:
        """Has the person signed in at Google yet? Google is asked at most
        once per interval, whatever the browser does; once it says who, the
        request is made, and the sign-in is gone."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        from lab import google as googlemod
        key = token_hash(str(flow or ""))
        with self._flows_lock:
            self._prune_flows()
            entry = self._flows.get(key)
            if entry is None:
                return refusal("This Google sign-in has ended: start again.", 410)
            if entry["result"] is not None:
                return dict(entry["result"])
            now = time.monotonic()
            if entry["busy"] or (entry["identity"] is None and now < entry["next"]):
                return {"ok": True, "state": "pending"}
            entry["busy"] = True
        try:
            if entry["identity"] is None:
                try:
                    state, claims = self.google.poll(entry["device_code"])
                except googlemod.GoogleError as exc:
                    logs.error("google sign-in poll", exc, logs.WARNING)
                    if exc.permanent:
                        self._forget_flow(key)
                        return refusal("Google refused this panel's sign-in: an "
                                       "admin has to check its Google client.", 503, ended=True)
                    state, claims = "unanswered", None
                if state == "slow_down":
                    entry["interval"] += 5.0
                if state in ("pending", "slow_down", "unanswered"):
                    return {"ok": True, "state": "pending"}
                if state in ("denied", "expired"):
                    self._forget_flow(key)
                    return refusal("The Google sign-in was declined." if state == "denied"
                                   else "The Google sign-in expired: start again.",
                                   403 if state == "denied" else 410, ended=True)
                identity, problem = self._identity(claims or {})
                if problem:
                    self._forget_flow(key)
                    return refusal(problem, 403, ended=True)
                # Kept on the sign-in before anything else: Google answers a
                # device code once, and a step below may fail and be retried.
                entry["identity"] = identity
            return self._request_for(key, entry)
        finally:
            with self._flows_lock:
                entry["busy"] = False
                entry["next"] = time.monotonic() + entry["interval"]

    def _request_for(self, key: str, entry: dict) -> dict:
        """The request for the account Google proved, with the password and
        note given at the start; the sign-in is used up."""
        identity = entry["identity"]
        name = identity["username"]
        if hmac.compare_digest(entry["folded"], hashlib.sha256(
                name.encode("utf-8")).hexdigest()):
            self._forget_flow(key)
            return refusal("The password cannot be the username: start again "
                           "with another.", 400, ended=True)
        old = self.db.user_by_name(name)
        if old and not (old["kind"] == "person" and old["status"] == "pending"):
            self._forget_flow(key)
            return refusal(f"There is already an account for {name}: sign in "
                           "with it, or ask an admin.", 409, ended=True)
        if not old and self._too_many_pending():
            self._forget_flow(key)
            return refusal("Too many requests are already waiting. Ask an "
                           "admin.", 429, ended=True)
        made = self.db.create_request(name, entry["hash"], identity["name"],
                                      entry["note"], time.time(), replacing=old)
        if made is None:
            self._forget_flow(key)
            return refusal("That request changed meanwhile: start again.", 409, ended=True)
        answer = {"ok": True, "state": "done", "identity": identity,
                  "username": name, "replaced": bool(old)}
        with self._flows_lock:
            # Kept, before anything else can fail, for a poll that asks again
            # -- which must find "done", never make the request a second
            # time; the password's hash and digest go now.
            entry.update(result=answer, hash="", folded="",
                         expires=time.monotonic() + GOOGLE_DONE_S)
        # Only the username: the name came from Google, the note was typed by
        # somebody not approved yet, and the activity feed is on every screen.
        # The record is the rig's log, not the request: a full disk loses the
        # line, never the answer.
        try:
            self.db.add_event(None, "info", f"An account was requested ({name}), "
                              "verified with Google"
                              + (", replacing the request waiting under that name"
                                 if old else "")
                              + ". An admin approves it in Settings -> People.")
        except Exception as exc:
            logs.error("google sign-in record", exc)
        return answer

    def _forget_flow(self, key: str) -> None:
        with self._flows_lock:
            self._flows.pop(key, None)

    def _prune_flows(self) -> None:
        """Forget sign-ins past their time. Caller holds _flows_lock."""
        now = time.monotonic()
        for key in [k for k, v in self._flows.items()
                    if now > v["expires"] and not v["busy"]]:
            del self._flows[key]

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
        problem = self._not_admin(actor, "Pairing a tablet needs the admin role.")
        if problem:
            return problem
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
        problem = self._not_admin(actor, "Pairing a tablet needs the admin role.")
        if problem:
            return problem
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
        # Pairing again replaces the tablet's session: the old one never
        # expires, so left alone it would watch for ever from wherever it is.
        self.db.delete_sessions(user["id"])
        token = new_token()
        self.db.create_session(token_hash(token), user["id"], None, address,
                               agent)
        self.db.add_event(None, "action", f"Tablet {user['display_name']!r} "
                                          f"paired from {address or 'unknown'}",
                          user=user["username"])
        return {"ok": True, "token": token, "me": self.me(self.resolve(token))}

    # --- guests -------------------------------------------------------------

    def guest(self, address: str = "") -> dict:
        """A session that only watches, for somebody with no account: only
        while an admin allows it, and never written to the database."""
        refused = refusal("Watching without an account is not allowed here. "
                          "Sign in, or ask an admin to allow guests.", 403)
        if not self.enabled or not self._guests_on:
            return refused
        local = _loopback(address)
        keys = () if local else (f"addr:{address}",)
        wait = self._guest_starts.take(*keys) if keys else 0.0
        if wait > 0:
            seconds = math.ceil(wait)
            return refusal(f"Too many guest sessions from here. Try again in "
                           f"{seconds} s, or sign in.", 429, retry_after=seconds)
        token = new_token()
        hashed = token_hash(token)
        now = time.monotonic()
        with self._lock:
            # Asked again under the lock: switched off meanwhile is refused.
            allowed = self._guests_on
            self._prune_guests(now)
            here = sum(1 for g in self._guests.values()
                       if g["address"] == address)
            full = (len(self._guests) >= GUESTS_MAX
                    or (not local and here >= GUESTS_PER_ADDRESS))
            if allowed and not full:
                self._guests[hashed] = {"since": now, "seen": now,
                                        "address": address}
        if not allowed or full:
            if keys:
                self._guest_starts.give_back(*keys)
            if not allowed:
                return refused
            return refusal("Too many guests are watching already. Try again "
                           "later, or sign in.", 503, retry_after=60)
        return {"ok": True, "token": token,
                "me": self.me(Principal(-2, "guest", "Guest", "guest",
                                        "viewer", hashed))}

    def guest_request_wait(self, principal: Principal) -> float:
        """0 with this guest's request counted, or the seconds it must wait."""
        return self._guest_requests.take(f"guest:{principal.session}")

    def _guest(self, hashed: str) -> Principal | None:
        """The guest behind a token's hash, its poll counted; None if it is
        not one -- or no longer: guests switched off, idle, or too old."""
        now = time.monotonic()
        with self._lock:
            entry = self._guests.get(hashed)
            if entry is None:
                return None
            if (not self._guests_on or now - entry["seen"] > GUEST_IDLE_S
                    or now - entry["since"] > GUEST_SESSION_S):
                self._guests.pop(hashed, None)
                return None
            entry["seen"] = now
        return Principal(-2, "guest", "Guest", "guest", "viewer", hashed)

    def _prune_guests(self, now: float) -> None:
        """Drop the guests that stopped polling or are too old. Caller holds
        the lock."""
        self._guests = {k: v for k, v in self._guests.items()
                        if now - v["seen"] <= GUEST_IDLE_S
                        and now - v["since"] <= GUEST_SESSION_S}

    def guests_report(self) -> dict:
        with self._lock:
            self._prune_guests(time.monotonic())
            watching = len(self._guests)
        return {"allowed": self._guests_on, "watching": watching,
                "max": GUESTS_MAX}

    def set_guests(self, actor: Principal, allowed: bool) -> dict:
        """Let people watch without an account, or stop it -- which ends
        every guest session at once."""
        if not self.enabled:
            return refusal("Accounts are switched off on this server.", 400)
        problem = self._not_admin(actor, "Allowing guests needs the admin role.")
        if problem:
            return problem
        allowed = allowed is True
        if allowed:
            try:
                self.db.set_meta("guests", "on")
            except Exception as exc:
                return refusal(f"Guests stay off: the change could not be "
                               f"recorded ({exc}).", 500)
        with self._lock:
            changed = self._guests_on != allowed
            self._guests_on = allowed
            ended = 0 if allowed else len(self._guests)
            if not allowed:
                self._guests.clear()
        if changed:
            self._log(actor, "action", f"{actor.name} " + (
                "allowed guests to watch without an account" if allowed else
                f"stopped guests watching ({ended} signed out)"))
        if not allowed:
            # Shut first, recorded after: a full SD card must not keep the
            # door open. Unrecorded, it would open again at the next start.
            try:
                self.db.set_meta("guests", "off")
            except Exception as exc:
                return refusal(f"Guests are off now, but the change could not "
                               f"be recorded ({exc}): after a restart of the "
                               f"panel they would be allowed again.", 500)
        return {"ok": True, "guests": self.guests_report()}

    # --- administration -----------------------------------------------------

    def _admin(self, actor: Principal) -> bool:
        return (actor.kind in ("person", "local") and actor.at_least("admin")
                and not actor.must_change and not actor.idle())

    def _not_admin(self, actor: Principal, message: str) -> dict | None:
        """Why `actor` may not administer now, or None. An idle session is
        asked for its password -- the panel opens that dialog by itself --
        rather than told it lacks a role it has."""
        if self._admin(actor):
            return None
        if actor.kind == "person" and actor.at_least("admin"):
            if actor.must_change:
                return refusal("Choose a new password first.", 403,
                               must_change=True)
            return refusal("Your session has been idle. Enter your password "
                           "to act again.", 401, reauth=True)
        return refusal(message, 403)

    def _public(self, user: dict | None) -> dict | None:
        if not user:
            return None
        hidden = {"password_hash", "uuid"}
        return {k: v for k, v in user.items() if k not in hidden}

    def users_report(self, actor: Principal) -> dict:
        problem = self._not_admin(actor, "Managing accounts needs the admin "
                                         "role.")
        if problem:
            return problem
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
                entry["email"] = self.email_of(user["username"])
                people.append(entry)
        return {"ok": True, "people": people, "tablets": tablets,
                "requests": self.db.role_requests(self.chamber),
                "email": {"domain": self.email_domain,
                          "google": self.verifying,
                          "usable": bool(self.verifying
                                         and self.google.settings() is not None)},
                "guests": self.guests_report(),
                "you": {"username": actor.username, "role": actor.role}}

    def _target(self, actor: Principal, user_id: int,
                person: bool = True) -> tuple[dict | None, dict | None]:
        """The account an admin action is about, or the reason it is refused."""
        problem = self._not_admin(actor, "Managing accounts needs the admin "
                                         "role.")
        if problem:
            return None, problem
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

    def _as_shown(self, user: dict, created_at) -> dict | None:
        """Refuses a decision about a request that is not the one the admin
        was shown: SQLite gives a deleted request's id to the next one, and a
        request made again replaces the one waiting."""
        if created_at is None:
            return None
        try:
            same = float(created_at) == float(user["created_at"])
        except (TypeError, ValueError):
            same = False
        return None if same else refusal(
            "This request was made again since your page was loaded: look at "
            "it again before deciding.", 409)

    def approve(self, actor: Principal, user_id: int, role: str,
                created_at=None) -> dict:
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        problem = self._as_shown(user, created_at)
        if problem:
            return problem
        if user["status"] != "pending":
            return refusal("That account is not waiting for approval.", 409)
        problem = self._grantable(actor, role)
        if problem:
            return problem
        if self.verifying and not user.get("email_verified_at"):
            return refusal(f"{user['username']}'s request was not verified with "
                           "Google: ask them to request the account again (it "
                           "now starts with a Google sign-in), or, once you "
                           "have checked who they are, let them in on the "
                           "machine with tools/users.py role ... --unconfirmed.",
                           409)
        # Only if it is still exactly the request just checked: a request
        # made again in between must not be let in unseen.
        if not self.db.activate_pending(user["id"], user["uuid"]):
            return refusal("That request changed meanwhile: look at it again.", 409)
        self.db.set_role(user["id"], self.chamber, role, actor.username)
        self._log(actor, "action", f"{actor.name} approved {user['display_name']} "
                                   f"({user['username']}) as {role}")
        return {"ok": True}

    def decline(self, actor: Principal, user_id: int, created_at=None) -> dict:
        user, problem = self._target(actor, user_id)
        if problem:
            return problem
        problem = self._as_shown(user, created_at)
        if problem:
            return problem
        if user["status"] != "pending":
            return refusal("Only a request still waiting can be declined; "
                           "disable an account instead.", 409)
        if not self.db.delete_pending(user["id"], user["uuid"]):
            return refusal("That request changed meanwhile: look at it again.", 409)
        # The name only: the rest was typed by somebody nobody vouched for.
        self._log(actor, "action", f"{actor.name} declined the account request "
                                   f"of {user['username']}")
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
                       password: str, address: str = "") -> dict:
        if actor.kind != "person" or not actor.at_least("owner"):
            return refusal("Only the owner hands the chamber over.", 403)
        problem = self._not_admin(actor, "Only the owner hands the chamber "
                                         "over.")
        if problem:
            return problem
        problem = self._check_own_password(actor, password, address)
        if problem:
            return problem
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
        if actor.idle():
            return refusal("Your session has been idle. Enter your password "
                           "to act again.", 401, reauth=True)
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
            # Asked for under other circumstances: since then the account
            # may have been disabled, or given that role or a higher one --
            # approving now would re-enable a role, or demote somebody.
            if user["status"] != "active" or user["role"] is None \
                    or RANK[request["role"]] <= RANK.get(user["role"], -1):
                self.db.decide_role_request(request["id"], "withdrawn",
                                            actor.username)
                return refusal(
                    f"This request is out of date: {user['display_name']} is "
                    + (f"now {user['role']}" if user["status"] == "active"
                       and user["role"] else f"{user['status']}")
                    + ". It has been closed.", 409)
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
                "owner": bool(self.db.holders(self.chamber, "owner")),
                "email_domain": self.email_domain,
                "google_signin": self.verifying,
                # Whether the sign-in page offers "Watch as a guest".
                "guests": self._guests_on}


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
