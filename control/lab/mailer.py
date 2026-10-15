"""Mail from the panel: the code that proves an institutional address.

A person asking for an account gives the username of their institutional
email; the panel sends a code to that address, and typing it back proves
both that the mailbox exists and that it is theirs. Nothing else is mailed.

The settings live in `mail.json` beside the database, written on the machine
by `tools/users.py mail set` -- never over the network, since they hold the
sending account's password -- and kept 0600. Without the file the panel
sends nothing, and says so where an admin approves accounts.

    {"host": "smtp.gmail.com", "port": 587, "security": "starttls",
     "username": "lab@example.org", "password": "...",
     "sender": "Vacuum Lab <lab@example.org>"}

Standard library only: smtplib over TLS (STARTTLS on 587, or SSL on 465),
certificates checked. "none" is for a relay on the same machine and for the
selftest's fake server.
"""

from __future__ import annotations

import email.message
import email.utils
import json
import os
import smtplib
import ssl
import tempfile

#: The file beside the database.
SETTINGS_FILE = "mail.json"
#: How long one sending may take before it is given up.
SEND_TIMEOUT_S = 15.0
SECURITY = ("starttls", "ssl", "none")


class MailError(RuntimeError):
    """The mail could not be sent; the message never carries the password."""


def check_settings(settings: dict) -> str | None:
    """Why these settings cannot be used, or None."""
    if not isinstance(settings, dict):
        return "the settings are not a JSON object"
    if not str(settings.get("host") or "").strip():
        return "no host"
    port = settings.get("port")
    if not isinstance(port, int) or isinstance(port, bool) or not 0 < port < 65536:
        return "the port is not a number from 1 to 65535"
    if settings.get("security") not in SECURITY:
        return f"security is one of {', '.join(SECURITY)}"
    # smtplib sends the login in ASCII: anything else fails at every
    # sending, with an error that quotes the password's own characters.
    for key in ("username", "password"):
        if not str(settings.get(key) or "").isascii():
            return f"the {key} has characters other than ASCII"
    sender = str(settings.get("sender") or "")
    if "@" not in email.utils.parseaddr(sender)[1]:
        return "no sender address"
    if any(c in sender for c in "\r\n"):
        return "the sender has a line break in it"
    return None


def save_settings(path: str, settings: dict) -> None:
    """Write the settings, readable by this account only, all or nothing."""
    problem = check_settings(settings)
    if problem:
        raise ValueError(problem)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=folder, prefix=".mail-")
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as out:
            json.dump(settings, out, indent=2)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


class Mailer:
    """Sends through the account in `path`; read on every use, so setting it
    up on the machine needs no restart."""

    def __init__(self, path: str | None):
        self.path = path

    def settings(self) -> dict | None:
        """The usable settings, or None (no file, or a broken one)."""
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
        return self.settings() is not None

    def send(self, to: str, subject: str, body: str) -> None:
        settings = self.settings()
        if settings is None:
            raise MailError("this server has no mail account set up")
        if any(c in to + subject for c in "\r\n"):
            raise MailError("a line break in the address or the subject")
        message = email.message.EmailMessage()
        message["From"] = settings["sender"]
        message["To"] = to
        message["Subject"] = subject
        message["Date"] = email.utils.formatdate(localtime=True)
        message["Message-ID"] = email.utils.make_msgid()
        message.set_content(body)
        host, port = settings["host"], settings["port"]
        context = ssl.create_default_context()
        try:
            if settings["security"] == "ssl":
                server = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_S,
                                          context=context)
            else:
                server = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_S)
            with server:
                if settings["security"] == "starttls":
                    server.starttls(context=context)
                if settings.get("username"):
                    server.login(settings["username"],
                                 str(settings.get("password") or ""))
                server.send_message(message)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(f"the mail server refused the account "
                            f"({exc.smtp_code})") from None
        except smtplib.SMTPRecipientsRefused:
            raise MailError(f"the mail server refused the address {to}") from None
        except Exception as exc:
            # The type only: an error's own text may quote what was sent --
            # an encoding error quotes the password it could not encode.
            raise MailError(f"could not send through {host}:{port}: "
                            f"{type(exc).__name__}") from None
