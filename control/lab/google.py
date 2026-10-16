"""Asking for an account with Google: who is asking, proved by Google.

The panel has no domain name and no HTTPS (yet), and Google's usual "Sign in
with Google" button wants an https:// address with a name. The device flow
needs neither: the panel shows a short code, the person opens
google.com/device on their own phone or computer, signs in with their
institutional account and types the code, and Google tells the panel --
the server, not the browser -- whose account it was. The panel keeps the
address and the name for the request, and nothing else: no token, no access
to the account. Afterwards the person signs in with the username and the
password they chose; Google is not asked again.

The client (type "TVs and Limited Input devices", in a Google Cloud project
whose consent screen is Internal to the institution) lives in `google.json`
beside the database, written on the machine by `tools/users.py google set`
-- never over the network -- and kept 0600:

    {"client_id": "....apps.googleusercontent.com", "client_secret": "..."}

Standard library only: HTTPS with certificate checks, no redirects followed,
every failure reduced to a GoogleError that names Google's own error code
and never what was sent.
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

#: The file beside the database.
SETTINGS_FILE = "google.json"
#: How long one request to Google may take.
TIMEOUT_S = 10.0
#: Google's endpoints (the selftest points them at a fake).
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Who the person is: the address, whether Google checked it, the name.
SCOPES = "openid email profile"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
#: The refusals asking again cannot mend (RFC 6749 5.2): the client is wrong,
#: or the code is. Anything else -- no code at all, a cut answer, an HTML
#: page from a front end, a 429 -- is Google or the network having a moment.
PERMANENT = ("invalid_client", "unauthorized_client", "invalid_grant",
             "unsupported_grant_type", "invalid_request", "invalid_scope")


class GoogleError(RuntimeError):
    """Google could not be asked, or refused; never carries the secret.
    `permanent`: asking again will not help (a client Google does not know,
    a code already used) -- as opposed to the network or Google being down."""

    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def check_settings(settings) -> str | None:
    """Why these settings cannot be used, or None."""
    if not isinstance(settings, dict):
        return "the settings are not a JSON object"
    for key in ("client_id", "client_secret"):
        value = settings.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"no {key}"
        if not value.isascii() or any(c in value for c in "\r\n\t "):
            return f"the {key} is not plain ASCII on one line"
    return None


def read_client_file(path: str) -> dict:
    """The client from the JSON file Google Cloud gives for download
    ({"installed": {...}} or {"web": {...}}, or the two keys at the top)."""
    with open(path) as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        for section in ("installed", "web"):
            if isinstance(data.get(section), dict):
                data = data[section]
                break
    settings = {"client_id": str(data.get("client_id") or "").strip(),
                "client_secret": str(data.get("client_secret") or "").strip()}
    problem = check_settings(settings)
    if problem:
        raise ValueError(f"{path}: {problem}")
    return settings


def save_settings(path: str, settings: dict) -> None:
    """Write the settings, readable by this account only, all or nothing."""
    problem = check_settings(settings)
    if problem:
        raise ValueError(problem)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".google-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as out:
            json.dump({"client_id": settings["client_id"],
                       "client_secret": settings["client_secret"]}, out, indent=2)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Google's endpoints never redirect. urllib would follow a 301-303 (as a
    GET, without the form) to wherever it pointed: refused, so nothing of the
    panel's goes anywhere but Google."""

    def redirect_request(self, *args, **kwargs):
        return None


def _plain(text) -> str:
    """Google's own error codes are words and underscores; anything else in
    a reply is dropped rather than repeated."""
    text = str(text or "")
    return text[:40] if text and text.replace("_", "").isalnum() else ""


def _post(url: str, fields: dict) -> tuple[int, dict]:
    """POST a form to Google: (HTTP status, its JSON answer)."""
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(), method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    opener = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            answer = json.loads(response.read() or b"{}")
            return response.status, answer if isinstance(answer, dict) else {}
    except urllib.error.HTTPError as exc:
        try:
            answer = json.loads(exc.read() or b"{}")
        except Exception:                 # a cut, a timeout, not JSON
            answer = {}
        return exc.code, answer if isinstance(answer, dict) else {}
    except Exception as exc:
        raise GoogleError(f"could not reach Google: {type(exc).__name__}") from None


def claims_of(id_token) -> dict:
    """The claims of an ID token, unchecked: it came from Google's token
    endpoint over verified TLS, straight to this server, so its signature
    proves nothing more (OpenID Connect Core 3.1.3.7); its fields are still
    checked by whoever uses them (lab/auth.py)."""
    try:
        part = str(id_token).split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except (IndexError, ValueError, TypeError):
        raise GoogleError("Google's answer did not say who it was",
                          permanent=True) from None
    if not isinstance(claims, dict):
        raise GoogleError("Google's answer did not say who it was", permanent=True)
    return claims


class Google:
    """The device flow, through the client in `path`; read on every use, so
    setting it up on the machine needs no restart."""

    def __init__(self, path: str | None):
        self.path = path

    def settings(self) -> dict | None:
        if not self.path:
            return None
        try:
            with open(self.path) as fh:
                settings = json.load(fh)
        except (OSError, ValueError):
            return None
        return None if check_settings(settings) else settings

    @property
    def configured(self) -> bool:
        """Whether a client file is there -- usable or not. A file that
        cannot be read, or is broken, still means requests must be verified:
        it fails closed (no sign-in starts) rather than open (requests taken
        unchecked)."""
        return bool(self.path) and os.path.lexists(self.path)

    def _client(self) -> dict:
        settings = self.settings()
        if settings is None:
            raise GoogleError(f"the Google client file cannot be used "
                              f"({SETTINGS_FILE}): see tools/users.py google",
                              permanent=True)
        return settings

    def start(self) -> dict:
        """A new code for a person to type at Google's device page."""
        settings = self._client()
        status, answer = _post(DEVICE_URL, {"client_id": settings["client_id"],
                                            "scope": SCOPES})
        if status != 200:
            code = _plain(answer.get("error"))
            raise GoogleError(f"Google refused to start a sign-in (HTTP {status}"
                              f"{', ' + code if code else ''})",
                              permanent=code in PERMANENT)
        try:
            started = {"device_code": str(answer["device_code"]),
                       "user_code": str(answer["user_code"]),
                       "url": str(answer.get("verification_url")
                                  or answer["verification_uri"]),
                       "expires_in": float(answer.get("expires_in", 1800)),
                       "interval": float(answer.get("interval", 5))}
        except (KeyError, TypeError, ValueError):
            raise GoogleError("Google's answer was not a sign-in code") from None
        if not started["url"].startswith("https://"):
            raise GoogleError("Google's answer named no https:// page")
        return started

    def poll(self, device_code: str) -> tuple[str, dict | None]:
        """Has the person signed in yet? ("pending" | "slow_down" | "denied" |
        "expired" | "ok", the ID token's claims when "ok")."""
        settings = self._client()
        status, answer = _post(TOKEN_URL, {
            "client_id": settings["client_id"],
            "client_secret": settings["client_secret"],
            "device_code": device_code, "grant_type": GRANT})
        if status == 200:
            return "ok", claims_of(answer.get("id_token"))
        code = _plain(answer.get("error"))
        known = {"authorization_pending": "pending", "slow_down": "slow_down",
                 "access_denied": "denied", "expired_token": "expired"}
        if code in known:
            return known[code], None
        # A client Google does not know, a code it will not take: asking
        # every few seconds will not mend it. Anything else is worth asking
        # again at the next interval.
        raise GoogleError(f"Google refused the sign-in (HTTP {status}"
                          f"{', ' + code if code else ''})",
                          permanent=code in PERMANENT)


def now() -> float:
    """The wall clock, for an ID token's expiry (replaceable in the tests)."""
    return time.time()
