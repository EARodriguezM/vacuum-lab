"""A small Chrome DevTools Protocol client, standard library only.

Drives headless Chrome for tools/uitest.py, and the kiosk tablet's WebView
through `adb forward` for its `--attach` mode. One connection carries many
pages: each browser context (a separate localStorage, so a separate person
signed in) gets its own page and session, and a reader thread hands each
reply to whoever asked and each event to its page, so several pages can be
driven from several threads at once.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request


class WebSocket:
    """Just enough RFC 6455 for DevTools: text frames, client-masked."""

    def __init__(self, url: str):
        host_port, path = url[len("ws://"):].split("/", 1)
        host, port = host_port.rsplit(":", 1)
        self.sock = socket.create_connection((host, int(port)), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /{path} HTTP/1.1\r\nHost: {host_port}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\n"
                           "Sec-WebSocket-Version: 13\r\n\r\n").encode())
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("DevTools closed the connection")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError(response.split(b"\r\n", 1)[0].decode())
        self.buffer = response.split(b"\r\n\r\n", 1)[1]
        # The handshake has its timeout; the reader waits as long as it
        # takes -- a hidden kiosk page sends nothing for minutes.
        self.sock.settimeout(None)
        self._send_lock = threading.Lock()

    def send(self, text: str) -> None:
        data = text.encode()
        header = bytearray([0x81])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        with self._send_lock:
            self.sock.sendall(bytes(header) + mask + masked)

    def _read(self, n: int) -> bytes:
        while len(self.buffer) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buffer += chunk
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def recv(self) -> str:
        message = b""
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            n = second & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            if opcode == 8:
                raise EOFError
            if opcode in (9, 10):
                continue
            message += payload
            if first & 0x80:
                return message.decode()

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class Connection:
    """One DevTools WebSocket, shared by every page on it."""

    def __init__(self, url: str):
        self.ws = WebSocket(url)
        self._next = 0
        self._lock = threading.Lock()
        self._waiting: dict[int, queue.Queue] = {}
        self._pages: dict[str | None, "Page"] = {}
        self.closed = False
        self._reader = threading.Thread(target=self._read, daemon=True,
                                        name="cdp-reader")
        self._reader.start()

    def _read(self) -> None:
        try:
            while True:
                message = json.loads(self.ws.recv())
                if "id" in message:
                    box = self._waiting.pop(message["id"], None)
                    if box is not None:
                        box.put(message)
                    continue
                page = self._pages.get(message.get("sessionId"))
                if page is not None:
                    page._event(message)
        except (EOFError, OSError, ValueError):
            self.closed = True
            for box in list(self._waiting.values()):
                box.put({"error": {"message": "connection closed"}})

    def call(self, method: str, session: str | None = None,
             timeout: float = 60.0, **params) -> dict:
        with self._lock:
            self._next += 1
            mine = self._next
            box = self._waiting[mine] = queue.Queue(maxsize=1)
        # After registering: a reader that dies from here on answers the box.
        if self.closed:
            self._waiting.pop(mine, None)
            raise ConnectionError(f"{method}: the DevTools connection is closed")
        message = {"id": mine, "method": method, "params": params}
        if session:
            message["sessionId"] = session
        self.ws.send(json.dumps(message))
        try:
            reply = box.get(timeout=timeout)
        except queue.Empty:
            self._waiting.pop(mine, None)
            raise TimeoutError(f"{method}: no reply in {timeout:.0f} s")
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def close(self) -> None:
        self.ws.close()


class Page:
    """One page: navigate, run JavaScript, collect what went wrong."""

    def __init__(self, connection: Connection, session: str | None = None,
                 context: str | None = None, target: str | None = None):
        self.connection = connection
        self.session = session
        self.context = context
        self.target = target
        #: Scripts already added to run before every document, by text.
        self.added: set[str] = set()
        self._events: list[dict] = []
        self._events_lock = threading.Lock()
        connection._pages[session] = self
        for domain in ("Runtime", "Page", "Log", "Network"):
            try:
                self.call(f"{domain}.enable")
            except RuntimeError:
                pass                     # a WebView may lack a domain

    def _event(self, message: dict) -> None:
        with self._events_lock:
            self._events.append(message)

    def call(self, method: str, timeout: float = 60.0, **params) -> dict:
        return self.connection.call(method, self.session, timeout, **params)

    def js(self, expression: str, timeout: float = 60.0):
        result = self.call("Runtime.evaluate", timeout=timeout,
                           expression=expression, returnByValue=True,
                           awaitPromise=True)
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            raise RuntimeError(details.get("exception", {}).get("description")
                               or details.get("text"))
        return result.get("result", {}).get("value")

    def goto(self, url: str) -> None:
        self.call("Page.navigate", url=url)

    def wait(self, expression: str, timeout: float = 10.0,
             step: float = 0.1):
        """Until `expression` is truthy in the page; its value, or None."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                value = self.js(expression)
            except RuntimeError:
                value = None
            if value:
                return value
            time.sleep(step)
        return None

    def size(self, width: int, height: int, mobile: bool = False,
             touch: bool = True) -> None:
        self.call("Emulation.setDeviceMetricsOverride", width=width,
                  height=height, deviceScaleFactor=1, mobile=mobile)
        if touch:
            self.call("Emulation.setTouchEmulationEnabled", enabled=True,
                      maxTouchPoints=5)

    def on_new_document(self, source: str) -> None:
        """Run `source` before the page's own scripts, from the next load on;
        once per page, however often it is asked."""
        if source in self.added:
            return
        self.call("Page.addScriptToEvaluateOnNewDocument", source=source)
        self.added.add(source)

    def problems(self) -> list[str]:
        """Exceptions, console errors and failed loads since the last call."""
        with self._events_lock:
            events, self._events = self._events, []
        found = []
        for event in events:
            method, params = event.get("method"), event.get("params", {})
            if method == "Runtime.exceptionThrown":
                details = params["exceptionDetails"]
                found.append("exception: "
                             + (details.get("exception", {}).get("description")
                                or details.get("text", "")).splitlines()[0])
            elif (method == "Runtime.consoleAPICalled"
                  and params.get("type") in ("error", "assert")):
                found.append(f"console.{params['type']}: " + " ".join(
                    str(a.get("value", a.get("description", "")))
                    for a in params.get("args", [])))
            elif method == "Log.entryAdded" and params["entry"]["level"] == "error":
                entry = params["entry"]
                # The fetches the page made are judged by uitest itself:
                # refusals (401, 409) are part of the flows it drives.
                if entry.get("source") != "network":
                    found.append(f"log: {entry.get('text')}")
            elif method == "Network.loadingFailed" and not params.get("canceled"):
                found.append(f"failed load: {params.get('errorText')}")
        return found

    def screenshot(self, path: str) -> None:
        data = self.call("Page.captureScreenshot", format="png")["data"]
        with open(path, "wb") as fh:
            fh.write(base64.b64decode(data))


class Chrome:
    """Headless Chrome, with a fresh browser context per page asked for."""

    def __init__(self, binary: str | None = None):
        binary = binary or shutil.which("google-chrome-stable") \
            or shutil.which("google-chrome") or shutil.which("chromium")
        if not binary:
            raise FileNotFoundError("no Chrome or Chromium found")
        self.profile = tempfile.mkdtemp(prefix="uitest-chrome-")
        self.process = subprocess.Popen(
            [binary, "--headless=new", "--remote-debugging-port=0",
             f"--user-data-dir={self.profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             "--disable-background-networking", "--mute-audio",
             # A coarse pointer, as on the tablet: the 40 px rule applies.
             "--blink-settings=primaryPointerType=4,availablePointerTypes=4,"
             "primaryHoverType=1,availableHoverTypes=1",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = os.path.join(self.profile, "DevToolsActivePort")
        for _ in range(150):
            if os.path.exists(port_file):
                lines = open(port_file).read().split()
                if len(lines) >= 2:
                    break
            time.sleep(0.1)
        else:
            self._stop()
            raise RuntimeError("Chrome did not start")
        self.pages: list[Page] = []
        try:
            self.connection = Connection(f"ws://127.0.0.1:{lines[0]}{lines[1]}")
        except Exception:
            self._stop()
            raise

    def page(self, width: int = 1024, height: int = 600,
             mobile: bool = False) -> Page:
        context = self.connection.call("Target.createBrowserContext")[
            "browserContextId"]
        target = self.connection.call("Target.createTarget", url="about:blank",
                                      browserContextId=context)["targetId"]
        session = self.connection.call("Target.attachToTarget",
                                       targetId=target, flatten=True)["sessionId"]
        page = Page(self.connection, session, context, target)
        page.size(width, height, mobile)
        self.pages.append(page)
        return page

    def close_pages(self) -> None:
        """Close every page opened so far, and its browser context: a page
        left open keeps polling the server for the rest of the run."""
        while self.pages:
            page = self.pages.pop()
            for method, params in (("Target.closeTarget", {"targetId": page.target}),
                                   ("Target.disposeBrowserContext",
                                    {"browserContextId": page.context})):
                try:
                    self.connection.call(method, timeout=10, **params)
                except Exception:                           # noqa: BLE001
                    pass
            self.connection._pages.pop(page.session, None)

    def close(self) -> None:
        try:
            self.connection.call("Browser.close", timeout=5)
        except Exception:                                   # noqa: BLE001
            pass
        self._stop()

    def _stop(self) -> None:
        """The process ended and its profile removed, whatever state it is in."""
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
        shutil.rmtree(self.profile, ignore_errors=True)


def attach(port: int = 9444, match: str = "") -> Page:
    """The page already open in a WebView or browser on `port` -- the kiosk
    tablet's, after `adb forward tcp:9444 localabstract:<its socket>`."""
    targets = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{port}/json", timeout=10))
    pages = [t for t in targets if t.get("type") == "page"
             and match in t.get("url", "")]
    if not pages:
        raise RuntimeError(f"no page on port {port}"
                           + (f" matching {match!r}" if match else ""))
    return Page(Connection(pages[0]["webSocketDebuggerUrl"]))
