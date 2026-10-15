#!/usr/bin/env python3
"""The panel in a real browser: every page, every role, both screen sizes.

Builds the whole simulated rig in this process (as `webserver.py
--simulate` would, with accounts on), signs in a viewer, an operator, an
admin and a paired tablet at 1024x600 (the kiosk tablet) and 390x844 (a
phone), each in a browser context of its own, and walks every tab and
sub-tab. After every step it asks: did anything throw or fail to load? does
the page scroll sideways? does it say "NaN" or "undefined"? is every button
at least 40 px tall to a finger? Then the flows that matter on the rig: who
may press what, the override dialog, a guided prompt and its Done, a drive
fault through to its acknowledged alarm, the idle prompt, the reload after
a deploy, a cleared recipe field, a run opened from History (at its top,
its operator shown and never typed), the poll rate and the alarm sound left
running -- and the curve maths run in the browser against the vectors the
selftest runs in Python. The database starts with a few recorded runs, so
History lays out a table and trends as it does on the lab PC.

    ./tools/uitest.py                 everything, in about a minute
    ./tools/uitest.py --only pages    one part (repeatable); see PARTS
    ./tools/uitest.py --shots DIR     keep a screenshot of every page
    ./tools/uitest.py --attach 9444   audit the page open in the kiosk's
                                      WebView (after `adb forward tcp:9444
                                      localabstract:<its DevTools socket>`)

Needs Chrome or Chromium (`google-chrome-stable` on the laptop). Controls
are found by their `data-test` attribute, never by their English text, so
the Spanish interface will not break this. Waits are on the panel's own
poll counter, not on sleeps.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from tools.cdp import Chrome, Page, attach  # noqa: E402

PARTS = ("pages", "maths", "gating", "override", "prompt", "fault", "idle",
         "deploy", "form", "history", "signup")
SIZES = {"tablet": (1024, 600, False), "phone": (390, 844, True)}
ROLES = ("viewer", "operator", "admin", "tablet")
PASSWORD = "uitest-pass-1"
#: The smallest a button may be under a finger (styles.css, pointer: coarse).
TOUCH_PX = 40
#: The panel's poll period; the test waits in polls, not seconds.
POLL_S = 2.0

#: Loaded before the panel's own code in every page: counts the state polls,
#: keeps the requests that failed, and keeps every AudioContext made.
INSTRUMENT = r"""
(() => {
  const ui = window.__ui = { polls: 0, failed: [], audio: [], loaded: Date.now() };
  const real = window.fetch.bind(window);
  window.fetch = async (input, init) => {
    const url = String(input && input.url || input);
    const method = (init && init.method) || "GET";
    try {
      const response = await real(input, init);
      if (url.includes("/api/state") && response.ok) ui.polls += 1;
      // A GET that fails is a broken page; a POST refused (401 re-auth,
      // 403, 409 busy or interlocked) is the server saying no, which the
      // flows drive on purpose.
      if (response.status >= 500 || (method === "GET" && response.status >= 400
          && response.status !== 401)) {
        ui.failed.push(`${response.status} ${method} ${url}`);
      }
      return response;
    } catch (error) {
      if (!(error && error.name === "AbortError")) ui.failed.push(`network ${method} ${url}`);
      throw error;
    }
  };
  for (const name of ["AudioContext", "webkitAudioContext"]) {
    const Real = window[name];
    if (!Real) continue;
    window[name] = class extends Real {
      constructor(...args) { super(...args); ui.audio.push(this); }
    };
  }
})();
"""

#: What each page is checked for, run in the page.
AUDIT = r"""
(() => {
  const text = document.body ? document.body.innerText : "";
  const bad = [...text.matchAll(/.{0,30}\b(?:NaN|undefined)\b.{0,30}/g)].map((m) => m[0]);
  // Against the screen's width (clientWidth), not innerWidth: a mobile
  // browser -- the kiosk's WebView, the phone here -- widens its layout
  // viewport to fit what overflows, and innerWidth grows with it.
  const screenW = document.documentElement.clientWidth;
  const sideways = document.documentElement.scrollWidth > screenW + 1
    || document.body.scrollWidth > screenW + 1;
  const coarse = matchMedia("(pointer: coarse)").matches;
  const small = coarse ? [...document.querySelectorAll("button, a.btn, select")]
    .filter((e) => e.offsetParent && !e.closest("[data-test-skip-size]"))
    .map((e) => [e, e.getBoundingClientRect()])
    .filter(([e, r]) => r.height > 0 && r.height < %(touch)s - 0.5)
    .map(([e, r]) => `${(e.dataset.test || (e.tagName === "SELECT"
      ? e.options[e.selectedIndex]?.text : e.innerText) || e.value || e.tagName)
      .trim().slice(0, 30)} ${Math.round(r.height)}px`) : [];
  const failed = window.__ui ? window.__ui.failed.splice(0) : [];
  return { bad, sideways, small, failed };
})()
""" % {"touch": TOUCH_PX}


# --- the tally ---------------------------------------------------------------


class Report:
    def __init__(self):
        self.passed = 0
        self.failures: list[str] = []
        self._lock = threading.Lock()

    def check(self, label: str, ok, detail: str = "") -> bool:
        with self._lock:
            if ok:
                self.passed += 1
                print(f"  PASS  {label}" + (f"  ({detail})" if detail else ""))
            else:
                self.failures.append(label)
                print(f"  FAIL  {label}" + (f"  ({detail})" if detail else ""))
        return bool(ok)

    def guarded(self, label: str, work) -> None:
        """Run one part; an exception is a FAIL, not the end of the test."""
        try:
            work()
        except Exception as exc:                            # noqa: BLE001
            traceback.print_exc()
            self.check(f"{label} ran to its end", False,
                       f"{type(exc).__name__}: {exc}"[:160])


# --- the rig, in this process ---------------------------------------------------


class Server:
    """The simulated rig behind a real HTTP server, with accounts on."""

    def __init__(self):
        import webserver
        from lab import api, auth

        self.api = api
        self.folder = tempfile.mkdtemp(prefix="uitest-")
        self.db_path = os.path.join(self.folder, "lab.db")
        args = webserver.parse(["--simulate", "--db", self.db_path,
                                "--bind", "127.0.0.1", "--port", "0"])
        self.app = webserver.build(args)
        auth.bootstrap_owner(self.app.db, self.app.chamber["id"], "owner",
                             PASSWORD, "Owner")
        self.seed_runs()
        api.Handler.app = self.app

        class Quiet(api.Handler):
            def log_message(self, fmt, *args):
                if " 5" in (fmt % args)[-6:]:          # a 5xx is worth seeing
                    super().log_message(fmt, *args)

        # Eight panels and this test share one address. The cap on
        # connections from one client is the selftest's to check.
        api.MAX_CONNECTIONS_PER_ADDRESS = 128
        auth.SIGNUPS_PER_HOUR = 50
        self.httpd = api.Server(("127.0.0.1", 0), Quiet)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True,
                         name="uitest-http").start()
        self.owner = self.login("owner")

    def seed_runs(self) -> None:
        """Finished runs, a day apart, each with a pump-down curve: the History
        table, its trends and a run's own page get something to lay out. With
        none, a table too wide for the tablet went unseen (2026-10-14)."""
        import math
        db = self.app.db
        recipes = [
            ("base_calibration", "completed",
             {"base_mbar": 4.0e-5, "ultimate_mbar": 3.9e-5, "time_to_base_s": 3000}),
            ("full_vacuum", "aborted", {"ultimate_mbar": 2.1e-5}),
            ("pumpdown", "completed", {"ultimate_mbar": 6.8e-6, "time_to_target_s": 900}),
            ("rate_of_rise", "completed",
             {"leak_rate_mbar_l_s": 3.2e-6, "r_squared": 0.99, "verdict": "linear"}),
            ("hold", "failed", {"ultimate_mbar": 8.1e-6}),
            ("base_calibration", "completed",
             {"base_mbar": 2.6e-5, "ultimate_mbar": 2.5e-5, "time_to_base_s": 3300}),
        ]
        first = time.time() - (len(recipes) + 1) * 86400
        for n, (procedure, status, result) in enumerate(recipes):
            run = db.create_run(
                procedure, f"{procedure.replace('_', ' ').capitalize()} after "
                "opening the chamber to change the target holder",
                {"interval_s": 30}, operator="Seeded Operator",
                notes="seeded by uitest", started_by="owner",
                chamber=self.app.chamber["id"])
            began = first + n * 86400
            for t in range(0, 3601, 30):
                spinning = t >= 300
                speed = min(1500.0, (t - 300) * 7.5) if spinning else 0.0
                p = 1000 * math.exp(-t / 40) + 2e-5 * (1 + 1500 / (t + 1))
                db.add_sample(run["id"], {
                    "t": float(t), "at": began + t, "p1": p, "p1_status": 0,
                    "p_best": p, "speed": speed, "set_speed": 1500.0,
                    "power": 80.0 if 0 < speed < 1500 else 12.0 if spinning else 0.0,
                    "current": 3.8 if 0 < speed < 1500 else 0.6,
                    "voltage": 21.75 if 0 < speed < 1500 else 23.25,
                    "station": 1, "motor": 1 if spinning else 0, "error_code": "000000",
                    "phase": "rough" if t < 300 else "spinup" if speed < 1500
                             else "basepressure"})
            db.finish_run(run["id"], status, result)
            with db._lock:
                db._db.execute("UPDATE runs SET started_at=?, created_at=?, ended_at=? "
                               "WHERE id=?", (began, began, began + 3600, run["id"]))
                db._db.commit()

    def call(self, path: str, body: dict | None = None,
             token: str | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path, method="POST" if body is not None else "GET",
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"})
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def login(self, username: str) -> str:
        code, body = self.call("/api/login", {"username": username,
                                              "password": PASSWORD})
        if code != 200:
            raise RuntimeError(f"sign-in for {username}: {code} {body}")
        return body["token"]

    def person(self, username: str, role: str) -> str:
        """An approved account with this role; its session token."""
        self.call("/api/signup", {"username": username, "name": username.title(),
                                  "password": PASSWORD})
        people = self.call("/api/users", token=self.owner)[1]["people"]
        found = next(p for p in people if p["username"] == username)
        if found["status"] == "pending":
            code, body = self.call("/api/users/approve",
                                   {"id": found["id"], "role": role}, self.owner)
            if code != 200:
                raise RuntimeError(f"approving {username}: {body}")
        return self.login(username)

    def tablet(self, label: str) -> str:
        code = self.call("/api/devices/add", {"label": label},
                         self.owner)[1]["code"]
        return self.call("/api/pair", {"code": code})[1]["token"]

    def sessions(self) -> dict[str, dict]:
        """One stored session per role, as the panel keeps it."""
        return {
            "viewer": {"person": self.person("vic", "viewer")},
            "operator": {"person": self.person("olga", "operator")},
            "admin": {"person": self.person("adam", "admin")},
            "tablet": {"device": self.tablet("uitest tablet")},
        }

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        try:
            self.app.close(budget=20)
        finally:
            shutil.rmtree(self.folder, ignore_errors=True)


# --- driving one page ---------------------------------------------------------


def open_as(page: Page, base: str, session: dict | None,
            where: str = "dashboard") -> None:
    """The panel, signed in as `session` (None: signed out), at `#where`."""
    page.on_new_document(INSTRUMENT)
    page.goto(f"{base}/?seed")
    page.wait("document.readyState === 'complete'")
    page.js("localStorage.clear()")
    if session is not None:
        page.js(f"localStorage.setItem('vacuum-lab-session', "
                f"{json.dumps(json.dumps(session))})")
    page.goto(f"{base}/?r={time.time()}#{where}")
    page.wait("window.__ui && window.__ui.polls >= 1", timeout=15)


def polls(page: Page) -> int:
    return page.js("window.__ui ? window.__ui.polls : 0") or 0


def wait_polls(page: Page, n: int = 1, timeout: float | None = None) -> bool:
    """Until `n` more state polls have been applied."""
    start = polls(page)
    return bool(page.wait(f"window.__ui.polls >= {start + n}",
                          timeout or n * POLL_S + 6))


def settle(page: Page) -> None:
    """Two animation frames: the click's redraw is on screen."""
    page.js("new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


def click(page: Page, test: str) -> bool:
    """Press the visible, enabled control `[data-test=test]`."""
    return bool(page.js(
        "((t) => { const e = [...document.querySelectorAll(`[data-test='${t}']`)]"
        ".find(x => x.offsetParent && !x.disabled); if (!e) return false;"
        " e.click(); return true; })(%s)" % json.dumps(test)))


def present(page: Page, test: str) -> dict:
    """{shown, enabled} for `[data-test=test]`."""
    return page.js(
        "((t) => { const all = [...document.querySelectorAll(`[data-test='${t}']`)]"
        ".filter(x => x.offsetParent); return {shown: all.length > 0,"
        " enabled: all.some(x => !x.disabled)}; })(%s)" % json.dumps(test)) or {}


def type_into(page: Page, test: str, value: str) -> bool:
    """Set a field the way typing does, so the panel's onInput sees it."""
    return bool(page.js(
        "((t, v) => { const e = document.querySelector(`[data-test='${t}']`);"
        " if (!e) return false; const proto = e.tagName === 'TEXTAREA'"
        " ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
        " Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, v);"
        " e.dispatchEvent(new Event('input', {bubbles: true}));"
        " e.dispatchEvent(new Event('change', {bubbles: true})); return true; })"
        "(%s, %s)" % (json.dumps(test), json.dumps(value))))


def audit(page: Page, report: Report, label: str, shots: str | None = None) -> None:
    settle(page)
    found = page.js(AUDIT) or {}
    problems = page.problems()
    if shots:
        page.screenshot(os.path.join(shots, re.sub(r"[^\w.-]+", "_", label) + ".png"))
    report.check(f"{label}: nothing thrown, logged as an error or failed to load",
                 not problems and not found.get("failed"),
                 "; ".join(problems + found.get("failed", []))[:200])
    report.check(f"{label}: no sideways scroll", not found.get("sideways"))
    report.check(f"{label}: no NaN or undefined on screen", not found.get("bad"),
                 "; ".join(found.get("bad", []))[:160])
    report.check(f"{label}: every button at least {TOUCH_PX} px to a finger",
                 not found.get("small"), ", ".join(found.get("small", []))[:160])


def tabs(page: Page, selector: str) -> list[str]:
    return page.js(
        "[...document.querySelectorAll(%s)].filter(e => e.offsetParent)"
        ".map(e => e.dataset.test)" % json.dumps(selector)) or []


def sweep(chrome: Chrome, server: Server, role: str, size: str, session: dict,
          report: Report, shots: str | None) -> None:
    width, height, mobile = SIZES[size]
    page = chrome.page(width, height, mobile)
    open_as(page, server.base, session)
    label = f"{role}@{size}"
    for tab in tabs(page, "nav.tabs:not(.subtabs) [data-test^='tab-']"):
        click(page, tab)
        settle(page)
        audit(page, report, f"{label} {tab}", shots)
        for sub in tabs(page, "nav.subtabs [data-test^='subtab-']"):
            click(page, sub)
            audit(page, report, f"{label} {tab}/{sub}", shots)
    click(page, "tab-dashboard")
    before, start = polls(page), time.monotonic()
    time.sleep(4 * POLL_S + 0.5)
    rate = (polls(page) - before) / (time.monotonic() - start) * POLL_S
    report.check(f"{label}: one state poll every {POLL_S:.0f} s",
                 0.75 <= rate <= 1.25, f"{rate:.2f} per period")
    running = page.js("window.__ui.audio.filter(c => c.state === 'running').length")
    report.check(f"{label}: no alarm sound left running", running == 0,
                 f"{running} AudioContext running")


# --- the parts ------------------------------------------------------------------


def part_pages(chrome, server, sessions, report, shots):
    """Every page, every role, both sizes, and the signed-out page."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, None, "dashboard")
    page.wait("document.querySelector(\"[data-test='signin-submit']\")")
    audit(page, report, "signed out", shots)
    threads = []
    for size in SIZES:
        for role in ROLES:
            work = (lambda r=role, s=size: report.guarded(
                f"the {r}@{s} sweep",
                lambda: sweep(chrome, server, r, s, sessions[r], report, shots)))
            threads.append(threading.Thread(target=work, name=f"{role}@{size}"))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def part_maths(chrome, server, sessions, report, shots):
    """The panel's curve maths against the same vectors as the selftest."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["viewer"])
    with open(os.path.join(ROOT, "tools", "checks", "analysis_vectors.json")) as fh:
        vectors = json.load(fh)
    results = page.js("""(async (v) => {
      const A = await import('/analysis.js');
      const close = (a, b) => (a === null || b === null) ? a === b
        : Math.abs(a - b) <= v.tolerance * Math.max(1, Math.abs(b));
      const out = [];
      for (const c of v.rate) {
        const got = A.latestRate(c.points.map(([t, p]) => ({t, p_best: p})), c.window_s);
        out.push([`rate: ${c.name}`, close(got, c.expected), `${got} against ${c.expected}`]);
      }
      const full = v.full_speed.map((c) => A.atFullSpeed(c.speed, c.nominal));
      out.push(['full speed: 95 % is full, a missing speed is unknown',
                full.every((x, i) => x === v.full_speed[i].expected), JSON.stringify(full)]);
      for (const c of v.linear_fit) {
        const got = A.linearFit(c.points);
        const ok = c.slope === null ? got === null : got !== null && close(got.slope, c.slope)
          && close(got.intercept, c.intercept) && close(got.r2, c.r_squared);
        out.push([`fit: ${c.name}`, ok, JSON.stringify(got)]);
      }
      return out;
    })(__VECTORS__)""".replace("__VECTORS__", json.dumps(vectors)))
    for label, ok, detail in results:
        report.check(f"in the browser, {label}", ok, detail)
    _, state = server.call("/api/state", token=sessions["viewer"]["person"])
    taken = page.js("import('/analysis.js').then(A => A.JUDGEMENT)")
    report.check("the panel judges with the server's thresholds",
                 taken == state.get("judgement"), str(taken)[:120])


def part_gating(chrome, server, sessions, report, shots):
    """Who may press what: the panel greys out what the server would refuse."""
    seen = {}
    for role in ("viewer", "operator", "admin"):
        page = chrome.page(*SIZES["tablet"])
        open_as(page, server.base, sessions[role], "tests")
        page.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
        if click(page, "run-new"):
            page.wait("document.querySelector(\"[data-test='run-start']\")")
        seen[role] = present(page, "run-start")
        open_as(page, server.base, sessions[role], "equipment/pump")
        page.wait("document.querySelector(\"[data-test='pump-on']\")")
        seen[role + "-pump"] = present(page, "pump-on")
        audit(page, report, f"gating {role} on the pump page", shots)
    report.check("a viewer cannot start a run", not seen["viewer"].get("enabled"),
                 str(seen["viewer"]))
    report.check("an operator can", seen["operator"].get("enabled"),
                 str(seen["operator"]))
    report.check("a viewer cannot switch the station on",
                 not seen["viewer-pump"].get("enabled"), str(seen["viewer-pump"]))
    report.check("an operator can", seen["operator-pump"].get("enabled"),
                 str(seen["operator-pump"]))


def part_override(chrome, server, sessions, report, shots):
    """An admin's override names the one rule it waives, with a reason."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["admin"], "equipment/pump")
    # The chamber is at atmosphere: the turbopump start is refused, and an
    # admin is offered the way past it.
    shown = page.wait("[...document.querySelectorAll(\"[data-test='override-open']\")]"
                      ".some(e => e.offsetParent)", timeout=10)
    report.check("an admin is offered Override… beside a refused start", shown)
    if not shown:
        return
    refused = server.app.registry.get("pump").interlocks()
    rule = next((r.get("rule") for a, r in refused.items()
                 if not r.get("allowed") and r.get("rule")), None)
    click(page, "override-open")
    dialog = page.wait("document.querySelector(\"[data-test='override-dialog']\")"
                       " && document.querySelector(\"[data-test='override-dialog']\").innerText")
    report.check("the dialog names the rule it waives",
                 dialog and rule and rule in dialog, f"{rule}: {(dialog or '')[:90]}")
    confirm = present(page, "override-confirm")
    report.check("it cannot be sent without a reason and the check",
                 confirm.get("shown") and not confirm.get("enabled"), str(confirm))
    type_into(page, "override-reason", "uitest: looking at the dialog only")
    page.js("document.querySelector(\"[data-test='override-checked']\").click()")
    settle(page)
    report.check("with both it can", present(page, "override-confirm").get("enabled"))
    click(page, "override-cancel")
    settle(page)
    report.check("cancelling closes it and sends nothing",
                 not present(page, "override-dialog").get("shown")
                 and not server.app.registry.get("pump").snapshot()
                 .get("setpoints", {}).get("MotorPump"))
    audit(page, report, "override dialog", shots)
    viewer = chrome.page(*SIZES["tablet"])
    open_as(viewer, server.base, sessions["operator"], "equipment/pump")
    wait_polls(viewer)
    report.check("an operator is never offered Override…",
                 not present(viewer, "override-open").get("shown"))


def part_prompt(chrome, server, sessions, report, shots):
    """A guided run's prompt is shown, and Done carries it on."""
    token = sessions["operator"]["person"]
    gate = server.app.engine.hardware.gate
    driver, gate.driver = gate.driver, None      # by hand, as on the rig today
    code, body = server.call("/api/run/start", {
        "procedure": "rate_of_rise", "confirm": True,
        "config": {"mode": "guided", "settle_s": 0, "duration_s": 30,
                   "interval_s": 1, "volume_l": 10}}, token)
    if not report.check("a guided rate-of-rise starts", code == 200,
                        str(body.get("error", ""))[:120]):
        gate.driver = driver
        return
    try:
        page = chrome.page(*SIZES["tablet"])
        open_as(page, server.base, sessions["operator"], "tests")
        shown = page.wait("document.querySelector(\"[data-test='prompt']\")"
                          " && document.querySelector(\"[data-test='prompt']\").innerText",
                          timeout=15)
        report.check("the prompt asks for the manual step", shown,
                     (shown or "")[:100])
        audit(page, report, "guided prompt", shots)
        click(page, "prompt-done")
        gone = page.wait("!document.querySelector(\"[data-test='prompt']\")",
                         timeout=10)
        report.check("Done clears it", gone)
        phase = server.app.engine.status().get("phase")
        report.check("and the run carries on past isolating the chamber",
                     phase in ("settle", "measuring", "done"), str(phase))
    finally:
        server.app.engine.stop("uitest")
        _wait(lambda: not server.app.engine.status()["running"], 15)
        gate.driver = driver


def part_fault(chrome, server, sessions, report, shots):
    """A drive fault raises an alarm on every screen; it is acknowledged."""
    from tools import fake_tc600
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "dashboard")
    fake_tc600.STATE[303] = "Err006"
    try:
        shown = page.wait("document.querySelector(\"[data-test='alarm-bar']\")"
                          " && document.querySelector(\"[data-test='alarm-bar']\").innerText",
                          timeout=20)
        report.check("a drive fault reaches the alarm bar", shown and "Err006" in shown,
                     (shown or "")[:100])
        title = page.js("document.title")
        report.check("and the tab's title", "!" in title or "alarm" in title.lower()
                     or "Err006" in title, title)
        audit(page, report, "alarm bar", shots)
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if not (click(page, "alarm-ack-all") or click(page, "alarm-ack")):
                break
            wait_polls(page)
        bar = page.js("(document.querySelector(\"[data-test='alarm-bar']\") || {})"
                      ".innerText || ''")
        report.check("acknowledging takes it off the bar", "Err006" not in bar,
                     bar[:100] or "the bar is gone")
    finally:
        fake_tc600.STATE[303] = "000000"
        _wait(lambda: not (server.app.supervisor.recovering or {}).get("active"), 30)


def part_idle(chrome, server, sessions, report, shots):
    """An idle session watches, and asks for the password before it acts."""
    auth = server.app.auth
    token = server.person("ida", "operator")
    saved = auth.idle_s
    auth.idle_s = 3.0                     # one rig per process: shorten it here
    try:
        page = chrome.page(*SIZES["tablet"])
        open_as(page, server.base, {"person": token}, "equipment/pump")
        page.wait("document.querySelector(\"[data-test='pump-on']\")")
        time.sleep(auth.idle_s + 1)
        wait_polls(page)
        station = present(page, "pump-on")
        report.check("an idle operator's controls wait for the password",
                     station.get("shown") and not station.get("enabled"),
                     str(station))
        click(page, "reauth-open")
        asked = page.wait("document.querySelector(\"[data-test='reauth-password']\")",
                          timeout=10)
        report.check("Enter password asks for it", asked)
        audit(page, report, "idle prompt", shots)
        if asked:
            # Back to the normal limit: three seconds would idle the session
            # again before the panel's next poll could show it active.
            auth.idle_s = saved
            type_into(page, "reauth-password", PASSWORD)
            click(page, "reauth-submit")
            closed = page.wait("!document.querySelector(\"[data-test='reauth-password']\")",
                               timeout=10)
            wait_polls(page)
            # What the password changes: the panel leaves "watching only",
            # and the server no longer calls the session idle. (Whether the
            # button is enabled also depends on who holds control -- another
            # part's operator may, for up to the 15 s lease.)
            watching = present(page, "reauth-open").get("shown")
            idle_now = server.call("/api/state", token=token)[1] \
                .get("me", {}).get("idle")
            report.check("the right password lets the operator act again",
                         closed and not watching and not idle_now,
                         f"dialog closed {bool(closed)}, still watching only "
                         f"{watching}, server says idle {idle_now}")
    finally:
        auth.idle_s = saved


def part_deploy(chrome, server, sessions, report, shots):
    """New files on the server: open panels reload by themselves."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["viewer"])
    loaded = page.js("window.__ui.loaded")
    original = server.app.build
    server.app.build = "uitest-new-build"
    try:
        reloaded = page.wait(f"window.__ui && window.__ui.loaded !== {loaded}"
                             " && window.__ui.polls >= 1", timeout=12)
        report.check("a new build reloads an open panel at the next poll",
                     reloaded)
    finally:
        server.app.build = original


def part_form(chrome, server, sessions, report, shots):
    """A cleared recipe field stops the start; it is never sent as 0."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "tests")
    page.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
    if click(page, "run-new"):
        page.wait("document.querySelector(\"[data-test='run-start']\")")
    # The form fills in its defaults just after it appears: a field cleared
    # before that is filled in again, and Start rightly stays enabled.
    page.wait("[...document.querySelectorAll(\"input[data-test^='recipe-field-']\")]"
              ".every(e => e.value !== '')", timeout=10)
    field = page.js("(() => { const e = document.querySelector("
                    "\"[data-test^='recipe-field-'] input[type='number'],"
                    " input[data-test^='recipe-field-'][type='number']\");"
                    " return e ? e.dataset.test || e.closest('[data-test]').dataset.test"
                    " : null; })()")
    if not report.check("the recipe form has a numeric field", field, str(field)):
        return
    before = present(page, "run-start")
    page.js("((t) => { const box = document.querySelector(`[data-test='${t}']`);"
            " const e = box.tagName === 'INPUT' ? box : box.querySelector('input');"
            " Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')"
            ".set.call(e, ''); e.dispatchEvent(new Event('input', {bubbles: true}));"
            " })(%s)" % json.dumps(field))
    settle(page)
    after = present(page, "run-start")
    report.check(f"clearing {field} disables Start",
                 before.get("enabled") and not after.get("enabled"),
                 f"before {before}, after {after}")
    audit(page, report, "recipe form with a cleared field", shots)


def part_history(chrome, server, sessions, report, shots):
    """Who ran a run is the account that started it; a run opens at its top."""
    page = chrome.page(*SIZES["tablet"])
    open_as(page, server.base, sessions["operator"], "tests")
    page.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
    if click(page, "run-new"):
        page.wait("document.querySelector(\"[data-test='run-start']\")")
    shown = page.js("""(() => { const e = document.querySelector("[data-test='run-operator']");
      return e ? {text: e.innerText.trim(), typed: e.tagName === 'INPUT'
        || e.closest('.field').querySelectorAll('input, textarea').length} : null; })()""")
    report.check("the run form names the signed-in operator, and nothing can be "
                 "typed there", shown and shown["text"] == "Olga" and not shown["typed"],
                 str(shown))

    open_as(page, server.base, sessions["operator"], "history")
    listed = page.wait("document.querySelector(\"[data-test^='history-run-']\")",
                       timeout=15)
    if not report.check("History lists the recorded runs", listed):
        return
    page.wait("document.querySelectorAll('section.card svg').length > 3", timeout=10)
    page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    settle(page)
    before = page.js("window.scrollY")
    row = page.js("[...document.querySelectorAll(\"[data-test^='history-run-']\")]"
                  ".pop().dataset.test")
    click(page, row)
    opened = page.wait("document.querySelector(\"[data-test='history-close']\")",
                       timeout=10)
    page.wait("!document.body.innerText.includes('Loading the curves')", timeout=10)
    settle(page)
    top = page.js("window.scrollY")
    report.check("a run opens at its top, not where the list was scrolled",
                 opened and before > 200 and top == 0, f"list at {before}, run at {top}")
    field = page.js("""(() => { const e = document.querySelector("[data-test='history-operator']");
      return e ? {text: e.innerText.trim(), typed: e.tagName === 'INPUT'
        || e.closest('.field').querySelectorAll('input').length} : null; })()""")
    report.check("a run's operator is shown and cannot be edited",
                 field and field["text"] == "Seeded Operator (owner)"
                 and not field["typed"],
                 str(field))
    audit(page, report, "a run opened from History", shots)
    click(page, "history-close")
    page.wait("document.querySelector(\"[data-test^='history-run-']\")", timeout=10)
    settle(page)
    back = page.js("window.scrollY")
    report.check("closing it goes back to the same place in the list",
                 abs(back - before) <= 2, f"{before} -> {back}")

    # Saving a note closes the run and reloads the list: same place again.
    page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    settle(page)
    before = page.js("window.scrollY")
    click(page, row)
    page.wait("document.querySelector(\"[data-test='history-save']\")", timeout=10)
    type_into(page, "history-notes", "uitest: a note, saved")
    click(page, "history-save")
    page.wait("document.querySelector(\"[data-test^='history-run-']\")", timeout=10)
    run_id = int(row.rsplit("-", 1)[1])
    _wait(lambda: (server.app.db.run(run_id) or {}).get("notes")
          == "uitest: a note, saved", 5)
    wait_polls(page)
    settle(page)
    back = page.js("window.scrollY")
    report.check("saving a run's notes goes back to the same place in the list",
                 abs(back - before) <= 2, f"{before} -> {back}")

    # The case reported: a link to a run from low on another page.
    open_as(page, server.base, sessions["operator"], "dashboard")
    wait_polls(page)
    page.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    settle(page)
    low = page.js("window.scrollY")
    page.js(f"location.hash = 'history/{run_id}'")
    page.wait("document.querySelector(\"[data-test='history-close']\")", timeout=10)
    settle(page)
    top = page.js("window.scrollY")
    report.check("a link to a run opens it at its top, wherever the page was",
                 low > 100 and top == 0, f"dashboard at {low}, run at {top}")

    # A paired tablet watches: it is nobody who could run anything.
    tablet = chrome.page(*SIZES["tablet"])
    open_as(tablet, server.base, sessions["tablet"], "tests")
    tablet.wait("document.querySelector(\"[data-test='run-start'], [data-test='run-new']\")")
    if click(tablet, "run-new"):
        tablet.wait("document.querySelector(\"[data-test='run-start']\")")
    named = tablet.js("(document.querySelector(\"[data-test='run-operator']\") || {})"
                      ".innerText || ''").strip()
    report.check("a paired tablet's run form names no operator", named == "—",
                 repr(named))


def part_signup(chrome, server, sessions, report, shots):
    """Asking for an account from a phone: the institutional username, the
    code mailed there, and the admin seeing it confirmed."""
    from lab import mailer
    from tools.fake_smtp import FakeSMTP
    smtp = FakeSMTP()
    path = os.path.join(server.folder, mailer.SETTINGS_FILE)
    mailer.save_settings(path, smtp.settings())
    try:
        page = chrome.page(*SIZES["phone"])
        open_as(page, server.base, None, "dashboard")
        page.wait("document.querySelector(\"[data-test='signin-mode-signup']\")")
        click(page, "signin-mode-signup")
        page.wait("document.querySelector(\"[data-test='signup-username']\")")
        note = page.js("document.querySelector(\"[data-test='signup-username']\")"
                       ".closest('.field').innerText") or ""
        report.check("the sign-up form asks for the institutional username",
                     "@unal.edu.co" in note, " ".join(note.split())[:120])
        audit(page, report, "sign-up form", shots)
        type_into(page, "signup-username", "Nina.Ruiz@unal.edu.co")
        type_into(page, "signup-password", "nina-pass-1")
        type_into(page, "signup-repeat", "nina-pass-1")
        settle(page)
        click(page, "signup-submit")
        asked = page.wait("document.querySelector(\"[data-test='signup-code']\")",
                          timeout=15)
        codes = smtp.codes("nina.ruiz@unal.edu.co")
        report.check("a code is mailed to the address, and the page asks for it",
                     asked and len(codes) == 1, f"{len(codes)} mailed")
        audit(page, report, "sign-up: the code", shots)
        type_into(page, "signup-code", codes[0] if codes else "")
        settle(page)
        click(page, "signup-confirm")
        done = page.wait("document.body.innerText.includes('Address confirmed')",
                         timeout=10)
        user = server.app.db.user_by_name("nina.ruiz") or {}
        report.check("typed back, the address is confirmed",
                     done and user.get("email_verified_at"),
                     f"page {bool(done)}, stored {user.get('email_verified_at')}")
        admin = chrome.page(*SIZES["tablet"])
        open_as(admin, server.base, sessions["admin"], "settings")
        shown = admin.wait("(document.querySelector(\"[data-test='address-nina.ruiz']\")"
                           " || {}).innerText", timeout=15) or ""
        report.check("the People page shows the request's address confirmed",
                     "confirmed" in shown and "not" not in shown, shown)
        audit(admin, report, "People with a confirmed request", shots)
    finally:
        if os.path.exists(path):
            os.unlink(path)
        smtp.close()


def _wait(predicate, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


PART_FUNCTIONS = {"pages": part_pages, "maths": part_maths,
                  "gating": part_gating, "override": part_override,
                  "prompt": part_prompt, "fault": part_fault,
                  "idle": part_idle, "deploy": part_deploy, "form": part_form,
                  "history": part_history, "signup": part_signup}


# --- the kiosk -------------------------------------------------------------------


def run_attached(port: int, report: Report, shots: str | None) -> None:
    """Audit what the kiosk is showing, in place, through its WebView."""
    page = attach(port)
    page.on_new_document(INSTRUMENT)
    url = page.js("location.href")
    page.goto(re.sub(r"[?#].*$", "", url) + f"?r={time.time()}#dashboard")
    page.wait("window.__ui && window.__ui.polls >= 1", timeout=20)
    for tab in tabs(page, "nav.tabs:not(.subtabs) [data-test^='tab-']"):
        click(page, tab)
        audit(page, report, f"kiosk {tab}", shots)
        for sub in tabs(page, "nav.subtabs [data-test^='subtab-']"):
            click(page, sub)
            audit(page, report, f"kiosk {tab}/{sub}", shots)
    click(page, "tab-dashboard")
    before, start = polls(page), time.monotonic()
    time.sleep(4 * POLL_S + 0.5)
    rate = (polls(page) - before) / (time.monotonic() - start) * POLL_S
    report.check("kiosk: one state poll per period", 0.75 <= rate <= 1.25,
                 f"{rate:.2f}")
    running = page.js("window.__ui.audio.filter(c => c.state === 'running').length")
    report.check("kiosk: no alarm sound left running", running == 0, str(running))


# --- the command -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", action="append", choices=PARTS)
    parser.add_argument("--shots", metavar="DIR",
                        help="keep a screenshot of every page audited")
    parser.add_argument("--attach", type=int, metavar="PORT",
                        help="audit the page open on this DevTools port")
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)
    if args.shots:
        os.makedirs(args.shots, exist_ok=True)
    report = Report()
    started = time.monotonic()

    if args.attach:
        report.guarded("the kiosk audit",
                       lambda: run_attached(args.attach, report, args.shots))
    else:
        server = Server()
        chrome = None
        try:
            chrome = Chrome()
            sessions = server.sessions()
            for name in [p for p in PARTS if p in set(args.only or PARTS)]:
                print(f"\n{name}")
                report.guarded(name, lambda n=name: PART_FUNCTIONS[n](
                    chrome, server, sessions, report, args.shots))
                chrome.close_pages()
        finally:
            if chrome is not None:
                chrome.close()
            server.close()

    took = time.monotonic() - started
    if report.failures:
        print("\nFailed checks")
        for label in report.failures:
            print(f"  - {label}")
    print(f"\n{report.passed} passed, {len(report.failures)} failed "
          f"in {took:.0f} s")
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
