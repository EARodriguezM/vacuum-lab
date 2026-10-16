"""Google's device flow, for the tests: a code, a sign-in, an ID token.

lab/google.py asks Google for a code (`/device/code`), the person types it at
google.com/device on their own phone, and the panel polls `/token` until
Google says who signed in. Here the test plays the person: `approve(code,
email, name)` signs in, `deny(code)` declines. Plain HTTP on 127.0.0.1;
point lab.google's DEVICE_URL and TOKEN_URL at `device_url` and `token_url`.

It refuses what Google would: an unknown client or a wrong secret
(invalid_client), a poll before the person has signed in
(authorization_pending), polling while `slow_down` is set, a declined
sign-in (access_denied) and an expired code (expired_token).
"""

from __future__ import annotations

import base64
import http.server
import json
import secrets
import threading
import time
import urllib.parse

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-test-secret"


class FakeGoogle:
    def __init__(self, client_id: str = CLIENT_ID,
                 client_secret: str = CLIENT_SECRET):
        self.client_id, self.client_secret = client_id, client_secret
        #: device_code -> {"user_code", "claims" (None until signed in),
        #: "denied", "expired"}
        self.flows: dict[str, dict] = {}
        self.device_calls = 0
        self.token_calls = 0
        self.slow_down = False
        self.down = False             # every request answers 503
        self.interval = 5             # what it asks the panel to wait
        self.garbage = False          # a sign-in's answer is 200 and not JSON
                                      # an object (a broken proxy, say)
        self.cut_wait = False         # "still waiting" cut off half-way
        self.html_429 = False         # a front end's HTML "too many requests"
        self._lock = threading.Lock()
        outer = self

        class Google(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                form = {k: v[0] for k, v in
                        urllib.parse.parse_qs(raw.decode()).items()}
                if outer.down:
                    self.answer(503, {"error": "backend_error"})
                    return
                if self.path == "/device/code":
                    outer.device_calls += 1
                    if form.get("client_id") != outer.client_id:
                        self.answer(401, {"error": "invalid_client"})
                        return
                    device = secrets.token_urlsafe(24)
                    user = "-".join(secrets.token_hex(2).upper() for _ in range(3))
                    with outer._lock:
                        outer.flows[device] = {"user_code": user, "claims": None,
                                               "denied": False, "expired": False}
                    self.answer(200, {"device_code": device, "user_code": user,
                                      "verification_url": "https://www.google.com/device",
                                      "expires_in": 1800,
                                      "interval": outer.interval})
                    return
                if self.path == "/token":
                    outer.token_calls += 1
                    if (form.get("client_id") != outer.client_id
                            or form.get("client_secret") != outer.client_secret):
                        self.answer(401, {"error": "invalid_client",
                                          "error_description": raw.decode()})
                        return
                    with outer._lock:
                        flow = outer.flows.get(form.get("device_code", ""))
                    if flow is None or form.get("grant_type") != (
                            "urn:ietf:params:oauth:grant-type:device_code"):
                        self.answer(400, {"error": "invalid_grant"})
                    elif outer.cut_wait:
                        self.send_response(428)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", "200")
                        self.end_headers()
                        self.wfile.write(b'{"error": "authori')
                        self.close_connection = True
                    elif outer.html_429:
                        body = b"<html><body>Too many requests</body></html>"
                        self.send_response(429)
                        self.send_header("Content-Type", "text/html")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    elif outer.slow_down:
                        self.answer(403, {"error": "slow_down"})
                    elif flow["denied"]:
                        self.answer(403, {"error": "access_denied"})
                    elif flow["expired"]:
                        self.answer(400, {"error": "expired_token"})
                    elif flow["claims"] is None:
                        self.answer(428, {"error": "authorization_pending"})
                    elif outer.garbage:
                        body = b"[]"
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        claims = base64.urlsafe_b64encode(json.dumps(
                            flow["claims"]).encode()).decode().rstrip("=")
                        self.answer(200, {"access_token": "access-token",
                                          "id_token": f"e30.{claims}.signature",
                                          "expires_in": 3599, "token_type": "Bearer",
                                          "scope": "openid email profile"})
                    return
                self.answer(404, {"error": "not_found"})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Google)
        self.server.daemon_threads = True
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.device_url, self.token_url = f"{base}/device/code", f"{base}/token"
        threading.Thread(target=self.server.serve_forever, daemon=True,
                         name="fake-google").start()

    def _flow(self, user_code: str) -> dict:
        with self._lock:
            for flow in self.flows.values():
                if flow["user_code"] == user_code:
                    return flow
        raise KeyError(user_code)

    def approve(self, user_code: str, email: str, name: str = "",
                hd: str | None = "unal.edu.co", verified=True, **changes) -> None:
        """The person signs in at google.com/device and types the code.
        `changes` overrides any claim (aud, iss, exp...)."""
        claims = {"iss": "https://accounts.google.com", "aud": self.client_id,
                  "azp": self.client_id, "sub": secrets.token_hex(8),
                  "email": email, "email_verified": verified, "name": name,
                  "iat": int(time.time()), "exp": int(time.time()) + 3600}
        if hd is not None:
            claims["hd"] = hd
        claims.update(changes)
        self._flow(user_code)["claims"] = claims

    def deny(self, user_code: str) -> None:
        self._flow(user_code)["denied"] = True

    def expire(self, user_code: str) -> None:
        self._flow(user_code)["expired"] = True

    def settings(self) -> dict:
        """google.json for this fake."""
        return {"client_id": self.client_id, "client_secret": self.client_secret}

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
