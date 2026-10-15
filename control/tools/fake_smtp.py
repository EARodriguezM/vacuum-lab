"""A mail server for the tests: takes every message and keeps it.

The panel mails a code to the institutional address of whoever asks for an
account (lab/auth.py, lab/mailer.py). The selftest and the browser test point
it here and read the code back out of the message. Plain SMTP on 127.0.0.1,
no TLS and no login -- mail.json's security "none" -- standard library only.

    server = FakeSMTP()
    mailer.save_settings(path, server.settings())
    ...
    server.codes("ana@unal.edu.co")     # every 6-digit code sent there
    server.close()
"""

from __future__ import annotations

import re
import socketserver
import threading

#: What goes wrong on purpose: {"refuse_rcpt": True} answers 550 to every
#: recipient, as a server does for an address it will not take.
FAULTS: dict = {}


class FakeSMTP:
    def __init__(self):
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        outer = self

        class Session(socketserver.StreamRequestHandler):
            def reply(self, text: str) -> None:
                self.wfile.write((text + "\r\n").encode())

            def handle(self) -> None:
                self.reply("220 fake ESMTP")
                recipients: list[str] = []
                while True:
                    line = self.rfile.readline(4096)
                    if not line:
                        return
                    command = line.decode("utf-8", "replace").strip()
                    verb = command.split(" ", 1)[0].upper()
                    if verb in ("EHLO", "HELO"):
                        self.reply("250 fake")
                    elif verb == "MAIL":
                        recipients = []
                        self.reply("250 OK")
                    elif verb == "RCPT":
                        if FAULTS.get("refuse_rcpt"):
                            self.reply("550 no such user")
                            continue
                        found = re.search(r"<([^>]*)>", command)
                        recipients.append(found.group(1) if found else "")
                        self.reply("250 OK")
                    elif verb == "DATA":
                        self.reply("354 end with a dot")
                        lines = []
                        while True:
                            raw = self.rfile.readline(65536)
                            if not raw or raw in (b".\r\n", b".\n"):
                                break
                            lines.append(raw[1:] if raw.startswith(b"..") else raw)
                        with outer._lock:
                            outer.messages.append({
                                "to": list(recipients),
                                "data": b"".join(lines).decode("utf-8", "replace")})
                        self.reply("250 kept")
                    elif verb == "QUIT":
                        self.reply("221 bye")
                        return
                    else:
                        self.reply("250 OK")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Session)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="fake-smtp").start()

    def settings(self) -> dict:
        """mail.json for this server."""
        return {"host": "127.0.0.1", "port": self.port, "security": "none",
                "username": "", "password": "",
                "sender": "Vacuum Lab <lab@example.org>"}

    def sent_to(self, address: str) -> list[str]:
        with self._lock:
            return [m["data"] for m in self.messages if address in m["to"]]

    def codes(self, address: str) -> list[str]:
        """Every 6-digit code mailed to `address`, oldest first."""
        return [code for data in self.sent_to(address)
                for code in re.findall(r"^\s+(\d{6})\s*$", data, re.MULTILINE)]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
